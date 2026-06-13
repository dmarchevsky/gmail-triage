"""Gmail REST client (httpx) + OAuth 2.0 flow.

Hard constraint (spec §1.1/§4.3): read-and-organize only. This wrapper exposes
an allowlist of endpoints — profile, labels, history, messages get/list,
modify/batchModify/trash. There is no generic passthrough and no code path to
send/draft/insert/import endpoints. The only requested scope is gmail.modify.
"""

import asyncio
import base64
import hashlib
import json
import re
import secrets
import urllib.parse
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_config
from app.logging_setup import get_logger
from app.models import GmailAuth

log = get_logger(__name__)

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# Refuse to operate if any of these ever appear in granted scopes (§6.1).
SEND_CAPABLE_SCOPES = {
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://mail.google.com/",
}

MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 1.0


class GmailError(Exception):
    pass


class GmailAuthError(GmailError):
    """Token invalid/revoked or scope violation."""


def assert_scopes_safe(granted_scopes: list[str] | None) -> None:
    granted = set(granted_scopes or [])
    bad = granted & SEND_CAPABLE_SCOPES
    if bad:
        raise GmailAuthError(
            f"Granted Gmail scopes include send-capable scope(s) {sorted(bad)}; "
            "MailTriage refuses to operate with send permission."
        )


# ── OAuth flow ───────────────────────────────────────────────────────────────

_pending_states: dict[str, datetime] = {}
STATE_TTL = timedelta(minutes=10)


def _client_config(raw_json: str) -> dict:
    data = json.loads(raw_json)
    for key in ("installed", "web"):
        if key in data:
            return data[key]
    if "client_id" in data:
        return data
    raise GmailError("Unrecognized OAuth client credentials JSON")


def build_auth_url(client_secret_json: str, redirect_uri: str) -> str:
    cfg = _client_config(client_secret_json)
    state = secrets.token_urlsafe(24)
    _pending_states[state] = datetime.now(UTC)
    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


def consume_state(state: str) -> bool:
    now = datetime.now(UTC)
    for s, ts in list(_pending_states.items()):
        if now - ts > STATE_TTL:
            del _pending_states[s]
    return _pending_states.pop(state, None) is not None


async def exchange_code(client_secret_json: str, code: str, redirect_uri: str) -> dict:
    cfg = _client_config(client_secret_json)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(GOOGLE_TOKEN_URL, data={
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        })
    if resp.status_code != 200:
        raise GmailAuthError(f"Token exchange failed: {resp.status_code} {resp.text[:300]}")
    token = resp.json()
    token["expiry"] = (datetime.now(UTC)
                       + timedelta(seconds=token.get("expires_in", 3600))).isoformat()
    return token


# ── Token persistence (Fernet-encrypted) ────────────────────────────────────

def save_token(session: Session, token: dict, email: str | None = None) -> GmailAuth:
    scopes = token.get("scope", "").split() if isinstance(token.get("scope"), str) \
        else token.get("scope") or []
    assert_scopes_safe(scopes)
    encrypted = get_config().fernet().encrypt(json.dumps(token).encode()).decode()
    row = session.scalar(select(GmailAuth).limit(1))
    if row is None:
        row = GmailAuth(token_json=encrypted, granted_scopes=scopes, email_address=email)
        session.add(row)
    else:
        row.token_json = encrypted
        row.granted_scopes = scopes
        if email:
            row.email_address = email
    return row


def load_token(session: Session) -> tuple[GmailAuth, dict] | None:
    row = session.scalar(select(GmailAuth).limit(1))
    if row is None:
        return None
    token = json.loads(get_config().fernet().decrypt(row.token_json.encode()).decode())
    return row, token


# ── Authenticated client with refresh + backoff ─────────────────────────────

