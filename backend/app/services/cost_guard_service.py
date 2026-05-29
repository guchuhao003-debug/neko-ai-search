"""In-memory rate limiting, quota, and concurrency guards."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from threading import Lock
from time import monotonic

from app.config import Settings


@dataclass(frozen=True)
class CostGuardError(Exception):
    """Base error returned when a request is blocked by cost controls."""

    code: str
    message: str
    retry_after_seconds: int | None = None

    def __str__(self) -> str:
        """Return the user-facing guard message."""
        return self.message


class RateLimitExceeded(CostGuardError):
    """Raised when an IP exceeds the short-term request rate."""


class QuotaExceeded(CostGuardError):
    """Raised when an IP or the service exceeds daily external API quota."""


class ConcurrencyLimitExceeded(CostGuardError):
    """Raised when an IP has too many active streaming searches."""


class InMemoryCostGuard:
    """Protect paid search and model APIs with process-local limits."""

    def __init__(
        self,
        settings: Settings,
        *,
        clock: Callable[[], float] = monotonic,
        today: Callable[[], date] | None = None,
    ) -> None:
        """Create the in-memory guard from runtime settings."""
        self.settings = settings
        self.clock = clock
        self.today = today or (lambda: datetime.now(timezone.utc).date())
        self._minute_hits: dict[str, deque[float]] = defaultdict(deque)
        self._ip_daily_external: dict[str, int] = defaultdict(int)
        self._global_daily_external = 0
        self._active_streams: dict[str, int] = defaultdict(int)
        self._quota_day = self.today()
        self._lock = Lock()

    def check_rate_limit(self, client_id: str) -> None:
        """Block clients that exceed the per-minute request limit."""
        now = self.clock()
        window_start = now - 60

        with self._lock:
            hits = self._minute_hits[client_id]
            while hits and hits[0] <= window_start:
                hits.popleft()

            limit = self.settings.rate_limit_per_minute
            if len(hits) >= limit:
                retry_after = max(round(60 - (now - hits[0])), 1)
                raise RateLimitExceeded(
                    code="rate_limited",
                    message="搜索过于频繁，请稍后再试。",
                    retry_after_seconds=retry_after,
                )

            hits.append(now)

    def reserve_external_quota(self, client_id: str) -> None:
        """Reserve quota before paid Tavily or DeepSeek calls are made."""
        with self._lock:
            self._reset_daily_quota_if_needed()

            ip_limit = self.settings.ip_daily_external_quota
            global_limit = self.settings.global_daily_external_quota
            if self._ip_daily_external[client_id] >= ip_limit:
                raise QuotaExceeded(
                    code="ip_quota_exceeded",
                    message="今日搜索配额已用完，请明天再试。",
                    retry_after_seconds=self._seconds_until_tomorrow(),
                )

            if self._global_daily_external >= global_limit:
                raise QuotaExceeded(
                    code="global_quota_exceeded",
                    message="平台今日搜索预算已用完，请稍后再试。",
                    retry_after_seconds=self._seconds_until_tomorrow(),
                )

            self._ip_daily_external[client_id] += 1
            self._global_daily_external += 1

    def acquire_stream(self, client_id: str) -> None:
        """Reserve one active streaming slot for a client."""
        with self._lock:
            active = self._active_streams[client_id]
            if active >= self.settings.ip_concurrent_streams:
                raise ConcurrencyLimitExceeded(
                    code="too_many_concurrent_requests",
                    message="当前搜索请求较多，请稍后再试。",
                    retry_after_seconds=10,
                )

            self._active_streams[client_id] += 1

    def release_stream(self, client_id: str) -> None:
        """Release one active streaming slot for a client."""
        with self._lock:
            active = self._active_streams.get(client_id, 0)
            if active <= 1:
                self._active_streams.pop(client_id, None)
                return

            self._active_streams[client_id] = active - 1

    def reset(self) -> None:
        """Clear all rate, quota, and concurrency counters for tests."""
        with self._lock:
            self._minute_hits.clear()
            self._ip_daily_external.clear()
            self._global_daily_external = 0
            self._active_streams.clear()
            self._quota_day = self.today()

    def _reset_daily_quota_if_needed(self) -> None:
        """Reset external quota counters when the UTC day changes."""
        current_day = self.today()
        if current_day == self._quota_day:
            return

        self._ip_daily_external.clear()
        self._global_daily_external = 0
        self._quota_day = current_day

    def _seconds_until_tomorrow(self) -> int:
        """Return an approximate retry delay until the next UTC day."""
        return 24 * 60 * 60
