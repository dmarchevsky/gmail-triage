"""Typed access to the key/value `settings` table.

Secrets (telegram bot token, gmail oauth client secret, ui password hash) are
Fernet-encrypted at rest and never returned by GET /settings.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_config
from app.models import Setting

SECRET_KEYS = {
    "telegram_bot_token",
    "gmail_client_secret_json",
    "ui_password_hash",
}

DEFAULTS: dict[str, Any] = {
    "poll_interval_seconds": 300,
    "initial_lookback_hours": 24,
    "store_bodies": False,
    "classify_body_max_chars": 2000,
    "digest_body_max_chars": 6000,
    "llm_base_url": "",  # empty -> use env LLM_BASE_URL
    "llm_model": "",
    "llm_classify_timeout_seconds": 120,
    "llm_digest_timeout_seconds": 300,
    "llm_max_concurrency": 1,
    "telegram_bot_token": "",
    "telegram_default_chat_id": "",
    "gmail_client_secret_json": "",
    "ignore_senders": [],  # list of glob/regex patterns skipped before LLM
    # Gmail label IDs that define the poll scope. Default = inbox + the four
    # category tabs (so Promotions/Updates that skip the inbox are triaged too).
    "poll_scope_labels": ["INBOX", "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL",
                          "CATEGORY_UPDATES", "CATEGORY_FORUMS"],
    "poller_paused": False,
    "first_run_complete": False,
}


def _encrypt(value: str) -> str:
    return get_config().fernet().encrypt(value.encode()).decode()


def _decrypt(value: str) -> str:
    return get_config().fernet().decrypt(value.encode()).decode()


def get_setting(session: Session, key: str) -> Any:
    row = session.get(Setting, key)
    if row is None:
        return DEFAULTS.get(key)
    value = row.value
    if key in SECRET_KEYS and isinstance(value, str) and value:
        return _decrypt(value)
    return value


def set_setting(session: Session, key: str, value: Any) -> None:
    if key in SECRET_KEYS and isinstance(value, str) and value:
        value = _encrypt(value)
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value


def get_all_settings(session: Session, redact: bool = True) -> dict[str, Any]:
    """All known settings with defaults applied; secrets redacted to a
    configured/not-configured marker for the UI."""
    stored = {row.key: row.value for row in session.scalars(select(Setting))}
    out: dict[str, Any] = {}
    for key, default in DEFAULTS.items():
        value = stored.get(key, default)
        if key in SECRET_KEYS:
            if redact:
                out[key + "_configured"] = bool(value)
            else:
                out[key] = _decrypt(value) if isinstance(value, str) and value else value
        else:
            out[key] = value
    return out


def update_settings(session: Session, updates: dict[str, Any]) -> None:
    for key, value in updates.items():
        if key not in DEFAULTS:
            raise KeyError(f"Unknown setting: {key}")
        set_setting(session, key, value)
