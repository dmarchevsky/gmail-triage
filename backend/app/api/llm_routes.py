"""LLM health test, Telegram test, manual classification trigger."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_session
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


@router.post("/classify/run-now")
async def classify_now(session: Session = Depends(get_session)) -> dict:
    try:
        return await classifier.classify_pending(session)
    except GmailAuthError as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail=str(e)) from e
