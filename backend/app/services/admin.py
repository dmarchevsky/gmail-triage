"""Database purge operations: processing-data purge and full factory reset.

Both are local-only — no Gmail mutations (labels already applied stay).
"""

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import (
    AuditLog,
    Category,
    CategoryCriteriaHistory,
    Digest,
    DigestRun,
    Email,
    EmailAction,
    Feedback,
    GmailAuth,
    Rule,
    Setting,
)
from app.services import digest_scheduler, gmail
from app.services.audit import audit
from app.state import app_state

log = get_logger(__name__)

# FK-safe order: children before parents.
PROCESSING_TABLES = [EmailAction, Feedback, DigestRun, Email, AuditLog]
CONFIG_TABLES = [CategoryCriteriaHistory, Rule, Digest, Category, Setting, GmailAuth]


def _delete_all(session: Session, models: list) -> dict[str, int]:
    counts: dict[str, int] = {}
    for model in models:
        counts[model.__tablename__] = session.scalar(
            select(func.count()).select_from(model)) or 0
        session.execute(delete(model))
    return counts


def purge_processing_data(session: Session) -> dict[str, int]:
    """Delete emails/classifications/actions/digest runs/feedback/audit log;
    keep Gmail connection, categories, rules, digests, settings. Clears the
    sync watermark so the next poll re-ingests the initial-lookback window."""
    counts = _delete_all(session, PROCESSING_TABLES)
    auth_row = session.scalar(select(GmailAuth).limit(1))
    if auth_row is not None:
        auth_row.history_id = None
    app_state.poller_last_run_at = None
    app_state.poller_last_error = None
    audit(session, "user", "data_purged", {"deleted": counts})
    session.commit()
    log.info("processing_data_purged", **counts)
    return counts


async def factory_reset(session: Session) -> dict[str, int]:
    """Wipe everything: revoke + delete the Gmail token, all data and all
    configuration. The first-run wizard reappears (first_run_complete gone)."""
    loaded = gmail.load_token(session)
    if loaded is not None:
        try:
            await gmail.revoke_token(loaded[1])
        except Exception as e:  # noqa: BLE001 — still reset locally if revoke fails
            log.warning("factory_reset_revoke_failed", error=str(e))

    counts = _delete_all(session, PROCESSING_TABLES + CONFIG_TABLES)
    session.commit()
    digest_scheduler.reschedule_all()

    app_state.gmail_email = None
    app_state.gmail_status = "not_connected"
    app_state.telegram_status = "unconfigured"
    app_state.poller_last_run_at = None
    app_state.poller_last_error = None
    log.info("factory_reset_done", **counts)
    return counts
