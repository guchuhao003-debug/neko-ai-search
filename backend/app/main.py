"""FastAPI entrypoint for the neko-ai-search backend."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import AsyncIterator
from ipaddress import ip_address, ip_network
from time import monotonic
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, Response, StreamingResponse

from app.config import get_settings
from app.schemas import (
    AdminCreateUserRequest,
    AdminCreditReasonResponseItem,
    AdminCreditAdjustmentRequest,
    AdminCreditAdjustmentResponse,
    AdminDeleteUserResponse,
    AdminManagedUserResponseItem,
    AdminRecentSearchResponseItem,
    AdminRecentUserResponseItem,
    AdminStatsResponse,
    AdminStatsSummaryResponse,
    AdminUpdateUserRequest,
    AdminUserListResponse,
    AuthStatusResponse,
    AuthUser,
    CreditAccountResponse,
    CreditLedgerResponseItem,
    CreditSummaryResponse,
    LoginRequest,
    RegisterRequest,
    SearchHistoryListResponse,
    SearchHistoryResponseItem,
    SearchRequest,
    SearchResponse,
)
from app.services.account_service import (
    ADMIN_ROLE,
    DISABLED_STATUS,
    USER_ROLE,
    AdminCreditReasonStat,
    AdminManagedUser,
    AdminRecentSearchStat,
    AdminRecentUserStat,
    AdminStatsSummary,
    AccountUser,
    CreditAccount,
    CreditLedgerRecord,
    DuplicateUserError,
    HistoryRecord,
    InvalidCredentialsError,
    InsufficientCreditError,
    SessionRecord,
    create_account_service,
)
from app.services.ai_service import DeepSeekService, generate_rule_based_related_questions
from app.services.cache_service import SearchResponseCache
from app.services.cost_guard_service import CostGuardError, create_cost_guard
from app.services.metrics_service import MetricsRegistry
from app.services.media_proxy_service import MediaProxyError, fetch_remote_media
from app.services.observability_service import SearchObserver, SearchStep
from app.services.search_service import TavilySearchService
from app.services.security_service import SecurityBlockedError, SecurityService
from app.services.sse import format_sse


settings = get_settings()
app = FastAPI(title=settings.app_name)
search_cache = SearchResponseCache(ttl_seconds=settings.search_cache_ttl_seconds)
cost_guard = create_cost_guard(settings)
account_service = create_account_service(settings)
security_service = SecurityService(settings.security_blocked_terms_path)
metrics = MetricsRegistry()

SEARCH_CREDIT_COSTS: Dict[str, int] = {"fast": 1, "deep": 3}
SEARCH_USAGE_REASON = "search_usage"
SEARCH_REFERENCE_TYPE = "search"


class AuthRateLimiter:
    """Process-local limiter for login and registration attempts."""

    def __init__(self, *, clock=monotonic) -> None:
        """Create an auth limiter with a monotonic clock."""
        self.clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, client_id: str, limit: int) -> None:
        """Raise an HTTP 429 when a client exceeds auth attempt limits."""
        now = self.clock()
        window_start = now - 60
        hits = self._hits[client_id]
        while hits and hits[0] <= window_start:
            hits.popleft()

        if len(hits) >= limit:
            retry_after = max(round(60 - (now - hits[0])), 1)
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "auth_rate_limited",
                    "message": "登录或注册尝试过于频繁，请稍后再试。",
                    "retry_after_seconds": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )

        hits.append(now)

    def reset(self) -> None:
        """Clear auth limiter counters for tests."""
        self._hits.clear()


auth_rate_limiter = AuthRateLimiter()

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


def get_client_id(request: Request) -> str:
    """Return a stable client identifier for rate limiting."""
    direct_host = request.client.host if request.client else "unknown"
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for and is_trusted_proxy_host(direct_host):
        forwarded_host = forwarded_for.split(",", maxsplit=1)[0].strip()
        return forwarded_host or direct_host

    return direct_host


def is_trusted_proxy_host(host: str) -> bool:
    """Return whether the direct client is allowed to supply forwarding headers."""
    if host in settings.trusted_proxy_ips:
        return True

    try:
        client_ip = ip_address(host)
    except ValueError:
        return False

    for trusted_proxy in settings.trusted_proxy_ips:
        try:
            if client_ip in ip_network(trusted_proxy, strict=False):
                return True
        except ValueError:
            continue

    return False


def check_auth_rate_limit(request: Request, email: str | None = None) -> None:
    """Limit login and registration attempts by IP and target email."""
    limit = settings.auth_rate_limit_per_minute
    client_id = get_client_id(request)
    auth_rate_limiter.check(f"ip:{client_id}", limit)

    if email:
        normalized_email = email.strip().lower()
        auth_rate_limiter.check(f"email:{normalized_email}", limit)


def guard_error_payload(
    exc: CostGuardError,
    search_id: Optional[str] = None,
) -> Dict[str, object]:
    """Serialize a cost guard error for HTTP and SSE responses."""
    payload: Dict[str, object] = {
        "code": exc.code,
        "message": exc.message,
    }
    if exc.retry_after_seconds is not None:
        payload["retry_after_seconds"] = exc.retry_after_seconds
    if search_id:
        payload["search_id"] = search_id
    return payload


def raise_guard_http_error(exc: CostGuardError) -> None:
    """Raise a rate-limit style HTTP error for non-streaming requests."""
    headers = {}
    if exc.retry_after_seconds is not None:
        headers["Retry-After"] = str(exc.retry_after_seconds)

    raise HTTPException(status_code=429, detail=guard_error_payload(exc), headers=headers)


def security_error_payload(
    exc: SecurityBlockedError,
    search_id: Optional[str] = None,
) -> Dict[str, object]:
    """Serialize a security error for HTTP and SSE responses."""
    payload: Dict[str, object] = {
        "code": exc.code,
        "message": exc.message,
        "reason": exc.reason,
    }
    if search_id:
        payload["search_id"] = search_id
    return payload


def raise_security_http_error(exc: SecurityBlockedError) -> None:
    """Raise an HTTP error for blocked non-streaming requests."""
    raise HTTPException(status_code=400, detail=security_error_payload(exc))


class SearchAuthenticationError(Exception):
    """Raised when a search request has no authenticated user."""

    code = "authentication_required"
    message = "请先登录后再使用积分搜索。"

    def __str__(self) -> str:
        """Return the user-facing authentication message."""
        return self.message


class SearchCreditError(Exception):
    """Raised when a user cannot afford the current paid search mode."""

    code = "insufficient_credits"

    def __init__(self, required_credits: int, current_balance: int) -> None:
        """Create an error with the required and available credit amounts."""
        self.required_credits = required_credits
        self.current_balance = current_balance
        super().__init__(self.message)

    @property
    def message(self) -> str:
        """Return the user-facing insufficient credit message."""
        return (
            f"当前搜索需要 {self.required_credits} 积分，"
            f"你的余额为 {self.current_balance} 积分。"
        )


def search_auth_error_payload(
    exc: SearchAuthenticationError,
    search_id: Optional[str] = None,
) -> Dict[str, object]:
    """Serialize a search authentication error for HTTP and SSE responses."""
    payload: Dict[str, object] = {
        "code": exc.code,
        "message": str(exc),
    }
    if search_id:
        payload["search_id"] = search_id
    return payload


def search_credit_error_payload(
    exc: SearchCreditError,
    search_id: Optional[str] = None,
) -> Dict[str, object]:
    """Serialize an insufficient-credit error for HTTP and SSE responses."""
    payload: Dict[str, object] = {
        "code": exc.code,
        "message": str(exc),
        "required_credits": exc.required_credits,
        "current_balance": exc.current_balance,
    }
    if search_id:
        payload["search_id"] = search_id
    return payload


def search_credit_cost(search_request: SearchRequest) -> int:
    """Return the credit cost for the requested search mode."""
    return SEARCH_CREDIT_COSTS[search_request.mode]


def check_search_credit(
    user: AccountUser | None,
    search_request: SearchRequest,
) -> tuple[AccountUser, int, CreditAccount]:
    """Validate authentication and balance before reserving paid API quota."""
    if user is None:
        raise SearchAuthenticationError()

    cost = search_credit_cost(search_request)
    account = account_service.get_credit_account(user.id)
    if account.balance < cost:
        raise SearchCreditError(cost, account.balance)

    return user, cost, account


def require_search_user(user: AccountUser | None) -> AccountUser:
    """Require authentication before cache lookup or paid search work."""
    if user is None:
        raise SearchAuthenticationError()

    return user


def debit_search_credit(
    user: AccountUser,
    cost: int,
    search_id: str,
) -> CreditLedgerRecord:
    """Atomically deduct credits immediately before external search work."""
    try:
        return account_service.adjust_credits(
            user.id,
            -cost,
            SEARCH_USAGE_REASON,
            SEARCH_REFERENCE_TYPE,
            search_id,
        )
    except InsufficientCreditError as exc:
        account = account_service.get_credit_account(user.id)
        raise SearchCreditError(cost, account.balance) from exc


def raise_search_auth_http_error(exc: SearchAuthenticationError) -> None:
    """Raise an HTTP error for unauthenticated paid search requests."""
    raise HTTPException(status_code=401, detail=search_auth_error_payload(exc))


def raise_search_credit_http_error(exc: SearchCreditError) -> None:
    """Raise an HTTP error for searches blocked by insufficient credits."""
    raise HTTPException(status_code=402, detail=search_credit_error_payload(exc))


def step_done_payload(step: SearchStep, **extra: object) -> Dict[str, object]:
    """Return a step completion payload and record its duration metric."""
    payload = step.done_payload(**extra)
    metrics.observe_ms(
        "search_step_duration_ms",
        int(payload["duration_ms"]),
        step=step.name,
        status="success",
    )
    return payload


def step_error_payload(step: SearchStep, exc: Exception) -> Dict[str, object]:
    """Return a step error payload and record its duration metric."""
    payload = step.error_payload(exc)
    metrics.observe_ms(
        "search_step_duration_ms",
        int(payload["duration_ms"]),
        step=step.name,
        status="error",
    )
    return payload


def trace_done_payload(observer: SearchObserver, **extra: object) -> Dict[str, object]:
    """Return a trace completion payload and record its total duration metric."""
    payload = observer.trace_done_payload(**extra)
    metrics.observe_ms(
        "search_trace_duration_ms",
        int(payload["duration_ms"]),
        status="success",
    )
    return payload


def trace_error_payload(observer: SearchObserver, exc: Exception) -> Dict[str, object]:
    """Return a trace error payload and record its total duration metric."""
    payload = observer.trace_error_payload(exc)
    metrics.observe_ms(
        "search_trace_duration_ms",
        int(payload["duration_ms"]),
        status="error",
    )
    return payload


def auth_user_payload(user: AccountUser) -> AuthUser:
    """Serialize a stored account user into the public API shape."""
    return AuthUser(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        status=user.status,
        created_at=user.created_at,
        is_admin=is_admin_user(user),
    )


def history_item_payload(item: HistoryRecord) -> SearchHistoryResponseItem:
    """Serialize a private history record into the public API shape."""
    return SearchHistoryResponseItem(
        id=item.id,
        query=item.query,
        mode=item.mode,
        created_at=item.created_at,
    )


def credit_account_payload(account: CreditAccount) -> CreditAccountResponse:
    """Serialize a private credit account into the public API shape."""
    return CreditAccountResponse(
        balance=account.balance,
        updated_at=account.updated_at,
    )


def credit_ledger_payload(item: CreditLedgerRecord) -> CreditLedgerResponseItem:
    """Serialize a private credit ledger row into the public API shape."""
    return CreditLedgerResponseItem(
        id=item.id,
        change_amount=item.change_amount,
        balance_after=item.balance_after,
        reason=item.reason,
        reference_type=item.reference_type,
        reference_id=item.reference_id,
        created_at=item.created_at,
    )


def admin_summary_payload(summary: AdminStatsSummary) -> AdminStatsSummaryResponse:
    """Serialize administrator summary counters into the API shape."""
    return AdminStatsSummaryResponse(
        total_users=summary.total_users,
        active_sessions=summary.active_sessions,
        total_history_items=summary.total_history_items,
        total_credit_balance=summary.total_credit_balance,
        total_credits_granted=summary.total_credits_granted,
        total_credits_spent=summary.total_credits_spent,
        total_search_debits=summary.total_search_debits,
        fast_history_items=summary.fast_history_items,
        deep_history_items=summary.deep_history_items,
        registered_today=summary.registered_today,
        searches_today=summary.searches_today,
        credits_spent_today=summary.credits_spent_today,
    )


def admin_recent_user_payload(item: AdminRecentUserStat) -> AdminRecentUserResponseItem:
    """Serialize a recent administrator user row into the API shape."""
    return AdminRecentUserResponseItem(
        id=item.id,
        email=item.email,
        display_name=item.display_name,
        balance=item.balance,
        history_count=item.history_count,
        created_at=item.created_at,
    )


def admin_managed_user_payload(item: AdminManagedUser) -> AdminManagedUserResponseItem:
    """Serialize a managed user row into the administrator API shape."""
    return AdminManagedUserResponseItem(
        id=item.id,
        email=item.email,
        display_name=item.display_name,
        role=item.role,
        status=item.status,
        balance=item.balance,
        history_count=item.history_count,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def admin_recent_search_payload(
    item: AdminRecentSearchStat,
) -> AdminRecentSearchResponseItem:
    """Serialize a recent administrator search row into the API shape."""
    return AdminRecentSearchResponseItem(
        id=item.id,
        user_email=item.user_email,
        query=item.query,
        mode=item.mode,
        created_at=item.created_at,
    )


def admin_credit_reason_payload(
    item: AdminCreditReasonStat,
) -> AdminCreditReasonResponseItem:
    """Serialize grouped administrator credit reason statistics."""
    return AdminCreditReasonResponseItem(
        reason=item.reason,
        ledger_count=item.ledger_count,
        total_change=item.total_change,
    )


def set_session_cookie(response: Response, session: SessionRecord) -> None:
    """Attach the HTTP-only session cookie used by the Vue client."""
    response.set_cookie(
        key=settings.session_cookie_name,
        value=session.token,
        max_age=settings.session_ttl_seconds,
        expires=session.expires_at,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response) -> None:
    """Clear the browser session cookie during logout."""
    response.delete_cookie(key=settings.session_cookie_name, path="/", samesite="lax")


def get_current_user(request: Request) -> AccountUser | None:
    """Return the authenticated user from the session cookie when available."""
    token = request.cookies.get(settings.session_cookie_name)
    return account_service.get_user_by_session(token)


def is_admin_user(user: AccountUser) -> bool:
    """Return whether the authenticated user is allowed to access admin APIs."""
    admin_emails = {email.strip().lower() for email in settings.admin_emails}
    return user.role == ADMIN_ROLE or user.email.strip().lower() in admin_emails


def require_current_user(request: Request) -> AccountUser:
    """Require a valid session cookie for private account endpoints."""
    user = get_current_user(request)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "authentication_required",
                "message": "请先登录后再操作",
            },
        )
    return user


def require_admin_user(request: Request) -> AccountUser:
    """Require a valid administrator session for platform statistics APIs."""
    user = require_current_user(request)
    if not is_admin_user(user):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "admin_required",
                "message": "当前账号没有后台统计权限",
            },
        )
    return user


def record_history_if_authenticated(
    user: AccountUser | None,
    search_request: SearchRequest,
) -> None:
    """Record search history only for the current authenticated user."""
    if user is None:
        return

    account_service.record_history(user.id, search_request.query, search_request.mode)


def no_results_answer(query: str) -> str:
    """Return a grounded answer when the search provider gives no sources."""
    return (
        f"暂时没有检索到与“{query}”相关的可用搜索结果。"
        "这通常是外部搜索源短时返回为空、网络波动，"
        "或关键词过于宽泛导致。请稍后重试，或补充更具体的关键词。"
    )


def no_results_response(request: SearchRequest) -> SearchResponse:
    """Build a non-cacheable response for empty source searches."""
    answer = no_results_answer(request.query)
    return SearchResponse(
        query=request.query,
        mode=request.mode,
        answer=answer,
        results=[],
        related_questions=generate_rule_based_related_questions(request.query),
    )


@app.get("/health")
async def health() -> Dict[str, str]:
    """Return service health for local checks and deployment probes."""
    return {"status": "ok", "service": settings.app_name}


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics_endpoint() -> str:
    """Return process-local metrics in text format."""
    return metrics.render_prometheus()


@app.post("/api/auth/register", response_model=AuthStatusResponse)
async def register_account(
    payload: RegisterRequest,
    request: Request,
    response: Response,
) -> AuthStatusResponse:
    """Create a user account and start an HTTP-only cookie session."""
    check_auth_rate_limit(request, payload.email)
    try:
        session = account_service.register(
            payload.email,
            payload.password,
            payload.display_name,
        )
    except DuplicateUserError as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc)}) from exc

    set_session_cookie(response, session)
    return AuthStatusResponse(user=auth_user_payload(session.user))


@app.post("/api/auth/login", response_model=AuthStatusResponse)
async def login_account(
    payload: LoginRequest,
    request: Request,
    response: Response,
) -> AuthStatusResponse:
    """Validate user credentials and start an HTTP-only cookie session."""
    check_auth_rate_limit(request, payload.email)
    try:
        session = account_service.login(payload.email, payload.password)
    except InvalidCredentialsError as exc:
        raise HTTPException(status_code=401, detail={"message": str(exc)}) from exc

    set_session_cookie(response, session)
    return AuthStatusResponse(user=auth_user_payload(session.user))


@app.post("/api/auth/logout", response_model=AuthStatusResponse)
async def logout_account(request: Request, response: Response) -> AuthStatusResponse:
    """Delete the active session and clear the browser cookie."""
    account_service.delete_session(request.cookies.get(settings.session_cookie_name))
    clear_session_cookie(response)
    return AuthStatusResponse(user=None)


@app.get("/api/auth/me", response_model=AuthStatusResponse)
async def get_auth_status(request: Request) -> AuthStatusResponse:
    """Return the current user when the session cookie is valid."""
    user = get_current_user(request)
    return AuthStatusResponse(user=auth_user_payload(user) if user else None)


@app.get("/api/history", response_model=SearchHistoryListResponse)
async def list_search_history(request: Request) -> SearchHistoryListResponse:
    """Return private search history for the authenticated user."""
    user = require_current_user(request)
    items = [history_item_payload(item) for item in account_service.list_history(user.id)]
    return SearchHistoryListResponse(items=items)


@app.delete("/api/history/{history_id}", response_model=SearchHistoryListResponse)
async def delete_search_history_item(
    history_id: int,
    request: Request,
) -> SearchHistoryListResponse:
    """Delete one private search history item owned by the current user."""
    user = require_current_user(request)
    if not account_service.delete_history(user.id, history_id):
        raise HTTPException(status_code=404, detail={"message": "历史记录不存在"})

    items = [history_item_payload(item) for item in account_service.list_history(user.id)]
    return SearchHistoryListResponse(items=items)


@app.delete("/api/history", response_model=SearchHistoryListResponse)
async def clear_search_history(request: Request) -> SearchHistoryListResponse:
    """Clear all private search history for the authenticated user."""
    user = require_current_user(request)
    account_service.clear_history(user.id)
    return SearchHistoryListResponse(items=[])


@app.get("/api/credits", response_model=CreditSummaryResponse)
async def get_credit_summary(request: Request) -> CreditSummaryResponse:
    """Return the current user's credit balance and recent ledger rows."""
    user = require_current_user(request)
    account = account_service.get_credit_account(user.id)
    ledger = account_service.list_credit_ledger(user.id)
    return CreditSummaryResponse(
        account=credit_account_payload(account),
        ledger=[credit_ledger_payload(item) for item in ledger],
    )


