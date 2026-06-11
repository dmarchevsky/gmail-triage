"""GET /api/v1/status — component health. Public (docker healthcheck)."""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import GmailAuth
from app.services import settings_service
from app.state import app_state

router = APIRouter()


@router.get("/status")
def get_status(session: Session = Depends(get_session)) -> dict:
    gmail_connected = session.scalar(select(GmailAuth).limit(1)) is not None
    return {
        "ok": True,
        "version": "0.1.0",
        "gmail": {
            "connected": gmail_connected,
            "email": app_state.gmail_email,
            "status": app_state.gmail_status,
        },
        "llm": {"status": app_state.llm_status},
        "telegram": {"status": app_state.telegram_status},
        "poller": {
            "status": app_state.poller_status,
            "last_run_at": app_state.poller_last_run_at,
            "last_error": app_state.poller_last_error,
            "paused": bool(settings_service.get_setting(session, "poller_paused")),
        },
        "dry_run": bool(settings_service.get_setting(session, "dry_run")),
    }
