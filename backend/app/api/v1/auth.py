"""Authentication routes: pairing-code exchange, refresh, logout, identity.

Bearer access tokens are short-lived and bound to a revocable server-side session. A client is signed in by exchanging a
one-time pairing code (issued by an administrator with `python -m app.cli issue-pairing-code`).
"""

import math
from datetime import datetime

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import bearer_scheme, get_current_user
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.ratelimit import AuthRateLimiter, client_address
from app.core.security import AuthConfigError, InvalidToken, verify_claims
from app.db.models import User
from app.db.session import get_db
from app.services import sessions as service

router = APIRouter(prefix="/auth", tags=["auth"])

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}  # token responses must never be cached


class ExchangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # No minimum length: a too-short code gets the same generic 401 as any other bad code (no format oracle).
    code: str = Field(min_length=1, max_length=200)
    client_label: str | None = Field(default=None, max_length=60)
    # True when the client will keep the refresh token on disk: the server then applies the tighter persistent limits.
    persistent: bool = False


class RefreshIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=1, max_length=200)


class LogoutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str | None = Field(default=None, min_length=1, max_length=200)


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int  # access token lifetime, seconds
    refresh_expires_in: int
    session_expires_at: datetime


def _throttle(request: Request, scope: str, settings: Settings) -> tuple[AuthRateLimiter, str]:
    """Refuse an over-limit client BEFORE any code/token is looked up, so a blocked client learns nothing about validity."""
    limiter: AuthRateLimiter = request.app.state.auth_limiter
    who = client_address(request.client.host if request.client else None, request.headers.get("x-forwarded-for"),
                         settings.trusted_proxy_hops)
    wait = limiter.check_and_count(scope, who)
    if wait is not None:
        seconds = max(1, math.ceil(wait))
        raise AppError("rate_limited", "Too many attempts. Try again later.", 429,
                       headers={"Retry-After": str(seconds), **NO_STORE}, details={"retry_after_seconds": seconds})
    return limiter, who


def _out(pair: service.TokenPair, response: Response) -> TokenOut:
    for k, v in NO_STORE.items():
        response.headers[k] = v
    return TokenOut(access_token=pair.access_token, refresh_token=pair.refresh_token, expires_in=pair.expires_in,
                    refresh_expires_in=pair.refresh_expires_in, session_expires_at=pair.session_expires_at)


@router.post("/exchange", response_model=TokenOut)
def exchange(body: ExchangeIn, request: Request, response: Response, db: Session = Depends(get_db),
             settings: Settings = Depends(get_settings)):
    """Trade a one-time pairing code for an access token and a refresh token. Rate limited per client address."""
    limiter, who = _throttle(request, "exchange", settings)
    try:
        return _out(service.exchange_pairing_code(db, settings, body.code, body.client_label, body.persistent), response)
    except AppError as exc:
        if exc.status_code == 401:
            limiter.record_failure("exchange", who)
        raise
    except AuthConfigError:
        raise AppError("auth_unavailable", "Authentication is not configured on the server", 503)


@router.post("/refresh", response_model=TokenOut)
def refresh(body: RefreshIn, request: Request, response: Response, db: Session = Depends(get_db),
            settings: Settings = Depends(get_settings)):
    """Rotate the refresh token and get a new access token. Reusing an old refresh token revokes the session."""
    limiter, who = _throttle(request, "refresh", settings)
    try:
        return _out(service.refresh_session(db, settings, body.refresh_token), response)
    except AppError as exc:
        if exc.status_code == 401:
            limiter.record_failure("refresh", who)
        raise
    except AuthConfigError:
        raise AppError("auth_unavailable", "Authentication is not configured on the server", 503)


@router.post("/logout", status_code=204)
def logout(
    request: Request,
    body: LogoutIn | None = None,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """End the session. Works with the refresh token (so it still works after the access token expired) or the bearer token.
    The reply is the same whether or not a session was found. Attempts are rate limited per client address."""
    _throttle(request, "logout", settings)
    if body and body.refresh_token:
        service.revoke_by_refresh_token(db, body.refresh_token)
        return Response(status_code=204, headers=NO_STORE)
    if credentials is None:
        raise AppError("unauthorized", "Missing credentials", 401)
    try:
        _, sid = verify_claims(credentials.credentials, settings)
    except AuthConfigError:
        raise AppError("auth_unavailable", "Authentication is not configured on the server", 503)
    except InvalidToken:
        raise AppError("unauthorized", "Invalid or expired token", 401)
    if sid:
        service.revoke_by_sid(db, sid)
    return Response(status_code=204, headers=NO_STORE)


@router.post("/logout-all", status_code=204)
def logout_all(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Revoke every session of the authenticated user (all browsers/devices)."""
    service.revoke_all_for_user(db, user.id)
    return Response(status_code=204, headers=NO_STORE)


@router.get("/me")
def me(user: User = Depends(get_current_user)) -> dict:
    """Return the authenticated user's identity."""
    return {"id": user.id, "github_user_id": user.github_user_id, "username": user.username}
