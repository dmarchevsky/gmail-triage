"""Login/logout/session endpoints for the web UI."""

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from app import auth

router = APIRouter(prefix="/auth")


class LoginBody(BaseModel):
    password: str


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
    return {"authenticated": bool(token and auth.session_token_valid(token))}
