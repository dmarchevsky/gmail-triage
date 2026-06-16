"""Rules CRUD, reorder, and dry test against recent classified emails."""

from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import EmailAction, Label, Rule
from app.services import rules as rules_engine
from app.services.audit import audit

router = APIRouter(prefix="/rules")


class RuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    enabled: bool = True
    priority: int = 100
    match_category_id: int | None = None
    match_min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    match_sender_pattern: str | None = None
    actions: list[dict]
    stop_processing: bool = True
    dry_run: bool = True  # per-rule: True records planned actions only

    @field_validator("actions")
    @classmethod
    def validate_action_list(cls, v: list[dict]) -> list[dict]:
        try:
            return rules_engine.validate_actions(v)
        except ValueError as e:
            raise ValueError(str(e)) from e


def _pending_planned(session: Session, rule_id: int) -> int:
    return session.scalar(select(func.count(EmailAction.id)).where(
        EmailAction.rule_id == rule_id,
        EmailAction.dry_run.is_(True),
        EmailAction.executed.is_(False))) or 0


def _validate_label_ids(session: Session, actions: list[dict]) -> None:
    ids = {a["label_id"] for a in actions if a.get("label_id") is not None}
    if not ids:
        return
    found = set(session.scalars(select(Label.id).where(Label.id.in_(ids))))
    missing = ids - found
    if missing:
        raise HTTPException(status_code=400,
                            detail=f"Unknown label id(s): {sorted(missing)}")


def serialize(r: Rule, session: Session, *,
              pending_planned: int | None = None,
              label_map: dict[int, Label] | None = None) -> dict:
    # enrich label actions with the label's name + color for the UI
    if label_map is None:
        label_ids = {a["label_id"] for a in (r.actions or []) if a.get("label_id")}
        label_map = {lb.id: lb for lb in session.scalars(
            select(Label).where(Label.id.in_(label_ids)))} if label_ids else {}
    actions = []
    for a in r.actions or []:
        a = dict(a)
        lb = label_map.get(a.get("label_id"))
        if lb is not None:
            a["label_name"] = lb.name
            a["text_color"] = lb.text_color
            a["background_color"] = lb.background_color
        actions.append(a)
    if pending_planned is None:
        pending_planned = _pending_planned(session, r.id)
    return {
        "id": r.id, "name": r.name, "enabled": r.enabled, "priority": r.priority,
        "match_category_id": r.match_category_id,
        "match_min_confidence": r.match_min_confidence,
        "match_sender_pattern": r.match_sender_pattern,
        "actions": actions, "stop_processing": r.stop_processing,
        "dry_run": r.dry_run, "is_default": r.is_default,
        "pending_planned": pending_planned,
    }


@router.get("")
def list_rules(session: Session = Depends(get_session)) -> list[dict]:
    rules = list(session.scalars(
        select(Rule).order_by(Rule.is_default, Rule.priority, Rule.id)))
    rule_ids = [r.id for r in rules]
    # Batch the pending-planned counts and label lookups into one query each
    # instead of a COUNT + Label SELECT per rule.
    pending = {rid: cnt for rid, cnt in session.execute(
        select(EmailAction.rule_id, func.count(EmailAction.id))
        .where(EmailAction.rule_id.in_(rule_ids),
               EmailAction.dry_run.is_(True),
               EmailAction.executed.is_(False))
        .group_by(EmailAction.rule_id))} if rule_ids else {}
    label_ids = {a["label_id"] for r in rules for a in (r.actions or [])
                 if a.get("label_id")}
    label_map = {lb.id: lb for lb in session.scalars(
        select(Label).where(Label.id.in_(label_ids)))} if label_ids else {}
    return [serialize(r, session, pending_planned=pending.get(r.id, 0),
                      label_map=label_map) for r in rules]


@router.post("", status_code=201)
def create_rule(body: RuleIn, session: Session = Depends(get_session)) -> dict:
    _validate_label_ids(session, body.actions)
    rule = Rule(**body.model_dump())
    session.add(rule)
    session.flush()
    audit(session, "user", "rule_created", {"id": rule.id, "name": rule.name})
    session.commit()
    return serialize(rule, session)


class ReorderBody(BaseModel):
    ordered_ids: list[int]


class BulkRuleIds(BaseModel):
    rule_ids: list[int]


class BulkRuleUpdate(BaseModel):
    rule_ids: list[int]
    enabled: bool | None = None
    dry_run: bool | None = None


class DefaultRuleIn(BaseModel):
    """Editable fields of the catch-all rule: action(s) may be empty (no-op)."""
    enabled: bool = True
    dry_run: bool = True
    stop_processing: bool = True
    actions: list[dict]

    @field_validator("actions")
    @classmethod
    def validate_action_list(cls, v: list[dict]) -> list[dict]:
        if not v:
            return []
        try:
            return rules_engine.validate_actions(v)
        except ValueError as e:
            raise ValueError(str(e)) from e


@router.delete("/bulk")
def bulk_delete_rules(body: BulkRuleIds,
                      session: Session = Depends(get_session)) -> dict:
    if not body.rule_ids:
        return {"deleted": 0}
    rules = [r for r in session.scalars(select(Rule).where(Rule.id.in_(body.rule_ids)))
             if not r.is_default]
    for rule in rules:
        audit(session, "user", "rule_deleted", {"id": rule.id, "name": rule.name})
        session.delete(rule)
    session.commit()
    return {"deleted": len(rules)}


