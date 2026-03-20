"""Use Claude to analyze raw CloudWatch logs + Databricks data and extract the probable failure reason.

This is the "Parent Agent" — it synthesizes inputs from:
  Agent 1: CloudWatch logs (unstructured log lines)
  Agent 2: Databricks SQL (structured DB records)
"""

import logging
import os
from pathlib import Path

import anthropic

logger = logging.getLogger(__name__)

# ─── Base prompt for general (non-KYC) log analysis ──────────────────────────

LOG_ANALYSIS_PROMPT = """You are a backend engineer diagnosing a customer issue. Give a DIRECT diagnosis.

{context}

ARCHITECTURE (internal — never mention these names in response):
- Goblin = payment orchestration | App-Server = API gateway | GOMS = order management

ERROR CODE REFERENCE:
EXCEEDS_DAILY_LIMIT → hit transfer limit | 3DS_FAILED → 3D Secure failed | ACQUIRER_DECLINED → bank declined
EXPIRED → didn't complete in time | RATE_EXPIRED → FX rate expired | INSUFFICIENT_FUNDS → low balance
PARTNER_TIMEOUT → payout partner failed | PENDING_RECONCILIATION → stuck awaiting bank confirmation

DATA (Databricks = authoritative, CloudWatch = contextual):
{log_lines}

RESPOND EXACTLY IN THIS FORMAT — no deviations:

[ROOT_CAUSE]
• <one line: what failed + exact error/status/reason from data>

[CX_ADVICE]
• <one line: exact action for CX agent — e.g. "Customer can retry now" or "Refund auto-reverses in 48h">

RULES (violating ANY = failed response):
- NEVER say "escalate to assigned engineer" or "if issue persists" or "verify IDs are correct"
- NEVER say "Ask customer to retry the transaction" — instead say WHEN they can retry and what to change
- NEVER use filler: "Let me analyze", "Based on", "It appears"
- NEVER exceed 2 bullets per section (4 total max)
- ALWAYS include exact order ID, amount, error code from the data
- If data shows no clear error, say "No failure recorded — payment may not have been initiated" (don't guess)"""


# ─── KYC-specific prompt with rejection reasons knowledge base ────────────────

KYC_ANALYSIS_PROMPT = """You are a backend engineer diagnosing a KYC/verification issue. Give a DIRECT diagnosis.

{context}

KYC PROVIDERS: UAE=Lulu(EFR) | UK/US=Persona | EU=Sumsub→Persona

DIAGNOSIS GUIDE:
DRAFT → session expired/app crash → reject current, user restarts
PROCESSING (stuck) → webhook missing from provider → check with provider
REJECTED + reason → match KB below → give exact fix
PENDING (UK/EU) → Persona manual review → route to KYC review team
rejection_count >= 5 → escalation needed, manual review
"customer onboarding failed" (UAE) → Lulu flagged → email Lulu team

KNOWN REJECTION REASONS & RESOLUTIONS:
{rejection_reasons}

DATA (Databricks = authoritative, CloudWatch = contextual):
{log_lines}

RESPOND EXACTLY IN THIS FORMAT — no deviations:

[ROOT_CAUSE]
• <kyc_status + rejection_reason + provider + rejection_count in one line>

[CX_ADVICE]
• <exact resolution from KB if reason matches, or specific next step>

RULES (violating ANY = failed response):
- NEVER say "escalate to assigned engineer" or "if issue persists" or "verify IDs"
- NEVER use filler: "Let me analyze", "Based on", "It appears"
- NEVER exceed 2 bullets per section (4 total max)
- ALWAYS include kyc_status, provider, rejection_reason, rejection_count from data
- MATCH rejection_reason against KB above → use the EXACT resolution listed
- If rejection_count >= 5, explicitly say "manual review required — escalate to KYC review team"
- If no rejection_reason, state the status and what it means (e.g. "PROCESSING = stuck, webhook not received")"""


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


