"""Cloudflare Access — the app's only identity source.

The UI is published through a cloudflared tunnel behind a Cloudflare Access
application that signs visitors in with Google. Access injects a signed JWT in
the ``Cf-Access-Jwt-Assertion`` header of every request it lets through; this
module verifies it against the team's JWKS and returns the claims. Only the
signature-verified ``email`` claim counts — never trust the plain
``Cf-Access-Authenticated-User-Email`` header, which anything able to reach the
origin can set.

Ported from chore-tracker's ``app/auth/cf_access.py``.
"""

from functools import lru_cache
from typing import Any

import jwt
from starlette.concurrency import run_in_threadpool

from app.config import get_config
from app.logging_setup import get_logger

log = get_logger(__name__)

HEADER = "cf-access-jwt-assertion"


class AccessUnavailable(Exception):
    """Cloudflare's key endpoint could not be reached; the token was never checked."""


class AccessRejected(Exception):
    """The assertion is missing or invalid."""

    status_code = 401


class AccessNotAllowed(AccessRejected):
    """A genuine Google identity that is not in CF_ACCESS_ALLOWED_EMAILS."""

    status_code = 403


def build_jwks_client(team_domain: str) -> jwt.PyJWKClient:
    # PyJWT's default timeout is 30s and this fetch sits in the request path.
    return jwt.PyJWKClient(
        f"https://{team_domain}/cdn-cgi/access/certs",
        cache_keys=True,
        lifespan=600,
        timeout=5,
    )


class AccessVerifier:
    def __init__(self, team_domain: str, audiences: list[str], issuers: list[str],
                 allowed_emails: set[str]) -> None:
        self.team_domain = team_domain
        self.audiences = audiences
        self.issuers = issuers
        self.allowed_emails = allowed_emails
        self.jwks = build_jwks_client(team_domain)

    def _decode(self, token: str) -> dict[str, Any]:
        key = self.jwks.get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=self.audiences,
                            options={"require": ["iss", "aud", "exp"]})
        # Checked here rather than via PyJWT's `issuer=` so more than one is accepted.
        if claims["iss"] not in self.issuers:
            raise jwt.InvalidIssuerError("Invalid issuer")
        return claims

    async def email_for(self, token: str | None) -> str:
        """Verify the assertion and return its allowlisted, lowercased email.

        Raises AccessRejected / AccessNotAllowed (the visitor's problem) or AccessUnavailable
        (server's problem: 503).
        """
        if not token:
            raise AccessRejected("Cloudflare Access authentication required")
        try:
            # PyJWKClient does blocking network I/O on a cache miss; keep it off the
            # event loop the queue/poller tasks share.
            claims = await run_in_threadpool(self._decode, token)
        except jwt.PyJWKClientConnectionError as exc:
            # MUST stay above the PyJWTError arm: it is a subclass of it.
            log.error("cf_access_jwks_unavailable", jwks_url=self.jwks.uri, error=str(exc))
            raise AccessUnavailable(str(exc)) from exc
        except jwt.PyJWTError as exc:
            unverified = _unverified(token)
            log.warning("cf_access_rejected", error=str(exc),
                        token_iss=unverified.get("iss"), token_aud=unverified.get("aud"),
                        expected_iss=self.issuers, expected_aud=self.audiences)
            raise AccessRejected(f"Invalid Cloudflare Access token: {exc}") from exc
        email = claims.get("email")
        email = email.strip().lower() if isinstance(email, str) else ""
        if not email or email not in self.allowed_emails:
            log.warning("cf_access_email_not_allowed", email=email or None)
            raise AccessNotAllowed(f"{email or 'This account'} is not allowed to use MailTriage")
        return email

    def logout_url(self, return_to: str = "") -> str:
        # Must be the TEAM domain: the app host's own /cdn-cgi/access/logout
        # returns a bare page and clears nothing.
        url = f"https://{self.team_domain}/cdn-cgi/access/logout"
        return f"{url}?returnTo={return_to}" if return_to else url

    def warm(self) -> None:
        """Fetch the key set once at startup. Never raises — the app must start regardless."""
        try:
            self.jwks.get_jwk_set()
        except Exception as exc:
            log.error("cf_access_jwks_unavailable_at_startup", jwks_url=self.jwks.uri,
                      error=str(exc))
        else:
            log.info("cf_access_jwks_ready", jwks_url=self.jwks.uri)


@lru_cache
def get_verifier() -> AccessVerifier | None:
    """The verifier for the configured team, or None in DEV_AUTH mode."""
    cfg = get_config()
    if cfg.dev_auth:
        return None
    return AccessVerifier(cfg.cf_access_team_domain, cfg.cf_access_audiences,
                          cfg.cf_access_issuers, cfg.allowed_emails)


def _unverified(token: str) -> dict[str, Any]:
    """The token's claims WITHOUT verification. Diagnostics only — never trusted."""
    try:
        return jwt.decode(token, options={"verify_signature": False})
    except Exception:
        return {}
