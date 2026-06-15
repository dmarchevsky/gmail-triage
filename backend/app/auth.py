"""Single-user auth: session cookie (login form) or HTTP Basic fallback.

The active password is the DB-stored `ui_password_hash` (set via the Settings
UI) when present, otherwise the `UI_PASSWORD` env var (bootstrap fallback).
Auth can be disabled at runtime (`auth_disabled` setting). Both are cached in
`app_state` to avoid a DB query per request. Login attempts are rate-limited
in-process. Session cookie is a signed, expiring token (HMAC via Fernet TTL).
"""

import base64
import binascii
import hashlib
import hmac
import secrets
import time

from cryptography.fernet import InvalidToken
from fastapi import Request, Response
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.config import get_config
from app.services import settings_service
from app.state import app_state

PBKDF2_ALGORITHM = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 240_000

SESSION_COOKIE = "mailtriage_session"
SESSION_TTL_SECONDS = 12 * 3600
LOGIN_RATE_LIMIT = 5  # attempts
LOGIN_RATE_WINDOW = 60  # seconds

# Paths reachable without auth: health/status (docker healthcheck), login, and
# static assets for the login page itself.
PUBLIC_API_PATHS = {"/api/v1/status", "/api/v1/auth/login", "/api/v1/auth/session"}

_login_attempts: list[float] = []


def login_rate_limited() -> bool:
    now = time.monotonic()
    while _login_attempts and now - _login_attempts[0] > LOGIN_RATE_WINDOW:
        _login_attempts.pop(0)
    return len(_login_attempts) >= LOGIN_RATE_LIMIT


def record_login_attempt() -> None:
    _login_attempts.append(time.monotonic())


def hash_password(password: str) -> str:
    """Serialize a salted PBKDF2 hash as ``pbkdf2_sha256$<iters>$<salt>$<hash>``."""
    salt = secrets.token_hex(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(),
                                  PBKDF2_ITERATIONS).hex()
    return f"{PBKDF2_ALGORITHM}${PBKDF2_ITERATIONS}${salt}${derived}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations, salt, derived = stored_hash.split("$")
        if algorithm != PBKDF2_ALGORITHM:
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(),
                                        int(iterations)).hex()
        return hmac.compare_digest(candidate, derived)
    except (ValueError, AttributeError):
        return False


def load_auth_state(session: Session) -> None:
    """Refresh the cached auth state in ``app_state`` from the DB."""
    app_state.auth_disabled = bool(settings_service.get_setting(session, "auth_disabled"))
    app_state.ui_password_hash = settings_service.get_setting(session, "ui_password_hash") or None


def password_is_set() -> bool:
    """Whether any active password exists (DB hash or env fallback)."""
    if app_state.ui_password_hash:
        return True
    return bool(get_config().ui_password)


def check_password(password: str) -> bool:
    if app_state.ui_password_hash:
        return verify_password(password, app_state.ui_password_hash)
    expected = get_config().ui_password
    return hmac.compare_digest(password.encode(), expected.encode())


def issue_session_token() -> str:
    return get_config().fernet().encrypt(b"mailtriage-session").decode()


def session_token_valid(token: str) -> bool:
    try:
        get_config().fernet().decrypt(token.encode(), ttl=SESSION_TTL_SECONDS)
        return True
    except (InvalidToken, ValueError):
        return False


def set_session_cookie(response: Response) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        issue_session_token(),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
    )


def _basic_auth_ok(header: str) -> bool:
    try:
        scheme, _, payload = header.partition(" ")
        if scheme.lower() != "basic":
            return False
        decoded = base64.b64decode(payload).decode()
        _user, _, password = decoded.partition(":")
        return check_password(password)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return False


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)  # static UI shell; API enforces auth
        if app_state.auth_disabled:
            return await call_next(request)  # auth-less mode (opt-in via Settings)
        if path in PUBLIC_API_PATHS:
            return await call_next(request)
        token = request.cookies.get(SESSION_COOKIE)
        if token and session_token_valid(token):
            return await call_next(request)
        basic = request.headers.get("Authorization", "")
        if basic and _basic_auth_ok(basic):
            return await call_next(request)
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
