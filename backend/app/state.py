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


app_state = AppState()
