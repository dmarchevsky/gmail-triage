"""M7: secrets redaction audit, login rate limiting, config export/import."""

import json

from app.services import gmail
from tests.test_m1_gmail import CLIENT_SECRET_JSON, MODIFY_SCOPE, make_token


def test_settings_export_never_contains_secrets(auth_client, db_session):
    auth_client.put("/api/v1/settings", json={
        "telegram_bot_token": "123:supersecret",
        "gmail_client_secret_json": CLIENT_SECRET_JSON,
    })
    exported = auth_client.get("/api/v1/settings").json()
    blob = json.dumps(exported)
    assert "supersecret" not in blob
    assert "csecret" not in blob  # OAuth client secret
    assert exported["telegram_bot_token_configured"] is True
    assert exported["gmail_client_secret_json_configured"] is True


def test_gmail_auth_endpoint_never_returns_token(auth_client, db_session):
    gmail.save_token(db_session, make_token(), email="me@gmail.test")
    db_session.commit()
    info = auth_client.get("/api/v1/gmail/auth").json()
    blob = json.dumps(info)
    assert "at-123" not in blob
    assert "rt-456" not in blob
    assert info["granted_scopes"] == [MODIFY_SCOPE]


def test_status_endpoint_is_minimal(client):
    """Public healthcheck endpoint must not leak config or secrets."""
    body = json.dumps(client.get("/api/v1/status").json())
    for needle in ["token", "secret", "password", "criteria"]:
        assert needle not in body.lower()


def test_login_rate_limited(client):
    for _ in range(5):
        client.post("/api/v1/auth/login", json={"password": "wrong"})
    resp = client.post("/api/v1/auth/login", json={"password": "wrong"})
    assert resp.status_code == 429


def test_settings_import_roundtrip(auth_client):
    exported = auth_client.get("/api/v1/settings").json()
    exported["poll_interval_seconds"] = 600
    exported["unknown_junk"] = "x"
    result = auth_client.post("/api/v1/settings/import", json=exported).json()
    assert "poll_interval_seconds" in result["imported"]
    assert "unknown_junk" not in result["imported"]
    assert not any(k.endswith("_configured") for k in result["imported"])
    assert auth_client.get("/api/v1/settings").json()["poll_interval_seconds"] == 600


def test_prompt_settings_default_to_disk_and_roundtrip(auth_client):
    """Editable prompts default to the on-disk prompt files and persist on save;
    summarization depth round-trip."""
    from app.services.settings_service import _prompt_file

    settings = auth_client.get("/api/v1/settings").json()
    assert settings["prompt_classification_system"] == \
        _prompt_file("classification_system.txt")
    assert settings["prompt_digest_synthesis"] == _prompt_file("digest_synthesis_system.txt")
    assert settings["summarization_depth"] == "default"

    auth_client.put("/api/v1/settings", json={
        "prompt_summary_concise": "Custom concise prompt.",
        "summarization_depth": "extended"})
    updated = auth_client.get("/api/v1/settings").json()
    assert updated["prompt_summary_concise"] == "Custom concise prompt."
    assert updated["summarization_depth"] == "extended"


def test_removed_settings_rejected(auth_client):
    """The retired batch-size / digest-body knobs are no longer valid keys."""
    for key in ("digest_micro_batch_size", "digest_body_max_chars"):
        resp = auth_client.put("/api/v1/settings", json={key: 7})
        assert resp.status_code >= 400


def test_active_summary_prompt_tracks_depth():
    from app.services import settings_service

    base = dict(settings_service.DEFAULTS)
    base["summarization_depth"] = "extended"
    assert settings_service.active_summary_prompt(base) == base["prompt_summary_extended"]
    base["summarization_depth"] = "concise"
    assert settings_service.active_summary_prompt(base) == base["prompt_summary_concise"]


def test_audit_log_never_contains_secret_values(auth_client, db_session):
    from app.models import AuditLog

    auth_client.put("/api/v1/settings", json={"telegram_bot_token": "999:topsecret"})
    rows = db_session.query(AuditLog).all()
    dumped = json.dumps([{"actor": r.actor, "event_type": r.event_type,
                          "payload": r.payload} for r in rows])
    assert "topsecret" not in dumped


def test_log_snippet_truncation():
    from app.logging_setup import MAX_SNIPPET_LOG_CHARS, truncate_snippet

    assert truncate_snippet("x" * 1000) == "x" * MAX_SNIPPET_LOG_CHARS
    assert MAX_SNIPPET_LOG_CHARS == 200  # spec §6.6
    assert truncate_snippet(None) is None


def test_telegram_status_derived_from_settings(auth_client):
    assert auth_client.get("/api/v1/status").json()["telegram"]["status"] \
        == "unconfigured"
    auth_client.put("/api/v1/settings", json={
        "telegram_bot_token": "123:abc", "telegram_default_chat_id": "55"})
    # Configured in DB -> no longer "unconfigured", even with no send yet
    # (and survives restarts, unlike the in-memory app_state).
    assert auth_client.get("/api/v1/status").json()["telegram"]["status"] \
        == "configured"
