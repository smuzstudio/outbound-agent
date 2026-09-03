"""Env loading + global settings."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


# Provider recorded for rehearsal sends. Rows carrying it are kept for review
# but MUST be excluded from dedupe and the daily cap — otherwise a dry run
# permanently burns the lead (and its whole domain) before you ever email it.
DRYRUN_PROVIDER = "dryrun"


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")

    sender_name: str = os.getenv("SENDER_NAME", "Smuz")
    sender_email: str = os.getenv("SENDER_EMAIL", "hello@smuz.io")
    sender_company: str = os.getenv("SENDER_COMPANY", "Smuz")
    sender_booking_url: str = os.getenv("SENDER_BOOKING_URL", "https://smuz.io")
    sender_reply_to: str = os.getenv("SENDER_REPLY_TO", "hello@smuz.io")

    resend_api_key: str = os.getenv("RESEND_API_KEY", "")
    smtp_host: str = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")

    hunter_api_key: str = os.getenv("HUNTER_API_KEY", "")
    producthunt_token: str = os.getenv("PRODUCTHUNT_TOKEN", "")
    apollo_api_key: str = os.getenv("APOLLO_API_KEY", "")

    daily_send_cap: int = int(os.getenv("DAILY_SEND_CAP", "25"))
    dry_run: bool = _bool("DRY_RUN", True)
    db_path: str = os.getenv("DB_PATH", "./outbound.db")


settings = Settings()
