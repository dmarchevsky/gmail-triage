"""Single-user auth via Cloudflare Access (see app/cf_access.py).

Every /api/ request except the public paths must carry a valid Access JWT whose
email claim is in CF_ACCESS_ALLOWED_EMAILS. With DEV_AUTH (local development
only, mutually exclusive with CF_ACCESS_*) every request is let through.
"""

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app import cf_access

DEV_EMAIL = "dev@localhost"

# Paths reachable without auth: status (docker healthcheck) and the session
# probe the UI uses to decide what to render.
PUBLIC_API_PATHS = {"/api/v1/status", "/api/v1/auth/session"}


async def authenticate(request: Request) -> str:
    """The signed-in email for this request. Raises cf_access.AccessRejected /
    cf_access.AccessUnavailable."""
    verifier = cf_access.get_verifier()
    if verifier is None:
        return DEV_EMAIL
    return await verifier.email_for(request.headers.get(cf_access.HEADER))


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)  # static UI shell; Access gates it at the edge
        if path in PUBLIC_API_PATHS:
            return await call_next(request)
        try:
            request.state.user_email = await authenticate(request)
        except cf_access.AccessUnavailable:
            return JSONResponse(
                {"detail": "MailTriage cannot reach Cloudflare right now to check your "
                           "sign-in. This is a server-side problem; try again shortly."},
                status_code=503, headers={"Retry-After": "30"})
        except cf_access.AccessRejected as exc:
            return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)
        return await call_next(request)
