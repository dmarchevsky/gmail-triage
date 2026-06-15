"""Rule engine + action executor (spec §4.3, §4.5).

Action types are a closed set; send/reply/forward/draft/permanent-delete are
unrepresentable here and in the Gmail client wrapper. Rules are evaluated in
priority order (lower first); first match wins unless stop_processing=false.
No rule matched -> leave untouched (always).
"""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, model_validator
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import Email, EmailAction, EmailStatus, Label, Rule
from app.services.audit import audit
from app.services.gmail import GmailClient
from app.services.labels import ensure_gmail_label
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
    label_id: int | None = None  # add_label/remove_label: the Label to apply

    @model_validator(mode="after")
    def check_params(self) -> "ActionSpec":
        if self.type in (ActionType.add_label, ActionType.remove_label) \
                and self.label_id is None:
            raise ValueError(f"{self.type.value} requires label_id")
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
    """Planned (rule, action) pairs in execution order.

    The catch-all default rule (`is_default`) is always evaluated last and only
    contributes its actions when no other rule matched.
    """
    planned: list[tuple[Rule, dict]] = []
    matched = False
    default: Rule | None = None
    for rule in sorted(rules, key=lambda r: (bool(r.is_default), r.priority, r.id or 0)):
        if rule.is_default:
            default = rule
            continue
        if not rule_matches(rule, email):
            continue
        matched = True
        for action in rule.actions or []:
            planned.append((rule, action))
        if rule.stop_processing:
            break
    if not matched and default is not None:
        for action in default.actions or []:
            planned.append((default, action))
    return planned


def is_hard_rule(rule: Rule) -> bool:
    """Pre-LLM deterministic rule: sender pattern, no category match (§4.2.1)."""
    return bool(rule.enabled and rule.match_sender_pattern
                and rule.match_category_id is None)


def _label_for_action(session: Session, action: dict) -> Label | None:
    if action.get("label_id") is not None:
        return session.get(Label, action["label_id"])
    return None


async def _execute_action_set(client: GmailClient, label_cache: dict,
                              session: Session, gmail_message_id: str,
                              actions: list[dict],
                              labels: list[Label | None]) -> None:
    """Resolve Gmail label ids and execute one batched modify (+trash)."""
    add_ids: list[str] = []
    remove_ids: list[str] = []
    trash = False
    for action, label in zip(actions, labels, strict=True):
        atype = ActionType(action["type"])
        if atype == ActionType.add_label and label is not None:
            add_ids.append(await ensure_gmail_label(client, label_cache, label))
        elif atype == ActionType.remove_label and label is not None:
            remove_ids.append(await ensure_gmail_label(client, label_cache, label))
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

    # Phase 1: resolve target Labels (reads only; session stays clean).
    labels: list[Label | None] = [_label_for_action(session, action)
                                  for _rule, action in planned]

    live = [(rule, action, label)
            for (rule, action), label in zip(planned, labels, strict=True)
            if not rule.dry_run]

    # Phase 2: execute the live subset before touching the session.
    live_ok = bool(live)
    exec_error: str | None = None
    if live and client is not None:
        try:
            await _execute_action_set(
                client, {}, session, email.gmail_message_id,
                [a for _, a, _n in live], [n for _, _a, n in live])
        except Exception as e:
            live_ok = False
            exec_error = str(e)[:500]
            log.error("action_execution_failed", email_id=email.id, error=str(e))

    # Phase 3: persist the outcome in one short transaction.
    now = datetime.now(UTC)
    any_executed = bool(live) and live_ok
    email.dry_run = not any_executed  # True only if nothing ran live
    for (_rule, action), label in zip(planned, labels, strict=True):
        is_live = not _rule.dry_run
        params = dict(action)
        if label is not None:
            params["label_name"] = label.name
            params["text_color"] = label.text_color
            params["background_color"] = label.background_color
        session.add(EmailAction(
            email_id=email.id, rule_id=_rule.id,
            action_type=action["type"], action_params=params,
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

    label_cache: dict = {}
    applied = failed = 0
    for email_id, action_rows in by_email.items():
        email = session.get(Email, email_id)
        if email is None:
            continue
        actions = [r.action_params or {"type": r.action_type} for r in action_rows]
        labels = [_label_for_action(session, a) for a in actions]
        try:
            await _execute_action_set(client, label_cache, session,
                                      email.gmail_message_id, actions, labels)
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


def _reapply_targets(session: Session, rule: Rule) -> list[Email]:
    """Already-classified emails this rule should act on when reapplied.

    Normal rules target every email they match; the default catch-all targets
    only emails the full pipeline leaves for it (nothing else matched).
    """
    emails = list(session.scalars(
        select(Email).where(Email.status.in_(
            [EmailStatus.classified.value, EmailStatus.actioned.value]))
        .order_by(Email.received_at.desc())))
    if rule.is_default:
        enabled = load_enabled_rules(session)
        return [e for e in emails
                if any(r is rule for r, _a in evaluate_rules(enabled, e))]
    return [e for e in emails if rule_matches(rule, e)]


async def reapply_rule(session: Session, client: GmailClient | None,
                       rule: Rule) -> dict:
    """Re-run a single rule against the existing classified backlog.

    Clears the rule's prior actions on each target email, then applies just this
    rule's actions (respecting its dry_run flag). Per-email failures are recorded
    and processing continues.
    """
    targets = _reapply_targets(session, rule)
    label_cache: dict = {}
    actions = rule.actions or []
    labels = [_label_for_action(session, a) for a in actions]
    applied = failed = 0
    for email in targets:
        session.execute(delete(EmailAction).where(
            EmailAction.email_id == email.id, EmailAction.rule_id == rule.id))
        if not actions:
            session.commit()
            continue
        live_ok = True
        exec_error: str | None = None
        if not rule.dry_run and client is not None:
            try:
                await _execute_action_set(client, label_cache, session,
                                          email.gmail_message_id, actions, labels)
            except Exception as e:
                live_ok = False
                exec_error = str(e)[:500]
                log.error("reapply_failed", email_id=email.id, rule_id=rule.id,
                          error=str(e))
        is_live = not rule.dry_run
        executed = is_live and live_ok
        now = datetime.now(UTC)
        for action, label in zip(actions, labels, strict=True):
            params = dict(action)
            if label is not None:
                params["label_name"] = label.name
                params["text_color"] = label.text_color
                params["background_color"] = label.background_color
            session.add(EmailAction(
                email_id=email.id, rule_id=rule.id,
                action_type=action["type"], action_params=params,
                executed=executed, dry_run=rule.dry_run,
                executed_at=now if executed else None,
                error=exec_error if is_live else None))
        if executed:
            email.dry_run = False
            email.status = EmailStatus.actioned.value
            applied += 1
        elif exec_error is not None:
            failed += 1
        session.commit()

    audit(session, "user", "rule_reapplied", {
        "rule_id": rule.id, "matched": len(targets),
        "applied": applied, "failed": failed})
    session.commit()
    return {"matched": len(targets), "applied": applied, "failed": failed,
            "emails": len(targets)}
