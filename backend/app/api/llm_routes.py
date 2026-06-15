"""LLM health test, Telegram test, manual classification trigger, live queue."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import (
    Digest,
    DigestRun,
    DigestRunStatus,
    Email,
    EmailStatus,
)
from app.services import classifier, llm, settings_service, telegram
from app.services.gmail import GmailAuthError
from app.state import app_state

router = APIRouter()


@router.post("/telegram/test")
async def telegram_test(session: Session = Depends(get_session)) -> dict:
    token = settings_service.get_setting(session, "telegram_bot_token")
    chat_id = settings_service.get_setting(session, "telegram_default_chat_id")
    if not token or not chat_id:
        raise HTTPException(status_code=400,
                            detail="Set telegram_bot_token and telegram_default_chat_id first")
    result = await telegram.test_connection(token, str(chat_id))
    app_state.telegram_status = "ok" if result["ok"] else "error"
    return result


@router.post("/llm/test")
async def llm_test(session: Session = Depends(get_session)) -> dict:
    settings = settings_service.get_all_settings(session, redact=False)
    return await llm.health_probe(settings)


@router.get("/llm/context")
async def llm_context(session: Session = Depends(get_session)) -> dict:
    """Detected context window (from llama.cpp /props) + the configured value."""
    settings = settings_service.get_all_settings(session, redact=False)
    detected = await llm.fetch_context_length(settings)
    return {"detected": detected,
            "configured": settings.get("llm_max_context_tokens") or 0}


@router.get("/llm/queue")
def llm_queue(session: Session = Depends(get_session)) -> dict:
    """Live snapshot of work hitting the (serial) LLM: pending/in-flight email
    classifications and any digests currently being summarized."""
    pending = session.scalar(
        select(func.count()).select_from(Email)
        .where(Email.status == EmailStatus.pending.value)) or 0

    processing = [
        {"id": e.id, "sender": e.sender, "subject": e.subject}
        for e in session.scalars(
            select(Email)
            .where(Email.status == EmailStatus.processing.value)
            .order_by(Email.processing_started_at))
    ]

    digests = [
        {"run_id": r.id, "digest_id": r.digest_id, "name": name,
         "started_at": r.started_at.isoformat() if r.started_at else None}
        for r, name in session.execute(
            select(DigestRun, Digest.name)
            .join(Digest, Digest.id == DigestRun.digest_id)
            .where(DigestRun.status == DigestRunStatus.running.value)
            .order_by(DigestRun.started_at))
    ]

    return {"pending": pending, "processing": processing, "digests": digests}


@router.post("/classify/run-now")
async def classify_now(session: Session = Depends(get_session)) -> dict:
    try:
        return await classifier.classify_pending(session)
    except GmailAuthError as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail=str(e)) from e
