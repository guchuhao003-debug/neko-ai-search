"""Tests for search result ranking."""

from datetime import datetime, timezone
from typing import Optional

import pytest

from app.schemas import SearchResult
from app.services.search_service import (
    deduplicate_search_results,
    enrich_video_thumbnails,
    normalize_search_payload,
    rank_search_results,
)


NOW = datetime(2026, 5, 28, tzinfo=timezone.utc)


def _result(
    index: int,
    url: str,
    score: float,
    published_date: Optional[str] = None,
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

    results = normalize_search_payload(payload, "architecture image and video resources")

    assert {result.type for result in results} == {"text", "file", "video", "image"}
    assert next(result for result in results if result.type == "file").file_type == "PDF"
    assert next(result for result in results if result.type == "image").thumbnail_url


def test_normalize_search_payload_reads_nested_thumbnail_fields() -> None:
    """Provider thumbnail variants should become stable preview URLs."""
    payload = {
        "results": [
            {
                "title": "Video result",
                "url": "https://www.bilibili.com/video/BV123",
                "content": "Video source",
                "score": 0.82,
                "metadata": {
                    "open_graph": {
                        "image": "https://example.com/video-cover.webp",
                    },
                },
            },
            {
                "title": "Article result",
                "url": "https://example.com/article",
                "content": "Article source",
                "score": 0.78,
                "images": [
                    {
                        "image_url": "https://example.com/article-cover.png",
                    }
                ],
            },
        ],
    }

    results = normalize_search_payload(payload, "video and article covers")

    assert next(result for result in results if result.type == "video").thumbnail_url == (
        "https://example.com/video-cover.webp"
    )
    assert next(result for result in results if result.type == "text").thumbnail_url == (
        "https://example.com/article-cover.png"
    )


@pytest.mark.anyio
async def test_enrich_video_thumbnails_builds_youtube_cover() -> None:
    """YouTube video results should receive a deterministic thumbnail URL."""
    results = [
        SearchResult(
            id=1,
            type="video",
            title="Demo video",
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            content="Video source",
        )
    ]

    enriched = await enrich_video_thumbnails(results)

    assert enriched[0].thumbnail_url == "https://img.youtube.com/vi/dQw4w9WgXcQ/hqdefault.jpg"


@pytest.mark.anyio
async def test_enrich_video_thumbnails_fetches_bilibili_cover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bilibili video results should use the provider metadata cover."""

    async def fake_fetch_bilibili_thumbnail(url: str) -> str:
        """Return a deterministic Bilibili cover for the requested URL."""
        assert url == "https://www.bilibili.com/video/BV1xx411c7mD/"
        return "https://i0.hdslb.com/bfs/archive/cover.jpg"

    monkeypatch.setattr(
        "app.services.search_service._fetch_bilibili_thumbnail",
        fake_fetch_bilibili_thumbnail,
    )
    results = [
        SearchResult(
            id=1,
            type="video",
            title="Bilibili video",
            url="https://www.bilibili.com/video/BV1xx411c7mD/",
            content="Video source",
        )
    ]

    enriched = await enrich_video_thumbnails(results)

    assert enriched[0].thumbnail_url == "https://i0.hdslb.com/bfs/archive/cover.jpg"


def test_deduplicate_search_results_keeps_highest_scored_duplicate() -> None:
    """Duplicate URLs should collapse to the strongest result."""
    results = [
        _result(1, "https://example.com/page?utm=one", 0.4),
        _result(2, "https://example.com/page?utm=two", 0.9),
    ]

    deduped = deduplicate_search_results(results)

    assert len(deduped) == 1
    assert deduped[0].score == 0.9
