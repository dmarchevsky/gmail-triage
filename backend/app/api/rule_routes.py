"""Rules CRUD, reorder, and dry test against recent classified emails."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Email, EmailStatus, Rule
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

    @field_validator("actions")
    @classmethod
    def validate_action_list(cls, v: list[dict]) -> list[dict]:
        try:
            return rules_engine.validate_actions(v)
        except ValueError as e:
            raise ValueError(str(e)) from e


def serialize(r: Rule) -> dict:
    return {
        "id": r.id, "name": r.name, "enabled": r.enabled, "priority": r.priority,
        "match_category_id": r.match_category_id,
        "match_min_confidence": r.match_min_confidence,
        "match_sender_pattern": r.match_sender_pattern,
        "actions": r.actions, "stop_processing": r.stop_processing,
    }


@router.get("")
def list_rules(session: Session = Depends(get_session)) -> list[dict]:
    return [serialize(r) for r in
            session.scalars(select(Rule).order_by(Rule.priority, Rule.id))]


@router.post("", status_code=201)
def create_rule(body: RuleIn, session: Session = Depends(get_session)) -> dict:
    rule = Rule(**body.model_dump())
    session.add(rule)
    session.flush()
    audit(session, "user", "rule_created", {"id": rule.id, "name": rule.name})
    session.commit()
    return serialize(rule)


@router.put("/{rule_id}")
def update_rule(rule_id: int, body: RuleIn,
                session: Session = Depends(get_session)) -> dict:
    rule = session.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    for key, value in body.model_dump().items():
        setattr(rule, key, value)
    audit(session, "user", "rule_updated", {"id": rule.id})
    session.commit()
    return serialize(rule)


@router.delete("/{rule_id}")
def delete_rule(rule_id: int, session: Session = Depends(get_session)) -> dict:
    rule = session.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    audit(session, "user", "rule_deleted", {"id": rule.id, "name": rule.name})
    session.delete(rule)
    session.commit()
    return {"deleted": rule_id}


class ReorderBody(BaseModel):
    ordered_ids: list[int]


@router.post("/reorder")
def reorder(body: ReorderBody, session: Session = Depends(get_session)) -> list[dict]:
    rules = {r.id: r for r in session.scalars(select(Rule))}
    unknown = set(body.ordered_ids) - set(rules)
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown rule ids: {sorted(unknown)}")
    for position, rule_id in enumerate(body.ordered_ids):
        rules[rule_id].priority = (position + 1) * 10
    audit(session, "user", "rules_reordered", {"order": body.ordered_ids})
    session.commit()
    return [serialize(r) for r in
            sorted(rules.values(), key=lambda r: (r.priority, r.id))]


class TestBody(BaseModel):
    limit: int = Field(default=20, ge=1, le=200)


@router.post("/{rule_id}/test")
def test_rule(rule_id: int, body: TestBody,
              session: Session = Depends(get_session)) -> dict:
    """Evaluate one rule against the last N classified emails. No execution."""
    rule = session.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    emails = session.scalars(
        select(Email).where(Email.status.in_(
            [EmailStatus.classified.value, EmailStatus.actioned.value]))
        .order_by(Email.received_at.desc()).limit(body.limit))
    matches = []
    tested = 0
    for email in emails:
        tested += 1
        if rules_engine.rule_matches(rule, email):
            matches.append({
                "email_id": email.id,
                "subject": email.subject,
                "sender": email.sender,
                "confidence": email.confidence,
                "planned_actions": [a["type"] for a in rule.actions or []],
            })
    return {"tested": tested, "matched": len(matches), "matches": matches}
