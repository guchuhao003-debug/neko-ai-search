"""FastAPI entrypoint for the neko-ai-search backend."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.schemas import SearchRequest, SearchResponse
from app.services.ai_service import DeepSeekService
from app.services.cache_service import SearchResponseCache
from app.services.search_service import TavilySearchService
from app.services.sse import format_sse


settings = get_settings()
app = FastAPI(title=settings.app_name)
search_cache = SearchResponseCache()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.frontend_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_search_service() -> TavilySearchService:
    """Create the Tavily search service for request handling."""
    return TavilySearchService(settings)


def get_ai_service() -> DeepSeekService:
    """Create the DeepSeek generation service for request handling."""
    return DeepSeekService(settings)


@app.get("/health")
async def health() -> dict[str, str]:
    """Return service health for local checks and deployment probes."""
    return {"status": "ok", "service": settings.app_name}


@app.post("/api/search", response_model=SearchResponse)
async def search_once(request: SearchRequest) -> SearchResponse:
    """Run the full search pipeline without streaming."""
    cached = search_cache.get(request.query)
    if cached is not None:
        return cached

    search_service = get_search_service()
    ai_service = get_ai_service()
    results = await search_service.search(request.query)
    answer_parts: list[str] = []

    async for token in ai_service.stream_answer(request.query, results):
        answer_parts.append(token)

    answer = "".join(answer_parts)
    related = await ai_service.generate_related_questions(request.query, answer)
    response = SearchResponse(
        query=request.query,
        answer=answer,
        results=results,
        related_questions=related,
    )
    search_cache.set(response)
    return response


@app.post("/api/search/stream")
async def search_stream(request: SearchRequest) -> StreamingResponse:
    """Stream search progress, answer tokens, and related questions as SSE."""

    async def event_generator() -> AsyncIterator[str]:
        """Yield SSE frames for the complete AI search lifecycle."""
        try:
            yield format_sse("search_start", {"query": request.query})
            cached = search_cache.get(request.query)
            if cached is not None:
                yield format_sse("cache_hit", {"query": cached.query})
                yield format_sse(
                    "sources",
                    {
                        "results": [
                            result.model_dump(mode="json")
                            for result in cached.results
                        ]
                    },
                )
                yield format_sse("answer_start", {})
                yield format_sse("answer_done", {"answer": cached.answer})
                yield format_sse("related", {"questions": cached.related_questions})
                yield format_sse("done", {})
                return

            search_service = get_search_service()
            ai_service = get_ai_service()
            answer_parts: list[str] = []
            results = await search_service.search(request.query)
            yield format_sse(
                "sources",
                {"results": [result.model_dump(mode="json") for result in results]},
            )
            yield format_sse("answer_start", {})

            async for token in ai_service.stream_answer(request.query, results):
                answer_parts.append(token)
                yield format_sse("token", {"text": token})

            answer = "".join(answer_parts)
            yield format_sse("answer_done", {"answer": answer})
            related = await ai_service.generate_related_questions(request.query, answer)
            yield format_sse("related", {"questions": related})
            search_cache.set(
                SearchResponse(
                    query=request.query,
                    answer=answer,
                    results=results,
                    related_questions=related,
                )
            )
            yield format_sse("done", {})
        except Exception as exc:
            yield format_sse("error", {"message": str(exc)})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
