"""Web Push: VAPID key, subscription management, test notification."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import PushSubscription
from app.services import webpush
from app.services.audit import audit

router = APIRouter(prefix="/push")


class SubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=128)
    auth: str = Field(min_length=1, max_length=64)


class SubscriptionIn(BaseModel):
    """Shape of the browser's PushSubscription.toJSON()."""
    endpoint: str = Field(min_length=1, max_length=2048)
    keys: SubscriptionKeys


class EndpointIn(BaseModel):
    endpoint: str


@router.get("/vapid-public-key")
def vapid_public_key(session: Session = Depends(get_session)) -> dict:
    return {"public_key": webpush.vapid_public_key(session)}


@router.post("/subscriptions")
def subscribe(body: SubscriptionIn, request: Request,
              session: Session = Depends(get_session)) -> dict:
    try:
        webpush.validate_endpoint(body.endpoint)
        webpush.validate_keys(body.keys.p256dh, body.keys.auth)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    sub = session.scalar(select(PushSubscription)
                         .where(PushSubscription.endpoint == body.endpoint))
    created = sub is None
    if created:
        sub = PushSubscription(endpoint=body.endpoint)
        session.add(sub)
    sub.p256dh = body.keys.p256dh
    sub.auth = body.keys.auth
    sub.user_agent = (request.headers.get("user-agent") or "")[:512] or None
    sub.failure_count = 0
    if created:
        session.flush()
        audit(session, "user", "push_subscribed", {"id": sub.id})
    session.commit()
    return {"id": sub.id, "created": created}


@router.delete("/subscriptions")
def unsubscribe(body: EndpointIn, session: Session = Depends(get_session)) -> dict:
    sub = session.scalar(select(PushSubscription)
                         .where(PushSubscription.endpoint == body.endpoint))
    if sub is not None:
        audit(session, "user", "push_unsubscribed", {"id": sub.id})
        session.delete(sub)
        session.commit()
    return {"deleted": sub is not None}


@router.post("/test")
async def send_test(session: Session = Depends(get_session)) -> dict:
    return await webpush.notify_all(session, {
        "title": "MailTriage", "body": "Test notification — notifications work.",
        "tag": "test", "url": "/digests"})
