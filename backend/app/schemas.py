"""Pydantic schemas shared by the API layer and services."""

from __future__ import annotations

from typing import List, Literal, Optional, Union

from pydantic import BaseModel, Field
from pydantic import HttpUrl


SearchMode = Literal["fast", "deep"]
UserRole = Literal["user", "admin"]
UserStatus = Literal["active", "disabled"]


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
    role: UserRole = "user"
    status: UserStatus = "active"
    created_at: str
    is_admin: bool = False


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


class CreditAccountResponse(BaseModel):
    """Current credit account summary for the authenticated user."""

    balance: int
    updated_at: str


class CreditLedgerResponseItem(BaseModel):
    """One immutable credit ledger row for the authenticated user."""

    id: int
    change_amount: int
    balance_after: int
    reason: str
    reference_type: Optional[str] = None
    reference_id: Optional[str] = None
    created_at: str


class CreditSummaryResponse(BaseModel):
    """Credit account and recent ledger rows for one authenticated user."""

    account: CreditAccountResponse
    ledger: List[CreditLedgerResponseItem]


class AdminStatsSummaryResponse(BaseModel):
    """Platform-wide administrator summary counters."""

    total_users: int
    active_sessions: int
    total_history_items: int
    total_credit_balance: int
    total_credits_granted: int
    total_credits_spent: int
    total_search_debits: int
    fast_history_items: int
    deep_history_items: int
    registered_today: int
    searches_today: int
    credits_spent_today: int


class AdminRecentUserResponseItem(BaseModel):
    """Recent user row visible in the administrator dashboard."""

    id: int
    email: str
    display_name: str
    balance: int
    history_count: int
    created_at: str


class AdminManagedUserResponseItem(BaseModel):
    """User row visible in the administrator user-management table."""

    id: int
    email: str
    display_name: str
    role: UserRole
    status: UserStatus
    balance: int
    history_count: int
    created_at: str
    updated_at: str


class AdminUserListResponse(BaseModel):
    """Paginated user-management response for administrators."""

    items: List[AdminManagedUserResponseItem]
    total: int
    limit: int
    offset: int


class AdminCreateUserRequest(BaseModel):
    """Payload used by administrators to create managed users."""

    email: str = Field(
        ...,
        min_length=3,
        max_length=254,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    password: str = Field(..., min_length=8, max_length=128)
    display_name: str = Field(..., min_length=1, max_length=80)
    role: UserRole = "user"
    status: UserStatus = "active"


class AdminUpdateUserRequest(BaseModel):
    """Payload used by administrators to edit user profile and access."""

    display_name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    role: Optional[UserRole] = None
    status: Optional[UserStatus] = None


class AdminCreditAdjustmentRequest(BaseModel):
    """Payload used by administrators to adjust one user's credits."""

    change_amount: int = Field(..., ge=-100000, le=100000)
    reason: str = Field(..., min_length=2, max_length=80)


class AdminCreditAdjustmentResponse(BaseModel):
    """Result of an administrator credit adjustment."""

    user: AdminManagedUserResponseItem
    account: CreditAccountResponse
    ledger: CreditLedgerResponseItem


class AdminDeleteUserResponse(BaseModel):
    """Deletion result for administrator user-management actions."""

    deleted: bool


class AdminRecentSearchResponseItem(BaseModel):
    """Recent user search row visible in the administrator dashboard."""

    id: int
    user_email: str
    query: str
    mode: SearchMode
    created_at: str


class AdminCreditReasonResponseItem(BaseModel):
    """Grouped credit ledger reason statistics for administrators."""

    reason: str
    ledger_count: int
    total_change: int


class AdminStatsResponse(BaseModel):
    """Administrator statistics dashboard payload."""

    summary: AdminStatsSummaryResponse
    recent_users: List[AdminRecentUserResponseItem]
    recent_searches: List[AdminRecentSearchResponseItem]
    credit_reasons: List[AdminCreditReasonResponseItem]
