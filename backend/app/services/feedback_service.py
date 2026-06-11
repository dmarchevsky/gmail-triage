"""Feedback → criteria self-revision loop (spec §4.7).

A debounced background job builds a revision prompt per affected category and
stores the LLM's proposed criteria on the feedback row. Nothing changes
automatically — the user approves/edits/rejects in the Feedback queue.
"""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import (
    Category,
    CategoryCriteriaHistory,
    CriteriaSource,
    Feedback,
    FeedbackStatus,
    ProposalStatus,
)
from app.services import gmail, llm, settings_service
from app.services.audit import audit
from app.services.classifier import fetch_body
from app.services.gmail import GmailClient

log = get_logger(__name__)

DEBOUNCE_SECONDS = 60.0
MAX_PRIOR_FEEDBACK = 5

PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "criteria_md": {"type": "string"},
        "explanation": {"type": "string"},
    },
    "required": ["criteria_md", "explanation"],
    "additionalProperties": False,
}

_pending_jobs: dict[int, asyncio.Task] = {}


def target_category_id(feedback: Feedback) -> int | None:
    """Revise the category the email should be in; if the correction is
    'none', tighten the category it was wrongly assigned to."""
    if feedback.correct_category_id is not None:
        return feedback.correct_category_id
    if feedback.email is not None:
        return feedback.email.classification_id
    return None


def schedule_proposal_generation(category_id: int,
                                 debounce: float | None = None) -> None:
    """Debounced per-category proposal job (in-process)."""
    delay = DEBOUNCE_SECONDS if debounce is None else debounce
    existing = _pending_jobs.get(category_id)
    if existing is not None and not existing.done():
        existing.cancel()
    _pending_jobs[category_id] = asyncio.create_task(
        _delayed_generation(category_id, delay))


async def _delayed_generation(category_id: int, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    from app.db import get_sessionmaker

    session = get_sessionmaker()()
    try:
        feedbacks = session.scalars(
            select(Feedback).where(
                Feedback.status == FeedbackStatus.open.value,
                Feedback.proposal_status == ProposalStatus.none.value))
        for feedback in feedbacks:
            if target_category_id(feedback) == category_id:
                try:
                    await generate_proposal(session, feedback)
                except llm.LLMError as e:
                    log.warning("proposal_generation_failed",
                                feedback_id=feedback.id, error=str(e))
    except Exception as e:  # noqa: BLE001 — background job must not crash loop
        log.error("proposal_job_failed", category_id=category_id, error=str(e))
    finally:
        session.close()


async def generate_proposal(session: Session, feedback: Feedback) -> Feedback:
    """Build the revision prompt and store the proposal on the feedback row."""
    category_id = target_category_id(feedback)
    if category_id is None:
        return feedback
    category = session.get(Category, category_id)
    if category is None:
        return feedback
    email = feedback.email
    settings = settings_service.get_all_settings(session, redact=False)

    body = ""
    client_secret = settings.get("gmail_client_secret_json")
    if email is not None and client_secret and gmail.load_token(session) is not None:
        client = GmailClient(session, client_secret)
        try:
            body = await fetch_body(session, client, email)
        except gmail.GmailError:
            body = ""
        finally:
            await client.aclose()
    body = (body or (email.snippet if email else "") or "")[
        : int(settings["classify_body_max_chars"])]

    prior = session.scalars(
        select(Feedback)
        .where(Feedback.id != feedback.id,
               Feedback.correct_category_id == category_id)
        .order_by(Feedback.created_at.desc()).limit(MAX_PRIOR_FEEDBACK)).all()
    prior_block = "\n".join(
        f"- email {p.email.subject!r} from {p.email.sender!r}: "
        f"note={p.user_note or '(none)'}"
        for p in prior if p.email is not None) or "(none)"

    original = (email.classification.name
                if email is not None and email.classification else "none")
    corrected = (session.get(Category, feedback.correct_category_id).name
                 if feedback.correct_category_id else "none")

    system = llm.load_prompt("criteria_revision_system.txt").format(
        category=category.name)
    user = (
        f"Current criteria for {category.name!r} (version "
        f"{category.criteria_version}):\n{category.criteria_md or '(empty)'}\n\n"
        f"Misclassified email:\nFrom: {email.sender if email else '?'}\n"
        f"Subject: {email.subject if email else '?'}\n"
        f"Date: {email.received_at if email else '?'}\n"
        f"Body (truncated):\n{body}\n\n"
        f"The model originally classified it as: {original}\n"
        f"Model's original rationale: {email.rationale if email else '(none)'}\n"
        f"The user says the correct category is: {corrected}\n"
        f"User note: {feedback.user_note or '(none)'}\n\n"
        f"Recent prior feedback for this category:\n{prior_block}\n\n"
        "Produce the revised criteria now."
    )

    result = await llm.chat_json(
        system, user, PROPOSAL_SCHEMA, "criteria_revision",
        timeout=float(settings["llm_classify_timeout_seconds"]),
        settings=settings,
        max_concurrency=int(settings["llm_max_concurrency"]))

    feedback.proposed_criteria_md = str(result["criteria_md"])
    feedback.proposal_explanation = str(result["explanation"])[:2000]
    feedback.proposal_status = ProposalStatus.pending_review.value
    audit(session, "system", "criteria_proposal_generated",
          {"feedback_id": feedback.id, "category_id": category.id})
    session.commit()
    return feedback


def approve_proposal(session: Session, feedback: Feedback,
                     edited_criteria_md: str | None = None) -> Category:
    category_id = target_category_id(feedback)
    category = session.get(Category, category_id) if category_id else None
    if category is None:
        raise ValueError("Feedback has no target category")
    new_criteria = edited_criteria_md if edited_criteria_md is not None \
        else feedback.proposed_criteria_md
    if not new_criteria:
        raise ValueError("No proposed criteria to approve")

    category.criteria_md = new_criteria
    category.criteria_version += 1
    session.add(CategoryCriteriaHistory(
        category_id=category.id, version=category.criteria_version,
        criteria_md=new_criteria, source=CriteriaSource.llm_feedback.value,
        feedback_ids=[feedback.id]))
    feedback.proposal_status = ProposalStatus.approved.value
    feedback.status = FeedbackStatus.incorporated.value
    feedback.resolved_at = datetime.now(UTC)
    audit(session, "user", "criteria_proposal_approved", {
        "feedback_id": feedback.id, "category_id": category.id,
        "new_version": category.criteria_version,
        "edited": edited_criteria_md is not None})
    session.commit()
    return category


def reject_proposal(session: Session, feedback: Feedback) -> None:
    feedback.proposal_status = ProposalStatus.rejected.value
    # Feedback stays open/resolvable manually (criteria untouched).
    audit(session, "user", "criteria_proposal_rejected",
          {"feedback_id": feedback.id})
    session.commit()
