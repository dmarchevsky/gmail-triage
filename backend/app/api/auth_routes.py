"""Session probe and logout for the web UI (identity comes from Cloudflare Access)."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app import auth, cf_access
from app.db import get_session
from app.services import settings_service

router = APIRouter(prefix="/auth")


def _logout_url(session: Session) -> str | None:
    verifier = cf_access.get_verifier()
    if verifier is None:
        return None
    base = (settings_service.get_setting(session, "public_base_url") or "").rstrip("/")
    return verifier.logout_url(f"{base}/" if base else "")


@router.get("/session")
async def session_info(request: Request, session: Session = Depends(get_session)):
    """Public: tells the UI who is signed in, or why nobody is."""
    mode = "dev" if cf_access.get_verifier() is None else "cf_access"
    body = {"authenticated": False, "email": None, "mode": mode,
            "logout_url": _logout_url(session)}
    try:
        body.update(authenticated=True, email=await auth.authenticate(request))
    except cf_access.AccessUnavailable:
        return JSONResponse({**body, "detail": "Cannot reach Cloudflare to check sign-in"},
                            status_code=503, headers={"Retry-After": "30"})
    except cf_access.AccessRejected as exc:
        return JSONResponse({**body, "detail": str(exc)}, status_code=exc.status_code)
    return body


@router.post("/logout")
def logout(session: Session = Depends(get_session)) -> dict:
    """No app session to clear — the UI navigates to the Access logout URL."""
    return {"ok": True, "logout_url": _logout_url(session)}
