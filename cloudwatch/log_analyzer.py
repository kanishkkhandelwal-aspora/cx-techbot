"""Use Claude to analyze raw CloudWatch logs and extract the probable failure reason."""

import logging
import os
from pathlib import Path

import anthropic

logger = logging.getLogger(__name__)

# ─── Base prompt for general (non-KYC) log analysis ──────────────────────────

LOG_ANALYSIS_PROMPT = """You are analyzing CloudWatch logs from Aspora's backend services to diagnose a customer issue reported by a CX agent.

## Context
{context}

## Your Task
Analyze the logs and respond in EXACTLY this structured format (keep the section headers exactly as shown):

[ROOT_CAUSE]
The specific failure reason in short bullet points. Include error codes, status values, or rejection reasons found. Keep it point-to-point — no filler sentences.

[CX_ADVICE]
Actionable bullet points for what the CX agent should do or tell the customer. Keep it direct and practical.

## Category-Specific Guidance
- **Payment errors**: Look for failure reason codes (EXCEEDS_DAILY_LIMIT, 3DS failure, acquirer rejection, timeout), payment status transitions, and acquirer responses.

## Rules
- Be point-to-point. Use short bullet points (•), not paragraphs.
- Be specific: "• Payment expired — attempt_status EXPIRED after 15 min timeout" is good. "There was an error" is bad.
- Include exact error codes, status values, failure reasons.
- CX_ADVICE bullets should tell the agent exactly what to do or say.
- If no clear error, state the last known status from the logs.

## Log Lines
{log_lines}

## Your Response (use the exact section format above):"""


# ─── KYC-specific prompt with rejection reasons knowledge base ────────────────

KYC_ANALYSIS_PROMPT = """You are analyzing CloudWatch logs from Aspora's KYC (identity verification) backend services to diagnose a customer's verification issue reported by a CX agent.

## Context
{context}

## Architecture
- **Verification Service** — Processes KYC data, talks to providers (Lulu for UAE, Persona for UK/US, Sumsub for EU), updates KYC status. Most KYC errors originate here.
- **Workflow Service** — Manages data collection screens (the pages the user sees). Screen-level errors, SDK token failures, or data collection issues show up here.

## KYC Providers by Region
- UAE: Lulu (via EFR) — Native flow. Common errors: NO_MATCH, DOCUMENT_EXPIRED, no active residency visa, onboarding failed.
- UK/US: Persona — SDK flow. "Pending" = manual review. Check for webhook failures, SDK token issues.
- EU: Sumsub (migrating to Persona) — SDK flow.

## Known Rejection Reasons & Resolutions
{rejection_reasons}

## Special Scenarios to Watch For
- KYC stuck in DRAFT: session expired or app crashed mid-flow → reject current KYC so user can restart.
- KYC stuck in PROCESSING: webhook from provider didn't arrive → check verification service logs.
- PENDING status (UK/EU): Persona flagged for manual review → route to KYC review team.
- UAE "customer onboarding failed": Lulu flagged as suspicious → raise with Lulu via email.
- Backend-mobile screen sync mismatch: backend expects one screen but app sends another → escalate to mobile team.

## Your Task
Analyze the logs and respond in EXACTLY this structured format (keep the section headers exactly as shown):

[ROOT_CAUSE]
Short bullet points with the specific KYC error, rejection reason, or status found. Include provider name (Lulu/Persona/Sumsub) if identifiable. If it matches a known rejection reason from the KB, state it. Mention rejection_count if visible (5+ = escalation needed). Keep it point-to-point.

[CX_ADVICE]
Actionable bullet points for the CX agent. If it matches a known rejection reason, include the specific resolution from the KB. Tell the agent exactly what to do or say to the customer.

## Rules
- Be point-to-point. Use short bullet points (•), not paragraphs.
- Be specific: "• KYC status: PROCESSING → stuck, provider: Lulu (EFR), no webhook received" is good.
- Include exact rejection reason, error code, KYC status, provider name.
- CX_ADVICE bullets should be direct actions.
- Cross-reference with the known rejection reasons and give the matching resolution.

## Log Lines
{log_lines}

## Your Response (use the exact section format above):"""


def _load_rejection_reasons() -> str:
    """Load KYC rejection reasons from the knowledge base file."""
    # Try relative to project root
    project_root = Path(__file__).parent.parent
    kb_path = project_root / "kyc-resolution" / "references" / "rejection_reasons.md"

    try:
        if kb_path.exists():
            content = kb_path.read_text(encoding="utf-8")
            logger.info(f"Loaded KYC rejection reasons ({len(content)} chars)")
            return content
    except Exception as e:
        logger.warning(f"Could not load rejection reasons: {e}")

    return "(Rejection reasons knowledge base not available)"


