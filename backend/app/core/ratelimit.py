"""In-memory sliding-window rate limiting for the authentication endpoints.

WHAT THIS IS FOR. Pairing codes and refresh tokens are random 192-256 bit values, so guessing one is computationally
infeasible; the limiter does not make them secure. It exists to (a) cap the noise and database load a single client can
generate against the unauthenticated endpoints, (b) make a guessing attempt visible (a lockout is logged), and (c) slow a
shoulder-surfer or a script that has only a *partial* code. The real protection is entropy + single use + short lifetime
(docs/AUTHENTICATION.md).

TWO BACKENDS (COMPASS_AUTH_RATE_LIMIT_BACKEND):
  * "memory" (default): `AuthRateLimiter`, fastest, no database writes, but state lives in ONE process.
  * "database": `DatabaseAuthRateLimiter`, the same budgets counted in a table (`auth_throttle_events`), so every worker and
    instance that shares the database enforces ONE limit. Recommended for any deployment with more than one process. It is
    approximate under concurrency (two simultaneous requests can both pass a check just below the limit) and depends on the
    clocks of the instances being roughly in step; it costs a few short queries per auth request.

LIMITATIONS of the memory backend (documented in docs/AUTHENTICATION.md):
  * State lives in the memory of ONE process. With N worker processes or N instances each has its own counters, so the
    effective limit is up to N times higher, and a restart forgets everything. A shared store (Redis, or the reverse
    proxy / API gateway) is the upgrade path; it is not implemented.
  * The key is the client IP. Behind a proxy it is only the real client if the proxy is trusted
    (`COMPASS_TRUSTED_PROXY_HOPS`); otherwise every user shares the proxy's address (one attacker can lock everyone out)
    or a spoofed X-Forwarded-For can dodge the limit. Many clients behind one NAT share a budget.
  * Attackers with many IP addresses are only limited by the global attempt ceiling, which is deliberately generous so that
    a botnet cannot turn the limiter into a denial of service for legitimate users.
Nothing here ever sees, stores or logs a credential: only the client address and counts.
"""

import ipaddress
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from collections import OrderedDict, deque
from typing import Callable

logger = logging.getLogger("compass.auth.ratelimit")


class SlidingWindow:
    """At most `limit` events per `window` seconds per key; memory bounded by `max_keys` (oldest keys are evicted)."""

    def __init__(self, limit: int, window: float, clock: Callable[[], float], max_keys: int = 10_000) -> None:
        self.limit, self.window, self._clock, self._max_keys = limit, window, clock, max_keys
        self._events: "OrderedDict[str, deque[float]]" = OrderedDict()

    def _prune(self, key: str, now: float) -> "deque[float] | None":
        q = self._events.get(key)
        if q is None:
            return None
        while q and q[0] <= now - self.window:
            q.popleft()
        if not q:
            del self._events[key]
            return None
        return q

    def retry_after(self, key: str) -> float | None:
        """Seconds until one more event would be allowed, or None if it is allowed now."""
        now = self._clock()
        q = self._prune(key, now)
        if q is not None and len(q) >= self.limit:
            return max(0.001, q[0] + self.window - now)
        return None

    def add(self, key: str) -> None:
        now = self._clock()
        q = self._prune(key, now)
        if q is None:
            q = self._events[key] = deque()
            while len(self._events) > self._max_keys:
                self._events.popitem(last=False)
        else:
            self._events.move_to_end(key)
        q.append(now)

    def count(self, key: str) -> int:
        q = self._prune(key, self._clock())
        return len(q) if q else 0


