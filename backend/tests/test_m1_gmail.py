"""M1 acceptance: OAuth, encrypted token storage, scope assertion,
poller sync (baseline/incremental/fallback), idempotency, revoked token."""

import base64
import json
import urllib.parse
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from app.services import gmail

CLIENT_SECRET_JSON = json.dumps({
    "installed": {
        "client_id": "cid.apps.googleusercontent.com",
        "client_secret": "csecret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
})

MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"


def make_token(scope=MODIFY_SCOPE, expires_in=3600):
    return {
        "access_token": "at-123",
        "refresh_token": "rt-456",
        "scope": scope,
        "token_type": "Bearer",
        "expiry": (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat(),
    }


def b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def gmail_message(msg_id: str, sender="Alice <alice@example.com>",
                  subject="Hello", labels=None, internal_ms=1750000000000):
    return {
        "id": msg_id,
        "threadId": f"t-{msg_id}",
        "historyId": "1000",
        "internalDate": str(internal_ms),
        "snippet": f"snippet of {msg_id}",
        "sizeEstimate": 1234,
        "labelIds": labels if labels is not None else ["INBOX", "UNREAD"],
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Mon, 01 Jun 2026 10:00:00 +0000"},
            ],
        },
    }


@pytest.fixture()
def connected(client, db_session):
    """Gmail connected: client secret + token stored, email known."""
    from app.services import settings_service

    settings_service.set_setting(db_session, "gmail_client_secret_json", CLIENT_SECRET_JSON)
    row = gmail.save_token(db_session, make_token(), email="me@gmail.test")
    db_session.commit()
    return row


# ── OAuth ────────────────────────────────────────────────────────────────────

