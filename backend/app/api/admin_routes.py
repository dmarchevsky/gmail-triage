"""Danger-zone endpoints: purge processing data, factory reset."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_session
from app.services import admin

router = APIRouter(prefix="/admin")


@router.post("/purge-data")
def purge_data(session: Session = Depends(get_session)) -> dict:
    counts = admin.purge_processing_data(session)
    return {"ok": True, "deleted": counts}


@router.post("/factory-reset")
async def factory_reset(session: Session = Depends(get_session)) -> dict:
    counts = await admin.factory_reset(session)
    return {"ok": True, "deleted": counts}
