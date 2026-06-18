"""GET /api/v1/status — component health. Public (docker healthcheck)."""

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Email, EmailStatus, GmailAuth, Rule
from app.services import settings_service
from app.state import app_state

router = APIRouter()


def _telegram_status(session: Session) -> str:
    """Derived from stored config (app_state alone resets on restart)."""
    configured = bool(settings_service.get_setting(session, "telegram_bot_token")) \
        and bool(settings_service.get_setting(session, "telegram_default_chat_id"))
    if not configured:
        return "unconfigured"
    return app_state.telegram_status if app_state.telegram_status != "unconfigured" \
        else "configured"


@router.get("/status")
def get_status(session: Session = Depends(get_session)) -> dict:
    gmail_row = session.scalar(select(GmailAuth).limit(1))
    gmail_connected = gmail_row is not None
    return {
        "ok": True,
        "version": "0.1.0",
        "gmail": {
            "connected": gmail_connected,
            "email": app_state.gmail_email,
            "status": app_state.gmail_status,
        },
        "ingest": {
            "mode": settings_service.get_setting(session, "gmail_ingest_mode"),
            "pubsub_status": app_state.pubsub_status,
            "last_notification_at": app_state.last_notification_at,
            "watch_expiration": gmail_row.watch_expiration if gmail_row else None,
        },
        "llm": {"status": app_state.llm_status},
        "telegram": {"status": _telegram_status(session)},
        "poller": {
            "status": app_state.poller_status,
            "last_run_at": app_state.poller_last_run_at,
            "last_error": app_state.poller_last_error,
            "paused": bool(settings_service.get_setting(session, "poller_paused")),
        },
        "rules_mode": {
            "live": session.scalar(select(func.count(Rule.id)).where(
                Rule.enabled.is_(True), Rule.dry_run.is_(False),
                Rule.is_default.is_(False))) or 0,
            "dry": session.scalar(select(func.count(Rule.id)).where(
                Rule.enabled.is_(True), Rule.dry_run.is_(True),
                Rule.is_default.is_(False))) or 0,
        },
        "classifier": {
            "running": app_state.classifier_running,
            "pending_emails": session.scalar(select(func.count(Email.id)).where(
                Email.status.in_([EmailStatus.pending.value,
                                  EmailStatus.processing.value]))) or 0,
        },
    }
