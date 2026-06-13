"""Digest CRUD, run-now, run history."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Category, Digest, DigestRun
from app.services import digest_scheduler
from app.services.audit import audit
from app.services.digests import run_digest

router = APIRouter(prefix="/digests")


class DigestIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    enabled: bool = True
    category_ids: list[int] = []
    cron_times: list[str] = []
    timezone: str = "UTC"
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    prompt_template: str | None = None
    telegram_chat_id: str | None = None
    include_links: bool = True
    include_metadata: bool = True
    max_emails: int = Field(default=50, ge=1, le=500)
    send_no_news: bool = False

    @field_validator("cron_times")
    @classmethod
    def validate_times(cls, v: list[str]) -> list[str]:
        for t in v:
            digest_scheduler.parse_hhmm(t)
        return v


def serialize(d: Digest) -> dict:
    return {
        "id": d.id, "name": d.name, "enabled": d.enabled,
        "category_ids": d.category_ids, "cron_times": d.cron_times,
        "timezone": d.timezone, "min_confidence": d.min_confidence,
        "prompt_template": d.prompt_template,
        "telegram_chat_id": d.telegram_chat_id,
        "include_links": d.include_links, "include_metadata": d.include_metadata,
        "max_emails": d.max_emails, "send_no_news": d.send_no_news,
    }


def serialize_run(r: DigestRun) -> dict:
    return {
        "id": r.id, "digest_id": r.digest_id,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "status": r.status, "email_ids": r.email_ids,
        "summary_text": r.summary_text,
        "telegram_message_id": r.telegram_message_id, "error": r.error,
    }


def _check_categories(session: Session, ids: list[int]) -> None:
    for cid in ids:
        if session.get(Category, cid) is None:
            raise HTTPException(status_code=400, detail=f"Unknown category id {cid}")


@router.get("")
def list_digests(session: Session = Depends(get_session)) -> list[dict]:
    return [serialize(d) for d in session.scalars(select(Digest).order_by(Digest.id))]


@router.post("", status_code=201)
def create_digest(body: DigestIn, session: Session = Depends(get_session)) -> dict:
    _check_categories(session, body.category_ids)
    digest = Digest(**body.model_dump())
    session.add(digest)
    session.flush()
    audit(session, "user", "digest_created", {"id": digest.id, "name": digest.name})
    session.commit()
    digest_scheduler.reschedule_all()
    return serialize(digest)


class BulkDigestIds(BaseModel):
    digest_ids: list[int]


class BulkDigestUpdate(BaseModel):
    digest_ids: list[int]
    enabled: bool


@router.delete("/bulk")
def bulk_delete_digests(body: BulkDigestIds,
                        session: Session = Depends(get_session)) -> dict:
    if not body.digest_ids:
        return {"deleted": 0}
    digests = list(session.scalars(select(Digest).where(Digest.id.in_(body.digest_ids))))
    for digest in digests:
        audit(session, "user", "digest_deleted", {"id": digest.id, "name": digest.name})
        session.delete(digest)
    session.commit()
    digest_scheduler.reschedule_all()
    return {"deleted": len(digests)}


@router.put("/bulk")
def bulk_update_digests(body: BulkDigestUpdate,
                        session: Session = Depends(get_session)) -> dict:
    if not body.digest_ids:
        return {"updated": 0}
    digests = list(session.scalars(select(Digest).where(Digest.id.in_(body.digest_ids))))
    for digest in digests:
        digest.enabled = body.enabled
    audit(session, "user", "digests_bulk_updated",
          {"ids": body.digest_ids, "enabled": body.enabled})
    session.commit()
    digest_scheduler.reschedule_all()
    return {"updated": len(digests)}


@router.put("/{digest_id}")
def update_digest(digest_id: int, body: DigestIn,
                  session: Session = Depends(get_session)) -> dict:
    digest = session.get(Digest, digest_id)
    if digest is None:
        raise HTTPException(status_code=404, detail="Digest not found")
    _check_categories(session, body.category_ids)
    for key, value in body.model_dump().items():
        setattr(digest, key, value)
    audit(session, "user", "digest_updated", {"id": digest.id})
    session.commit()
    digest_scheduler.reschedule_all()
    return serialize(digest)


@router.delete("/{digest_id}")
def delete_digest(digest_id: int, session: Session = Depends(get_session)) -> dict:
    digest = session.get(Digest, digest_id)
    if digest is None:
        raise HTTPException(status_code=404, detail="Digest not found")
    audit(session, "user", "digest_deleted", {"id": digest.id, "name": digest.name})
    session.delete(digest)
    session.commit()
    digest_scheduler.reschedule_all()
    return {"deleted": digest_id}


@router.post("/bulk-send")
async def bulk_send_digests(body: BulkDigestIds,
                            session: Session = Depends(get_session)) -> dict:
    """Send selected digests immediately (no preview)."""
    if not body.digest_ids:
        return {"sent": 0, "errors": []}
    digests = list(session.scalars(select(Digest).where(Digest.id.in_(body.digest_ids))))
    sent = 0
    errors = []
    for digest in digests:
        try:
            await run_digest(session, digest, actor="user", preview=False)
            sent += 1
        except Exception as e:
            errors.append({"digest_id": digest.id, "error": str(e)[:200]})
    return {"sent": sent, "errors": errors}


@router.post("/{digest_id}/run-now")
async def run_now(digest_id: int, body: dict | None = None,
                  session: Session = Depends(get_session)) -> dict:
    digest = session.get(Digest, digest_id)
    if digest is None:
        raise HTTPException(status_code=404, detail="Digest not found")
    preview = bool((body or {}).get("preview", False))
    run = await run_digest(session, digest, actor="user", preview=preview)
    return serialize_run(run)


@router.get("/{digest_id}/runs")
def run_history(digest_id: int, session: Session = Depends(get_session)) -> list[dict]:
    if session.get(Digest, digest_id) is None:
        raise HTTPException(status_code=404, detail="Digest not found")
    runs = session.scalars(select(DigestRun).where(DigestRun.digest_id == digest_id)
                           .order_by(DigestRun.started_at.desc()).limit(50))
    return [serialize_run(r) for r in runs]
