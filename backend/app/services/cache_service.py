"""In-memory cache helpers for completed search responses."""

from __future__ import annotations

from collections import OrderedDict

from app.schemas import SearchResponse


class SearchResponseCache:
    """Store completed search responses by normalized query text."""

    def __init__(self, max_size: int = 128) -> None:
        """Create a bounded least-recently-used cache."""
        self.max_size = max_size
        self._items: OrderedDict[str, SearchResponse] = OrderedDict()

    def get(self, query: str) -> SearchResponse | None:
        """Return a cached response for a query when it exists."""
        key = normalize_query(query)
        cached = self._items.get(key)
        if cached is None:
            return None

        # Move hit entries to the end so the oldest unused item is evicted first.
        self._items.move_to_end(key)
        return cached.model_copy(deep=True)

    def set(self, response: SearchResponse) -> None:
        """Cache a completed search response."""
        key = normalize_query(response.query)
        self._items[key] = response.model_copy(deep=True)
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
