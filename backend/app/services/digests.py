"""Digest generation + delivery (spec §4.6).

Eligibility: email classification in digest categories, confidence ≥
threshold, received after the last *successful* run, and not already included
in any previous successful run of this digest. Dry-run renders only (status
dry_run) and does not consume eligibility; failed sends (status error) also
keep emails eligible.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import (
    Digest,
    DigestRun,
    DigestRunStatus,
    Email,
    EmailStatus,
)
from app.services import gmail, llm, settings_service, telegram
from app.services.audit import audit
from app.services.classifier import fetch_body
from app.services.gmail import GmailAuthError, GmailClient
from app.state import app_state

log = get_logger(__name__)

GMAIL_DEEP_LINK = "https://mail.google.com/mail/u/0/#all/{msg_id}"
DIGEST_MAX_CHARS = 3500  # synthesis budget; final message may add metadata/links


def eligible_emails(session: Session, digest: Digest) -> list[Email]:
    prior_runs = session.scalars(select(DigestRun).where(
        DigestRun.digest_id == digest.id,
        DigestRun.status == DigestRunStatus.success.value))
    prior_ids: set[int] = set()
    last_success: datetime | None = None
    for run in prior_runs:
        prior_ids.update(run.email_ids or [])
        if last_success is None or (run.started_at and run.started_at > last_success):
            last_success = run.started_at

    query = (select(Email)
             .where(Email.classification_id.in_(digest.category_ids or []),
                    Email.confidence >= digest.min_confidence,
                    Email.status.in_([EmailStatus.classified.value,
                                      EmailStatus.actioned.value]))
             .order_by(Email.received_at.desc()))
    if last_success is not None:
        query = query.where(Email.received_at > last_success)

    out = []
    for email in session.scalars(query):
        if email.id not in prior_ids:
            out.append(email)
        if len(out) >= digest.max_emails:
            break
    return out


async def _summarize(session: Session, digest: Digest, emails: list[Email],
                     settings: dict) -> str:
    """Two-stage: per-email micro-summary, then synthesis."""
    timeout = float(settings["llm_digest_timeout_seconds"])
    concurrency = int(settings["llm_max_concurrency"])
    body_budget = int(settings["digest_body_max_chars"])

    client: GmailClient | None = None
    client_secret = settings.get("gmail_client_secret_json")
    if client_secret and gmail.load_token(session) is not None:
        client = GmailClient(session, client_secret)

    micro_system = llm.load_prompt("digest_email_summary_system.txt")
    micro: list[str] = []
    try:
        for email in emails:
            body = ""
            if client is not None:
                try:
                    body = await fetch_body(session, client, email)
                except gmail.GmailError as e:
                    log.warning("digest_body_fetch_failed", email_id=email.id,
                                error=str(e))
            session.commit()  # release any body-hash write before the LLM await
            user = (f"From: {email.sender}\nSubject: {email.subject}\n"
                    f"Date: {email.received_at}\n\n"
                    f"{(body or email.snippet or '')[:body_budget]}")
            summary = await llm.chat_text(micro_system, user, timeout=timeout,
                                          max_concurrency=concurrency,
                                          settings=settings)
            micro.append(f"- [{email.sender} | {email.subject}] "
                         f"{summary[:500]}")
    finally:
        if client is not None:
            await client.aclose()

    synthesis_system = (digest.prompt_template
                        or llm.load_prompt("digest_synthesis_system.txt")
                        ).format(max_chars=DIGEST_MAX_CHARS)
    synthesis_user = (
        f"Digest: {digest.name}\nEmails ({len(emails)}), each as a one-line "
        f"micro-summary:\n\n" + "\n".join(micro)
        + "\n\nProduce the digest now."
    )
    return (await llm.chat_text(synthesis_system, synthesis_user, timeout=timeout,
                                max_concurrency=concurrency,
                                settings=settings))[:DIGEST_MAX_CHARS]


def _render_message(digest: Digest, emails: list[Email], summary: str,
                    dry_run_prefix: bool) -> str:
    esc = telegram.escape_html
    try:
        tz = ZoneInfo(digest.timezone or "UTC")
    except (KeyError, ZoneInfoNotFoundError):
        tz = UTC
    parts = []
    if dry_run_prefix:
        parts.append("[DRY RUN]")
    parts.append(f"<b>{esc(digest.name)}</b> — {len(emails)} email(s)")
    parts.append(esc(summary))
    if digest.include_metadata:
        lines = []
        for e in emails:
            when = e.received_at.astimezone(tz).strftime("%H:%M") \
                if e.received_at else "?"
            line = f"• {when} {esc(e.sender or '?')} — {esc(e.subject or '(no subject)')}"
            if digest.include_links:
                line += (f' <a href="{GMAIL_DEEP_LINK.format(msg_id=e.gmail_message_id)}"'
                         f">open</a>")
            lines.append(line)
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


async def run_digest(session: Session, digest: Digest, actor: str = "scheduler",
                     preview: bool = False) -> DigestRun:
    """preview=True renders the summary (status dry_run) without sending and
    without consuming eligibility."""
    settings = settings_service.get_all_settings(session, redact=False)

    run = DigestRun(digest_id=digest.id, status=DigestRunStatus.running.value)
    session.add(run)
    session.commit()

    try:
        emails = eligible_emails(session, digest)
        run.email_ids = [e.id for e in emails]
        # Commit before the (potentially minutes-long) summarization so no
        # dirty state can autoflush into a held SQLite write transaction.
        session.commit()

        if not emails:
            run.status = DigestRunStatus.empty.value
            if digest.send_no_news and not preview:
                token = settings.get("telegram_bot_token")
                chat_id = digest.telegram_chat_id \
                    or settings.get("telegram_default_chat_id")
                if token and chat_id:
                    await telegram.send_message(
                        token, str(chat_id),
                        f"<b>{telegram.escape_html(digest.name)}</b>: no news.",
                    )
            run.finished_at = datetime.now(UTC)
            session.commit()
            return run

        summary = await _summarize(session, digest, emails, settings)
        run.summary_text = summary

        if preview:
            run.status = DigestRunStatus.dry_run.value
        else:
            token = settings.get("telegram_bot_token")
            chat_id = digest.telegram_chat_id or settings.get("telegram_default_chat_id")
            if not token or not chat_id:
                raise telegram.TelegramError(
                    "Telegram bot token / chat id not configured")
            message = _render_message(digest, emails, summary, dry_run_prefix=False)
            ids = await telegram.send_message(token, str(chat_id), message)
            run.telegram_message_id = ",".join(ids)
            app_state.telegram_status = "ok"
            run.status = DigestRunStatus.success.value
    except (llm.LLMError, telegram.TelegramError, GmailAuthError,
            gmail.GmailError) as e:
        run.status = DigestRunStatus.error.value
        run.error = str(e)[:500]
        if isinstance(e, telegram.TelegramError):
            app_state.telegram_status = "error"
        log.error("digest_run_failed", digest_id=digest.id, error=str(e))
    finally:
        run.finished_at = datetime.now(UTC)
        audit(session, actor, "digest_run", {
            "digest_id": digest.id, "run_id": run.id, "status": run.status,
            "email_count": len(run.email_ids or [])})
        session.commit()
    return run
