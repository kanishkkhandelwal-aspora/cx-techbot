"""Entry point — wires everything, starts poller."""

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


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

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
            logging.info("CloudWatch searcher initialized")
        except Exception as e:
            logging.warning(f"CloudWatch searcher failed to initialize: {e}")
            logging.warning("Bot will run without CloudWatch investigation capability")
    else:
        logging.info("No AWS credentials — CloudWatch investigation disabled")

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
        f"| cloudwatch={'enabled' if cw_searcher else 'disabled'}"
    )
    poller.run()


if __name__ == "__main__":
    main()
