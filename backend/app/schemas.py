"""Pydantic schemas shared by the API layer and services."""

from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl


class SearchRequest(BaseModel):
    """Incoming search request from the Vue client."""

    query: str = Field(..., min_length=2, max_length=500)


class SearchResult(BaseModel):
    """Normalized web result returned by Tavily."""

    id: int
    title: str
    url: HttpUrl | str
    content: str
    score: float | None = None
    published_date: str | None = None


class RelatedQuestions(BaseModel):
    """Related follow-up questions generated after an answer completes."""

    questions: list[str]


class SearchResponse(BaseModel):
    """Non-streaming response shape used by tests and future clients."""

    query: str
    answer: str
    results: list[SearchResult]
    related_questions: list[str]
