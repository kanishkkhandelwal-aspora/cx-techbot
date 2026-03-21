"""SQLite database for logging classifications, assignments, and investigation metrics."""

import json
import logging
import sqlite3
from pathlib import Path

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
    classified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- v2 columns for dashboard
    response_time_ms INTEGER,          -- total processing time (classify + investigate + post)
    data_sources TEXT,                  -- JSON: ["CloudWatch", "Databricks"]
    error_found INTEGER DEFAULT 0,     -- 1 if investigation found an error
    root_cause_summary TEXT,           -- first 500 chars of root cause
    services_searched TEXT,            -- JSON: ["goblin-service", "goms-service"]
    is_triage INTEGER DEFAULT 0,       -- 1 if low-confidence / triaged
    cw_log_lines INTEGER DEFAULT 0,    -- total log lines from CloudWatch
    db_rows INTEGER DEFAULT 0          -- total rows from Databricks
);
"""

# Migrations for existing DBs — add new columns if missing
MIGRATIONS = [
    "ALTER TABLE cx_queries ADD COLUMN response_time_ms INTEGER",
    "ALTER TABLE cx_queries ADD COLUMN data_sources TEXT",
    "ALTER TABLE cx_queries ADD COLUMN error_found INTEGER DEFAULT 0",
    "ALTER TABLE cx_queries ADD COLUMN root_cause_summary TEXT",
    "ALTER TABLE cx_queries ADD COLUMN services_searched TEXT",
    "ALTER TABLE cx_queries ADD COLUMN is_triage INTEGER DEFAULT 0",
    "ALTER TABLE cx_queries ADD COLUMN cw_log_lines INTEGER DEFAULT 0",
    "ALTER TABLE cx_queries ADD COLUMN db_rows INTEGER DEFAULT 0",
]


class MetricsDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()
        self._run_migrations()
        logger.info(f"Metrics DB initialized at {db_path}")

    def _create_tables(self):
        self.conn.execute(CREATE_TABLE_SQL)
        self.conn.commit()

    def _run_migrations(self):
        """Add new columns to existing DBs — safe to re-run (ignores duplicates)."""
        for migration in MIGRATIONS:
            try:
                self.conn.execute(migration)
            except sqlite3.OperationalError:
                pass  # column already exists
        self.conn.commit()

    def record(self, msg, classification, assignment=None,
               assigned_to: str = "AUTO_RESOLVED",
               response_time_ms: int = 0,
               data_sources: list[str] = None,
               error_found: bool = False,
               root_cause_summary: str = "",
               services_searched: list[str] = None,
               is_triage: bool = False,
               cw_log_lines: int = 0,
               db_rows: int = 0):
        """Insert one row with full investigation metadata."""
        try:
            self.conn.execute(
                """INSERT OR IGNORE INTO cx_queries
                   (message_ts, channel_id, slack_user, category, confidence,
                    summary, assigned_to, extracted_order_ids, extracted_user_ids,
                    corridor, response_time_ms, data_sources, error_found,
                    root_cause_summary, services_searched, is_triage,
                    cw_log_lines, db_rows)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    msg.timestamp,
                    msg.channel,
                    msg.user,
                    classification.category,
                    classification.confidence,
                    classification.summary,
                    assignment.engineer if assignment else assigned_to,
                    json.dumps(classification.order_ids),
                    json.dumps(classification.user_ids),
                    classification.corridor,
                    response_time_ms,
                    json.dumps(data_sources or []),
                    1 if error_found else 0,
                    (root_cause_summary or "")[:500],
                    json.dumps(services_searched or []),
                    1 if is_triage else 0,
                    cw_log_lines,
                    db_rows,
                ),
            )
            self.conn.commit()
            logger.info(f"Recorded classification for message {msg.timestamp}")
        except Exception as e:
            logger.error(f"Failed to record to DB: {e}")

    # ─── Dashboard query methods ─────────────────────────────────────────

    def get_stats_summary(self) -> dict:
        """Get high-level stats for dashboard."""
        cur = self.conn.cursor()

        # Total queries
        cur.execute("SELECT COUNT(*) FROM cx_queries")
        total = cur.fetchone()[0]

        # Today's queries
        cur.execute("SELECT COUNT(*) FROM cx_queries WHERE DATE(classified_at) = DATE('now')")
        today = cur.fetchone()[0]

        # Avg response time (today)
        cur.execute(
            "SELECT AVG(response_time_ms) FROM cx_queries "
            "WHERE DATE(classified_at) = DATE('now') AND response_time_ms > 0"
        )
        avg_rt = cur.fetchone()[0] or 0

        # Error rate (today)
        cur.execute(
            "SELECT SUM(error_found), COUNT(*) FROM cx_queries "
            "WHERE DATE(classified_at) = DATE('now')"
        )
        row = cur.fetchone()
        error_count = row[0] or 0
        today_total = row[1] or 1
        error_rate = round(error_count / today_total * 100, 1)

        # Triage rate (today)
        cur.execute(
            "SELECT SUM(is_triage) FROM cx_queries "
            "WHERE DATE(classified_at) = DATE('now')"
        )
        triage_count = cur.fetchone()[0] or 0
        triage_rate = round(triage_count / today_total * 100, 1)

        return {
            "total_queries": total,
            "today_queries": today,
            "avg_response_time_ms": round(avg_rt),
            "today_error_rate": error_rate,
            "today_triage_rate": triage_rate,
        }

    def get_category_distribution(self, days: int = 7) -> list[dict]:
        """Category breakdown for the last N days."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT category, COUNT(*) as cnt FROM cx_queries "
            "WHERE classified_at >= datetime('now', ?) "
            "GROUP BY category ORDER BY cnt DESC",
            (f"-{days} days",),
        )
        return [{"category": row[0], "count": row[1]} for row in cur.fetchall()]

    def get_daily_volume(self, days: int = 30) -> list[dict]:
        """Queries per day for the last N days."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT DATE(classified_at) as day, COUNT(*) as cnt "
            "FROM cx_queries "
            "WHERE classified_at >= datetime('now', ?) "
            "GROUP BY day ORDER BY day",
            (f"-{days} days",),
        )
        return [{"date": row[0], "count": row[1]} for row in cur.fetchall()]

    def get_engineer_workload(self, days: int = 7) -> list[dict]:
        """Per-engineer assignment count."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT assigned_to, COUNT(*) as cnt FROM cx_queries "
            "WHERE classified_at >= datetime('now', ?) "
            "GROUP BY assigned_to ORDER BY cnt DESC",
            (f"-{days} days",),
        )
        return [{"engineer": row[0], "count": row[1]} for row in cur.fetchall()]

    def get_recent_queries(self, limit: int = 20) -> list[dict]:
        """Most recent queries for the live feed."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT message_ts, slack_user, category, confidence, summary, "
            "assigned_to, classified_at, response_time_ms, data_sources, "
            "error_found, root_cause_summary, is_triage "
            "FROM cx_queries ORDER BY classified_at DESC LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
        return [
            {
                "message_ts": r[0],
                "slack_user": r[1],
                "category": r[2],
                "confidence": r[3],
                "summary": r[4],
                "assigned_to": r[5],
                "classified_at": r[6],
                "response_time_ms": r[7],
                "data_sources": r[8],
                "error_found": r[9],
                "root_cause_summary": r[10],
                "is_triage": r[11],
            }
            for r in rows
        ]

    def get_hourly_volume(self, hours: int = 24) -> list[dict]:
        """Hourly query volume for the last N hours."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT strftime('%Y-%m-%d %H:00', classified_at) as hour, COUNT(*) as cnt "
            "FROM cx_queries "
            "WHERE classified_at >= datetime('now', ?) "
            "GROUP BY hour ORDER BY hour",
            (f"-{hours} hours",),
        )
        return [{"hour": row[0], "count": row[1]} for row in cur.fetchall()]

    def get_response_time_trend(self, days: int = 7) -> list[dict]:
        """Average response time per day."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT DATE(classified_at) as day, AVG(response_time_ms) as avg_ms, "
            "MIN(response_time_ms) as min_ms, MAX(response_time_ms) as max_ms "
            "FROM cx_queries "
            "WHERE classified_at >= datetime('now', ?) AND response_time_ms > 0 "
            "GROUP BY day ORDER BY day",
            (f"-{days} days",),
        )
        return [
            {"date": row[0], "avg_ms": round(row[1]), "min_ms": row[2], "max_ms": row[3]}
            for row in cur.fetchall()
        ]

    def close(self):
        self.conn.close()
        logger.info("Metrics DB closed")
