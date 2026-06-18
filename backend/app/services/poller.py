"""Inbox poller: baseline sync, historyId incremental sync, fallback re-sync.

Runs as an asyncio task started from the app lifespan. Each cycle:
- skip if paused or Gmail not connected;
- first run: baseline via messages.list (q=after:<initial_lookback>);
- later runs: users.history.list from the stored historyId; on 404
  (history expired) fall back to messages.list after the newest stored email;
- fetch metadata for new message ids, persist idempotently (unique
  gmail_message_id); ingest messages whose Gmail labels fall in the
  configured poll scope (poll_scope_labels: inbox + chosen category tabs),
  excluding Sent/Drafts/Spam/Trash/Chats and the user's own mail.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging_setup import get_logger, truncate_snippet
from app.models import Email, EmailStatus
from app.services import gmail, settings_service
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
    try:
        msg = await client.get_message_metadata(message_id)
    except GmailNotFound:
        # Message was deleted/moved between the history record and this fetch.
        # Skip it so one missing message can't abort (and stall) the whole poll.
        log.info("message_gone_skipped", gmail_message_id=message_id)
        return False
    meta = gmail.parse_message_meta(msg)
    labels = set(meta.pop("label_ids"))
    if labels & EXCLUDED_LABELS or not (labels & scope):
        return False  # out of the configured scope (or Sent/Draft/Spam/Trash/Chat)
    sender_addr = meta["sender"].lower()
    if any(own in sender_addr for own in own_addresses):
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
        ids = [added["message"]["id"]
               for record in page.get("history", [])
               for added in record.get("messagesAdded", [])]
        new_count += await _ingest_new_ids(session, client, ids, own, scope)
        page_token = page.get("nextPageToken")
        if not page_token:
            break
    client.auth_row.history_id = latest_history_id
    session.commit()
    return new_count


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
    if new_count:
        audit(session, "system", "poll_completed", {"mode": mode, "new_emails": new_count})
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