@app.get("/api/admin/users", response_model=AdminUserListResponse)
async def list_admin_users(
    request: Request,
    query: str = Query("", max_length=120),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> AdminUserListResponse:
    """Return paginated user-management rows for authenticated administrators."""
    require_admin_user(request)
    result = account_service.list_admin_users(query, limit, offset)
    return AdminUserListResponse(
        items=[admin_managed_user_payload(item) for item in result.items],
        total=result.total,
        limit=result.limit,
        offset=result.offset,
    )


@app.post("/api/admin/users", response_model=AdminManagedUserResponseItem)
async def create_admin_user(
    payload: AdminCreateUserRequest,
    request: Request,
) -> AdminManagedUserResponseItem:
    """Create a managed user account as an authenticated administrator."""
    require_admin_user(request)
    try:
        user = account_service.create_user_as_admin(
            payload.email,
            payload.password,
            payload.display_name,
            payload.role,
            payload.status,
        )
    except DuplicateUserError as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"message": str(exc)}) from exc

    return admin_managed_user_payload(user)


@app.patch("/api/admin/users/{user_id}", response_model=AdminManagedUserResponseItem)
async def update_admin_user(
    user_id: int,
    payload: AdminUpdateUserRequest,
    request: Request,
) -> AdminManagedUserResponseItem:
    """Update a managed user's display name, role, or account status."""
    admin_user = require_admin_user(request)
    if payload.display_name is None and payload.role is None and payload.status is None:
        raise HTTPException(
            status_code=400,
            detail={"message": "至少需要提交一个需要更新的字段"},
        )

    if user_id == admin_user.id and payload.status == DISABLED_STATUS:
        raise HTTPException(
            status_code=400,
            detail={"message": "管理员不能禁用自己的账号"},
        )
    if user_id == admin_user.id and payload.role == USER_ROLE:
        raise HTTPException(
            status_code=400,
            detail={"message": "管理员不能移除自己的管理员角色"},
        )

    try:
        user = account_service.update_user_as_admin(
            user_id,
            payload.display_name,
            payload.role,
            payload.status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"message": str(exc)}) from exc

    if user is None:
        raise HTTPException(status_code=404, detail={"message": "用户不存在"})

    return admin_managed_user_payload(user)