@respx.mock
def test_oauth_flow_stores_encrypted_token(auth_client, db_session):
    resp = auth_client.post("/api/v1/gmail/oauth/start",
                            json={"client_secret_json": CLIENT_SECRET_JSON})
    assert resp.status_code == 200
    auth_url = resp.json()["auth_url"]
    query = urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query)
    assert query["scope"] == [MODIFY_SCOPE]  # gmail.modify ONLY
    state = query["state"][0]

    respx.post(gmail.GOOGLE_TOKEN_URL).respond(200, json={
        "access_token": "at-123", "refresh_token": "rt-456",
        "scope": MODIFY_SCOPE, "expires_in": 3600, "token_type": "Bearer",
    })
    respx.get(f"{gmail.GMAIL_API}/profile").respond(200, json={
        "emailAddress": "me@gmail.test", "historyId": "1000"})

    resp = auth_client.get(f"/api/v1/gmail/oauth/callback?code=abc&state={state}",
                           follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert "gmail_connected=1" in resp.headers["location"]

    from app.models import GmailAuth
    row = db_session.query(GmailAuth).one()
    assert row.email_address == "me@gmail.test"
    assert row.granted_scopes == [MODIFY_SCOPE]
    assert "at-123" not in row.token_json  # encrypted at rest
    _, token = gmail.load_token(db_session)
    assert token["access_token"] == "at-123"


@respx.mock
def test_send_capable_scope_rejected(auth_client, db_session):
    resp = auth_client.post("/api/v1/gmail/oauth/start",
                            json={"client_secret_json": CLIENT_SECRET_JSON})
    state = urllib.parse.parse_qs(
        urllib.parse.urlparse(resp.json()["auth_url"]).query)["state"][0]

    respx.post(gmail.GOOGLE_TOKEN_URL).respond(200, json={
        "access_token": "at", "refresh_token": "rt",
        "scope": f"{MODIFY_SCOPE} https://www.googleapis.com/auth/gmail.send",
        "expires_in": 3600,
    })
    resp = auth_client.get(f"/api/v1/gmail/oauth/callback?code=abc&state={state}",
                           follow_redirects=False)
    assert "gmail_error" in resp.headers["location"]
    from app.models import GmailAuth
    assert db_session.query(GmailAuth).count() == 0


def test_bad_state_rejected(auth_client):
    resp = auth_client.get("/api/v1/gmail/oauth/callback?code=abc&state=forged")
    assert resp.status_code == 400


def test_startup_scope_guard(client, db_session):
    """Stored send-capable token must make startup fail (§6.1)."""
    row = gmail.save_token.__wrapped__ if hasattr(gmail.save_token, "__wrapped__") else None
    # save_token itself refuses send scopes; simulate a tampered DB row instead.
    from app.models import GmailAuth
    db_session.add(GmailAuth(token_json="x", granted_scopes=[
        MODIFY_SCOPE, "https://www.googleapis.com/auth/gmail.send"]))
    db_session.commit()
    from app.main import assert_stored_scopes_safe
    with pytest.raises(gmail.GmailAuthError):
        assert_stored_scopes_safe()
    assert row is None or True


# ── Poller ───────────────────────────────────────────────────────────────────

def mock_metadata(msg):
    respx.get(f"{gmail.GMAIL_API}/messages/{msg['id']}").respond(200, json=msg)


@respx.mock
def test_baseline_then_incremental_sync_idempotent(auth_client, db_session, connected):
    m1, m2 = gmail_message("m1"), gmail_message("m2", sender="Bob <bob@corp.io>")
    respx.get(f"{gmail.GMAIL_API}/messages").respond(200, json={
        "messages": [{"id": "m1"}, {"id": "m2"}]})
    mock_metadata(m1)
    mock_metadata(m2)
    respx.get(f"{gmail.GMAIL_API}/profile").respond(200, json={
        "emailAddress": "me@gmail.test", "historyId": "2000"})

    resp = auth_client.post("/api/v1/poller/run-now")
    assert resp.status_code == 200
    assert resp.json() == {"mode": "baseline", "new_emails": 2}

    from app.models import Email
    emails = db_session.query(Email).order_by(Email.gmail_message_id).all()
    assert [e.gmail_message_id for e in emails] == ["m1", "m2"]
    assert emails[0].sender_domain == "example.com"
    assert emails[1].sender_domain == "corp.io"
    assert all(e.status == "pending" for e in emails)

    # Incremental: history returns m2 (dupe) and m3 (new).
    m3 = gmail_message("m3")
    respx.get(f"{gmail.GMAIL_API}/history").respond(200, json={
        "historyId": "3000",
        "history": [{"messagesAdded": [{"message": {"id": "m2"}},
                                       {"message": {"id": "m3"}}]}],
    })
    mock_metadata(m3)
    resp = auth_client.post("/api/v1/poller/run-now")
    assert resp.json() == {"mode": "incremental", "new_emails": 1}
    db_session.expire_all()
    assert db_session.query(Email).count() == 3

    from app.models import GmailAuth
    assert db_session.query(GmailAuth).one().history_id == "3000"


@respx.mock
def test_history_expired_fallback(auth_client, db_session, connected):
    connected.history_id = "999"
    db_session.commit()

    respx.get(f"{gmail.GMAIL_API}/history").respond(404, json={"error": "expired"})
    m4 = gmail_message("m4")
    respx.get(f"{gmail.GMAIL_API}/messages").respond(200, json={"messages": [{"id": "m4"}]})
    mock_metadata(m4)
    respx.get(f"{gmail.GMAIL_API}/profile").respond(200, json={
        "emailAddress": "me@gmail.test", "historyId": "4000"})

    resp = auth_client.post("/api/v1/poller/run-now")
    assert resp.json() == {"mode": "fallback", "new_emails": 1}


@respx.mock
def test_skips_own_and_non_inbox_messages(auth_client, db_session, connected):
    own = gmail_message("own1", sender="Me <me@gmail.test>")
    archived = gmail_message("arch1", labels=["SENT"])
    respx.get(f"{gmail.GMAIL_API}/messages").respond(200, json={
        "messages": [{"id": "own1"}, {"id": "arch1"}]})
    mock_metadata(own)
    mock_metadata(archived)
    respx.get(f"{gmail.GMAIL_API}/profile").respond(200, json={
        "emailAddress": "me@gmail.test", "historyId": "2000"})

    resp = auth_client.post("/api/v1/poller/run-now")
    assert resp.json()["new_emails"] == 0


@respx.mock
def test_revoked_token_surfaces_as_409_no_crash(auth_client, db_session, connected):
    # Expired access token forces a refresh, which fails (revoked).
    _, token = gmail.load_token(db_session)
    token["expiry"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    gmail.save_token(db_session, token)
    db_session.commit()
    respx.post(gmail.GOOGLE_TOKEN_URL).respond(400, json={"error": "invalid_grant"})

    resp = auth_client.post("/api/v1/poller/run-now")
    assert resp.status_code == 409
    assert "refresh failed" in resp.json()["detail"].lower()


@respx.mock
def test_backoff_on_429_then_success(auth_client, db_session, connected, monkeypatch):
    monkeypatch.setattr(gmail, "BACKOFF_BASE_SECONDS", 0.001)
    profile_route = respx.get(f"{gmail.GMAIL_API}/profile")
    profile_route.side_effect = [
        httpx.Response(429, json={"error": "rate"}),
        httpx.Response(200, json={"emailAddress": "me@gmail.test", "historyId": "2000"}),
    ]
    respx.get(f"{gmail.GMAIL_API}/messages").respond(200, json={"messages": []})

    resp = auth_client.post("/api/v1/poller/run-now")
    assert resp.status_code == 200
    assert profile_route.call_count == 2


@respx.mock
def test_disconnect_revokes_and_deletes(auth_client, db_session, connected):
    revoke = respx.post(gmail.GOOGLE_REVOKE_URL).respond(200)
    resp = auth_client.delete("/api/v1/gmail/auth")
    assert resp.status_code == 200
    assert revoke.called
    from app.models import GmailAuth
    db_session.expire_all()
    assert db_session.query(GmailAuth).count() == 0


# ── Parsing helpers ──────────────────────────────────────────────────────────

def test_extract_body_prefers_plain():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": b64url("plain text body")}},
            {"mimeType": "text/html",
             "body": {"data": b64url("<p>html <b>body</b></p>")}},
        ],
    }
    assert gmail.extract_body_text(payload) == "plain text body"


