"""Feedback capture, listing, and the criteria-revision proposal flow."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db import get_session
from app.models import Category, Email, Feedback, FeedbackStatus, ProposalStatus
from app.services import feedback_service
from app.services.audit import audit
from app.services.llm import LLMError

router = APIRouter()


class FeedbackIn(BaseModel):
    correct_category_id: int | None = None  # null = "none" is correct
    user_note: str | None = None


def serialize(f: Feedback, session: Session,
              merged_into: int | None = None, covers_count: int | None = None,
              source_merged_into: int | None = None,
              source_covers_count: int | None = None) -> dict:
    email = f.email
    original = email.classification.name if email and email.classification else None
    correct = session.get(Category, f.correct_category_id) \
        if f.correct_category_id else None
    source_category = session.get(Category, f.source_category_id) \
        if f.source_category_id else None
    return {
        "id": f.id,
        "email_id": f.email_id,
        "email_subject": email.subject if email else None,
        "email_sender": email.sender if email else None,
        "original_category": original,
        "correct_category_id": f.correct_category_id,
        "correct_category": correct.name if correct else None,
        "source_category_id": f.source_category_id,
        "source_category": source_category.name if source_category else None,
        "user_note": f.user_note,
        "status": f.status,
        "proposed_criteria_md": f.proposed_criteria_md,
        "proposal_explanation": f.proposal_explanation,
        "proposal_status": f.proposal_status,
        "proposal_feedback_ids": f.proposal_feedback_ids,
        "proposed_source_criteria_md": f.proposed_source_criteria_md,
        "proposal_source_explanation": f.proposal_source_explanation,
        "proposal_source_status": f.proposal_source_status,
        "proposal_source_feedback_ids": f.proposal_source_feedback_ids,
        # consolidated-proposal hints for the UI:
        "merged_into": merged_into,          # id of the representative covering this row
        "covers_count": covers_count,        # how many feedback items this proposal covers
        "source_merged_into": source_merged_into,    # same, for the source/exclusion proposal
        "source_covers_count": source_covers_count,
        "created_at": f.created_at.isoformat() if f.created_at else None,
    }


@router.post("/emails/{email_id}/feedback", status_code=201)
async def create_feedback(email_id: int, body: FeedbackIn,
                          session: Session = Depends(get_session)) -> dict:
    email = session.get(Email, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="Email not found")
    if body.correct_category_id is not None \
            and session.get(Category, body.correct_category_id) is None:
        raise HTTPException(status_code=404, detail="Category not found")
    feedback = Feedback(email_id=email_id,
                        correct_category_id=body.correct_category_id,
                        user_note=body.user_note)
    if body.correct_category_id is not None \
            and email.classification_id is not None \
            and email.classification_id != body.correct_category_id:
        feedback.source_category_id = email.classification_id
    session.add(feedback)
    session.flush()
    audit(session, "user", "feedback_created",
          {"feedback_id": feedback.id, "email_id": email_id,
           "correct_category_id": body.correct_category_id,
           "source_category_id": feedback.source_category_id})
    session.commit()
    category_id = feedback_service.target_category_id(feedback)
    if category_id is not None:
        feedback_service.schedule_proposal_generation(category_id, kind="target")
    source_category_id = feedback_service.source_category_id_for(feedback)
    if source_category_id is not None:
        feedback_service.schedule_proposal_generation(source_category_id, kind="source")
    return serialize(feedback, session)


@router.get("/feedback")
def list_feedback(status: str | None = None,
                  session: Session = Depends(get_session)) -> list[dict]:
    # Warm the Category identity map once so the per-row serialize() lookups
    # below resolve from memory instead of issuing a query each.
    session.scalars(select(Category)).all()
    query = select(Feedback).options(
        joinedload(Feedback.email).joinedload(Email.classification))
    if status:
        if status not in [s.value for s in FeedbackStatus]:
            raise HTTPException(status_code=400, detail="Invalid status")
        query = query.where(Feedback.status == status)
    rows = list(session.scalars(
        query.order_by(Feedback.created_at.desc()).limit(500)))

    # Map each covered feedback id -> the representative that covers it, so the
    # UI can show one consolidated proposal and mark the rest as merged.
    covered_by: dict[int, int] = {}
    covers_count: dict[int, int] = {}
    source_covered_by: dict[int, int] = {}
    source_covers_count: dict[int, int] = {}
    for f in rows:
        if f.proposal_status == ProposalStatus.pending_review.value \
                and f.proposal_feedback_ids:
            covers_count[f.id] = len(f.proposal_feedback_ids)
            for cid in f.proposal_feedback_ids:
                if cid != f.id:
                    covered_by[cid] = f.id
        if f.proposal_source_status == ProposalStatus.pending_review.value \
                and f.proposal_source_feedback_ids:
            source_covers_count[f.id] = len(f.proposal_source_feedback_ids)
            for cid in f.proposal_source_feedback_ids:
                if cid != f.id:
                    source_covered_by[cid] = f.id
    return [serialize(f, session, merged_into=covered_by.get(f.id),
                      covers_count=covers_count.get(f.id),
                      source_merged_into=source_covered_by.get(f.id),
                      source_covers_count=source_covers_count.get(f.id)) for f in rows]


def _get_feedback(session: Session, feedback_id: int) -> Feedback:
    feedback = session.get(Feedback, feedback_id,
                           options=[joinedload(Feedback.email)])
    if feedback is None:
        raise HTTPException(status_code=404, detail="Feedback not found")
    return feedback


@router.post("/feedback/{feedback_id}/generate-proposal")
async def generate_proposal_now(feedback_id: int,
                                session: Session = Depends(get_session)) -> dict:
    """Manual/immediate consolidated proposal generation for this feedback's
    target category (the background job is debounced)."""
    feedback = _get_feedback(session, feedback_id)
    category_id = feedback_service.target_category_id(feedback)
    if category_id is None:
        raise HTTPException(status_code=400, detail="Feedback has no target category")
    try:
        representative = await feedback_service.generate_proposal_for_category(
            session, category_id)
    except LLMError as e:
        raise HTTPException(status_code=502, detail=f"LLM error: {e}") from e
    session.expire_all()
    rep = representative or _get_feedback(session, feedback_id)
    covers = len(rep.proposal_feedback_ids) if rep.proposal_feedback_ids else None
    return serialize(rep, session, covers_count=covers)


@router.post("/feedback/{feedback_id}/generate-source-proposal")
async def generate_source_proposal_now(feedback_id: int,
                                       session: Session = Depends(get_session)) -> dict:
    """Manual/immediate consolidated EXCLUSION proposal generation for this
    feedback's source category (the category the email was wrongly
    classified into). Mirrors `generate_proposal_now`."""
    feedback = _get_feedback(session, feedback_id)
    if feedback.source_category_id is None:
        raise HTTPException(status_code=400, detail="Feedback has no source category")
    try:
        representative = await feedback_service.generate_exclusion_proposal_for_category(
            session, feedback.source_category_id)
    except LLMError as e:
        raise HTTPException(status_code=502, detail=f"LLM error: {e}") from e
    session.expire_all()
    rep = representative or _get_feedback(session, feedback_id)
    covers = len(rep.proposal_source_feedback_ids) if rep.proposal_source_feedback_ids else None
    return serialize(rep, session, source_covers_count=covers)


class ApproveBody(BaseModel):
    criteria_md: str | None = None  # edited-then-approved text
    kind: Literal["target", "source"] = "target"


@router.post("/feedback/{feedback_id}/approve")
def approve(feedback_id: int, body: ApproveBody | None = None,
            session: Session = Depends(get_session)) -> dict:
    feedback = _get_feedback(session, feedback_id)
    kind = body.kind if body else "target"
    status_attr = "proposal_source_status" if kind == "source" else "proposal_status"
    if getattr(feedback, status_attr) != ProposalStatus.pending_review.value \
            and not (body and body.criteria_md):
        raise HTTPException(status_code=409, detail="No proposal pending review")
    try:
        category = feedback_service.approve_proposal(
            session, feedback, body.criteria_md if body else None, kind=kind)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"feedback": serialize(feedback, session),
            "category_id": category.id,
            "criteria_version": category.criteria_version}


class RejectBody(BaseModel):
    kind: Literal["target", "source"] = "target"


@router.post("/feedback/{feedback_id}/reject")
def reject(feedback_id: int, body: RejectBody | None = None,
          session: Session = Depends(get_session)) -> dict:
    feedback = _get_feedback(session, feedback_id)
    kind = body.kind if body else "target"
    status_attr = "proposal_source_status" if kind == "source" else "proposal_status"
    if getattr(feedback, status_attr) != ProposalStatus.pending_review.value:
        raise HTTPException(status_code=409, detail="No proposal pending review")
    feedback_service.reject_proposal(session, feedback, kind=kind)
    return serialize(feedback, session)


@router.post("/feedback/{feedback_id}/dismiss")
def dismiss(feedback_id: int, session: Session = Depends(get_session)) -> dict:
    feedback = _get_feedback(session, feedback_id)
    feedback.status = FeedbackStatus.dismissed.value
    audit(session, "user", "feedback_dismissed", {"feedback_id": feedback_id})
    session.commit()
    return serialize(feedback, session)
