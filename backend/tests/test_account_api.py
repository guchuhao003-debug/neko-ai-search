"""Tests for session-cookie authentication and private history APIs."""

from __future__ import annotations

from dataclasses import replace
from typing import AsyncIterator, List

import pytest
from httpx import ASGITransport, AsyncClient

from app import main
from app.schemas import SearchResult
from app.services.account_service import AccountService
from app.services.cost_guard_service import InMemoryCostGuard


class AuthSearchService:
    """Fake search provider used to avoid external API calls in auth tests."""

    def __init__(self) -> None:
        """Initialize the call counter."""
        self.calls = 0

    async def search(self, query: str) -> List[SearchResult]:
        """Return a deterministic result for one query."""
        self.calls += 1
        return [
            SearchResult(
                id=1,
                title=f"Source for {query}",
                url="https://example.com/auth-source",
                content="Auth test source content",
            )
        ]


class AuthAiService:
    """Fake AI provider used to avoid model calls in auth tests."""

    async def stream_answer(
        self,
        query: str,
        results: List[SearchResult],
        mode: str = "fast",
    ) -> AsyncIterator[str]:
        """Yield a deterministic answer token."""
        yield f"Answer for {query} [1]"

    async def generate_related_questions(
        self,
        query: str,
        answer: str,
        mode: str = "fast",
    ) -> List[str]:
        """Return one deterministic related question."""
        return [f"More about {query}"]


@pytest.fixture()
def isolated_accounts(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> AccountService:
    """Swap the module-level account service for a temp SQLite database."""
    service = AccountService(str(tmp_path / "accounts.sqlite3"), session_ttl_seconds=3600)
    monkeypatch.setattr(main, "account_service", service)
    main.search_cache.clear()
    main.cost_guard.reset()
    main.metrics.reset()
    return service


@pytest.mark.anyio
async def test_register_login_me_and_logout_use_session_cookie(
    isolated_accounts: AccountService,
) -> None:
    """Register, read the current user, logout, and login again through cookies."""
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        registered = await client.post(
            "/api/auth/register",
            json={
                "email": "User@Example.com",
                "password": "strong-password",
                "display_name": "Neko User",
            },
        )
        me_after_register = await client.get("/api/auth/me")
        logout = await client.post("/api/auth/logout")
        me_after_logout = await client.get("/api/auth/me")
        login = await client.post(
            "/api/auth/login",
            json={"email": "user@example.com", "password": "strong-password"},
        )
        me_after_login = await client.get("/api/auth/me")

    assert registered.status_code == 200
    assert "neko_session" in registered.headers["set-cookie"]
    assert registered.json()["user"]["email"] == "user@example.com"
    assert me_after_register.json()["user"]["display_name"] == "Neko User"
    assert logout.status_code == 200
    assert me_after_logout.json()["user"] is None
    assert login.status_code == 200
    assert me_after_login.json()["user"]["email"] == "user@example.com"


@pytest.mark.anyio
async def test_duplicate_email_and_wrong_password_are_rejected(
    isolated_accounts: AccountService,
) -> None:
    """Duplicate registrations and invalid credentials should not authenticate."""
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/auth/register",
            json={
                "email": "dupe@example.com",
                "password": "strong-password",
                "display_name": "First User",
            },
        )
        duplicate = await client.post(
            "/api/auth/register",
            json={
                "email": "DUPE@example.com",
                "password": "strong-password",
                "display_name": "Second User",
            },
        )
        wrong_password = await client.post(
            "/api/auth/login",
            json={"email": "dupe@example.com", "password": "wrong-password"},
        )

    assert first.status_code == 200
    assert duplicate.status_code == 409
    assert wrong_password.status_code == 401


