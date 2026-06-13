"""Regression tests for the intermittent 'database is locked' 500s.

SQLite has a single writer: background pipelines must never hold a write
transaction across network awaits, and short contention must wait (busy
timeout) instead of erroring.
"""

import sqlite3
import threading
import time
from datetime import UTC, datetime

import respx
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from tests.test_m2_classification import CHAT_URL, llm_response


def _db_path():
    from app.config import get_config

    return get_config().data_dir / "mailtriage.db"


def test_busy_timeout_configured(client):
    from app.db import get_engine

    with get_engine().connect() as conn:
        timeout_ms = conn.execute(text("PRAGMA busy_timeout")).scalar()
    assert timeout_ms == 30000


def test_write_waits_out_a_held_lock_instead_of_500(auth_client):
    """A 6s-held external write lock (> old 5s default timeout) must not
    produce a 500 — the request waits and then succeeds."""
    release = threading.Event()
    locked = threading.Event()

    def hold_lock():
        conn = sqlite3.connect(_db_path(), timeout=5)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO audit_log (ts, actor, event_type, payload) "
                "VALUES (?, 'system', 'lock_test', '{}')",
                (datetime.now(UTC).isoformat(),))
            locked.set()
            release.wait(timeout=15)
            conn.rollback()
        finally:
            conn.close()

    def release_after(delay: float):
        time.sleep(delay)
        release.set()

    holder = threading.Thread(target=hold_lock)
    releaser = threading.Thread(target=release_after, args=(6.0,))
    holder.start()
    assert locked.wait(timeout=5)
    releaser.start()
    try:
        start = time.monotonic()
        resp = auth_client.post("/api/v1/categories",
                                json={"name": "LockTest", "criteria_md": "x"})
        elapsed = time.monotonic() - start
    finally:
        release.set()
        holder.join(timeout=15)
        releaser.join(timeout=15)
    assert resp.status_code == 201, resp.text
    assert elapsed >= 5.0  # it actually waited out the lock


def _assert_no_write_lock_held():
    """Probe: a concurrent writer must succeed immediately while the app is
    awaiting a network call. Raises sqlite3.OperationalError if the app holds
    a write transaction at that moment."""
    conn = sqlite3.connect(_db_path(), timeout=0.5)
    try:
        conn.execute(
            "INSERT INTO audit_log (ts, actor, event_type, payload) "
            "VALUES (?, 'system', 'probe', '{}')",
            (datetime.now(UTC).isoformat(),))
        conn.commit()
    finally:
        conn.close()


@respx.mock
def test_no_write_lock_held_during_digest_llm_calls(auth_client, db_session):
    from app.models import Category, Email
    from app.services import settings_service
    from tests.test_m5_digests import TG_SEND, tg_ok  # noqa: F401 (fixtures pattern)

    settings_service.set_setting(db_session, "telegram_bot_token", "123:abc")
    settings_service.set_setting(db_session, "telegram_default_chat_id", "5")
    cat = Category(name="MarketNews", criteria_md="m")
    db_session.add(cat)
    db_session.flush()
    db_session.add(Email(gmail_message_id="lk1", sender="a@x.com", subject="s",
                         snippet="snip", status="classified",
                         classification_id=cat.id, confidence=0.9,
                         received_at=datetime.now(UTC)))
    db_session.commit()
    digest = auth_client.post("/api/v1/digests", json={
        "name": "d", "category_ids": [cat.id], "min_confidence": 0.5}).json()

    def llm_side_effect(request):
        _assert_no_write_lock_held()
        return llm_response("Summary.")

    respx.post(CHAT_URL).mock(side_effect=llm_side_effect)
    run = auth_client.post(f"/api/v1/digests/{digest['id']}/run-now",
                           json={"preview": True}).json()
    assert run["status"] == "dry_run"
    assert run["summary_text"] == "Summary."


@respx.mock
def test_no_write_lock_held_during_gmail_action_calls(auth_client, db_session):
    from app.models import Email

    # Recreate the pipeline fixture inline (imported fixture functions can't
    # be called directly): connected Gmail + category + pending email.
    from app.services import gmail as gmail_mod
    from app.services import settings_service
    from tests.test_m1_gmail import CLIENT_SECRET_JSON, b64url, gmail_message, make_token
    from tests.test_m3_rules import GMAIL_API, classify_ok, pipeline  # noqa: F401

    settings_service.set_setting(db_session, "gmail_client_secret_json",
                                 CLIENT_SECRET_JSON)
    gmail_mod.save_token(db_session, make_token(), email="me@gmail.test")
    db_session.commit()
    cat = auth_client.post("/api/v1/categories", json={
        "name": "MarketNews", "criteria_md": "m"}).json()
    from app.models import Label
    label = Label(name="MailTriage/MarketNews")
    db_session.add(label)
    db_session.add(Email(gmail_message_id="m1", sender="Brew <crew@brew.com>",
                         sender_domain="brew.com", subject="s", snippet="x",
                         status="pending"))
    db_session.commit()
    full = gmail_message("m1")
    full["payload"]["parts"] = [
        {"mimeType": "text/plain", "body": {"data": b64url("Body.")}}]

    auth_client.post("/api/v1/rules", json={
        "name": "r", "match_category_id": cat["id"], "dry_run": False,
        "actions": [{"type": "add_label", "label_id": label.id},
                    {"type": "archive"}]})

    respx.get(f"{GMAIL_API}/messages/m1").respond(200, json=full)
    respx.post(CHAT_URL).mock(return_value=classify_ok())
    respx.get(f"{GMAIL_API}/labels").respond(200, json={"labels": []})
    respx.post(f"{GMAIL_API}/labels").respond(200, json={
        "id": "Label_9", "name": "MailTriage/MarketNews"})

    def modify_side_effect(request):
        _assert_no_write_lock_held()
        from httpx import Response
        return Response(200, json={})

    modify = respx.post(f"{GMAIL_API}/messages/m1/modify").mock(
        side_effect=modify_side_effect)

    resp = auth_client.post("/api/v1/classify/run-now")
    assert resp.json()["actioned"] == 1
    assert modify.call_count == 1

    from app.models import EmailAction
    actions = db_session.query(EmailAction).all()
    assert len(actions) == 2
    assert all(a.executed for a in actions)


def test_error_handlers_log_and_return_json(client):
    app = client.app

    @app.get("/boom")
    def boom():
        raise RuntimeError("kaboom")

    @app.get("/busy")
    def busy():
        raise OperationalError("INSERT ...", None, Exception("database is locked"))

    quiet = TestClient(app, raise_server_exceptions=False)
    resp = quiet.get("/boom")
    assert resp.status_code == 500
    assert resp.json() == {"detail": "Internal server error"}

    resp = quiet.get("/busy")
    assert resp.status_code == 503
    assert "busy" in resp.json()["detail"].lower()