class GmailClient:
    """One instance per poll run; refreshes the access token as needed."""

    def __init__(self, session: Session, client_secret_json: str):
        loaded = load_token(session)
        if loaded is None:
            raise GmailAuthError("Gmail is not connected")
        self.db = session
        self.auth_row, self.token = loaded
        assert_scopes_safe(self.auth_row.granted_scopes)
        self.client_cfg = _client_config(client_secret_json)
        self._http = httpx.AsyncClient(timeout=60)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _ensure_fresh(self) -> None:
        expiry = datetime.fromisoformat(self.token.get("expiry", "1970-01-01T00:00:00+00:00"))
        if expiry - datetime.now(UTC) > timedelta(seconds=60):
            return
        resp = await self._http.post(GOOGLE_TOKEN_URL, data={
            "client_id": self.client_cfg["client_id"],
            "client_secret": self.client_cfg["client_secret"],
            "refresh_token": self.token.get("refresh_token", ""),
            "grant_type": "refresh_token",
        })
        if resp.status_code != 200:
            raise GmailAuthError(f"Token refresh failed: {resp.status_code} {resp.text[:300]}")
        fresh = resp.json()
        self.token["access_token"] = fresh["access_token"]
        self.token["expiry"] = (datetime.now(UTC)
                                + timedelta(seconds=fresh.get("expires_in", 3600))).isoformat()
        save_token(self.db, self.token)
        self.db.commit()

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        await self._ensure_fresh()
        url = f"{GMAIL_API}{path}"
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            headers = {"Authorization": f"Bearer {self.token['access_token']}"}
            try:
                resp = await self._http.request(method, url, headers=headers, **kwargs)
            except httpx.TransportError as e:
                last_exc = e
                await asyncio.sleep(BACKOFF_BASE_SECONDS * 2 ** attempt)
                continue
            if resp.status_code in (401,):
                raise GmailAuthError("Gmail API returned 401 (token revoked?)")
            if resp.status_code == 429 or resp.status_code >= 500:
                await asyncio.sleep(BACKOFF_BASE_SECONDS * 2 ** attempt)
                last_exc = GmailError(f"{resp.status_code} {resp.text[:200]}")
                continue
            if resp.status_code == 404:
                if "/history" in path:
                    raise GmailHistoryExpired()
                raise GmailNotFound(f"404 for {path}")
            if resp.status_code >= 400:
                raise GmailError(f"Gmail API {resp.status_code}: {resp.text[:300]}")
            return resp.json() if resp.content else {}
        raise GmailError(f"Gmail API retries exhausted: {last_exc}")

    # Allowlisted endpoints only — read & organize. No send/draft/insert.

    async def get_profile(self) -> dict:
        return await self._request("GET", "/profile")

    async def list_labels(self) -> list[dict]:
        return (await self._request("GET", "/labels")).get("labels", [])

    async def create_label(self, name: str, color: dict | None = None) -> dict:
        body = {
            "name": name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        }
        if color:  # {"textColor": "#hex", "backgroundColor": "#hex"}
            body["color"] = color
        return await self._request("POST", "/labels", json=body)

    async def patch_label(self, label_id: str, name: str | None = None,
                          color: dict | None = None) -> dict:
        body: dict = {}
        if name is not None:
            body["name"] = name
        if color is not None:
            body["color"] = color
        return await self._request("PATCH", f"/labels/{label_id}", json=body)

    async def delete_label(self, label_id: str) -> dict:
        # Removes the label from all messages; does NOT delete any message.
        return await self._request("DELETE", f"/labels/{label_id}")

    async def list_history(self, start_history_id: str, page_token: str | None = None) -> dict:
        params = {
            "startHistoryId": start_history_id,
            "historyTypes": "messageAdded",
            "labelId": "INBOX",
        }
        if page_token:
            params["pageToken"] = page_token
        return await self._request("GET", "/history", params=params)

    async def list_messages(self, q: str, page_token: str | None = None,
                            max_results: int = 100) -> dict:
        params: dict = {"q": q, "maxResults": max_results, "labelIds": "INBOX"}
        if page_token:
            params["pageToken"] = page_token
        return await self._request("GET", "/messages", params=params)

    async def get_message_metadata(self, message_id: str) -> dict:
        return await self._request(
            "GET", f"/messages/{message_id}",
            params={"format": "metadata",
                    "metadataHeaders": ["From", "Subject", "Date"]})

    async def get_message_full(self, message_id: str) -> dict:
        return await self._request("GET", f"/messages/{message_id}", params={"format": "full"})

    async def modify_message(self, message_id: str, add_label_ids: list[str] | None = None,
                             remove_label_ids: list[str] | None = None) -> dict:
        return await self._request("POST", f"/messages/{message_id}/modify", json={
            "addLabelIds": add_label_ids or [],
            "removeLabelIds": remove_label_ids or [],
        })

    async def batch_modify(self, message_ids: list[str],
                           add_label_ids: list[str] | None = None,
                           remove_label_ids: list[str] | None = None) -> None:
        await self._request("POST", "/messages/batchModify", json={
            "ids": message_ids,
            "addLabelIds": add_label_ids or [],
            "removeLabelIds": remove_label_ids or [],
        })

    async def trash_message(self, message_id: str) -> dict:
        # messages.trash only — the permanent-delete endpoint is intentionally absent.
        return await self._request("POST", f"/messages/{message_id}/trash")


class GmailHistoryExpired(GmailError):
    """startHistoryId too old (HTTP 404) — caller must do a full re-sync."""


class GmailNotFound(GmailError):
    """A specific resource (e.g. a message) 404'd — usually deleted/moved
    between a history record and the fetch; safe to skip that item."""


async def revoke_token(token: dict) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        await client.post(GOOGLE_REVOKE_URL,
                          params={"token": token.get("refresh_token")
                                  or token.get("access_token", "")})


# ── Message parsing helpers ──────────────────────────────────────────────────

def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def extract_body_text(payload: dict) -> str:
    """Prefer text/plain; fall back to stripped text/html (spec §4.1)."""
    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        body_data = part.get("body", {}).get("data")
        if body_data:
            try:
                text = _b64url_decode(body_data).decode("utf-8", errors="replace")
            except (ValueError, TypeError):
                text = ""
            if mime == "text/plain":
                plain.append(text)
            elif mime == "text/html":
                html.append(text)
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    if plain:
        return "\n".join(plain).strip()
    if html:
        soup = BeautifulSoup("\n".join(html), "html.parser")
        return re.sub(r"\n{3,}", "\n\n", soup.get_text(separator="\n")).strip()
    return ""


def parse_message_meta(msg: dict) -> dict:
    headers = {h["name"].lower(): h["value"]
               for h in msg.get("payload", {}).get("headers", [])}
    sender = headers.get("from", "")
    _, addr = parseaddr(sender)
    domain = addr.rsplit("@", 1)[-1].lower() if "@" in addr else None
    internal_ms = int(msg.get("internalDate", "0"))
    received = datetime.fromtimestamp(internal_ms / 1000, UTC) if internal_ms else None
    return {
        "gmail_message_id": msg["id"],
        "gmail_thread_id": msg.get("threadId"),
        "history_id": msg.get("historyId"),
        "received_at": received,
        "sender": sender[:512],
        "sender_domain": domain,
        "subject": headers.get("subject", "")[:2000],
        "snippet": msg.get("snippet", ""),
        "size_estimate": msg.get("sizeEstimate"),
        "label_ids": msg.get("labelIds", []),
    }


def body_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
