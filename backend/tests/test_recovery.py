"""Recovery sweeps: stalled `processing` emails (incl. NULL timestamps), bounded
`error` retries, stalled-digest failover, and run_digest's broadened error handling."""

from datetime import UTC, datetime, timedelta

import respx

from app.models import Category, Digest, DigestRun, DigestRunStatus, Email, EmailStatus
from app.services import classifier

SETTINGS = {
    "llm_classify_timeout_seconds": 120,
    "llm_digest_timeout_seconds": 300,
    "classify_max_attempts": 5,
}


def _email(db, mid, *, status, started=None, attempts=0):
    e = Email(gmail_message_id=mid, sender="a@x.com", subject="s",
              status=status, processing_started_at=started, attempts=attempts)
    db.add(e)
    db.commit()
    return e


# ── email stall recovery ─────────────────────────────────────────────────────

def test_stalled_email_with_null_timestamp_is_recovered(db_session):
    """A `processing` row whose processing_started_at is NULL would be invisible to
    `NULL < cutoff`; it must still be reset to pending."""
    e = _email(db_session, "n1", status=EmailStatus.processing.value, started=None)
    classifier._recover_stalled_emails(db_session, SETTINGS)
    db_session.commit()
    db_session.refresh(e)
    assert e.status == EmailStatus.pending.value


def test_stalled_email_past_timeout_reset_recent_left(db_session):
    old = _email(db_session, "old", status=EmailStatus.processing.value,
                 started=datetime.now(UTC) - timedelta(seconds=1000))
    recent = _email(db_session, "recent", status=EmailStatus.processing.value,
                    started=datetime.now(UTC))
    classifier._recover_stalled_emails(db_session, SETTINGS)
    db_session.commit()
    db_session.refresh(old)
    db_session.refresh(recent)
    assert old.status == EmailStatus.pending.value
    assert old.processing_started_at is None
    assert recent.status == EmailStatus.processing.value


def test_stalled_email_at_cap_marked_error(db_session):
    e = _email(db_session, "cap", status=EmailStatus.processing.value,
               started=datetime.now(UTC) - timedelta(seconds=1000), attempts=5)
    classifier._recover_stalled_emails(db_session, SETTINGS)
    db_session.commit()
    db_session.refresh(e)
    assert e.status == EmailStatus.error.value
    assert "gave up" in (e.error or "")


# ── error retry recovery ─────────────────────────────────────────────────────

def test_error_email_under_cap_retried(db_session):
    e = _email(db_session, "err", status=EmailStatus.error.value, attempts=2)
    classifier._recover_error_emails(db_session, SETTINGS)
    db_session.commit()
    db_session.refresh(e)
    assert e.status == EmailStatus.pending.value


def test_error_email_at_cap_stays_error(db_session):
    e = _email(db_session, "errcap", status=EmailStatus.error.value, attempts=5)
    classifier._recover_error_emails(db_session, SETTINGS)
    db_session.commit()
    db_session.refresh(e)
    assert e.status == EmailStatus.error.value


# ── digest stall recovery ────────────────────────────────────────────────────

def _running_run(db, started):
    d = Digest(name="d", category_ids=[], cron_times=["07:00"], timezone="UTC")
    db.add(d)
    db.flush()
    run = DigestRun(digest_id=d.id, status=DigestRunStatus.running.value,
                    started_at=started)
    db.add(run)
    db.commit()
    return run


def test_stalled_digest_run_failed(db_session):
    run = _running_run(db_session, datetime.now(UTC) - timedelta(seconds=3600))
    classifier._recover_stalled_digests(db_session, SETTINGS)
    db_session.commit()
    db_session.refresh(run)
    assert run.status == DigestRunStatus.error.value
    assert run.finished_at is not None
    assert run.error


def test_recent_running_digest_left_alone(db_session):
    run = _running_run(db_session, datetime.now(UTC))
    classifier._recover_stalled_digests(db_session, SETTINGS)
    db_session.commit()
    db_session.refresh(run)
    assert run.status == DigestRunStatus.running.value


# ── run_digest broadened exception handling ──────────────────────────────────

@respx.mock
def test_run_digest_unexpected_error_marks_run_error(auth_client, db_session,
                                                     monkeypatch):
    """A non-listed exception (e.g. ValueError) inside summarization must mark the
    DigestRun `error`, never leave it stuck in `running`."""
    from app.services import digests, settings_service

    settings_service.set_setting(db_session, "telegram_bot_token", "123:abc")
    settings_service.set_setting(db_session, "telegram_default_chat_id", "555")
    db_session.commit()

    cat = Category(name="c", criteria_md="m")
    db_session.add(cat)
    db_session.flush()
    db_session.add(Email(gmail_message_id="r1", sender="a@x.com", subject="s",
                         snippet="x", status=EmailStatus.classified.value,
                         classification_id=cat.id, confidence=0.9,
                         received_at=datetime.now(UTC)))
    db_session.commit()

    async def boom(*a, **k):
        raise ValueError("kaboom")

    monkeypatch.setattr(digests, "_summarize", boom)

    d = auth_client.post("/api/v1/digests", json={
        "name": "d", "category_ids": [cat.id], "cron_times": ["07:00"],
        "min_confidence": 0.0}).json()
    run = auth_client.post(f"/api/v1/digests/{d['id']}/run-now").json()
    assert run["status"] == "error"
    assert "kaboom" in (run["error"] or "")
