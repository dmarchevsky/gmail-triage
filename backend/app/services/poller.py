"""Inbox poller: baseline sync, historyId incremental sync, fallback re-sync.

Runs as an asyncio task started from the app lifespan. Each cycle:
- skip if paused or Gmail not connected;
- first run: baseline via messages.list (q=after:<initial_lookback>);
- later runs: users.history.list from the stored historyId; on 404
  (history expired) fall back to messages.list after the newest stored email;
- fetch metadata for new message ids, persist idempotently (unique
  gmail_message_id); ingest messages whose Gmail labels fall in the
  configured poll scope (poll_scope_labels: inbox + chosen category tabs),
  excluding Sent/Drafts/Spam/Trash/Chats and the user's own mail;
- after an incremental sync, periodically sweep the last CATCHUP_WINDOW with
  messages.list as a safety net for anything history reported but we could
  not fetch (Gmail can 404 a just-announced message) or never reported.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging_setup import get_logger, truncate_snippet
from app.models import Email, EmailStatus
from app.services import gmail, settings_service, telegram
from app.services.audit import audit
from app.services.gmail import (
    GmailAuthError,
    GmailClient,
    GmailHistoryExpired,
    GmailNotFound,
)
from app.state import app_state

log = get_logger(__name__)

# Created inside poller_loop (must be bound to the running event loop).
_wake_event: asyncio.Event | None = None
def wake() -> None:
    """Interrupt the sleep between poll cycles (e.g. after un-pausing)."""
    if _wake_event is not None:
        _wake_event.set()


# Never ingest these regardless of scope (Sent/Drafts/Spam/Trash/Chats).
EXCLUDED_LABELS = {"SENT", "DRAFT", "TRASH", "SPAM", "CHAT"}

# Gmail expires a watch within 7 days; renew once we are this close to expiry.
WATCH_RENEW_BEFORE = timedelta(hours=24)
# In push mode the configured poll interval governs real-time (handled by wakes);
# the loop itself only needs to poll occasionally as a catch-up safety net.
PUSH_FALLBACK_POLL_SECONDS = 900
# Gmail can announce a message in history.list before messages.get can serve it
# (seen in push mode: 404 ~300 ms after the notification, fine minutes later).
# Retry a 404 after these delays (seconds) before treating it as deleted.
NOT_FOUND_RETRY_DELAYS: tuple[float, ...] = (2, 5, 10)
# Catch-up sweep: re-list recent mail so nothing history missed is lost for good.
CATCHUP_WINDOW = timedelta(hours=24)
CATCHUP_SWEEP_INTERVAL = timedelta(minutes=15)
_last_catchup_at: datetime | None = None
# Telegram "reconnect Gmail" alert: send immediately on first failure, then at
# most once per this interval while the auth error persists.
AUTH_ALERT_COOLDOWN = timedelta(hours=24)


def _own_addresses(session: Session) -> set[str]:
    loaded = gmail.load_token(session)
    if loaded and loaded[0].email_address:
        return {loaded[0].email_address.lower()}
    return set()


def _scope_labels(session: Session) -> set[str]:
    """Gmail label IDs that define the poll scope (configurable in Settings)."""
    return set(settings_service.get_setting(session, "poll_scope_labels") or [])


def _existing_message_ids(session: Session, ids: list[str]) -> set[str]:
    """One IN-query returning which of `ids` are already ingested."""
    if not ids:
        return set()
    return set(session.scalars(
        select(Email.gmail_message_id).where(Email.gmail_message_id.in_(ids))))


async def _persist_message(session: Session, client: GmailClient, message_id: str,
                           own_addresses: set[str], scope: set[str]) -> bool:
    """Fetch + stage one new message into the session (no existence check, no
    commit — callers pre-filter known ids and commit per page). Returns True if
    a row was added."""
    msg = None
    for delay in (*NOT_FOUND_RETRY_DELAYS, None):
        try:
            msg = await client.get_message_metadata(message_id)
            break
        except GmailNotFound:
            if delay is None:
                break
            log.info("message_not_found_retry", gmail_message_id=message_id, delay=delay)
            await asyncio.sleep(delay)
    if msg is None:
        # Still 404 after retries: deleted/moved since the history record (e.g. a
        # draft autosave). Skip it so one missing message can't stall the poll;
        # the catch-up sweep picks it up if it reappears within CATCHUP_WINDOW.
        log.info("message_gone_skipped", gmail_message_id=message_id)
        return False
    meta = gmail.parse_message_meta(msg)
    labels = set(meta.pop("label_ids"))
    sender_addr = meta["sender"].lower()
    if labels & EXCLUDED_LABELS:
        reason = "excluded"
    elif not (labels & scope):
        reason = "scope"
    elif any(own in sender_addr for own in own_addresses):
        reason = "own"
    else:
        reason = None
    if reason:
        log.info("message_skipped", gmail_message_id=message_id,
                 label_ids=sorted(labels), reason=reason)
        return False
    session.add(Email(**meta, status=EmailStatus.pending.value, dry_run=False))
    log.info("email_ingested", gmail_message_id=message_id,
             sender_domain=meta["sender_domain"],
             snippet=truncate_snippet(meta["snippet"]))
    return True


async def _ingest_new_ids(session: Session, client: GmailClient, ids: list[str],
                          own: set[str], scope: set[str]) -> int:
    """Batch-filter already-known ids, stage the rest, commit once. Dedups
    within the page so the unique gmail_message_id constraint can't trip."""
    known = _existing_message_ids(session, ids)
    new_count = 0
    seen: set[str] = set()
    for mid in ids:
        if mid in known or mid in seen:
            continue
        seen.add(mid)
        if await _persist_message(session, client, mid, own, scope):
            new_count += 1
    session.commit()
    return new_count


