"""Evidence-backed response playbook.

Rules only match on structured facts extracted from investigation results.
They do not inspect the original Slack message or Claude wording.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from knowledge_base.case_facts import CaseFacts


@dataclass(frozen=True)
class PlaybookMatch:
    issue: str
    guidance: str
    score: int
    response_mode: str


@dataclass(frozen=True)
class PlaybookRule:
    issue: str
    guidance: str
    response_mode: str
    categories: tuple[str, ...]
    required: dict[str, object] = field(default_factory=dict)
    supporting: dict[str, object] = field(default_factory=dict)
    excluded: dict[str, object] = field(default_factory=dict)
    required_signals: tuple[str, ...] = ()
    supporting_signals: tuple[str, ...] = ()
    excluded_signals: tuple[str, ...] = ()


def _matches_value(actual: object, expected: object) -> bool:
    if expected is None:
        return actual is None
    if isinstance(expected, tuple):
        return any(_matches_value(actual, item) for item in expected)
    if isinstance(expected, bool):
        return bool(actual) is expected
    if isinstance(expected, int):
        return actual == expected
    if actual is None:
        return False
    return str(actual).strip().lower() == str(expected).strip().lower()


def _matches_rule(rule: PlaybookRule, facts: CaseFacts) -> int:
    if rule.categories and facts.category not in rule.categories:
        return 0

    for key, expected in rule.required.items():
        if not _matches_value(getattr(facts, key), expected):
            return 0

    for signal in rule.required_signals:
        if signal not in facts.signals:
            return 0

    for key, expected in rule.excluded.items():
        if _matches_value(getattr(facts, key), expected):
            return 0

    for signal in rule.excluded_signals:
        if signal in facts.signals:
            return 0

    score = len(rule.required) * 10 + len(rule.required_signals) * 10

    for key, expected in rule.supporting.items():
        if _matches_value(getattr(facts, key), expected):
            score += 1

    for signal in rule.supporting_signals:
        if signal in facts.signals:
            score += 1

    return score


RULES: tuple[PlaybookRule, ...] = (
    PlaybookRule(
        issue="Bank authorization not completed",
        categories=("payment_error_diagnosis",),
        response_mode="auto_resolve",
        required_signals=("reason_canceled",),
        supporting={"provider": ("truelayer", "lean", "plaid")},
        guidance="Ask the customer to re-initiate the transfer and complete the full bank authorization flow without cancelling.",
    ),
    PlaybookRule(
        issue="Open banking authorization required",
        categories=("payment_error_diagnosis",),
        response_mode="auto_resolve",
        required_signals=("authorization_required",),
        supporting={"provider": ("truelayer", "lean", "plaid")},
        guidance="The customer did not complete bank authorization. Ask them to retry and finish the full consent flow.",
    ),
    PlaybookRule(
        issue="Provider rejected",
        categories=("payment_error_diagnosis",),
        response_mode="auto_resolve",
        required_signals=("provider_rejected",),
        supporting={"provider": ("truelayer", "lean", "plaid")},
        guidance="Ask the customer to retry. If the same bank keeps failing, suggest a different payment method.",
    ),
    PlaybookRule(
        issue="Checkout challenge cancelled",
        categories=("payment_error_diagnosis",),
        response_mode="auto_resolve",
        required_signals=("challenge_cancelled",),
        supporting_signals=("authenticationprocesserror",),
        guidance="Ask the customer to retry from a different device or network, or use a different card.",
    ),
    PlaybookRule(
        issue="Authentication declined",
        categories=("payment_error_diagnosis",),
        response_mode="auto_resolve",
        required_signals=("authentication_declined",),
        guidance="Ask the customer to retry with the correct OTP or password, or use a different payment method.",
    ),
    PlaybookRule(
        issue="Do Not Honour",
        categories=("payment_error_diagnosis",),
        response_mode="auto_resolve",
        required_signals=("do_not_honour",),
        guidance="Ask the customer to contact their bank to approve online or merchant payments because the decline came from the bank's risk engine.",
    ),
    PlaybookRule(
        issue="Daily limit exceeded",
        categories=("payment_error_diagnosis",),
        response_mode="auto_resolve",
        required_signals=("exceeds_daily_limit",),
        guidance="This is a bank-side transfer limit issue. Ask the customer to increase their daily or international transfer limit with the bank.",
    ),
    PlaybookRule(
        issue="Insufficient funds",
        categories=("payment_error_diagnosis",),
        response_mode="auto_resolve",
        required_signals=("insufficient_funds",),
        guidance="Ask the customer to check their available balance or use another funding source before retrying.",
    ),
    PlaybookRule(
        issue="GOMS order stuck in CREATED",
        categories=("db_lookup_status",),
        response_mode="escalate",
        required={"order_status": "created"},
        supporting_signals=("manual_sync_required", "move_to_on_hold"),
        guidance="Manual sync is required. The order should move to ON_HOLD before release or refund action can be taken in GOMS.",
    ),
    PlaybookRule(
        issue="Order stuck in COMPLIANCE_PROCESSING",
        categories=("db_lookup_status", "manual_backend_action"),
        response_mode="escalate",
        required={"order_status": "compliance_processing"},
        guidance="Orders stuck in compliance_processing usually need manual release by ops.",
    ),
    PlaybookRule(
        issue="Falcon and GOMS sync failure",
        categories=("db_lookup_status", "payment_error_diagnosis"),
        response_mode="escalate",
        required={"sync_issue": True},
        supporting_signals=("webhook_not_sent", "kafka_timeout", "status_sync_mismatch"),
        guidance="This looks like a Falcon-GOMS sync issue. Tag Ayush or Raj to manually send the webhook or trigger sync.",
    ),
    PlaybookRule(
        issue="Fulfillment stuck in RELEASE_ORDER",
        categories=("db_lookup_status", "payment_error_diagnosis"),
        response_mode="escalate",
        required={"fulfillment_sub_status": "release_order"},
        guidance="Fulfillment appears stuck in RELEASE_ORDER and usually needs manual release.",
    ),
    PlaybookRule(
        issue="NO_MATCH on Lulu face verification",
        categories=("kyc_verification",),
        response_mode="hybrid",
        required={"kyc_status": "rejected"},
        required_signals=("no_match",),
        supporting={"provider": ("lulu", "efr")},
        guidance="Retries often resolve this for genuine users. If the same result repeats, raise it with Lulu because the live face did not match the Emirates ID photo.",
    ),
    PlaybookRule(
        issue="Documents expired",
        categories=("kyc_verification",),
        response_mode="auto_resolve",
        required_signals=("documents_expired",),
        guidance="Ask the customer to upload valid, non-expired identity documents before retrying KYC.",
    ),
    PlaybookRule(
        issue="Face prints mismatch",
        categories=("kyc_verification",),
        response_mode="hybrid",
        required_signals=("face_prints_mismatch",),
        guidance="This usually needs Lulu review because the customer's current appearance differs too much from the ID photo.",
    ),
    PlaybookRule(
        issue="EFR timeout",
        categories=("kyc_verification",),
        response_mode="auto_resolve",
        required_signals=("efr_timeout",),
        guidance="This looks like a transient EFR timeout. Ask the customer to retry after a short wait.",
    ),
)


def find_playbook_match(facts: CaseFacts) -> PlaybookMatch | None:
    """Return the most specific matching playbook, or None if ambiguous."""
    best_rule: PlaybookRule | None = None
    best_score = 0
    tied = False

    for rule in RULES:
        score = _matches_rule(rule, facts)
        if score == 0:
            continue
        if score > best_score:
            best_rule = rule
            best_score = score
            tied = False
        elif score == best_score:
            tied = True

    if not best_rule or tied:
        return None

    return PlaybookMatch(
        issue=best_rule.issue,
        guidance=best_rule.guidance,
        score=best_score,
        response_mode=best_rule.response_mode,
    )
