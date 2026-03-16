"""Glue module: classify -> investigate -> analyze -> assign -> format -> post -> record."""

import logging
import re

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

logger = logging.getLogger(__name__)

# UUID pattern for extracting IDs from messages
UUID_PATTERN = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.IGNORECASE)


class Handler:
    def __init__(self, classifier: CXClassifier, assigner: Assigner,
                 metrics_db: MetricsDB, poller: Poller | None,
                 cw_searcher: CloudWatchSearcher | None = None,
                 anthropic_client: anthropic.Anthropic | None = None,
                 classifier_model: str = "claude-haiku-4-5-20251001"):
        self.classifier = classifier
        self.assigner = assigner
        self.metrics = metrics_db
        self.poller = poller
        self.cw_searcher = cw_searcher
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
            # 1. Classify
            result = self.classifier.classify(msg.text)

            # 2. Check if triage needed
            is_triage = (
                result.category == "other_needs_triage"
                or result.confidence < 0.5
            )

            # 3. Assign
            assignment = self.assigner.assign(result.category, msg.timestamp)

            # 4. CloudWatch investigation + Claude analysis (enabled categories only)
            analysis = None
            services_searched = []
            category_enabled = result.category in INVESTIGATE_CATEGORIES

            if self.cw_searcher and category_enabled and not is_triage:
                investigation = self._investigate(msg, result)
                if investigation:
                    services_searched = investigation.services_searched
                    # Parse Claude's structured response into sections
                    if investigation.analyzed_reason:
                        analysis = parse_structured_analysis(investigation.analyzed_reason)
                    else:
                        # No logs found or Claude not invoked — build a helpful
                        # fallback so the formatter still shows Root Cause / CX Advice.
                        total_lines = sum(
                            s.total_results for s in investigation.search_steps
                        )
                        if total_lines == 0:
                            svc_list = ", ".join(services_searched) if services_searched else "CloudWatch"
                            if result.category == "kyc_verification":
                                analysis = {
                                    "root_cause": (
                                        f"• No logs found for the provided user ID "
                                        f"in {svc_list} (last 48 hours).\n"
                                        f"• This could mean the user hasn't initiated "
                                        f"the KYC/verification flow yet, or the user ID "
                                        f"may be incorrect."
                                    ),
                                    "cx_advice": (
                                        "• Verify the user ID is correct.\n"
                                        "• Ask the customer to retry the verification flow.\n"
                                        "• If the issue persists, escalate to the assigned engineer."
                                    ),
                                }
                            else:
                                analysis = {
                                    "root_cause": (
                                        f"• No logs found for the provided IDs "
                                        f"in {svc_list} (last 48 hours).\n"
                                        f"• The transaction may not have reached the "
                                        f"backend, or the IDs may be incorrect."
                                    ),
                                    "cx_advice": (
                                        "• Verify the IDs provided are correct.\n"
                                        "• Ask the customer to retry the transaction.\n"
                                        "• If the issue persists, escalate to the assigned engineer."
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
                )

            self.poller.post_message(msg.channel, response, msg.timestamp)

            # 6. Record to DB
            self.metrics.record(msg, result, assignment)

            # 7. Mark done (eyes -> checkmark)
            self.poller.ack_done(msg.channel, msg.timestamp)
            self.poller.mark_done(msg.timestamp)

            logger.info(
                "Handled message ts=%s category=%s assigned_to=%s confidence=%.2f",
                msg.timestamp, result.category, assignment.engineer, result.confidence,
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

    # ─── Normal investigation flow ─────────────────────────────────────────

    def _investigate(self, msg: SlackMessage, classification: CXClassification):
        """Run CloudWatch investigation + Claude analysis for any category.

        Returns InvestigationResult or None on failure.
        """
        try:
            logger.info(
                f"Starting CloudWatch investigation for {msg.timestamp} "
                f"(category={classification.category})"
            )

            # Use the generic investigate() — it knows which log groups per category
            investigation = self.cw_searcher.investigate(
                category=classification.category,
                order_ids=classification.order_ids,
                user_ids=classification.user_ids,
                payment_attempt_ids=classification.payment_attempt_ids,
                fulfillment_ids=classification.fulfillment_ids,
                checkout_pay_ids=classification.checkout_pay_ids,
                hours_back=48,
            )

            # Use Claude to analyze the logs and extract the structured response
            all_log_lines = []
            if classification.category == "kyc_verification":
                # For KYC, send ALL lines — the JSON response bodies contain
                # status, rejection_reasons, rejection_count, kyc_provider etc.
                # Claude needs the full picture to diagnose KYC issues.
                for step in investigation.search_steps:
                    all_log_lines.extend(step.all_lines)
            else:
                # For other categories, prioritize error lines
                for step in investigation.search_steps:
                    if step.error_lines:
                        all_log_lines.extend(step.error_lines)
                    elif step.all_lines:
                        all_log_lines.extend(step.all_lines)

            if all_log_lines and self.anthropic_client:
                context = (
                    f"Category: {classification.category}\n"
                    f"Original CX query: {msg.text[:300]}\n"
                    f"Services searched: {', '.join(investigation.services_searched)}"
                )
                analyzed = analyze_logs_with_claude(
                    client=self.anthropic_client,
                    log_lines=all_log_lines,
                    model=self.classifier_model,
                    context=context,
                    category=classification.category,
                )
                investigation.analyzed_reason = analyzed
                logger.info(f"Claude analysis: {analyzed[:150]}...")

            logger.info(
                f"Investigation complete for {msg.timestamp}: "
                f"error_found={investigation.error_found}, "
                f"device_id={investigation.device_id}, "
                f"services={investigation.services_searched}, "
                f"steps={len(investigation.search_steps)}"
            )
            return investigation

        except Exception as e:
            logger.error(f"Investigation failed for {msg.timestamp}: {e}")
            return None
