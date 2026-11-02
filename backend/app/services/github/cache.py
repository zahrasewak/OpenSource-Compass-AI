"""ETag cache interface and an in-process implementation.

Entries are only ever served after GitHub confirms them with a 304 to a conditional request,
so a stale entry can never be returned; the size bound exists purely to limit memory.
A Redis-backed implementation can be added behind the same protocol (not implemented yet).
"""

import copy
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class CacheEntry:
    etag: str
    data: Any  # parsed JSON body
    next_url: str | None = None  # pagination link of that page, if any


class ETagCache(Protocol):
    def get(self, key: str) -> CacheEntry | None: ...

    def set(self, key: str, entry: CacheEntry) -> None: ...


class InMemoryETagCache:
    """Thread-safe LRU cache. Stores and returns deep copies so callers cannot mutate cached data."""

    def __init__(self, max_entries: int = 1000) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._max = max_entries
        self._items: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> CacheEntry | None:
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            self._items.move_to_end(key)
            return CacheEntry(entry.etag, copy.deepcopy(entry.data), entry.next_url)

    def set(self, key: str, entry: CacheEntry) -> None:
        stored = CacheEntry(entry.etag, copy.deepcopy(entry.data), entry.next_url)
        with self._lock:
            self._items[key] = stored
            self._items.move_to_end(key)
            while len(self._items) > self._max:
                self._items.popitem(last=False)

    def __len__(self) -> int:
        return len(self._items)
