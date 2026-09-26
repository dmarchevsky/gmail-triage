"""Cloudflare Access auth: JWT verification, allowlist, public paths, config guards."""

import time
import urllib.parse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.conftest import ACCESS_AUD, TEAM_DOMAIN, make_access_token

HEADER = "Cf-Access-Jwt-Assertion"


def _get(client, token=None, path="/api/v1/settings", **headers):
    if token is not None:
        headers[HEADER] = token
    return client.get(path, headers=headers)


def test_valid_token_accepted(client):
    assert _get(client, make_access_token()).status_code == 200


def test_email_claim_case_insensitive(client):
    assert _get(client, make_access_token(email="Me@Example.COM")).status_code == 200


def test_missing_token_rejected(client):
    resp = _get(client)
    assert resp.status_code == 401
    assert "Cloudflare Access" in resp.json()["detail"]


@pytest.mark.parametrize("overrides", [
    {"aud": ["some-other-app"]},
    {"exp": int(time.time()) - 60},
    {"iss": "https://evil.cloudflareaccess.com"},
    {"iss": None},
    {"exp": None},
])
def test_invalid_claims_rejected(client, overrides):
    assert _get(client, make_access_token(**overrides)).status_code == 401


def test_wrong_signing_key_rejected(client):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert _get(client, make_access_token(key=other)).status_code == 401


def test_garbage_token_rejected(client):
    assert _get(client, "not-a-jwt").status_code == 401


def test_email_not_allowlisted_is_403(client):
    resp = _get(client, make_access_token(email="stranger@example.com"))
    assert resp.status_code == 403
    assert "stranger@example.com" in resp.json()["detail"]


def test_token_without_email_is_403(client):
    assert _get(client, make_access_token(email=None)).status_code == 403


def test_spoofed_email_header_alone_rejected(client):
    resp = _get(client, **{"Cf-Access-Authenticated-User-Email": "me@example.com"})
    assert resp.status_code == 401


def test_multiple_audiences_and_renamed_issuer(client, monkeypatch):
    from app import cf_access, config
    monkeypatch.setenv("CF_ACCESS_AUD", f"other-aud, {ACCESS_AUD}")
    monkeypatch.setenv("CF_ACCESS_ISSUER", "https://old-name.cloudflareaccess.com")
    config.get_config.cache_clear()
    cf_access.get_verifier.cache_clear()
    token = make_access_token(iss="https://old-name.cloudflareaccess.com", aud=["other-aud"])
    assert _get(client, token).status_code == 200
    # the team-domain issuer is no longer accepted once an explicit one is configured
    assert _get(client, make_access_token()).status_code == 401


def test_jwks_outage_is_503_not_403(client, monkeypatch):
    def unreachable(self, token):
        raise jwt.PyJWKClientConnectionError("connection refused")
    monkeypatch.setattr(jwt.PyJWKClient, "get_signing_key_from_jwt", unreachable)
    resp = _get(client, make_access_token())
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "30"


def test_public_paths_need_no_token(client):
    assert client.get("/api/v1/status").status_code == 200
    assert client.get("/api/v1/auth/session").status_code == 401  # answers, not blocked


def test_removed_password_endpoints_gone(auth_client):
    assert auth_client.post("/api/v1/auth/login", json={"password": "x"}).status_code in (
        404, 405)
    assert auth_client.put("/api/v1/auth/password",
                           json={"new_password": "x"}).status_code in (404, 405)


def test_session_reports_identity_and_logout_url(auth_client):
    auth_client.put("/api/v1/settings", json={"public_base_url": "https://mail.example.com/"})
    body = auth_client.get("/api/v1/auth/session").json()
    assert body["authenticated"] is True
    assert body["email"] == "me@example.com"
    assert body["mode"] == "cf_access"
    parsed = urllib.parse.urlparse(body["logout_url"])
    assert parsed.netloc == TEAM_DOMAIN  # team domain, not the app host
    assert parsed.path == "/cdn-cgi/access/logout"
    assert urllib.parse.parse_qs(parsed.query)["returnTo"] == ["https://mail.example.com/"]
    assert auth_client.post("/api/v1/auth/logout").json()["logout_url"] == body["logout_url"]


def test_session_not_allowlisted(client):
    resp = _get(client, make_access_token(email="stranger@example.com"),
                path="/api/v1/auth/session")
    assert resp.status_code == 403
    body = resp.json()
    assert body["authenticated"] is False
    assert body["logout_url"].startswith(f"https://{TEAM_DOMAIN}/")


def test_dev_auth_lets_everything_through(client, monkeypatch):
    from app import cf_access, config
    for name in ("CF_ACCESS_TEAM_DOMAIN", "CF_ACCESS_AUD", "CF_ACCESS_ALLOWED_EMAILS"):
        monkeypatch.delenv(name)
    monkeypatch.setenv("DEV_AUTH", "true")
    config.get_config.cache_clear()
    cf_access.get_verifier.cache_clear()
    assert client.get("/api/v1/settings").status_code == 200
    body = client.get("/api/v1/auth/session").json()
    assert body == {"authenticated": True, "email": "dev@localhost", "mode": "dev",
                    "logout_url": None}


ACCESS = {"cf_access_team_domain": "t.cloudflareaccess.com", "cf_access_aud": "aud",
          "cf_access_allowed_emails": "me@example.com"}


@pytest.mark.parametrize("kwargs, match", [
    ({}, "CF_ACCESS_TEAM_DOMAIN, CF_ACCESS_AUD, CF_ACCESS_ALLOWED_EMAILS"),
    ({**ACCESS, "cf_access_allowed_emails": " , "}, "CF_ACCESS_ALLOWED_EMAILS"),
    ({**ACCESS, "dev_auth": True}, "DEV_AUTH"),
    ({"cf_access_issuer": "https://x", "dev_auth": True}, "DEV_AUTH"),
])
def test_validate_secrets_fails_closed(kwargs, match, monkeypatch):
    from app.config import AppConfig
    for name in ("CF_ACCESS_TEAM_DOMAIN", "CF_ACCESS_AUD", "CF_ACCESS_ALLOWED_EMAILS"):
        monkeypatch.delenv(name)
    with pytest.raises(RuntimeError, match=match):
        AppConfig(app_secret_key="strong-key", **kwargs).validate_secrets()


def test_validate_secrets_accepts_either_mode(monkeypatch):
    from app.config import AppConfig
    for name in ("CF_ACCESS_TEAM_DOMAIN", "CF_ACCESS_AUD", "CF_ACCESS_ALLOWED_EMAILS"):
        monkeypatch.delenv(name)
    AppConfig(app_secret_key="strong-key", **ACCESS).validate_secrets()
    AppConfig(app_secret_key="strong-key", dev_auth=True).validate_secrets()
