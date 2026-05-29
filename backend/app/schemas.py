"""Pydantic schemas shared by the API layer and services."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, HttpUrl


SearchMode = Literal["fast", "deep"]


class SearchRequest(BaseModel):
    """Incoming search request from the Vue client."""

    query: str = Field(..., min_length=2, max_length=500)
    mode: SearchMode = "fast"


class SearchResult(BaseModel):
    """Normalized multimodal result returned by search providers."""

    id: int
    type: Literal["text", "image", "video", "file"] = "text"
    title: str
    url: HttpUrl | str
    content: str
    score: float | None = None
    published_date: str | None = None
    file_type: str | None = None
    thumbnail_url: HttpUrl | str | None = None


class RelatedQuestions(BaseModel):
    """Related follow-up questions generated after an answer completes."""

    questions: list[str]


class SearchResponse(BaseModel):
    """Non-streaming response shape used by tests and future clients."""

    query: str
    mode: SearchMode = "fast"
    answer: str
    results: list[SearchResult]
    related_questions: list[str]
