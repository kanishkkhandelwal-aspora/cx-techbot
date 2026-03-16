"""Load config from .env and return a typed dataclass."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass
class Config:
    anthropic_api_key: str
    slack_bot_token: str
    slack_channel_id: str
    env: str = "stage"
    poll_interval: int = 15
    cursor_file: str = ".cxbot_cursor"
    db_path: str = "cxbot_metrics.db"
    classifier_model: str = "claude-haiku-4-5-20251001"
    # AWS CloudWatch
    aws_region: str = "eu-west-2"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_session_token: str = ""
    cw_goblin_log_group: str = "/ecs/vance-core/prod/london/01/goblin-service-logs"


def load_config() -> Config:
    """Load configuration from .env file and environment variables."""
    load_dotenv(override=True)

    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "")
    slack_bot_token = os.getenv("SLACK_BOT_TOKEN", "")
    slack_channel_id = os.getenv("SLACK_CHANNEL_ID", "")

    missing = []
    if not anthropic_api_key:
        missing.append("ANTHROPIC_API_KEY")
    if not slack_bot_token:
        missing.append("SLACK_BOT_TOKEN")
    if not slack_channel_id:
        missing.append("SLACK_CHANNEL_ID")

    if missing:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing)}. "
            "Copy .env.example to .env and fill in the values."
        )

    return Config(
        anthropic_api_key=anthropic_api_key,
        slack_bot_token=slack_bot_token,
        slack_channel_id=slack_channel_id,
        env=os.getenv("CXBOT_ENV", "stage"),
        poll_interval=int(os.getenv("CXBOT_POLL_INTERVAL", "15")),
        cursor_file=os.getenv("CXBOT_CURSOR_FILE", ".cxbot_cursor"),
        db_path=os.getenv("CXBOT_DB_PATH", "cxbot_metrics.db"),
        classifier_model=os.getenv("CLASSIFIER_MODEL", "claude-haiku-4-5-20251001"),
        # AWS
        aws_region=os.getenv("AWS_REGION", "eu-west-2"),
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", ""),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", ""),
        aws_session_token=os.getenv("AWS_SESSION_TOKEN", ""),
        cw_goblin_log_group=os.getenv("CW_GOBLIN_LOG_GROUP", "/ecs/vance-core/prod/london/01/goblin-service-logs"),
    )