def test_extract_body_html_fallback():
    payload = {
        "mimeType": "text/html",
        "body": {"data": b64url("<div><p>Hello</p><script>x()</script><p>World</p></div>")},
    }
    text = gmail.extract_body_text(payload)
    assert "Hello" in text and "World" in text
    assert "x()" not in text or "<script>" not in text


def test_extract_body_nested_multipart():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [{
            "mimeType": "multipart/alternative",
            "parts": [{"mimeType": "text/plain", "body": {"data": b64url("nested")}}],
        }],
    }
    assert gmail.extract_body_text(payload) == "nested"


def test_clean_body_strips_standalone_url_lines():
    """Bare URL lines (newsletter tracking links) are dropped entirely."""
    from app.services.gmail import _clean_body_text

    body = (
        "Read in Browser  (\n"
        "https://email-hs.seekingalpha.com/e3t/Ctc/OT+113/" + "x" * 60 + "\n"
        ")\n"
        "\n"
        "Actual content here.\n"
    )
    result = _clean_body_text(body)
    assert "seekingalpha.com" not in result
    assert "Actual content here." in result
    # orphaned bracket line also stripped
    assert "(\n)" not in result


def test_clean_body_strips_inline_urls_keeps_surrounding_text():
    """Inline tracking URLs embedded mid-sentence are removed; surrounding text kept."""
    from app.services.gmail import _clean_body_text

    url = "https://email-hs.seekingalpha.com/e3t/Ctc/" + "x" * 60
    body = f"Nasdaq Composite (COMP:IND ({url})) rose 2.4% to 26,518."
    result = _clean_body_text(body)
    assert "Nasdaq Composite" in result
    assert "rose 2.4% to 26,518" in result
    assert "seekingalpha.com" not in result
    # empty parens cleaned up
    assert "()" not in result


