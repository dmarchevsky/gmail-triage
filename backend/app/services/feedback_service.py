"""Feedback → criteria self-revision loop (spec §4.7).

A debounced background job builds a revision prompt per affected category and
stores the LLM's proposed criteria on the feedback row. Nothing changes
automatically — the user approves/edits/rejects in the Feedback queue.
"""

import asyncio
from datetime import UTC, datetime
from typing import Literal

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

_pending_jobs: dict[tuple[int, str], asyncio.Task] = {}


def target_category_id(feedback: Feedback) -> int | None:
    """Revise the category the email should be in; if the correction is
    'none', tighten the category it was wrongly assigned to."""
    if feedback.correct_category_id is not None:
        return feedback.correct_category_id
    if feedback.email is not None:
        return feedback.email.classification_id
    return None


def source_category_id_for(feedback: Feedback) -> int | None:
    """The category the email was wrongly classified into — the 'losing'
    category an exclusion proposal narrows — when set and different from the
    target category the feedback corrects to. Parallels `target_category_id`."""
    if feedback.source_category_id is not None \
            and feedback.source_category_id != feedback.correct_category_id:
        return feedback.source_category_id
    return None


def schedule_proposal_generation(category_id: int,
                                 debounce: float | None = None,
                                 kind: Literal["target", "source"] = "target") -> None:
    """Debounced per-(category, kind) proposal job (in-process). A category
    can simultaneously be a target for one feedback and a source for
    another, so jobs are keyed by (category_id, kind), not bare category_id."""
    delay = DEBOUNCE_SECONDS if debounce is None else debounce
    key = (category_id, kind)
    existing = _pending_jobs.get(key)
    if existing is not None and not existing.done():
        existing.cancel()
    _pending_jobs[key] = asyncio.create_task(
        _delayed_generation(category_id, delay, kind))


async def _delayed_generation(category_id: int, delay: float,
                              kind: Literal["target", "source"] = "target") -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    from app.db import get_sessionmaker

    session = get_sessionmaker()()
    try:
        if kind == "source":
            await generate_exclusion_proposal_for_category(session, category_id)
        else:
            await generate_proposal_for_category(session, category_id)
    except llm.LLMError as e:
        log.warning("proposal_generation_failed", category_id=category_id,
                    kind=kind, error=str(e))
    except Exception as e:  # noqa: BLE001 — background job must not crash loop
        log.error("proposal_job_failed", category_id=category_id, kind=kind,
                  error=str(e))
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


def open_feedback_for_source_category(session: Session, category_id: int) -> list[Feedback]:
    """All open feedback whose source category (the category the email was
    wrongly classified into) is `category_id`, oldest first."""
    return list(session.scalars(
        select(Feedback)
        .options(joinedload(Feedback.email))
        .where(Feedback.status == FeedbackStatus.open.value,
               Feedback.source_category_id == category_id)
        .order_by(Feedback.created_at)))


def _pending_target_proposals_for_category(session: Session,
                                           category_id: int) -> list[Feedback]:
    """Any feedback (regardless of `status`) currently holding a pending TARGET
    proposal whose target category is `category_id`. Unlike
    `open_feedback_for_category`, not filtered to open rows — used to find a
    same-category proposal collision across kinds when approving the other
    kind (see `approve_proposal`)."""
    return list(session.scalars(
        select(Feedback)
        .outerjoin(Email, Email.id == Feedback.email_id)
        .where(Feedback.proposal_status == ProposalStatus.pending_review.value,
               or_(Feedback.correct_category_id == category_id,
                   and_(Feedback.correct_category_id.is_(None),
                        Email.classification_id == category_id)))))


def _pending_source_proposals_for_category(session: Session,
                                           category_id: int) -> list[Feedback]:
    """Any feedback currently holding a pending SOURCE proposal whose source
    category is `category_id`. Parallels
    `_pending_target_proposals_for_category`."""
    return list(session.scalars(
        select(Feedback)
        .where(Feedback.proposal_source_status == ProposalStatus.pending_review.value,
               Feedback.source_category_id == category_id)))


