"""Classification pipeline (spec §4.2).

For each pending email: cheap pre-filters (ignore list), lazy body fetch from
Gmail, prompt built from enabled categories' criteria_md, LLM call with JSON
schema at temperature 0 (one retry inside chat_json), persist result.

Rule execution happens in M3; this module only sets classification fields.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import Category, Email, EmailStatus
from app.services import gmail, llm, settings_service
from app.services.gmail import GmailAuthError, GmailClient
from app.services.matchers import sender_matches

log = get_logger(__name__)

CLASSIFICATION_SCHEMA_TEMPLATE = {
    "type": "object",
    "properties": {
        "category": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "rationale": {"type": "string"},
    },
    "required": ["category", "confidence", "rationale"],
    "additionalProperties": False,
}


def build_classification_prompt(categories: list[Category], email: Email,
                                body: str, max_body_chars: int) -> tuple[str, str, dict]:
    categories_block = "\n\n".join(
        f"### {c.name}\n{c.criteria_md.strip() or '(no criteria provided)'}"
        for c in categories
    )
    user = llm.load_prompt("classification_user.txt").format(
        categories_block=categories_block,
        sender=email.sender or "(unknown)",
        subject=email.subject or "(no subject)",
        date=email.received_at.isoformat() if email.received_at else "(unknown)",
        body=body[:max_body_chars] if body else "(empty body)",
    )
    schema = {**CLASSIFICATION_SCHEMA_TEMPLATE,
              "properties": {**CLASSIFICATION_SCHEMA_TEMPLATE["properties"],
                             "category": {"type": "string",
                                          "enum": [c.name for c in categories] + ["none"]}}}
    return llm.load_prompt("classification_system.txt"), user, schema


async def fetch_body(session: Session, client: GmailClient, email: Email) -> str:
    """Body text for an email: stored copy if retained, else fetch from Gmail."""
    if email.body_text is not None:
        return email.body_text
    msg = await client.get_message_full(email.gmail_message_id)
    body = gmail.extract_body_text(msg.get("payload", {}))
    email.body_text_hash = gmail.body_hash(body)
    if bool(settings_service.get_setting(session, "store_bodies")):
        email.body_text = body
    return body


async def classify_email(session: Session, client: GmailClient, email: Email,
                         categories: list[Category], settings: dict) -> None:
    body = await fetch_body(session, client, email)
    session.commit()  # release the body-hash write before the LLM await
    system, user, schema = build_classification_prompt(
        categories, email, body, int(settings["classify_body_max_chars"]))
    try:
        result = await llm.chat_json(
            system, user, schema, "email_classification",
            timeout=float(settings["llm_classify_timeout_seconds"]),
            settings=settings,
            max_concurrency=int(settings["llm_max_concurrency"]),
        )
    except llm.LLMInvalidOutput as e:
        email.status = EmailStatus.error.value
        email.error = f"LLM output invalid after retry: {e}"
        return

    name = result["category"]
    category = next((c for c in categories if c.name == name), None)
    email.classification_id = category.id if category else None
    email.confidence = max(0.0, min(1.0, float(result["confidence"])))
    email.rationale = str(result["rationale"])[:2000]
    _, email.llm_model = llm.resolve_llm_target(settings)
    email.classified_at = datetime.now(UTC)
    email.status = EmailStatus.classified.value
    email.error = None


async def classify_pending(session: Session, limit: int = 50) -> dict:
    """Classify up to `limit` pending emails, then run the rule engine on each.
    LLM-unreachable stops the batch and leaves the remainder pending (§10.7)."""
    from app.services import rules as rules_engine

    settings = settings_service.get_all_settings(session, redact=False)
    categories = list(session.scalars(
        select(Category).where(Category.enabled.is_(True)).order_by(Category.id)))
    pending = list(session.scalars(
        select(Email).where(Email.status == EmailStatus.pending.value)
        .order_by(Email.received_at).limit(limit)))
    if not pending:
        return {"classified": 0, "skipped": 0, "errors": 0, "actioned": 0,
                "pending_left": 0}

    ignore_patterns = settings.get("ignore_senders") or []
    dry_run = bool(settings.get("dry_run", True))
    all_rules = rules_engine.load_enabled_rules(session)
    hard_rules = [r for r in all_rules if rules_engine.is_hard_rule(r)]
    counts = {"classified": 0, "skipped": 0, "errors": 0, "actioned": 0}

    client_secret = settings.get("gmail_client_secret_json")
    if not client_secret:
        raise GmailAuthError("Gmail is not connected")
    client = GmailClient(session, client_secret)
    try:
        for email in pending:
            if sender_matches(ignore_patterns, email.sender or ""):
                email.status = EmailStatus.skipped.value
                counts["skipped"] += 1
                session.commit()
                continue

            hard = next((r for r in hard_rules
                         if sender_matches([r.match_sender_pattern], email.sender or "")),
                        None)
            if hard is not None:
                # Deterministic pre-filter: bypass the LLM entirely (§4.2.1).
                email.confidence = 1.0
                email.rationale = f"hard rule: {hard.name}"
                email.classified_at = datetime.now(UTC)
                email.status = EmailStatus.classified.value
            else:
                if not categories:
                    break  # nothing to classify against; leave pending
                try:
                    await classify_email(session, client, email, categories, settings)
                except llm.LLMUnavailable as e:
                    log.warning("llm_unreachable_batch_stopped", error=str(e))
                    session.commit()
                    break

            counts["classified" if email.status == EmailStatus.classified.value
                   else "errors"] += 1
            session.commit()
            log.info("email_classified", email_id=email.id, status=email.status,
                     category_id=email.classification_id, confidence=email.confidence)

            if email.status == EmailStatus.classified.value and all_rules:
                acted = await rules_engine.apply_rules_to_email(
                    session, client, email, all_rules, dry_run)
                if acted:
                    counts["actioned"] += 1
    finally:
        await client.aclose()

    remaining = session.scalar(
        select(Email.id).where(Email.status == EmailStatus.pending.value).limit(1))
    return {**counts, "pending_left": int(remaining is not None)}
