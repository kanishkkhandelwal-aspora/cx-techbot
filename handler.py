"""Glue module: classify -> investigate (CW + DB in parallel) -> synthesize -> assign -> format -> post -> record.

Multi-agent architecture:
  Agent 1: CloudWatch log searcher (unstructured logs)
  Agent 2: Databricks SQL searcher (structured DB records)
  Parent:  Claude synthesizer (merges both inputs into root cause + CX advice)
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic

from classifier.classifier import CXClassifier, CXClassification
from assigner.assigner import Assigner, Assignment
from slack_bot.poller import Poller, SlackMessage
from slack_bot.formatter import (
    format_full_response, format_triage_response, format_direct_search_response,
)
from metrics.db import MetricsDB
from cloudwatch.log_searcher import (
    CloudWatchSearcher, INVESTIGATE_CATEGORIES, SERVICE_ALIASES, SERVICE_DISPLAY_NAMES,
)
from cloudwatch.log_analyzer import analyze_logs_with_claude, parse_structured_analysis
from db_agent.db_searcher import DatabricksSearcher, INVESTIGATE_CATEGORIES as DB_INVESTIGATE_CATEGORIES

logger = logging.getLogger(__name__)

# UUID pattern for extracting IDs from messages
UUID_PATTERN = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.IGNORECASE)


class Handler:
    def __init__(self, classifier: CXClassifier, assigner: Assigner,
                 metrics_db: MetricsDB, poller: Poller | None,
                 cw_searcher: CloudWatchSearcher | None = None,
                 db_searcher: DatabricksSearcher | None = None,
                 anthropic_client: anthropic.Anthropic | None = None,
                 classifier_model: str = "claude-haiku-4-5-20251001"):
        self.classifier = classifier
        self.assigner = assigner
        self.metrics = metrics_db
        self.poller = poller
        self.cw_searcher = cw_searcher
        self.db_searcher = db_searcher
        self.anthropic_client = anthropic_client
        self.classifier_model = classifier_model

    def handle(self, msg: SlackMessage):
        """Route: bot @mention → direct search, otherwise → normal classify flow."""
        if msg.is_bot_mention:
            return self._handle_direct_search(msg)
        return self._handle_classify(msg)

    def _handle_classify(self, msg: SlackMessage):
        """Classify -> Investigate -> Analyze -> Assign -> Post single response -> Record."""
        try:
            start_time = time.monotonic()

            # 1. Classify
            result = self.classifier.classify(msg.text)

            # 2. Check if triage needed
            is_triage = (
                result.category == "other_needs_triage"
                or result.confidence < 0.5
            )

            # 3. Assign
            assignment = self.assigner.assign(result.category, msg.timestamp)

            # 4. Multi-agent investigation: CloudWatch + Databricks in parallel
            #    Agent 1: CloudWatch logs (unstructured)
            #    Agent 2: Databricks SQL (structured)
            #    Parent: Claude synthesizer
            analysis = None
            investigation = None
            db_investigation = None
            services_searched = []
            data_sources = []  # Track which sources provided data
            cw_enabled = result.category in INVESTIGATE_CATEGORIES
            db_enabled = result.category in DB_INVESTIGATE_CATEGORIES

            if not is_triage and (
                (self.cw_searcher and cw_enabled) or (self.db_searcher and db_enabled)
            ):
                investigation, db_investigation = self._investigate_parallel(
                    msg, result, cw_enabled, db_enabled,
                )

                # Collect services searched from CloudWatch
                if investigation:
                    services_searched = investigation.services_searched

                # Collect data sources
                if investigation and any(s.total_results > 0 for s in investigation.search_steps):
                    data_sources.append("CloudWatch")
                if db_investigation and db_investigation.has_data:
                    data_sources.append("Databricks")

                # Get log lines + DB summary for Claude synthesis
                all_log_lines = []
                db_summary = ""

                if investigation:
                    for step in investigation.search_steps:
                        all_log_lines.extend(step.all_lines)

                if db_investigation and db_investigation.has_data:
                    db_summary = db_investigation.summary_text

                # Run Claude synthesis (Parent Agent)
                if (all_log_lines or db_summary) and self.anthropic_client:
                    context = (
                        f"Category: {result.category}\n"
                        f"Original CX query: {msg.text[:300]}\n"
                        f"Data sources: {', '.join(data_sources) if data_sources else 'none'}"
                    )
                    if investigation:
                        context += f"\nServices searched: {', '.join(investigation.services_searched)}"

                    analyzed = analyze_logs_with_claude(
                        client=self.anthropic_client,
                        log_lines=all_log_lines,
                        model=self.classifier_model,
                        context=context,
                        category=result.category,
                        db_summary=db_summary,
                    )
                    if analyzed:
                        analysis = parse_structured_analysis(analyzed)
                        logger.info(f"Claude synthesis: {analyzed[:150]}...")

                # Fallback when no data from either source
                if not analysis:
                    has_cw_lines = investigation and sum(
                        s.total_results for s in investigation.search_steps
                    ) > 0
                    has_db_data = db_investigation and db_investigation.has_data

                    if not has_cw_lines and not has_db_data:
                        # Extract IDs from classification for the fallback
                        ids_str = ""
                        if result.user_ids:
                            ids_str = f"user_id: {result.user_ids[0]}"
                        elif result.order_ids:
                            ids_str = f"order_id: {result.order_ids[0]}"

                        if result.category == "kyc_verification":
                            analysis = {
                                "root_cause": (
                                    f"• No KYC record found for {ids_str or 'provided ID'} in database (searched up to 14 days).\n"
                                    f"• User likely hasn't initiated the KYC flow yet."
                                ),
                                "cx_advice": (
                                    "• Confirm with customer if they started the KYC process in the app.\n"
                                    "• If yes, ask them to close and reopen the app, then retry KYC from scratch."
                                ),
                            }
                        else:
                            analysis = {
                                "root_cause": (
                                    f"• No transaction record found for {ids_str or 'provided ID'} in database (searched up to 14 days).\n"
                                    f"• Payment likely didn't reach the backend — user may not have completed checkout."
                                ),
                                "cx_advice": (
                                    "• Confirm with customer if the payment screen loaded and they entered card details.\n"
                                    "• If amount was debited from bank, it will auto-reverse within 48h."
                                ),
                            }

            # 5. Format & post single combined response
            if is_triage:
                response = format_triage_response(result, assignment, msg.user)
            else:
                response = format_full_response(
                    classification=result,
                    assignment=assignment,
                    analysis=analysis,
                    poster_user_id=msg.user,
                    services_searched=services_searched,
                    data_sources=data_sources,
                )

            self.poller.post_message(msg.channel, response, msg.timestamp)

            # 6. Record to DB with enriched metrics
            elapsed_ms = int((time.monotonic() - start_time) * 1000)

            # Compute log/row counts for metrics
            cw_log_lines = 0
            if investigation:
                cw_log_lines = sum(s.total_results for s in investigation.search_steps)
            db_rows = 0
            if db_investigation:
                db_rows = sum(q.row_count for q in db_investigation.queries_run)

            root_cause_summary = ""
            if analysis and analysis.get("root_cause"):
                root_cause_summary = analysis["root_cause"]

            self.metrics.record(
                msg, result, assignment,
                response_time_ms=elapsed_ms,
                data_sources=data_sources,
                error_found=bool(investigation and investigation.error_found),
                root_cause_summary=root_cause_summary,
                services_searched=services_searched,
                is_triage=is_triage,
                cw_log_lines=cw_log_lines,
                db_rows=db_rows,
            )

            # 7. Mark done (eyes -> checkmark)
            self.poller.ack_done(msg.channel, msg.timestamp)
            self.poller.mark_done(msg.timestamp)

            logger.info(
                "Handled message ts=%s category=%s assigned_to=%s confidence=%.2f elapsed=%dms",
                msg.timestamp, result.category, assignment.engineer, result.confidence, elapsed_ms,
            )

        except Exception as e:
            # NEVER crash. Log the error, try to escalate.
            logger.error(f"Handler error for {msg.timestamp}: {e}")
            try:
                fallback_assignment = self.assigner.assign("other_needs_triage", msg.timestamp)
                self.poller.post_message(
                    msg.channel,
                    f":warning: *CX-Tech Bot — Error*\n\n"
                    f"Bot encountered an error classifying this query.\n"
                    f"*Assigned to:* {fallback_assignment.slack_tag}\n\n"
                    f"_{fallback_assignment.slack_tag}, please triage this manually._",
                    msg.timestamp,
                )
                self.poller.ack_done(msg.channel, msg.timestamp)
                self.poller.mark_done(msg.timestamp)
            except Exception:
                logger.error(f"Failed to post fallback for {msg.timestamp}")

    # ─── Direct @bot search ──────────────────────────────────────────────

    def _handle_direct_search(self, msg: SlackMessage):
        """Handle @bot mentions: targeted search of a specific service.

        Supports commands like:
            @bot search <user_id> in <service>
            @bot check <service> for <user_id>
            @bot search verification-service for 2943a5e9-...
        """
        try:
            # Strip the bot mention tag from text
            clean_text = re.sub(r'<@[A-Z0-9]+>\s*', '', msg.text).strip()

            # Extract UUIDs from the command text
            ids_in_cmd = UUID_PATTERN.findall(clean_text)

            # If no UUID in the command and this is a thread reply,
            # look at the parent message to grab IDs
            if not ids_in_cmd and msg.thread_ts != msg.timestamp:
                ids_in_cmd = self._get_ids_from_parent(msg)

            # Extract service name — match any known alias in the text
            service_log_group = None
            service_display = None
            text_lower = clean_text.lower()
            # Sort by longest alias first to match "verification-service" before "verification"
            for alias in sorted(SERVICE_ALIASES, key=len, reverse=True):
                if alias in text_lower:
                    service_log_group = SERVICE_ALIASES[alias]
                    service_display = SERVICE_DISPLAY_NAMES.get(service_log_group, alias)
                    break

            # Validate we have what we need
            if not ids_in_cmd:
                self.poller.post_message(
                    msg.channel,
                    ":warning: *CX-Tech Bot* — I couldn't find a user/order ID in your message or the parent thread.\n"
                    "Usage: `@bot search <user_id> in <service-name>`\n"
                    "Available services: `goblin`, `app-server`, `goms`, `verification`, `workflow`",
                    msg.thread_ts,
                )
                self.poller.ack_done(msg.channel, msg.timestamp)
                self.poller.mark_done(msg.timestamp)
                return

            if not service_log_group:
                self.poller.post_message(
                    msg.channel,
                    ":warning: *CX-Tech Bot* — I couldn't identify which service to search.\n"
                    "Usage: `@bot search <user_id> in <service-name>`\n"
                    "Available services: `goblin`, `app-server`, `goms`, `verification`, `workflow`",
                    msg.thread_ts,
                )
                self.poller.ack_done(msg.channel, msg.timestamp)
                self.poller.mark_done(msg.timestamp)
                return

            # Run the search
            search_id = ids_in_cmd[0]  # use the first ID found
            logger.info(f"Direct search: {search_id} in {service_display}")

            result = self.cw_searcher.search_logs(
                log_group=service_log_group,
                search_term=search_id,
                hours_back=336,  # 14 days — wide window for direct searches
                limit=200,
            )

            # Analyze with Claude if we got results
            analysis = None
            if result.all_lines and self.anthropic_client:
                context = (
                    f"Direct search by CX agent.\n"
                    f"Search ID: {search_id}\n"
                    f"Service: {service_display}\n"
                    f"Original request: {clean_text[:300]}"
                )
                raw_analysis = analyze_logs_with_claude(
                    client=self.anthropic_client,
                    log_lines=result.all_lines,
                    model=self.classifier_model,
                    context=context,
                    category="kyc_verification" if "verification" in service_display or "workflow" in service_display else "",
                )
                if raw_analysis:
                    analysis = parse_structured_analysis(raw_analysis)

            # No logs found fallback
            if not analysis or (not analysis.get("root_cause") and not analysis.get("cx_advice")):
                if result.total_results == 0:
                    analysis = {
                        "root_cause": f"• No logs found for `{search_id}` in {service_display} (last 14 days).",
                        "cx_advice": "• Verify the ID is correct.\n• Try searching a different service.",
                    }
                else:
                    analysis = {
                        "root_cause": f"• Found {result.total_results} log lines in {service_display} but no clear errors detected.",
                        "cx_advice": "• The logs are available but no obvious failure pattern was found.\n• Escalate to the assigned engineer for deeper investigation.",
                    }

            # Format and post
            response = format_direct_search_response(
                search_id=search_id,
                service=service_display,
                analysis=analysis,
                total_lines=result.total_results,
                error_lines=len(result.error_lines),
                poster_user_id=msg.user,
            )
            self.poller.post_message(msg.channel, response, msg.thread_ts)
            self.poller.ack_done(msg.channel, msg.timestamp)
            self.poller.mark_done(msg.timestamp)

            logger.info(
                f"Direct search done: {search_id} in {service_display} "
                f"→ {result.total_results} lines, {len(result.error_lines)} errors"
            )

        except Exception as e:
            logger.error(f"Direct search error for {msg.timestamp}: {e}")
            try:
                self.poller.post_message(
                    msg.channel,
                    f":warning: *CX-Tech Bot* — Error running direct search: {str(e)[:200]}",
                    msg.thread_ts,
                )
                self.poller.ack_done(msg.channel, msg.timestamp)
                self.poller.mark_done(msg.timestamp)
            except Exception:
                logger.error(f"Failed to post direct search error for {msg.timestamp}")

    def _get_ids_from_parent(self, msg: SlackMessage) -> list[str]:
        """Fetch the parent thread message and extract UUIDs from it."""
        try:
            resp = self.poller.client.conversations_replies(
                channel=msg.channel,
                ts=msg.thread_ts,
                limit=1,
                inclusive=True,
            )
            messages = resp.get("messages", [])
            if messages:
                parent_text = messages[0].get("text", "")
                return UUID_PATTERN.findall(parent_text)
        except Exception as e:
            logger.error(f"Failed to fetch parent message: {e}")
        return []

    # ─── Multi-agent investigation flow ─────────────────────────────────────

    def _investigate_parallel(self, msg, classification, cw_enabled, db_enabled):
        """Run Agent 1 (CloudWatch) + Agent 2 (Databricks) in parallel.

        ALWAYS fires both agents concurrently via ThreadPoolExecutor.
        If one agent has no searcher or isn't enabled, it's simply skipped.
        Each agent has a 90s timeout to prevent blocking.

        Returns (InvestigationResult | None, DBInvestigationResult | None).
        """
        investigation = None
        db_investigation = None

        futures = {}
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent") as pool:
            # Agent 1: CloudWatch
            if self.cw_searcher and cw_enabled:
                logger.info("[Parallel] Launching Agent 1 (CloudWatch)")
                futures["cw"] = pool.submit(
                    self._run_cw_investigation, msg, classification,
                )
            else:
                logger.info("[Parallel] Agent 1 (CloudWatch) skipped — %s",
                            "no searcher" if not self.cw_searcher else "category not enabled")

            # Agent 2: Databricks
            if self.db_searcher and db_enabled:
                logger.info("[Parallel] Launching Agent 2 (Databricks)")
                futures["db"] = pool.submit(
                    self._run_db_investigation, classification,
                )
            else:
                logger.info("[Parallel] Agent 2 (Databricks) skipped — %s",
                            "no searcher" if not self.db_searcher else "category not enabled")

            # Collect results — both should complete nearly simultaneously
            for key, future in futures.items():
                try:
                    result = future.result(timeout=90)
                    if key == "cw":
                        investigation = result
                    else:
                        db_investigation = result
                except Exception as e:
                    logger.error(f"Agent {key} failed or timed out: {e}")

        # Log parallel completion
        cw_lines = sum(s.total_results for s in investigation.search_steps) if investigation else 0
        db_rows = sum(q.row_count for q in db_investigation.queries_run) if db_investigation else 0
        logger.info(f"[Parallel] Both agents done — CW: {cw_lines} lines, DB: {db_rows} rows")

        return investigation, db_investigation

    def _run_cw_investigation(self, msg, classification):
        """Agent 1: CloudWatch log investigation."""
        try:
            logger.info(
                f"[Agent 1/CW] Starting investigation for {msg.timestamp} "
                f"(category={classification.category})"
            )
            investigation = self.cw_searcher.investigate(
                category=classification.category,
                order_ids=classification.order_ids,
                user_ids=classification.user_ids,
                payment_attempt_ids=classification.payment_attempt_ids,
                fulfillment_ids=classification.fulfillment_ids,
                checkout_pay_ids=classification.checkout_pay_ids,
                hours_back=48,
            )
            total = sum(s.total_results for s in investigation.search_steps)
            logger.info(
                f"[Agent 1/CW] Done: {total} lines, "
                f"error_found={investigation.error_found}, "
                f"services={investigation.services_searched}"
            )
            return investigation
        except Exception as e:
            logger.error(f"[Agent 1/CW] Failed: {e}")
            return None

    def _run_db_investigation(self, classification):
        """Agent 2: Databricks SQL investigation."""
        try:
            logger.info(
                f"[Agent 2/DB] Starting investigation "
                f"(category={classification.category})"
            )
            db_inv = self.db_searcher.investigate(
                category=classification.category,
                order_ids=classification.order_ids,
                user_ids=classification.user_ids,
                payment_attempt_ids=classification.payment_attempt_ids,
                fulfillment_ids=classification.fulfillment_ids,
                checkout_pay_ids=classification.checkout_pay_ids,
            )
            logger.info(
                f"[Agent 2/DB] Done: has_data={db_inv.has_data}, "
                f"tables={db_inv.tables_searched}, "
                f"queries={len(db_inv.queries_run)}"
            )
            return db_inv
        except Exception as e:
            logger.error(f"[Agent 2/DB] Failed: {e}")
            return None