async def _build_email_blocks(session: Session, client: GmailClient | None,
                              included: list[Feedback], body_max: int) -> list[str]:
    """Render one prompt block per feedback's email (fetch body via Gmail,
    falling back to the stored snippet, truncated to `body_max`). Shared by
    the target-revision and source-exclusion proposal paths."""
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
    return blocks


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
        blocks = await _build_email_blocks(session, client, included, body_max)
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


async def generate_exclusion_proposal_for_category(session: Session,
                                                    category_id: int) -> Feedback | None:
    """Build ONE consolidated EXCLUSION prompt from all open feedback whose
    *source* category (the category the email was wrongly classified into)
    is `category_id`, and store the proposal on the most-recent feedback
    (the representative). Mirrors `generate_proposal_for_category` but edits
    the losing category so it stops matching these emails, instead of
    broadening the winning category. Supersedes any prior pending source
    proposal for the category so every feedback is considered together."""
    category = session.get(Category, category_id)
    if category is None:
        return None
    fb_list = open_feedback_for_source_category(session, category_id)
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
        blocks = await _build_email_blocks(session, client, included, body_max)
    finally:
        if client is not None:
            await client.aclose()

    system = llm.load_prompt("criteria_exclusion_system.txt").format(
        category=category.name)
    user = (
        f"Current criteria for {category.name!r} (version "
        f"{category.criteria_version}):\n{category.criteria_md or '(empty)'}\n\n"
        f"The model matched the following {len(included)} email(s) to this "
        f"category, but the user says they belong to a different category; add "
        f"targeted exclusions so none of them match here anymore:\n\n"
        + "\n\n".join(blocks)
        + "\n\nProduce the revised criteria now."
    )

    result = await llm.chat_json(
        system, user, PROPOSAL_SCHEMA, "criteria_exclusion",
        timeout=float(settings["llm_classify_timeout_seconds"]),
        settings=settings,
        max_concurrency=int(settings["llm_max_concurrency"]))

    # Supersede any other pending source proposal for this category.
    for fb in fb_list:
        if fb.id != representative.id \
                and fb.proposal_source_status == ProposalStatus.pending_review.value:
            fb.proposal_source_status = ProposalStatus.none.value
            fb.proposed_source_criteria_md = None
            fb.proposal_source_explanation = None
            fb.proposal_source_feedback_ids = None

    representative.proposed_source_criteria_md = str(result["criteria_md"])
    representative.proposal_source_explanation = str(result["explanation"])[:2000]
    representative.proposal_source_status = ProposalStatus.pending_review.value
    representative.proposal_source_feedback_ids = [fb.id for fb in included]
    audit(session, "system", "exclusion_proposal_generated",
          {"category_id": category.id, "representative_id": representative.id,
           "covers": len(included)})
    session.commit()
    return representative


