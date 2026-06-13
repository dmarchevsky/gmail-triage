"""Feedback → criteria self-revision loop (spec §4.7).

A debounced background job builds a revision prompt per affected category and
stores the LLM's proposed criteria on the feedback row. Nothing changes
automatically — the user approves/edits/rejects in the Feedback queue.
"""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, joinedload

from app.logging_setup import get_logger
from app.models import (
    Category,
    CategoryCriteriaHistory,
    CriteriaSource,
    Email,
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
# Cap how many misclassified emails go into one consolidated revision prompt
# (bounds Gmail body fetches + LLM context); the rest get a follow-up proposal.
MAX_CONSOLIDATED_EMAILS = 10

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
        await generate_proposal_for_category(session, category_id)
    except llm.LLMError as e:
        log.warning("proposal_generation_failed", category_id=category_id,
                    error=str(e))
    except Exception as e:  # noqa: BLE001 — background job must not crash loop
        log.error("proposal_job_failed", category_id=category_id, error=str(e))
    finally:
        session.close()


def open_feedback_for_category(session: Session, category_id: int) -> list[Feedback]:
    """All open feedback whose target category is `category_id` (correct
    category, or the wrongly-assigned one when the correction is 'none'),
    oldest first."""
    return list(session.scalars(
        select(Feedback)
        .options(joinedload(Feedback.email))
        .outerjoin(Email, Email.id == Feedback.email_id)
        .where(Feedback.status == FeedbackStatus.open.value,
               or_(Feedback.correct_category_id == category_id,
                   and_(Feedback.correct_category_id.is_(None),
                        Email.classification_id == category_id)))
        .order_by(Feedback.created_at)))


async def generate_proposal_for_category(session: Session,
                                         category_id: int) -> Feedback | None:
    """Build ONE consolidated revision prompt from all open feedback for the
    category and store the proposal on the most-recent feedback (the
    representative). Supersedes any prior pending proposal for the category so
    every feedback is considered together (no overwrite-on-approve)."""
    category = session.get(Category, category_id)
    if category is None:
        return None
    fb_list = open_feedback_for_category(session, category_id)
    if not fb_list:
        return None
    included = fb_list[-MAX_CONSOLIDATED_EMAILS:]
    representative = included[-1]

    settings = settings_service.get_all_settings(session, redact=False)
    body_max = int(settings["classify_body_max_chars"])

    client: GmailClient | None = None
    client_secret = settings.get("gmail_client_secret_json")
    if client_secret and gmail.load_token(session) is not None:
        client = GmailClient(session, client_secret)
    try:
        blocks = []
        for i, fb in enumerate(included, 1):
            email = fb.email
            body = ""
            if client is not None and email is not None:
                try:
                    body = await fetch_body(session, client, email)
                except gmail.GmailError:
                    body = ""
            body = (body or (email.snippet if email else "") or "")[:body_max]
            original = (email.classification.name
                        if email is not None and email.classification else "none")
            corrected = (session.get(Category, fb.correct_category_id).name
                         if fb.correct_category_id else "none")
            blocks.append(
                f"--- Email {i} ---\n"
                f"From: {email.sender if email else '?'}\n"
                f"Subject: {email.subject if email else '?'}\n"
                f"Originally classified as: {original}\n"
                f"Model rationale: {email.rationale if email else '(none)'}\n"
                f"User says correct category is: {corrected}\n"
                f"User note: {fb.user_note or '(none)'}\n"
                f"Body (truncated):\n{body}")
    finally:
        if client is not None:
            await client.aclose()

    system = llm.load_prompt("criteria_revision_system.txt").format(
        category=category.name)
    user = (
        f"Current criteria for {category.name!r} (version "
        f"{category.criteria_version}):\n{category.criteria_md or '(empty)'}\n\n"
        f"The model misclassified the following {len(included)} email(s); revise "
        f"the criteria so all of them classify correctly:\n\n"
        + "\n\n".join(blocks)
        + "\n\nProduce the revised criteria now."
    )

    result = await llm.chat_json(
        system, user, PROPOSAL_SCHEMA, "criteria_revision",
        timeout=float(settings["llm_classify_timeout_seconds"]),
        settings=settings,
        max_concurrency=int(settings["llm_max_concurrency"]))

    # Supersede any other pending proposal for this category.
    for fb in fb_list:
        if fb.id != representative.id \
                and fb.proposal_status == ProposalStatus.pending_review.value:
            fb.proposal_status = ProposalStatus.none.value
            fb.proposed_criteria_md = None
            fb.proposal_explanation = None
            fb.proposal_feedback_ids = None

    representative.proposed_criteria_md = str(result["criteria_md"])
    representative.proposal_explanation = str(result["explanation"])[:2000]
    representative.proposal_status = ProposalStatus.pending_review.value
    representative.proposal_feedback_ids = [fb.id for fb in included]
    audit(session, "system", "criteria_proposal_generated",
          {"category_id": category.id, "representative_id": representative.id,
           "covers": len(included)})
    session.commit()
    return representative


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

    # Every feedback this consolidated proposal covers is incorporated at once.
    covered_ids = feedback.proposal_feedback_ids or [feedback.id]

    category.criteria_md = new_criteria
    category.criteria_version += 1
    session.add(CategoryCriteriaHistory(
        category_id=category.id, version=category.criteria_version,
        criteria_md=new_criteria, source=CriteriaSource.llm_feedback.value,
        feedback_ids=covered_ids))

    now = datetime.now(UTC)
    for fb in session.scalars(select(Feedback).where(Feedback.id.in_(covered_ids))):
        fb.status = FeedbackStatus.incorporated.value
        fb.resolved_at = now
    feedback.proposal_status = ProposalStatus.approved.value
    audit(session, "user", "criteria_proposal_approved", {
        "feedback_id": feedback.id, "category_id": category.id,
        "new_version": category.criteria_version,
        "covered": covered_ids, "edited": edited_criteria_md is not None})
    session.commit()
    return category


def reject_proposal(session: Session, feedback: Feedback) -> None:
    feedback.proposal_status = ProposalStatus.rejected.value
    feedback.proposal_feedback_ids = None
    # Covered feedback stays open/resolvable manually (criteria untouched).
    audit(session, "user", "criteria_proposal_rejected",
          {"feedback_id": feedback.id})
    session.commit()
