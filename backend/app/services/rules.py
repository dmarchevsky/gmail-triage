"""Rule engine + action executor (spec §4.3, §4.5).

Action types are a closed set; send/reply/forward/draft/permanent-delete are
unrepresentable here and in the Gmail client wrapper. Rules are evaluated in
priority order (lower first); first match wins unless stop_processing=false.
No rule matched -> leave untouched (always).
"""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import Category, Email, EmailAction, EmailStatus, Rule
from app.services.audit import audit
from app.services.gmail import GmailClient
from app.services.matchers import sender_matches

log = get_logger(__name__)


class ActionType(StrEnum):
    add_label = "add_label"
    remove_label = "remove_label"
    mark_read = "mark_read"
    archive = "archive"
    trash = "trash"


class ActionSpec(BaseModel):
    type: ActionType
    category_id: int | None = None   # add_label: label of this category
    label_name: str | None = None    # add_label/remove_label: explicit label

    @model_validator(mode="after")
    def check_params(self) -> "ActionSpec":
        if self.type == ActionType.add_label and not (self.category_id or self.label_name):
            raise ValueError("add_label requires category_id or label_name")
        if self.type == ActionType.remove_label and not self.label_name:
            raise ValueError("remove_label requires label_name")
        return self


def validate_actions(raw_actions: list[dict]) -> list[dict]:
    if not raw_actions:
        raise ValueError("A rule needs at least one action")
    return [ActionSpec(**a).model_dump(exclude_none=True) for a in raw_actions]


def rule_matches(rule: Rule, email: Email) -> bool:
    if not rule.enabled:
        return False
    if rule.match_category_id is not None \
            and email.classification_id != rule.match_category_id:
        return False
    confidence = email.confidence if email.confidence is not None else 0.0
    if confidence < (rule.match_min_confidence or 0.0):
        return False
    if rule.match_sender_pattern:
        if not sender_matches([rule.match_sender_pattern], email.sender or ""):
            return False
    return True


def evaluate_rules(rules: list[Rule], email: Email) -> list[tuple[Rule, dict]]:
    """Planned (rule, action) pairs in execution order."""
    planned: list[tuple[Rule, dict]] = []
    for rule in sorted(rules, key=lambda r: (r.priority, r.id or 0)):
        if not rule_matches(rule, email):
            continue
        for action in rule.actions or []:
            planned.append((rule, action))
        if rule.stop_processing:
            break
    return planned


def is_hard_rule(rule: Rule) -> bool:
    """Pre-LLM deterministic rule: sender pattern, no category match (§4.2.1)."""
    return bool(rule.enabled and rule.match_sender_pattern
                and rule.match_category_id is None)


class LabelManager:
    """Ensures Gmail labels exist; caches name -> id for one client lifetime."""

    def __init__(self, client: GmailClient):
        self.client = client
        self._cache: dict[str, str] | None = None

    async def get_id(self, name: str) -> str:
        if self._cache is None:
            self._cache = {lb["name"]: lb["id"] for lb in await self.client.list_labels()}
        if name not in self._cache:
            created = await self.client.create_label(name)
            self._cache[created["name"]] = created["id"]
        return self._cache[name]


async def _label_name_for_action(session: Session, action: dict) -> str | None:
    if action.get("label_name"):
        return action["label_name"]
    if action.get("category_id"):
        category = session.get(Category, action["category_id"])
        if category:
            return category.gmail_label_name or f"MailTriage/{category.name}"
    return None


async def apply_rules_to_email(session: Session, client: GmailClient | None,
                               email: Email, rules: list[Rule], dry_run: bool) -> int:
    """Evaluate rules, persist EmailAction rows; execute against Gmail unless
    dry_run. Returns number of planned actions."""
    planned = evaluate_rules(rules, email)
    if not planned:
        return 0

    email.dry_run = dry_run
    add_ids: list[str] = []
    remove_ids: list[str] = []
    trash = False
    action_rows: list[EmailAction] = []
    labels = LabelManager(client) if client is not None else None

    for rule, action in planned:
        row = EmailAction(email_id=email.id, rule_id=rule.id,
                          action_type=action["type"], action_params=action,
                          executed=False, dry_run=dry_run)
        session.add(row)
        action_rows.append(row)
        if dry_run:
            continue
        atype = ActionType(action["type"])
        if atype == ActionType.add_label:
            name = await _label_name_for_action(session, action)
            if name and labels:
                add_ids.append(await labels.get_id(name))
        elif atype == ActionType.remove_label:
            if action.get("label_name") and labels:
                remove_ids.append(await labels.get_id(action["label_name"]))
        elif atype == ActionType.mark_read:
            remove_ids.append("UNREAD")
        elif atype == ActionType.archive:
            remove_ids.append("INBOX")
        elif atype == ActionType.trash:
            trash = True

    if not dry_run and client is not None:
        try:
            if add_ids or remove_ids:
                await client.modify_message(email.gmail_message_id,
                                            add_label_ids=sorted(set(add_ids)),
                                            remove_label_ids=sorted(set(remove_ids)))
            if trash:
                await client.trash_message(email.gmail_message_id)
            now = datetime.now(UTC)
            for row in action_rows:
                row.executed = True
                row.executed_at = now
        except Exception as e:
            for row in action_rows:
                if not row.executed:
                    row.error = str(e)[:500]
            email.error = f"action execution failed: {str(e)[:300]}"
            log.error("action_execution_failed", email_id=email.id, error=str(e))
            audit(session, "system", "actions_failed",
                  {"email_id": email.id, "error": str(e)[:300]})
            session.commit()
            return len(planned)

    email.status = EmailStatus.actioned.value
    audit(session, "system", "actions_planned" if dry_run else "actions_executed", {
        "email_id": email.id,
        "actions": [a["type"] for _, a in planned],
        "dry_run": dry_run,
    })
    session.commit()
    return len(planned)


def load_enabled_rules(session: Session) -> list[Rule]:
    return list(session.scalars(
        select(Rule).where(Rule.enabled.is_(True)).order_by(Rule.priority, Rule.id)))
