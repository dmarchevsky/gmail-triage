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
import html as _html
import re as _re
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import Category, DigestRun, DigestRunStatus, Email, EmailStatus
from app.services import llm, settings_service
from app.services.audit import audit
from app.services.gmail import GmailAuthError, GmailClient, GmailNotFound
from app.services.matchers import sender_matches
from app.state import app_state

log = get_logger(__name__)

_IDLE_SLEEP = 5    # seconds between queue polls when nothing pending
_LLM_BACKOFF = 60  # seconds to wait after LLMUnavailable before retrying
_RECOVERY_INTERVAL = 60  # seconds between recovery sweeps (stall_checker)
_ERROR_RETRY_EVERY = 10  # retry `error` emails every Nth sweep (~10 min)

# Output-token cap for the combined classify+summarize call. Bounds wall-clock
# time for local models; extended needs more room for a detailed paragraph.
_SUMMARY_MAX_TOKENS = {"concise": 1024, "default": 2048, "extended": 4096}

# Character limit passed to the LLM prompt so the model targets the right length.
_SUMMARY_MAX_CHARS = {"concise": 150, "default": 350, "extended": 700}

# Multiplier applied to classify_body_max_chars per depth so Extended sees more
# of long multi-item emails (promotional lists, changelogs, etc.) without
# changing the default or concise behaviour.
_BODY_DEPTH_MULTIPLIER = {"concise": 0.5, "default": 1.0, "extended": 2.0}



