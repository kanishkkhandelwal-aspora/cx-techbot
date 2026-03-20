"""9-category Haiku classifier using Claude LLM."""

import json
import logging
import time
from dataclasses import dataclass, field

import anthropic

from classifier.extractor import extract_ids
from classifier.fallback import keyword_classify

logger = logging.getLogger(__name__)

CLASSIFIER_SYSTEM_PROMPT = """You are a message classifier for Aspora's #cx-tech-queries Slack channel. Classify each message into exactly ONE of 9 categories.

## Categories

1. **payment_error_diagnosis** — ANY issue where the customer tried to make a payment/transaction/transfer and it FAILED, got stuck, was declined, timed out, or didn't go through. This includes: card failures, UPI failures, 3DS issues, payment timeouts, acquirer rejections, "unable to complete transaction", "money deducted but transfer failed", "transaction not going through", payment attempt errors.
   Signals: "payment failed", "transaction declined", "3DS", "card not working", "PA-" IDs, "EXCEEDS_DAILY_LIMIT", "unable to transact", "transaction failed", "transaction not going through", "payment error", "payment stuck", "payment not working", "could not complete", "debit failed", "money deducted but failed", bank names + failure, "debit card", "credit card", "UPI"

2. **kyc_verification** — KYC pending/stuck/rejected, document verification failures, compliance service errors, identity verification, PEP/sanctions screening, "unable to complete KYC".
   Signals: "KYC", "verification", "document rejected", "compliance", "identity", "Onfido", "Persona", "Sumsub", "PEP", "sanctions", "verification stuck"

3. **db_lookup_status** — ONLY when the CX agent is asking to CHECK or LOOK UP a status, NOT when the customer has an actual failure. This is for status discrepancies between systems (AlphaDesk vs Falcon), refund tracking, CNR (Credit Not Received where money WAS sent but partner hasn't confirmed), fulfillment tracking.
   Signals: "status of order", "check status", "what is the status", "refund status", "AlphaDesk shows X but Falcon shows Y", "CNR", "not syncing between systems", "fulfillment status", "order stuck in processing"
   NOT this category: "payment failed", "unable to transact", "transaction error" → those go to payment_error_diagnosis

4. **referral_promo** — Referral rewards not credited, promo code not working, cashback missing, campaign/offer issues.
   Signals: "referral", "promo code", "cashback", "reward not credited", "offer", "campaign", "first transaction bonus"

5. **bbps_partner_escalation** — BBPS bill payment failures, partner-side issues (Checkout.com, LULU, banking partners), webhook failures from partners, corridor-level outages affecting multiple customers.
   Signals: "BBPS", "bill payment", "Checkout.com", "LULU partner", "partner down", "webhook failure", "corridor down", "all customers affected", "partner payout failed"

6. **manual_backend_action** — State change requests, mobile number updates, manual DB corrections, account unlocks — anything requiring production database write access.
   Signals: "change state", "update mobile", "mobile number change", "unlock account", "DB update", "CURL", "manual fix needed"

7. **rate_fx_investigation** — Exchange rate discrepancies, FX rate complaints, rate vs market rate, rate lock expiry, markup questions, corridor pricing.
   Signals: "exchange rate", "FX rate", "rate difference", "rate shown vs applied", "markup", "mid-market rate", "rate lock"

8. **app_bug_engineering** — App crashes, UI bugs, API errors suggesting code bugs, feature malfunctions, reproducible issues.
   Signals: "app crash", "UI bug", "screen not loading", "button not working", "error on app", "after update", "white screen", "API returning 500"

9. **other_needs_triage** — Anything that doesn't clearly fit above. Ambiguous, multi-issue, new patterns, greetings, thanks, noise.
   Signals: very short messages, "thanks", "got it", no technical content, unclear what's being asked

## Decision Rules (apply in this order — first match wins)

1. If message says "KYC" or "verification" or "document" → **kyc_verification**
2. If message mentions ANY transaction/payment failure, decline, error, "unable to transact/transfer", "money deducted but failed", card/UPI issue → **payment_error_diagnosis**
3. If message mentions a partner name (Checkout.com, LULU, BBPS) + failure/issue → **bbps_partner_escalation**
4. If message asks to change/update/unlock something in the DB → **manual_backend_action**
5. If message is about rates/FX/exchange → **rate_fx_investigation**
6. If message describes app misbehaviour or crashes → **app_bug_engineering**
7. If message is about referral/promo/cashback → **referral_promo**
8. If message is ONLY about checking/looking up a status, refund tracking, or system sync issues (NOT a failure) → **db_lookup_status**
9. When genuinely uncertain → **other_needs_triage**

## IMPORTANT: payment_error_diagnosis vs db_lookup_status
- "Customer unable to do transaction" → **payment_error_diagnosis** (it's a FAILURE)
- "Money deducted but transfer not received" → **payment_error_diagnosis** (it's a FAILURE)
- "Payment not going through" → **payment_error_diagnosis** (it's a FAILURE)
- "Check the status of order AE123" → **db_lookup_status** (it's a STATUS CHECK)
- "AlphaDesk shows completed but Falcon shows pending" → **db_lookup_status** (it's a SYNC issue)
- "Refund not received" → **db_lookup_status** (it's TRACKING)
- When in doubt between the two, prefer **payment_error_diagnosis**

## Identifier Formats

- **order_id**: Country prefix (AE/UK/US/EU/PK/PH/IN) + 8-12 alphanumeric. Example: AE13SNKS8O00
- **user_id**: UUID v4 (8-4-4-4-12 hex with dashes). Example: 019e5135-1ac8-468b-b00f-a7f257cb3dc4. NEVER put UUIDs in order_ids.
- **payment_attempt_id**: PA- prefix. Example: PA-XXXXXXXXXX
- **fulfillment_id**: 32 hex chars, no dashes. Example: 6f3b4de9f08144dab154ff9f9b98be70
- **checkout_pay_id**: pay_ prefix. Example: pay_e5xbsxab4cqifk2in4dj3ej2pa
- **corridor**: UAE-India, UAE-Pakistan, UAE-Philippines, UK-India, US-India, etc.

## Output Format

Return ONLY a JSON object — no markdown, no explanation, no wrapping:
{
  "category": "one_of_the_9_categories",
  "confidence": 0.0-1.0,
  "summary": "One sentence summary of what the query is about",
  "order_ids": ["AE..."],
  "user_ids": ["uuid-1"],
  "payment_attempt_ids": ["PA-..."],
  "fulfillment_ids": ["32hexchars"],
  "checkout_pay_ids": ["pay_..."],
  "corridor": "UAE-India or empty string"
}"""