@app.delete("/api/admin/users/{user_id}", response_model=AdminDeleteUserResponse)
async def delete_admin_user(
    user_id: int,
    request: Request,
) -> AdminDeleteUserResponse:
    """Delete a managed user account while preventing administrator self-lockout."""
    admin_user = require_admin_user(request)
    if user_id == admin_user.id:
        raise HTTPException(
            status_code=400,
            detail={"message": "管理员不能删除自己的账号"},
        )

    deleted = account_service.delete_user_as_admin(user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail={"message": "用户不存在"})

    return AdminDeleteUserResponse(deleted=True)


@app.post(
    "/api/admin/users/{user_id}/credits",
    response_model=AdminCreditAdjustmentResponse,
)
async def adjust_admin_user_credits(
    user_id: int,
    payload: AdminCreditAdjustmentRequest,
    request: Request,
) -> AdminCreditAdjustmentResponse:
    """Adjust one managed user's credits and append an auditable ledger row."""
    admin_user = require_admin_user(request)
    if payload.change_amount == 0:
        raise HTTPException(
            status_code=400,
            detail={"message": "积分调整值不能为 0"},
        )
    if account_service.get_admin_user(user_id) is None:
        raise HTTPException(status_code=404, detail={"message": "用户不存在"})

    try:
        ledger = account_service.adjust_credits(
            user_id,
            payload.change_amount,
            payload.reason,
            "admin_user_adjustment",
            str(admin_user.id),
        )
    except InsufficientCreditError as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"message": str(exc)}) from exc

    account = account_service.get_credit_account(user_id)
    user = account_service.get_admin_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail={"message": "用户不存在"})

    return AdminCreditAdjustmentResponse(
        user=admin_managed_user_payload(user),
        account=credit_account_payload(account),
        ledger=credit_ledger_payload(ledger),
    )


