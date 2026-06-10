"""In-memory cache helpers for completed search responses."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic

from app.schemas import SearchMode, SearchResponse


@dataclass(frozen=True)
class CacheEntry:
    """Cached response with an expiry timestamp."""

    response: SearchResponse
    expires_at: float


class SearchResponseCache:
    """Store completed search responses by normalized query text."""

    def __init__(
        self,
        max_size: int = 128,
        ttl_seconds: int = 1800,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        """Create a bounded least-recently-used cache."""
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._items: OrderedDict[str, CacheEntry] = OrderedDict()

    def get(self, query: str, mode: SearchMode = "fast") -> SearchResponse | None:
        """Return a cached response for a query when it exists."""
        key = build_cache_key(query, mode)
        cached = self._items.get(key)
        if cached is None:
            return None

        if cached.expires_at <= self.clock() or not cached.response.results:
            self._items.pop(key, None)
            return None

        # Move hit entries to the end so the oldest unused item is evicted first.
        self._items.move_to_end(key)
        return cached.response.model_copy(deep=True)

    def set(self, response: SearchResponse) -> None:
        """Cache a completed search response."""
        key = build_cache_key(response.query, response.mode)
        if not response.results:
            self._items.pop(key, None)
            return

        self._items[key] = CacheEntry(
            response=response.model_copy(deep=True),
            expires_at=self.clock() + self.ttl_seconds,
        )
        self._items.move_to_end(key)

        # Keep memory usage bounded for long-running backend processes.
        while len(self._items) > self.max_size:
            self._items.popitem(last=False)

    def clear(self) -> None:
        """Clear all cached responses."""
        self._items.clear()


def normalize_query(query: str) -> str:
    """Normalize user query text so repeated searches share one cache key."""
    return " ".join(query.strip().lower().split())


def build_cache_key(query: str, mode: SearchMode = "fast") -> str:
    """Build a cache key that separates fast and deep answers."""
    return f"{mode}:{normalize_query(query)}"
