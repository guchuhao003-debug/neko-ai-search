"""Tests for rate limiting, quota, and stream concurrency guards."""

from dataclasses import replace

import pytest

from app.config import get_settings
from app.services.cost_guard_service import (
    ConcurrencyLimitExceeded,
    InMemoryCostGuard,
    QuotaExceeded,
    RateLimitExceeded,
    SQLiteCostGuard,
)


def _guard(**overrides: int) -> InMemoryCostGuard:
    """Create a guard with test-friendly settings."""
    settings = replace(
        get_settings(),
        rate_limit_per_minute=overrides.get("rate_limit_per_minute", 2),
        ip_daily_external_quota=overrides.get("ip_daily_external_quota", 2),
        global_daily_external_quota=overrides.get("global_daily_external_quota", 4),
        ip_concurrent_streams=overrides.get("ip_concurrent_streams", 1),
    )
    return InMemoryCostGuard(settings)


def test_rate_limit_blocks_after_minute_budget() -> None:
    """A client should be blocked after exceeding the per-minute request budget."""
    guard = _guard(rate_limit_per_minute=2)

    guard.check_rate_limit("127.0.0.1")
    guard.check_rate_limit("127.0.0.1")

    with pytest.raises(RateLimitExceeded):
        guard.check_rate_limit("127.0.0.1")


def test_external_quota_blocks_after_daily_budget() -> None:
    """A client should be blocked after exceeding daily paid-call quota."""
    guard = _guard(ip_daily_external_quota=1)

    guard.reserve_external_quota("127.0.0.1")

    with pytest.raises(QuotaExceeded):
        guard.reserve_external_quota("127.0.0.1")


def test_stream_concurrency_blocks_parallel_requests() -> None:
    """A client should not exceed the configured active stream count."""
    guard = _guard(ip_concurrent_streams=1)

    guard.acquire_stream("127.0.0.1")
    with pytest.raises(ConcurrencyLimitExceeded):
        guard.acquire_stream("127.0.0.1")

    guard.release_stream("127.0.0.1")
    guard.acquire_stream("127.0.0.1")


def test_sqlite_quota_persists_across_guard_instances(tmp_path) -> None:
    """SQLite quota counters should survive guard object recreation."""
    settings = replace(
        get_settings(),
        ip_daily_external_quota=1,
        global_daily_external_quota=3,
    )
    db_path = tmp_path / "cost_guard.sqlite3"

    first_guard = SQLiteCostGuard(settings, str(db_path))
    first_guard.reserve_external_quota("127.0.0.1")

    second_guard = SQLiteCostGuard(settings, str(db_path))
    with pytest.raises(QuotaExceeded):
        second_guard.reserve_external_quota("127.0.0.1")


def test_sqlite_rate_limit_blocks_after_minute_budget(tmp_path) -> None:
    """SQLite rate counters should block clients within the current minute."""
    settings = replace(get_settings(), rate_limit_per_minute=1)
    guard = SQLiteCostGuard(settings, str(tmp_path / "cost_guard.sqlite3"))

    guard.check_rate_limit("127.0.0.1")

    with pytest.raises(RateLimitExceeded):
        guard.check_rate_limit("127.0.0.1")


def test_sqlite_active_streams_clear_on_new_process_start(tmp_path) -> None:
    """SQLite active stream counters should not stay stuck after restart."""
    settings = replace(get_settings(), ip_concurrent_streams=1)
    db_path = tmp_path / "cost_guard.sqlite3"

    first_guard = SQLiteCostGuard(settings, str(db_path))
    first_guard.acquire_stream("127.0.0.1")
    with pytest.raises(ConcurrencyLimitExceeded):
        first_guard.acquire_stream("127.0.0.1")

    second_guard = SQLiteCostGuard(settings, str(db_path))
    second_guard.acquire_stream("127.0.0.1")
