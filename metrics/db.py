"""SQLite database for logging classifications and assignments."""

import json
import logging
import sqlite3

logger = logging.getLogger(__name__)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS cx_queries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_ts TEXT UNIQUE NOT NULL,
    channel_id TEXT NOT NULL,
    slack_user TEXT,
    category TEXT NOT NULL,
    confidence REAL NOT NULL,
    summary TEXT,
    assigned_to TEXT NOT NULL,
    extracted_order_ids TEXT,
    extracted_user_ids TEXT,
    corridor TEXT,
    classified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class MetricsDB:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self._create_tables()
        logger.info(f"Metrics DB initialized at {db_path}")

    def _create_tables(self):
        self.conn.execute(CREATE_TABLE_SQL)
        self.conn.commit()

    def record(self, msg, classification, assignment):
        """Insert one row. Ignore duplicates (UNIQUE on message_ts)."""
        try:
            self.conn.execute(
                """INSERT OR IGNORE INTO cx_queries
                   (message_ts, channel_id, slack_user, category, confidence,
                    summary, assigned_to, extracted_order_ids, extracted_user_ids, corridor)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    msg.timestamp,
                    msg.channel,
                    msg.user,
                    classification.category,
                    classification.confidence,
                    classification.summary,
                    assignment.engineer,
                    json.dumps(classification.order_ids),
                    json.dumps(classification.user_ids),
                    classification.corridor,
                ),
            )
            self.conn.commit()
            logger.info(f"Recorded classification for message {msg.timestamp}")
        except Exception as e:
            logger.error(f"Failed to record to DB: {e}")

    def close(self):
        self.conn.close()
        logger.info("Metrics DB closed")
