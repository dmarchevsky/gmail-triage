"""Labels CRUD + Gmail color palette. Labels are applied to emails by rules."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Label, Rule
from app.services import gmail, settings_service
from app.services import labels as labels_service
from app.services.audit import audit
from app.services.gmail import GmailAuthError, GmailClient

router = APIRouter(prefix="/labels")


class LabelIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    text_color: str | None = None
    background_color: str | None = None


def serialize(lb: Label) -> dict:
    return {
        "id": lb.id, "name": lb.name, "gmail_label_id": lb.gmail_label_id,
        "text_color": lb.text_color, "background_color": lb.background_color,
    }


def _rules_using_label(session: Session, label_id: int) -> list[str]:
    names = []
    for rule in session.scalars(select(Rule)):
        for action in rule.actions or []:
            if action.get("label_id") == label_id:
                names.append(rule.name)
                break
    return names


async def _open_client(session: Session) -> GmailClient | None:
    secret = settings_service.get_setting(session, "gmail_client_secret_json")
    if not secret or gmail.load_token(session) is None:
        return None
    try:
        return GmailClient(session, secret)
    except GmailAuthError:
        return None


@router.get("/palette")
def palette() -> list[dict]:
    return labels_service.GMAIL_PALETTE


@router.get("")
def list_labels(session: Session = Depends(get_session)) -> list[dict]:
    return [serialize(lb) for lb in session.scalars(select(Label).order_by(Label.name))]


def _validate_color(body: LabelIn) -> None:
    if not labels_service.is_allowed_color(body.text_color, body.background_color):
        raise HTTPException(status_code=400,
                            detail="Color must be a {text, background} pair from the "
                            "Gmail palette (GET /labels/palette)")


@router.post("", status_code=201)
async def create_label(body: LabelIn, session: Session = Depends(get_session)) -> dict:
    _validate_color(body)
    if session.scalar(select(Label).where(Label.name == body.name)):
        raise HTTPException(status_code=409, detail="Label name already exists")
    label = Label(name=body.name, text_color=body.text_color,
                  background_color=body.background_color)
    session.add(label)
    session.flush()
    audit(session, "user", "label_created", {"id": label.id, "name": label.name})
    session.commit()

    client = await _open_client(session)
    if client is not None:
        try:
            await labels_service.sync_label_to_gmail(session, client, label)
            session.commit()
        finally:
            await client.aclose()
    return serialize(label)


@router.put("/{label_id}")
async def update_label(label_id: int, body: LabelIn,
                       session: Session = Depends(get_session)) -> dict:
    _validate_color(body)
    label = session.get(Label, label_id)
    if label is None:
        raise HTTPException(status_code=404, detail="Label not found")
    clash = session.scalar(select(Label).where(Label.name == body.name,
                                               Label.id != label_id))
    if clash:
        raise HTTPException(status_code=409, detail="Label name already exists")
    label.name = body.name
    label.text_color = body.text_color
    label.background_color = body.background_color
    audit(session, "user", "label_updated", {"id": label.id})
    session.commit()

    client = await _open_client(session)
    if client is not None:
        try:
            await labels_service.sync_label_to_gmail(session, client, label)
            session.commit()
        finally:
            await client.aclose()
    return serialize(label)


@router.delete("/{label_id}")
async def delete_label(label_id: int, force: bool = False,
                       session: Session = Depends(get_session)) -> dict:
    label = session.get(Label, label_id)
    if label is None:
        raise HTTPException(status_code=404, detail="Label not found")
    used_by = _rules_using_label(session, label_id)
    if used_by and not force:
        raise HTTPException(status_code=409,
                            detail=f"Label is used by rule(s): {', '.join(used_by)}. "
                            "Remove it from those rules first, or force-delete.")

    if label.gmail_label_id:
        client = await _open_client(session)
        if client is not None:
            try:
                await client.delete_label(label.gmail_label_id)
            except gmail.GmailError:
                pass  # already gone / not found — proceed with local delete
            finally:
                await client.aclose()

    audit(session, "user", "label_deleted", {"id": label.id, "name": label.name})
    session.delete(label)
    session.commit()
    return {"deleted": label_id}
