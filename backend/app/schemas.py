"""Pydantic schemas shared by the API layer and services."""

from __future__ import annotations

from typing import List, Literal, Optional, Union

from pydantic import BaseModel, Field
from pydantic import HttpUrl


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
    url: Union[HttpUrl, str]
    content: str
    score: Optional[float] = None
    published_date: Optional[str] = None
    file_type: Optional[str] = None
    thumbnail_url: Optional[Union[HttpUrl, str]] = None


class RelatedQuestions(BaseModel):
    """Related follow-up questions generated after an answer completes."""

    questions: List[str]


class SearchResponse(BaseModel):
    """Non-streaming response shape used by tests and future clients."""

    query: str
    mode: SearchMode = "fast"
    answer: str
    results: List[SearchResult]
    related_questions: List[str]


class AuthUser(BaseModel):
    """Public authenticated user profile returned to the Vue client."""

    id: int
    email: str
    display_name: str
    created_at: str


class AuthStatusResponse(BaseModel):
    """Current authentication status for optional session checks."""

    user: Optional[AuthUser] = None


class RegisterRequest(BaseModel):
    """Incoming user registration payload."""

    email: str = Field(..., min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(..., min_length=8, max_length=128)
    display_name: str = Field(..., min_length=1, max_length=80)


class LoginRequest(BaseModel):
    """Incoming user login payload."""

    email: str = Field(..., min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(..., min_length=8, max_length=128)


class SearchHistoryResponseItem(BaseModel):
    """Private search history item returned to authenticated users."""

    id: int
    query: str
    mode: SearchMode
    created_at: str


class SearchHistoryListResponse(BaseModel):
    """List wrapper for private user history."""

    items: List[SearchHistoryResponseItem]
