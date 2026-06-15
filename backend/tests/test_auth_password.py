"""Runtime password change and auth-less mode (Settings UI auth controls)."""

import pytest

from app import auth
from app.services import settings_service


def test_hash_password_roundtrip():
    h = auth.hash_password("hunter2")
    assert h.startswith("pbkdf2_sha256$")
    assert auth.verify_password("hunter2", h)
    assert not auth.verify_password("wrong", h)
    assert not auth.verify_password("hunter2", "garbage")


def test_change_password_updates_login(auth_client):
    resp = auth_client.put("/api/v1/auth/password",
                           json={"current_password": "test-password",
                                 "new_password": "brand-new-pass"})
    assert resp.status_code == 200

    # Old password no longer works, new one does.
    assert auth_client.post("/api/v1/auth/login",
                            json={"password": "test-password"}).status_code == 401
    assert auth_client.post("/api/v1/auth/login",
                            json={"password": "brand-new-pass"}).status_code == 200


def test_change_password_wrong_current_rejected(auth_client, db_session):
    resp = auth_client.put("/api/v1/auth/password",
                           json={"current_password": "nope",
                                 "new_password": "brand-new-pass"})
    assert resp.status_code == 401
    # Hash is unchanged (still the env-fallback, i.e. no stored hash).
    assert settings_service.get_setting(db_session, "ui_password_hash") == ""


def test_change_password_empty_rejected(auth_client):
    resp = auth_client.put("/api/v1/auth/password",
                           json={"current_password": "test-password", "new_password": "   "})
    assert resp.status_code == 400


def test_disable_auth_opens_access(auth_client):
    resp = auth_client.post("/api/v1/auth/disable",
                            json={"current_password": "test-password"})
    assert resp.status_code == 200

    # Without a session cookie, protected routes are now reachable.
    auth_client.cookies.clear()
    assert auth_client.get("/api/v1/settings").status_code == 200
    session = auth_client.get("/api/v1/auth/session").json()
    assert session == {"authenticated": True, "auth_disabled": True}


def test_disable_then_enable_restores_auth(auth_client):
    assert auth_client.post("/api/v1/auth/disable",
                            json={"current_password": "test-password"}).status_code == 200
    # Re-enabling is allowed while auth is disabled (middleware bypassed).
    assert auth_client.post("/api/v1/auth/enable").status_code == 200

    auth_client.cookies.clear()
    assert auth_client.get("/api/v1/settings").status_code == 401
    session = auth_client.get("/api/v1/auth/session").json()
    assert session == {"authenticated": False, "auth_disabled": False}


def test_disable_wrong_current_rejected(auth_client):
    resp = auth_client.post("/api/v1/auth/disable", json={"current_password": "nope"})
    assert resp.status_code == 401


def test_update_settings_rejects_protected_keys(db_session):
    for key in ("auth_disabled", "ui_password_hash"):
        with pytest.raises(KeyError):
            settings_service.update_settings(db_session, {key: "x"})


def test_put_settings_endpoint_rejects_protected(auth_client):
    assert auth_client.put("/api/v1/settings",
                           json={"auth_disabled": True}).status_code == 400


def test_rate_limit_applies_after_password_change(auth_client):
    assert auth_client.put("/api/v1/auth/password",
                           json={"current_password": "test-password",
                                 "new_password": "another-pass"}).status_code == 200
    for _ in range(5):
        auth_client.post("/api/v1/auth/login", json={"password": "bad"})
    assert auth_client.post("/api/v1/auth/login",
                            json={"password": "another-pass"}).status_code == 429
