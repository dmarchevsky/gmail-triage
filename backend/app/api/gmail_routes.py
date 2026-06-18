"""Gmail OAuth + connection management endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.logging_setup import get_logger
from app.models import GmailAuth
from app.services import gmail, settings_service
from app.services.audit import audit
from app.state import app_state

log = get_logger(__name__)
router = APIRouter(prefix="/gmail")


def _redirect_uri(request: Request) -> str:
    return str(request.base_url).rstrip("/") + "/api/v1/gmail/oauth/callback"


class OAuthStartBody(BaseModel):
    client_secret_json: str | None = None  # optionally (re)set credentials here


@router.post("/oauth/start")
def oauth_start(body: OAuthStartBody, request: Request,
                session: Session = Depends(get_session)) -> dict:
    if body.client_secret_json:
        settings_service.set_setting(session, "gmail_client_secret_json",
                                     body.client_secret_json)
        session.commit()
    client_secret = body.client_secret_json or settings_service.get_setting(
        session, "gmail_client_secret_json")
    if not client_secret:
        raise HTTPException(status_code=400,
                            detail="Paste your Google OAuth client credentials JSON first")
    # In push mode also request the pubsub scope so the same user token can pull
    # the Pub/Sub subscription (no separate service-account secret).
    push_mode = settings_service.get_setting(session, "gmail_ingest_mode") == "push"
    try:
        url = gmail.build_auth_url(client_secret, _redirect_uri(request),
                                   include_pubsub=push_mode)
    except gmail.GmailError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"auth_url": url}


@router.get("/oauth/callback")
async def oauth_callback(request: Request, session: Session = Depends(get_session)):
    error = request.query_params.get("error")
    if error:
        return RedirectResponse(f"/?gmail_error={error}")
    code = request.query_params.get("code", "")
    state = request.query_params.get("state", "")
    if not code or not gmail.consume_state(state):
        raise HTTPException(status_code=400, detail="Invalid OAuth state or missing code")
    client_secret = settings_service.get_setting(session, "gmail_client_secret_json")
    if not client_secret:
        raise HTTPException(status_code=400, detail="OAuth client credentials missing")
    try:
        token = await gmail.exchange_code(client_secret, code, _redirect_uri(request))
        gmail.save_token(session, token)  # asserts no send-capable scope
        session.commit()
        client = gmail.GmailClient(session, client_secret)
        try:
            profile = await client.get_profile()
        finally:
            await client.aclose()
        row = session.scalar(select(GmailAuth).limit(1))
        row.email_address = profile.get("emailAddress")
        app_state.gmail_email = row.email_address
        app_state.gmail_status = "ok"
        audit(session, "user", "gmail_connected", {"email": row.email_address})
    except gmail.GmailAuthError as e:
        session.execute(delete(GmailAuth))
        audit(session, "user", "gmail_connect_failed", {"error": str(e)})
        return RedirectResponse("/?gmail_error=auth_failed")
    return RedirectResponse("/?gmail_connected=1")


_SYSTEM_LABEL_NAMES = {
    "INBOX": "Inbox",
    "CATEGORY_PERSONAL": "Primary",
    "CATEGORY_PROMOTIONS": "Promotions",
    "CATEGORY_SOCIAL": "Social",
    "CATEGORY_UPDATES": "Updates",
    "CATEGORY_FORUMS": "Forums",
}


@router.get("/labels")
async def list_gmail_labels(session: Session = Depends(get_session)) -> list[dict]:
    """Scope-relevant Gmail labels (inbox + category tabs + user labels) for
    the Mailbox-scope picker in Settings."""
    client_secret = settings_service.get_setting(session, "gmail_client_secret_json")
    if not client_secret or gmail.load_token(session) is None:
        raise HTTPException(status_code=409, detail="Gmail is not connected")
    client = gmail.GmailClient(session, client_secret)
    try:
        labels = await client.list_labels()
    finally:
        await client.aclose()
    out = []
    for lb in labels:
        lid = lb.get("id", "")
        if lid == "INBOX" or lid.startswith("CATEGORY_") or lb.get("type") == "user":
            out.append({
                "id": lid,
                "name": lb.get("name", lid),
                "display_name": _SYSTEM_LABEL_NAMES.get(lid, lb.get("name", lid)),
                "type": lb.get("type", "system"),
            })
    # Sort: inbox first, then categories, then user labels alphabetically.
    order = {"INBOX": 0}
    order.update({k: 1 for k in _SYSTEM_LABEL_NAMES if k != "INBOX"})
    out.sort(key=lambda x: (order.get(x["id"], 2), x["display_name"].lower()))
    return out


@router.get("/auth")
def get_auth_info(session: Session = Depends(get_session)) -> dict:
    row = session.scalar(select(GmailAuth).limit(1))
    if row is None:
        return {"connected": False}
    return {
        "connected": True,
        "email": row.email_address,
        "granted_scopes": row.granted_scopes,
        "history_id": row.history_id,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.delete("/auth")
async def disconnect(session: Session = Depends(get_session)) -> dict:
    loaded = gmail.load_token(session)
    if loaded is None:
        return {"connected": False}
    row, token = loaded
    # Tear down an active Gmail watch first (best-effort; needs the live token,
    # so it must happen before revoke). Otherwise Gmail keeps publishing change
    # notifications to the Pub/Sub topic until the watch lapses (~7 days).
    if row.watch_expiration:
        client_secret = settings_service.get_setting(session, "gmail_client_secret_json")
        if client_secret:
            try:
                client = gmail.GmailClient(session, client_secret)
                try:
                    await client.stop_watch()
                finally:
                    await client.aclose()
            except Exception as e:  # noqa: BLE001 — disconnect proceeds regardless
                log.warning("watch_stop_failed", error=str(e))
    try:
        await gmail.revoke_token(token)
    except Exception as e:  # noqa: BLE001 — still delete locally if revoke fails
        log.warning("token_revoke_failed", error=str(e))
    session.execute(delete(GmailAuth))
    audit(session, "user", "gmail_disconnected", {})
    app_state.gmail_status = "not_connected"
    app_state.gmail_email = None
    return {"connected": False}
