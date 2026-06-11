"""LLM health test + manual classification trigger."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_session
from app.services import classifier, llm, settings_service
from app.services.gmail import GmailAuthError

router = APIRouter()


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
