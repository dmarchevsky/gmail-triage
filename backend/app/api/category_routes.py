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
    criteria_md: str = ""
    enabled: bool = True


def serialize(c: Category) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "description": c.description,
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
        criteria_md=body.criteria_md, enabled=body.enabled, criteria_version=1)
    session.add(category)
    session.flush()
    _record_history(session, category, CriteriaSource.user.value)
    audit(session, "user", "category_created", {"id": category.id, "name": category.name})
    session.commit()
    return serialize(category)


class BulkCategoryIds(BaseModel):
    category_ids: list[int]


class BulkCategoryUpdate(BaseModel):
    category_ids: list[int]
    enabled: bool


@router.delete("/bulk")
def bulk_delete_categories(body: BulkCategoryIds,
                           session: Session = Depends(get_session)) -> dict:
    if not body.category_ids:
        return {"deleted": 0}
    categories = list(session.scalars(
        select(Category).where(Category.id.in_(body.category_ids))))
    for category in categories:
        audit(session, "user", "category_deleted",
              {"id": category.id, "name": category.name})
        session.delete(category)
    session.commit()
    return {"deleted": len(categories)}


@router.put("/bulk")
def bulk_update_categories(body: BulkCategoryUpdate,
                           session: Session = Depends(get_session)) -> dict:
    if not body.category_ids:
        return {"updated": 0}
    categories = list(session.scalars(
        select(Category).where(Category.id.in_(body.category_ids))))
    for category in categories:
        category.enabled = body.enabled
    audit(session, "user", "categories_bulk_updated",
          {"ids": body.category_ids, "enabled": body.enabled})
    session.commit()
    return {"updated": len(categories)}


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
    category.enabled = body.enabled
    if criteria_changed:
        category.criteria_md = body.criteria_md
        category.criteria_version += 1
        _record_history(session, category, CriteriaSource.user.value)
    audit(session, "user", "category_updated",
          {"id": category.id, "criteria_changed": criteria_changed})
    session.commit()
    return serialize(category)


@router.delete("/{category_id}")
def delete_category(category_id: int, session: Session = Depends(get_session)) -> dict:
    category = session.get(Category, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="Category not found")
    audit(session, "user", "category_deleted", {"id": category.id, "name": category.name})
    session.delete(category)
    session.commit()
    return {"deleted": category_id}


class QuickLabelIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    text_color: str | None = None
    background_color: str | None = None
    min_confidence: float = Field(default=0.8, ge=0.0, le=1.0)


@router.post("/{category_id}/quick-label", status_code=201)
def quick_label(category_id: int, body: QuickLabelIn,
                session: Session = Depends(get_session)) -> dict:
    """Create a Label and a dry-run Rule that applies it to this category."""
    from app.models import Label, Rule
    from app.services import labels as labels_service

    category = session.get(Category, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="Category not found")
    if not labels_service.is_allowed_color(body.text_color, body.background_color):
        raise HTTPException(status_code=400, detail="Color not in the Gmail palette")
    if session.scalar(select(Label).where(Label.name == body.name)):
        raise HTTPException(status_code=409, detail="Label name already exists")

    label = Label(name=body.name, text_color=body.text_color,
                  background_color=body.background_color)
    session.add(label)
    session.flush()
    rule = Rule(name=f"Label {category.name} → {label.name}",
                match_category_id=category.id, match_min_confidence=body.min_confidence,
                actions=[{"type": "add_label", "label_id": label.id}], dry_run=True)
    session.add(rule)
    session.flush()
    audit(session, "user", "quick_label_created",
          {"category_id": category.id, "label_id": label.id, "rule_id": rule.id})
    session.commit()
    return {"label_id": label.id, "rule_id": rule.id, "label_name": label.name}


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
