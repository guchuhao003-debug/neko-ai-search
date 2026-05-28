"""Tests for API-level search response caching."""

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app import main
from app.schemas import SearchResult


class CountingSearchService:
    """Fake search service that counts external search calls."""

    def __init__(self) -> None:
        """Initialize the call counter."""
        self.calls = 0

    async def search(self, query: str) -> list[SearchResult]:
        """Return deterministic search results."""
        self.calls += 1
        return [
            SearchResult(
                id=1,
                title=f"Result for {query}",
                url="https://example.com/source",
                content="Cached source content",
            )
        ]


class CountingAiService:
    """Fake AI service that counts answer generation calls."""

    def __init__(self) -> None:
        """Initialize the call counters."""
        self.stream_calls = 0
        self.related_calls = 0

    async def stream_answer(
        self,
        query: str,
        results: list[SearchResult],
    ) -> AsyncIterator[str]:
        """Yield a deterministic answer."""
        self.stream_calls += 1
        yield f"Answer for {query} [1]"

    async def generate_related_questions(self, query: str, answer: str) -> list[str]:
        """Return deterministic related questions."""
        self.related_calls += 1
        return [f"More about {query}"]


@pytest.fixture(autouse=True)
def clear_search_cache() -> None:
    """Keep tests isolated from the module-level search cache."""
    main.search_cache.clear()


@pytest.mark.asyncio
async def test_search_once_returns_cached_response_without_repeating_ai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second non-streaming request should use the cached complete response."""
    search_service = CountingSearchService()
    ai_service = CountingAiService()
    monkeypatch.setattr(main, "get_search_service", lambda: search_service)
    monkeypatch.setattr(main, "get_ai_service", lambda: ai_service)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/api/search", json={"query": "DeepSeek V4"})
        second = await client.post("/api/search", json={"query": "  deepseek   v4 "})

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["answer"] == second.json()["answer"]
    assert search_service.calls == 1
    assert ai_service.stream_calls == 1
    assert ai_service.related_calls == 1


@pytest.mark.asyncio
async def test_search_stream_returns_cache_hit_without_repeating_ai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second streaming request should emit cached data without token generation."""
    search_service = CountingSearchService()
    ai_service = CountingAiService()
    monkeypatch.setattr(main, "get_search_service", lambda: search_service)
    monkeypatch.setattr(main, "get_ai_service", lambda: ai_service)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/api/search/stream", json={"query": "DeepSeek V4"})
        second = await client.post("/api/search/stream", json={"query": "deepseek v4"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert "event: cache_hit" not in first.text
    assert "event: cache_hit" in second.text
    assert "event: token" not in second.text
    assert "Answer for DeepSeek V4 [1]" in second.text
    assert search_service.calls == 1
    assert ai_service.stream_calls == 1
    assert ai_service.related_calls == 1
