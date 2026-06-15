"""Digest generation + delivery (spec §4.6).

Eligibility: email classification in digest categories, confidence ≥
threshold, received after the last *successful* run, and not already included
in any previous successful run of this digest. Dry-run renders only (status
dry_run) and does not consume eligibility; failed sends (status error) also
keep emails eligible.
"""

import re
from datetime import UTC, datetime, timedelta
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
from app.services.classifier import digest_stale_after_seconds, fetch_body
from app.services.gmail import GmailClient
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


def _depth_profile(depth: int, body_budget: int) -> dict:
    """Map a digest's depth (1 brief · 2 standard · 3 detailed) to the input/
    output knobs that trade detail for speed."""
    if depth <= 1:  # Brief — snippet only, terse, minimal generation.
        return {"fetch_body": False, "body_budget": min(body_budget, 400),
                "synthesis_chars": 1500, "micro_tokens": 60,
                "style": "Be terse: a few short lines, headline themes only."}
    if depth >= 3:  # Detailed — fuller bodies, longer synthesis.
        return {"fetch_body": True, "body_budget": int(body_budget * 1.5),
                "synthesis_chars": 6000, "micro_tokens": 160,
                "style": "Be thorough; include notable specifics per item."}
    return {"fetch_body": True, "body_budget": body_budget,
            "synthesis_chars": DIGEST_MAX_CHARS, "micro_tokens": 100, "style": ""}


def _clamp_tokens(tokens: int, settings: dict) -> int:
    """Cap an output-token budget so input + output can't overflow the model's
    context window; reserve at least half the window for the prompt."""
    max_ctx = int(settings.get("llm_max_context_tokens") or 0)
    if max_ctx > 0:
        tokens = min(tokens, max(256, max_ctx // 2))
    return tokens


def _parse_numbered(text: str, n: int) -> list[str] | None:
    """Parse `[1] ... [2] ...` lines into n summaries, or None on any mismatch."""
    out: dict[int, str] = {}
    for line in text.splitlines():
        m = re.match(r"\s*\[(\d+)\]\s*(.*)", line)
        if m:
            idx = int(m.group(1))
            if 1 <= idx <= n:
                out[idx] = m.group(2).strip()
    if len(out) == n and all(out.get(i) for i in range(1, n + 1)):
        return [out[i] for i in range(1, n + 1)]
    return None


def _email_block(email: Email, content: str, budget: int, marker: str = "") -> str:
    return (f"{marker}From: {email.sender}\nSubject: {email.subject}\n"
            f"Date: {email.received_at}\n{content[:budget]}")


async def _summarize(session: Session, digest: Digest, emails: list[Email],
                     settings: dict) -> str:
    """Two-stage: batched per-email micro-summaries, then synthesis. Output is
    token-capped so a verbose local model can't run a single call for minutes."""
    timeout = float(settings["llm_digest_timeout_seconds"])
    concurrency = int(settings["llm_max_concurrency"])
    batch_size = max(1, int(settings.get("digest_micro_batch_size") or 5))
    prof = _depth_profile(digest.depth, int(settings["digest_body_max_chars"]))

    client: GmailClient | None = None
    if prof["fetch_body"]:
        client_secret = settings.get("gmail_client_secret_json")
        if client_secret and gmail.load_token(session) is not None:
            client = GmailClient(session, client_secret)

    async def content_for(email: Email) -> str:
        text = ""
        if client is not None:
            try:
                text = await fetch_body(session, client, email)
            except gmail.GmailError as e:
                log.warning("digest_body_fetch_failed", email_id=email.id,
                            error=str(e))
        return text or email.snippet or ""

    micro_single = llm.load_prompt("digest_email_summary_system.txt")
    micro_batch = llm.load_prompt("digest_email_summary_batch_system.txt")
    micro: list[str] = []
    try:
        for start in range(0, len(emails), batch_size):
            chunk = emails[start:start + batch_size]
            contents = [await content_for(e) for e in chunk]
            session.commit()  # release any body-hash writes before the LLM await

            summaries: list[str] | None = None
            if len(chunk) > 1:
                user = "\n\n".join(
                    _email_block(e, c, prof["body_budget"], marker=f"[{i}] ")
                    for i, (e, c) in enumerate(zip(chunk, contents, strict=True), 1))
                raw = await llm.chat_text(
                    micro_batch, user, timeout=timeout, max_concurrency=concurrency,
                    settings=settings,
                    max_tokens=_clamp_tokens(prof["micro_tokens"] * len(chunk) + 32,
                                             settings))
                summaries = _parse_numbered(raw, len(chunk))

            if summaries is None:  # single email, or batch parse failed → one-by-one
                summaries = []
                for email, content in zip(chunk, contents, strict=True):
                    s = await llm.chat_text(
                        micro_single, _email_block(email, content, prof["body_budget"]),
                        timeout=timeout, max_concurrency=concurrency, settings=settings,
                        max_tokens=_clamp_tokens(prof["micro_tokens"] + 16, settings))
                    summaries.append(s.strip())

            for email, summary in zip(chunk, summaries, strict=True):
                micro.append(f"- [{email.sender} | {email.subject}] {summary[:500]}")
    finally:
        if client is not None:
            await client.aclose()

    synthesis_chars = prof["synthesis_chars"]
    synthesis_system = (digest.prompt_template
                        or llm.load_prompt("digest_synthesis_system.txt")
                        ).format(max_chars=synthesis_chars)
    style = f"{prof['style']}\n" if prof["style"] else ""
    synthesis_user = (
        f"Digest: {digest.name}\nEmails ({len(emails)}), each as a one-line "
        f"micro-summary:\n\n" + "\n".join(micro)
        + f"\n\n{style}Produce the digest now."
    )
    return (await llm.chat_text(
        synthesis_system, synthesis_user, timeout=timeout,
        max_concurrency=concurrency, settings=settings,
        max_tokens=_clamp_tokens(synthesis_chars // 4 + 64, settings),
    ))[:synthesis_chars]


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
