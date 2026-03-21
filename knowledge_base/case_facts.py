"""Structured fact extraction from DB and CloudWatch investigations."""

from __future__ import annotations

from dataclasses import dataclass, field
import re

from cloudwatch.log_searcher import InvestigationResult
from db_agent.db_searcher import DBInvestigationResult


def _norm(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def _first_csv_token(value: object) -> str:
    text = _norm(value)
    if not text:
        return ""
    text = text.strip("[]")
    for token in re.split(r"[,|]", text):
        token = token.strip(" '\"")
        if token:
            return token
    return ""


@dataclass
class CaseFacts:
    category: str
    payment_failure_reason: str = ""
    provider: str = ""
    acquirer: str = ""
    order_status: str = ""
    order_sub_state: str = ""
    fulfillment_status: str = ""
    fulfillment_sub_status: str = ""
    payout_status: str = ""
    payout_error: str = ""
    kyc_status: str = ""
    rejection_reason: str = ""
    rejection_count: int | None = None
    sync_issue: bool = False
    retryable: bool | None = None
    manual_action_needed: bool = False
    signals: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)

    @property
    def evidence_strength(self) -> int:
        fields = [
            self.payment_failure_reason,
            self.provider,
            self.acquirer,
            self.order_status,
            self.order_sub_state,
            self.fulfillment_status,
            self.fulfillment_sub_status,
            self.payout_status,
            self.payout_error,
            self.kyc_status,
            self.rejection_reason,
        ]
        return sum(1 for value in fields if value) + len(self.signals) + len(self.sources)

    def to_prompt_text(self) -> str:
        lines = []
        mapping = [
            ("payment_failure_reason", self.payment_failure_reason),
            ("provider", self.provider),
            ("acquirer", self.acquirer),
            ("order_status", self.order_status),
            ("order_sub_state", self.order_sub_state),
            ("fulfillment_status", self.fulfillment_status),
            ("fulfillment_sub_status", self.fulfillment_sub_status),
            ("payout_status", self.payout_status),
            ("payout_error", self.payout_error),
            ("kyc_status", self.kyc_status),
            ("rejection_reason", self.rejection_reason),
        ]
        for key, value in mapping:
            if value:
                lines.append(f"- {key}: {value}")
        if self.rejection_count is not None:
            lines.append(f"- rejection_count: {self.rejection_count}")
        if self.sync_issue:
            lines.append("- sync_issue: true")
        if self.retryable is not None:
            lines.append(f"- retryable: {'true' if self.retryable else 'false'}")
        if self.manual_action_needed:
            lines.append("- manual_action_needed: true")
        if self.signals:
            lines.append(f"- signals: {', '.join(sorted(self.signals))}")
        if self.sources:
            lines.append(f"- evidence_sources: {', '.join(sorted(self.sources))}")
        return "\n".join(lines) if lines else "- no structured facts extracted"


def _set_if_empty(facts: CaseFacts, field_name: str, value: object) -> None:
    text = _norm(value)
    if text and not getattr(facts, field_name):
        setattr(facts, field_name, text)


def _detect_provider(text: str) -> str:
    if "truelayer" in text:
        return "truelayer"
    if "plaid" in text:
        return "plaid"
    if "lean" in text:
        return "lean"
    if "checkout" in text:
        return "checkout"
    if "lulu" in text:
        return "lulu"
    if "persona" in text:
        return "persona"
    if "sumsub" in text:
        return "sumsub"
    if "efr" in text:
        return "efr"
    return ""


def _add_signal(facts: CaseFacts, signal: str) -> None:
    if signal:
        facts.signals.add(signal)


