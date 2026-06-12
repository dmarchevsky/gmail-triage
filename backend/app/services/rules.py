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


async def _execute_action_set(client: GmailClient, labels: LabelManager,
                              session: Session, gmail_message_id: str,
                              actions: list[dict],
                              label_names: list[str | None]) -> None:
    """Resolve label ids and execute one batched modify (+trash) for an email."""
    add_ids: list[str] = []
    remove_ids: list[str] = []
    trash = False
    for action, name in zip(actions, label_names, strict=True):
        atype = ActionType(action["type"])
        if atype == ActionType.add_label and name:
            add_ids.append(await labels.get_id(name))
        elif atype == ActionType.remove_label and name:
            remove_ids.append(await labels.get_id(name))
        elif atype == ActionType.mark_read:
            remove_ids.append("UNREAD")
        elif atype == ActionType.archive:
            remove_ids.append("INBOX")
        elif atype == ActionType.trash:
            trash = True
    if add_ids or remove_ids:
        await client.modify_message(gmail_message_id,
                                    add_label_ids=sorted(set(add_ids)),
                                    remove_label_ids=sorted(set(remove_ids)))
    if trash:
        await client.trash_message(gmail_message_id)


async def apply_rules_to_email(session: Session, client: GmailClient | None,
                               email: Email, rules: list[Rule]) -> int:
    """Evaluate rules, persist EmailAction rows. Each rule carries its own
    dry_run flag: live rules execute against Gmail, dry rules only record
    planned actions; one email may get a mix.

    All Gmail awaits happen with a clean session, and all DB mutations land in
    one short transaction at the end.
    """
    planned = evaluate_rules(rules, email)
    if not planned:
        return 0

    # Phase 1: resolve label names (reads only; session stays clean).
    label_names: list[str | None] = []
    for _rule, action in planned:
        if ActionType(action["type"]) in (ActionType.add_label, ActionType.remove_label):
            label_names.append(await _label_name_for_action(session, action))
        else:
            label_names.append(None)

    live = [(rule, action, name)
            for (rule, action), name in zip(planned, label_names, strict=True)
            if not rule.dry_run]

    # Phase 2: execute the live subset before touching the session.
    live_ok = bool(live)
    exec_error: str | None = None
    if live and client is not None:
        try:
            await _execute_action_set(
                client, LabelManager(client), session, email.gmail_message_id,
                [a for _, a, _n in live], [n for _, _a, n in live])
        except Exception as e:
            live_ok = False
            exec_error = str(e)[:500]
            log.error("action_execution_failed", email_id=email.id, error=str(e))

    # Phase 3: persist the outcome in one short transaction.
    now = datetime.now(UTC)
    any_executed = bool(live) and live_ok
    email.dry_run = not any_executed  # True only if nothing ran live
    for _rule, action in planned:
        is_live = not _rule.dry_run
        session.add(EmailAction(
            email_id=email.id, rule_id=_rule.id,
            action_type=action["type"], action_params=action,
            executed=is_live and live_ok, dry_run=_rule.dry_run,
            executed_at=now if (is_live and live_ok) else None,
            error=exec_error if is_live else None))
    if exec_error is not None:
        email.error = f"action execution failed: {exec_error[:300]}"
        audit(session, "system", "actions_failed",
              {"email_id": email.id, "error": exec_error[:300]})
    else:
        email.status = EmailStatus.actioned.value
        audit(session, "system",
              "actions_executed" if any_executed else "actions_planned", {
                  "email_id": email.id,
                  "live_actions": [a["type"] for _r, a, _n in live],
                  "planned_actions": [a["type"] for r, a in planned if r.dry_run],
              })
    session.commit()
    return len(planned)


async def apply_planned_for_rule(session: Session, client: GmailClient,
                                 rule: Rule) -> dict:
    """Execute a rule's previously recorded, unexecuted dry-run plans — exactly
    as reviewed in the UI (no re-evaluation). Per-email failures are recorded
    and processing continues."""
    rows = list(session.scalars(
        select(EmailAction).where(EmailAction.rule_id == rule.id,
                                  EmailAction.dry_run.is_(True),
                                  EmailAction.executed.is_(False))
        .order_by(EmailAction.email_id, EmailAction.id)))
    by_email: dict[int, list[EmailAction]] = {}
    for row in rows:
        by_email.setdefault(row.email_id, []).append(row)

    labels = LabelManager(client)
    applied = failed = 0
    for email_id, action_rows in by_email.items():
        email = session.get(Email, email_id)
        if email is None:
            continue
        actions = [r.action_params or {"type": r.action_type} for r in action_rows]
        label_names = [await _label_name_for_action(session, a)
                       if ActionType(a["type"]) in (ActionType.add_label,
                                                    ActionType.remove_label)
                       else None for a in actions]
        try:
            await _execute_action_set(client, labels, session,
                                      email.gmail_message_id, actions, label_names)
        except Exception as e:
            for row in action_rows:
                row.error = str(e)[:500]
            failed += len(action_rows)
            log.error("apply_planned_failed", email_id=email_id, rule_id=rule.id,
                      error=str(e))
            session.commit()
            continue
        now = datetime.now(UTC)
        for row in action_rows:
            row.executed = True
            row.dry_run = False
            row.executed_at = now
            row.error = None
        email.dry_run = False
        applied += len(action_rows)
        session.commit()

    audit(session, "user", "planned_actions_applied", {
        "rule_id": rule.id, "applied": applied, "failed": failed,
        "emails": len(by_email)})
    session.commit()
    return {"applied": applied, "failed": failed, "emails": len(by_email)}


def load_enabled_rules(session: Session) -> list[Rule]:
    return list(session.scalars(
        select(Rule).where(Rule.enabled.is_(True)).order_by(Rule.priority, Rule.id)))