async def _baseline_sync(session: Session, client: GmailClient) -> int:
    lookback_hours = int(settings_service.get_setting(session, "initial_lookback_hours"))
    new_count = 0
    own = _own_addresses(session)
    scope = _scope_labels(session)
    if lookback_hours > 0:
        after = datetime.now(UTC) - timedelta(hours=lookback_hours)
        q = f"after:{int(after.timestamp())} -in:sent -in:chats"
        page_token = None
        while True:
            page = await client.list_messages(q=q, page_token=page_token)
            ids = [ref["id"] for ref in page.get("messages", [])]
            new_count += await _ingest_new_ids(session, client, ids, own, scope)
            page_token = page.get("nextPageToken")
            if not page_token:
                break
    profile = await client.get_profile()
    client.auth_row.history_id = str(profile.get("historyId", ""))
    session.commit()
    return new_count


async def _incremental_sync(session: Session, client: GmailClient,
                            start_history_id: str) -> int:
    new_count = 0
    own = _own_addresses(session)
    scope = _scope_labels(session)
    page_token = None
    latest_history_id = start_history_id
    while True:
        page = await client.list_history(start_history_id, page_token=page_token)
        latest_history_id = str(page.get("historyId", latest_history_id))
        # History carries each message's labels at add time: drop drafts/sent/
        # chats here so they cost no fetch (drafts also 404 once re-saved).
        ids = [added["message"]["id"]
               for record in page.get("history", [])
               for added in record.get("messagesAdded", [])
               if not set(added["message"].get("labelIds", [])) & EXCLUDED_LABELS]
        new_count += await _ingest_new_ids(session, client, ids, own, scope)
        page_token = page.get("nextPageToken")
        if not page_token:
            break
    client.auth_row.history_id = latest_history_id
    session.commit()
    return new_count


async def _catchup_sweep(session: Session, client: GmailClient) -> int:
    """Safety net: ingest any in-scope message from the last CATCHUP_WINDOW that
    is not in the DB yet. Known ids are filtered with one IN-query per page, so a
    healthy sweep costs a single messages.list call."""
    after_ts = int((datetime.now(UTC) - CATCHUP_WINDOW).timestamp())
    q = f"after:{after_ts} -in:sent -in:chats -in:drafts -in:spam -in:trash"
    own = _own_addresses(session)
    scope = _scope_labels(session)
    new_count = 0
    page_token = None
    while True:
        page = await client.list_messages(q=q, page_token=page_token)
        ids = [ref["id"] for ref in page.get("messages", [])]
        known = _existing_message_ids(session, ids)
        for mid in ids:
            if mid not in known and await _persist_message(session, client, mid, own, scope):
                log.warning("catchup_ingested", gmail_message_id=mid)
                new_count += 1
        session.commit()
        page_token = page.get("nextPageToken")
        if not page_token:
            break
    return new_count


async def _maybe_catchup_sweep(session: Session, client: GmailClient) -> int:
    """Run _catchup_sweep at most once per CATCHUP_SWEEP_INTERVAL (push mode
    wakes the poller on every mailbox change)."""
    global _last_catchup_at
    now = datetime.now(UTC)
    if _last_catchup_at is not None and now - _last_catchup_at < CATCHUP_SWEEP_INTERVAL:
        return 0
    _last_catchup_at = now
    return await _catchup_sweep(session, client)