def strip_summary_html(text: str) -> str:
    """Remove HTML tags and unescape entities from a stored summary.

    Applied at write time (summarize_email) and defensively at read time so
    old summaries with stray markup are also cleaned. Newlines are preserved
    so list-format summaries (one item per line) survive storage."""
    text = _re.sub(r"<[^>]+>", "", text)
    text = _html.unescape(text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return _re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _audit_classification_failed(session: Session, email: Email,
                                 reason: str, error: str | None) -> None:
    """Record a classification failure in the audit log so it appears in the
    dashboard Recent activity feed (not only the email detail modal). `reason`
    is a short stable code: timeout | invalid_output | gmail_not_found |
    gmail_auth | stalled."""
    audit(session, "system", "classification_failed",
          {"email_id": email.id, "reason": reason, "error": (error or "")[:300]})


CLASSIFICATION_SCHEMA_TEMPLATE = {
    "type": "object",
    "properties": {
        "category": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "rationale": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["category", "confidence", "rationale", "summary"],
    "additionalProperties": False,
}


def build_classification_prompt(categories: list[Category], email: Email,
                                body: str, max_body_chars: int,
                                system_prompt: str,
                                summarization_depth: str = "default",
                                settings: dict | None = None) -> tuple[str, str, dict]:
    categories_block = "\n\n".join(
        f"### {c.name}\n{c.criteria_md.strip() or '(no criteria provided)'}"
        for c in categories
    )
    effective_body_max = int(max_body_chars * _BODY_DEPTH_MULTIPLIER.get(summarization_depth, 1.0))
    user = llm.load_prompt("classification_user.txt").format(
        categories_block=categories_block,
        sender=email.sender or "(unknown)",
        subject=email.subject or "(no subject)",
        date=email.received_at.isoformat() if email.received_at else "(unknown)",
        body=body[:effective_body_max] if body else "(empty body)",
    )
    schema = {**CLASSIFICATION_SCHEMA_TEMPLATE,
              "properties": {**CLASSIFICATION_SCHEMA_TEMPLATE["properties"],
                             "category": {"type": "string",
                                          "enum": [c.name for c in categories] + ["none"]}}}
    max_chars = _SUMMARY_MAX_CHARS.get(summarization_depth, _SUMMARY_MAX_CHARS["default"])
    summary_instruction = settings_service.active_summary_prompt(
        settings or {}).format(max_chars=max_chars)
    return system_prompt + "\n" + summary_instruction, user, schema



async def fetch_body(session: Session, client: GmailClient, email: Email,
                     store_bodies: bool | None = None) -> str:
    """Body text for an email: stored copy if retained, else fetch from Gmail.

    Callers that already hold the settings dict should pass `store_bodies` to
    avoid a per-email settings query; otherwise it is read from the DB."""
    from app.services import gmail
    if email.body_text is not None:
        return email.body_text
    msg = await client.get_message_full(email.gmail_message_id)
    body = gmail.extract_body_text(msg.get("payload", {}))
    email.body_text_hash = gmail.body_hash(body)
    if store_bodies is None:
        store_bodies = bool(settings_service.get_setting(session, "store_bodies"))
    if store_bodies:
        email.body_text = body
    return body


async def classify_email(session: Session, client: GmailClient, email: Email,
                         categories: list[Category], settings: dict) -> None:
    email.attempts = (email.attempts or 0) + 1
    try:
        body = await fetch_body(session, client, email,
                                store_bodies=bool(settings.get("store_bodies")))
    except GmailNotFound:
        email.status = EmailStatus.error.value
        email.error = "Message no longer available in Gmail"
        # A 404 is a deterministic permanent failure — park attempts at the cap
        # so the recovery sweep doesn't retry it (and re-hit Gmail) every cycle.
        email.attempts = max(email.attempts or 0,
                             int(settings.get("classify_max_attempts") or 5))
        _audit_classification_failed(session, email, "gmail_not_found", email.error)
        return
    session.commit()  # release the body-hash write before the LLM await
    depth = settings.get("summarization_depth") or "default"
    system, user, schema = build_classification_prompt(
        categories, email, body, int(settings["classify_body_max_chars"]),
        settings.get("prompt_classification_system")
        or settings_service.DEFAULTS["prompt_classification_system"],
        summarization_depth=depth,
        settings=settings)
    enable_thinking = bool(settings.get("llm_classify_enable_thinking") or False)
    max_tok_override = int(settings.get("llm_classify_max_tokens") or 0)
    max_tokens = max_tok_override if max_tok_override > 0 else _SUMMARY_MAX_TOKENS.get(depth, 2048)
    extra_body = ({"chat_template_kwargs": {"enable_thinking": True}} if enable_thinking else None)
    try:
        result = await llm.chat_json(
            system, user, schema, "email_classification",
            timeout=float(settings["llm_classify_timeout_seconds"]),
            settings=settings,
            max_concurrency=int(settings["llm_max_concurrency"]),
            max_tokens=max_tokens,
            extra_body=extra_body,
        )
    except llm.LLMInvalidOutput as e:
        email.status = EmailStatus.error.value
        email.error = f"LLM output invalid after retry: {e}"
        _audit_classification_failed(session, email, "invalid_output", email.error)
        return
    except llm.LLMTimeout as e:
        email.status = EmailStatus.error.value
        email.error = f"LLM timed out: {e}"
        _audit_classification_failed(session, email, "timeout", email.error)
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
    raw_summary = result.get("summary", "")
    cleaned = strip_summary_html(raw_summary).strip() if raw_summary else ""
    email.summary = cleaned or None


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
            _audit_classification_failed(session, email, "gmail_auth", email.error)
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


def _recover_stalled_emails(session: Session, settings: dict) -> None:
    """Reset emails stuck in `processing` back to `pending` (or `error` once the
    attempt cap is hit). Also catches orphaned rows whose `processing_started_at`
    is NULL — `NULL < cutoff` is never true in SQL, so they would otherwise never
    be recovered."""
    timeout = float(settings.get("llm_classify_timeout_seconds") or 120) + 30
    max_attempts = int(settings.get("classify_max_attempts") or 5)
    cutoff = datetime.now(UTC) - timedelta(seconds=timeout)
    stalled = list(session.scalars(
        select(Email).where(
            Email.status == EmailStatus.processing.value,
            or_(Email.processing_started_at < cutoff,
                Email.processing_started_at.is_(None)),
        )
    ))
    for e in stalled:
        e.processing_started_at = None
        if (e.attempts or 0) >= max_attempts:
            e.status = EmailStatus.error.value
            e.error = f"Stalled in processing; gave up after {e.attempts} attempts"
            _audit_classification_failed(session, e, "stalled", e.error)
            log.warning("stalled_email_failed", email_id=e.id, attempts=e.attempts)
        else:
            e.status = EmailStatus.pending.value
            log.warning("stalled_email_reset", email_id=e.id, attempts=e.attempts)


def _recover_error_emails(session: Session, settings: dict) -> None:
    """Retry emails left in `error` by resetting them to `pending`, while under
    the attempt cap. At the cap they stay terminally `error` so a permanently
    broken email (deleted in Gmail, invalid LLM output) can't hot-loop."""
    max_attempts = int(settings.get("classify_max_attempts") or 5)
    retryable = list(session.scalars(
        select(Email).where(
            Email.status == EmailStatus.error.value,
            Email.attempts < max_attempts,
        )
    ))
    for e in retryable:
        e.status = EmailStatus.pending.value
        e.processing_started_at = None
        log.info("error_email_retry", email_id=e.id, attempts=e.attempts)


def digest_stale_after_seconds(settings: dict) -> float:
    """How long a digest run may sit in `running` before it is presumed dead.
    Comfortably above a normal run so legitimately-slow summarizations aren't
    cut off. Shared by the recovery sweep and run_digest's concurrency guard."""
    return max(float(settings.get("llm_digest_timeout_seconds") or 300) * 2, 1800)


def _recover_stalled_digests(session: Session, settings: dict) -> None:
    """Fail digest runs stuck in `running` past a generous threshold — e.g. when a
    restart killed the process mid-summarization so run_digest's finally never ran."""
    cutoff = datetime.now(UTC) - timedelta(seconds=digest_stale_after_seconds(settings))
    stalled = list(session.scalars(
        select(DigestRun).where(
            DigestRun.status == DigestRunStatus.running.value,
            DigestRun.started_at < cutoff,
        )
    ))
    for run in stalled:
        run.status = DigestRunStatus.error.value
        if not run.error:
            run.error = "Stalled run recovered (no completion within threshold)"
        if run.finished_at is None:
            run.finished_at = datetime.now(UTC)
        log.warning("stalled_digest_run_recovered", run_id=run.id,
                    digest_id=run.digest_id)


async def stall_checker() -> None:
    """Periodic recovery sweep: reset stalled `processing` emails, retry `error`
    emails (bounded by classify_max_attempts), and fail digest runs stuck in
    `running`. Crash-safe — never lets an exception escape the loop."""
    log.info("stall_checker_started")
    tick = 0
    while True:
        await asyncio.sleep(_RECOVERY_INTERVAL)
        tick += 1
        from app.db import get_sessionmaker
        session = get_sessionmaker()()
        try:
            settings = settings_service.get_all_settings(session, redact=False)
            _recover_stalled_emails(session, settings)
            if tick % _ERROR_RETRY_EVERY == 0:
                _recover_error_emails(session, settings)
            _recover_stalled_digests(session, settings)
            session.commit()
        except Exception:  # noqa: BLE001
            log.exception("stall_checker_error")
        finally:
            session.close()
