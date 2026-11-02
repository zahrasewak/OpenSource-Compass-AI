"""Keyed single-flight locks: only one request at a time refreshes a given resource.

When several requests need the same stale repository/issue/analysis at once (a browser opening an issue page fires
match, difficulty, health ... together), the first one fetches from GitHub while the rest wait, then re-read the
database and find fresh data, so GitHub is called once instead of once per request.

Scope: threads in ONE process. Several worker processes would still each fetch once; a shared (e.g. Redis) lock is the
upgrade path and is not implemented.
"""

import threading
from contextlib import contextmanager
from typing import Hashable, Iterator

from app.core import requestctx
from app.services.github.errors import GitHubDeadlineError


class KeyedLocks:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[Hashable, list] = {}  # key -> [lock, number of users]

    def waiters(self, key: Hashable) -> int:
        """Requests currently queued behind the holder of `key`."""
        with self._guard:
            entry = self._locks.get(key)
            return max(0, entry[1] - 1) if entry else 0

    def _forget(self, key: Hashable, entry: list) -> None:
        with self._guard:
            entry[1] -= 1
            if entry[1] == 0:
                self._locks.pop(key, None)  # no leak: idle keys are forgotten

    @contextmanager
    def hold(self, key: Hashable) -> Iterator[None]:
        """Exclusive access for `key`. Inside a request, waiting is abandoned if the client disconnects or the time budget
        runs out, and while others wait for the holder's result the holder is shielded from disconnect-cancellation."""
        with self._guard:
            entry = self._locks.setdefault(key, [threading.Lock(), 0])
            entry[1] += 1
        ctx = requestctx.current()
        if ctx is None:
            entry[0].acquire()
        else:
            while not entry[0].acquire(timeout=0.25):  # a waiter that is cancelled or out of time stops waiting
                if ctx.cancelled():
                    self._forget(key, entry)
                    raise requestctx.RequestCancelled()
                if ctx.expired():
                    self._forget(key, entry)
                    raise GitHubDeadlineError("the request's time budget ran out while waiting for another request's work")
        shield = None
        if ctx is not None:
            shield = lambda: self.waiters(key) > 0  # noqa: E731
            ctx.add_shield(shield)
        try:
            yield
        finally:
            if ctx is not None and shield is not None:
                ctx.remove_shield(shield)
            entry[0].release()
            self._forget(key, entry)

    def __len__(self) -> int:
        return len(self._locks)


locks = KeyedLocks()