class AuthRateLimiter:
    """Three budgets, all consulted BEFORE a code/token is even looked up (so a blocked client learns nothing):

    * per-client attempts per minute (every request),
    * per-client FAILED attempts per window (a lockout after repeated wrong codes/tokens),
    * a global attempts-per-minute ceiling that protects the database.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        attempts_per_minute: int = 20,
        max_failures: int = 5,
        failure_window_seconds: float = 900.0,
        global_attempts_per_minute: int = 600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.enabled = enabled
        self._lock = threading.Lock()
        self._attempts = SlidingWindow(attempts_per_minute, 60.0, clock)
        self._failures = SlidingWindow(max_failures, failure_window_seconds, clock)
        self._global = SlidingWindow(global_attempts_per_minute, 60.0, clock, max_keys=16)
        self._clock = clock
        self._logged: dict[str, float] = {}

    @staticmethod
    def _key(scope: str, client: str) -> str:
        return f"{scope}|{client}"

    def check_and_count(self, scope: str, client: str) -> float | None:
        """Call at the start of a request. Returns seconds to wait if the client must be refused, else counts the attempt."""
        if not self.enabled:
            return None
        key = self._key(scope, client)
        with self._lock:
            wait = self._failures.retry_after(key)
            kind = "failures"
            if wait is None:
                wait, kind = self._attempts.retry_after(key), "attempts"
            if wait is None:
                wait, kind = self._global.retry_after(scope), "global"
            if wait is not None:
                self._log_refusal(scope, client, kind, wait)
                return wait
            self._attempts.add(key)
            self._global.add(scope)
            return None

    def _log_refusal(self, scope: str, client: str, kind: str, wait: float) -> None:
        """One warning per (scope, client, budget) per minute: an attacker must not be able to flood the log through the limiter."""
        now = self._clock()
        key = f"{scope}|{client}|{kind}"
        if now - self._logged.get(key, -1e9) >= 60.0:
            self._logged[key] = now
            if len(self._logged) > 10_000:
                self._logged.clear()
            logger.warning("auth rate limit: scope=%s client=%s budget=%s retry_after=%.0fs (further refusals this minute are not logged)",
                           scope, client, kind, wait)

    def record_failure(self, scope: str, client: str) -> None:
        """Call when the code/token was rejected. Only failures count towards the lockout."""
        if not self.enabled:
            return
        with self._lock:
            key = self._key(scope, client)
            self._failures.add(key)
            if self._failures.count(key) == self._failures.limit:
                logger.warning("auth rate limit: scope=%s client=%s reached %d failures; locked out", scope, client, self._failures.limit)


class DatabaseAuthRateLimiter:
    """Same budgets and behaviour as AuthRateLimiter, counted in the database so all workers share them.

    `session_factory` must return a NEW session each call; every operation uses its own short transaction, so a failure
    recorded here survives the rollback of the request that caused it. `clock` returns epoch seconds (injectable for tests).
    """

    def __init__(
        self,
        session_factory: Callable[[], "object"],
        *,
        enabled: bool = True,
        attempts_per_minute: int = 20,
        max_failures: int = 5,
        failure_window_seconds: float = 900.0,
        global_attempts_per_minute: int = 600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.enabled = enabled
        self._factory = session_factory
        self._attempts, self._max_failures, self._window = attempts_per_minute, max_failures, failure_window_seconds
        self._global = global_attempts_per_minute
        self._clock = clock
        self._logged: dict[str, float] = {}
        self._last_cleanup = -1e12

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), tz=timezone.utc)

    @staticmethod
    def _aware(dt: datetime) -> datetime:
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # SQLite returns naive datetimes

    def _cleanup(self, db, now: datetime) -> None:
        """Delete rows older than the longest window, at most once a minute per process."""
        from sqlalchemy import delete

        from app.db.models import AuthThrottleEvent

        if self._clock() - self._last_cleanup < 60:
            return
        self._last_cleanup = self._clock()
        db.execute(delete(AuthThrottleEvent).where(AuthThrottleEvent.created_at < now - timedelta(seconds=max(self._window, 60.0) + 60)))

    def _wait_for(self, db, scope: str, client: str, kind: str, limit: int, window: float, now: datetime,
                  per_client: bool = True) -> float | None:
        from sqlalchemy import func, select

        from app.db.models import AuthThrottleEvent

        cond = [AuthThrottleEvent.scope == scope, AuthThrottleEvent.kind == kind,
                AuthThrottleEvent.created_at > now - timedelta(seconds=window)]
        if per_client:
            cond.append(AuthThrottleEvent.client == client)
        count, oldest = db.execute(select(func.count(), func.min(AuthThrottleEvent.created_at)).where(*cond)).one()
        if count >= limit and oldest is not None:
            return max(0.001, (self._aware(oldest) + timedelta(seconds=window) - now).total_seconds())
        return None

    def check_and_count(self, scope: str, client: str) -> float | None:
        if not self.enabled:
            return None
        from app.db.models import AuthThrottleEvent

        with self._factory() as db:
            now = self._now()
            self._cleanup(db, now)
            wait, kind = self._wait_for(db, scope, client, "failure", self._max_failures, self._window, now), "failures"
            if wait is None:
                wait, kind = self._wait_for(db, scope, client, "attempt", self._attempts, 60.0, now), "attempts"
            if wait is None:
                wait, kind = self._wait_for(db, scope, client, "attempt", self._global, 60.0, now, per_client=False), "global"
            if wait is None:
                db.add(AuthThrottleEvent(scope=scope, client=client[:64], kind="attempt", created_at=now))
            db.commit()
        if wait is not None:
            AuthRateLimiter._log_refusal(self, scope, client, kind, wait)  # same once-per-minute logging
        return wait

    def record_failure(self, scope: str, client: str) -> None:
        if not self.enabled:
            return
        from sqlalchemy import func, select

        from app.db.models import AuthThrottleEvent

        with self._factory() as db:
            now = self._now()
            db.add(AuthThrottleEvent(scope=scope, client=client[:64], kind="failure", created_at=now))
            db.commit()
            failures = db.execute(select(func.count()).where(
                AuthThrottleEvent.scope == scope, AuthThrottleEvent.client == client, AuthThrottleEvent.kind == "failure",
                AuthThrottleEvent.created_at > now - timedelta(seconds=self._window))).scalar_one()
        if failures == self._max_failures:
            logger.warning("auth rate limit: scope=%s client=%s reached %d failures; locked out", scope, client, self._max_failures)


def client_address(peer: str | None, forwarded_for: str | None, trusted_hops: int) -> str:
    """The address used as the rate-limit key.

    With `trusted_hops` == 0 (default) the TCP peer is used and X-Forwarded-For is ignored (it is client-controlled).
    With N > 0 the entry N positions from the END of X-Forwarded-For is used: that is the address the outermost trusted
    proxy saw, and the part of the header an attacker can prepend is ignored. Invalid values fall back to the peer.
    """
    fallback = peer or "unknown"
    if trusted_hops <= 0 or not forwarded_for:
        return fallback
    parts = [p.strip() for p in forwarded_for.split(",") if p.strip()]
    if len(parts) < trusted_hops:
        return fallback
    candidate = parts[-trusted_hops]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return fallback
