"""Signed bearer tokens (JWT, HS256).

Tokens carry only the internal user id (`sub`). Identity is established server-side when the
token is issued; the client can never choose which user it is. A future GitHub OAuth flow
only needs to call `issue_token` after verifying the GitHub identity.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import jwt

from app.core.config import Settings

ALGORITHM = "HS256"
MIN_SECRET_LENGTH = 32


class AuthConfigError(RuntimeError):
    """The server is not configured to handle tokens."""


class InvalidToken(Exception):
    """Token is malformed, badly signed, expired or otherwise unacceptable."""


def _key(settings: Settings) -> str:
    secret = settings.auth_secret.get_secret_value() if settings.auth_secret else ""
    if len(secret) < MIN_SECRET_LENGTH:
        raise AuthConfigError(f"COMPASS_AUTH_SECRET must be set and at least {MIN_SECRET_LENGTH} characters")
    return secret


def issue_token(user_id: int, settings: Settings, ttl: timedelta | None = None) -> str:
    """Create a signed token for `user_id`."""
    now = datetime.now(timezone.utc)
    ttl = ttl if ttl is not None else timedelta(minutes=settings.auth_token_ttl_minutes)
    claims = {
        "sub": str(user_id),
        "iss": settings.auth_issuer,
        "aud": settings.auth_audience,
        "iat": now,
        "exp": now + ttl,
    }
    return jwt.encode(claims, _key(settings), algorithm=ALGORITHM)


def verify_token(token: str, settings: Settings) -> int:
    """Return the user id in a valid token; raise InvalidToken otherwise."""
    key = _key(settings)
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=[ALGORITHM],  # pinned: rejects "none" and algorithm confusion
            issuer=settings.auth_issuer,
            audience=settings.auth_audience,
            options={"require": ["sub", "exp", "iat", "iss", "aud"]},
        )
        return int(claims["sub"])
    except (jwt.PyJWTError, ValueError) as exc:
        raise InvalidToken(type(exc).__name__) from exc


def new_secret(nbytes: int = 32) -> str:
    """A random, URL-safe secret (>= 256 bits by default) for refresh tokens and pairing codes."""
    return secrets.token_urlsafe(nbytes)


def hash_secret(value: str) -> str:
    """SHA-256 of a high-entropy random secret. Fast hashing is appropriate because the input is not a
    human-chosen password; only this hash is ever stored."""
    return hashlib.sha256(value.encode()).hexdigest()


def issue_access_token(user_id: int, sid: str, settings: Settings, ttl: timedelta | None = None) -> str:
    """Short-lived access token bound to server-side session `sid` (so revoking the session ends it)."""
    now = datetime.now(timezone.utc)
    ttl = ttl if ttl is not None else timedelta(seconds=settings.auth_access_ttl_seconds)
    claims = {"sub": str(user_id), "sid": sid, "typ": "access", "iss": settings.auth_issuer,
              "aud": settings.auth_audience, "iat": now, "exp": now + ttl}
    return jwt.encode(claims, _key(settings), algorithm=ALGORITHM)


def verify_claims(token: str, settings: Settings) -> tuple[int, str | None]:
    """Return (user_id, session id or None for a legacy token); raise InvalidToken otherwise."""
    key = _key(settings)
    try:
        claims = jwt.decode(token, key, algorithms=[ALGORITHM], issuer=settings.auth_issuer, audience=settings.auth_audience,
                            options={"require": ["sub", "exp", "iat", "iss", "aud"]})
        sid = claims.get("sid")
        if sid is not None and (not isinstance(sid, str) or claims.get("typ") != "access"):
            raise ValueError("malformed session claims")
        return int(claims["sub"]), sid
    except (jwt.PyJWTError, ValueError) as exc:
        raise InvalidToken(type(exc).__name__) from exc
