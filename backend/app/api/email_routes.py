"""Emails listing/detail + dashboard stats + audit log."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
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


@router.get("/emails/{email_id}")
def get_email(email_id: int, session: Session = Depends(get_session)) -> dict:
    email = session.get(Email, email_id, options=[joinedload(Email.classification),
                                                  joinedload(Email.actions)])
    if email is None:
        raise HTTPException(status_code=404, detail="Email not found")
    return serialize_email(email, detail=True)


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
