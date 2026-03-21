"""Decision helpers for evidence-backed response modes."""

from __future__ import annotations

from knowledge_base.case_facts import CaseFacts
from knowledge_base.cx_response_playbook import PlaybookMatch


def decide_response_mode(
    *,
    category: str,
    classifier_confidence: float,
    facts: CaseFacts,
    playbook_match: PlaybookMatch | None,
) -> str:
    """Choose how assertive the final response should be."""
    if category == "other_needs_triage" or classifier_confidence < 0.5:
        return "triage"

    if playbook_match:
        return playbook_match.response_mode

    if facts.manual_action_needed or facts.sync_issue:
        return "escalate"

    if facts.evidence_strength >= 3:
        return "hybrid"

    return "triage"
