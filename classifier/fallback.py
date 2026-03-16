"""Keyword-based fallback classifier when LLM is unavailable or low confidence."""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from classifier.classifier import CXClassification

CATEGORY_KEYWORDS = {
    "payment_error_diagnosis": [
        "payment failed", "transaction declined", "3ds", "card not working",
        "debit card", "credit card", "payment attempt", "exceeds_daily_limit",
        "acquirer", "upi fail", "upi declined", "payment timeout", "card declined",
    ],
    "kyc_verification": [
        "kyc", "verification failed", "document rejected", "compliance",
        "identity verification", "onfido", "pep screening", "sanctions",
    ],
    "db_lookup_status": [
        "order status", "transfer status", "refund status", "cnr",
        "not syncing", "alphadesk", "falcon status", "fulfillment",
        "money deducted but", "where is my transfer", "status check",
    ],
    "referral_promo": [
        "referral", "promo code", "cashback", "reward not credited",
        "campaign", "offer not applied", "referral bonus",
    ],
    "bbps_partner_escalation": [
        "bbps", "bill payment", "checkout.com", "lulu", "partner",
        "webhook", "corridor down", "partner payout",
    ],
    "manual_backend_action": [
        "change state", "update mobile", "mobile number change",
        "unlock account", "db update", "manual fix", "curl",
        "state change", "number change",
    ],
    "rate_fx_investigation": [
        "exchange rate", "fx rate", "rate difference", "markup",
        "mid-market", "rate lock", "rate shown", "rate applied",
    ],
    "app_bug_engineering": [
        "app crash", "ui bug", "screen not loading", "button not working",
        "white screen", "app not opening", "after update", "api error",
    ],
}


def keyword_classify(text: str) -> tuple[str, float]:
    """Classify message by keyword matching.

    Returns (category, confidence). First match wins.
    If no match, returns ("other_needs_triage", 0.3).
    """
    lowered = text.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            if keyword in lowered:
                return category, 0.6
    return "other_needs_triage", 0.3
