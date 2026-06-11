"""Feedback capture + listing. The criteria-revision proposal flow is M6."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db import get_session
from app.models import Category, Email, Feedback, FeedbackStatus
from app.services.audit import audit

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
    await _maybe_schedule_proposal(session, feedback)
    return serialize(feedback, session)


async def _maybe_schedule_proposal(session: Session, feedback: Feedback) -> None:
    """M6 hooks the debounced criteria-revision job in here."""


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
