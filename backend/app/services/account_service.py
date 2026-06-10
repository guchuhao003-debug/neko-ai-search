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


class AccountError(Exception):
    """Base class for account service errors."""


class DuplicateUserError(AccountError):
    """Raised when an email address has already been registered."""


class InvalidCredentialsError(AccountError):
    """Raised when login credentials are invalid."""


@dataclass(frozen=True)
class AccountUser:
    """Authenticated user profile returned to API clients."""

    id: int
    email: str
    display_name: str
    created_at: str


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


class AccountService:
    """Persist users, sessions, and user-scoped search history in SQLite."""

    def __init__(
        self,
        db_path: str,
        session_ttl_seconds: int,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Create the account service and initialize its database schema."""
        self.db_path = db_path
        self.session_ttl_seconds = session_ttl_seconds
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
                    INSERT INTO users (email, display_name, password_salt, password_hash)
                    VALUES (?, ?, ?, ?)
                    """,
                    (normalized_email, clean_name[:80], salt, password_hash),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateUserError("该邮箱已注册") from exc

            user = AccountUser(
                id=int(cursor.lastrowid),
                email=normalized_email,
                display_name=clean_name[:80],
                created_at=self._created_at_for_user(connection, int(cursor.lastrowid)),
            )
            return self._create_session(connection, user)

    def login(self, email: str, password: str) -> SessionRecord:
        """Validate credentials and return a fresh authenticated session."""
        normalized_email = normalize_email(email)

        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, email, display_name, password_salt, password_hash, created_at
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
                SELECT users.id, users.email, users.display_name, users.created_at
                FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ? AND sessions.expires_at > ?
                """,
                (token_hash, now),
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

    def reset(self) -> None:
        """Clear account data for tests while keeping the schema available."""
        with self._lock, self._connect() as connection:
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
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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

                CREATE INDEX IF NOT EXISTS idx_sessions_token_hash
                    ON sessions (token_hash);
                CREATE INDEX IF NOT EXISTS idx_history_user_created
                    ON search_history (user_id, created_at DESC, id DESC);
                """
            )

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

    def _created_at_for_user(self, connection: sqlite3.Connection, user_id: int) -> str:
        """Read the timestamp SQLite assigned to a new user row."""
        row = connection.execute(
            "SELECT created_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return str(row["created_at"])

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
    return AccountService(settings.account_db_path, settings.session_ttl_seconds)


def normalize_email(email: str) -> str:
    """Normalize email addresses before uniqueness checks and login."""
    return email.strip().lower()


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
        created_at=str(row["created_at"]),
    )


def row_to_history(row: sqlite3.Row) -> HistoryRecord:
    """Convert a SQLite history row into an API-safe history object."""
    return HistoryRecord(
        id=int(row["id"]),
        query=str(row["query"]),
        mode=cast(SearchMode, str(row["mode"])),
        created_at=str(row["created_at"]),
    )