def _parse_log_lines(facts: CaseFacts, lines: list[str]) -> None:
    for line in lines:
        lower = _norm(line)

        provider = _detect_provider(lower)
        if provider and not facts.provider:
            facts.provider = provider

        if "challenge_cancelled" in lower:
            _add_signal(facts, "challenge_cancelled")
            if not facts.payment_failure_reason:
                facts.payment_failure_reason = "challenge_cancelled"
        if "authenticationprocesserror" in lower:
            _add_signal(facts, "authenticationprocesserror")
        if "user_canceled_at_provider" in lower or "user cancelled at provider" in lower or "user canceled at provider" in lower:
            _add_signal(facts, "user_canceled_at_provider")
            if not facts.payment_failure_reason:
                facts.payment_failure_reason = "user_canceled_at_provider"
        if 'reason="canceled"' in lower or " reason=canceled" in lower or " reason=\"canceled\"" in lower:
            _add_signal(facts, "reason_canceled")
            if not facts.payment_failure_reason:
                facts.payment_failure_reason = "canceled"
        if "provider_rejected" in lower:
            _add_signal(facts, "provider_rejected")
            if not facts.payment_failure_reason:
                facts.payment_failure_reason = "provider_rejected"
        if "do not honour" in lower:
            _add_signal(facts, "do_not_honour")
            if not facts.payment_failure_reason:
                facts.payment_failure_reason = "do_not_honour"
        if "authentication_rejected" in lower or "authentication_declined" in lower:
            _add_signal(facts, "authentication_declined")
            if not facts.payment_failure_reason:
                facts.payment_failure_reason = "authentication_declined"
        if "exceeds_daily_limit" in lower:
            _add_signal(facts, "exceeds_daily_limit")
            if not facts.payment_failure_reason:
                facts.payment_failure_reason = "exceeds_daily_limit"
        if "insufficient funds" in lower:
            _add_signal(facts, "insufficient_funds")
            if not facts.payment_failure_reason:
                facts.payment_failure_reason = "insufficient_funds"
        if "concurrent_authentication_request" in lower:
            _add_signal(facts, "concurrent_authentication_request")
        if "webhook not sent" in lower:
            _add_signal(facts, "webhook_not_sent")
            facts.sync_issue = True
        if "kafka timeout" in lower or "kafka timeouts" in lower:
            _add_signal(facts, "kafka_timeout")
            facts.sync_issue = True
        if "no_match" in lower:
            _add_signal(facts, "no_match")
            if not facts.rejection_reason:
                facts.rejection_reason = "no_match"
        if "documents_expired" in lower or "document expired" in lower:
            _add_signal(facts, "documents_expired")
            if not facts.rejection_reason:
                facts.rejection_reason = "documents_expired"
        if "face_prints_mismatch" in lower:
            _add_signal(facts, "face_prints_mismatch")
            if not facts.rejection_reason:
                facts.rejection_reason = "face_prints_mismatch"
        if "gettemporarykey" in lower and "timed out" in lower:
            _add_signal(facts, "efr_timeout")
        if "manual sync trigger required" in lower:
            _add_signal(facts, "manual_sync_required")
            facts.manual_action_needed = True
        if "move to on_hold" in lower:
            _add_signal(facts, "move_to_on_hold")
            facts.manual_action_needed = True


