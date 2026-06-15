"""Classification pipeline: per-email queue with processing state.

Flow for each pending email:
  pending → processing → classified / actioned / skipped / error

The queue_loop() background task processes one email at a time, newest-first.
LLM connection failure pauses the queue (LLMUnavailable); a per-email timeout
(LLMTimeout) marks only that email as error and continues immediately.
The stall_checker() task resets emails stuck in processing after a timeout.
classify_pending() remains for the synchronous /classify/run-now endpoint.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import Category, Email, EmailStatus
from app.services import llm, settings_service
from app.services.gmail import GmailAuthError, GmailClient, GmailNotFound
from app.services.matchers import sender_matches
from app.state import app_state

log = get_logger(__name__)

_IDLE_SLEEP = 5    # seconds between queue polls when nothing pending
_LLM_BACKOFF = 60  # seconds to wait after LLMUnavailable before retrying

CLASSIFICATION_SCHEMA_TEMPLATE = {
    "type": "object",
    "properties": {
        "category": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "rationale": {"type": "string"},
    },
    "required": ["category", "confidence", "rationale"],
    "additionalProperties": False,
}


def build_classification_prompt(categories: list[Category], email: Email,
                                body: str, max_body_chars: int) -> tuple[str, str, dict]:
    categories_block = "\n\n".join(
        f"### {c.name}\n{c.criteria_md.strip() or '(no criteria provided)'}"
        for c in categories
    )
    user = llm.load_prompt("classification_user.txt").format(
        categories_block=categories_block,
        sender=email.sender or "(unknown)",
        subject=email.subject or "(no subject)",
        date=email.received_at.isoformat() if email.received_at else "(unknown)",
        body=body[:max_body_chars] if body else "(empty body)",
    )
    schema = {**CLASSIFICATION_SCHEMA_TEMPLATE,
              "properties": {**CLASSIFICATION_SCHEMA_TEMPLATE["properties"],
                             "category": {"type": "string",
                                          "enum": [c.name for c in categories] + ["none"]}}}
    return llm.load_prompt("classification_system.txt"), user, schema


async def fetch_body(session: Session, client: GmailClient, email: Email) -> str:
    """Body text for an email: stored copy if retained, else fetch from Gmail."""
    from app.services import gmail
    if email.body_text is not None:
        return email.body_text
    msg = await client.get_message_full(email.gmail_message_id)
    body = gmail.extract_body_text(msg.get("payload", {}))
    email.body_text_hash = gmail.body_hash(body)
    if bool(settings_service.get_setting(session, "store_bodies")):
        email.body_text = body
    return body


async def classify_email(session: Session, client: GmailClient, email: Email,
                         categories: list[Category], settings: dict) -> None:
    try:
        body = await fetch_body(session, client, email)
    except GmailNotFound:
        email.status = EmailStatus.error.value
        email.error = "Message no longer available in Gmail"
        return
    session.commit()  # release the body-hash write before the LLM await
    system, user, schema = build_classification_prompt(
        categories, email, body, int(settings["classify_body_max_chars"]))
    try:
        result = await llm.chat_json(
            system, user, schema, "email_classification",
            timeout=float(settings["llm_classify_timeout_seconds"]),
            settings=settings,
            max_concurrency=int(settings["llm_max_concurrency"]),
        )
    except llm.LLMInvalidOutput as e:
        email.status = EmailStatus.error.value
        email.error = f"LLM output invalid after retry: {e}"
        return
    except llm.LLMTimeout as e:
        email.status = EmailStatus.error.value
        email.error = f"LLM timed out: {e}"
        return

    name = result["category"]
    category = next((c for c in categories if c.name == name), None)
    email.classification_id = category.id if category else None
    email.confidence = max(0.0, min(1.0, float(result["confidence"])))
    email.rationale = str(result["rationale"])[:2000]
    _, email.llm_model = llm.resolve_llm_target(settings)
    email.classified_at = datetime.now(UTC)
    email.status = EmailStatus.classified.value
    email.error = None


async def classify_one(session: Session, client: GmailClient, email: Email,
                       categories: list[Category], rules: list,
                       settings: dict) -> Email:
    """Classify a single email (ignore list → hard rules → LLM), then run the
    rule engine. Sets processing state before any I/O. Raises LLMUnavailable
    on connection failure; leaves status pending when there is nothing to
    classify against."""
    from app.services import rules as rules_engine

    email.status = EmailStatus.processing.value
    email.processing_started_at = datetime.now(UTC)
    session.commit()

    if sender_matches(settings.get("ignore_senders") or [], email.sender or ""):
        email.status = EmailStatus.skipped.value
        session.commit()
        return email

    hard = next((r for r in rules if rules_engine.is_hard_rule(r)
                 and sender_matches([r.match_sender_pattern], email.sender or "")),
                None)
    if hard is not None:
        email.confidence = 1.0
        email.rationale = f"hard rule: {hard.name}"
        email.classified_at = datetime.now(UTC)
        email.status = EmailStatus.classified.value
    else:
        if not categories:
            email.status = EmailStatus.pending.value  # nothing to classify against
            session.commit()
            return email
        try:
            await classify_email(session, client, email, categories, settings)
        except llm.LLMUnavailable:
            session.commit()
            raise
    session.commit()
    log.info("email_classified", email_id=email.id, status=email.status,
             category_id=email.classification_id, confidence=email.confidence)

    if email.status == EmailStatus.classified.value and rules:
        await rules_engine.apply_rules_to_email(session, client, email, rules)
    return email


async def classify_pending(session: Session, limit: int = 50) -> dict:
    """Classify up to `limit` pending emails synchronously (used by /classify/run-now).
    Processes newest-first. LLM-unreachable stops the batch and leaves the
    remainder pending."""
    from app.services import rules as rules_engine

    settings = settings_service.get_all_settings(session, redact=False)
    categories = list(session.scalars(
        select(Category).where(Category.enabled.is_(True)).order_by(Category.id)))
    pending = list(session.scalars(
        select(Email).where(Email.status == EmailStatus.pending.value)
        .order_by(Email.created_at.desc()).limit(limit)))
    if not pending:
        return {"classified": 0, "skipped": 0, "errors": 0, "actioned": 0,
                "pending_left": 0}

    all_rules = rules_engine.load_enabled_rules(session)
    counts = {"classified": 0, "skipped": 0, "errors": 0, "actioned": 0}

    client_secret = settings.get("gmail_client_secret_json")
    if not client_secret:
        raise GmailAuthError("Gmail is not connected")
    client = GmailClient(session, client_secret)
    app_state.classifier_running = True
    app_state.classifier_done = 0
    app_state.classifier_total = len(pending)
    try:
        for email in pending:
            try:
                await classify_one(session, client, email, categories,
                                   all_rules, settings)
            except llm.LLMUnavailable as e:
                log.warning("llm_unreachable_batch_stopped", error=str(e))
                email.status = EmailStatus.pending.value
                email.processing_started_at = None
                session.commit()
                break
            app_state.classifier_done += 1
            if email.status == EmailStatus.pending.value:
                continue  # no categories to classify against
            if email.status == EmailStatus.skipped.value:
                counts["skipped"] += 1
            elif email.status == EmailStatus.error.value:
                counts["errors"] += 1
            else:
                counts["classified"] += 1
                if email.status == EmailStatus.actioned.value:
                    counts["actioned"] += 1
    finally:
        app_state.classifier_running = False
        await client.aclose()

    remaining = session.scalar(
        select(Email.id).where(Email.status == EmailStatus.pending.value).limit(1))
    return {**counts, "pending_left": int(remaining is not None)}


async def _process_next() -> bool:
    """Pick the next pending email (newest first), classify it, return True if work done."""
    from app.db import get_sessionmaker
    from app.services import rules as rules_engine

    session = get_sessionmaker()()
    try:
        email = session.scalars(
            select(Email)
            .where(Email.status == EmailStatus.pending.value)
            .order_by(Email.created_at.desc())
            .limit(1)
        ).first()
        if email is None:
            return False

        settings = settings_service.get_all_settings(session, redact=False)
        client_secret = settings.get("gmail_client_secret_json")
        if not client_secret:
            return False  # Gmail not connected; no point processing

        categories = list(session.scalars(
            select(Category).where(Category.enabled.is_(True)).order_by(Category.id)))
        all_rules = rules_engine.load_enabled_rules(session)

        app_state.classifier_running = True
        app_state.classifier_current_email_id = email.id

        try:
            client = GmailClient(session, client_secret)
        except GmailAuthError:
            email.status = EmailStatus.error.value
            email.error = "Gmail auth expired"
            email.processing_started_at = None
            session.commit()
            return True

        try:
            await classify_one(session, client, email, categories, all_rules, settings)
        except llm.LLMUnavailable as err:
            # Cannot reach LLM — reset this email to pending and back off
            email.status = EmailStatus.pending.value
            email.processing_started_at = None
            session.commit()
            log.warning("llm_unavailable_queue_paused", error=str(err))
            app_state.classifier_running = False
            app_state.classifier_current_email_id = None
            await asyncio.sleep(_LLM_BACKOFF)
            return True
        finally:
            await client.aclose()
            app_state.classifier_current_email_id = None

        return True
    finally:
        session.close()


async def queue_loop() -> None:
    """Continuous background task: picks and classifies one pending email at a time."""
    log.info("classifier_queue_started")
    while True:
        try:
            did_work = await _process_next()
        except Exception:  # noqa: BLE001
            log.exception("classifier_queue_unhandled_error")
            await asyncio.sleep(5)
            continue
        if not did_work:
            app_state.classifier_running = False
            await asyncio.sleep(_IDLE_SLEEP)


async def stall_checker() -> None:
    """Periodic task: reset emails stuck in processing back to pending."""
    log.info("stall_checker_started")
    while True:
        await asyncio.sleep(60)
        from app.db import get_sessionmaker
        session = get_sessionmaker()()
        try:
            settings = settings_service.get_all_settings(session, redact=False)
            timeout = float(settings.get("llm_classify_timeout_seconds") or 120) + 30
            cutoff = datetime.now(UTC) - timedelta(seconds=timeout)
            stalled = list(session.scalars(
                select(Email).where(
                    Email.status == EmailStatus.processing.value,
                    Email.processing_started_at < cutoff,
                )
            ))
            for e in stalled:
                e.status = EmailStatus.pending.value
                e.processing_started_at = None
                log.warning("stalled_email_reset", email_id=e.id)
            if stalled:
                session.commit()
        except Exception:  # noqa: BLE001
            log.exception("stall_checker_error")
        finally:
            session.close()