def approve_proposal(session: Session, feedback: Feedback,
                     edited_criteria_md: str | None = None,
                     kind: Literal["target", "source"] = "target") -> Category:
    """Approve a pending proposal, bump the edited category's criteria
    version, and record a CategoryCriteriaHistory row.

    A feedback row can carry a pending proposal of BOTH kinds at once (its
    target proposal and its source/exclusion proposal), independently
    reviewable. Resolution is deferred until both sides that exist for a row
    are terminal (approved or rejected):
      - `kind == "target"`: covered rows are marked `incorporated` UNLESS the
        row still has a pending SOURCE proposal — that row is left `open` so
        it stays reachable (e.g. for `/generate-source-proposal`) rather than
        vanishing from the open-feedback queue with its source proposal
        stranded.
      - `kind == "source"`: never marks rows incorporated on its own (the
        source edit narrows the *losing* category — an independent edit from
        the target side) EXCEPT it completes the resolution deferred above:
        for each covered row whose target proposal was already `approved`,
        it now marks the row `incorporated` / sets `resolved_at`.

    Cross-kind collision: a category can simultaneously be the subject of a
    pending TARGET proposal (from one feedback) and a pending SOURCE
    proposal (from another) — both generated against the same starting
    criteria_md/version. Approving one would let the other's stale text
    silently clobber this edit if later approved, so approving either kind
    resets any pending proposal of the OTHER kind still targeting this same
    category back to `none` — it must be regenerated against the new text.
    """
    if kind == "source":
        category_id = feedback.source_category_id
        proposed_attr, status_attr, ids_attr = (
            "proposed_source_criteria_md", "proposal_source_status",
            "proposal_source_feedback_ids")
        no_category_msg = "Feedback has no source category"
    else:
        category_id = target_category_id(feedback)
        proposed_attr, status_attr, ids_attr = (
            "proposed_criteria_md", "proposal_status", "proposal_feedback_ids")
        no_category_msg = "Feedback has no target category"

    category = session.get(Category, category_id) if category_id else None
    if category is None:
        raise ValueError(no_category_msg)
    new_criteria = edited_criteria_md if edited_criteria_md is not None \
        else getattr(feedback, proposed_attr)
    if not new_criteria:
        raise ValueError("No proposed criteria to approve")

    # Every feedback this consolidated proposal covers is incorporated at once
    # (subject to the deferral below when the other side is still pending).
    covered_ids = getattr(feedback, ids_attr) or [feedback.id]

    # Cross-kind collision: reset any pending proposal of the OTHER kind still
    # targeting this same category — it was generated against the criteria_md
    # we're about to replace and would otherwise silently clobber this edit if
    # approved later.
    other_pending = (_pending_source_proposals_for_category(session, category.id)
                     if kind == "target"
                     else _pending_target_proposals_for_category(session, category.id))
    for fb in other_pending:
        if kind == "target":
            fb.proposal_source_status = ProposalStatus.none.value
            fb.proposed_source_criteria_md = None
            fb.proposal_source_explanation = None
            fb.proposal_source_feedback_ids = None
        else:
            fb.proposal_status = ProposalStatus.none.value
            fb.proposed_criteria_md = None
            fb.proposal_explanation = None
            fb.proposal_feedback_ids = None

    category.criteria_md = new_criteria
    category.criteria_version += 1
    session.add(CategoryCriteriaHistory(
        category_id=category.id, version=category.criteria_version,
        criteria_md=new_criteria, source=CriteriaSource.llm_feedback.value,
        feedback_ids=covered_ids))

    now = datetime.now(UTC)
    for fb in session.scalars(select(Feedback).where(Feedback.id.in_(covered_ids))):
        if kind == "target":
            # Leave open if a source proposal is still pending review — resolve
            # it once that side reaches a terminal state (see kind == "source"
            # branch below).
            if fb.proposal_source_status == ProposalStatus.pending_review.value:
                continue
            fb.status = FeedbackStatus.incorporated.value
            fb.resolved_at = now
        else:
            # Completes a resolution deferred by a prior target approval.
            if fb.proposal_status == ProposalStatus.approved.value:
                fb.status = FeedbackStatus.incorporated.value
                fb.resolved_at = now
    setattr(feedback, status_attr, ProposalStatus.approved.value)
    audit(session, "user",
          "criteria_proposal_approved" if kind == "target"
          else "exclusion_proposal_approved",
          {"feedback_id": feedback.id, "category_id": category.id,
           "new_version": category.criteria_version,
           "covered": covered_ids, "edited": edited_criteria_md is not None})
    session.commit()
    return category


def reject_proposal(session: Session, feedback: Feedback,
                    kind: Literal["target", "source"] = "target") -> None:
    if kind == "source":
        feedback.proposal_source_status = ProposalStatus.rejected.value
        feedback.proposal_source_feedback_ids = None
    else:
        feedback.proposal_status = ProposalStatus.rejected.value
        feedback.proposal_feedback_ids = None
    # Covered feedback stays open/resolvable manually (criteria untouched).
    audit(session, "user",
          "criteria_proposal_rejected" if kind == "target"
          else "exclusion_proposal_rejected",
          {"feedback_id": feedback.id})
    session.commit()
