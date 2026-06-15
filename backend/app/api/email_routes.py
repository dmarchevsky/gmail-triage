"""Emails listing/detail + dashboard stats + audit log."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.db import get_session
from app.models import AuditLog, Category, Email, EmailAction, Feedback

router = APIRouter()


def serialize_action(a: EmailAction) -> dict:
    return {
        "id": a.id, "rule_id": a.rule_id, "action_type": a.action_type,
        "action_params": a.action_params, "executed": a.executed,
        "dry_run": a.dry_run,
        "executed_at": a.executed_at.isoformat() if a.executed_at else None,
        "error": a.error,
    }


def serialize_email(e: Email, detail: bool = False) -> dict:
    data = {
        "id": e.id,
        "gmail_message_id": e.gmail_message_id,
        "received_at": e.received_at.isoformat() if e.received_at else None,
        "sender": e.sender,
        "subject": e.subject,
        "snippet": e.snippet,
        "classification_id": e.classification_id,
        "classification": e.classification.name if e.classification else None,
        "confidence": e.confidence,
        "status": e.status,
        "dry_run": e.dry_run,
        "actions": [serialize_action(a) for a in e.actions],
    }
    if detail:
        data.update({
            "gmail_thread_id": e.gmail_thread_id,
            "rationale": e.rationale,
            "llm_model": e.llm_model,
            "classified_at": e.classified_at.isoformat() if e.classified_at else None,
            "error": e.error,
            "sender_domain": e.sender_domain,
        })
    return data


@router.get("/emails")
def list_emails(
    session: Session = Depends(get_session),
    category_id: int | None = None,
    status: str | None = None,
    confidence_min: float | None = Query(default=None, ge=0, le=1),
    confidence_max: float | None = Query(default=None, ge=0, le=1),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    q: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> dict:
    query = select(Email).options(joinedload(Email.classification),
                                  joinedload(Email.actions))
    if category_id is not None:
        if category_id == 0:  # 0 = unclassified/"none"
            query = query.where(Email.classification_id.is_(None))
        else:
            query = query.where(Email.classification_id == category_id)
    if status:
        query = query.where(Email.status == status)
    if confidence_min is not None:
        query = query.where(Email.confidence >= confidence_min)
    if confidence_max is not None:
        query = query.where(Email.confidence <= confidence_max)
    if date_from:
        query = query.where(Email.received_at >= date_from)
    if date_to:
        query = query.where(Email.received_at <= date_to)
    if q:
        like = f"%{q}%"
        query = query.where(Email.subject.ilike(like) | Email.sender.ilike(like))

    total = session.scalar(select(func.count()).select_from(query.subquery()))
    rows = session.scalars(
        query.order_by(Email.received_at.desc())
        .offset((page - 1) * page_size).limit(page_size)).unique()
    return {"total": total, "page": page, "page_size": page_size,
            "items": [serialize_email(e) for e in rows]}


@router.get("/emails/ids")
def list_email_ids(
    session: Session = Depends(get_session),
    category_id: int | None = None,
    status: str | None = None,
    confidence_min: float | None = Query(default=None, ge=0, le=1),
    confidence_max: float | None = Query(default=None, ge=0, le=1),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    q: str | None = None,
) -> dict:
    """All IDs matching the current filters (no pagination). Used for
    select-all-across-pages. Capped at 5 000."""
    query = select(Email.id)
    if category_id is not None:
        if category_id == 0:
            query = query.where(Email.classification_id.is_(None))
        else:
            query = query.where(Email.classification_id == category_id)
    if status:
        query = query.where(Email.status == status)
    if confidence_min is not None:
        query = query.where(Email.confidence >= confidence_min)
    if confidence_max is not None:
        query = query.where(Email.confidence <= confidence_max)
    if date_from:
        query = query.where(Email.received_at >= date_from)
    if date_to:
        query = query.where(Email.received_at <= date_to)
    if q:
        like = f"%{q}%"
        query = query.where(Email.subject.ilike(like) | Email.sender.ilike(like))
    ids = list(session.scalars(
        query.order_by(Email.received_at.desc()).limit(5000)))
    return {"ids": ids}


@router.get("/emails/{email_id}")
def get_email(email_id: int, session: Session = Depends(get_session)) -> dict:
    email = session.get(Email, email_id, options=[joinedload(Email.classification),
                                                  joinedload(Email.actions)])
    if email is None:
        raise HTTPException(status_code=404, detail="Email not found")
    return serialize_email(email, detail=True)


@router.post("/emails/{email_id}/reclassify")
async def reclassify_email(email_id: int,
                           session: Session = Depends(get_session)) -> dict:
    """Reset one email to pending and clear all actions. The queue_loop picks it
    up and classifies it; clients poll for status updates."""
    from sqlalchemy import delete

    from app.models import EmailStatus

    email = session.get(Email, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="Email not found")

    session.execute(delete(EmailAction).where(EmailAction.email_id == email_id))
    email.classification_id = None
    email.confidence = None
    email.rationale = None
    email.error = None
    email.classified_at = None
    email.processing_started_at = None
    email.status = EmailStatus.pending.value
    session.commit()

    session.expire(email)
    refreshed = session.get(Email, email_id,
                            options=[joinedload(Email.classification),
                                     joinedload(Email.actions)])
    return serialize_email(refreshed, detail=True)


class BulkEmailIds(BaseModel):
    email_ids: list[int]


@router.post("/emails/reclassify-bulk")
async def reclassify_bulk(body: BulkEmailIds,
                          session: Session = Depends(get_session)) -> dict:
    """Reset selected emails to pending (clear all actions). The queue_loop
    picks them up and classifies them; clients poll for status updates."""
    from sqlalchemy import delete

    from app.models import EmailStatus

    if not body.email_ids:
        return {"queued": 0}

    session.execute(delete(EmailAction).where(
        EmailAction.email_id.in_(body.email_ids)))
    emails = list(session.scalars(select(Email).where(Email.id.in_(body.email_ids))))
    for email in emails:
        email.classification_id = None
        email.confidence = None
        email.rationale = None
        email.error = None
        email.classified_at = None
        email.processing_started_at = None
        email.status = EmailStatus.pending.value
    session.commit()

    return {"queued": len(emails)}


@router.post("/emails/rerun-rules-bulk")
async def rerun_rules_bulk(body: BulkEmailIds,
                           session: Session = Depends(get_session)) -> dict:
    """Re-apply current rules to selected already-classified emails (no LLM)."""
    from sqlalchemy import delete

    from app.models import EmailStatus
    from app.services import rules as rules_engine
    from app.services import settings_service
    from app.services.gmail import GmailAuthError, GmailClient

    if not body.email_ids:
        return {"processed": 0, "actioned": 0, "errors": 0}

    settings = settings_service.get_all_settings(session, redact=False)
    client_secret = settings.get("gmail_client_secret_json")
    if not client_secret:
        raise HTTPException(status_code=409, detail="Gmail is not connected")

    eligible = list(session.scalars(
        select(Email).where(
            Email.id.in_(body.email_ids),
            Email.classification_id.is_not(None),
            Email.status.in_([EmailStatus.classified.value,
                              EmailStatus.actioned.value]))))
    if not eligible:
        return {"processed": 0, "actioned": 0, "errors": 0}

    session.execute(delete(EmailAction).where(
        EmailAction.email_id.in_([e.id for e in eligible])))
    for email in eligible:
        email.status = EmailStatus.classified.value
    session.commit()

    rules = rules_engine.load_enabled_rules(session)
    try:
        client = GmailClient(session, client_secret)
    except GmailAuthError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    counts = {"processed": 0, "actioned": 0, "errors": 0}
    try:
        for email in eligible:
            try:
                await rules_engine.apply_rules_to_email(session, client, email, rules)
                counts["processed"] += 1
                if email.status == EmailStatus.actioned.value:
                    counts["actioned"] += 1
            except Exception:
                counts["errors"] += 1
    finally:
        await client.aclose()
    return counts


@router.get("/stats")
def stats(session: Session = Depends(get_session)) -> dict:
    now = datetime.now(UTC)
    today = now - timedelta(days=1)
    week = now - timedelta(days=7)

    def window(since: datetime) -> dict:
        processed = session.scalar(select(func.count(Email.id)).where(
            Email.created_at >= since,
            Email.status.in_(["classified", "actioned", "skipped", "error"])))
        actioned = session.scalar(select(func.count(EmailAction.id)).where(
            EmailAction.executed.is_(True))
            .join(Email, Email.id == EmailAction.email_id)
            .where(Email.created_at >= since)) or 0
        planned = session.scalar(select(func.count(EmailAction.id))
                                 .join(Email, Email.id == EmailAction.email_id)
                                 .where(Email.created_at >= since,
                                        EmailAction.dry_run.is_(True))) or 0
        return {"processed": processed or 0, "actions_executed": actioned,
                "actions_planned_dry_run": planned}

    by_category = [
        {"category": name, "count": count}
        for name, count in session.execute(
            select(Category.name, func.count(Email.id))
            .join(Email, Email.classification_id == Category.id)
            .where(Email.created_at >= week)
            .group_by(Category.name).order_by(func.count(Email.id).desc()))
    ]
    unclassified = session.scalar(select(func.count(Email.id)).where(
        Email.created_at >= week, Email.classification_id.is_(None),
        Email.status.in_(["classified", "actioned"]))) or 0
    if unclassified:
        by_category.append({"category": "none", "count": unclassified})

    recent = [{
        "ts": r.ts.isoformat() if r.ts else None,
        "actor": r.actor, "event_type": r.event_type, "payload": r.payload,
    } for r in session.scalars(select(AuditLog).order_by(AuditLog.ts.desc()).limit(20))]

    # Per-category precision from feedback: an email classified as C and
    # flagged with a different correct category counts against C. LLM
    # confidence is uncalibrated — these empirical counts are what the user
    # should tune rule thresholds against (spec §4.2 note).
    precision = []
    for category in session.scalars(select(Category).order_by(Category.id)):
        classified_total = session.scalar(select(func.count(Email.id)).where(
            Email.classification_id == category.id,
            Email.status.in_(["classified", "actioned"]))) or 0
        flagged_wrong = session.scalar(
            select(func.count(Feedback.id))
            .join(Email, Email.id == Feedback.email_id)
            .where(Email.classification_id == category.id,
                   (Feedback.correct_category_id.is_(None))
                   | (Feedback.correct_category_id != category.id))) or 0
        precision.append({
            "category_id": category.id,
            "category": category.name,
            "classified_total": classified_total,
            "flagged_wrong": flagged_wrong,
            "precision": (round(1 - flagged_wrong / classified_total, 3)
                          if classified_total else None),
        })

    return {"today": window(today), "week": window(week),
            "by_category": by_category, "recent_activity": recent,
            "category_precision": precision}


@router.get("/audit-log")
def audit_log(
    session: Session = Depends(get_session),
    event_type: str | None = None,
    actor: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> dict:
    query = select(AuditLog)
    if event_type:
        query = query.where(AuditLog.event_type == event_type)
    if actor:
        query = query.where(AuditLog.actor == actor)
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    rows = session.scalars(query.order_by(AuditLog.ts.desc())
                           .offset((page - 1) * page_size).limit(page_size))
    return {"total": total, "page": page, "items": [{
        "id": r.id, "ts": r.ts.isoformat() if r.ts else None, "actor": r.actor,
        "event_type": r.event_type, "payload": r.payload} for r in rows]}
