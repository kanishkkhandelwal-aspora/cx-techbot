"""CloudWatch Logs searcher for CX-Tech Bot investigation pipelines.

Log formats vary by service:
    Goblin/payment:      2026-03-11 07:43:43 [txn-ID] [req-ID] [deviceid] ...
    Verification/workflow: 2026-03-12 13:13:26 [request-id] [deviceid] INFO ...
"""

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# ─── Base path for all log groups ───────────────────────────────────────────
LOG_GROUP_BASE = "/ecs/vance-core/prod/london/01"

# ─── Category → log groups mapping ─────────────────────────────────────────
# For each category, list the services to search IN ORDER (most relevant first).
# Currently enabled: payment_error_diagnosis, kyc_verification (more to come)
CATEGORY_LOG_GROUPS = {
    "payment_error_diagnosis": [
        f"{LOG_GROUP_BASE}/goblin-service-logs",
        f"{LOG_GROUP_BASE}/app-server-service-logs",
        f"{LOG_GROUP_BASE}/goms-service-logs",
    ],
    "kyc_verification": [
        f"{LOG_GROUP_BASE}/verification-service-logs",
        f"{LOG_GROUP_BASE}/workflow-service-logs",
    ],
}

# Categories that have CloudWatch investigation enabled
INVESTIGATE_CATEGORIES = set(CATEGORY_LOG_GROUPS.keys())

# ─── Service name → log group mapping (for direct @bot searches) ─────────
# Users can say "goblin", "verification", "workflow", etc.
SERVICE_ALIASES = {
    "goblin":               f"{LOG_GROUP_BASE}/goblin-service-logs",
    "goblin-service":       f"{LOG_GROUP_BASE}/goblin-service-logs",
    "app-server":           f"{LOG_GROUP_BASE}/app-server-service-logs",
    "app-server-service":   f"{LOG_GROUP_BASE}/app-server-service-logs",
    "appserver":            f"{LOG_GROUP_BASE}/app-server-service-logs",
    "goms":                 f"{LOG_GROUP_BASE}/goms-service-logs",
    "goms-service":         f"{LOG_GROUP_BASE}/goms-service-logs",
    "verification":         f"{LOG_GROUP_BASE}/verification-service-logs",
    "verification-service": f"{LOG_GROUP_BASE}/verification-service-logs",
    "workflow":             f"{LOG_GROUP_BASE}/workflow-service-logs",
    "workflow-service":     f"{LOG_GROUP_BASE}/workflow-service-logs",
}

# Friendly display names
SERVICE_DISPLAY_NAMES = {
    f"{LOG_GROUP_BASE}/goblin-service-logs":       "goblin-service",
    f"{LOG_GROUP_BASE}/app-server-service-logs":   "app-server-service",
    f"{LOG_GROUP_BASE}/goms-service-logs":         "goms-service",
    f"{LOG_GROUP_BASE}/verification-service-logs": "verification-service",
    f"{LOG_GROUP_BASE}/workflow-service-logs":      "workflow-service",
}

# ─── Log line parsing ──────────────────────────────────────────────────────
# Goblin / payment services: [txn-id] [req-id] [device-id]  (3 brackets)
LOG_LINE_PATTERN_3 = re.compile(
    r'^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+'
    r'\[([^\]]+)\]\s*'   # group 1: transaction_id
    r'\[([^\]]+)\]\s*'   # group 2: request_id
    r'\[([^\]]+)\]'      # group 3: device_id
)

# Verification / workflow services: [request-id] [device-id]  (2 brackets)
LOG_LINE_PATTERN_2 = re.compile(
    r'^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+'
    r'\[([^\]]*)\]\s*'   # group 1: request_id (may be 32-hex)
    r'\[([^\]]*)\]'      # group 2: device_id (may be empty)
)

# ─── KYC request ID extraction ────────────────────────────────────────────
# Patterns to extract kyc_request_id / ekyc_request_id from log lines
KYC_REQUEST_ID_PATTERNS = [
    re.compile(r'(?:kyc_request_id|kycRequestId|ekyc_request_id|ekycRequestId)[=:\s"]+([a-zA-Z0-9_-]{8,})', re.IGNORECASE),
    re.compile(r'(?:requestId|request_id)[=:\s"]+([a-f0-9]{24,})', re.IGNORECASE),
]

