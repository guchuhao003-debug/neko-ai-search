"""Tests for search result ranking."""

from datetime import datetime, timezone

from app.schemas import SearchResult
from app.services.search_service import (
    deduplicate_search_results,
    normalize_search_payload,
    rank_search_results,
)


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


def test_normalize_search_payload_detects_multimodal_result_types() -> None:
    """Tavily payload normalization should produce text, file, video, and image results."""
    payload = {
        "results": [
            {
                "title": "Documentation",
                "url": "https://docs.example.com/guide",
                "content": "Text source",
                "score": 0.9,
            },
            {
                "title": "White paper",
                "url": "https://example.com/report.pdf",
                "content": "PDF source",
                "score": 0.88,
            },
            {
                "title": "Demo video",
                "url": "https://www.youtube.com/watch?v=demo",
                "content": "Video source",
                "score": 0.86,
            },
        ],
        "images": [
            {
                "url": "https://example.com/diagram.png",
                "description": "Architecture diagram",
            }
        ],
    }

    results = normalize_search_payload(payload, "架构图片和视频资料")

    assert {result.type for result in results} == {"text", "file", "video", "image"}
    assert next(result for result in results if result.type == "file").file_type == "PDF"
    assert next(result for result in results if result.type == "image").thumbnail_url


def test_deduplicate_search_results_keeps_highest_scored_duplicate() -> None:
    """Duplicate URLs should collapse to the strongest result."""
    results = [
        _result(1, "https://example.com/page?utm=one", 0.4),
        _result(2, "https://example.com/page?utm=two", 0.9),
    ]

    deduped = deduplicate_search_results(results)

    assert len(deduped) == 1
    assert deduped[0].score == 0.9
