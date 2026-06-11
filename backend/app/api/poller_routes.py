"""Poller control endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_session
from app.services import poller, settings_service
from app.services.audit import audit
from app.services.gmail import GmailAuthError

router = APIRouter(prefix="/poller")


@router.post("/run-now")
async def run_now(session: Session = Depends(get_session)) -> dict:
    try:
        result = await poller.poll_once(session)
    except GmailAuthError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    audit(session, "user", "poll_run_now", result)
    return result


@router.put("/pause")
def pause(body: dict, session: Session = Depends(get_session)) -> dict:
    paused = body.get("paused")
    if not isinstance(paused, bool):
        raise HTTPException(status_code=400, detail="body must be {paused: bool}")
    settings_service.set_setting(session, "poller_paused", paused)
    audit(session, "user", "poller_paused" if paused else "poller_resumed", {})
    if not paused:
        poller.wake()
    return {"paused": paused}
