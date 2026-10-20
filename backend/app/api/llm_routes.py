"""LLM health test, Telegram test, manual classification trigger, live queue."""

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import (
    Category,
    Digest,
    DigestRun,
    DigestRunStatus,
    Email,
    EmailStatus,
)
from app.services import classifier, decider, gmail, llm, settings_service, telegram
from app.services.gmail import GmailAuthError, GmailClient, GmailNotFound
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


class ModelsRequest(BaseModel):
    """Unsaved draft values from the Settings form; empty → stored setting / env."""
    base_url: str = ""
    api_key: str = ""


@router.post("/llm/models")
async def llm_models(body: ModelsRequest | None = None,
                     session: Session = Depends(get_session)) -> dict:
    """All model ids served by the endpoint, for the Settings model picker."""
    settings = settings_service.get_all_settings(session, redact=False)
    body = body or ModelsRequest()
    if body.base_url.strip():
        settings["llm_base_url"] = body.base_url.strip()
    if body.api_key:
        settings["llm_api_key"] = body.api_key
    base_url, model = llm.resolve_llm_target(settings)
    try:
        models = await llm.list_models(base_url, llm.resolve_llm_api_key(settings))
    except Exception as e:  # noqa: BLE001 — any failure is reported, not raised
        return {"ok": False, "base_url": base_url, "error": str(e)[:300]}
    return {"ok": True, "base_url": base_url, "models": models,
            "configured_model": settings.get("llm_model") or ""}


class DeciderDraft(BaseModel):
    """Unsaved draft values from the Settings form; empty → stored setting."""
    base_url: str = ""
    model: str = ""


def _decider_settings(session: Session, body: DeciderDraft | None) -> dict:
    settings = settings_service.get_all_settings(session, redact=False)
    if body and body.base_url.strip():
        settings["decider_base_url"] = body.base_url.strip()
    if body and body.model.strip():
        settings["decider_model"] = body.model.strip()
    return settings


@router.post("/llm/decider/test")
async def decider_test(body: DeciderDraft | None = None,
                       session: Session = Depends(get_session)) -> dict:
    return await decider.health_probe(_decider_settings(session, body))


@router.post("/llm/decider/models")
async def decider_models(body: DeciderDraft | None = None,
                         session: Session = Depends(get_session)) -> dict:
    """Decision models (task == "decision") served by the decider endpoint."""
    settings = _decider_settings(session, body)
    base_url, model = decider.resolve_decider_target(settings)
    try:
        models = await decider.list_decision_models(settings)
    except Exception as e:  # noqa: BLE001 — any failure is reported, not raised
        return {"ok": False, "base_url": base_url, "error": str(e)[:300]}
    return {"ok": True, "base_url": base_url, "models": models, "configured_model": model}


async def _preview_backend(coro) -> dict:
    started = time.monotonic()
    try:
        result = {"ok": True, **(await coro)}
    except llm.LLMError as e:
        result = {"ok": False, "error": str(e)[:300]}
    return {**result, "latency_ms": round((time.monotonic() - started) * 1000)}


@router.post("/llm/decider/preview/{email_id}")
async def decider_preview(email_id: int, session: Session = Depends(get_session)) -> dict:
    """Classify one email with both backends side by side. Read-only: the
    email's stored classification is not touched."""
    email = session.get(Email, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="Email not found")
    settings = settings_service.get_all_settings(session, redact=False)
    categories = list(session.scalars(
        select(Category).where(Category.enabled.is_(True)).order_by(Category.id)))
    if not categories:
        raise HTTPException(status_code=400, detail="No enabled categories")
    body = email.body_text
    if body is None:
        client_secret = settings.get("gmail_client_secret_json")
        if not client_secret:
            raise HTTPException(status_code=409, detail="Gmail is not connected")
        try:
            client = GmailClient(session, client_secret)
        except GmailAuthError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        try:
            msg = await client.get_message_full(email.gmail_message_id)
        except GmailNotFound as e:
            raise HTTPException(status_code=404, detail="Message no longer in Gmail") from e
        finally:
            await client.aclose()
        body = gmail.extract_body_text(msg.get("payload", {}))

    async def run_llm() -> dict:
        result = await classifier.run_llm_classification(categories, email, body, settings)
        return {"model": llm.resolve_llm_target(settings)[1],
                "category": result["category"],
                "confidence": max(0.0, min(1.0, float(result["confidence"]))),
                "rationale": str(result["rationale"])}

    async def run_decider() -> dict:
        result = await decider.decide_category(categories, email, body, settings)
        return {"model": result.model, "category": result.choice,
                "confidence": result.confidence,
                "probabilities": dict(sorted(result.probabilities.items(),
                                             key=lambda kp: -kp[1]))}

    llm_result, decider_result = await asyncio.gather(
        _preview_backend(run_llm()), _preview_backend(run_decider()))
    return {"email_id": email.id, "active_backend": settings.get("classifier_backend"),
            "llm": llm_result, "decider": decider_result}


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