@router.put("/bulk")
def bulk_update_rules(body: BulkRuleUpdate,
                      session: Session = Depends(get_session)) -> dict:
    if not body.rule_ids:
        return {"updated": 0}
    rules = list(session.scalars(select(Rule).where(Rule.id.in_(body.rule_ids))))
    for rule in rules:
        if body.enabled is not None:
            rule.enabled = body.enabled
        if body.dry_run is not None:
            rule.dry_run = body.dry_run
    audit(session, "user", "rules_bulk_updated",
          {"ids": body.rule_ids, "enabled": body.enabled, "dry_run": body.dry_run})
    session.commit()
    return {"updated": len(rules)}


@router.put("/{rule_id}/default")
def update_default_rule(rule_id: int, body: DefaultRuleIn,
                        session: Session = Depends(get_session)) -> dict:
    """Edit the catch-all rule: only its action(s), dry-run, enabled and flow.
    Match gates stay fixed (it always matches when nothing else did)."""
    rule = session.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    if not rule.is_default:
        raise HTTPException(status_code=400, detail="Not the default rule")
    _validate_label_ids(session, body.actions)
    rule.enabled = body.enabled
    rule.dry_run = body.dry_run
    rule.stop_processing = body.stop_processing
    rule.actions = body.actions
    audit(session, "user", "default_rule_updated", {"id": rule.id})
    session.commit()
    return serialize(rule, session)


@router.put("/{rule_id}")
def update_rule(rule_id: int, body: RuleIn,
                session: Session = Depends(get_session)) -> dict:
    rule = session.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    if rule.is_default:
        raise HTTPException(status_code=400,
                            detail="Use PUT /rules/{id}/default for the default rule")
    _validate_label_ids(session, body.actions)
    for key, value in body.model_dump().items():
        setattr(rule, key, value)
    audit(session, "user", "rule_updated", {"id": rule.id})
    session.commit()
    return serialize(rule, session)


@router.delete("/{rule_id}")
def delete_rule(rule_id: int, session: Session = Depends(get_session)) -> dict:
    rule = session.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    if rule.is_default:
        raise HTTPException(status_code=400, detail="The default rule cannot be deleted")
    audit(session, "user", "rule_deleted", {"id": rule.id, "name": rule.name})
    session.delete(rule)
    session.commit()
    return {"deleted": rule_id}


@router.post("/reorder")
def reorder(body: ReorderBody, session: Session = Depends(get_session)) -> list[dict]:
    rules = {r.id: r for r in session.scalars(select(Rule))}
    unknown = set(body.ordered_ids) - set(rules)
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown rule ids: {sorted(unknown)}")
    # The default rule is pinned last; never reorder it even if passed in.
    position = 0
    for rule_id in body.ordered_ids:
        if rules[rule_id].is_default:
            continue
        rules[rule_id].priority = (position + 1) * 10
        position += 1
    audit(session, "user", "rules_reordered", {"order": body.ordered_ids})
    session.commit()
    return [serialize(r, session) for r in
            sorted(rules.values(), key=lambda r: (r.is_default, r.priority, r.id))]


@router.post("/{rule_id}/apply-planned")
async def apply_planned(rule_id: int, session: Session = Depends(get_session)) -> dict:
    """Execute this (now live) rule's previously planned dry-run actions."""
    rule = session.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    if rule.dry_run:
        raise HTTPException(status_code=409,
                            detail="Rule is still in dry-run; switch it to live first")
    async with _gmail_client_cm(session, required=True) as client:
        return await rules_engine.apply_planned_for_rule(session, client, rule)


def _gmail_client(session: Session):
    """Build a Gmail client or raise 409 — shared by the reapply endpoints."""
    from app.services import settings_service
    from app.services.gmail import GmailAuthError, GmailClient

    client_secret = settings_service.get_setting(session, "gmail_client_secret_json")
    if not client_secret:
        raise HTTPException(status_code=409, detail="Gmail is not connected")
    try:
        return GmailClient(session, client_secret)
    except GmailAuthError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@asynccontextmanager
async def _gmail_client_cm(session: Session, *, required: bool):
    """Yield a Gmail client (or None when not required) and always close it —
    the shared open/try-finally lifecycle for the apply/reapply endpoints."""
    client = _gmail_client(session) if required else None
    try:
        yield client
    finally:
        if client is not None:
            await client.aclose()


@router.post("/{rule_id}/reapply")
async def reapply_rule_route(rule_id: int,
                             session: Session = Depends(get_session)) -> dict:
    """Re-run this rule against the existing classified backlog."""
    rule = session.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    async with _gmail_client_cm(session, required=not rule.dry_run) as client:
        return await rules_engine.reapply_rule(session, client, rule)


@router.post("/reapply-bulk")
async def reapply_bulk(body: BulkRuleIds,
                       session: Session = Depends(get_session)) -> dict:
    """Re-run each selected rule against the existing classified backlog."""
    if not body.rule_ids:
        return {"rules": 0, "matched": 0, "applied": 0, "failed": 0}
    rules = list(session.scalars(
        select(Rule).where(Rule.id.in_(body.rule_ids))
        .order_by(Rule.is_default, Rule.priority, Rule.id)))
    required = any(not r.dry_run for r in rules)
    totals = {"rules": 0, "matched": 0, "applied": 0, "failed": 0}
    async with _gmail_client_cm(session, required=required) as client:
        for rule in rules:
            result = await rules_engine.reapply_rule(session, client, rule)
            totals["rules"] += 1
            totals["matched"] += result["matched"]
            totals["applied"] += result["applied"]
            totals["failed"] += result["failed"]
    return totals