# Cache the rejection reasons at module load
_REJECTION_REASONS_CACHE: str | None = None


def _get_rejection_reasons() -> str:
    """Get cached rejection reasons (loads once)."""
    global _REJECTION_REASONS_CACHE
    if _REJECTION_REASONS_CACHE is None:
        _REJECTION_REASONS_CACHE = _load_rejection_reasons()
    return _REJECTION_REASONS_CACHE


def parse_structured_analysis(raw: str) -> dict:
    """Parse Claude's structured [ROOT_CAUSE]/[CX_ADVICE] response.

    Returns dict with keys: root_cause, cx_advice.
    """
    result = {"root_cause": "", "cx_advice": ""}

    sections = {"[ROOT_CAUSE]": "root_cause", "[CX_ADVICE]": "cx_advice"}

    current_key = None
    current_lines = []

    for line in raw.split("\n"):
        stripped = line.strip()
        if stripped in sections:
            if current_key:
                result[current_key] = "\n".join(current_lines).strip()
            current_key = sections[stripped]
            current_lines = []
        else:
            if current_key:
                current_lines.append(line)

    # Save last section
    if current_key:
        result[current_key] = "\n".join(current_lines).strip()

    return result


def analyze_logs_with_claude(
    client: anthropic.Anthropic,
    log_lines: list[str],
    model: str = "claude-haiku-4-5-20251001",
    context: str = "",
    category: str = "",
) -> str:
    """Send raw log lines to Claude for root cause analysis.

    Args:
        client: Anthropic client
        log_lines: Raw log lines from CloudWatch
        model: Claude model to use
        context: Context about the query (category, original message, services searched)
        category: The classified category — determines which prompt to use

    Returns:
        Raw structured analysis string (parse with parse_structured_analysis)
    """
    if not log_lines:
        return ""

    # ─── Smart line selection to stay within token budget ─────────────
    # Problem: KYC logs can have huge JSON response bodies (base64 data etc.)
    # that blow past the 200k token limit even with 50 lines.
    #
    # Strategy:
    # 1. Truncate each line to 2000 chars max
    # 2. Prioritize error/status lines over generic INFO lines
    # 3. Cap total text at ~120k chars (~30k tokens, safe with prompt overhead)
    MAX_LINE_LEN = 2000
    MAX_TOTAL_CHARS = 120_000
    MAX_LINES = 60

    # Import error patterns to identify high-value lines
    from cloudwatch.log_searcher import ERROR_PATTERNS

    truncated = [line[:MAX_LINE_LEN] for line in log_lines]

    # Split into error lines and other lines
    error_lines = []
    other_lines = []
    for line in truncated:
        if any(p.search(line) for p in ERROR_PATTERNS):
            error_lines.append(line)
        else:
            other_lines.append(line)

    # Build final list: errors first, then pad with other lines
    lines_to_analyze = error_lines[:MAX_LINES]
    remaining_slots = MAX_LINES - len(lines_to_analyze)
    if remaining_slots > 0:
        lines_to_analyze.extend(other_lines[:remaining_slots])

    # Cap total character count
    final_lines = []
    total_chars = 0
    for line in lines_to_analyze:
        if total_chars + len(line) > MAX_TOTAL_CHARS:
            break
        final_lines.append(line)
        total_chars += len(line)

    lines_to_analyze = final_lines
    log_text = "\n".join(lines_to_analyze)

    # Use KYC-specific prompt for KYC category (includes rejection reasons KB)
    if category == "kyc_verification":
        rejection_reasons = _get_rejection_reasons()
        prompt = KYC_ANALYSIS_PROMPT.format(
            context=context or "No additional context",
            rejection_reasons=rejection_reasons,
            log_lines=log_text,
        )
        max_tokens = 400
    else:
        prompt = LOG_ANALYSIS_PROMPT.format(
            context=context or "No additional context",
            log_lines=log_text,
        )
        max_tokens = 350

    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        analysis = response.content[0].text.strip()
        logger.info(f"Claude log analysis complete ({len(lines_to_analyze)} lines, category={category or 'general'})")
        return analysis

    except anthropic.RateLimitError:
        logger.warning("Claude rate limited during log analysis — skipping")
        return ""
    except Exception as e:
        logger.error(f"Claude log analysis failed: {e}")
        return ""