VALID_CATEGORIES = {
    "payment_error_diagnosis",
    "kyc_verification",
    "db_lookup_status",
    "referral_promo",
    "bbps_partner_escalation",
    "manual_backend_action",
    "rate_fx_investigation",
    "app_bug_engineering",
    "other_needs_triage",
}


@dataclass
class CXClassification:
    category: str
    confidence: float
    summary: str
    order_ids: list[str] = field(default_factory=list)
    user_ids: list[str] = field(default_factory=list)
    payment_attempt_ids: list[str] = field(default_factory=list)
    fulfillment_ids: list[str] = field(default_factory=list)
    checkout_pay_ids: list[str] = field(default_factory=list)
    corridor: str = ""


class CXClassifier:
    def __init__(self, client: anthropic.Anthropic, model: str):
        self.client = client
        self.model = model

    def classify(self, message_text: str) -> CXClassification:
        """Classify a message into one of 9 categories.

        1. Run regex extraction first (deterministic)
        2. Call Haiku with system prompt + message + extracted ID hints
        3. Parse JSON response
        4. If LLM fails or confidence < 0.5 -> keyword fallback
        5. Merge regex-extracted IDs into result (post-correction)
        6. Return CXClassification
        """
        # Step 1: Deterministic ID extraction
        extracted = extract_ids(message_text)
        logger.info(f"Extracted IDs: {extracted}")

        # Step 2: Try LLM classification
        llm_result = self._call_llm(message_text, extracted)

        if llm_result is not None and llm_result.confidence >= 0.5:
            # Step 5: Merge regex-extracted IDs (post-correction)
            result = self._merge_ids(llm_result, extracted)
            return result

        # Step 4: Fallback to keywords
        logger.info("Falling back to keyword classifier")
        category, confidence = keyword_classify(message_text)
        summary = llm_result.summary if llm_result else "Classified by keyword fallback"
        result = CXClassification(
            category=category,
            confidence=confidence,
            summary=summary,
        )
        return self._merge_ids(result, extracted)

    def _call_llm(self, message_text: str, extracted: dict) -> CXClassification | None:
        """Call Claude Haiku for classification. Retry once on 429."""
        hints = ""
        if extracted:
            hints = f"\n\n[Pre-extracted IDs from regex: {json.dumps(extracted)}]"

        user_content = message_text + hints

        for attempt in range(2):
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=512,
                    system=CLASSIFIER_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": user_content}],
                    timeout=10.0,
                )

                raw = response.content[0].text.strip()
                # Strip markdown fences if present
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                    if raw.endswith("```"):
                        raw = raw[:-3]
                    raw = raw.strip()

                data = json.loads(raw)
                return self._parse_llm_response(data)

            except anthropic.RateLimitError:
                if attempt == 0:
                    logger.warning("Rate limited by Anthropic, retrying in 1s...")
                    time.sleep(1)
                    continue
                logger.error("Rate limited twice, falling back to keywords")
                return None

            except (json.JSONDecodeError, KeyError, IndexError) as e:
                logger.error(f"Failed to parse LLM response: {e}")
                return None

            except Exception as e:
                logger.error(f"LLM call failed: {e}")
                return None

        return None

    def _parse_llm_response(self, data: dict) -> CXClassification:
        """Parse the JSON response from the LLM into a CXClassification."""
        category = data.get("category", "other_needs_triage")
        if category not in VALID_CATEGORIES:
            logger.warning(f"Invalid category from LLM: {category}, defaulting to other_needs_triage")
            category = "other_needs_triage"

        return CXClassification(
            category=category,
            confidence=float(data.get("confidence", 0.0)),
            summary=data.get("summary", ""),
            order_ids=data.get("order_ids", []),
            user_ids=data.get("user_ids", []),
            payment_attempt_ids=data.get("payment_attempt_ids", []),
            fulfillment_ids=data.get("fulfillment_ids", []),
            checkout_pay_ids=data.get("checkout_pay_ids", []),
            corridor=data.get("corridor", ""),
        )

    def _merge_ids(self, result: CXClassification, extracted: dict) -> CXClassification:
        """Merge regex-extracted IDs into the classification result.

        Regex extraction is authoritative — IDs found by regex are added
        to the correct fields, fixing any LLM misclassifications.
        """
        if "order_id" in extracted:
            existing = set(result.order_ids)
            for oid in extracted["order_id"]:
                existing.add(oid)
            result.order_ids = list(existing)

        if "user_id" in extracted:
            existing = set(result.user_ids)
            for uid in extracted["user_id"]:
                existing.add(uid)
                # Remove UUIDs that LLM might have put in order_ids
                if uid in result.order_ids:
                    result.order_ids.remove(uid)
            result.user_ids = list(existing)

        if "payment_attempt_id" in extracted:
            existing = set(result.payment_attempt_ids)
            for pid in extracted["payment_attempt_id"]:
                existing.add(pid)
            result.payment_attempt_ids = list(existing)

        if "fulfillment_id" in extracted:
            existing = set(result.fulfillment_ids)
            for fid in extracted["fulfillment_id"]:
                existing.add(fid)
            result.fulfillment_ids = list(existing)

        if "checkout_pay_id" in extracted:
            existing = set(result.checkout_pay_ids)
            for cpid in extracted["checkout_pay_id"]:
                existing.add(cpid)
            result.checkout_pay_ids = list(existing)

        return result
