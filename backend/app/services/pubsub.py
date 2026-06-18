"""Cloud Pub/Sub *pull* consumer for Gmail push notifications.

Outbound-only (no inbound endpoint, fits the trusted-LAN deployment): a
background task long-polls the configured pull subscription, acks notifications,
and wakes the poller. The Gmail notification payload ({emailAddress, historyId})
is only a wake signal — the poller keeps using its own stored historyId cursor,
so duplicate / out-of-order / forged messages cause at worst a redundant,
idempotent sync. Polling stays on as a safety net (see poller.poller_loop).

Auth reuses the connected user's OAuth token (gmail.modify + pubsub scopes), so
there is no separate service-account secret to store.
"""

import asyncio
from datetime import UTC, datetime

import httpx
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.services import gmail, poller, settings_service
from app.state import app_state

log = get_logger(__name__)

PUBSUB_API = "https://pubsub.googleapis.com/v1"
PULL_MAX_MESSAGES = 10
# Re-check cadence when push is disabled / unconfigured / disconnected.
_IDLE_SLEEP = 30
# Backoff after a Pub/Sub error so the loop never hot-spins.
_ERROR_BACKOFF = 30
# Short pause after an empty pull, guarding against a server that returns
# immediately (keeps real-time latency to a couple of seconds without spinning).
_EMPTY_PULL_SLEEP = 2


class PubSubError(Exception):
    """A Pub/Sub pull/ack request failed."""


def _push_active(session: Session) -> tuple[bool, str, str]:
    """(active, subscription, client_secret): whether the consumer should pull.
    Active only when push mode is selected, a subscription is configured, OAuth
    credentials exist, and Gmail is connected."""
    mode = settings_service.get_setting(session, "gmail_ingest_mode")
    subscription = settings_service.get_setting(session, "gmail_pubsub_subscription") or ""
    client_secret = settings_service.get_setting(session, "gmail_client_secret_json") or ""
    connected = gmail.load_token(session) is not None
    active = bool(mode == "push" and subscription and client_secret and connected)
    return active, subscription, client_secret


async def _ack(subscription: str, access_token: str, ack_ids: list[str]) -> None:
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.post(
            f"{PUBSUB_API}/{subscription}:acknowledge",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"ackIds": ack_ids},
        )
    if resp.status_code != 200:
        raise PubSubError(f"acknowledge {resp.status_code}: {resp.text[:200]}")


async def _pull_once(subscription: str, access_token: str) -> int:
    """One synchronous pull + ack cycle. Acks every received message and wakes
    the poller if any arrived. Returns the number of messages received.

    Messages are acked regardless of payload contents: the payload is only a wake
    signal, so there is no poison-message risk and nothing to validate before
    acking. Acking promptly prevents Pub/Sub redelivery of already-handled pings.
    """
    async with httpx.AsyncClient(timeout=120) as http:
        resp = await http.post(
            f"{PUBSUB_API}/{subscription}:pull",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"maxMessages": PULL_MAX_MESSAGES},
        )
    if resp.status_code != 200:
        raise PubSubError(f"pull {resp.status_code}: {resp.text[:200]}")
    received = resp.json().get("receivedMessages") or []
    if not received:
        return 0
    # Wake the poller BEFORE acking: if the ack fails, Pub/Sub redelivers the
    # ping (harmless — the sync is idempotent), so a wake is never lost.
    app_state.last_notification_at = datetime.now(UTC).isoformat()
    poller.wake()
    ack_ids = [m["ackId"] for m in received if m.get("ackId")]
    if ack_ids:
        await _ack(subscription, access_token, ack_ids)
    log.info("pubsub_notifications", count=len(received))
    return len(received)


async def pubsub_loop() -> None:
    """Background task; never crashes the app on Pub/Sub errors. No-ops (idle
    sleep) whenever push mode is off, unconfigured, or Gmail is disconnected."""
    from app.db import get_sessionmaker

    app_state.pubsub_status = "stopped"
    while True:
        try:
            session = get_sessionmaker()()
            try:
                active, subscription, client_secret = _push_active(session)
                if not active:
                    app_state.pubsub_status = "stopped"
                    await asyncio.sleep(_IDLE_SLEEP)
                    continue
                # Refresh + persist the token while we hold the session, then
                # release it before the long-poll so we don't pin a DB connection.
                access_token = await gmail.get_access_token(session, client_secret)
            finally:
                session.close()

            app_state.pubsub_status = "running"
            count = await _pull_once(subscription, access_token)
            if count == 0:
                await asyncio.sleep(_EMPTY_PULL_SLEEP)
        except asyncio.CancelledError:
            app_state.pubsub_status = "stopped"
            raise
        except gmail.GmailAuthError as e:
            app_state.pubsub_status = "error"
            log.warning("pubsub_auth_error", error=str(e))
            await asyncio.sleep(_ERROR_BACKOFF)
        except Exception as e:  # noqa: BLE001 — consumer must survive anything
            app_state.pubsub_status = "error"
            log.error("pubsub_loop_error", error=str(e))
            await asyncio.sleep(_ERROR_BACKOFF)
