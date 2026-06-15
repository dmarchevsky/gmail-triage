"""M4 backend support: emails listing/filters/detail, stats, audit log."""

from datetime import UTC, datetime, timedelta

import pytest

from app.models import AuditLog, Category, Email, EmailAction


@pytest.fixture()
def dataset(db_session):
    cat1 = Category(name="MarketNews", criteria_md="m")
    cat2 = Category(name="Receipts", criteria_md="r")
    db_session.add_all([cat1, cat2])
    db_session.flush()
    now = datetime.now(UTC)
    emails = [
        Email(gmail_message_id="e1", sender="a@x.com", subject="Stocks up",
              status="classified", classification_id=cat1.id, confidence=0.95,
              received_at=now - timedelta(hours=1)),
        Email(gmail_message_id="e2", sender="b@y.com", subject="Your receipt",
              status="actioned", classification_id=cat2.id, confidence=0.6,
              received_at=now - timedelta(hours=2), dry_run=True),
        Email(gmail_message_id="e3", sender="c@z.com", subject="hello",
              status="classified", classification_id=None, confidence=0.2,
              received_at=now - timedelta(days=3),
              created_at=now - timedelta(days=3)),
        Email(gmail_message_id="e4", sender="d@w.com", subject="pending one",
              status="pending", received_at=now - timedelta(minutes=5)),
    ]
    db_session.add_all(emails)
    db_session.flush()
    db_session.add(EmailAction(email_id=emails[1].id, action_type="archive",
                               executed=False, dry_run=True))
    db_session.add(AuditLog(actor="system", event_type="poll_completed", payload={}))
    db_session.commit()
    return {"cat1": cat1.id, "cat2": cat2.id}


def test_email_filters(auth_client, dataset):
    assert auth_client.get("/api/v1/emails").json()["total"] == 4
    assert auth_client.get(
        f"/api/v1/emails?category_id={dataset['cat1']}").json()["total"] == 1
    assert auth_client.get("/api/v1/emails?category_id=0").json()["total"] == 2
    assert auth_client.get("/api/v1/emails?status=actioned").json()["total"] == 1
    assert auth_client.get("/api/v1/emails?confidence_min=0.5").json()["total"] == 2
    assert auth_client.get("/api/v1/emails?q=receipt").json()["total"] == 1
    page = auth_client.get("/api/v1/emails?page_size=2&page=2").json()
    assert page["total"] == 4 and len(page["items"]) == 2
    # newest first
    first = auth_client.get("/api/v1/emails").json()["items"][0]
    assert first["gmail_message_id"] == "e4"


def test_email_detail_includes_actions(auth_client, db_session, dataset):
    e2 = db_session.query(Email).filter_by(gmail_message_id="e2").one()
    detail = auth_client.get(f"/api/v1/emails/{e2.id}").json()
    assert detail["classification"] == "Receipts"
    assert detail["actions"][0]["action_type"] == "archive"
    assert detail["actions"][0]["dry_run"] is True
    assert auth_client.get("/api/v1/emails/99999").status_code == 404


def test_stats(auth_client, dataset):
    stats = auth_client.get("/api/v1/stats").json()
    assert stats["today"]["processed"] == 2   # e1 classified, e2 actioned (e4 pending)
    assert stats["week"]["processed"] == 3
    cats = {c["category"]: c for c in stats["category_precision"]}
    assert cats["MarketNews"]["classified_1d"] == 1
    assert cats["MarketNews"]["classified_7d"] == 1
    assert cats["Receipts"]["classified_7d"] == 1
    assert any(a["event_type"] == "poll_completed" for a in stats["recent_activity"])


def test_audit_log_endpoint(auth_client, dataset):
    log = auth_client.get("/api/v1/audit-log?event_type=poll_completed").json()
    assert log["total"] == 1
    assert log["items"][0]["actor"] == "system"


def test_timestamps_serialized_with_utc_offset(auth_client, db_session, dataset):
    """Naive-SQLite datetimes must come out tz-tagged so the browser parses
    them as UTC, not local (the 7-hours-ahead bug)."""
    e2 = db_session.query(Email).filter_by(gmail_message_id="e2").one()
    detail = auth_client.get(f"/api/v1/emails/{e2.id}").json()
    assert detail["received_at"].endswith("+00:00")
    listed = auth_client.get("/api/v1/emails").json()["items"]
    assert all(i["received_at"].endswith("+00:00") for i in listed if i["received_at"])
    log = auth_client.get("/api/v1/audit-log").json()["items"]
    assert all(r["ts"].endswith("+00:00") for r in log if r["ts"])


def test_utc_datetime_roundtrip_normalizes_aware_writes(db_session):
    from datetime import UTC, datetime, timedelta, timezone

    pdt = timezone(timedelta(hours=-7))
    written = datetime(2026, 6, 12, 11, 30, tzinfo=pdt)  # 18:30 UTC
    db_session.add(Email(gmail_message_id="tz1", received_at=written))
    db_session.commit()
    db_session.expire_all()
    read = db_session.query(Email).filter_by(gmail_message_id="tz1").one().received_at
    assert read.tzinfo is not None
    assert read == written                       # same instant
    assert read.astimezone(UTC).hour == 18       # stored/read as UTC
