"""Feedback capture, listing, and the criteria-revision proposal flow."""

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


def serialize(f: Feedback, session: Session) -> dict:
    email = f.email
    original = email.classification.name if email and email.classification else None
    correct = session.get(Category, f.correct_category_id) \
        if f.correct_category_id else None
    return {
        "id": f.id,
        "email_id": f.email_id,
        "email_subject": email.subject if email else None,
        "email_sender": email.sender if email else None,
        "original_category": original,
        "correct_category_id": f.correct_category_id,
        "correct_category": correct.name if correct else None,
        "user_note": f.user_note,
        "status": f.status,
        "proposed_criteria_md": f.proposed_criteria_md,
        "proposal_explanation": f.proposal_explanation,
        "proposal_status": f.proposal_status,
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
    session.add(feedback)
    session.flush()
    audit(session, "user", "feedback_created",
          {"feedback_id": feedback.id, "email_id": email_id,
           "correct_category_id": body.correct_category_id})
    session.commit()
    category_id = feedback_service.target_category_id(feedback)
    if category_id is not None:
        feedback_service.schedule_proposal_generation(category_id)
    return serialize(feedback, session)


@router.get("/feedback")
def list_feedback(status: str | None = None,
                  session: Session = Depends(get_session)) -> list[dict]:
    query = select(Feedback).options(joinedload(Feedback.email))
    if status:
        if status not in [s.value for s in FeedbackStatus]:
            raise HTTPException(status_code=400, detail="Invalid status")
        query = query.where(Feedback.status == status)
    rows = session.scalars(query.order_by(Feedback.created_at.desc()))
    return [serialize(f, session) for f in rows]


def _get_feedback(session: Session, feedback_id: int) -> Feedback:
    feedback = session.get(Feedback, feedback_id,
                           options=[joinedload(Feedback.email)])
    if feedback is None:
        raise HTTPException(status_code=404, detail="Feedback not found")
    return feedback


@router.post("/feedback/{feedback_id}/generate-proposal")
async def generate_proposal_now(feedback_id: int,
                                session: Session = Depends(get_session)) -> dict:
    """Manual/immediate proposal generation (the background job is debounced)."""
    feedback = _get_feedback(session, feedback_id)
    try:
        await feedback_service.generate_proposal(session, feedback)
    except LLMError as e:
        raise HTTPException(status_code=502, detail=f"LLM error: {e}") from e
    return serialize(feedback, session)


class ApproveBody(BaseModel):
    criteria_md: str | None = None  # edited-then-approved text


@router.post("/feedback/{feedback_id}/approve")
def approve(feedback_id: int, body: ApproveBody | None = None,
            session: Session = Depends(get_session)) -> dict:
    feedback = _get_feedback(session, feedback_id)
    if feedback.proposal_status != ProposalStatus.pending_review.value \
            and not (body and body.criteria_md):
        raise HTTPException(status_code=409, detail="No proposal pending review")
    try:
        category = feedback_service.approve_proposal(
            session, feedback, body.criteria_md if body else None)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"feedback": serialize(feedback, session),
            "category_id": category.id,
            "criteria_version": category.criteria_version}


@router.post("/feedback/{feedback_id}/reject")
def reject(feedback_id: int, session: Session = Depends(get_session)) -> dict:
    feedback = _get_feedback(session, feedback_id)
    if feedback.proposal_status != ProposalStatus.pending_review.value:
        raise HTTPException(status_code=409, detail="No proposal pending review")
    feedback_service.reject_proposal(session, feedback)
    return serialize(feedback, session)


@router.post("/feedback/{feedback_id}/dismiss")
def dismiss(feedback_id: int, session: Session = Depends(get_session)) -> dict:
    feedback = _get_feedback(session, feedback_id)
    feedback.status = FeedbackStatus.dismissed.value
    audit(session, "user", "feedback_dismissed", {"feedback_id": feedback_id})
    session.commit()
    return serialize(feedback, session)
