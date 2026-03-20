"""Keyword-based fallback classifier when LLM is unavailable or low confidence."""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from classifier.classifier import CXClassification

CATEGORY_KEYWORDS = {
    "payment_error_diagnosis": [
        "payment failed", "transaction declined", "transaction failed",
        "transaction not going", "unable to transact", "unable to complete transaction",
        "unable to do transaction", "unable to transfer", "unable to send",
        "could not complete", "not able to transact", "not able to transfer",
        "not able to do transaction", "money deducted but failed",
        "payment not going", "payment stuck", "payment error", "payment not working",
        "debit failed", "transfer failed", "transfer not going",
        "3ds", "card not working", "card declined", "card failed",
        "debit card", "credit card", "upi fail", "upi declined", "upi not working",
        "payment attempt", "exceeds_daily_limit", "acquirer",
        "payment timeout", "pa-",
    ],
    "kyc_verification": [
        "kyc", "verification failed", "verification stuck", "verification pending",
        "document rejected", "compliance", "identity verification",
        "onfido", "persona", "sumsub", "pep screening", "sanctions",
        "unable to complete kyc", "unable to verify",
    ],
    "db_lookup_status": [
        "order status", "check status", "what is the status",
        "refund status", "refund not received", "cnr",
        "not syncing", "alphadesk", "falcon status", "fulfillment status",
        "status check", "where is my refund",
    ],
    "referral_promo": [
        "referral", "promo code", "cashback", "reward not credited",
        "campaign", "offer not applied", "referral bonus",
    ],
    "bbps_partner_escalation": [
        "bbps", "bill payment", "checkout.com", "lulu partner",
        "partner down", "partner payout", "webhook fail", "corridor down",
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
