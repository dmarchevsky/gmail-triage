"""Retention loop: hard-delete emails past the configured retention window."""

from datetime import UTC, datetime, timedelta

from app.models import Email, EmailAction, Feedback
from app.services import settings_service
from app.services.retention import _delete_expired


def _email(db_session, *, gmail_id: str, status: str = "classified",
           received_at: datetime | None = None) -> Email:
    e = Email(
        gmail_message_id=gmail_id,
        status=status,
        received_at=received_at or datetime.now(UTC),
    )
    db_session.add(e)
    db_session.flush()
    return e


def test_no_op_when_retention_disabled(client, db_session):
    settings_service.set_setting(db_session, "retention_days", 0)
    old = _email(db_session, gmail_id="o1",
                 received_at=datetime.now(UTC) - timedelta(days=365))
    db_session.commit()

    assert _delete_expired(db_session) == 0
    db_session.expire_all()
    assert db_session.get(Email, old.id) is not None


def test_deletes_expired_terminal_emails(client, db_session):
    settings_service.set_setting(db_session, "retention_days", 30)
    old = _email(db_session, gmail_id="o1", status="classified",
                 received_at=datetime.now(UTC) - timedelta(days=31))
    recent = _email(db_session, gmail_id="n1", status="classified",
                    received_at=datetime.now(UTC) - timedelta(days=5))
    db_session.commit()

    assert _delete_expired(db_session) == 1
    db_session.expire_all()
    assert db_session.get(Email, old.id) is None
    assert db_session.get(Email, recent.id) is not None


def test_skips_pending_and_processing(client, db_session):
    settings_service.set_setting(db_session, "retention_days", 30)
    p = _email(db_session, gmail_id="p1", status="pending",
               received_at=datetime.now(UTC) - timedelta(days=60))
    q = _email(db_session, gmail_id="p2", status="processing",
               received_at=datetime.now(UTC) - timedelta(days=60))
    db_session.commit()

    assert _delete_expired(db_session) == 0
    db_session.expire_all()
    assert db_session.get(Email, p.id) is not None
    assert db_session.get(Email, q.id) is not None


def test_cascades_to_actions_and_feedback(client, db_session):
    settings_service.set_setting(db_session, "retention_days", 30)
    old = _email(db_session, gmail_id="o2", status="actioned",
                 received_at=datetime.now(UTC) - timedelta(days=40))
    db_session.add_all([
        EmailAction(email_id=old.id, action_type="mark_read", dry_run=False),
        Feedback(email_id=old.id),
    ])
    db_session.commit()

    assert _delete_expired(db_session) == 1
    db_session.expire_all()
    assert db_session.query(EmailAction).filter_by(email_id=old.id).count() == 0
    assert db_session.query(Feedback).filter_by(email_id=old.id).count() == 0


def test_all_terminal_statuses_deleted(client, db_session):
    settings_service.set_setting(db_session, "retention_days", 7)
    cutoff = datetime.now(UTC) - timedelta(days=8)
    for i, status in enumerate(["classified", "actioned", "skipped", "error"]):
        _email(db_session, gmail_id=f"e{i}", status=status, received_at=cutoff)
    db_session.commit()

    assert _delete_expired(db_session) == 4
