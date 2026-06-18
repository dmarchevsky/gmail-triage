"""In-process runtime status shared between background tasks and the API."""

from dataclasses import dataclass


@dataclass
class AppState:
    gmail_email: str | None = None
    gmail_status: str = "not_connected"  # not_connected|ok|auth_error|error
    llm_status: str = "unknown"          # unknown|ok|unreachable
    telegram_status: str = "unconfigured"  # unconfigured|ok|error
    poller_status: str = "stopped"       # stopped|running|paused|error
    poller_last_run_at: str | None = None
    poller_last_error: str | None = None
    # Gmail push (Pub/Sub pull consumer) state.
    pubsub_status: str = "stopped"       # stopped|running|error
    last_notification_at: str | None = None
    classifier_running: bool = False
    classifier_current_email_id: int | None = None
    # Auth state cached from the DB (settings table) to avoid a query per request.
    # Refreshed at startup and whenever an /auth endpoint mutates it.
    auth_disabled: bool = False
    ui_password_hash: str | None = None


app_state = AppState()