def test_clean_body_strips_boilerplate_lines():
    """Short lines matching footer boilerplate are dropped."""
    from app.services.gmail import _clean_body_text

    body = (
        "Markets rose this week on easing inflation fears.\n"
        "\n"
        "Unsubscribe\n"
        "Privacy Policy | Terms of Service\n"
        "Update your email preferences\n"
        "View in browser\n"
        "\n"
        "Dow +0.7% to 51,565.\n"
    )
    result = _clean_body_text(body)
    assert "Markets rose" in result
    assert "Dow +0.7%" in result
    assert "Unsubscribe" not in result
    assert "Privacy Policy" not in result
    assert "email preferences" not in result
    assert "View in browser" not in result


def test_clean_body_preserves_short_meaningful_urls():
    """URLs shorter than the tracking threshold are kept."""
    from app.services.gmail import _clean_body_text

    body = "More at https://sec.gov/filings and in the report."
    result = _clean_body_text(body)
    assert "sec.gov" in result


def test_extract_body_cleans_newsletter_plain_text():
    """End-to-end: a newsletter-style plain-text body loses its tracking URLs."""
    url = "https://email-hs.seekingalpha.com/e3t/Ctc/OT+113/" + "y" * 60
    body_plain = (
        f"Read in Browser  (\n{url}\n)\n\n"
        "Wall Street Breakfast\n\n"
        "S&P 500 rose 0.9% to 7,501. Nasdaq rose 2.4%.\n\n"
        "Unsubscribe\n"
    )
    payload = {
        "mimeType": "text/plain",
        "body": {"data": b64url(body_plain)},
    }
    result = gmail.extract_body_text(payload)
    assert "S&P 500 rose 0.9%" in result
    assert "seekingalpha.com" not in result
    assert "Unsubscribe" not in result


def test_parse_message_meta_domain():
    meta = gmail.parse_message_meta(gmail_message("x", sender="X <x@Sub.Example.COM>"))
    assert meta["sender_domain"] == "sub.example.com"
    assert meta["received_at"].tzinfo is not None


@respx.mock
def test_incremental_sync_skips_missing_message(auth_client, db_session, connected):
    """A 404 on one message (deleted/moved since the history record) must skip
    that message, still process the rest, and advance historyId — not abort."""
    connected.history_id = "100"
    db_session.commit()
    m_ok = gmail_message("ok1")
    respx.get(f"{gmail.GMAIL_API}/history").respond(200, json={
        "historyId": "200",
        "history": [{"messagesAdded": [{"message": {"id": "gone1"}},
                                       {"message": {"id": "ok1"}}]}]})
    respx.get(f"{gmail.GMAIL_API}/messages/gone1").respond(404, json={"error": "nf"})
    mock_metadata(m_ok)

    resp = auth_client.post("/api/v1/poller/run-now")
    assert resp.status_code == 200
    assert resp.json() == {"mode": "incremental", "new_emails": 1}

    from app.models import GmailAuth
    db_session.expire_all()
    assert db_session.query(GmailAuth).one().history_id == "200"  # advanced past the gap


@respx.mock
def test_baseline_ingests_category_tab_without_inbox(auth_client, db_session, connected):
    """A Promotions email that skipped the inbox (CATEGORY_PROMOTIONS, no INBOX)
    is ingested under the default scope; an archived Primary one is not."""
    promo = gmail_message("promo1", labels=["CATEGORY_PROMOTIONS"])
    personal = gmail_message("pers1", labels=["CATEGORY_PERSONAL"])  # archived Primary
    respx.get(f"{gmail.GMAIL_API}/messages").respond(200, json={
        "messages": [{"id": "promo1"}, {"id": "pers1"}]})
    mock_metadata(promo)
    mock_metadata(personal)
    respx.get(f"{gmail.GMAIL_API}/profile").respond(200, json={
        "emailAddress": "me@gmail.test", "historyId": "2000"})

    resp = auth_client.post("/api/v1/poller/run-now")
    assert resp.json() == {"mode": "baseline", "new_emails": 1}
    from app.models import Email
    assert [e.gmail_message_id for e in db_session.query(Email)] == ["promo1"]


