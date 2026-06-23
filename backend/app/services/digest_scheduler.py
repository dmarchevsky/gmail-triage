"""APScheduler wiring for digest schedules (AsyncIOScheduler, in-process)."""

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.logging_setup import get_logger
from app.models import Digest

log = get_logger(__name__)

_scheduler: AsyncIOScheduler | None = None


def parse_hhmm(value: str) -> tuple[int, int]:
    hour_s, _, minute_s = value.partition(":")
    hour, minute = int(hour_s), int(minute_s or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid time {value!r}")
    return hour, minute


def build_triggers(digest: Digest) -> list[CronTrigger]:
    triggers = []
    for value in digest.cron_times or []:
        hour, minute = parse_hhmm(value)
        triggers.append(CronTrigger(hour=hour, minute=minute,
                                    timezone=digest.timezone or "UTC"))
    return triggers


async def _run_digest_job(digest_id: int) -> None:
    from app.db import get_sessionmaker
    from app.services.digests import run_digest

    session = get_sessionmaker()()
    try:
        digest = session.get(Digest, digest_id)
        if digest is None or not digest.enabled:
            return
        await run_digest(session, digest, actor="scheduler")
    except Exception as e:  # noqa: BLE001 — scheduler jobs must not crash the loop
        log.error("digest_job_failed", digest_id=digest_id, error=str(e))
    finally:
        session.close()


async def _check_thresholds_job() -> None:
    from app.db import get_sessionmaker
    from app.models import Digest
    from app.services.digests import count_eligible_emails, run_digest

    session = get_sessionmaker()()
    try:
        digests = session.scalars(
            select(Digest).where(
                Digest.enabled.is_(True),
                Digest.email_threshold.is_not(None),
            )
        ).all()
        for digest in digests:
            try:
                count = count_eligible_emails(session, digest)
                if count >= digest.email_threshold:
                    log.info("threshold_triggered", digest_id=digest.id,
                             count=count, threshold=digest.email_threshold)
                    await run_digest(session, digest, actor="threshold")
            except Exception as e:  # noqa: BLE001
                log.error("threshold_check_digest_failed",
                          digest_id=digest.id, error=str(e))
    except Exception as e:  # noqa: BLE001
        log.error("threshold_job_failed", error=str(e))
    finally:
        session.close()


def reschedule_all() -> None:
    """Sync scheduler jobs with the digests table. Safe to call on any CRUD."""
    if _scheduler is None:
        return
    from app.db import get_sessionmaker

    for job in _scheduler.get_jobs():
        if job.id.startswith("digest-"):
            job.remove()
    session = get_sessionmaker()()
    try:
        for digest in session.scalars(select(Digest).where(Digest.enabled.is_(True))):
            for i, trigger in enumerate(build_triggers(digest)):
                _scheduler.add_job(_run_digest_job, trigger,
                                   args=[digest.id],
                                   id=f"digest-{digest.id}-{i}",
                                   replace_existing=True,
                                   misfire_grace_time=3600)
    finally:
        session.close()
    log.info("digest_jobs_rescheduled",
             jobs=[str(j) for j in _scheduler.get_jobs()])


def start() -> None:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler()
    if not _scheduler.running:
        _scheduler.start()
    reschedule_all()
    from apscheduler.triggers.interval import IntervalTrigger
    _scheduler.add_job(
        _check_thresholds_job,
        IntervalTrigger(minutes=30),
        id="threshold-check",
        replace_existing=True,
    )


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
