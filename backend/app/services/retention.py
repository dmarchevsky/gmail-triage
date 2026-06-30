"""Automatic retention: hard-delete emails older than `retention_days` setting."""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.db import get_sessionmaker
from app.logging_setup import get_logger
from app.models import Email, EmailStatus
from app.services import settings_service

log = get_logger(__name__)

_RETENTION_INTERVAL = 24 * 60 * 60  # seconds


def _delete_expired(session: Session) -> int:
    """Delete emails (and cascade children) older than retention_days.
    Returns deleted email count. No-op when retention_days == 0."""
    retention_days = settings_service.get_setting(session, "retention_days")
    if not retention_days:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=int(retention_days))
    result = session.execute(
        delete(Email).where(
            Email.received_at < cutoff,
            ~Email.status.in_([EmailStatus.pending.value, EmailStatus.processing.value]),
        )
    )
    session.commit()
    return result.rowcount


async def retention_loop() -> None:
    """Background task: purge emails past the retention window once every 24 hours."""
    log.info("retention_loop_started")
    while True:
        await asyncio.sleep(_RETENTION_INTERVAL)
        session = get_sessionmaker()()
        try:
            deleted = _delete_expired(session)
            if deleted:
                log.info("retention_purged", count=deleted)
        except Exception:  # noqa: BLE001
            log.exception("retention_loop_error")
        finally:
            session.close()
