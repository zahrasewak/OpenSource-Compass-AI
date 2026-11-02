"""Per-request cancellation and time budget, visible to worker threads.

Why this exists (Stage 15, finding from Stage 14): the API endpoints are synchronous and run in a thread pool; the GitHub
client is synchronous too. When a browser gives up (its own timeout) or the user navigates away, the connection closes,
but nothing told the worker thread, so it kept calling GitHub (spending the shared rate limit) until it finished.

Mechanism: an ASGI middleware (app/core/middleware.py) creates one RequestContext per HTTP request, stores it in a
ContextVar (which anyio copies into the worker thread) and sets `cancel` when the client disconnects. Long-running code
calls `GitHubClient._checkpoint()` between outbound requests and while waiting, which raises `RequestCancelled` or
`GitHubDeadlineError` instead of continuing.

What it does NOT do: it cannot abort an HTTP request that is already in flight (at most one request, itself bounded by the
remaining time budget, finishes after a disconnect), and it does not cancel work other requests are waiting for (see
`add_shield`). There are no durable background jobs in this application today; if one is added it must not run under a
request context, or it would be cancelled together with the request that started it.
"""

import contextvars
import threading
import time
from typing import Callable


class RequestCancelled(Exception):
    """The client went away; the remaining work for this request is abandoned (nothing was written)."""


class RequestContext:
    def __init__(self, deadline_seconds: float | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.cancel = threading.Event()
        self._clock = clock
        self.deadline = clock() + deadline_seconds if deadline_seconds else None
        self._shields: list[Callable[[], bool]] = []
        self._lock = threading.Lock()

    def remaining(self) -> float | None:
        """Seconds of time budget left (None = no budget)."""
        return None if self.deadline is None else self.deadline - self._clock()

    def expired(self) -> bool:
        r = self.remaining()
        return r is not None and r <= 0

    def add_shield(self, fn: Callable[[], bool]) -> None:
        """While any shield returns True the disconnect is ignored: other requests are waiting for this work (single-flight)."""
        with self._lock:
            self._shields.append(fn)

    def remove_shield(self, fn: Callable[[], bool]) -> None:
        with self._lock:
            if fn in self._shields:
                self._shields.remove(fn)

    def cancelled(self) -> bool:
        if not self.cancel.is_set():
            return False
        with self._lock:
            shields = list(self._shields)
        return not any(s() for s in shields)


_current: contextvars.ContextVar[RequestContext | None] = contextvars.ContextVar("compass_request_context", default=None)


def current() -> RequestContext | None:
    return _current.get()


def activate(ctx: RequestContext) -> contextvars.Token:
    return _current.set(ctx)


def deactivate(token: contextvars.Token) -> None:
    _current.reset(token)
