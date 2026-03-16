"""Build Slack mrkdwn response messages."""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from classifier.classifier import CXClassification
    from assigner.assigner import Assignment

CATEGORY_DISPLAY_NAMES = {
    "payment_error_diagnosis": "Payment Error Diagnosis",
    "kyc_verification": "KYC / Verification Service Check",
    "db_lookup_status": "DB Lookup & Status Check",
    "referral_promo": "Referral / Promo System Check",
    "bbps_partner_escalation": "BBPS / Partner Escalation",
    "manual_backend_action": "Manual Backend Action",
    "rate_fx_investigation": "Rate / FX Investigation",
    "app_bug_engineering": "App Bug / Engineering Escalation",
    "other_needs_triage": "Other / Needs Triage",
}


def format_full_response(
    classification: "CXClassification",
    assignment: "Assignment",
    analysis: dict | None = None,
    poster_user_id: str = "",
    services_searched: list[str] | None = None,
) -> str:
    """Format a single combined Slack response with point-to-point analysis.

    Args:
        classification: The classified query result
        assignment: The assigned engineer
        analysis: Parsed analysis dict with root_cause, cx_advice
        poster_user_id: Slack user ID of the person who posted the query
        services_searched: List of CloudWatch services that were searched
    """
    display_name = CATEGORY_DISPLAY_NAMES.get(
        classification.category, classification.category
    )
    tag = assignment.slack_tag
    poster_tag = f"<@{poster_user_id}>" if poster_user_id else ""

    parts = [f":mag: *CX-Tech Bot — {display_name}*\n"]

    # ─── If we have structured analysis from CloudWatch + Claude ──────
    if analysis and (analysis.get("root_cause") or analysis.get("cx_advice")):
        # 1. Root Cause
        root_cause = analysis.get("root_cause", "")
        if root_cause:
            parts.append(f"\n*1. Root Cause*\n{root_cause}\n")

        # 2. CX Advice (tag the original poster)
        cx_advice = analysis.get("cx_advice", "")
        if cx_advice:
            parts.append(f"\n*2. CX Advice* {poster_tag}\n{cx_advice}\n")

    else:
        # No CloudWatch analysis — show classification summary
        parts.append(f"\n*Summary:* {classification.summary}\n")
        if poster_tag:
            parts.append(f"\n{poster_tag}\n")

    # ─── Footer: assignment + services ─────────────────────────────────
    parts.append(f"\n———\n*Assigned to:* {tag}")

    if services_searched:
        services = ", ".join(services_searched)
        parts.append(f"  |  _Services searched: {services}_")

    parts.append(f"\n_{tag}, please pick this up._")

    return "".join(parts)


def format_triage_response(
    classification: "CXClassification",
    assignment: "Assignment",
    poster_user_id: str = "",
) -> str:
    """Format a triage/fallback response when confidence is low."""
    display_name = CATEGORY_DISPLAY_NAMES.get(
        classification.category, classification.category
    )
    tag = assignment.slack_tag
    poster_tag = f"<@{poster_user_id}>" if poster_user_id else ""
    confidence_pct = f"{int(classification.confidence * 100)}%"

    return (
        f":warning: *CX-Tech Bot — Needs Manual Triage*\n\n"
        f"*Category:* {display_name}\n"
        f"*Confidence:* {confidence_pct}\n"
        f"*Summary:* {classification.summary}\n\n"
        f"*Assigned to:* {tag}\n"
        f"_{tag}, this needs manual triage. Bot couldn't confidently classify it._ {poster_tag}"
    )


def format_direct_search_response(
    search_id: str,
    service: str,
    analysis: dict | None = None,
    total_lines: int = 0,
    error_lines: int = 0,
    poster_user_id: str = "",
) -> str:
    """Format response for a direct @bot search command."""
    poster_tag = f"<@{poster_user_id}>" if poster_user_id else ""

    parts = [f":mag: *CX-Tech Bot — Direct Search*\n"]
    parts.append(f"_Service:_ `{service}`  |  _Search ID:_ `{search_id}`\n")
    parts.append(f"_Results:_ {total_lines} log lines, {error_lines} errors\n")

    if analysis and (analysis.get("root_cause") or analysis.get("cx_advice")):
        root_cause = analysis.get("root_cause", "")
        if root_cause:
            parts.append(f"\n*1. Root Cause*\n{root_cause}\n")

        cx_advice = analysis.get("cx_advice", "")
        if cx_advice:
            parts.append(f"\n*2. CX Advice* {poster_tag}\n{cx_advice}\n")
    else:
        parts.append(f"\nNo analysis available. {poster_tag}\n")

    return "".join(parts)
