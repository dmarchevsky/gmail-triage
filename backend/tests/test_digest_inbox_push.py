"""In-app digest inbox, persisted digest content, delivery channels, Web Push."""

import json
import time

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import select

from app.models import DigestRun, Email, PushSubscription, Setting
from app.services import webpush
from app.services.webpush import b64url_decode, b64url_encode
from tests import test_m5_digests
from tests.test_m5_digests import TG_SEND, tg_ok

digest_setup = test_m5_digests.digest_setup  # re-export the fixture for this module

PUSH_URL = "https://fcm.googleapis.com/fcm/send/device-1"


# ── helpers ──────────────────────────────────────────────────────────────────

class FakeBrowser:
    """A push subscriber: holds the user-agent key pair + auth secret and can
    decrypt aes128gcm bodies the way a browser does."""

    def __init__(self, endpoint: str = PUSH_URL):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = b"0123456789abcdef"
        self.endpoint = endpoint

    @property
    def p256dh(self) -> str:
        return b64url_encode(self.key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))

    def subscription(self) -> dict:
        return {"endpoint": self.endpoint,
                "keys": {"p256dh": self.p256dh, "auth": b64url_encode(self.auth)}}

    def decrypt(self, body: bytes) -> dict:
        salt, idlen = body[:16], body[20]
        as_public, ciphertext = body[21:21 + idlen], body[21 + idlen:]
        sender = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public)
        ecdh = self.key.exchange(ec.ECDH(), sender)
        ua_public = b64url_decode(self.p256dh)
        ikm = HKDF(hashes.SHA256(), 32, salt=self.auth,
                   info=b"WebPush: info\x00" + ua_public + as_public).derive(ecdh)
        cek = HKDF(hashes.SHA256(), 16, salt=salt,
                   info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
        nonce = HKDF(hashes.SHA256(), 12, salt=salt,
                     info=b"Content-Encoding: nonce\x00").derive(ikm)
        plain = AESGCM(cek).decrypt(nonce, ciphertext, None)
        assert plain.endswith(b"\x02")
        return json.loads(plain[:-1])


def _set_channels(auth_client, digest: dict, telegram: bool, inapp: bool) -> dict:
    body = {k: v for k, v in digest.items() if k not in ("id", "last_run")}
    body.update(notify_telegram=telegram, notify_inapp=inapp)
    resp = auth_client.put(f"/api/v1/digests/{digest['id']}", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _run_now(auth_client, digest_id: int, preview: bool = False) -> dict:
    return auth_client.post(f"/api/v1/digests/{digest_id}/run-now",
                            json={"preview": preview}).json()


# ── RFC 8291 / VAPID primitives ──────────────────────────────────────────────

def test_encrypt_matches_rfc8291_appendix_a():
    as_key = ec.derive_private_key(int.from_bytes(
        b64url_decode("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1())
    body = webpush.encrypt(
        b"When I grow up, I want to be a watermelon",
        "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
        "BTBZMqHH6r4Tts7J_aSIgg",
        sender_key=as_key, salt=b64url_decode("DGv6ra1nlYgDCS1FRnbzlw"))
    assert b64url_encode(body) == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYW"
        "AmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgS"
        "xsj_Qulcy4a-fN")


def test_encrypt_round_trips_with_browser_decrypt():
    browser = FakeBrowser()
    sub = browser.subscription()
    body = webpush.encrypt(b'{"hello": "world"}', sub["keys"]["p256dh"], sub["keys"]["auth"])
    assert browser.decrypt(body) == {"hello": "world"}


def test_vapid_jwt_verifies_with_public_key():
    key = ec.generate_private_key(ec.SECP256R1())
    header = webpush.vapid_authorization(PUSH_URL, key, "mailto:me@example.com",
                                         now=time.time())
    assert header.startswith("vapid t=")
    token = header.split("t=")[1].split(",")[0]
    k = header.split("k=")[1]
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64url_decode(k))
    claims = jwt.decode(token, pub, algorithms=["ES256"],
                        audience="https://fcm.googleapis.com")
    assert claims["sub"] == "mailto:me@example.com"
    assert claims["exp"] > time.time()


@pytest.mark.parametrize("endpoint", [
    "http://fcm.googleapis.com/fcm/send/x",       # not https
    "https://evil.example.com/push",               # unknown host
    "https://fcm.googleapis.com.evil.com/x",       # suffix trick
    "https://127.0.0.1/x",
    "https://fcm.googleapis.com:8443/x",           # odd port
])
def test_endpoint_allowlist_rejects(endpoint):
    with pytest.raises(ValueError):
        webpush.validate_endpoint(endpoint)


@pytest.mark.parametrize("endpoint", [
    "https://fcm.googleapis.com/fcm/send/abc",
    "https://updates.push.services.mozilla.com/wpush/v2/abc",
    "https://web.push.apple.com/QK1abc",
    "https://wns2-par02p.notify.windows.com/w/?token=abc",
])
def test_endpoint_allowlist_accepts(endpoint):
    webpush.validate_endpoint(endpoint)


# ── push API ─────────────────────────────────────────────────────────────────

def test_vapid_key_generated_once_and_secret_hidden(auth_client, db_session):
    k1 = auth_client.get("/api/v1/push/vapid-public-key").json()["public_key"]
    k2 = auth_client.get("/api/v1/push/vapid-public-key").json()["public_key"]
    assert k1 == k2 and len(b64url_decode(k1)) == 65
    row = db_session.get(Setting, webpush.VAPID_KEY_SETTING)
    assert row is not None and "PRIVATE KEY" not in row.value  # Fernet-encrypted at rest
    settings = auth_client.get("/api/v1/settings").json()
    assert not any(k.startswith("webpush") for k in settings)
    resp = auth_client.put("/api/v1/settings",
                           json={webpush.VAPID_KEY_SETTING: "x"})
    assert resp.status_code == 400


def test_subscribe_upserts_and_validates(auth_client, db_session):
    browser = FakeBrowser()
    r1 = auth_client.post("/api/v1/push/subscriptions", json=browser.subscription())
    r2 = auth_client.post("/api/v1/push/subscriptions", json=browser.subscription())
    assert r1.json()["created"] is True and r2.json()["created"] is False
    assert len(db_session.scalars(select(PushSubscription)).all()) == 1

    bad = FakeBrowser("https://evil.example.com/x").subscription()
    assert auth_client.post("/api/v1/push/subscriptions", json=bad).status_code == 400
    garbage = {**browser.subscription(), "keys": {"p256dh": "AAAA", "auth": "AAAA"}}
    assert auth_client.post("/api/v1/push/subscriptions", json=garbage).status_code == 400

    r = auth_client.request("DELETE", "/api/v1/push/subscriptions",
                            json={"endpoint": browser.endpoint})
    assert r.json() == {"deleted": True}
    assert db_session.scalars(select(PushSubscription)).all() == []


def test_push_endpoints_require_auth(client):
    assert client.get("/api/v1/push/vapid-public-key").status_code == 401


@respx.mock
def test_push_test_sends_encrypted_vapid_request(auth_client):
    browser = FakeBrowser()
    auth_client.post("/api/v1/push/subscriptions", json=browser.subscription())
    route = respx.post(PUSH_URL).mock(return_value=httpx.Response(201))
    result = auth_client.post("/api/v1/push/test").json()
    assert result == {"sent": 1, "removed": 0, "errors": []}
    req = route.calls[0].request
    assert req.headers["Content-Encoding"] == "aes128gcm"
    assert req.headers["TTL"] == "86400"
    assert req.headers["Authorization"].startswith("vapid t=")
    assert browser.decrypt(req.content)["title"] == "MailTriage"


@respx.mock
def test_gone_subscription_is_removed(auth_client, db_session):
    browser = FakeBrowser()
    auth_client.post("/api/v1/push/subscriptions", json=browser.subscription())
    respx.post(PUSH_URL).mock(return_value=httpx.Response(410))
    result = auth_client.post("/api/v1/push/test").json()
    assert result["removed"] == 1
    assert db_session.scalars(select(PushSubscription)).all() == []


@respx.mock
def test_failing_subscription_dropped_after_max_failures(auth_client, db_session):
    browser = FakeBrowser()
    auth_client.post("/api/v1/push/subscriptions", json=browser.subscription())
    respx.post(PUSH_URL).mock(return_value=httpx.Response(500))
    for _ in range(webpush.MAX_FAILURES - 1):
        assert auth_client.post("/api/v1/push/test").json()["errors"]
    db_session.expire_all()
    assert db_session.scalars(select(PushSubscription)).one().failure_count == \
        webpush.MAX_FAILURES - 1
    assert auth_client.post("/api/v1/push/test").json()["removed"] == 1


# ── digest content snapshot ──────────────────────────────────────────────────

@respx.mock
def test_assemble_run_stores_content_snapshot(auth_client, db_session, digest_setup):
    respx.post(TG_SEND).mock(return_value=tg_ok())
    run = _run_now(auth_client, digest_setup["digest"]["id"])
    assert run["status"] == "success"
    content = run["content"]
    assert content["mode"] == "assemble" and content["email_count"] == 2
    subjects = {i["subject"] for i in content["items"]}
    assert subjects == {"S&P up", "Bonds <down>"}
    item = next(i for i in content["items"] if i["subject"] == "S&P up")
    assert item["summary"] == "S&P summary"
    assert item["category"] == "MarketNews"
    assert item["gmail_url"].endswith("d1")
    assert content["headline"]  # subjects preview for the feed / push body

    # The snapshot survives retention deleting the source emails.
    for e in db_session.scalars(select(Email)):
        db_session.delete(e)
    db_session.commit()
    detail = auth_client.get(f"/api/v1/digests/runs/{run['id']}").json()
    assert detail["content"]["email_count"] == 2
    assert detail["digest_name"] == "Market news"


@respx.mock
def test_synthesize_and_preview_store_headline_and_body(auth_client, db_session,
                                                        digest_setup):
    from tests.test_m2_classification import CHAT_URL, llm_response
    from tests.test_m5_digests import _set_digest_mode

    _set_digest_mode(db_session, digest_setup["digest"]["id"], "synthesize")
    respx.post(CHAT_URL).mock(return_value=llm_response("Markets moved.\nStocks up, bonds down."))
    run = _run_now(auth_client, digest_setup["digest"]["id"], preview=True)
    assert run["status"] == "dry_run"
    assert run["content"]["headline"] == "Markets moved."
    assert run["content"]["body"] == "Stocks up, bonds down."
    assert run["notified_inapp"] is False


def test_empty_run_stores_empty_content(auth_client, db_session, digest_setup):
    for e in db_session.scalars(select(Email)):
        db_session.delete(e)
    db_session.commit()
    run = _run_now(auth_client, digest_setup["digest"]["id"])
    assert run["status"] == "empty"
    assert run["content"]["items"] == [] and run["content"]["email_count"] == 0


# ── delivery channels ────────────────────────────────────────────────────────

def test_digest_defaults_to_telegram_only(digest_setup):
    d = digest_setup["digest"]
    assert d["notify_telegram"] is True and d["notify_inapp"] is False


def test_digest_requires_a_channel(auth_client, digest_setup):
    d = digest_setup["digest"]
    body = {k: v for k, v in d.items() if k not in ("id", "last_run")}
    body.update(notify_telegram=False, notify_inapp=False)
    assert auth_client.put(f"/api/v1/digests/{d['id']}", json=body).status_code == 422


@respx.mock
def test_inapp_only_skips_telegram_and_pushes(auth_client, digest_setup):
    browser = FakeBrowser()
    auth_client.post("/api/v1/push/subscriptions", json=browser.subscription())
    d = _set_channels(auth_client, digest_setup["digest"], telegram=False, inapp=True)
    tg = respx.post(TG_SEND).mock(return_value=tg_ok())
    push = respx.post(PUSH_URL).mock(return_value=httpx.Response(201))

    run = _run_now(auth_client, d["id"])
    assert run["status"] == "success" and run["notified_inapp"] is True
    assert run["read_at"] is None
    assert tg.call_count == 0
    payload = browser.decrypt(push.calls[0].request.content)
    assert payload["title"] == "Market news"
    assert payload["url"] == f"/digests/runs/{run['id']}"
    assert payload["tag"] == f"digest-{d['id']}"


@respx.mock
def test_both_channels_deliver(auth_client, digest_setup):
    auth_client.post("/api/v1/push/subscriptions", json=FakeBrowser().subscription())
    d = _set_channels(auth_client, digest_setup["digest"], telegram=True, inapp=True)
    tg = respx.post(TG_SEND).mock(return_value=tg_ok())
    push = respx.post(PUSH_URL).mock(return_value=httpx.Response(201))
    run = _run_now(auth_client, d["id"])
    assert run["status"] == "success" and run["error"] is None
    assert tg.call_count == 1 and push.call_count == 1
    assert run["telegram_message_id"] == "42"


@respx.mock
def test_inapp_without_push_subscriptions_still_delivers(auth_client, digest_setup):
    d = _set_channels(auth_client, digest_setup["digest"], telegram=False, inapp=True)
    run = _run_now(auth_client, d["id"])
    assert run["status"] == "success" and run["notified_inapp"] is True


@respx.mock
def test_telegram_failure_with_inapp_is_success_with_error(auth_client, digest_setup,
                                                           monkeypatch):
    import app.services.telegram as tg_mod

    async def no_sleep(_):
        return None
    monkeypatch.setattr(tg_mod.asyncio, "sleep", no_sleep)
    d = _set_channels(auth_client, digest_setup["digest"], telegram=True, inapp=True)
    respx.post(TG_SEND).mock(return_value=httpx.Response(500, text="boom"))
    run = _run_now(auth_client, d["id"])
    assert run["status"] == "success"
    assert run["notified_inapp"] is True
    assert run["error"].startswith("telegram:")
    # Watermark advanced: the next run has nothing new.
    assert _run_now(auth_client, d["id"])["status"] == "empty"


@respx.mock
def test_telegram_only_failure_still_errors(auth_client, digest_setup, monkeypatch):
    import app.services.telegram as tg_mod

    async def no_sleep(_):
        return None
    monkeypatch.setattr(tg_mod.asyncio, "sleep", no_sleep)
    respx.post(TG_SEND).mock(return_value=httpx.Response(500, text="boom"))
    run = _run_now(auth_client, digest_setup["digest"]["id"])
    assert run["status"] == "error" and run["notified_inapp"] is False


@respx.mock
def test_preview_is_never_delivered(auth_client, digest_setup):
    d = _set_channels(auth_client, digest_setup["digest"], telegram=True, inapp=True)
    tg = respx.post(TG_SEND).mock(return_value=tg_ok())
    run = _run_now(auth_client, d["id"], preview=True)
    assert run["status"] == "dry_run" and run["notified_inapp"] is False
    assert tg.call_count == 0


@respx.mock
def test_no_news_inapp(auth_client, db_session, digest_setup):
    d = digest_setup["digest"]
    body = {k: v for k, v in d.items() if k not in ("id", "last_run")}
    body.update(notify_telegram=False, notify_inapp=True, send_no_news=True)
    auth_client.put(f"/api/v1/digests/{d['id']}", json=body)
    for e in db_session.scalars(select(Email)):
        db_session.delete(e)
    db_session.commit()
    run = _run_now(auth_client, d["id"])
    assert run["status"] == "empty" and run["notified_inapp"] is True


# ── inbox feed / read state ──────────────────────────────────────────────────

@respx.mock
def test_feed_unread_count_and_mark_read(auth_client, db_session, digest_setup):
    d = _set_channels(auth_client, digest_setup["digest"], telegram=False, inapp=True)
    run = _run_now(auth_client, d["id"])

    feed = auth_client.get("/api/v1/digests/runs").json()
    assert [f["id"] for f in feed] == [run["id"]]
    assert feed[0]["digest_name"] == "Market news" and feed[0]["email_count"] == 2
    assert auth_client.get("/api/v1/digests/runs/unread-count").json() == {"unread": 1}
    assert len(auth_client.get("/api/v1/digests/runs?unread=true").json()) == 1

    marked = auth_client.post(f"/api/v1/digests/runs/{run['id']}/read").json()
    assert marked["read_at"] is not None
    assert auth_client.get("/api/v1/digests/runs/unread-count").json() == {"unread": 0}
    assert auth_client.get("/api/v1/digests/runs?unread=true").json() == []


def test_mark_all_read_and_pagination(auth_client, db_session, digest_setup):
    did = digest_setup["digest"]["id"]
    for _ in range(3):
        db_session.add(DigestRun(digest_id=did, status="success", notified_inapp=True,
                                 email_ids=[]))
    db_session.add(DigestRun(digest_id=did, status="error", email_ids=[]))
    db_session.commit()

    feed = auth_client.get("/api/v1/digests/runs").json()
    assert len(feed) == 3  # errored run hidden by default
    assert len(auth_client.get("/api/v1/digests/runs?include_all=true").json()) == 4
    page2 = auth_client.get(f"/api/v1/digests/runs?limit=2&before_id={feed[1]['id']}").json()
    assert [f["id"] for f in page2] == [feed[2]["id"]]

    assert auth_client.post("/api/v1/digests/runs/read-all").json() == {"updated": 3}
    assert auth_client.get("/api/v1/digests/runs/unread-count").json() == {"unread": 0}


def test_run_routes_404(auth_client):
    assert auth_client.get("/api/v1/digests/runs/999").status_code == 404
    assert auth_client.post("/api/v1/digests/runs/999/read").status_code == 404
