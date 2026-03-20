"""CX-Tech Bot Dashboard — Real-time monitoring web app.

Run: python dashboard/app.py
Accessible at: http://localhost:5050
"""

import json
import os
import sys
from pathlib import Path

from flask import Flask, jsonify, render_template

# Add parent dir to path so we can import metrics
sys.path.insert(0, str(Path(__file__).parent.parent))
from metrics.db import MetricsDB

app = Flask(__name__)

# DB path — relative to project root
PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = os.getenv("CXBOT_DB_PATH", str(PROJECT_ROOT / "cxbot_metrics.db"))


def get_db() -> MetricsDB:
    """Get a MetricsDB instance (creates new connection each request for thread safety)."""
    return MetricsDB(DB_PATH)


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Main dashboard page."""
    return render_template("dashboard.html")


@app.route("/api/stats")
def api_stats():
    """High-level summary stats."""
    db = get_db()
    try:
        return jsonify(db.get_stats_summary())
    finally:
        db.close()


@app.route("/api/categories")
def api_categories():
    """Category distribution (last 7 days)."""
    db = get_db()
    try:
        return jsonify(db.get_category_distribution(days=7))
    finally:
        db.close()


@app.route("/api/daily-volume")
def api_daily_volume():
    """Daily query volume (last 30 days)."""
    db = get_db()
    try:
        return jsonify(db.get_daily_volume(days=30))
    finally:
        db.close()


@app.route("/api/hourly-volume")
def api_hourly_volume():
    """Hourly query volume (last 24 hours)."""
    db = get_db()
    try:
        return jsonify(db.get_hourly_volume(hours=24))
    finally:
        db.close()


@app.route("/api/engineers")
def api_engineers():
    """Engineer workload (last 7 days)."""
    db = get_db()
    try:
        return jsonify(db.get_engineer_workload(days=7))
    finally:
        db.close()


@app.route("/api/response-times")
def api_response_times():
    """Response time trend (last 7 days)."""
    db = get_db()
    try:
        return jsonify(db.get_response_time_trend(days=7))
    finally:
        db.close()


@app.route("/api/recent")
def api_recent():
    """Recent queries (last 20)."""
    db = get_db()
    try:
        return jsonify(db.get_recent_queries(limit=20))
    finally:
        db.close()


@app.route("/api/health")
def api_health():
    """Health check endpoint."""
    try:
        db = get_db()
        stats = db.get_stats_summary()
        db.close()
        return jsonify({"status": "ok", "total_queries": stats["total_queries"]})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


# ─── Run ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("DASHBOARD_PORT", "5050"))
    print(f"🚀 CX-Tech Bot Dashboard starting on http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=True)