ERROR_PATTERNS = [
    # ─── General ───
    re.compile(r'(?<!\w)ERROR(?!\w)', re.IGNORECASE),
    re.compile(r'fail(?:ed|ure|ing)?', re.IGNORECASE),
    re.compile(r'exception', re.IGNORECASE),
    re.compile(r'timeout', re.IGNORECASE),
    re.compile(r'declined', re.IGNORECASE),
    re.compile(r'rejected', re.IGNORECASE),
    re.compile(r'[Cc]an\s*not\s+update', re.IGNORECASE),
    # ─── Payment-specific ───
    re.compile(r'EXCEEDS_DAILY_LIMIT', re.IGNORECASE),
    re.compile(r'3[Dd][Ss]'),
    re.compile(r'acquirer.?reject', re.IGNORECASE),
    re.compile(r'payment.?expired', re.IGNORECASE),
    re.compile(r'status[=:]\s*(?:FAILED|DECLINED|ERROR|EXPIRED)', re.IGNORECASE),
    # ─── KYC / Verification-specific ───
    re.compile(r'kyc.?(?:fail|reject|stuck|pending|block)', re.IGNORECASE),
    re.compile(r'kyc\s+status\s+from\s+\w+\s+to\s+\w+', re.IGNORECASE),
    re.compile(r'[Cc]an\s*not\s+update\s+user\s+kyc', re.IGNORECASE),
    re.compile(r'verification.?(?:fail|reject|timeout|stuck)', re.IGNORECASE),
    re.compile(r'NO_MATCH', re.IGNORECASE),
    re.compile(r'DOCUMENT_EXPIRED', re.IGNORECASE),
    re.compile(r'onboarding.?failed', re.IGNORECASE),
    re.compile(r'ekyc.?(?:fail|error|reject)', re.IGNORECASE),
    re.compile(r'efr.?(?:fail|error|timeout)', re.IGNORECASE),
    re.compile(r'persona.?(?:fail|error|timeout)', re.IGNORECASE),
    re.compile(r'sumsub.?(?:fail|error|reject)', re.IGNORECASE),
    re.compile(r'sdk.?(?:error|fail|null)', re.IGNORECASE),
    re.compile(r'residency.?visa', re.IGNORECASE),
    re.compile(r'NullPointerException', re.IGNORECASE),
    re.compile(r'deserialization.?error', re.IGNORECASE),
    re.compile(r'status\s+from\s+(?:PROCESSING|REJECTED|BLOCKED|DRAFT)', re.IGNORECASE),
    # ─── KYC JSON response body patterns ───
    # Catches rejection data inside JSON like: "status":"REJECTED", "rejection_reasons":["NO_MATCH"]
    re.compile(r'"status"\s*:\s*"(?:REJECTED|BLOCKED|FAILED|SUSPENDED)"', re.IGNORECASE),
    re.compile(r'"rejection_reasons"\s*:\s*\[(?!\s*\])', re.IGNORECASE),  # non-empty array
    re.compile(r'"rejection_count"\s*:\s*[1-9]', re.IGNORECASE),  # non-zero count
    re.compile(r'"previous_status"\s*:\s*"(?:REJECTED|BLOCKED)"', re.IGNORECASE),
    re.compile(r'Customer onboarding failed', re.IGNORECASE),
    re.compile(r'EFR\s+API\s+Timeout', re.IGNORECASE),
    re.compile(r'EFR\s+SDK\s+validation\s+failure', re.IGNORECASE),
    # ─── Other ───
    re.compile(r'rate.?(?:mismatch|expired|lock)', re.IGNORECASE),
    re.compile(r'webhook.?(?:fail|timeout|error)', re.IGNORECASE),
    re.compile(r'partner.?(?:fail|down|timeout|error)', re.IGNORECASE),
]


