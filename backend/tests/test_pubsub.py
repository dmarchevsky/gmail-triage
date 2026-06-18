"""Gmail push (watch + Cloud Pub/Sub *pull*) real-time ingestion.

Pull-subscription model: a background consumer long-polls the subscription,
acks notifications, and wakes the existing poller — no inbound endpoint. The
notification ({emailAddress, historyId}) is only a wake signal; the poller keeps
using its own stored historyId cursor, so duplicates/forgeries are idempotent.
"""

import base64
import json
import urllib.parse
from datetime import UTC, datetime, timedelta

import pytest
import respx

from app.services import gmail, settings_service

CLIENT_SECRET_JSON = json.dumps({
    "installed": {
        "client_id": "cid.apps.googleusercontent.com",
        "client_secret": "csecret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
})
MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
PUBSUB_SCOPE = "https://www.googleapis.com/auth/pubsub"


def make_token(scope=MODIFY_SCOPE, expires_in=3600):
    return {
        "access_token": "at-123",
        "refresh_token": "rt-456",
        "scope": scope,
        "token_type": "Bearer",
        "expiry": (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat(),
    }


# ── Task 1: settings + schema foundation ─────────────────────────────────────

def test_ingest_settings_defaults(db_session):
    assert settings_service.get_setting(db_session, "gmail_ingest_mode") == "poll"
    assert settings_service.get_setting(db_session, "gmail_pubsub_topic") == ""
    assert settings_service.get_setting(db_session, "gmail_pubsub_subscription") == ""


def test_ingest_settings_roundtrip_and_exposed(db_session):
    settings_service.set_setting(db_session, "gmail_ingest_mode", "push")
    settings_service.set_setting(db_session, "gmail_pubsub_topic", "projects/p/topics/t")
    settings_service.set_setting(db_session, "gmail_pubsub_subscription",
                                 "projects/p/subscriptions/s")
    db_session.commit()
    allset = settings_service.get_all_settings(db_session)
    assert allset["gmail_ingest_mode"] == "push"
    assert allset["gmail_pubsub_topic"] == "projects/p/topics/t"
    assert allset["gmail_pubsub_subscription"] == "projects/p/subscriptions/s"


def test_pubsub_config_not_secret(db_session):
    """Topic/subscription are resource names, not secrets — never redacted."""
    assert "gmail_pubsub_topic" not in settings_service.SECRET_KEYS
    assert "gmail_pubsub_subscription" not in settings_service.SECRET_KEYS
    assert "gmail_ingest_mode" not in settings_service.SECRET_KEYS


def test_gmail_auth_has_watch_expiration(db_session):
    from app.models import GmailAuth
    db_session.add(GmailAuth(token_json="x", granted_scopes=[],
                             watch_expiration="1750000000000"))
    db_session.commit()
    db_session.expire_all()
    assert db_session.query(GmailAuth).one().watch_expiration == "1750000000000"


# ── Task 2: Gmail client watch/stop + pubsub scope + token helper ────────────

@pytest.fixture()
def connected(client, db_session):
    """Gmail connected: client secret + token stored, email known."""
    settings_service.set_setting(db_session, "gmail_client_secret_json", CLIENT_SECRET_JSON)
    row = gmail.save_token(db_session, make_token(), email="me@gmail.test")
    db_session.commit()
    return row


def test_build_auth_url_default_scope_modify_only():
    url = gmail.build_auth_url(CLIENT_SECRET_JSON, "https://x/cb")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["scope"] == [MODIFY_SCOPE]


def test_build_auth_url_push_includes_pubsub_scope():
    url = gmail.build_auth_url(CLIENT_SECRET_JSON, "https://x/cb", include_pubsub=True)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["scope"] == [f"{MODIFY_SCOPE} {PUBSUB_SCOPE}"]


def test_pubsub_scope_is_not_send_capable():
    gmail.assert_scopes_safe([MODIFY_SCOPE, PUBSUB_SCOPE])  # must not raise


@respx.mock
async def test_watch_posts_topic_and_labels(connected, db_session):
    route = respx.post(f"{gmail.GMAIL_API}/watch").respond(200, json={
        "historyId": "5555", "expiration": "1750000000000"})
    client = gmail.GmailClient(db_session, CLIENT_SECRET_JSON)
    try:
        out = await client.watch("projects/p/topics/t", ["INBOX"])
    finally:
        await client.aclose()
    assert out["expiration"] == "1750000000000"
    sent = json.loads(route.calls.last.request.content)
    assert sent["topicName"] == "projects/p/topics/t"
    assert sent["labelIds"] == ["INBOX"]
    assert sent["labelFilterBehavior"] == "include"


@respx.mock
async def test_stop_watch_posts_stop(connected, db_session):
    route = respx.post(f"{gmail.GMAIL_API}/stop").respond(200, json={})
    client = gmail.GmailClient(db_session, CLIENT_SECRET_JSON)
    try:
        await client.stop_watch()
    finally:
        await client.aclose()
    assert route.called


@respx.mock
async def test_get_access_token_refreshes_when_expired(connected, db_session):
    _, token = gmail.load_token(db_session)
    token["expiry"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    gmail.save_token(db_session, token)
    db_session.commit()
    respx.post(gmail.GOOGLE_TOKEN_URL).respond(200, json={
        "access_token": "fresh-at", "expires_in": 3600})
    tok = await gmail.get_access_token(db_session, CLIENT_SECRET_JSON)
    assert tok == "fresh-at"
    _, stored = gmail.load_token(db_session)
    assert stored["access_token"] == "fresh-at"


async def test_get_access_token_uses_cached_when_fresh(connected, db_session):
    tok = await gmail.get_access_token(db_session, CLIENT_SECRET_JSON)
    assert tok == "at-123"  # token still valid; no refresh performed


# ── Task 3: Pub/Sub pull consumer ────────────────────────────────────────────

PUBSUB_API = "https://pubsub.googleapis.com/v1"
SUBSCRIPTION = "projects/p/subscriptions/s"


def _pubsub_msg(history_id="9001", ack_id="ack-1", email="me@gmail.test"):
    data = base64.b64encode(
        json.dumps({"emailAddress": email, "historyId": history_id}).encode()).decode()
    return {"ackId": ack_id, "message": {"data": data, "messageId": "m-1"}}


@respx.mock
async def test_pull_once_acks_and_wakes(client, monkeypatch):
    from app.services import pubsub
    from app.state import app_state
    woke = []
    monkeypatch.setattr(pubsub.poller, "wake", lambda: woke.append(True))
    pull = respx.post(f"{PUBSUB_API}/{SUBSCRIPTION}:pull").respond(
        200, json={"receivedMessages": [_pubsub_msg()]})
    ack = respx.post(f"{PUBSUB_API}/{SUBSCRIPTION}:acknowledge").respond(200, json={})

    count = await pubsub._pull_once(SUBSCRIPTION, "at-123")

    assert count == 1
    assert pull.called and ack.called
    assert json.loads(ack.calls.last.request.content)["ackIds"] == ["ack-1"]
    assert woke == [True]
    assert app_state.last_notification_at is not None


@respx.mock
async def test_pull_once_empty_does_not_wake(client, monkeypatch):
    from app.services import pubsub
    woke = []
    monkeypatch.setattr(pubsub.poller, "wake", lambda: woke.append(True))
    respx.post(f"{PUBSUB_API}/{SUBSCRIPTION}:pull").respond(200, json={})
    count = await pubsub._pull_once(SUBSCRIPTION, "at-123")
    assert count == 0
    assert woke == []


@respx.mock
async def test_pull_once_raises_on_http_error(client):
    from app.services import pubsub
    respx.post(f"{PUBSUB_API}/{SUBSCRIPTION}:pull").respond(403, json={"error": "denied"})
    with pytest.raises(pubsub.PubSubError):
        await pubsub._pull_once(SUBSCRIPTION, "at-123")


def test_push_inactive_in_poll_mode(connected, db_session):
    from app.services import pubsub
    active, _, _ = pubsub._push_active(db_session)
    assert active is False  # default ingest mode is "poll"


def test_push_active_when_push_configured(connected, db_session):
    from app.services import pubsub
    settings_service.set_setting(db_session, "gmail_ingest_mode", "push")
    settings_service.set_setting(db_session, "gmail_pubsub_subscription", SUBSCRIPTION)
    db_session.commit()
    active, sub, secret = pubsub._push_active(db_session)
    assert active is True
    assert sub == SUBSCRIPTION and secret


def test_push_inactive_when_subscription_missing(connected, db_session):
    from app.services import pubsub
    settings_service.set_setting(db_session, "gmail_ingest_mode", "push")
    db_session.commit()
    active, _, _ = pubsub._push_active(db_session)
    assert active is False  # push selected but no subscription configured


# ── Task 4: watch lifecycle in poller + status exposure ──────────────────────

def _ms_from_now(**delta):
    return str(int((datetime.now(UTC) + timedelta(**delta)).timestamp() * 1000))


@respx.mock
async def test_ensure_watch_starts_when_absent(connected, db_session):
    from app.models import GmailAuth
    from app.services import poller
    settings_service.set_setting(db_session, "gmail_pubsub_topic", "projects/p/topics/t")
    db_session.commit()
    future_ms = _ms_from_now(days=7)
    watch = respx.post(f"{gmail.GMAIL_API}/watch").respond(200, json={
        "historyId": "5", "expiration": future_ms})
    client = gmail.GmailClient(db_session, CLIENT_SECRET_JSON)
    try:
        await poller._ensure_watch(db_session, client)
    finally:
        await client.aclose()
    assert watch.called
    sent = json.loads(watch.calls.last.request.content)
    assert sent["topicName"] == "projects/p/topics/t"
    db_session.expire_all()
    assert db_session.query(GmailAuth).one().watch_expiration == future_ms


@respx.mock
async def test_ensure_watch_skips_when_fresh(connected, db_session):
    from app.services import poller
    settings_service.set_setting(db_session, "gmail_pubsub_topic", "projects/p/topics/t")
    connected.watch_expiration = _ms_from_now(days=5)
    db_session.commit()
    watch = respx.post(f"{gmail.GMAIL_API}/watch").respond(200, json={"expiration": "x"})
    client = gmail.GmailClient(db_session, CLIENT_SECRET_JSON)
    try:
        await poller._ensure_watch(db_session, client)
    finally:
        await client.aclose()
    assert not watch.called  # still well within the renewal window


@respx.mock
async def test_ensure_watch_renews_near_expiry(connected, db_session):
    from app.services import poller
    settings_service.set_setting(db_session, "gmail_pubsub_topic", "projects/p/topics/t")
    connected.watch_expiration = _ms_from_now(hours=1)
    db_session.commit()
    watch = respx.post(f"{gmail.GMAIL_API}/watch").respond(200, json={
        "expiration": _ms_from_now(days=7)})
    client = gmail.GmailClient(db_session, CLIENT_SECRET_JSON)
    try:
        await poller._ensure_watch(db_session, client)
    finally:
        await client.aclose()
    assert watch.called  # within 24h of expiry → renew


@respx.mock
async def test_maybe_manage_watch_stops_in_poll_mode(connected, db_session):
    from app.models import GmailAuth
    from app.services import poller
    connected.watch_expiration = "1750000000000"
    db_session.commit()
    stop = respx.post(f"{gmail.GMAIL_API}/stop").respond(200, json={})
    await poller._maybe_manage_watch(db_session, push=False)
    assert stop.called
    db_session.expire_all()
    assert db_session.query(GmailAuth).one().watch_expiration is None


def test_status_exposes_ingest_section(client, db_session, connected):
    settings_service.set_setting(db_session, "gmail_ingest_mode", "push")
    connected.watch_expiration = "1750000000000"
    db_session.commit()
    body = client.get("/api/v1/status").json()
    assert body["ingest"]["mode"] == "push"
    assert body["ingest"]["watch_expiration"] == "1750000000000"
    assert "pubsub_status" in body["ingest"]
    assert "last_notification_at" in body["ingest"]


def test_oauth_start_requests_pubsub_scope_in_push_mode(auth_client, db_session, connected):
    settings_service.set_setting(db_session, "gmail_ingest_mode", "push")
    db_session.commit()
    resp = auth_client.post("/api/v1/gmail/oauth/start", json={})
    assert resp.status_code == 200
    q = urllib.parse.parse_qs(urllib.parse.urlparse(resp.json()["auth_url"]).query)
    assert PUBSUB_SCOPE in q["scope"][0]


def test_oauth_start_modify_only_in_poll_mode(auth_client, db_session, connected):
    resp = auth_client.post("/api/v1/gmail/oauth/start", json={})
    q = urllib.parse.parse_qs(urllib.parse.urlparse(resp.json()["auth_url"]).query)
    assert q["scope"] == [MODIFY_SCOPE]  # default poll mode: no pubsub scope


# ── Review fixes: robustness + validation ────────────────────────────────────

@respx.mock
async def test_maybe_manage_watch_swallows_watch_error(connected, db_session, monkeypatch):
    """A watch failure (e.g. topic IAM not yet granted) must never raise — it
    cannot be allowed to fail the catch-up poll cycle that already ingested mail."""
    from app.models import GmailAuth
    from app.services import poller
    monkeypatch.setattr(gmail, "BACKOFF_BASE_SECONDS", 0.001)
    settings_service.set_setting(db_session, "gmail_pubsub_topic", "projects/p/topics/t")
    db_session.commit()
    respx.post(f"{gmail.GMAIL_API}/watch").respond(500, json={"error": "iam"})
    await poller._maybe_manage_watch(db_session, push=True)  # must not raise
    db_session.expire_all()
    assert db_session.query(GmailAuth).one().watch_expiration is None


@respx.mock
async def test_disconnect_stops_active_watch(auth_client, db_session, connected):
    connected.watch_expiration = "1750000000000"
    db_session.commit()
    respx.post(gmail.GOOGLE_REVOKE_URL).respond(200)
    stop = respx.post(f"{gmail.GMAIL_API}/stop").respond(200, json={})
    resp = auth_client.delete("/api/v1/gmail/auth")
    assert resp.status_code == 200
    assert stop.called  # watch torn down before the token is revoked


def test_put_settings_rejects_invalid_ingest_mode(auth_client):
    resp = auth_client.put("/api/v1/settings", json={"gmail_ingest_mode": "bogus"})
    assert resp.status_code == 400


def test_put_settings_accepts_valid_ingest_mode(auth_client):
    resp = auth_client.put("/api/v1/settings", json={"gmail_ingest_mode": "push"})
    assert resp.status_code == 200
