"""Pure round-robin assignment across 3 engineers with Slack user ID tagging."""

import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime

logger = logging.getLogger(__name__)

# Engineer name → Slack user ID mapping
ENGINEERS = {
    "Vatsal": "U0A0E1KCDM2",
    "Adarsh": "U0A0E1KSDQC",
    "Kanishk": "U0A0716P36Z",
}

ENGINEER_NAMES = list(ENGINEERS.keys())  # ["Vatsal", "Adarsh", "Kanishk"]


@dataclass
class Assignment:
    engineer: str
    slack_user_id: str
    category: str
    message_ts: str
    assigned_at: str

    @property
    def slack_tag(self) -> str:
        """Return Slack mention tag like <@U0A0E1KCDM2>."""
        return f"<@{self.slack_user_id}>"


class Assigner:
    def __init__(self, state_file: str = "cxbot_assigner_state.json"):
        self.state_file = state_file
        self.counts: dict[str, int] = {e: 0 for e in ENGINEER_NAMES}
        self.last_reset: str = ""
        self.rr_index: int = 0
        self._load_state()

    def assign(self, category: str, message_ts: str = "") -> Assignment:
        """Assign a message to the next engineer via pure round-robin."""
        self._maybe_reset()

        engineer = self._next_round_robin()
        self.counts[engineer] += 1
        self._save_state()

        assignment = Assignment(
            engineer=engineer,
            slack_user_id=ENGINEERS[engineer],
            category=category,
            message_ts=message_ts,
            assigned_at=datetime.utcnow().isoformat(),
        )
        logger.info(f"Assigned {category} to {engineer} (counts: {self.counts})")
        return assignment

    def _next_round_robin(self) -> str:
        """Return the next engineer in rotation."""
        engineer = ENGINEER_NAMES[self.rr_index % len(ENGINEER_NAMES)]
        self.rr_index = (self.rr_index + 1) % len(ENGINEER_NAMES)
        return engineer

    def _maybe_reset(self):
        """Reset counts at midnight (compare stored date vs current date)."""
        today = date.today().isoformat()
        if self.last_reset != today:
            logger.info(f"Resetting assignment counts (last reset: {self.last_reset}, today: {today})")
            self.counts = {e: 0 for e in ENGINEER_NAMES}
            self.rr_index = 0
            self.last_reset = today
            self._save_state()

    def _load_state(self):
        """Load persisted state from JSON file."""
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, "r") as f:
                data = json.load(f)
            self.counts = data.get("counts", {e: 0 for e in ENGINEER_NAMES})
            self.last_reset = data.get("last_reset", "")
            self.rr_index = data.get("rr_index", 0)
            # Ensure all engineers have a count entry
            for e in ENGINEER_NAMES:
                if e not in self.counts:
                    self.counts[e] = 0
            logger.info(f"Loaded assigner state: counts={self.counts}, last_reset={self.last_reset}")
        except Exception as e:
            logger.error(f"Failed to load assigner state: {e}")

    def _save_state(self):
        """Persist state to JSON file (atomic write)."""
        data = {
            "counts": self.counts,
            "last_reset": self.last_reset,
            "rr_index": self.rr_index,
        }
        tmp_file = self.state_file + ".tmp"
        try:
            with open(tmp_file, "w") as f:
                json.dump(data, f)
            os.replace(tmp_file, self.state_file)
        except Exception as e:
            logger.error(f"Failed to save assigner state: {e}")
