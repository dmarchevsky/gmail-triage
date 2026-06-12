"""Purge processing data + factory reset (Danger zone)."""

from datetime import UTC, datetime

import pytest
import respx

from app.models import (
    AuditLog,
    Category,
    Digest,
    DigestRun,
    Email,
    EmailAction,
    Feedback,
    GmailAuth,
    Rule,
    Setting,
)
from app.services import gmail
from tests.test_m1_gmail import CLIENT_SECRET_JSON, gmail_message, make_token


@pytest.fixture()
def populated(auth_client, db_session):
    """Connected Gmail + config (category/rule/digest/settings) + processing
    data (email/action/feedback/digest run/audit rows)."""
    from app.services import settings_service

    settings_service.set_setting(db_session, "gmail_client_secret_json",
                                 CLIENT_SECRET_JSON)
    settings_service.set_setting(db_session, "first_run_complete", True)
    auth_row = gmail.save_token(db_session, make_token(), email="me@gmail.test")
    auth_row.history_id = "12345"

    category = Category(name="MarketNews", criteria_md="m")
    rule = Rule(name="r", actions=[{"type": "mark_read"}])
    digest = Digest(name="d", category_ids=[], cron_times=["07:00"])
    db_session.add_all([category, rule, digest])
    db_session.flush()
    email = Email(gmail_message_id="p1", sender="a@x.com", subject="s",
                  status="classified", classification_id=category.id,
                  confidence=0.9, received_at=datetime.now(UTC))
    db_session.add(email)
    db_session.flush()
    db_session.add_all([
        EmailAction(email_id=email.id, action_type="mark_read", dry_run=True),
        Feedback(email_id=email.id, correct_category_id=None),
        DigestRun(digest_id=digest.id, status="success", email_ids=[email.id]),
        AuditLog(actor="system", event_type="poll_completed", payload={}),
    ])
    db_session.commit()
    return {"category_id": category.id}


def counts(db_session) -> dict:
    return {model.__tablename__: db_session.query(model).count()
            for model in [Email, EmailAction, Feedback, DigestRun, AuditLog,
                          Category, Rule, Digest, Setting, GmailAuth]}


def test_endpoints_require_auth(client):
    assert client.post("/api/v1/admin/purge-data").status_code == 401
    assert client.post("/api/v1/admin/factory-reset").status_code == 401


def test_purge_data_keeps_config_clears_watermark(auth_client, db_session, populated):
    resp = auth_client.post("/api/v1/admin/purge-data")
    assert resp.status_code == 200
    deleted = resp.json()["deleted"]
    assert deleted["emails"] == 1
    assert deleted["email_actions"] == 1
    assert deleted["feedback"] == 1
    assert deleted["digest_runs"] == 1
    assert deleted["audit_log"] >= 1

    db_session.expire_all()
    after = counts(db_session)
    assert after["emails"] == 0
    assert after["email_actions"] == 0
    assert after["feedback"] == 0
    assert after["digest_runs"] == 0
    # config survives
    assert after["categories"] == 1
    assert after["rules"] == 1
    assert after["digests"] == 1
    assert after["settings"] >= 2
    assert after["gmail_auth"] == 1
    # watermark cleared; purge itself audited
    assert db_session.query(GmailAuth).one().history_id is None
    events = [a.event_type for a in db_session.query(AuditLog)]
    assert events == ["data_purged"]


@respx.mock
def test_purge_then_poll_does_baseline_sync(auth_client, db_session, populated):
    auth_client.post("/api/v1/admin/purge-data")

    m1 = gmail_message("m1")
    respx.get(f"{gmail.GMAIL_API}/messages").respond(200, json={
        "messages": [{"id": "m1"}]})
    respx.get(f"{gmail.GMAIL_API}/messages/m1").respond(200, json=m1)
    respx.get(f"{gmail.GMAIL_API}/profile").respond(200, json={
        "emailAddress": "me@gmail.test", "historyId": "9000"})

    resp = auth_client.post("/api/v1/poller/run-now")
    assert resp.json() == {"mode": "baseline", "new_emails": 1}


@respx.mock
def test_factory_reset_wipes_everything_and_revokes(auth_client, db_session,
                                                    populated):
    revoke = respx.post(gmail.GOOGLE_REVOKE_URL).respond(200)
    resp = auth_client.post("/api/v1/admin/factory-reset")
    assert resp.status_code == 200
    assert revoke.called

    db_session.expire_all()
    after = counts(db_session)
    assert all(n == 0 for n in after.values()), after

    settings = auth_client.get("/api/v1/settings").json()
    assert settings["first_run_complete"] is False  # wizard reappears
    status = auth_client.get("/api/v1/status").json()
    assert status["gmail"]["connected"] is False


@respx.mock
def test_factory_reset_survives_revoke_failure(auth_client, db_session, populated):
    respx.post(gmail.GOOGLE_REVOKE_URL).respond(500)
    resp = auth_client.post("/api/v1/admin/factory-reset")
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.query(GmailAuth).count() == 0