async def _fallback_sync(session: Session, client: GmailClient) -> int:
    """History expired: list recent messages since the newest stored email."""
    newest = session.scalar(select(Email.received_at).order_by(Email.received_at.desc())
                            .limit(1))
    after_ts = int((newest or (datetime.now(UTC) - timedelta(days=1))).timestamp()) - 3600
    new_count = 0
    own = _own_addresses(session)
    scope = _scope_labels(session)
    page_token = None
    while True:
        page = await client.list_messages(q=f"after:{after_ts} -in:sent -in:chats",
                                          page_token=page_token)
        for ref in page.get("messages", []):
            if await _persist_message(session, client, ref["id"], own, scope):
                new_count += 1
        page_token = page.get("nextPageToken")
        if not page_token:
            break
    profile = await client.get_profile()
    client.auth_row.history_id = str(profile.get("historyId", ""))
    session.commit()
    return new_count


async def poll_once(session: Session) -> dict:
    """One poll cycle. Raises GmailAuthError on auth problems."""
    client_secret = settings_service.get_setting(session, "gmail_client_secret_json")
    if not client_secret or gmail.load_token(session) is None:
        raise GmailAuthError("Gmail is not connected")
    client = GmailClient(session, client_secret)
    try:
        if client.auth_row.history_id:
            try:
                new_count = await _incremental_sync(session, client,
                                                    client.auth_row.history_id)
                new_count += await _maybe_catchup_sweep(session, client)
                mode = "incremental"
            except GmailHistoryExpired:
                log.info("history_expired_falling_back")
                new_count = await _fallback_sync(session, client)
                mode = "fallback"
        else:
            new_count = await _baseline_sync(session, client)
            mode = "baseline"
    finally:
        await client.aclose()

    app_state.gmail_status = "ok"
    app_state.gmail_email = client.auth_row.email_address
    reset_alert = client.auth_row.last_auth_alert_at is not None
    if reset_alert:
        client.auth_row.last_auth_alert_at = None
    if new_count:
        audit(session, "system", "poll_completed", {"mode": mode, "new_emails": new_count})
    if reset_alert or new_count:
        session.commit()

    return {"mode": mode, "new_emails": new_count}


def _record_poll_failure(session, error: str, *, kind: str | None = None) -> None:
    """Audit a poll failure so it surfaces in Recent activity. Best-effort: the
    poller must survive even if logging the failure itself fails."""
    payload: dict[str, str] = {"error": error}
    if kind:
        payload["kind"] = kind
    try:
        session.rollback()  # discard any partial work from the failed cycle
        audit(session, "system", "poll_failed", payload)
        session.commit()
    except Exception:  # noqa: BLE001 — never let audit logging crash the loop
        log.warning("poll_failure_audit_failed", error=error)


def _build_auth_alert_message(error: str, base_url: str | None) -> str:
    lines = [
        "⚠️ <b>MailTriage: Gmail reconnect needed</b>",
        f"Polling is failing: <code>{telegram.escape_html(error[:300])}</code>",
        "This will keep failing every poll cycle until you reconnect Gmail.",
    ]
    if base_url:
        reconnect_url = telegram.escape_html(f"{base_url.rstrip('/')}/#/settings?tab=mailbox")
        lines.append(f"Reconnect: {reconnect_url}")
    else:
        lines.append(
            "Open MailTriage → Settings → Mailbox and tap Reconnect. (Set a"
            " \"Public base URL\" in Settings → Notifications to get a direct"
            " link here when you're away from the LAN.)"
        )
    lines.append("You'll get a reminder once every 24h until this is resolved.")
    return "\n".join(lines)


async def _maybe_send_auth_alert(session: Session, error: str) -> None:
    """Telegram-alert about a Gmail auth failure: immediately the first time,
    then at most once per AUTH_ALERT_COOLDOWN while it stays broken. Must never
    raise — called from poller_loop's `except GmailAuthError` clause, which has
    no outer handler of its own."""
    try:
        loaded = gmail.load_token(session)
        if loaded is None:
            return  # nothing to persist the cooldown against
        row, _token = loaded
        token = settings_service.get_setting(session, "telegram_bot_token")
        chat_id = settings_service.get_setting(session, "telegram_default_chat_id")
        if not token or not chat_id:
            return  # Telegram not configured — nothing to send
        now = datetime.now(UTC)
        if row.last_auth_alert_at is not None \
                and now - row.last_auth_alert_at < AUTH_ALERT_COOLDOWN:
            return  # still within the 24h cooldown
        base_url = settings_service.get_setting(session, "public_base_url")
        message = _build_auth_alert_message(error, base_url)
        await telegram.send_message(token, str(chat_id), message)
        row.last_auth_alert_at = now
        session.commit()
    except Exception as e:  # noqa: BLE001 — alerting must never crash the poller
        session.rollback()
        log.warning("gmail_auth_alert_failed", error=str(e))


