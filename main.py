"""Entry point — wires everything, starts poller."""

import atexit
import os
import signal
import sys
import logging

import anthropic
from slack_sdk import WebClient

from config import load_config
from classifier.classifier import CXClassifier
from assigner.assigner import Assigner
from slack_bot.poller import Poller
from metrics.db import MetricsDB
from handler import Handler
from cloudwatch.log_searcher import CloudWatchSearcher
from db_agent.db_searcher import DatabricksSearcher

PID_FILE = ".cxbot.pid"


def _ensure_single_instance():
    """Ensure only one bot instance runs at a time using a PID file.

    If another instance is running, kill it first, then write our PID.
    """
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r") as f:
                old_pid = int(f.read().strip())
            # Check if process is still alive
            os.kill(old_pid, 0)  # Doesn't kill, just checks
            # It's alive — kill it
            logging.warning(f"Killing previous bot instance (PID {old_pid})")
            os.kill(old_pid, signal.SIGKILL)
            import time
            time.sleep(1)
        except (ProcessLookupError, ValueError, PermissionError):
            pass  # Old process already dead or invalid PID

    # Write our PID
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    # Clean up PID file on exit
    def _remove_pid():
        try:
            os.remove(PID_FILE)
        except OSError:
            pass

    atexit.register(_remove_pid)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    _ensure_single_instance()

    cfg = load_config()

    # Anthropic client
    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)

    # Classifier
    classifier = CXClassifier(client, cfg.classifier_model)

    # Assigner
    assigner = Assigner(state_file="cxbot_assigner_state.json")

    # Metrics DB
    metrics_db = MetricsDB(cfg.db_path)

    # CloudWatch searcher (optional — only if AWS creds are present)
    cw_searcher = None
    if cfg.aws_access_key_id and cfg.aws_secret_access_key:
        try:
            cw_searcher = CloudWatchSearcher(
                aws_region=cfg.aws_region,
                aws_access_key_id=cfg.aws_access_key_id,
                aws_secret_access_key=cfg.aws_secret_access_key,
                aws_session_token=cfg.aws_session_token,
                goblin_log_group=cfg.cw_goblin_log_group,
            )
            # Validate AWS credentials on startup
            if cw_searcher.check_credentials():
                logging.info("CloudWatch searcher initialized — credentials valid ✓")
            else:
                logging.error(
                    "⚠ CloudWatch credentials EXPIRED. Update .env with fresh STS tokens. "
                    "Bot will start but CloudWatch investigation will fail."
                )
        except Exception as e:
            logging.warning(f"CloudWatch searcher failed to initialize: {e}")
            logging.warning("Bot will run without CloudWatch investigation capability")
    else:
        logging.info("No AWS credentials — CloudWatch investigation disabled")

    # Databricks searcher (optional — only if Databricks creds are present)
    db_searcher = None
    if cfg.databricks_server_hostname and cfg.databricks_access_token:
        try:
            db_searcher = DatabricksSearcher(
                server_hostname=cfg.databricks_server_hostname,
                http_path=cfg.databricks_http_path,
                access_token=cfg.databricks_access_token,
            )
            logging.info("Databricks searcher initialized")
        except Exception as e:
            logging.warning(f"Databricks searcher failed to initialize: {e}")
            logging.warning("Bot will run without Databricks investigation capability")
    else:
        logging.info("No Databricks credentials — DB investigation disabled")

    # Slack client
    slack_client = WebClient(token=cfg.slack_bot_token)

    # Get bot's own user ID (for filtering self-messages)
    auth_resp = slack_client.auth_test()
    bot_user_id = auth_resp["user_id"]
    logging.info(f"Bot user ID: {bot_user_id}")

    # Handler (poller set after creation)
    handler = Handler(
        classifier, assigner, metrics_db, poller=None,
        cw_searcher=cw_searcher,
        db_searcher=db_searcher,
        anthropic_client=client,
        classifier_model=cfg.classifier_model,
    )

    # Poller
    poller = Poller(
        client=slack_client,
        channel_id=cfg.slack_channel_id,
        interval=cfg.poll_interval,
        cursor_file=cfg.cursor_file,
        bot_user_id=bot_user_id,
        on_message=handler.handle,
    )
    handler.poller = poller

    # Graceful shutdown
    def shutdown(sig, frame):
        logging.info("Shutting down...")
        metrics_db.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Start
    logging.info(
        f"CX-Tech Bot starting | channel={cfg.slack_channel_id} "
        f"| interval={cfg.poll_interval}s | env={cfg.env} "
        f"| cloudwatch={'enabled' if cw_searcher else 'disabled'} "
        f"| databricks={'enabled' if db_searcher else 'disabled'}"
    )
    poller.run()


if __name__ == "__main__":
    main()
