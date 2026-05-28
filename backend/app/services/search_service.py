"""Search service backed by langchain-tavily."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

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


def rank_search_results(
    results: list[SearchResult],
    *,
    now: datetime | None = None,
) -> list[SearchResult]:
    """Rank search results by relevance, source authority, and freshness."""
    current_time = now or datetime.now(timezone.utc)
    ranked = sorted(
        enumerate(results),
        key=lambda item: _ranking_score(item[1], current_time, item[0]),
        reverse=True,
    )

    return [
        result.model_copy(update={"id": index})
        for index, (_, result) in enumerate(ranked, start=1)
    ]


def _ranking_score(result: SearchResult, now: datetime, original_index: int) -> float:
    """Calculate a deterministic score for one normalized search result."""
    relevance = _safe_score(result.score)
    authority = _authority_score(str(result.url))
    freshness = _freshness_score(result.published_date, now)

    # Keep Tavily relevance important, but let authoritative and recent sources rise.
    composite = relevance * 0.42 + authority * 0.36 + freshness * 0.22
    return composite - original_index * 0.0001


def _safe_score(score: float | None) -> float:
    """Clamp Tavily score into a safe zero-to-one range."""
    if score is None:
        return 0.0

    return min(max(float(score), 0.0), 1.0)


def _authority_score(url: str) -> float:
    """Estimate source authority from hostname and documentation-like paths."""
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    path = parsed.path.lower()
    score = 0.25

    if hostname.endswith(".gov") or ".gov." in hostname:
        score = 1.0
    elif hostname.endswith(".edu") or ".edu." in hostname:
        score = 0.9
    elif hostname.endswith(".org") or ".org." in hostname:
        score = 0.62
    elif hostname.startswith(("docs.", "developer.", "learn.")):
        score = 0.76
    elif hostname.startswith(("news.", "blog.")):
        score = 0.34

    if any(segment in path for segment in ("/docs", "/documentation", "/reference")):
        score += 0.12

    return min(score, 1.0)


def _freshness_score(published_date: str | None, now: datetime) -> float:
    """Estimate freshness from a result publication date."""
    published = _parse_published_date(published_date)
    if published is None:
        return 0.2

    age_days = max((now.date() - published).days, 0)
    if age_days <= 30:
        return 1.0
    if age_days <= 90:
        return 0.85
    if age_days <= 180:
        return 0.7
    if age_days <= 365:
        return 0.55
    if age_days <= 730:
        return 0.35
    return 0.15


def _parse_published_date(value: str | None) -> date | None:
    """Parse common Tavily publication date formats into a date."""
    if not value:
        return None

    text = value.strip()
    if not text:
        return None

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass

    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue

    try:
        return parsedate_to_datetime(text).date()
    except (TypeError, ValueError):
        return None


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
        results = [
            _normalize_result(index, raw)
            for index, raw in enumerate(_extract_results(payload), start=1)
        ]
        return rank_search_results(results)

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
