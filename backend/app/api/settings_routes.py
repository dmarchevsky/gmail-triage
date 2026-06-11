"""GET/PUT /api/v1/settings."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_session
from app.services import settings_service
from app.services.audit import audit

router = APIRouter()


@router.get("/settings")
def get_settings(session: Session = Depends(get_session)) -> dict:
    return settings_service.get_all_settings(session)


@router.put("/settings")
def put_settings(updates: dict[str, Any], session: Session = Depends(get_session)) -> dict:
    try:
        settings_service.update_settings(session, updates)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    audit(session, "user", "settings_updated",
          {"keys": [k for k in updates if k not in settings_service.SECRET_KEYS]})
    return settings_service.get_all_settings(session)


@router.put("/dry-run")
def put_dry_run(body: dict, session: Session = Depends(get_session)) -> dict:
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="body must be {enabled: bool}")
    settings_service.set_setting(session, "dry_run", enabled)
    audit(session, "user", "dry_run_toggled", {"enabled": enabled})
    return {"dry_run": enabled}
