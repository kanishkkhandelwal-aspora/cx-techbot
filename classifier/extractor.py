"""Deterministic regex extraction of IDs from message text."""

import re

PATTERNS = {
    "order_id": r'\b(AE|UK|US|EU|PK|PH|IN)[A-Z0-9]{8,12}\b',
    "user_id": r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b',
    "payment_attempt_id": r'\bPA-[A-Za-z0-9]{8,20}\b',
    "fulfillment_id": r'\b[0-9a-f]{32}\b',
    "checkout_pay_id": r'\bpay_[a-z0-9]{20,40}\b',
}


def extract_ids(text: str) -> dict[str, list[str]]:
    """Extract all known ID types from message text.

    Returns a dict like {"order_id": ["AE13SNKS8O00"], "user_id": [...], ...}.
    Only keys with matches are included.
    """
    results = {}
    for id_type, pattern in PATTERNS.items():
        flags = re.IGNORECASE if id_type == "user_id" else 0
        matches = re.findall(pattern, text, flags)
        if id_type == "order_id":
            # re.findall with groups returns the group, not full match
            matches = re.findall(pattern, text)
            # findall returns the captured group (prefix only), so use finditer instead
            matches = [m.group() for m in re.finditer(pattern, text)]
        if matches:
            results[id_type] = list(set(matches))
    return results
