"""Digest generation + delivery (spec §4.6).

Eligibility: email classification in digest categories, confidence ≥
threshold, received after the last *successful* run, and not already included
in any previous successful run of this digest. Dry-run renders only (status
dry_run) and does not consume eligibility; failed sends (status error) also
keep emails eligible.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import (
    Digest,
    DigestRun,
    DigestRunStatus,
    Email,
    EmailStatus,
)
from app.services import llm, settings_service, telegram
from app.services.audit import audit
from app.services.classifier import digest_stale_after_seconds
from app.state import app_state

log = get_logger(__name__)

GMAIL_DEEP_LINK = "https://mail.google.com/mail/u/0/#all/{msg_id}"
DIGEST_MAX_CHARS = 3500  # synthesis budget; final message may add metadata/links


def eligible_emails(session: Session, digest: Digest) -> list[Email]:
    # Last successful run time bounds the window; the received_at > last_success
    # filter below excludes everything prior runs already covered, so there's no
    # need to load every historical run's email_ids into memory.
    last_success: datetime | None = session.scalar(
        select(func.max(DigestRun.started_at)).where(
            DigestRun.digest_id == digest.id,
            DigestRun.status == DigestRunStatus.success.value))

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
        out.append(email)
        if len(out) >= digest.max_emails:
            break
    return out


def _clamp_tokens(tokens: int, settings: dict) -> int:
    """Cap an output-token budget so input + output can't overflow the model's
    context window; reserve at least half the window for the prompt."""
    max_ctx = int(settings.get("llm_max_context_tokens") or 0)
    if max_ctx > 0:
        tokens = min(tokens, max(256, max_ctx // 2))
    return tokens


def _summary_line(email: Email) -> str:
    """One digest line for an email, from its saved summary (snippet fallback).
    Content only — sender/subject/time live in the message list below, so they
    are deliberately omitted here."""
    text = (email.summary or email.snippet or "").strip()
    return f"- {text[:500]}"


async def _summarize(session: Session, digest: Digest, emails: list[Email],
                     settings: dict) -> str:
    """Build the digest body from the per-email summaries saved at classification
    time. `digest_mode == "assemble"` lists them verbatim (no LLM); `"synthesize"`
    makes one LLM call combining them via the editable digest prompt."""
    lines = [_summary_line(e) for e in emails]
    any_content = any((e.summary or e.snippet) for e in emails)

    if (settings.get("digest_mode") or "assemble") != "synthesize":
        # Pure assembly — never ship a bodyless digest (let the caller fail the run
        # rather than send sender/subject lines with no content behind them).
        return "\n".join(lines)[:DIGEST_MAX_CHARS] if any_content else ""

    timeout = float(settings["llm_digest_timeout_seconds"])
    concurrency = int(settings["llm_max_concurrency"])
    synthesis_chars = DIGEST_MAX_CHARS
    synthesis_system = (digest.prompt_template
                        or settings.get("prompt_digest_synthesis")
                        or settings_service.DEFAULTS["prompt_digest_synthesis"]
                        ).format(max_chars=synthesis_chars)
    synthesis_user = (
        f"Digest: {digest.name}\nEmails ({len(emails)}), each as a one-line "
        f"summary:\n\n" + "\n".join(lines) + "\n\nProduce the digest now."
    )

    async def synthesize() -> str:
        return (await llm.chat_text(
            synthesis_system, synthesis_user, timeout=timeout,
            max_concurrency=concurrency, settings=settings,
            max_tokens=_clamp_tokens(synthesis_chars // 4 + 64, settings),
        )).strip()

    body = await synthesize()
    if not body:
        # The local model returned no content — retry once, then fall back to the
        # saved summaries so the digest is never blank. Only fall back when at
        # least one email actually has content; otherwise return "" and let the
        # caller fail the run rather than ship sender/subject lines as the "body".
        log.warning("digest_synthesis_empty_retrying", digest_id=digest.id,
                    emails=len(emails), any_content=any_content)
        body = await synthesize()
        if not body:
            log.warning("digest_synthesis_empty_fallback_list", digest_id=digest.id,
                        emails=len(emails), any_content=any_content)
            body = "\n".join(lines).strip() if any_content else ""
    return body[:synthesis_chars]


_EXPANDABLE_MIN_CHARS = 350


def _normalize_summary(text: str) -> str:
    """Strip trailing spaces per line and collapse runs of blank lines."""
    out: list[str] = []
    for ln in (line.rstrip() for line in text.strip().splitlines()):
        if ln == "" and (not out or out[-1] == ""):
            continue
        out.append(ln)
    return "\n".join(out)


def _blockquote(inner_html: str) -> str:
    """Wrap already-safe inner HTML in a blockquote; expandable when long."""
    expandable = inner_html.count("\n") >= 4 or len(inner_html) > _EXPANDABLE_MIN_CHARS
    tag = "<blockquote expandable>" if expandable else "<blockquote>"
    return f"{tag}{inner_html}</blockquote>"


def _summary_body(digest: Digest, emails: list[Email], summary: str,
                  digest_mode: str) -> list[str]:
    """Presentation parts for the summary. synthesize: a bold TL;DR line then the
    rest in a blockquote. assemble: per-email bullets (subject + saved summary)
    in a blockquote. Returned strings are already-safe HTML."""
    esc = telegram.escape_html
    if digest_mode == "synthesize":
        norm = _normalize_summary(summary)
        first, _, rest = norm.partition("\n")
        parts = []
        if first:
            parts.append(f"<b>{esc(first)}</b>")
        if rest.strip():
            parts.append(_blockquote(esc(rest)))
        return parts
    # assemble mode: `summary` is unused here — render directly from each email's
    # saved summary (same field _summarize used), so the bullets stay consistent
    # with the stored summary_text.
    lines = []
    for e in emails:
        text = (e.summary or e.snippet or "").strip()[:500]
        if not text:
            continue  # skip emails with no saved summary/snippet
        lines.append(f"• <b>{esc(e.subject or '(no subject)')}</b> — {esc(text)}")
    return [_blockquote("\n".join(lines))] if lines else []


def _render_message(digest: Digest, emails: list[Email], summary: str,
                    dry_run_prefix: bool, digest_mode: str = "assemble") -> str:
    esc = telegram.escape_html
    try:
        tz = ZoneInfo(digest.timezone or "UTC")
    except (KeyError, ZoneInfoNotFoundError):
        tz = UTC
    parts = []
    if dry_run_prefix:
        parts.append("[DRY RUN]")
    date_str = datetime.now(tz).strftime("%b %d")
    parts.append(
        f"📬 <b>{esc(digest.name)}</b> · {date_str}\n"
        f"<b>{len(emails)}</b> new email(s)")
    parts.extend(_summary_body(digest, emails, summary, digest_mode))
    if digest.include_metadata:
        lines = ["──────────"]
        for e in emails:
            when = e.received_at.astimezone(tz).strftime("%H:%M") \
                if e.received_at else "?"
            sender = esc(e.sender or "?")
            subject = esc(e.subject or "(no subject)")
            if digest.include_links:
                subject = (
                    f'<a href="{GMAIL_DEEP_LINK.format(msg_id=e.gmail_message_id)}">'
                    f"{subject}</a>")
            lines.append(f"🔹 <b>{sender}</b> <i>{when}</i>\n{subject}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


async def run_digest(session: Session, digest: Digest, actor: str = "scheduler",
                     preview: bool = False) -> DigestRun:
    """preview=True renders the summary (status dry_run) without sending and
    without consuming eligibility."""
    settings = settings_service.get_all_settings(session, redact=False)

    # Guard against concurrent runs of the same digest (e.g. a double-clicked
    # "run now"/"preview" or a scheduled run overlapping a manual one). The
    # `running` row is committed first thing below, so a slightly-later run sees
    # it and skips. Previews are guarded too: each preview holds the LLM (which
    # serves serially), so a flurry of preview clicks must not stack up runs —
    # any in-flight run of either kind blocks a new one. Only a *fresh* run
    # blocks — an orphaned `running` row left by a crash is past the stale
    # threshold (and the recovery sweep will fail it), so it must not block
    # forever.
    fresh_cutoff = datetime.now(UTC) - timedelta(
        seconds=digest_stale_after_seconds(settings))
    in_progress = session.scalars(
        select(DigestRun)
        .where(DigestRun.digest_id == digest.id,
               DigestRun.status == DigestRunStatus.running.value,
               DigestRun.started_at > fresh_cutoff)
        .order_by(DigestRun.started_at.desc())
    ).first()
    if in_progress is not None:
        log.warning("digest_run_skipped_already_running",
                    digest_id=digest.id, run_id=in_progress.id, preview=preview)
        return in_progress

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
                        f"✅ <b>{telegram.escape_html(digest.name)}</b>: no news.",
                    )
            run.finished_at = datetime.now(UTC)
            session.commit()
            return run

        summary = await _summarize(session, digest, emails, settings)
        run.summary_text = summary
        if not summary.strip():
            # Empty after retry + micro-summary fallback: surface as a failed run
            # rather than silently shipping a bodyless digest. Emails stay eligible.
            raise llm.LLMError(
                "digest body empty after retry and micro-summary fallback")

        if preview:
            run.status = DigestRunStatus.dry_run.value
        else:
            token = settings.get("telegram_bot_token")
            chat_id = digest.telegram_chat_id or settings.get("telegram_default_chat_id")
            if not token or not chat_id:
                raise telegram.TelegramError(
                    "Telegram bot token / chat id not configured")
            message = _render_message(
                digest, emails, summary, dry_run_prefix=False,
                digest_mode=settings.get("digest_mode") or "assemble")
            ids = await telegram.send_message(token, str(chat_id), message)
            run.telegram_message_id = ",".join(ids)
            app_state.telegram_status = "ok"
            run.status = DigestRunStatus.success.value
    except Exception as e:  # noqa: BLE001 — any failure must mark the run errored,
        # never leave it stuck in `running` (CancelledError is BaseException and
        # still propagates so shutdown/cancellation is not swallowed).
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
