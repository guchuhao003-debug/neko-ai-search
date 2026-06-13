"""SQLite-backed account, session, and private search history service."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, cast

from app.config import Settings
from app.schemas import SearchMode


PASSWORD_ITERATIONS = 210_000
SESSION_TOKEN_BYTES = 32
MAX_HISTORY_ITEMS = 24
MAX_CREDIT_LEDGER_ITEMS = 50
MAX_ADMIN_USER_PAGE_SIZE = 100
REGISTRATION_BONUS_REASON = "registration_bonus"
USER_ROLE = "user"
ADMIN_ROLE = "admin"
ACTIVE_STATUS = "active"
DISABLED_STATUS = "disabled"
VALID_USER_ROLES = {USER_ROLE, ADMIN_ROLE}
VALID_USER_STATUSES = {ACTIVE_STATUS, DISABLED_STATUS}


class AccountError(Exception):
    """Base class for account service errors."""


class DuplicateUserError(AccountError):
    """Raised when an email address has already been registered."""


class InvalidCredentialsError(AccountError):
    """Raised when login credentials are invalid."""


class InsufficientCreditError(AccountError):
    """Raised when a credit debit would make the balance negative."""


@dataclass(frozen=True)
class AccountUser:
    """Authenticated user profile returned to API clients."""

    id: int
    email: str
    display_name: str
    role: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SessionRecord:
    """Session token metadata returned after register or login."""

    token: str
    expires_at: int
    user: AccountUser


@dataclass(frozen=True)
class HistoryRecord:
    """Private search history item owned by one authenticated user."""

    id: int
    query: str
    mode: SearchMode
    created_at: str


@dataclass(frozen=True)
class CreditAccount:
    """Current credit balance for one authenticated user."""

    user_id: int
    balance: int
    updated_at: str


@dataclass(frozen=True)
class CreditLedgerRecord:
    """Immutable credit ledger row owned by one authenticated user."""

    id: int
    user_id: int
    change_amount: int
    balance_after: int
    reason: str
    reference_type: str | None
    reference_id: str | None
    created_at: str


@dataclass(frozen=True)
class AdminStatsSummary:
    """Aggregated platform counters visible only to administrators."""

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


@dataclass(frozen=True)
class AdminRecentUserStat:
    """Compact user row for the administrator statistics page."""

    id: int
    email: str
    display_name: str
    balance: int
    history_count: int
    created_at: str


@dataclass(frozen=True)
class AdminRecentSearchStat:
    """Recent search-history row with owner information for administrators."""

    id: int
    user_email: str
    query: str
    mode: SearchMode
    created_at: str


@dataclass(frozen=True)
class AdminCreditReasonStat:
    """Aggregated credit ledger rows grouped by reason."""

    reason: str
    ledger_count: int
    total_change: int


@dataclass(frozen=True)
class AdminStatsSnapshot:
    """Full administrator statistics snapshot returned by the service."""

    summary: AdminStatsSummary
    recent_users: list[AdminRecentUserStat]
    recent_searches: list[AdminRecentSearchStat]
    credit_reasons: list[AdminCreditReasonStat]


@dataclass(frozen=True)
class AdminManagedUser:
    """User row with account counters for administrator user management."""

    id: int
    email: str
    display_name: str
    role: str
    status: str
    balance: int
    history_count: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AdminUserList:
    """Paginated administrator user-management result."""

    items: list[AdminManagedUser]
    total: int
    limit: int
    offset: int


class AccountService:
    """Persist users, sessions, and user-scoped search history in SQLite."""

    def __init__(
        self,
        db_path: str,
        session_ttl_seconds: int,
        initial_credit_balance: int = 20,
        admin_emails: Iterable[str] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Create the account service and initialize its database schema."""
        self.db_path = db_path
        self.session_ttl_seconds = session_ttl_seconds
        self.initial_credit_balance = max(initial_credit_balance, 0)
        self.initial_admin_emails = {
            normalize_email(email) for email in (admin_emails or []) if email.strip()
        }
        self.clock = clock or time.time
        self._lock = threading.RLock()
        self._initialize_database()

    def register(self, email: str, password: str, display_name: str) -> SessionRecord:
        """Register a new user and return an authenticated session."""
        normalized_email = normalize_email(email)
        clean_name = display_name.strip() or normalized_email.split("@", maxsplit=1)[0]
        salt = secrets.token_hex(16)
        password_hash = hash_password(password, salt)

        with self._lock, self._connect() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO users (
                        email, display_name, password_salt, password_hash, role, status
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_email,
                        clean_name[:80],
                        salt,
                        password_hash,
                        self._role_for_email(normalized_email),
                        ACTIVE_STATUS,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateUserError("该邮箱已注册") from exc

            user = AccountUser(
                id=int(cursor.lastrowid),
                email=normalized_email,
                display_name=clean_name[:80],
                role=self._role_for_email(normalized_email),
                status=ACTIVE_STATUS,
                created_at=self._created_at_for_user(connection, int(cursor.lastrowid)),
                updated_at=self._updated_at_for_user(connection, int(cursor.lastrowid)),
            )
            self._ensure_credit_account(connection, user.id)
            return self._create_session(connection, user)

    def login(self, email: str, password: str) -> SessionRecord:
        """Validate credentials and return a fresh authenticated session."""
        normalized_email = normalize_email(email)

        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, email, display_name, password_salt, password_hash,
                    role, status, created_at, updated_at
                FROM users
                WHERE email = ?
                """,
                (normalized_email,),
            ).fetchone()
            if row is None:
                raise InvalidCredentialsError("邮箱或密码不正确")

            expected_hash = hash_password(password, str(row["password_salt"]))
            if not hmac.compare_digest(expected_hash, str(row["password_hash"])):
                raise InvalidCredentialsError("邮箱或密码不正确")

            user = row_to_user(row)
            if user.status != ACTIVE_STATUS:
                raise InvalidCredentialsError("账号已被禁用")

            return self._create_session(connection, user)

    def get_user_by_session(self, token: str | None) -> AccountUser | None:
        """Return the user attached to a valid session token, if present."""
        if not token:
            return None

        token_hash = hash_session_token(token)
        now = int(self.clock())
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT users.id, users.email, users.display_name, users.role,
                    users.status, users.created_at, users.updated_at
                FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ?
                    AND sessions.expires_at > ?
                    AND users.status = ?
                """,
                (token_hash, now, ACTIVE_STATUS),
            ).fetchone()

            if row is None:
                return None

            return row_to_user(row)

    def delete_session(self, token: str | None) -> None:
        """Delete one session token during logout."""
        if not token:
            return

        token_hash = hash_session_token(token)
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def record_history(self, user_id: int, query: str, mode: SearchMode) -> HistoryRecord:
        """Store a user's search query and remove old duplicate entries."""
        clean_query = query.strip()
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM search_history WHERE user_id = ? AND lower(query) = lower(?)",
                (user_id, clean_query),
            )
            cursor = connection.execute(
                """
                INSERT INTO search_history (user_id, query, mode)
                VALUES (?, ?, ?)
                """,
                (user_id, clean_query, mode),
            )
            history_id = int(cursor.lastrowid)
            self._trim_history(connection, user_id)
            row = connection.execute(
                """
                SELECT id, query, mode, created_at
                FROM search_history
                WHERE id = ? AND user_id = ?
                """,
                (history_id, user_id),
            ).fetchone()

            return row_to_history(row)

    def list_history(self, user_id: int, limit: int = MAX_HISTORY_ITEMS) -> list[HistoryRecord]:
        """Return recent private history for one authenticated user."""
        bounded_limit = min(max(limit, 1), MAX_HISTORY_ITEMS)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, query, mode, created_at
                FROM search_history
                WHERE user_id = ?
                ORDER BY datetime(created_at) DESC, id DESC
                LIMIT ?
                """,
                (user_id, bounded_limit),
            ).fetchall()

        return [row_to_history(row) for row in rows]

    def delete_history(self, user_id: int, history_id: int) -> bool:
        """Delete one private history item and report whether it existed."""
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM search_history WHERE id = ? AND user_id = ?",
                (history_id, user_id),
            )
            return cursor.rowcount > 0

    def clear_history(self, user_id: int) -> None:
        """Delete all private history items for one authenticated user."""
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM search_history WHERE user_id = ?", (user_id,))

    def get_credit_account(self, user_id: int) -> CreditAccount:
        """Return the user's credit account, creating it when missing."""
        with self._lock, self._connect() as connection:
            return self._ensure_credit_account(connection, user_id)

    def list_credit_ledger(
        self,
        user_id: int,
        limit: int = MAX_CREDIT_LEDGER_ITEMS,
    ) -> list[CreditLedgerRecord]:
        """Return recent immutable credit ledger rows for one user."""
        bounded_limit = min(max(limit, 1), MAX_CREDIT_LEDGER_ITEMS)
        with self._lock, self._connect() as connection:
            self._ensure_credit_account(connection, user_id)
            rows = connection.execute(
                """
                SELECT id, user_id, change_amount, balance_after, reason,
                    reference_type, reference_id, created_at
                FROM credit_ledger
                WHERE user_id = ?
                ORDER BY datetime(created_at) DESC, id DESC
                LIMIT ?
                """,
                (user_id, bounded_limit),
            ).fetchall()

        return [row_to_credit_ledger(row) for row in rows]

    def adjust_credits(
        self,
        user_id: int,
        change_amount: int,
        reason: str,
        reference_type: str | None = None,
        reference_id: str | None = None,
    ) -> CreditLedgerRecord:
        """Apply a credit delta and append one ledger row atomically."""
        clean_reason = reason.strip()
        if not clean_reason:
            raise ValueError("credit reason is required")

        with self._lock, self._connect() as connection:
            account = self._ensure_credit_account(connection, user_id)
            next_balance = account.balance + change_amount
            if next_balance < 0:
                raise InsufficientCreditError("积分余额不足")

            connection.execute(
                """
                UPDATE credit_accounts
                SET balance = ?, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
                """,
                (next_balance, user_id),
            )
            cursor = self._insert_credit_ledger(
                connection,
                user_id,
                change_amount,
                next_balance,
                clean_reason,
                reference_type,
                reference_id,
            )
            row = self._select_credit_ledger(connection, int(cursor.lastrowid), user_id)
            return row_to_credit_ledger(row)

    def list_admin_users(
        self,
        query: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> AdminUserList:
        """Return users for administrator management with search and pagination."""
        clean_query = query.strip().lower()
        bounded_limit = min(max(limit, 1), MAX_ADMIN_USER_PAGE_SIZE)
        safe_offset = max(offset, 0)
        where_clause = ""
        params: list[object] = []

        if clean_query:
            where_clause = """
                WHERE lower(users.email) LIKE ?
                    OR lower(users.display_name) LIKE ?
            """
            needle = f"%{clean_query}%"
            params.extend([needle, needle])

        with self._lock, self._connect() as connection:
            total_row = connection.execute(
                f"SELECT COUNT(*) AS total FROM users {where_clause}",
                params,
            ).fetchone()
            rows = connection.execute(
                f"""
                SELECT
                    users.id,
                    users.email,
                    users.display_name,
                    users.role,
                    users.status,
                    users.created_at,
                    users.updated_at,
                    COALESCE(credit_accounts.balance, 0) AS balance,
                    COUNT(search_history.id) AS history_count
                FROM users
                LEFT JOIN credit_accounts ON credit_accounts.user_id = users.id
                LEFT JOIN search_history ON search_history.user_id = users.id
                {where_clause}
                GROUP BY users.id
                ORDER BY datetime(users.created_at) DESC, users.id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, bounded_limit, safe_offset),
            ).fetchall()

        return AdminUserList(
            items=[row_to_admin_managed_user(row) for row in rows],
            total=int(total_row["total"]),
            limit=bounded_limit,
            offset=safe_offset,
        )

    def get_admin_user(self, user_id: int) -> AdminManagedUser | None:
        """Return one user-management row for administrators."""
        with self._lock, self._connect() as connection:
            row = self._select_admin_user(connection, user_id)

        return row_to_admin_managed_user(row) if row else None

    def create_user_as_admin(
        self,
        email: str,
        password: str,
        display_name: str,
        role: str = USER_ROLE,
        status: str = ACTIVE_STATUS,
    ) -> AdminManagedUser:
        """Create one managed user account without starting a browser session."""
        normalized_email = normalize_email(email)
        normalized_role = normalize_user_role(role)
        normalized_status = normalize_user_status(status)
        clean_name = normalize_display_name(display_name, normalized_email)
        salt = secrets.token_hex(16)
        password_hash = hash_password(password, salt)

        with self._lock, self._connect() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO users (
                        email, display_name, password_salt, password_hash, role, status
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_email,
                        clean_name,
                        salt,
                        password_hash,
                        normalized_role,
                        normalized_status,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateUserError("璇ラ偖绠卞凡娉ㄥ唽") from exc

            user_id = int(cursor.lastrowid)
            self._ensure_credit_account(connection, user_id)
            row = self._select_admin_user(connection, user_id)
            return row_to_admin_managed_user(row)

    def update_user_as_admin(
        self,
        user_id: int,
        display_name: str | None = None,
        role: str | None = None,
        status: str | None = None,
    ) -> AdminManagedUser | None:
        """Update profile, role, or status for one managed user."""
        assignments: list[str] = []
        params: list[object] = []
        normalized_status = normalize_user_status(status) if status is not None else None

        if display_name is not None:
            clean_name = display_name.strip()
            if not clean_name:
                raise ValueError("display_name is required")
            assignments.append("display_name = ?")
            params.append(clean_name[:80])
        if role is not None:
            assignments.append("role = ?")
            params.append(normalize_user_role(role))
        if normalized_status is not None:
            assignments.append("status = ?")
            params.append(normalized_status)

        with self._lock, self._connect() as connection:
            if assignments:
                cursor = connection.execute(
                    f"""
                    UPDATE users
                    SET {", ".join(assignments)}, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (*params, user_id),
                )
                if cursor.rowcount == 0:
                    return None

                # Disabled accounts must lose any existing browser sessions immediately.
                if normalized_status == DISABLED_STATUS:
                    connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

            row = self._select_admin_user(connection, user_id)

        return row_to_admin_managed_user(row) if row else None

    def delete_user_as_admin(self, user_id: int) -> bool:
        """Delete one managed user and cascade private account data."""
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM users WHERE id = ?", (user_id,))
            return cursor.rowcount > 0

    def get_admin_stats(self) -> AdminStatsSnapshot:
        """Return platform-wide statistics for administrator dashboards."""
        now = int(self.clock())
        with self._lock, self._connect() as connection:
            summary_row = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM users) AS total_users,
                    (SELECT COUNT(*) FROM sessions WHERE expires_at > ?) AS active_sessions,
                    (SELECT COUNT(*) FROM search_history) AS total_history_items,
                    COALESCE((SELECT SUM(balance) FROM credit_accounts), 0)
                        AS total_credit_balance,
                    COALESCE((
                        SELECT SUM(change_amount)
                        FROM credit_ledger
                        WHERE change_amount > 0
                    ), 0) AS total_credits_granted,
                    COALESCE((
                        SELECT ABS(SUM(change_amount))
                        FROM credit_ledger
                        WHERE change_amount < 0
                    ), 0) AS total_credits_spent,
                    (
                        SELECT COUNT(*)
                        FROM credit_ledger
                        WHERE reason = 'search_usage'
                    ) AS total_search_debits,
                    (
                        SELECT COUNT(*)
                        FROM search_history
                        WHERE mode = 'fast'
                    ) AS fast_history_items,
                    (
                        SELECT COUNT(*)
                        FROM search_history
                        WHERE mode = 'deep'
                    ) AS deep_history_items,
                    (
                        SELECT COUNT(*)
                        FROM users
                        WHERE date(created_at) = date('now')
                    ) AS registered_today,
                    (
                        SELECT COUNT(*)
                        FROM credit_ledger
                        WHERE reason = 'search_usage'
                            AND date(created_at) = date('now')
                    ) AS searches_today,
                    COALESCE((
                        SELECT ABS(SUM(change_amount))
                        FROM credit_ledger
                        WHERE change_amount < 0
                            AND date(created_at) = date('now')
                    ), 0) AS credits_spent_today
                """,
                (now,),
            ).fetchone()
            user_rows = connection.execute(
                """
                SELECT
                    users.id,
                    users.email,
                    users.display_name,
                    users.created_at,
                    COALESCE(credit_accounts.balance, 0) AS balance,
                    COUNT(search_history.id) AS history_count
                FROM users
                LEFT JOIN credit_accounts ON credit_accounts.user_id = users.id
                LEFT JOIN search_history ON search_history.user_id = users.id
                GROUP BY users.id
                ORDER BY datetime(users.created_at) DESC, users.id DESC
                LIMIT 6
                """
            ).fetchall()
            search_rows = connection.execute(
                """
                SELECT
                    search_history.id,
                    users.email AS user_email,
                    search_history.query,
                    search_history.mode,
                    search_history.created_at
                FROM search_history
                JOIN users ON users.id = search_history.user_id
                ORDER BY datetime(search_history.created_at) DESC, search_history.id DESC
                LIMIT 8
                """
            ).fetchall()
            reason_rows = connection.execute(
                """
                SELECT
                    reason,
                    COUNT(*) AS ledger_count,
                    COALESCE(SUM(change_amount), 0) AS total_change
                FROM credit_ledger
                GROUP BY reason
                ORDER BY ledger_count DESC, reason ASC
                LIMIT 8
                """
            ).fetchall()

        return AdminStatsSnapshot(
            summary=row_to_admin_stats_summary(summary_row),
            recent_users=[row_to_admin_recent_user(row) for row in user_rows],
            recent_searches=[row_to_admin_recent_search(row) for row in search_rows],
            credit_reasons=[row_to_admin_credit_reason(row) for row in reason_rows],
        )

    def reset(self) -> None:
        """Clear account data for tests while keeping the schema available."""
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM credit_ledger")
            connection.execute("DELETE FROM credit_accounts")
            connection.execute("DELETE FROM search_history")
            connection.execute("DELETE FROM sessions")
            connection.execute("DELETE FROM users")

    def _initialize_database(self) -> None:
        """Create all account tables and indexes if they do not exist."""
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    password_salt TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    expires_at INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS search_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    query TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'fast',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS credit_accounts (
                    user_id INTEGER PRIMARY KEY,
                    balance INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS credit_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    change_amount INTEGER NOT NULL,
                    balance_after INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    reference_type TEXT,
                    reference_id TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_token_hash
                    ON sessions (token_hash);
                CREATE INDEX IF NOT EXISTS idx_history_user_created
                    ON search_history (user_id, created_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_credit_ledger_user_created
                    ON credit_ledger (user_id, created_at DESC, id DESC);
                """
            )
            self._migrate_user_columns(connection)
            self._promote_configured_admins(connection)

    def _connect(self) -> sqlite3.Connection:
        """Open one SQLite connection with row dictionaries and foreign keys enabled."""
        connection = sqlite3.connect(self.db_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _create_session(
        self,
        connection: sqlite3.Connection,
        user: AccountUser,
    ) -> SessionRecord:
        """Persist and return a new random session token."""
        token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
        token_hash = hash_session_token(token)
        expires_at = int(self.clock()) + self.session_ttl_seconds
        connection.execute(
            """
            INSERT INTO sessions (user_id, token_hash, expires_at)
            VALUES (?, ?, ?)
            """,
            (user.id, token_hash, expires_at),
        )
        return SessionRecord(token=token, expires_at=expires_at, user=user)

    def _ensure_credit_account(
        self,
        connection: sqlite3.Connection,
        user_id: int,
    ) -> CreditAccount:
        """Create and return a user's credit account if it is missing."""
        row = connection.execute(
            """
            SELECT user_id, balance, updated_at
            FROM credit_accounts
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
        if row is not None:
            return row_to_credit_account(row)

        initial_balance = self.initial_credit_balance
        connection.execute(
            """
            INSERT INTO credit_accounts (user_id, balance)
            VALUES (?, ?)
            """,
            (user_id, initial_balance),
        )
        self._insert_credit_ledger(
            connection,
            user_id,
            initial_balance,
            initial_balance,
            REGISTRATION_BONUS_REASON,
            "account",
            str(user_id),
        )
        row = connection.execute(
            """
            SELECT user_id, balance, updated_at
            FROM credit_accounts
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
        return row_to_credit_account(row)

    def _insert_credit_ledger(
        self,
        connection: sqlite3.Connection,
        user_id: int,
        change_amount: int,
        balance_after: int,
        reason: str,
        reference_type: str | None,
        reference_id: str | None,
    ) -> sqlite3.Cursor:
        """Insert an immutable credit ledger row."""
        return connection.execute(
            """
            INSERT INTO credit_ledger (
                user_id, change_amount, balance_after, reason, reference_type, reference_id
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, change_amount, balance_after, reason, reference_type, reference_id),
        )

    def _select_credit_ledger(
        self,
        connection: sqlite3.Connection,
        ledger_id: int,
        user_id: int,
    ) -> sqlite3.Row:
        """Read one credit ledger row scoped to a single user."""
        return connection.execute(
            """
            SELECT id, user_id, change_amount, balance_after, reason,
                reference_type, reference_id, created_at
            FROM credit_ledger
            WHERE id = ? AND user_id = ?
            """,
            (ledger_id, user_id),
        ).fetchone()

    def _select_admin_user(
        self,
        connection: sqlite3.Connection,
        user_id: int,
    ) -> sqlite3.Row | None:
        """Read one user with management counters for administrator APIs."""
        return connection.execute(
            """
            SELECT
                users.id,
                users.email,
                users.display_name,
                users.role,
                users.status,
                users.created_at,
                users.updated_at,
                COALESCE(credit_accounts.balance, 0) AS balance,
                COUNT(search_history.id) AS history_count
            FROM users
            LEFT JOIN credit_accounts ON credit_accounts.user_id = users.id
            LEFT JOIN search_history ON search_history.user_id = users.id
            WHERE users.id = ?
            GROUP BY users.id
            """,
            (user_id,),
        ).fetchone()

    def _created_at_for_user(self, connection: sqlite3.Connection, user_id: int) -> str:
        """Read the timestamp SQLite assigned to a new user row."""
        row = connection.execute(
            "SELECT created_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return str(row["created_at"])

    def _updated_at_for_user(self, connection: sqlite3.Connection, user_id: int) -> str:
        """Read the latest user update timestamp assigned by SQLite."""
        row = connection.execute(
            "SELECT updated_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return str(row["updated_at"])

    def _migrate_user_columns(self, connection: sqlite3.Connection) -> None:
        """Add role and status columns to older local SQLite databases."""
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(users)").fetchall()
        }
        if "role" not in columns:
            connection.execute(
                f"ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT '{USER_ROLE}'"
            )
        if "status" not in columns:
            connection.execute(
                f"ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT '{ACTIVE_STATUS}'"
            )
        if "updated_at" not in columns:
            connection.execute("ALTER TABLE users ADD COLUMN updated_at TEXT")

        connection.execute(
            """
            UPDATE users
            SET role = ?
            WHERE role IS NULL OR trim(role) = ''
            """,
            (USER_ROLE,),
        )
        connection.execute(
            """
            UPDATE users
            SET status = ?
            WHERE status IS NULL OR trim(status) = ''
            """,
            (ACTIVE_STATUS,),
        )
        connection.execute(
            """
            UPDATE users
            SET updated_at = created_at
            WHERE updated_at IS NULL OR trim(updated_at) = ''
            """
        )

    def _promote_configured_admins(self, connection: sqlite3.Connection) -> None:
        """Keep ADMIN_EMAILS compatible by marking matching users as admins."""
        if not self.initial_admin_emails:
            return

        placeholders = ",".join("?" for _ in self.initial_admin_emails)
        connection.execute(
            f"""
            UPDATE users
            SET role = ?, updated_at = CURRENT_TIMESTAMP
            WHERE lower(email) IN ({placeholders})
            """,
            (ADMIN_ROLE, *sorted(self.initial_admin_emails)),
        )

    def _role_for_email(self, email: str) -> str:
        """Return the initial database role for a normalized email address."""
        return ADMIN_ROLE if normalize_email(email) in self.initial_admin_emails else USER_ROLE

    def _trim_history(self, connection: sqlite3.Connection, user_id: int) -> None:
        """Keep only the most recent private history rows for one user."""
        rows: Iterable[sqlite3.Row] = connection.execute(
            """
            SELECT id
            FROM search_history
            WHERE user_id = ?
            ORDER BY datetime(created_at) DESC, id DESC
            LIMIT -1 OFFSET ?
            """,
            (user_id, MAX_HISTORY_ITEMS),
        ).fetchall()
        old_ids = [int(row["id"]) for row in rows]
        if not old_ids:
            return

        placeholders = ",".join("?" for _ in old_ids)
        connection.execute(
            f"DELETE FROM search_history WHERE user_id = ? AND id IN ({placeholders})",
            (user_id, *old_ids),
        )


def create_account_service(settings: Settings) -> AccountService:
    """Create the configured account service for the FastAPI app."""
    return AccountService(
        settings.account_db_path,
        settings.session_ttl_seconds,
        initial_credit_balance=settings.credit_initial_balance,
        admin_emails=settings.admin_emails,
    )


def normalize_email(email: str) -> str:
    """Normalize email addresses before uniqueness checks and login."""
    return email.strip().lower()


def normalize_display_name(display_name: str, email: str) -> str:
    """Normalize an operator-supplied display name with an email fallback."""
    clean_name = display_name.strip() or normalize_email(email).split("@", maxsplit=1)[0]
    return clean_name[:80]


def normalize_user_role(role: str) -> str:
    """Validate and normalize a stored user role."""
    normalized_role = role.strip().lower()
    if normalized_role not in VALID_USER_ROLES:
        raise ValueError("invalid user role")
    return normalized_role


def normalize_user_status(status: str) -> str:
    """Validate and normalize a stored user status."""
    normalized_status = status.strip().lower()
    if normalized_status not in VALID_USER_STATUSES:
        raise ValueError("invalid user status")
    return normalized_status


def hash_password(password: str, salt: str) -> str:
    """Hash one password with PBKDF2-HMAC-SHA256."""
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PASSWORD_ITERATIONS,
    )
    return digest.hex()


def hash_session_token(token: str) -> str:
    """Hash session tokens before storing them in SQLite."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def row_to_user(row: sqlite3.Row) -> AccountUser:
    """Convert a SQLite user row into an API-safe profile object."""
    return AccountUser(
        id=int(row["id"]),
        email=str(row["email"]),
        display_name=str(row["display_name"]),
        role=str(row["role"]),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def row_to_history(row: sqlite3.Row) -> HistoryRecord:
    """Convert a SQLite history row into an API-safe history object."""
    return HistoryRecord(
        id=int(row["id"]),
        query=str(row["query"]),
        mode=cast(SearchMode, str(row["mode"])),
        created_at=str(row["created_at"]),
    )


def row_to_credit_account(row: sqlite3.Row) -> CreditAccount:
    """Convert a SQLite credit account row into a domain object."""
    return CreditAccount(
        user_id=int(row["user_id"]),
        balance=int(row["balance"]),
        updated_at=str(row["updated_at"]),
    )


def row_to_credit_ledger(row: sqlite3.Row) -> CreditLedgerRecord:
    """Convert a SQLite credit ledger row into a domain object."""
    reference_type = row["reference_type"]
    reference_id = row["reference_id"]
    return CreditLedgerRecord(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        change_amount=int(row["change_amount"]),
        balance_after=int(row["balance_after"]),
        reason=str(row["reason"]),
        reference_type=str(reference_type) if reference_type is not None else None,
        reference_id=str(reference_id) if reference_id is not None else None,
        created_at=str(row["created_at"]),
    )


def row_to_admin_stats_summary(row: sqlite3.Row) -> AdminStatsSummary:
    """Convert a SQLite aggregate row into an administrator summary."""
    return AdminStatsSummary(
        total_users=int(row["total_users"]),
        active_sessions=int(row["active_sessions"]),
        total_history_items=int(row["total_history_items"]),
        total_credit_balance=int(row["total_credit_balance"]),
        total_credits_granted=int(row["total_credits_granted"]),
        total_credits_spent=int(row["total_credits_spent"]),
        total_search_debits=int(row["total_search_debits"]),
        fast_history_items=int(row["fast_history_items"]),
        deep_history_items=int(row["deep_history_items"]),
        registered_today=int(row["registered_today"]),
        searches_today=int(row["searches_today"]),
        credits_spent_today=int(row["credits_spent_today"]),
    )


def row_to_admin_recent_user(row: sqlite3.Row) -> AdminRecentUserStat:
    """Convert a SQLite user aggregate row into an administrator user item."""
    return AdminRecentUserStat(
        id=int(row["id"]),
        email=str(row["email"]),
        display_name=str(row["display_name"]),
        balance=int(row["balance"]),
        history_count=int(row["history_count"]),
        created_at=str(row["created_at"]),
    )


def row_to_admin_recent_search(row: sqlite3.Row) -> AdminRecentSearchStat:
    """Convert a SQLite recent search row into an administrator search item."""
    return AdminRecentSearchStat(
        id=int(row["id"]),
        user_email=str(row["user_email"]),
        query=str(row["query"]),
        mode=cast(SearchMode, str(row["mode"])),
        created_at=str(row["created_at"]),
    )


def row_to_admin_credit_reason(row: sqlite3.Row) -> AdminCreditReasonStat:
    """Convert a SQLite grouped ledger row into an administrator reason item."""
    return AdminCreditReasonStat(
        reason=str(row["reason"]),
        ledger_count=int(row["ledger_count"]),
        total_change=int(row["total_change"]),
    )


def row_to_admin_managed_user(row: sqlite3.Row) -> AdminManagedUser:
    """Convert a SQLite user-management row into an administrator item."""
    return AdminManagedUser(
        id=int(row["id"]),
        email=str(row["email"]),
        display_name=str(row["display_name"]),
        role=str(row["role"]),
        status=str(row["status"]),
        balance=int(row["balance"]),
        history_count=int(row["history_count"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )
