"""Login/logout/session and password-management endpoints for the web UI."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import auth
from app.db import get_session
from app.services import settings_service
from app.services.audit import audit
from app.state import app_state

router = APIRouter(prefix="/auth")


class LoginBody(BaseModel):
    password: str


class ChangePasswordBody(BaseModel):
    current_password: str = ""
    new_password: str


class CurrentPasswordBody(BaseModel):
    current_password: str = ""


@router.post("/login")
def login(body: LoginBody, response: Response) -> dict:
    if auth.login_rate_limited():
        raise HTTPException(status_code=429, detail="Too many login attempts; wait a minute")
    auth.record_login_attempt()
    if not auth.check_password(body.password):
        raise HTTPException(status_code=401, detail="Invalid password")
    auth.set_session_cookie(response)
    return {"ok": True}


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(auth.SESSION_COOKIE)
    return {"ok": True}


@router.get("/session")
def session_info(request: Request) -> dict:
    token = request.cookies.get(auth.SESSION_COOKIE)
    authenticated = app_state.auth_disabled or bool(token and auth.session_token_valid(token))
    return {"authenticated": authenticated, "auth_disabled": app_state.auth_disabled}


@router.put("/password")
def change_password(body: ChangePasswordBody,
                    session: Session = Depends(get_session)) -> dict:
    if not body.new_password.strip():
        raise HTTPException(status_code=400, detail="New password must not be empty")
    # Verify the current password only when one is actually active.
    if auth.password_is_set() and not auth.check_password(body.current_password):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    settings_service.set_setting(session, "ui_password_hash",
                                 auth.hash_password(body.new_password))
    settings_service.set_setting(session, "auth_disabled", False)
    audit(session, "user", "password_changed")
    session.commit()
    auth.load_auth_state(session)
    return {"ok": True}


@router.post("/disable")
def disable_auth(body: CurrentPasswordBody,
                 session: Session = Depends(get_session)) -> dict:
    if auth.password_is_set() and not auth.check_password(body.current_password):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    settings_service.set_setting(session, "auth_disabled", True)
    audit(session, "user", "auth_disabled")
    session.commit()
    auth.load_auth_state(session)
    return {"ok": True}


@router.post("/enable")
def enable_auth(session: Session = Depends(get_session)) -> dict:
    if not auth.password_is_set():
        raise HTTPException(status_code=400,
                            detail="Set a password before re-enabling authentication")
    settings_service.set_setting(session, "auth_disabled", False)
    audit(session, "user", "auth_enabled")
    session.commit()
    auth.load_auth_state(session)
    return {"ok": True}