@dataclass
class LogSearchResult:
    """Result from a CloudWatch log search."""
    query_id: str = ""
    search_term: str = ""
    log_group: str = ""
    total_results: int = 0
    error_lines: list[str] = field(default_factory=list)
    all_lines: list[str] = field(default_factory=list)
    device_ids: list[str] = field(default_factory=list)
    transaction_ids: list[str] = field(default_factory=list)
    request_ids: list[str] = field(default_factory=list)
    kyc_request_ids: list[str] = field(default_factory=list)
    has_errors: bool = False
    error_summary: str = ""


@dataclass
class InvestigationResult:
    """Full investigation result across multiple log searches."""
    category: str = ""
    search_steps: list[LogSearchResult] = field(default_factory=list)
    root_cause: str = ""
    device_id: str = ""
    error_found: bool = False
    summary: str = ""
    analyzed_reason: str = ""  # Claude-analyzed root cause (set by handler)
    services_searched: list[str] = field(default_factory=list)


class CloudWatchSearcher:
    """Search CloudWatch logs for CX-Tech investigation."""

    def __init__(self, aws_region: str, aws_access_key_id: str,
                 aws_secret_access_key: str, aws_session_token: str = "",
                 goblin_log_group: str = ""):
        session_kwargs = {
            "region_name": aws_region,
            "aws_access_key_id": aws_access_key_id,
            "aws_secret_access_key": aws_secret_access_key,
        }
        if aws_session_token:
            session_kwargs["aws_session_token"] = aws_session_token

        self.session = boto3.Session(**session_kwargs)
        self.client = self.session.client("logs")
        logger.info(f"CloudWatch searcher initialized (region={aws_region})")

    def investigate(self, category: str,
                    order_ids: list[str] = None,
                    user_ids: list[str] = None,
                    payment_attempt_ids: list[str] = None,
                    fulfillment_ids: list[str] = None,
                    checkout_pay_ids: list[str] = None,
                    hours_back: int = 48) -> InvestigationResult:
        """Route to category-specific investigation."""
        if category not in CATEGORY_LOG_GROUPS:
            inv = InvestigationResult(category=category)
            inv.summary = (
                f"CloudWatch investigation not yet configured for category '{category}'. "
                f"Only {', '.join(INVESTIGATE_CATEGORIES)} are currently supported."
            )
            return inv

        if category == "kyc_verification":
            return self._investigate_kyc(
                user_ids=user_ids or [],
                hours_back=hours_back,
            )
        else:
            return self._investigate_generic(
                category=category,
                order_ids=order_ids,
                user_ids=user_ids,
                payment_attempt_ids=payment_attempt_ids,
                fulfillment_ids=fulfillment_ids,
                checkout_pay_ids=checkout_pay_ids,
                hours_back=hours_back,
            )

    # ─── KYC-specific investigation ─────────────────────────────────────

    def _investigate_kyc(self, user_ids: list[str],
                         hours_back: int = 48) -> InvestigationResult:
        """KYC investigation:

        Step 1: Search verification-service by user_id
                → response bodies already contain status, rejection_reasons,
                  kyc_provider, rejection_count, etc.
        Step 1b: If errors found, optionally dig deeper with request_id
                 from the brackets for more context.
        Step 2: Search workflow-service by user_id
                → screen-level errors, SDK failures, data collection issues
        Step 3: If device_id found, search workflow-service by device_id
                → broader context on what the user was doing

        Claude analyses ALL collected lines with the KYC prompt + rejection
        reasons knowledge base.
        """
        investigation = InvestigationResult(category="kyc_verification")
        verification_lg = f"{LOG_GROUP_BASE}/verification-service-logs"
        workflow_lg = f"{LOG_GROUP_BASE}/workflow-service-logs"

        if not user_ids:
            investigation.summary = "No user IDs found in the query to search CloudWatch."
            return investigation

        # ─── Step 1: Search verification-service by user_id ──────────────
        # Progressive window: start at hours_back, widen to 7d then 14d if 0 results.
        investigation.services_searched.append("verification-service")
        VERIFICATION_WINDOWS = [hours_back, 168, 336]  # 48h → 7 days → 14 days

        for uid in user_ids:
            step1_result = None
            used_window = hours_back

            for window in VERIFICATION_WINDOWS:
                if window < hours_back:
                    continue  # skip windows smaller than the initial request
                logger.info(f"KYC Step 1: Searching verification-service for user_id={uid} (window={window}h)")
                result = self.search_logs(
                    log_group=verification_lg,
                    search_term=uid,
                    hours_back=window,
                    limit=200,
                )

                if result.total_results > 0:
                    step1_result = result
                    used_window = window
                    logger.info(f"KYC Step 1: Found {result.total_results} lines at {window}h window")
                    break
                else:
                    logger.info(f"KYC Step 1: 0 results at {window}h window, widening...")

            # If still nothing after all windows, keep the last empty result
            if step1_result is None:
                step1_result = result
                logger.info(f"KYC Step 1: No logs found for user_id={uid} even at {VERIFICATION_WINDOWS[-1]}h")

            investigation.search_steps.append(step1_result)

            # Track device_id from bracket parsing
            if step1_result.device_ids and not investigation.device_id:
                investigation.device_id = step1_result.device_ids[0]

            if step1_result.has_errors:
                investigation.error_found = True
                investigation.root_cause = self._summarize_errors(step1_result.error_lines)
                logger.info(f"KYC Step 1: Errors found for user_id={uid}: {investigation.root_cause[:100]}")

                # ─── Step 1b: Dig deeper with request_id if errors found ─
                deep_ids = [rid for rid in step1_result.request_ids
                            if rid != uid and len(rid) >= 16]
                if deep_ids:
                    for rid in list(dict.fromkeys(deep_ids))[:2]:
                        logger.info(f"KYC Step 1b: Deep search verification-service for request_id={rid}")
                        deep_result = self.search_logs(
                            log_group=verification_lg,
                            search_term=rid,
                            hours_back=used_window,
                            limit=200,
                        )
                        investigation.search_steps.append(deep_result)
                        if deep_result.device_ids and not investigation.device_id:
                            investigation.device_id = deep_result.device_ids[0]

        step1_lines = sum(s.total_results for s in investigation.search_steps)
        logger.info(f"KYC Step 1 done: found {step1_lines} lines in verification-service")

        # ─── Step 2: Search workflow-service by user_id ──────────────────
        investigation.services_searched.append("workflow-service")
        for uid in user_ids:
            logger.info(f"KYC Step 2: Searching workflow-service for user_id={uid}")
            result = self.search_logs(
                log_group=workflow_lg,
                search_term=uid,
                hours_back=hours_back,
                limit=200,
            )
            investigation.search_steps.append(result)

            if result.device_ids and not investigation.device_id:
                investigation.device_id = result.device_ids[0]

            if result.has_errors:
                investigation.error_found = True
                investigation.root_cause = self._summarize_errors(result.error_lines)
                logger.info(f"KYC Step 2: Errors found in workflow-service for user_id={uid}: {investigation.root_cause[:100]}")

        # ─── Step 3: Search workflow-service by device_id ────────────────
        if investigation.device_id:
            logger.info(f"KYC Step 3: Searching workflow-service for device_id={investigation.device_id}")
            result = self.search_logs(
                log_group=workflow_lg,
                search_term=investigation.device_id,
                hours_back=hours_back,
                limit=200,
            )
            investigation.search_steps.append(result)

            if result.has_errors:
                investigation.error_found = True
                investigation.root_cause = self._summarize_errors(result.error_lines)
                logger.info(f"KYC Step 3: Errors found in workflow-service via device_id: {investigation.root_cause[:100]}")

        # ─── Summarize ───────────────────────────────────────────────────
        total_lines = sum(s.total_results for s in investigation.search_steps)
        if investigation.error_found:
            investigation.summary = (
                f"Errors found across {', '.join(investigation.services_searched)} "
                f"({total_lines} total log lines)."
            )
        elif total_lines > 0:
            investigation.summary = (
                f"Found {total_lines} log lines across {', '.join(investigation.services_searched)} "
                f"but no clear error patterns matched."
            )
        else:
            investigation.summary = (
                f"No relevant logs found in {', '.join(investigation.services_searched)} "
                f"for the provided user IDs in the last {hours_back} hours."
            )
        return investigation

    # ─── Generic investigation (payment, etc.) ────────────────────────────

    def _investigate_generic(self, category: str,
                             order_ids: list[str] = None,
                             user_ids: list[str] = None,
                             payment_attempt_ids: list[str] = None,
                             fulfillment_ids: list[str] = None,
                             checkout_pay_ids: list[str] = None,
                             hours_back: int = 48) -> InvestigationResult:
        """Generic investigation for payment and other categories.

        Flow:
        1. Search each log group with each ID (stop at first error hit)
        2. If no errors found but device_id exists → search again with device_id
        3. If nothing found → report
        """
        investigation = InvestigationResult(category=category)
        log_groups = CATEGORY_LOG_GROUPS[category]

        # Build search terms — most specific first
        search_terms = []
        for oid in (order_ids or []):
            search_terms.append(("order_id", oid))
        for paid in (payment_attempt_ids or []):
            search_terms.append(("payment_attempt_id", paid))
        for cpid in (checkout_pay_ids or []):
            search_terms.append(("checkout_pay_id", cpid))
        for fid in (fulfillment_ids or []):
            search_terms.append(("fulfillment_id", fid))
        for uid in (user_ids or []):
            search_terms.append(("user_id", uid))

        if not search_terms:
            investigation.summary = "No IDs (order, user, payment, etc.) found in the query to search CloudWatch."
            return investigation

        # ─── Step 1: Search each log group with each ID ─────────────────
        for log_group in log_groups:
            service_name = log_group.split("/")[-1].replace("-logs", "")
            investigation.services_searched.append(service_name)

            for id_type, search_term in search_terms:
                logger.info(f"Searching {service_name} for {id_type}={search_term}")
                result = self.search_logs(
                    log_group=log_group,
                    search_term=search_term,
                    hours_back=hours_back,
                )
                investigation.search_steps.append(result)

                if result.has_errors:
                    investigation.error_found = True
                    investigation.root_cause = self._summarize_errors(result.error_lines)
                    investigation.summary = (
                        f"Errors found in {service_name} for {id_type}={search_term}"
                    )
                    if result.device_ids:
                        investigation.device_id = result.device_ids[0]
                    logger.info(f"Errors found in {service_name}: {investigation.root_cause[:100]}")
                    return investigation

                if result.total_results > 0 and result.device_ids:
                    investigation.device_id = result.device_ids[0]

        # ─── Step 2: device_id fallback search ──────────────────────────
        if investigation.device_id and not investigation.error_found:
            logger.info(f"Step 2: Searching with device_id={investigation.device_id}")
            for log_group in log_groups:
                service_name = log_group.split("/")[-1].replace("-logs", "")
                device_result = self.search_logs(
                    log_group=log_group,
                    search_term=investigation.device_id,
                    hours_back=hours_back,
                    limit=200,
                )
                investigation.search_steps.append(device_result)

                if device_result.has_errors:
                    investigation.error_found = True
                    investigation.root_cause = self._summarize_errors(device_result.error_lines)
                    investigation.summary = (
                        f"Errors found in {service_name} via device_id={investigation.device_id}"
                    )
                    return investigation

            # Logs exist but no errors
            total_lines = sum(s.total_results for s in investigation.search_steps)
            investigation.summary = (
                f"Found {total_lines} log lines across {', '.join(investigation.services_searched)} "
                f"for device_id={investigation.device_id} but no clear errors."
            )
            return investigation

        # ─── Nothing or logs with no errors ───────────────────────────────
        total_lines = sum(s.total_results for s in investigation.search_steps)
        if total_lines > 0:
            investigation.summary = (
                f"Found {total_lines} log lines across {', '.join(investigation.services_searched)} "
                f"but no clear error patterns matched."
            )
        else:
            investigation.summary = (
                f"No relevant logs found in {', '.join(investigation.services_searched)} "
                f"for the provided IDs in the last {hours_back} hours."
            )
        return investigation

    # ─── Core search method ───────────────────────────────────────────────

    def search_logs(self, log_group: str, search_term: str,
                    hours_back: int = 24, limit: int = 100) -> LogSearchResult:
        """Search a log group using CloudWatch Logs Insights query."""
        result = LogSearchResult(
            search_term=search_term,
            log_group=log_group,
        )

        try:
            end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
            start_time = int((datetime.now(timezone.utc) - timedelta(hours=hours_back)).timestamp() * 1000)

            query = (
                f'fields @timestamp, @message '
                f'| filter @message like /{search_term}/ '
                f'| sort @timestamp desc '
                f'| limit {limit}'
            )

            service_name = log_group.split("/")[-1].replace("-logs", "")
            logger.info(f"CW query: {service_name} for '{search_term}' (last {hours_back}h)")

            response = self.client.start_query(
                logGroupName=log_group,
                startTime=start_time,
                endTime=end_time,
                queryString=query,
            )
            query_id = response["queryId"]
            result.query_id = query_id

            results_data = self._wait_for_query(query_id, timeout=30)

            if not results_data:
                result.error_summary = f"No logs found in last {hours_back}h"
                return result

            # Parse results
            for row in results_data:
                message = ""
                for field_item in row:
                    if field_item["field"] == "@message":
                        message = field_item["value"]
                        break

                if not message:
                    continue

                result.all_lines.append(message.strip())
                result.total_results += 1

                # Extract structured fields — try 3-bracket format first, then 2-bracket
                match = LOG_LINE_PATTERN_3.match(message)
                if match:
                    # Goblin/payment format: [txn_id][req_id][device_id]
                    txn_id, req_id, device_id = match.group(1), match.group(2), match.group(3)
                    if txn_id and txn_id not in result.transaction_ids:
                        result.transaction_ids.append(txn_id)
                    if req_id and req_id not in result.request_ids:
                        result.request_ids.append(req_id)
                    if device_id and device_id not in result.device_ids:
                        result.device_ids.append(device_id)
                else:
                    match2 = LOG_LINE_PATTERN_2.match(message)
                    if match2:
                        # Verification/workflow format: [request_id][device_id]
                        req_id, device_id = match2.group(1), match2.group(2)
                        if req_id and req_id not in result.request_ids:
                            result.request_ids.append(req_id)
                        if device_id and device_id not in result.device_ids:
                            result.device_ids.append(device_id)

                # Check for error patterns
                if any(p.search(message) for p in ERROR_PATTERNS):
                    result.error_lines.append(message.strip())
                    result.has_errors = True

            if result.has_errors:
                result.error_summary = f"{len(result.error_lines)} error(s) in {result.total_results} lines"
            else:
                result.error_summary = f"{result.total_results} lines, no obvious errors"

            logger.info(
                f"CW search done: {result.total_results} lines, "
                f"{len(result.error_lines)} errors, "
                f"{len(result.device_ids)} device_ids"
            )
            return result

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            error_msg = e.response["Error"]["Message"]
            logger.error(f"CloudWatch API error: {error_code} - {error_msg}")
            result.error_summary = f"AWS error: {error_code} - {error_msg}"
            return result
        except Exception as e:
            logger.error(f"CloudWatch search failed: {e}")
            result.error_summary = f"Search failed: {str(e)}"
            return result

    def _wait_for_query(self, query_id: str, timeout: int = 30) -> list:
        """Poll CloudWatch Insights query until complete or timeout."""
        start = time.time()
        while time.time() - start < timeout:
            try:
                response = self.client.get_query_results(queryId=query_id)
                status = response["status"]
                if status == "Complete":
                    return response.get("results", [])
                elif status in ("Failed", "Cancelled"):
                    logger.error(f"CW query {query_id} status: {status}")
                    return []
                time.sleep(1)
            except Exception as e:
                logger.error(f"Error polling CW query {query_id}: {e}")
                return []
        logger.warning(f"CW query {query_id} timed out after {timeout}s")
        return []

    def _summarize_errors(self, error_lines: list[str], max_lines: int = 5) -> str:
        """Deduplicate and summarize error lines."""
        if not error_lines:
            return "No errors found"
        seen = set()
        unique = []
        for line in error_lines:
            normalized = re.sub(r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}', '', line)
            normalized = re.sub(r'\[[^\]]+\]', '', normalized).strip()
            if normalized not in seen:
                seen.add(normalized)
                unique.append(line.strip())
            if len(unique) >= max_lines:
                break
        return " | ".join(unique[:max_lines])
