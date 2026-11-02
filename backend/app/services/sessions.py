"""Session-based authentication: pairing codes, short-lived access tokens, rotating refresh tokens, revocation.

Security properties (see docs/AUTHENTICATION.md):
  * pairing codes and refresh tokens are random (>= 192 bits), stored only as SHA-256 hashes, single use / rotated;
  * every state change is a conditional UPDATE, so two concurrent requests cannot both spend the same code or token;
  * presenting an already-rotated refresh token is treated as theft: the whole session is revoked;
  * access tokens are bound to a session (`sid`), so revoking the session invalidates them immediately;
  * failure responses are generic (they do not say whether a code/token existed, expired or was reused);
  * tokens and codes are never logged.
"""

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError
from app.core.security import hash_secret, issue_access_token, new_secret
from app.db.models import AuthSession, PairingCode, User

logger = logging.getLogger("compass.auth")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # SQLite returns naive datetimes


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int
    refresh_expires_in: int
    session_expires_at: datetime


def _invalid_code() -> AppError:
    return AppError("invalid_pairing_code", "The pairing code is invalid, expired or already used.", 401)


def _invalid_refresh() -> AppError:
    return AppError("invalid_refresh_token", "The refresh token is invalid, expired or revoked. Sign in again.", 401)


# ------------------------------------------------------------------ pairing codes


def create_pairing_code(db: Session, user_id: int, settings: Settings, ttl: timedelta | None = None) -> str:
    """Create a one-time code for `user_id`. Returned once; only its hash is stored."""
    now = _now()
    db.execute(delete(PairingCode).where(PairingCode.expires_at < now - timedelta(days=1)))  # housekeeping
    code = new_secret(24)
    db.add(PairingCode(user_id=user_id, code_hash=hash_secret(code),
                       expires_at=now + (ttl or timedelta(minutes=settings.auth_pairing_ttl_minutes))))
    db.commit()
    return code


def exchange_pairing_code(db: Session, settings: Settings, code: str, client_label: str | None,
                          persistent: bool = False) -> TokenPair:
    now = _now()
    row = db.scalar(select(PairingCode).where(PairingCode.code_hash == hash_secret(code)))
    if row is None or row.used_at is not None or _aware(row.expires_at) <= now:
        raise _invalid_code()
    spent = db.execute(update(PairingCode).where(PairingCode.id == row.id, PairingCode.used_at.is_(None)).values(used_at=now))
    if spent.rowcount != 1:  # lost a race with a concurrent exchange of the same code
        db.rollback()
        raise _invalid_code()
    if db.get(User, row.user_id) is None:
        db.rollback()
        raise _invalid_code()
    return _start_session(db, settings, row.user_id, client_label, now, persistent)


def _limits(settings: Settings, persistent: bool) -> tuple[timedelta, timedelta]:
    """(idle refresh lifetime, absolute session lifetime). A refresh token kept on disk gets the tighter pair."""
    if persistent:
        return (timedelta(hours=min(settings.auth_persistent_refresh_ttl_hours, settings.auth_refresh_ttl_hours)),
                timedelta(days=min(settings.auth_persistent_session_max_days, settings.auth_session_max_days)))
    return timedelta(hours=settings.auth_refresh_ttl_hours), timedelta(days=settings.auth_session_max_days)


def _start_session(db: Session, settings: Settings, user_id: int, label: str | None, now: datetime,
                   persistent: bool = False) -> TokenPair:
    refresh = new_secret(32)
    idle, absolute = _limits(settings, persistent)
    expires = now + absolute
    refresh_expires = min(now + idle, expires)
    session = AuthSession(sid=secrets.token_hex(16), user_id=user_id, refresh_hash=hash_secret(refresh),
                          expires_at=expires, refresh_expires_at=refresh_expires, client_label=(label or None), persistent=persistent)
    db.add(session)
    db.commit()
    return _pair(settings, session.user_id, session.sid, refresh, now, refresh_expires, expires)


def _pair(settings: Settings, user_id: int, sid: str, refresh: str, now: datetime, refresh_expires: datetime,
          session_expires: datetime) -> TokenPair:
    access = issue_access_token(user_id, sid, settings)
    return TokenPair(access, refresh, settings.auth_access_ttl_seconds,
                     max(0, int((refresh_expires - now).total_seconds())), session_expires)


# ------------------------------------------------------------------ refresh / revoke / validate


def revoke(db: Session, session: AuthSession, reason: str) -> None:
    if session.revoked_at is None:
        session.revoked_at, session.revoked_reason = _now(), reason
        db.commit()


def refresh_session(db: Session, settings: Settings, refresh_token: str) -> TokenPair:
    """Rotate: the presented refresh token is consumed and a new one issued. Reuse of a consumed token revokes the session."""
    now = _now()
    h = hash_secret(refresh_token)
    session = db.scalar(select(AuthSession).where(or_(AuthSession.refresh_hash == h, AuthSession.prev_refresh_hash == h)))
    if session is None or session.revoked_at is not None:
        raise _invalid_refresh()
    if session.refresh_hash != h:  # a token that was already rotated away: somebody kept a copy
        logger.warning("refresh token reuse detected; revoking session sid=%s", session.sid)
        revoke(db, session, "refresh_reuse")
        raise _invalid_refresh()
    if _aware(session.expires_at) <= now or _aware(session.refresh_expires_at) <= now:
        raise _invalid_refresh()

    new_refresh = new_secret(32)
    refresh_expires = min(now + _limits(settings, session.persistent)[0], _aware(session.expires_at))
    rotated = db.execute(
        update(AuthSession)
        .where(AuthSession.id == session.id, AuthSession.refresh_hash == h, AuthSession.revoked_at.is_(None))
        .values(prev_refresh_hash=h, refresh_hash=hash_secret(new_refresh), last_refreshed_at=now, refresh_expires_at=refresh_expires)
    )
    if rotated.rowcount != 1:  # a concurrent request already rotated this token
        db.rollback()
        logger.warning("concurrent refresh of one token; revoking session sid=%s", session.sid)
        fresh = db.scalar(select(AuthSession).where(AuthSession.id == session.id))
        if fresh is not None:
            revoke(db, fresh, "refresh_race")
        raise _invalid_refresh()
    db.commit()
    return _pair(settings, session.user_id, session.sid, new_refresh, now, refresh_expires, _aware(session.expires_at))


def validate_session(db: Session, sid: str, user_id: int) -> AuthSession | None:
    """The live session an access token claims, or None (unknown, revoked, expired, or someone else's)."""
    session = db.scalar(select(AuthSession).where(AuthSession.sid == sid))
    if session is None or session.user_id != user_id or session.revoked_at is not None or _aware(session.expires_at) <= _now():
        return None
    return session


def revoke_by_sid(db: Session, sid: str, reason: str = "logout") -> bool:
    session = db.scalar(select(AuthSession).where(AuthSession.sid == sid))
    if session is None:
        return False
    revoke(db, session, reason)
    return True


def revoke_by_refresh_token(db: Session, refresh_token: str, reason: str = "logout") -> bool:
    h = hash_secret(refresh_token)
    session = db.scalar(select(AuthSession).where(or_(AuthSession.refresh_hash == h, AuthSession.prev_refresh_hash == h)))
    if session is None:
        return False
    revoke(db, session, reason)
    return True


def revoke_all_for_user(db: Session, user_id: int, reason: str = "logout_all") -> int:
    now = _now()
    result = db.execute(update(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
                        .values(revoked_at=now, revoked_reason=reason))
    db.commit()
    return result.rowcount or 0