@pytest.mark.anyio
async def test_history_requires_auth_and_isolates_users(
    isolated_accounts: AccountService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authenticated search history should stay private per user."""
    search_service = AuthSearchService()
    monkeypatch.setattr(main, "get_search_service", lambda: search_service)
    monkeypatch.setattr(main, "get_ai_service", lambda: AuthAiService())

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as anonymous:
        rejected = await anonymous.get("/api/history")

    async with AsyncClient(transport=transport, base_url="http://test") as user_a:
        await user_a.post(
            "/api/auth/register",
            json={
                "email": "a@example.com",
                "password": "strong-password",
                "display_name": "User A",
            },
        )
        search = await user_a.post(
            "/api/search/stream",
            json={"query": "private neko topic", "mode": "deep"},
        )
        user_a_history = await user_a.get("/api/history")

    async with AsyncClient(transport=transport, base_url="http://test") as user_b:
        await user_b.post(
            "/api/auth/register",
            json={
                "email": "b@example.com",
                "password": "strong-password",
                "display_name": "User B",
            },
        )
        user_b_history = await user_b.get("/api/history")
        foreign_history_id = user_a_history.json()["items"][0]["id"]
        delete_foreign = await user_b.delete(f"/api/history/{foreign_history_id}")

    assert rejected.status_code == 401
    assert search.status_code == 200
    assert search_service.calls == 1
    assert user_a_history.status_code == 200
    assert user_a_history.json()["items"] == [
        {
            "id": user_a_history.json()["items"][0]["id"],
            "query": "private neko topic",
            "mode": "deep",
            "created_at": user_a_history.json()["items"][0]["created_at"],
        }
    ]
    assert user_b_history.status_code == 200
    assert user_b_history.json()["items"] == []
    assert delete_foreign.status_code == 404


@pytest.mark.anyio
async def test_history_delete_only_affects_current_user(
    isolated_accounts: AccountService,
) -> None:
    """Deleting one history row should use both item ID and current user ID."""
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/auth/register",
            json={
                "email": "clean@example.com",
                "password": "strong-password",
                "display_name": "Cleaner",
            },
        )
        user = await client.get("/api/auth/me")
        user_id = user.json()["user"]["id"]
        item = isolated_accounts.record_history(user_id, "delete me", "fast")
        deleted = await client.delete(f"/api/history/{item.id}")
        remaining = await client.get("/api/history")

    assert deleted.status_code == 200
    assert deleted.json()["items"] == []
    assert remaining.json()["items"] == []


@pytest.mark.anyio
async def test_non_streaming_search_records_authenticated_history(
    isolated_accounts: AccountService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-streaming searches should also persist private user history."""
    monkeypatch.setattr(main, "get_search_service", lambda: AuthSearchService())
    monkeypatch.setattr(main, "get_ai_service", lambda: AuthAiService())

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/auth/register",
            json={
                "email": "sync@example.com",
                "password": "strong-password",
                "display_name": "Sync User",
            },
        )
        response = await client.post(
            "/api/search",
            json={"query": "sync private history", "mode": "fast"},
        )
        history = await client.get("/api/history")

    assert response.status_code == 200
    assert history.json()["items"][0]["query"] == "sync private history"
    assert history.json()["items"][0]["mode"] == "fast"


@pytest.mark.anyio
async def test_quota_blocked_search_does_not_record_history(
    isolated_accounts: AccountService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quota failures should not pollute private search history."""
    settings = replace(
        main.settings,
        rate_limit_per_minute=100,
        ip_daily_external_quota=0,
        global_daily_external_quota=0,
        ip_concurrent_streams=10,
    )
    search_service = AuthSearchService()
    monkeypatch.setattr(main, "cost_guard", InMemoryCostGuard(settings))
    monkeypatch.setattr(main, "get_search_service", lambda: search_service)
    monkeypatch.setattr(main, "get_ai_service", lambda: AuthAiService())

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/auth/register",
            json={
                "email": "quota@example.com",
                "password": "strong-password",
                "display_name": "Quota User",
            },
        )
        response = await client.post(
            "/api/search",
            json={"query": "blocked by quota", "mode": "fast"},
        )
        history = await client.get("/api/history")

    assert response.status_code == 429
    assert history.json()["items"] == []
    assert search_service.calls == 0


def test_history_deduplicates_case_insensitive_queries(
    isolated_accounts: AccountService,
) -> None:
    """Repeated queries should move to the top instead of creating duplicates."""
    session = isolated_accounts.register(
        "dedupe@example.com",
        "strong-password",
        "Dedupe User",
    )
    isolated_accounts.record_history(session.user.id, "Neko Search", "fast")
    latest = isolated_accounts.record_history(session.user.id, "neko search", "deep")

    items = isolated_accounts.list_history(session.user.id)

    assert len(items) == 1
    assert items[0].id == latest.id
    assert items[0].query == "neko search"
    assert items[0].mode == "deep"
