"""M0 acceptance: status endpoint, migrations on start, auth, settings store."""

import pytest


def test_status_is_public_and_db_migrated(client):
    resp = client.get("/api/v1/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["rules_mode"] == {"live": 0, "dry": 0}
    assert body["gmail"]["connected"] is False


def test_api_requires_auth(client):
    assert client.get("/api/v1/settings").status_code == 401


def test_settings_roundtrip(auth_client):
    resp = auth_client.get("/api/v1/settings")
    assert resp.status_code == 200
    settings = resp.json()
    assert settings["poll_interval_seconds"] == 300
    # secrets are redacted to *_configured markers
    assert "telegram_bot_token" not in settings
    assert settings["telegram_bot_token_configured"] is False

    resp = auth_client.put("/api/v1/settings", json={"poll_interval_seconds": 120})
    assert resp.status_code == 200
    assert resp.json()["poll_interval_seconds"] == 120


def test_public_base_url_setting_roundtrips_unredacted(auth_client):
    """public_base_url is not a secret — it should round-trip in plain text,
    unlike telegram_bot_token above."""
    resp = auth_client.get("/api/v1/settings")
    assert resp.json()["public_base_url"] == ""

    resp = auth_client.put("/api/v1/settings",
                           json={"public_base_url": "https://mail.example.com"})
    assert resp.status_code == 200
    assert resp.json()["public_base_url"] == "https://mail.example.com"

    resp = auth_client.get("/api/v1/settings")
    assert resp.json()["public_base_url"] == "https://mail.example.com"


def test_unknown_setting_rejected(auth_client):
    resp = auth_client.put("/api/v1/settings", json={"nope": 1})
    assert resp.status_code == 400


def test_secret_setting_encrypted_at_rest(auth_client, db_session):
    auth_client.put("/api/v1/settings", json={"telegram_bot_token": "123:abc"})
    from app.models import Setting

    row = db_session.get(Setting, "telegram_bot_token")
    assert row.value != "123:abc"  # encrypted
    from app.services.settings_service import get_setting

    assert get_setting(db_session, "telegram_bot_token") == "123:abc"
    # and redacted in API
    settings = auth_client.get("/api/v1/settings").json()
    assert settings["telegram_bot_token_configured"] is True
    assert "telegram_bot_token" not in settings


def test_refuses_default_secrets(monkeypatch):
    from app.config import AppConfig

    access = {"cf_access_team_domain": "t.cloudflareaccess.com", "cf_access_aud": "aud",
              "cf_access_allowed_emails": "me@example.com"}
    cfg = AppConfig(app_secret_key="changeme", **access)
    with pytest.raises(RuntimeError, match="APP_SECRET_KEY"):
        cfg.validate_secrets()
    AppConfig(app_secret_key="strong-key", **access).validate_secrets()


def test_status_exposes_classifier_state(client, db_session):
    from app.models import Email

    body = client.get("/api/v1/status").json()
    assert body["classifier"] == {"running": False, "pending_emails": 0}
    db_session.add(Email(gmail_message_id="cp1", status="pending"))
    db_session.commit()
    assert client.get("/api/v1/status").json()["classifier"]["pending_emails"] == 1