@app.get("/api/admin/stats", response_model=AdminStatsResponse)
async def get_admin_stats(request: Request) -> AdminStatsResponse:
    """Return platform-wide statistics for authenticated administrators."""
    require_admin_user(request)
    snapshot = account_service.get_admin_stats()
    return AdminStatsResponse(
        summary=admin_summary_payload(snapshot.summary),
        recent_users=[admin_recent_user_payload(item) for item in snapshot.recent_users],
        recent_searches=[
            admin_recent_search_payload(item)
            for item in snapshot.recent_searches
        ],
        credit_reasons=[
            admin_credit_reason_payload(item)
            for item in snapshot.credit_reasons
        ],
    )


@app.get("/api/media-proxy")
async def media_proxy(
    url: str = Query(..., min_length=8, max_length=2048),
) -> Response:
    """Proxy remote image previews so result cards can display stable covers."""
    try:
        media = await fetch_remote_media(url)
    except MediaProxyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    return Response(
        content=media.content,
        media_type=media.media_type,
        headers={
            "Cache-Control": "public, max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/api/search", response_model=SearchResponse)
async def search_once(request: SearchRequest, http_request: Request) -> SearchResponse:
    """Run the full search pipeline without streaming."""
    observer = SearchObserver(request.query)
    active_step = None
    client_id = get_client_id(http_request)
    current_user = get_current_user(http_request)
    metrics.increment("search_requests_total", endpoint="search", mode=request.mode)
    observer.trace_start_payload()

    try:
        rate_step = observer.step("rate_limit")
        active_step = rate_step
        rate_step.start_payload()
        cost_guard.check_rate_limit(client_id)
        step_done_payload(rate_step, client_id=client_id)
        active_step = None

        security_step = observer.step("security_check")
        active_step = security_step
        security_step.start_payload()
        security_service.check_query(request.query)
        step_done_payload(security_step)
        active_step = None

        auth_step = observer.step("authentication")
        active_step = auth_step
        auth_step.start_payload()
        current_user = require_search_user(current_user)
        step_done_payload(auth_step, user_id=current_user.id)
        active_step = None

        cache_step = observer.step("cache_lookup")
        active_step = cache_step
        cache_step.start_payload()
        cached = search_cache.get(request.query, request.mode)
        step_done_payload(cache_step, cache_hit=cached is not None)
        active_step = None
        if cached is not None:
            record_history_if_authenticated(current_user, request)
            metrics.increment("search_cache_hits_total", mode=request.mode)
            trace_done_payload(observer, cache_hit=True)
            return cached
        metrics.increment("search_cache_misses_total", mode=request.mode)

        credit_check_step = observer.step("credit_check")
        active_step = credit_check_step
        credit_check_step.start_payload()
        current_user, credit_cost, credit_account = check_search_credit(current_user, request)
        step_done_payload(
            credit_check_step,
            required_credits=credit_cost,
            balance_before=credit_account.balance,
        )
        active_step = None

        quota_step = observer.step("external_quota")
        active_step = quota_step
        quota_step.start_payload()
        cost_guard.reserve_external_quota(client_id)
        step_done_payload(quota_step, client_id=client_id)
        active_step = None

        credit_debit_step = observer.step("credit_debit")
        active_step = credit_debit_step
        credit_debit_step.start_payload()
        credit_ledger = debit_search_credit(
            current_user,
            credit_cost,
            observer.search_id,
        )
        step_done_payload(
            credit_debit_step,
            charged_credits=credit_cost,
            balance_after=credit_ledger.balance_after,
        )
        active_step = None
        record_history_if_authenticated(current_user, request)

        search_service = get_search_service()
        ai_service = get_ai_service()

        search_step = observer.step("source_search")
        active_step = search_step
        search_step.start_payload()
        results = await search_service.search(request.query)
        results = security_service.sanitize_search_results(results)
        step_done_payload(search_step, result_count=len(results))
        active_step = None

        if not results:
            answer_step = observer.step("ai_answer_stream")
            active_step = answer_step
            answer_step.start_payload()
            response = no_results_response(request)
            step_done_payload(
                answer_step,
                chunk_count=0,
                answer_chars=len(response.answer),
                skipped=True,
            )
            active_step = None

            related_step = observer.step("related_questions")
            active_step = related_step
            related_step.start_payload()
            step_done_payload(
                related_step,
                question_count=len(response.related_questions),
                skipped=True,
            )
            active_step = None
            trace_done_payload(observer, cache_hit=False, result_count=0, cache_write=False)
            return response

        answer_parts: List[str] = []
        answer_step = observer.step("ai_answer_stream")
        active_step = answer_step
        answer_step.start_payload()
        async for token in ai_service.stream_answer(request.query, results, request.mode):
            answer_parts.append(token)

        answer = "".join(answer_parts)
        answer = security_service.sanitize_model_output(answer)
        step_done_payload(answer_step, chunk_count=len(answer_parts), answer_chars=len(answer))
        active_step = None

        related_step = observer.step("related_questions")
        active_step = related_step
        related_step.start_payload()
        related = await ai_service.generate_related_questions(
            request.query,
            answer,
            request.mode,
        )
        step_done_payload(related_step, question_count=len(related))
        active_step = None

        response = SearchResponse(
            query=request.query,
            mode=request.mode,
            answer=answer,
            results=results,
            related_questions=related,
        )

        cache_write_step = observer.step("cache_write")
        active_step = cache_write_step
        cache_write_step.start_payload()
        search_cache.set(response)
        step_done_payload(cache_write_step)
        active_step = None
        trace_done_payload(observer, cache_hit=False)
        return response
    except CostGuardError as exc:
        if active_step is not None:
            step_error_payload(active_step, exc)
        trace_error_payload(observer, exc)
        metrics.increment("search_errors_total", endpoint="search", code=exc.code)
        raise_guard_http_error(exc)
    except SecurityBlockedError as exc:
        if active_step is not None:
            step_error_payload(active_step, exc)
        trace_error_payload(observer, exc)
        metrics.increment("search_errors_total", endpoint="search", code=exc.code)
        raise_security_http_error(exc)
    except SearchAuthenticationError as exc:
        if active_step is not None:
            step_error_payload(active_step, exc)
        trace_error_payload(observer, exc)
        metrics.increment("search_errors_total", endpoint="search", code=exc.code)
        raise_search_auth_http_error(exc)
    except SearchCreditError as exc:
        if active_step is not None:
            step_error_payload(active_step, exc)
        trace_error_payload(observer, exc)
        metrics.increment("search_errors_total", endpoint="search", code=exc.code)
        raise_search_credit_http_error(exc)
    except Exception as exc:
        if active_step is not None:
            step_error_payload(active_step, exc)
        trace_error_payload(observer, exc)
        metrics.increment("search_errors_total", endpoint="search", code="unhandled")
        raise


@app.post("/api/search/stream")
async def search_stream(request: SearchRequest, http_request: Request) -> StreamingResponse:
    """Stream search progress, answer tokens, and related questions as SSE."""
    client_id = get_client_id(http_request)
    current_user = get_current_user(http_request)
    metrics.increment("search_requests_total", endpoint="stream", mode=request.mode)

    async def event_generator() -> AsyncIterator[str]:
        """Yield SSE frames for the complete AI search lifecycle."""
        nonlocal current_user

        observer = SearchObserver(request.query)
        active_step = None
        stream_acquired = False
        try:
            yield format_sse("trace_start", observer.trace_start_payload())
            yield format_sse(
                "search_start",
                {"query": request.query, "search_id": observer.search_id},
            )

            rate_step = observer.step("rate_limit")
            active_step = rate_step
            yield format_sse("step_start", rate_step.start_payload())
            cost_guard.check_rate_limit(client_id)
            yield format_sse("step_done", step_done_payload(rate_step, client_id=client_id))
            active_step = None

            security_step = observer.step("security_check")
            active_step = security_step
            yield format_sse("step_start", security_step.start_payload())
            security_service.check_query(request.query)
            yield format_sse("step_done", step_done_payload(security_step))
            active_step = None

            auth_step = observer.step("authentication")
            active_step = auth_step
            yield format_sse("step_start", auth_step.start_payload())
            current_user = require_search_user(current_user)
            yield format_sse(
                "step_done",
                step_done_payload(auth_step, user_id=current_user.id),
            )
            active_step = None

            concurrency_step = observer.step("stream_concurrency")
            active_step = concurrency_step
            yield format_sse("step_start", concurrency_step.start_payload())
            cost_guard.acquire_stream(client_id)
            stream_acquired = True
            yield format_sse(
                "step_done",
                step_done_payload(concurrency_step, client_id=client_id),
            )
            active_step = None

            cache_step = observer.step("cache_lookup")
            active_step = cache_step
            yield format_sse("step_start", cache_step.start_payload())
            cached = search_cache.get(request.query, request.mode)
            yield format_sse(
                "step_done",
                step_done_payload(cache_step, cache_hit=cached is not None),
            )
            active_step = None
            if cached is not None:
                record_history_if_authenticated(current_user, request)
                metrics.increment("search_cache_hits_total", mode=request.mode)
                yield format_sse(
                    "cache_hit",
                    {"query": cached.query, "search_id": observer.search_id},
                )
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
                yield format_sse("trace_done", trace_done_payload(observer, cache_hit=True))
                yield format_sse("done", {})
                return
            metrics.increment("search_cache_misses_total", mode=request.mode)

            credit_check_step = observer.step("credit_check")
            active_step = credit_check_step
            yield format_sse("step_start", credit_check_step.start_payload())
            user, credit_cost, credit_account = check_search_credit(current_user, request)
            yield format_sse(
                "step_done",
                step_done_payload(
                    credit_check_step,
                    required_credits=credit_cost,
                    balance_before=credit_account.balance,
                ),
            )
            active_step = None

            quota_step = observer.step("external_quota")
            active_step = quota_step
            yield format_sse("step_start", quota_step.start_payload())
            cost_guard.reserve_external_quota(client_id)
            yield format_sse("step_done", step_done_payload(quota_step, client_id=client_id))
            active_step = None

            credit_debit_step = observer.step("credit_debit")
            active_step = credit_debit_step
            yield format_sse("step_start", credit_debit_step.start_payload())
            credit_ledger = debit_search_credit(user, credit_cost, observer.search_id)
            yield format_sse(
                "step_done",
                step_done_payload(
                    credit_debit_step,
                    charged_credits=credit_cost,
                    balance_after=credit_ledger.balance_after,
                ),
            )
            active_step = None
            record_history_if_authenticated(user, request)

            search_service = get_search_service()
            ai_service = get_ai_service()
            answer_parts: List[str] = []

            search_step = observer.step("source_search")
            active_step = search_step
            yield format_sse("step_start", search_step.start_payload())
            results = await search_service.search(request.query)
            results = security_service.sanitize_search_results(results)
            yield format_sse(
                "step_done",
                step_done_payload(search_step, result_count=len(results)),
            )
            active_step = None
            yield format_sse(
                "sources",
                {"results": [result.model_dump(mode="json") for result in results]},
            )
            yield format_sse("answer_start", {})

            if not results:
                answer_step = observer.step("ai_answer_stream")
                active_step = answer_step
                yield format_sse("step_start", answer_step.start_payload())
                response = no_results_response(request)
                yield format_sse(
                    "step_done",
                    step_done_payload(
                        answer_step,
                        chunk_count=0,
                        answer_chars=len(response.answer),
                        skipped=True,
                    ),
                )
                active_step = None
                yield format_sse("answer_done", {"answer": response.answer})

                related_step = observer.step("related_questions")
                active_step = related_step
                yield format_sse("step_start", related_step.start_payload())
                yield format_sse(
                    "step_done",
                    step_done_payload(
                        related_step,
                        question_count=len(response.related_questions),
                        skipped=True,
                    ),
                )
                active_step = None
                yield format_sse("related", {"questions": response.related_questions})
                yield format_sse(
                    "trace_done",
                    trace_done_payload(
                        observer,
                        cache_hit=False,
                        result_count=0,
                        cache_write=False,
                    ),
                )
                yield format_sse("done", {})
                return

            answer_step = observer.step("ai_answer_stream")
            active_step = answer_step
            yield format_sse("step_start", answer_step.start_payload())
            async for token in ai_service.stream_answer(request.query, results, request.mode):
                answer_parts.append(token)
                yield format_sse("token", {"text": token})

            answer = "".join(answer_parts)
            answer = security_service.sanitize_model_output(answer)
            yield format_sse(
                "step_done",
                step_done_payload(
                    answer_step,
                    chunk_count=len(answer_parts),
                    answer_chars=len(answer),
                ),
            )
            active_step = None
            yield format_sse("answer_done", {"answer": answer})

            related_step = observer.step("related_questions")
            active_step = related_step
            yield format_sse("step_start", related_step.start_payload())
            related = await ai_service.generate_related_questions(
                request.query,
                answer,
                request.mode,
            )
            yield format_sse(
                "step_done",
                step_done_payload(related_step, question_count=len(related)),
            )
            active_step = None
            yield format_sse("related", {"questions": related})

            cache_write_step = observer.step("cache_write")
            active_step = cache_write_step
            yield format_sse("step_start", cache_write_step.start_payload())
            search_cache.set(
                SearchResponse(
                    query=request.query,
                    mode=request.mode,
                    answer=answer,
                    results=results,
                    related_questions=related,
                )
            )
            yield format_sse("step_done", step_done_payload(cache_write_step))
            active_step = None
            yield format_sse("trace_done", trace_done_payload(observer, cache_hit=False))
            yield format_sse("done", {})
        except CostGuardError as exc:
            if active_step is not None:
                yield format_sse("step_error", step_error_payload(active_step, exc))
            yield format_sse("trace_error", trace_error_payload(observer, exc))
            yield format_sse(
                "error",
                guard_error_payload(exc, observer.search_id),
            )
            metrics.increment("search_errors_total", endpoint="stream", code=exc.code)
        except SecurityBlockedError as exc:
            if active_step is not None:
                yield format_sse("step_error", step_error_payload(active_step, exc))
            yield format_sse("trace_error", trace_error_payload(observer, exc))
            yield format_sse(
                "error",
                security_error_payload(exc, observer.search_id),
            )
            metrics.increment("search_errors_total", endpoint="stream", code=exc.code)
        except SearchAuthenticationError as exc:
            if active_step is not None:
                yield format_sse("step_error", step_error_payload(active_step, exc))
            yield format_sse("trace_error", trace_error_payload(observer, exc))
            yield format_sse(
                "error",
                search_auth_error_payload(exc, observer.search_id),
            )
            metrics.increment("search_errors_total", endpoint="stream", code=exc.code)
        except SearchCreditError as exc:
            if active_step is not None:
                yield format_sse("step_error", step_error_payload(active_step, exc))
            yield format_sse("trace_error", trace_error_payload(observer, exc))
            yield format_sse(
                "error",
                search_credit_error_payload(exc, observer.search_id),
            )
            metrics.increment("search_errors_total", endpoint="stream", code=exc.code)
        except Exception as exc:
            if active_step is not None:
                yield format_sse("step_error", step_error_payload(active_step, exc))
            yield format_sse("trace_error", trace_error_payload(observer, exc))
            yield format_sse(
                "error",
                {"message": str(exc), "search_id": observer.search_id},
            )
            metrics.increment("search_errors_total", endpoint="stream", code="unhandled")
        finally:
            if stream_acquired:
                cost_guard.release_stream(client_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
