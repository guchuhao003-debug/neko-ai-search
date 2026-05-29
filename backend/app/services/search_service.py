"""Search service backed by langchain-tavily."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import unquote, urlparse

from app.config import Settings
from app.schemas import SearchResult


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".svg"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m3u8", ".avi", ".mkv"}
FILE_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".csv",
    ".txt",
    ".md",
    ".zip",
}
VIDEO_HOST_PARTS = ("youtube.com", "youtu.be", "bilibili.com", "vimeo.com")


class SearchConfigurationError(RuntimeError):
    """Raised when search cannot run because configuration is missing."""


def _normalize_result(index: int, raw: dict[str, Any]) -> SearchResult:
    """Map Tavily's result payload into the API's stable SearchResult schema."""
    url = str(raw.get("url") or "")
    result_type = _detect_result_type(url)
    return SearchResult(
        id=index,
        type=result_type,
        title=str(raw.get("title") or f"Source {index}"),
        url=url,
        content=str(raw.get("content") or raw.get("snippet") or ""),
        score=raw.get("score"),
        published_date=raw.get("published_date"),
        file_type=_file_type(url) if result_type == "file" else None,
        thumbnail_url=_thumbnail_url(raw),
    )


def _extract_results(payload: Any) -> list[dict[str, Any]]:
    """Extract raw result dictionaries from common Tavily response shapes."""
    if isinstance(payload, dict):
        results = payload.get("results") or payload.get("data") or []
        return results if isinstance(results, list) else []

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    return []


def _extract_image_results(payload: Any, start_index: int) -> list[SearchResult]:
    """Extract top-level Tavily image results into normalized search results."""
    if not isinstance(payload, dict):
        return []

    images = payload.get("images") or []
    if not isinstance(images, list):
        return []

    results: list[SearchResult] = []
    for offset, raw_image in enumerate(images, start=start_index):
        image = _normalize_image_result(offset, raw_image)
        if image is not None:
            results.append(image)
    return results


def normalize_search_payload(payload: Any, query: str) -> list[SearchResult]:
    """Normalize Tavily payload into ranked multimodal search results."""
    results = [
        _normalize_result(index, raw)
        for index, raw in enumerate(_extract_results(payload), start=1)
    ]
    results.extend(_extract_image_results(payload, len(results) + 1))
    results = deduplicate_search_results(results)
    return rank_search_results(results, query=query)


def deduplicate_search_results(results: list[SearchResult]) -> list[SearchResult]:
    """Remove duplicate URLs or titles while preserving the strongest result."""
    deduped: dict[str, SearchResult] = {}
    for result in results:
        key = _dedupe_key(result)
        existing = deduped.get(key)
        if existing is None or _safe_score(result.score) > _safe_score(existing.score):
            deduped[key] = result

    return list(deduped.values())


def _normalize_image_result(index: int, raw_image: Any) -> SearchResult | None:
    """Map Tavily image payload variants into SearchResult."""
    if isinstance(raw_image, str):
        url = raw_image
        title = _title_from_url(url, fallback=f"Image source {index}")
        description = "Query-related image result from Tavily."
    elif isinstance(raw_image, dict):
        url = str(
            raw_image.get("url")
            or raw_image.get("image_url")
            or raw_image.get("src")
            or ""
        )
        title = str(raw_image.get("title") or _title_from_url(url, f"Image source {index}"))
        description = str(raw_image.get("description") or raw_image.get("alt") or "")
    else:
        return None

    if not url:
        return None

    return SearchResult(
        id=index,
        type="image",
        title=title,
        url=url,
        content=description or "Query-related image result from Tavily.",
        score=0.72,
        thumbnail_url=url,
    )


def rank_search_results(
    results: list[SearchResult],
    *,
    query: str = "",
    now: datetime | None = None,
) -> list[SearchResult]:
    """Rank search results by relevance, source authority, and freshness."""
    current_time = now or datetime.now(timezone.utc)
    ranked = sorted(
        enumerate(results),
        key=lambda item: _ranking_score(item[1], current_time, item[0], query),
        reverse=True,
    )

    return [
        result.model_copy(update={"id": index})
        for index, (_, result) in enumerate(ranked, start=1)
    ]


def _ranking_score(
    result: SearchResult,
    now: datetime,
    original_index: int,
    query: str,
) -> float:
    """Calculate a deterministic score for one normalized search result."""
    relevance = _safe_score(result.score)
    authority = _authority_score(str(result.url))
    freshness = _freshness_score(result.published_date, now)
    type_intent = _type_intent_score(result.type, query)

    # Keep Tavily relevance important, but let authoritative and recent sources rise.
    composite = relevance * 0.38 + authority * 0.3 + freshness * 0.18 + type_intent * 0.14
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


def _type_intent_score(result_type: str, query: str) -> float:
    """Boost result types when the query clearly asks for a matching modality."""
    text = query.lower()
    if result_type == "image":
        terms = ("图片", "图像", "照片", "截图", "长什么样", "image", "photo", "picture")
    elif result_type == "video":
        terms = ("视频", "教程", "演示", "录像", "video", "tutorial", "demo")
    elif result_type == "file":
        terms = ("pdf", "报告", "白皮书", "文档", "表格", "文件", "document", "report")
    else:
        terms = ("文章", "网页", "资料", "解释", "介绍", "article", "web")

    return 1.0 if any(term in text for term in terms) else 0.25


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


def _detect_result_type(url: str) -> str:
    """Detect whether a URL points to text, image, video, or file content."""
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    extension = _url_extension(url)

    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in VIDEO_EXTENSIONS or any(part in hostname for part in VIDEO_HOST_PARTS):
        return "video"
    if extension in FILE_EXTENSIONS:
        return "file"
    return "text"


def _file_type(url: str) -> str | None:
    """Return a compact file type label from a URL extension."""
    extension = _url_extension(url)
    return extension.removeprefix(".").upper() if extension in FILE_EXTENSIONS else None


def _url_extension(url: str) -> str:
    """Read a lower-case extension from the URL path."""
    path = unquote(urlparse(url).path)
    if "." not in path:
        return ""

    return "." + path.rsplit(".", 1)[-1].lower()


def _thumbnail_url(raw: dict[str, Any]) -> str | None:
    """Read common thumbnail fields from a provider result."""
    value = raw.get("thumbnail_url") or raw.get("thumbnail") or raw.get("image")
    return str(value) if value else None


def _title_from_url(url: str, fallback: str) -> str:
    """Create a readable fallback title from a URL path."""
    path = unquote(urlparse(url).path).strip("/")
    if not path:
        return fallback

    filename = path.rsplit("/", 1)[-1]
    return filename or fallback


def _dedupe_key(result: SearchResult) -> str:
    """Build a stable key for result de-duplication."""
    parsed = urlparse(str(result.url))
    path = parsed.path.rstrip("/") or "/"
    if parsed.netloc:
        return f"url:{parsed.netloc.lower()}{path.lower()}"

    return f"title:{result.title.strip().lower()}"


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
            include_images=True,
            include_image_descriptions=True,
        )
        payload = await asyncio.to_thread(tool.invoke, {"query": query})
        return normalize_search_payload(payload, query)

    def _mock_results(self, query: str) -> list[SearchResult]:
        """Return deterministic local results for development without API keys."""
        return [
            SearchResult(
                id=1,
                type="text",
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
                type="text",
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
                type="image",
                title="Neko AI Search interface preview",
                url="https://example.com/neko-ai-search-preview.png",
                content="A mock image result that demonstrates multimodal result cards.",
                score=0.74,
                thumbnail_url="https://example.com/neko-ai-search-preview.png",
            ),
            SearchResult(
                id=4,
                type="file",
                title=f"Search report for {query}",
                url="https://example.com/neko-ai-search-report.pdf",
                content="A mock PDF result that demonstrates document search result cards.",
                score=0.7,
                file_type="PDF",
            ),
            SearchResult(
                id=5,
                type="video",
                title=f"Search intent analysis for {query}",
                url="https://www.youtube.com/watch?v=neko-ai-search-demo",
                content=(
                    "A search answer engine should combine direct synthesis, "
                    "inline citations, source lists, and complete web results."
                ),
                score=0.9,
            ),
        ]
