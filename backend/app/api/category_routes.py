"""Categories CRUD + criteria history (spec §4.4 p.3, §5)."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Category, CategoryCriteriaHistory, CriteriaSource
from app.services.audit import audit

router = APIRouter(prefix="/categories")


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    gmail_label_name: str | None = None
    criteria_md: str = ""
    enabled: bool = True


def serialize(c: Category) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "description": c.description,
        "gmail_label_name": c.gmail_label_name or f"MailTriage/{c.name}",
        "criteria_md": c.criteria_md,
        "criteria_version": c.criteria_version,
        "enabled": c.enabled,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def _record_history(session: Session, category: Category, source: str,
                    feedback_ids: list[int] | None = None) -> None:
    session.add(CategoryCriteriaHistory(
        category_id=category.id, version=category.criteria_version,
        criteria_md=category.criteria_md, source=source,
        feedback_ids=feedback_ids or []))


@router.get("")
def list_categories(session: Session = Depends(get_session)) -> list[dict]:
    return [serialize(c) for c in session.scalars(select(Category).order_by(Category.id))]


@router.post("", status_code=201)
def create_category(body: CategoryIn, session: Session = Depends(get_session)) -> dict:
    if session.scalar(select(Category).where(Category.name == body.name)):
        raise HTTPException(status_code=409, detail="Category name already exists")
    category = Category(
        name=body.name, description=body.description,
        gmail_label_name=body.gmail_label_name or f"MailTriage/{body.name}",
        criteria_md=body.criteria_md, enabled=body.enabled, criteria_version=1)
    session.add(category)
    session.flush()
    _record_history(session, category, CriteriaSource.user.value)
    audit(session, "user", "category_created", {"id": category.id, "name": category.name})
    return serialize(category)


@router.put("/{category_id}")
def update_category(category_id: int, body: CategoryIn,
                    session: Session = Depends(get_session)) -> dict:
    category = session.get(Category, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="Category not found")
    clash = session.scalar(select(Category).where(Category.name == body.name,
                                                  Category.id != category_id))
    if clash:
        raise HTTPException(status_code=409, detail="Category name already exists")
    criteria_changed = body.criteria_md != category.criteria_md
    category.name = body.name
    category.description = body.description
    if body.gmail_label_name:
        category.gmail_label_name = body.gmail_label_name
    category.enabled = body.enabled
    if criteria_changed:
        category.criteria_md = body.criteria_md
        category.criteria_version += 1
        _record_history(session, category, CriteriaSource.user.value)
    audit(session, "user", "category_updated",
          {"id": category.id, "criteria_changed": criteria_changed})
    return serialize(category)


@router.delete("/{category_id}")
def delete_category(category_id: int, session: Session = Depends(get_session)) -> dict:
    category = session.get(Category, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="Category not found")
    audit(session, "user", "category_deleted", {"id": category.id, "name": category.name})
    session.delete(category)
    return {"deleted": category_id}


@router.post("/{category_id}/reclassify-preview")
async def reclassify_preview(category_id: int, body: dict | None = None,
                             session: Session = Depends(get_session)) -> dict:
    """Stretch API hook (§4.7.6): re-classify this category's recent emails
    against the CURRENT criteria without persisting; returns the diff."""
    from datetime import UTC, datetime, timedelta

    from app.models import Email, EmailStatus
    from app.services import classifier, llm, settings_service
    from app.services.gmail import GmailAuthError, GmailClient

    category = session.get(Category, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="Category not found")
    days = int((body or {}).get("days", 7))
    limit = min(int((body or {}).get("limit", 25)), 50)

    settings = settings_service.get_all_settings(session, redact=False)
    categories = list(session.scalars(
        select(Category).where(Category.enabled.is_(True)).order_by(Category.id)))
    emails = list(session.scalars(
        select(Email).where(
            Email.classification_id == category_id,
            Email.received_at >= datetime.now(UTC) - timedelta(days=days),
            Email.status.in_([EmailStatus.classified.value,
                              EmailStatus.actioned.value]))
        .order_by(Email.received_at.desc()).limit(limit)))

    client_secret = settings.get("gmail_client_secret_json")
    if not client_secret:
        raise HTTPException(status_code=409, detail="Gmail is not connected")
    client = GmailClient(session, client_secret)
    diffs = []
    try:
        for email in emails:
            body_text = await classifier.fetch_body(session, client, email)
            system, user, schema = classifier.build_classification_prompt(
                categories, email, body_text,
                int(settings["classify_body_max_chars"]))
            try:
                result = await llm.chat_json(
                    system, user, schema, "email_classification",
                    timeout=float(settings["llm_classify_timeout_seconds"]),
                    settings=settings,
                    max_concurrency=int(settings["llm_max_concurrency"]))
            except llm.LLMError as e:
                diffs.append({"email_id": email.id, "error": str(e)[:200]})
                continue
            new_name = result["category"]
            if new_name != category.name:
                diffs.append({
                    "email_id": email.id, "subject": email.subject,
                    "old_category": category.name, "new_category": new_name,
                    "new_confidence": result["confidence"],
                    "rationale": result["rationale"],
                })
    except GmailAuthError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    finally:
        await client.aclose()
    return {"tested": len(emails), "changed": len(diffs), "diffs": diffs,
            "note": "Preview only — nothing was persisted or executed."}


@router.get("/{category_id}/criteria-history")
def criteria_history(category_id: int, session: Session = Depends(get_session)) -> list[dict]:
    if session.get(Category, category_id) is None:
        raise HTTPException(status_code=404, detail="Category not found")
    rows = session.scalars(
        select(CategoryCriteriaHistory)
        .where(CategoryCriteriaHistory.category_id == category_id)
        .order_by(CategoryCriteriaHistory.version.desc()))
    return [{
        "version": r.version,
        "criteria_md": r.criteria_md,
        "source": r.source,
        "feedback_ids": r.feedback_ids,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]
