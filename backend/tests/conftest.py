import os
import tempfile
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

# Must be set before app modules are imported anywhere in the test session.
_tmpdir = tempfile.mkdtemp(prefix="mailtriage-test-")
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-not-default-1234")
os.environ["DATA_DIR"] = _tmpdir
os.environ["DATABASE_URL"] = ""
# Cloudflare Access: tokens are signed with ACCESS_KEY below; the JWKS lookup is
# patched in the `client` fixture so tests never touch the network.
TEAM_DOMAIN = "test-team.cloudflareaccess.com"
ACCESS_AUD = "test-aud-tag"
ALLOWED_EMAIL = "me@example.com"
os.environ["CF_ACCESS_TEAM_DOMAIN"] = TEAM_DOMAIN
os.environ["CF_ACCESS_AUD"] = ACCESS_AUD
os.environ["CF_ACCESS_ALLOWED_EMAILS"] = ALLOWED_EMAIL
os.environ.pop("CF_ACCESS_ISSUER", None)
os.environ.pop("DEV_AUTH", None)

ACCESS_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def make_access_token(key=ACCESS_KEY, **overrides) -> str:
    """A Cloudflare Access JWT signed with the test key; None-valued overrides drop a claim."""
    claims = {"iss": f"https://{TEAM_DOMAIN}", "aud": [ACCESS_AUD], "email": ALLOWED_EMAIL,
              "exp": int(time.time()) + 3600, "iat": int(time.time())}
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-kid"})


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient with a fresh SQLite DB per test."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app import cf_access, config, db
    from app.state import AppState, app_state

    config.get_config.cache_clear()
    cf_access.get_verifier.cache_clear()
    db.reset_engine_for_tests()
    app_state.__dict__.update(AppState().__dict__)  # reset module-global state
    monkeypatch.setattr(jwt.PyJWKClient, "get_signing_key_from_jwt",
                        lambda self, token: SimpleNamespace(key=ACCESS_KEY.public_key()))
    monkeypatch.setattr(jwt.PyJWKClient, "get_jwk_set", lambda self, refresh=False: None)

    # Poller: no real sleeps on 404 retries, and keep the catch-up sweep throttled
    # off (tests that exercise it set _last_catchup_at back to None).
    from datetime import UTC, datetime

    from app.services import poller
    monkeypatch.setattr(poller, "NOT_FOUND_RETRY_DELAYS", (0, 0, 0))
    monkeypatch.setattr(poller, "_last_catchup_at", datetime.now(UTC))

    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c

    db.reset_engine_for_tests()
    config.get_config.cache_clear()
    cf_access.get_verifier.cache_clear()


@pytest.fixture()
def auth_client(client):
    """Client whose requests carry a valid Cloudflare Access assertion."""
    client.headers["Cf-Access-Jwt-Assertion"] = make_access_token()
    return client


@pytest.fixture()
def db_session(client):
    """Session bound to the same database as `client`."""
    from app.db import get_sessionmaker

    session = get_sessionmaker()()
    yield session
    session.close()