def build_case_facts(
    category: str,
    db_investigation: DBInvestigationResult | None = None,
    cw_investigation: InvestigationResult | None = None,
) -> CaseFacts:
    """Build normalized case facts from available evidence."""
    facts = CaseFacts(category=category)

    if db_investigation:
        for query in db_investigation.queries_run:
            if query.row_count == 0:
                continue
            facts.sources.add("databricks")
            for row in query.rows:
                table = query.table.lower()
                if "payment_attempt" in table:
                    _set_if_empty(facts, "payment_failure_reason", row.get("meta_failure_reason") or row.get("reason") or row.get("meta_response_summary"))
                    _set_if_empty(facts, "acquirer", row.get("meta_acquirer"))
                    provider = _detect_provider(" ".join(str(row.get(k, "")) for k in ("reason", "meta_failure_reason", "meta_response_summary", "meta_acquirer")))
                    if provider and not facts.provider:
                        facts.provider = provider
                elif "checkout_payment" in table:
                    _set_if_empty(facts, "payment_failure_reason", row.get("response_summary") or row.get("response_code") or row.get("status"))
                    if row.get("risk_flagged"):
                        _add_signal(facts, "risk_flagged")
                    provider = _detect_provider(" ".join(str(row.get(k, "")) for k in ("response_summary", "response_code")))
                    if provider and not facts.provider:
                        facts.provider = provider
                elif "goms_db_orders" in table or "goms_orders" in table:
                    _set_if_empty(facts, "order_status", row.get("status"))
                    _set_if_empty(facts, "order_sub_state", row.get("sub_state"))
                elif "appserver_db_orders" in table or "appserver_orders" in table:
                    _set_if_empty(facts, "order_status", row.get("order_status"))
                    provider = _detect_provider(" ".join(str(row.get(k, "")) for k in ("payment_acquirer", "fulfillment_provider")))
                    if provider and not facts.provider:
                        facts.provider = provider
                    _set_if_empty(facts, "acquirer", row.get("payment_acquirer"))
                elif "fulfillment" in table:
                    _set_if_empty(facts, "fulfillment_status", row.get("status"))
                    _set_if_empty(facts, "fulfillment_sub_status", row.get("sub_status"))
                elif "falcon" in table:
                    _set_if_empty(facts, "payout_status", row.get("status"))
                    _set_if_empty(facts, "payout_error", row.get("error"))
                elif "user_kyc" in table:
                    _set_if_empty(facts, "kyc_status", row.get("kyc_status") or row.get("current_kyc_status"))
                    provider = _detect_provider(" ".join(str(row.get(k, "")) for k in ("provider", "sub_provider", "resolving_providers")))
                    if provider and not facts.provider:
                        facts.provider = provider
                    _set_if_empty(facts, "rejection_reason", row.get("rejection_reason") or _first_csv_token(row.get("rejection_reasons")))
                    if facts.rejection_count is None and row.get("rejection_count") is not None:
                        try:
                            facts.rejection_count = int(row["rejection_count"])
                        except Exception:
                            pass

    if cw_investigation:
        all_lines: list[str] = []
        error_lines: list[str] = []
        for step in cw_investigation.search_steps:
            all_lines.extend(step.all_lines)
            error_lines.extend(step.error_lines)
        if all_lines:
            facts.sources.add("cloudwatch")
            _parse_log_lines(facts, error_lines or all_lines)

    # Normalize some values into explicit signals.
    if facts.payment_failure_reason:
        reason = facts.payment_failure_reason
        if "do not honour" in reason:
            _add_signal(facts, "do_not_honour")
        if "provider_rejected" in reason:
            _add_signal(facts, "provider_rejected")
        if "challenge_cancelled" in reason:
            _add_signal(facts, "challenge_cancelled")
        if "authenticationprocesserror" in reason:
            _add_signal(facts, "authenticationprocesserror")
        if "authentication_rejected" in reason or "authentication_declined" in reason:
            _add_signal(facts, "authentication_declined")
        if "authorization_required" in reason:
            _add_signal(facts, "authorization_required")
        if "canceled" in reason or "cancelled" in reason:
            _add_signal(facts, "reason_canceled")
        if "exceeds_daily_limit" in reason:
            _add_signal(facts, "exceeds_daily_limit")
        if "insufficient" in reason:
            _add_signal(facts, "insufficient_funds")

    if facts.rejection_reason:
        if "no_match" in facts.rejection_reason:
            _add_signal(facts, "no_match")
        if "documents_expired" in facts.rejection_reason or "document_expired" in facts.rejection_reason:
            _add_signal(facts, "documents_expired")
        if "face_prints_mismatch" in facts.rejection_reason:
            _add_signal(facts, "face_prints_mismatch")

    if facts.payout_status == "completed" and facts.order_status in {"processing", "created", "compliance_processing"}:
        facts.sync_issue = True
        _add_signal(facts, "status_sync_mismatch")
    if "webhook_not_sent" in facts.signals or "kafka_timeout" in facts.signals:
        facts.sync_issue = True
        _add_signal(facts, "status_sync_mismatch")

    if facts.order_status in {"created", "compliance_processing"}:
        facts.manual_action_needed = True
    if facts.fulfillment_sub_status == "release_order":
        facts.manual_action_needed = True
    if facts.rejection_count is not None and facts.rejection_count >= 5:
        facts.manual_action_needed = True
    if facts.sync_issue:
        facts.manual_action_needed = True

    if facts.payment_failure_reason:
        retryable_signals = {
            "challenge_cancelled",
            "provider_rejected",
            "authentication_declined",
            "authorization_required",
            "reason_canceled",
            "user_canceled_at_provider",
            "efr_timeout",
            "no_match",
            "face_prints_mismatch",
        }
        if facts.signals & retryable_signals:
            facts.retryable = True
        elif "do_not_honour" in facts.signals or "exceeds_daily_limit" in facts.signals or "insufficient_funds" in facts.signals:
            facts.retryable = False

    return facts
