"""Tests for completed search response caching."""

from app.schemas import SearchResponse, SearchResult
from app.services.cache_service import SearchResponseCache, normalize_query


def _response(query: str, answer: str = "answer") -> SearchResponse:
    """Create a minimal cached response for assertions."""
    return SearchResponse(
        query=query,
        answer=answer,
        results=[
            SearchResult(
                id=1,
                title="Source",
                url="https://example.com",
                content="Evidence",
            )
        ],
        related_questions=["next question"],
    )


def test_normalize_query_collapses_case_and_spacing() -> None:
    """Equivalent query text should map to one cache key."""
    assert normalize_query("  DeepSeek   V4  ") == "deepseek v4"


def test_cache_returns_copy_for_matching_query() -> None:
    """Cached responses should be isolated from caller mutation."""
    cache = SearchResponseCache()
    cache.set(_response("DeepSeek V4"))

    cached = cache.get(" deepseek   v4 ")
    assert cached is not None
    cached.answer = "changed"

    assert cache.get("DeepSeek V4").answer == "answer"


def test_cache_evicts_least_recently_used_item() -> None:
    """Cache should keep only the configured number of recent responses."""
    cache = SearchResponseCache(max_size=1)

    cache.set(_response("first"))
    cache.set(_response("second"))

    assert cache.get("first") is None
    assert cache.get("second") is not None


def test_cache_separates_answer_modes() -> None:
    """Fast and deep answers should not share the same cache entry."""
    cache = SearchResponseCache()
    fast = _response("DeepSeek V4", "fast answer")
    deep = _response("DeepSeek V4", "deep answer")
    deep.mode = "deep"

    cache.set(fast)
    cache.set(deep)

    assert cache.get("DeepSeek V4", "fast").answer == "fast answer"
    assert cache.get("DeepSeek V4", "deep").answer == "deep answer"


def test_cache_expires_stale_entries() -> None:
    """Expired cache entries should be treated as misses."""
    current_time = 100.0
    cache = SearchResponseCache(ttl_seconds=10, clock=lambda: current_time)
    cache.set(_response("DeepSeek V4"))

    current_time = 111.0

    assert cache.get("DeepSeek V4") is None
