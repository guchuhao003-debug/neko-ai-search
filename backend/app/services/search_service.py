"""Search service backed by langchain-tavily."""

from __future__ import annotations

import asyncio
from typing import Any

from app.config import Settings
from app.schemas import SearchResult


class SearchConfigurationError(RuntimeError):
    """Raised when search cannot run because configuration is missing."""


def _normalize_result(index: int, raw: dict[str, Any]) -> SearchResult:
    """Map Tavily's result payload into the API's stable SearchResult schema."""
    return SearchResult(
        id=index,
        title=str(raw.get("title") or f"Source {index}"),
        url=str(raw.get("url") or ""),
        content=str(raw.get("content") or raw.get("snippet") or ""),
        score=raw.get("score"),
        published_date=raw.get("published_date"),
    )


def _extract_results(payload: Any) -> list[dict[str, Any]]:
    """Extract raw result dictionaries from common Tavily response shapes."""
    if isinstance(payload, dict):
        results = payload.get("results") or payload.get("data") or []
        return results if isinstance(results, list) else []

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    return []


class TavilySearchService:
    """Run fresh web search queries through the LangChain Tavily integration."""

    def __init__(self, settings: Settings) -> None:
        """Store settings and defer importing Tavily until the service is used."""
        self.settings = settings

    async def search(self, query: str) -> list[SearchResult]:
        """Search the web and return normalized results."""
        if self.settings.use_mock_ai or not self.settings.tavily_api_key:
            return self._mock_results(query)

        # Import lazily so unit tests can run without optional integrations loaded.
        from langchain_tavily import TavilySearch

        tool = TavilySearch(
            max_results=self.settings.tavily_max_results,
            topic="general",
            search_depth="advanced",
            include_answer=False,
            include_raw_content=False,
        )
        payload = await asyncio.to_thread(tool.invoke, {"query": query})
        return [
            _normalize_result(index, raw)
            for index, raw in enumerate(_extract_results(payload), start=1)
        ]

    def _mock_results(self, query: str) -> list[SearchResult]:
        """Return deterministic local results for development without API keys."""
        return [
            SearchResult(
                id=1,
                title="Tavily Search API documentation",
                url="https://docs.tavily.com/",
                content=(
                    "Tavily provides web search results with title, url, "
                    "content, score, and optional published date fields."
                ),
                score=0.98,
            ),
            SearchResult(
                id=2,
                title="DeepSeek API OpenAI-compatible guide",
                url="https://api-docs.deepseek.com/",
                content=(
                    "DeepSeek exposes an OpenAI-compatible chat completions API "
                    "with streaming support and DeepSeek V4 model options."
                ),
                score=0.95,
            ),
            SearchResult(
                id=3,
                title=f"Search intent analysis for {query}",
                url="https://example.com/neko-ai-search-demo",
                content=(
                    "A search answer engine should combine direct synthesis, "
                    "inline citations, source lists, and complete web results."
                ),
                score=0.9,
            ),
        ]