async def _ensure_watch(session: Session, client: GmailClient) -> None:
    """Push mode: (re)start the Gmail watch if it is missing or within
    WATCH_RENEW_BEFORE of expiry. Persists the new expiration (epoch ms). The
    watch only asks Gmail to publish change notifications — it cannot send mail."""
    topic = settings_service.get_setting(session, "gmail_pubsub_topic")
    if not topic:
        return
    exp = client.auth_row.watch_expiration
    if exp:
        try:
            expires_at = datetime.fromtimestamp(int(exp) / 1000, UTC)
            if expires_at - datetime.now(UTC) > WATCH_RENEW_BEFORE:
                return  # still comfortably fresh
        except (ValueError, TypeError):
            pass  # malformed expiry → re-watch
    result = await client.watch(topic, list(_scope_labels(session)) or None)
    client.auth_row.watch_expiration = str(result.get("expiration", ""))
    session.commit()
    log.info("gmail_watch_started", expiration=client.auth_row.watch_expiration)


async def _maybe_manage_watch(session: Session, *, push: bool) -> None:
    """Keep the Gmail watch aligned with the ingest mode: ensure/renew it in push
    mode, tear down any lingering watch in poll mode. Best-effort: a watch error
    (e.g. the topic's publisher IAM not yet granted) is logged but never raised,
    so it cannot fail the catch-up poll cycle that already ingested mail."""
    client_secret = settings_service.get_setting(session, "gmail_client_secret_json")
    if not client_secret or gmail.load_token(session) is None:
        return
    try:
        client = GmailClient(session, client_secret)
        try:
            if push:
                await _ensure_watch(session, client)
            elif client.auth_row.watch_expiration:
                await client.stop_watch()
                client.auth_row.watch_expiration = None
                session.commit()
                log.info("gmail_watch_stopped")
        finally:
            await client.aclose()
    except Exception as e:  # noqa: BLE001 — watch upkeep must never fail the poll
        session.rollback()
        log.warning("gmail_watch_management_failed", push=push, error=str(e))


async def poller_loop() -> None:
    """Background task; never crashes the app on Gmail errors."""
    from app.db import get_sessionmaker

    global _wake_event
    _wake_event = asyncio.Event()
    app_state.poller_status = "running"
    while True:
        session = get_sessionmaker()()
        interval = 300
        try:
            interval = max(60, int(settings_service.get_setting(
                session, "poll_interval_seconds")))
            paused = bool(settings_service.get_setting(session, "poller_paused"))
            mode = settings_service.get_setting(session, "gmail_ingest_mode")
            connected = gmail.load_token(session) is not None
            if paused:
                app_state.poller_status = "paused"
            elif not connected:
                app_state.poller_status = "running"
                app_state.gmail_status = "not_connected"
            else:
                app_state.poller_status = "running"
                result = await poll_once(session)
                app_state.poller_last_run_at = datetime.now(UTC).isoformat()
                app_state.poller_last_error = None
                # Keep the watch aligned with the mode; in push mode the periodic
                # poll is just a catch-up safety net, so back off to a long cadence.
                await _maybe_manage_watch(session, push=(mode == "push"))
                if mode == "push":
                    interval = max(interval, PUSH_FALLBACK_POLL_SECONDS)
                log.info("poll_cycle_done", **result)
        except GmailAuthError as e:
            app_state.gmail_status = "auth_error"
            app_state.poller_last_error = str(e)
            log.warning("poll_auth_error", error=str(e))
            _record_poll_failure(session, str(e), kind="auth")
            await _maybe_send_auth_alert(session, str(e))
        except asyncio.CancelledError:
            app_state.poller_status = "stopped"
            raise
        except Exception as e:  # noqa: BLE001 — poller must survive anything
            app_state.poller_last_error = str(e)
            log.error("poll_cycle_failed", error=str(e))
            _record_poll_failure(session, str(e))
        finally:
            session.close()

        _wake_event.clear()
        try:
            await asyncio.wait_for(_wake_event.wait(), timeout=interval)
        except TimeoutError:
            pass