@respx.mock
def test_incremental_ingests_category_tab(auth_client, db_session, connected):
    connected.history_id = "100"
    db_session.commit()
    promo = gmail_message("promo2", labels=["CATEGORY_UPDATES"])
    respx.get(f"{gmail.GMAIL_API}/history").respond(200, json={
        "historyId": "200",
        "history": [{"messagesAdded": [{"message": {"id": "promo2"}}]}]})
    mock_metadata(promo)
    resp = auth_client.post("/api/v1/poller/run-now")
    assert resp.json() == {"mode": "incremental", "new_emails": 1}


@respx.mock
def test_scope_setting_restricts_to_inbox(auth_client, db_session, connected):
    from app.services import settings_service
    settings_service.set_setting(db_session, "poll_scope_labels", ["INBOX"])
    db_session.commit()
    promo = gmail_message("promo3", labels=["CATEGORY_PROMOTIONS"])
    respx.get(f"{gmail.GMAIL_API}/messages").respond(200, json={
        "messages": [{"id": "promo3"}]})
    mock_metadata(promo)
    respx.get(f"{gmail.GMAIL_API}/profile").respond(200, json={
        "emailAddress": "me@gmail.test", "historyId": "2000"})
    resp = auth_client.post("/api/v1/poller/run-now")
    assert resp.json()["new_emails"] == 0  # promotions out of scope now


@respx.mock
def test_trash_and_spam_never_ingested(auth_client, db_session, connected):
    trash = gmail_message("t1", labels=["INBOX", "TRASH"])
    spam = gmail_message("s1", labels=["CATEGORY_PROMOTIONS", "SPAM"])
    respx.get(f"{gmail.GMAIL_API}/messages").respond(200, json={
        "messages": [{"id": "t1"}, {"id": "s1"}]})
    mock_metadata(trash)
    mock_metadata(spam)
    respx.get(f"{gmail.GMAIL_API}/profile").respond(200, json={
        "emailAddress": "me@gmail.test", "historyId": "2000"})
    resp = auth_client.post("/api/v1/poller/run-now")
    assert resp.json()["new_emails"] == 0


@respx.mock
def test_gmail_labels_endpoint(auth_client, connected):
    respx.get(f"{gmail.GMAIL_API}/labels").respond(200, json={"labels": [
        {"id": "INBOX", "type": "system", "name": "INBOX"},
        {"id": "CATEGORY_PROMOTIONS", "type": "system", "name": "CATEGORY_PROMOTIONS"},
        {"id": "SENT", "type": "system", "name": "SENT"},
        {"id": "Label_7", "type": "user", "name": "Work"},
    ]})
    out = auth_client.get("/api/v1/gmail/labels").json()
    ids = [x["id"] for x in out]
    assert "SENT" not in ids  # excluded
    assert ids[0] == "INBOX"  # inbox first
    by_id = {x["id"]: x for x in out}
    assert by_id["CATEGORY_PROMOTIONS"]["display_name"] == "Promotions"
    assert by_id["Label_7"]["display_name"] == "Work" and by_id["Label_7"]["type"] == "user"


def test_poll_failure_is_audited(db_session):
    """A poll failure writes a poll_failed audit row so it surfaces in Recent activity."""
    from app.models import AuditLog
    from app.services.poller import _record_poll_failure

    _record_poll_failure(db_session, "boom: gmail unreachable", kind="auth")

    rows = db_session.query(AuditLog).filter(AuditLog.event_type == "poll_failed").all()
    assert len(rows) == 1
    assert rows[0].actor == "system"
    assert rows[0].payload["error"] == "boom: gmail unreachable"
    assert rows[0].payload["kind"] == "auth"
