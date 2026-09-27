"""Web Push delivery — hand-rolled over httpx + cryptography (no pywebpush).

Payloads are encrypted with RFC 8291 `aes128gcm` and authenticated with VAPID
(RFC 8292, ES256 JWT). The VAPID key pair is generated on first use and kept
Fernet-encrypted in the settings table. Subscription endpoints are restricted to
the browser vendors' push services so a stored subscription can never turn the
server into an SSRF proxy.
"""

import base64
import json
import os
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_config
from app.logging_setup import get_logger
from app.models import Digest, DigestRun, PushSubscription
from app.services import settings_service

log = get_logger(__name__)

VAPID_KEY_SETTING = "webpush_vapid_private_key"
# Push services of Chrome/Edge-on-Android (FCM), Firefox, Safari and Edge (WNS).
PUSH_HOSTS = {"fcm.googleapis.com"}
PUSH_HOST_SUFFIXES = (".push.services.mozilla.com", ".push.apple.com",
                      ".notify.windows.com")
RECORD_SIZE = 4096
TTL_SECONDS = 24 * 3600
VAPID_TTL_SECONDS = 12 * 3600
MAX_FAILURES = 5


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def validate_endpoint(endpoint: str) -> None:
    """Raise ValueError unless `endpoint` is an https URL on a known push service."""
    parts = urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.port not in (None, 443):
        raise ValueError("Push endpoint must be an https URL")
    if host not in PUSH_HOSTS and not host.endswith(PUSH_HOST_SUFFIXES):
        raise ValueError(f"Push endpoint host {host!r} is not a known push service")


def validate_keys(p256dh: str, auth: str) -> None:
    """Raise ValueError unless the subscription keys are a P-256 point + 16-byte secret."""
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64url_decode(p256dh))
        auth_ok = len(b64url_decode(auth)) == 16
    except (ValueError, TypeError) as e:
        raise ValueError("Invalid push subscription keys") from e
    if not auth_ok:
        raise ValueError("Invalid push subscription auth secret")


def _public_bytes(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.X962,
                                         serialization.PublicFormat.UncompressedPoint)


def vapid_private_key(session: Session) -> ec.EllipticCurvePrivateKey:
    """Load the VAPID key, generating and persisting it on first use."""
    pem = settings_service.get_setting(session, VAPID_KEY_SETTING)
    if not pem:
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        settings_service.set_setting(session, VAPID_KEY_SETTING, pem)
        try:
            session.commit()
        except IntegrityError:
            # A concurrent request created it first — use theirs.
            session.rollback()
            pem = settings_service.get_setting(session, VAPID_KEY_SETTING)
    return serialization.load_pem_private_key(pem.encode(), password=None)


def vapid_public_key(session: Session) -> str:
    """Base64url uncompressed public key — the browser's `applicationServerKey`."""
    return b64url_encode(_public_bytes(vapid_private_key(session)))


def vapid_subject(session: Session) -> str:
    """Contact URI for the push services (Apple rejects anything but mailto:/https:)."""
    base = (settings_service.get_setting(session, "public_base_url") or "").strip()
    if base.startswith("https://"):
        return base.rstrip("/")
    emails = [e.strip() for e in get_config().cf_access_allowed_emails.split(",") if e.strip()]
    return f"mailto:{emails[0]}" if emails else "mailto:mailtriage@example.com"


def vapid_authorization(endpoint: str, key: ec.EllipticCurvePrivateKey, subject: str,
                        now: float | None = None) -> str:
    parts = urlsplit(endpoint)
    claims = {"aud": f"{parts.scheme}://{parts.netloc}",
              "exp": int(now if now is not None else time.time()) + VAPID_TTL_SECONDS,
              "sub": subject}
    token = jwt.encode(claims, key, algorithm="ES256")
    return f"vapid t={token}, k={b64url_encode(_public_bytes(key))}"


