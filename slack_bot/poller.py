"""Polls Slack channel for new messages with cursor persistence and dedup."""

import logging
import os
import time
from dataclasses import dataclass
from typing import Callable

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)

MAX_SLACK_MSG_LEN = 39000
RETRY_DELAYS = [2, 5, 10]  # seconds for 429 retries (increased to avoid cascading)


@dataclass
class SlackMessage:
    text: str
    user: str
    timestamp: str
    thread_ts: str
    channel: str
    is_bot_mention: bool = False  # True when user @mentions the bot


class Poller:
    def __init__(
        self,
        client: WebClient,
        channel_id: str,
        interval: int,
        cursor_file: str,
        bot_user_id: str,
        on_message: Callable,
    ):
        self.client = client
        self.channel_id = channel_id
        self.interval = interval
        self.cursor_file = cursor_file
        self.bot_user_id = bot_user_id
        self.on_message = on_message
        self.last_ts: str = ""
        self.processing: set[str] = set()
        self.completed: dict[str, float] = {}  # ts -> completion unix time

    def run(self):
        """Blocking loop. Polls until KeyboardInterrupt / SIGTERM."""
        self._init_cursor()
        logger.info(f"Poller started. Cursor: {self.last_ts}")
        poll_count = 0
        consecutive_rate_limits = 0
        while True:
            try:
                self._poll()
                consecutive_rate_limits = 0  # Reset on success
            except Exception as e:
                error_str = str(e).lower()
                if "ratelimited" in error_str or "429" in error_str:
                    consecutive_rate_limits += 1
                    backoff = min(30 * consecutive_rate_limits, 120)
                    logger.warning(
                        f"Rate limited at poll level ({consecutive_rate_limits}x). "
                        f"Backing off {backoff}s"
                    )
                    time.sleep(backoff)
                    continue  # Skip thread scan this cycle
                else:
                    logger.error(f"Poll cycle error: {e}")
                    consecutive_rate_limits = 0

            # Scan threads for @bot mentions every 20th cycle (~5 min at 15s interval)
            # Only if we're not in a rate-limit backoff state
            poll_count += 1
            if poll_count % 20 == 0 and consecutive_rate_limits == 0:
                try:
                    self._poll_thread_mentions()
                except Exception as e:
                    logger.error(f"Thread mention poll error: {e}")

            self._cleanup_completed()
            time.sleep(self.interval)

    @staticmethod
    def _normalize_ts(ts: str) -> str:
        """Ensure timestamp has exactly 6 decimal digits (Slack format).

        Slack timestamps are always '<epoch>.<6digits>'. If extra decimals
        sneak in (e.g., from time.time()), the API silently returns 0 results.
        """
        if "." in ts:
            integer, frac = ts.split(".", 1)
            frac = (frac + "000000")[:6]  # pad or truncate to 6
            return f"{integer}.{frac}"
        return ts

    def _init_cursor(self):
        """Load cursor from file, or fetch latest message timestamp from Slack."""
        if os.path.exists(self.cursor_file):
            try:
                with open(self.cursor_file, "r") as f:
                    self.last_ts = self._normalize_ts(f.read().strip())
                if self.last_ts:
                    logger.info(f"Loaded cursor from file: {self.last_ts}")
                    return
            except Exception as e:
                logger.error(f"Failed to read cursor file: {e}")

        # No cursor file — fetch latest message to avoid processing history
        try:
            resp = self.client.conversations_history(
                channel=self.channel_id, limit=1
            )
            messages = resp.get("messages", [])
            if messages:
                self.last_ts = messages[0]["ts"]
                logger.info(f"Initialized cursor from latest message: {self.last_ts}")
            else:
                self.last_ts = "0"
                logger.info("Channel has no messages, starting from 0")
            self._save_cursor()
        except SlackApiError as e:
            logger.error(f"Failed to fetch latest message for cursor init: {e}")
            self.last_ts = "0"

    def _poll(self):
        """Fetch new messages since cursor, dedup, dispatch."""
        try:
            resp = self._slack_call(
                lambda: self.client.conversations_history(
                    channel=self.channel_id,
                    oldest=self.last_ts,
                    limit=50,
                )
            )
        except Exception as e:
            logger.error(f"conversations.history failed: {e}")
            return

        messages = resp.get("messages", [])
        if not messages:
            return

        # Process oldest-first
        messages.sort(key=lambda m: m["ts"])

        for msg_data in messages:
            ts = msg_data["ts"]

            # Skip if this is the cursor message itself
            if ts == self.last_ts:
                continue

            # Skip bot messages and self-messages
            if msg_data.get("bot_id") or msg_data.get("subtype") == "bot_message":
                continue
            if msg_data.get("user") == self.bot_user_id:
                continue

            # Check if this is a thread reply
            is_thread_reply = msg_data.get("thread_ts") and msg_data["thread_ts"] != ts
            text = msg_data.get("text", "")

            # Check if bot is @mentioned
            is_bot_mention = f"<@{self.bot_user_id}>" in text

            # Skip thread replies UNLESS they @mention the bot
            if is_thread_reply and not is_bot_mention:
                continue

            # Dedup: try to acquire
            if not self.try_acquire(ts):
                continue

            # Add eyes reaction — if already reacted or fails, skip
            if not self.add_reaction(self.channel_id, ts, "eyes"):
                self.processing.discard(ts)
                continue

            # Check for existing checkmark (already processed by another instance)
            if self._has_reaction(self.channel_id, ts, "white_check_mark"):
                self.processing.discard(ts)
                continue

            # Build SlackMessage and dispatch
            slack_msg = SlackMessage(
                text=text,
                user=msg_data.get("user", ""),
                timestamp=ts,
                thread_ts=msg_data.get("thread_ts", ts),
                channel=self.channel_id,
                is_bot_mention=is_bot_mention,
            )

            logger.info(f"Processing message {ts} from user {slack_msg.user}")
            try:
                self.on_message(slack_msg)
            except Exception as e:
                logger.error(f"on_message callback failed for {ts}: {e}")
                self.processing.discard(ts)

        # Update cursor to latest processed message
        latest_ts = messages[-1]["ts"]
        if latest_ts > self.last_ts:
            self.last_ts = latest_ts
            self._save_cursor()

    def _poll_thread_mentions(self):
        """Scan recent threads for @bot mentions that we haven't processed.

        conversations.history doesn't return thread replies, so we need
        this separate scan to catch @bot commands inside threads.
        """
        bot_mention_tag = f"<@{self.bot_user_id}>"

        try:
            # Fetch recent top-level messages (last 5) that have replies
            # Reduced from 10 to avoid Slack rate limits
            resp = self._slack_call(
                lambda: self.client.conversations_history(
                    channel=self.channel_id,
                    limit=5,
                )
            )
        except Exception as e:
            logger.error(f"Thread mention scan — conversations.history failed: {e}")
            return

        messages = resp.get("messages", [])
        for msg_data in messages:
            # Only check messages that have threads
            reply_count = msg_data.get("reply_count", 0)
            if reply_count == 0:
                continue

            thread_ts = msg_data["ts"]

            # Skip if we've already fully processed this thread recently
            if thread_ts in self.completed:
                continue

            # Small delay between thread fetches to respect rate limits
            time.sleep(1)

            try:
                thread_resp = self._slack_call(
                    lambda t=thread_ts: self.client.conversations_replies(
                        channel=self.channel_id,
                        ts=t,
                        limit=10,  # Reduced from 20 to cut API calls
                    )
                )
            except Exception as e:
                logger.error(f"Thread mention scan — replies fetch failed for {thread_ts}: {e}")
                continue

            replies = thread_resp.get("messages", [])
            for reply in replies:
                rts = reply.get("ts", "")

                # Skip parent message (it's the top-level message)
                if rts == thread_ts:
                    continue

                # Skip bot's own messages
                if reply.get("bot_id") or reply.get("user") == self.bot_user_id:
                    continue

                text = reply.get("text", "")

                # Only process if it @mentions the bot
                if bot_mention_tag not in text:
                    continue

                # Dedup
                if not self.try_acquire(rts):
                    continue

                # Add eyes — if already reacted, skip
                if not self.add_reaction(self.channel_id, rts, "eyes"):
                    self.processing.discard(rts)
                    continue

                if self._has_reaction(self.channel_id, rts, "white_check_mark"):
                    self.processing.discard(rts)
                    continue

                slack_msg = SlackMessage(
                    text=text,
                    user=reply.get("user", ""),
                    timestamp=rts,
                    thread_ts=thread_ts,
                    channel=self.channel_id,
                    is_bot_mention=True,
                )

                logger.info(f"Thread mention found: {rts} in thread {thread_ts}")
                try:
                    self.on_message(slack_msg)
                except Exception as e:
                    logger.error(f"on_message failed for thread mention {rts}: {e}")
                    self.processing.discard(rts)

    def try_acquire(self, ts: str) -> bool:
        """Atomic check-and-mark. Returns True if we claimed it."""
        if ts in self.processing or ts in self.completed:
            return False
        self.processing.add(ts)
        return True

    def mark_done(self, ts: str):
        """Move from processing to completed."""
        self.processing.discard(ts)
        self.completed[ts] = time.time()

    def post_message(self, channel: str, text: str, thread_ts: str = ""):
        """Post to Slack. Chunk if > 39000 chars."""
        if len(text) <= MAX_SLACK_MSG_LEN:
            self._post_single(channel, text, thread_ts)
        else:
            # Chunk the message
            for i in range(0, len(text), MAX_SLACK_MSG_LEN):
                chunk = text[i : i + MAX_SLACK_MSG_LEN]
                self._post_single(channel, chunk, thread_ts)

    def _post_single(self, channel: str, text: str, thread_ts: str):
        """Post a single message to Slack with retry."""
        try:
            self._slack_call(
                lambda: self.client.chat_postMessage(
                    channel=channel,
                    text=text,
                    thread_ts=thread_ts if thread_ts else None,
                )
            )
        except Exception as e:
            logger.error(f"Failed to post message: {e}")

    def ack_done(self, channel: str, ts: str):
        """Swap eyes -> white_check_mark."""
        try:
            self._slack_call(
                lambda: self.client.reactions_remove(
                    channel=channel, timestamp=ts, name="eyes"
                )
            )
        except Exception:
            pass  # eyes might already be removed

        self.add_reaction(channel, ts, "white_check_mark")

    def add_reaction(self, channel: str, ts: str, emoji: str) -> bool:
        """Add reaction. Returns False if already_reacted or error."""
        try:
            self._slack_call(
                lambda: self.client.reactions_add(
                    channel=channel, timestamp=ts, name=emoji
                )
            )
            return True
        except SlackApiError as e:
            if e.response.get("error") == "already_reacted":
                return False
            logger.error(f"Failed to add reaction {emoji} to {ts}: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to add reaction {emoji} to {ts}: {e}")
            return False

    def _has_reaction(self, channel: str, ts: str, emoji: str) -> bool:
        """Check if a specific reaction exists on a message."""
        try:
            resp = self._slack_call(
                lambda: self.client.reactions_get(
                    channel=channel, timestamp=ts
                )
            )
            reactions = resp.get("message", {}).get("reactions", [])
            return any(r["name"] == emoji for r in reactions)
        except Exception:
            return False

    def _save_cursor(self):
        """Persist cursor to file (atomic write)."""
        tmp_file = self.cursor_file + ".tmp"
        try:
            with open(tmp_file, "w") as f:
                f.write(self.last_ts)
            os.replace(tmp_file, self.cursor_file)
        except Exception as e:
            logger.error(f"Failed to save cursor: {e}")

    def _cleanup_completed(self):
        """Remove completed entries older than 1 hour."""
        cutoff = time.time() - 3600
        expired = [ts for ts, t in self.completed.items() if t < cutoff]
        for ts in expired:
            del self.completed[ts]

    def _slack_call(self, call_fn):
        """Execute a Slack API call with retry on 429 (3 attempts)."""
        for attempt, delay in enumerate(RETRY_DELAYS):
            try:
                resp = call_fn()
                # Check for non-exception rate limit responses
                if isinstance(resp, dict) and resp.get("error") == "ratelimited":
                    raise SlackApiError("Rate limited", resp)
                return resp
            except SlackApiError as e:
                error_str = str(e.response.get("error", "")) if hasattr(e, "response") else str(e)
                is_rate_limited = (
                    (hasattr(e.response, "status_code") and e.response.status_code == 429)
                    or "ratelimited" in error_str
                )
                if is_rate_limited:
                    retry_after = delay
                    if hasattr(e.response, "headers"):
                        retry_after = int(e.response.headers.get("Retry-After", delay))
                    logger.warning(
                        f"Slack rate limit (attempt {attempt + 1}/{len(RETRY_DELAYS)}), "
                        f"retrying in {retry_after}s"
                    )
                    time.sleep(retry_after)
                    continue
                raise
        # Final attempt without catch
        return call_fn()