def _prepare_log_text(log_lines: list[str]) -> tuple[str, int]:
    """Smart line selection to stay within token budget.

    Problem: KYC logs can have huge JSON response bodies (base64 data etc.)
    that blow past the 200k token limit even with 50 lines.

    Strategy:
    1. Truncate each line to 2000 chars max
    2. Prioritize error/status lines over generic INFO lines
    3. Cap total text at ~120k chars (~30k tokens, safe with prompt overhead)

    Returns (log_text, num_lines).
    """
    MAX_LINE_LEN = 2000
    MAX_TOTAL_CHARS = 120_000
    MAX_LINES = 60

    from cloudwatch.log_searcher import ERROR_PATTERNS

    truncated = [line[:MAX_LINE_LEN] for line in log_lines]

    error_lines = []
    other_lines = []
    for line in truncated:
        if any(p.search(line) for p in ERROR_PATTERNS):
            error_lines.append(line)
        else:
            other_lines.append(line)

    lines_to_analyze = error_lines[:MAX_LINES]
    remaining_slots = MAX_LINES - len(lines_to_analyze)
    if remaining_slots > 0:
        lines_to_analyze.extend(other_lines[:remaining_slots])

    final_lines = []
    total_chars = 0
    for line in lines_to_analyze:
        if total_chars + len(line) > MAX_TOTAL_CHARS:
            break
        final_lines.append(line)
        total_chars += len(line)

    return "\n".join(final_lines), len(final_lines)


def analyze_logs_with_claude(
    client: anthropic.Anthropic,
    log_lines: list[str],
    model: str = "claude-haiku-4-5-20251001",
    context: str = "",
    category: str = "",
    db_summary: str = "",
) -> str:
    """Synthesize CloudWatch logs + Databricks data into root cause analysis.

    This is the Parent Agent — it receives inputs from both Agent 1 (CloudWatch)
    and Agent 2 (Databricks) and produces a unified structured response.

    Args:
        client: Anthropic client
        log_lines: Raw log lines from CloudWatch (Agent 1)
        model: Claude model to use
        context: Context about the query
        category: The classified category — determines which prompt to use
        db_summary: Structured text from Databricks queries (Agent 2).
                    Empty string if Databricks is not configured or no data found.

    Returns:
        Raw structured analysis string (parse with parse_structured_analysis)
    """
    if not log_lines and not db_summary:
        return ""

    # Prepare log text (smart selection + truncation)
    log_text = ""
    num_lines = 0
    if log_lines:
        log_text, num_lines = _prepare_log_text(log_lines)

    # ─── Build the data sources section ───────────────────────────────
    data_sources = ""
    if db_summary:
        data_sources += f"\n\n## Structured Database Records (Databricks)\n{db_summary}"
    if log_text:
        data_sources += f"\n\n## CloudWatch Log Lines\n{log_text}"

    if not data_sources.strip():
        return ""

    # Use KYC-specific prompt for KYC category (includes rejection reasons KB)
    if category == "kyc_verification":
        rejection_reasons = _get_rejection_reasons()
        prompt = KYC_ANALYSIS_PROMPT.format(
            context=context or "No additional context",
            rejection_reasons=rejection_reasons,
            log_lines=data_sources,  # now includes both DB + logs
        )
        max_tokens = 350
    else:
        prompt = LOG_ANALYSIS_PROMPT.format(
            context=context or "No additional context",
            log_lines=data_sources,  # now includes both DB + logs
        )
        max_tokens = 350

    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
            timeout=30.0,  # 30s hard timeout — prevent hanging
        )
        analysis = response.content[0].text.strip()
        sources = []
        if num_lines > 0:
            sources.append(f"{num_lines} log lines")
        if db_summary:
            sources.append("DB records")
        logger.info(
            f"Claude synthesis complete ({', '.join(sources)}, "
            f"category={category or 'general'})"
        )
        return analysis

    except anthropic.RateLimitError:
        logger.warning("Claude rate limited during log analysis — skipping")
        return ""
    except Exception as e:
        logger.error(f"Claude log analysis failed: {e}")
        return ""
