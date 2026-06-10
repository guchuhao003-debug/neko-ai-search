"""Rate limiting, quota, and concurrency guards for paid API protection."""

from __future__ import annotations

import sqlite3
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from threading import Lock
from time import monotonic

from app.config import Settings


GLOBAL_QUOTA_CLIENT = "__global__"
GLOBAL_QUOTA_SCOPE = "global"
IP_QUOTA_SCOPE = "ip"


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


class SQLiteCostGuard:
    """Protect paid APIs with counters persisted in a local SQLite database."""

    def __init__(
        self,
        settings: Settings,
        db_path: str,
        *,
        clock: Callable[[], float] = monotonic,
        today: Callable[[], date] | None = None,
    ) -> None:
        """Create the SQLite-backed guard and initialize its tables."""
        self.settings = settings
        self.db_path = Path(db_path)
        self.clock = clock
        self.today = today or (lambda: datetime.now(timezone.utc).date())
        self._lock = Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.db_path,
            timeout=10,
            isolation_level=None,
            check_same_thread=False,
        )
        self._initialize_schema()

    def check_rate_limit(self, client_id: str) -> None:
        """Block clients that exceed the per-minute request limit."""
        now = self.clock()
        window_start = now - 60
        blocked_error: RateLimitExceeded | None = None

        def transaction() -> None:
            """Clean old hits and record the current hit if budget remains."""
            nonlocal blocked_error
            self._connection.execute(
                "DELETE FROM rate_hits WHERE hit_at <= ?",
                (window_start,),
            )
            count = self._connection.execute(
                "SELECT COUNT(*) FROM rate_hits WHERE client_id = ?",
                (client_id,),
            ).fetchone()[0]

            limit = self.settings.rate_limit_per_minute
            if count >= limit:
                oldest = self._connection.execute(
                    "SELECT MIN(hit_at) FROM rate_hits WHERE client_id = ?",
                    (client_id,),
                ).fetchone()[0]
                retry_after = max(round(60 - (now - float(oldest))), 1)
                blocked_error = RateLimitExceeded(
                    code="rate_limited",
                    message="搜索过于频繁，请稍后再试。",
                    retry_after_seconds=retry_after,
                )
                return

            self._connection.execute(
                "INSERT INTO rate_hits (client_id, hit_at) VALUES (?, ?)",
                (client_id, now),
            )

        self._run_transaction(transaction)
        if blocked_error is not None:
            raise blocked_error

    def reserve_external_quota(self, client_id: str) -> None:
        """Reserve quota before paid Tavily or DeepSeek calls are made."""
        current_day = self.today().isoformat()
        blocked_error: QuotaExceeded | None = None

        def transaction() -> None:
            """Increment the IP and global daily quota counters when allowed."""
            nonlocal blocked_error
            self._connection.execute(
                "DELETE FROM daily_quota WHERE quota_day != ?",
                (current_day,),
            )
            ip_count = self._daily_quota_count(current_day, IP_QUOTA_SCOPE, client_id)
            global_count = self._daily_quota_count(
                current_day,
                GLOBAL_QUOTA_SCOPE,
                GLOBAL_QUOTA_CLIENT,
            )

            if ip_count >= self.settings.ip_daily_external_quota:
                blocked_error = QuotaExceeded(
                    code="ip_quota_exceeded",
                    message="今日搜索配额已用完，请明天再试。",
                    retry_after_seconds=self._seconds_until_tomorrow(),
                )
                return

            if global_count >= self.settings.global_daily_external_quota:
                blocked_error = QuotaExceeded(
                    code="global_quota_exceeded",
                    message="平台今日搜索预算已用完，请稍后再试。",
                    retry_after_seconds=self._seconds_until_tomorrow(),
                )
                return

            self._upsert_daily_quota(current_day, IP_QUOTA_SCOPE, client_id, ip_count + 1)
            self._upsert_daily_quota(
                current_day,
                GLOBAL_QUOTA_SCOPE,
                GLOBAL_QUOTA_CLIENT,
                global_count + 1,
            )

        self._run_transaction(transaction)
        if blocked_error is not None:
            raise blocked_error

    def acquire_stream(self, client_id: str) -> None:
        """Reserve one active streaming slot for a client."""
        blocked_error: ConcurrencyLimitExceeded | None = None

        def transaction() -> None:
            """Increment the active stream counter when the client has capacity."""
            nonlocal blocked_error
            active = self._active_stream_count(client_id)
            if active >= self.settings.ip_concurrent_streams:
                blocked_error = ConcurrencyLimitExceeded(
                    code="too_many_concurrent_requests",
                    message="当前搜索请求较多，请稍后再试。",
                    retry_after_seconds=10,
                )
                return

            self._connection.execute(
                """
                INSERT INTO active_streams (client_id, count)
                VALUES (?, ?)
                ON CONFLICT(client_id) DO UPDATE SET count = excluded.count
                """,
                (client_id, active + 1),
            )

        self._run_transaction(transaction)
        if blocked_error is not None:
            raise blocked_error

    def release_stream(self, client_id: str) -> None:
        """Release one active streaming slot for a client."""
        def transaction() -> None:
            """Decrement the active stream counter and remove empty rows."""
            active = self._active_stream_count(client_id)
            if active <= 1:
                self._connection.execute(
                    "DELETE FROM active_streams WHERE client_id = ?",
                    (client_id,),
                )
                return

            self._connection.execute(
                "UPDATE active_streams SET count = ? WHERE client_id = ?",
                (active - 1, client_id),
            )

        self._run_transaction(transaction)

    def reset(self) -> None:
        """Clear all rate, quota, and concurrency counters for tests."""
        def transaction() -> None:
            """Delete all counter rows from the local guard database."""
            self._connection.execute("DELETE FROM rate_hits")
            self._connection.execute("DELETE FROM daily_quota")
            self._connection.execute("DELETE FROM active_streams")

        self._run_transaction(transaction)

    def _initialize_schema(self) -> None:
        """Create cost guard tables and clear stale active streams."""
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS rate_hits (
                    client_id TEXT NOT NULL,
                    hit_at REAL NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_rate_hits_client_time
                ON rate_hits (client_id, hit_at)
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_quota (
                    quota_day TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    count INTEGER NOT NULL,
                    PRIMARY KEY (quota_day, scope, client_id)
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS active_streams (
                    client_id TEXT PRIMARY KEY,
                    count INTEGER NOT NULL
                )
                """
            )
            self._connection.execute("DELETE FROM active_streams")

    def _run_transaction(self, callback: Callable[[], None]) -> None:
        """Run a SQLite transaction under a process-local lock."""
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                callback()
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def _daily_quota_count(self, quota_day: str, scope: str, client_id: str) -> int:
        """Return the stored daily quota count for one quota bucket."""
        row = self._connection.execute(
            """
            SELECT count FROM daily_quota
            WHERE quota_day = ? AND scope = ? AND client_id = ?
            """,
            (quota_day, scope, client_id),
        ).fetchone()
        return int(row[0]) if row else 0

    def _upsert_daily_quota(
        self,
        quota_day: str,
        scope: str,
        client_id: str,
        count: int,
    ) -> None:
        """Write the updated daily quota count for one quota bucket."""
        self._connection.execute(
            """
            INSERT INTO daily_quota (quota_day, scope, client_id, count)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(quota_day, scope, client_id)
            DO UPDATE SET count = excluded.count
            """,
            (quota_day, scope, client_id, count),
        )

    def _active_stream_count(self, client_id: str) -> int:
        """Return the current active stream count for a client."""
        row = self._connection.execute(
            "SELECT count FROM active_streams WHERE client_id = ?",
            (client_id,),
        ).fetchone()
        return int(row[0]) if row else 0

    def _seconds_until_tomorrow(self) -> int:
        """Return an approximate retry delay until the next UTC day."""
        return 24 * 60 * 60


def create_cost_guard(settings: Settings) -> InMemoryCostGuard | SQLiteCostGuard:
    """Create the configured cost guard implementation."""
    backend = settings.cost_guard_backend.lower()
    if backend == "memory":
        return InMemoryCostGuard(settings)
    if backend == "sqlite":
        return SQLiteCostGuard(settings, settings.cost_guard_sqlite_path)

    raise ValueError(f"Unsupported COST_GUARD_BACKEND: {settings.cost_guard_backend}")
