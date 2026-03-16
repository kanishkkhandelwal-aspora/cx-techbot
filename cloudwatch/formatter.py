"""Format CloudWatch investigation results for Slack."""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cloudwatch.log_searcher import InvestigationResult


def format_investigation(investigation: "InvestigationResult") -> str:
    """Format investigation results as a Slack mrkdwn block.

    Shows the analyzed root cause, not raw log lines.
    """
    # Case 1: Nothing found in CloudWatch at all
    if not investigation.error_found and not investigation.device_id and not investigation.analyzed_reason:
        return (
            ":cloud: *CloudWatch Investigation*\n\n"
            ":x: Could not find any relevant logs in CloudWatch for the provided IDs.\n"
            f"_{investigation.summary}_\n"
        )

    parts = [":cloud: *CloudWatch Investigation*\n\n"]

    # Case 2: Errors found + analyzed reason (best case)
    if investigation.error_found and investigation.analyzed_reason:
        parts.append(":rotating_light: *Root Cause Identified*\n\n")
        parts.append(f"{investigation.analyzed_reason}\n")

    # Case 3: Logs found, no error patterns matched, but Claude analyzed them
    elif not investigation.error_found and investigation.analyzed_reason:
        parts.append(":eyes: *Log Analysis*\n\n")
        parts.append(f"{investigation.analyzed_reason}\n")

    # Case 4: Logs found with device_id but no analysis
    elif not investigation.error_found and investigation.device_id and not investigation.analyzed_reason:
        parts.append(":eyes: *Logs found but no clear errors*\n\n")
        parts.append(f"_{investigation.summary}_\n")

    # Case 5: Errors found but analysis failed
    elif investigation.error_found and not investigation.analyzed_reason:
        parts.append(":rotating_light: *Errors Found*\n\n")
        parts.append(f"_{investigation.summary}_\n")

    # Show device ID if found
    if investigation.device_id:
        parts.append(f"\n*Device ID:* `{investigation.device_id}`\n")

    # Show which services were searched
    if investigation.services_searched:
        services = ", ".join(investigation.services_searched)
        parts.append(f"\n_Services searched: {services}_\n")

    return "".join(parts)