def encrypt(payload: bytes, p256dh: str, auth: str, *,
            sender_key: ec.EllipticCurvePrivateKey | None = None,
            salt: bytes | None = None) -> bytes:
    """RFC 8291 aes128gcm body (single record). `sender_key`/`salt` are for tests."""
    ua_public = b64url_decode(p256dh)
    auth_secret = b64url_decode(auth)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    sender_key = sender_key or ec.generate_private_key(ec.SECP256R1())
    as_public = _public_bytes(sender_key)
    salt = salt or os.urandom(16)

    ecdh_secret = sender_key.exchange(ec.ECDH(), ua_key)
    ikm = HKDF(hashes.SHA256(), 32, salt=auth_secret,
               info=b"WebPush: info\x00" + ua_public + as_public).derive(ecdh_secret)
    cek = HKDF(hashes.SHA256(), 16, salt=salt,
               info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt=salt,
                 info=b"Content-Encoding: nonce\x00").derive(ikm)

    header = salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public)]) + as_public
    # 0x02 = padding delimiter of the last (only) record; +16 for the GCM tag.
    if len(header) + len(payload) + 1 + 16 > RECORD_SIZE:
        raise ValueError("Push payload too large")
    return header + AESGCM(cek).encrypt(nonce, payload + b"\x02", None)


async def notify_all(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Send `payload` to every subscription. Never raises for per-subscription
    failures: gone (404/410) subscriptions are deleted, others count failures and
    are dropped after MAX_FAILURES in a row. Returns {sent, removed, errors}."""
    subs = list(session.scalars(select(PushSubscription)))
    result: dict[str, Any] = {"sent": 0, "removed": 0, "errors": []}
    if not subs:
        return result
    key = vapid_private_key(session)
    subject = vapid_subject(session)
    body = json.dumps(payload).encode()

    async with httpx.AsyncClient(timeout=15) as client:
        for sub in subs:
            host = urlsplit(sub.endpoint).hostname or "?"
            try:
                validate_endpoint(sub.endpoint)
                resp = await client.post(sub.endpoint, content=encrypt(body, sub.p256dh, sub.auth),
                                         headers={
                                             "Authorization": vapid_authorization(
                                                 sub.endpoint, key, subject),
                                             "Content-Encoding": "aes128gcm",
                                             "Content-Type": "application/octet-stream",
                                             "TTL": str(TTL_SECONDS),
                                             "Urgency": "normal",
                                         })
            except ValueError as e:  # bad endpoint/keys: can never succeed
                log.warning("webpush_subscription_invalid", host=host, error=str(e))
                session.delete(sub)
                result["removed"] += 1
                continue
            except httpx.TransportError as e:
                status, detail = None, str(e)
            else:
                status, detail = resp.status_code, resp.text[:200]

            if status is not None and 200 <= status < 300:
                sub.last_success_at = datetime.now(UTC)
                sub.failure_count = 0
                result["sent"] += 1
            elif status in (404, 410):
                log.info("webpush_subscription_gone", host=host, status=status)
                session.delete(sub)
                result["removed"] += 1
            else:
                sub.failure_count = (sub.failure_count or 0) + 1
                result["errors"].append(f"{host}: {status or ''} {detail}".strip())
                log.warning("webpush_send_failed", host=host, status=status,
                            detail=detail, failures=sub.failure_count)
                if sub.failure_count >= MAX_FAILURES:
                    session.delete(sub)
                    result["removed"] += 1
    session.commit()
    return result


def digest_payload(digest: Digest, run: DigestRun) -> dict[str, Any]:
    """Notification for a delivered digest run; `url` is an in-app route."""
    content = run.content or {}
    count = content.get("email_count", len(run.email_ids or []))
    if count == 0:
        body = "No news."
    else:
        body = content.get("headline") or f"{count} new email{'s' if count != 1 else ''}"
    return {"title": digest.name, "body": body[:200], "tag": f"digest-{digest.id}",
            "url": f"/digests/runs/{run.id}"}
