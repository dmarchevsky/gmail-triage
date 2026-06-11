import os
import tempfile

import pytest

# Must be set before app modules are imported anywhere in the test session.
_tmpdir = tempfile.mkdtemp(prefix="mailtriage-test-")
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-not-default-1234")
os.environ.setdefault("UI_PASSWORD", "test-password")
os.environ["DATA_DIR"] = _tmpdir
os.environ["DATABASE_URL"] = ""


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient with a fresh SQLite DB per test."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app import auth, config, db

    config.get_config.cache_clear()
    db.reset_engine_for_tests()
    auth._login_attempts.clear()

    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c

    db.reset_engine_for_tests()
    config.get_config.cache_clear()


@pytest.fixture()
def auth_client(client):
    """Client with a logged-in session."""
    resp = client.post("/api/v1/auth/login", json={"password": "test-password"})
    assert resp.status_code == 200
    return client


@pytest.fixture()
def db_session(client):
    """Session bound to the same database as `client`."""
    from app.db import get_sessionmaker

    session = get_sessionmaker()()
    yield session
    session.close()
