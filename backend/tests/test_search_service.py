"""Tests for search result ranking."""

from datetime import datetime, timezone

from app.schemas import SearchResult
from app.services.search_service import rank_search_results


NOW = datetime(2026, 5, 28, tzinfo=timezone.utc)


def _result(
    index: int,
    url: str,
    score: float,
    published_date: str | None = None,
) -> SearchResult:
    """Create a search result for ranking assertions."""
    return SearchResult(
        id=index,
        title=f"Result {index}",
        url=url,
        content="Evidence",
        score=score,
        published_date=published_date,
    )


def test_rank_search_results_promotes_authoritative_sources() -> None:
    """Authoritative sources should outrank generic pages with similar relevance."""
    results = [
        _result(1, "https://example.com/post", 0.99),
        _result(2, "https://www.nasa.gov/news", 0.7, "2025-12-01"),
    ]

    ranked = rank_search_results(results, now=NOW)

    assert ranked[0].url == "https://www.nasa.gov/news"
    assert ranked[0].id == 1
    assert ranked[1].id == 2


def test_rank_search_results_promotes_fresh_content() -> None:
    """Newer content should outrank older content from comparable sources."""
    results = [
        _result(1, "https://example.com/old", 0.8, "2023-01-01"),
        _result(2, "https://example.com/new", 0.8, "2026-05-01"),
    ]

    ranked = rank_search_results(results, now=NOW)

    assert ranked[0].url == "https://example.com/new"
    assert ranked[1].url == "https://example.com/old"


def test_rank_search_results_keeps_stable_order_for_equal_scores() -> None:
    """Equal ranking scores should preserve Tavily order after re-numbering."""
    results = [
        _result(1, "https://example.com/a", 0.5),
        _result(2, "https://example.com/b", 0.5),
    ]

    ranked = rank_search_results(results, now=NOW)

    assert [result.url for result in ranked] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert [result.id for result in ranked] == [1, 2]
