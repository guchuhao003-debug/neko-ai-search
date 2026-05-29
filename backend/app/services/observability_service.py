"""Lightweight observability helpers for search request tracing."""

from __future__ import annotations

import json
import logging
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from typing import Any
from uuid import uuid4


LOGGER_NAME = "neko_ai_search.observability"


def _get_logger() -> logging.Logger:
    """Return a JSON-message logger without duplicate handlers."""
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def _utc_now() -> str:
    """Return an ISO timestamp for structured logs and trace events."""
    return datetime.now(timezone.utc).isoformat()


def _json_log(level: int, payload: dict[str, Any]) -> None:
    """Write one structured JSON log entry."""
    _get_logger().log(level, json.dumps(payload, ensure_ascii=False))


@dataclass
class SearchStep:
    """Track one timed search pipeline step."""

    search_id: str
    name: str
    query: str
    started_at: float = field(default_factory=perf_counter)

    def start_payload(self) -> dict[str, Any]:
        """Return and log the step start payload."""
        payload = self._base_payload("step_start")
        _json_log(logging.INFO, payload)
        return payload

    def done_payload(self, **extra: Any) -> dict[str, Any]:
        """Return and log the successful step completion payload."""
        payload = self._base_payload("step_done")
        payload.update(
            {
                "status": "success",
                "duration_ms": self.duration_ms(),
                **extra,
            }
        )
        _json_log(logging.INFO, payload)
        return payload

    def error_payload(self, exc: Exception) -> dict[str, Any]:
        """Return and log the failed step payload."""
        payload = self._base_payload("step_error")
        payload.update(
            {
                "status": "error",
                "duration_ms": self.duration_ms(),
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "stacktrace": traceback.format_exc(),
            }
        )
        _json_log(logging.ERROR, payload)
        return payload

    def duration_ms(self) -> int:
        """Return elapsed step duration in milliseconds."""
        return round((perf_counter() - self.started_at) * 1000)

    def _base_payload(self, event: str) -> dict[str, Any]:
        """Build shared trace event fields."""
        return {
            "timestamp": _utc_now(),
            "event": event,
            "search_id": self.search_id,
            "step": self.name,
            "query": self.query,
        }


class SearchObserver:
    """Track a complete search request across logs and SSE trace events."""

    def __init__(self, query: str) -> None:
        """Create an observer with a unique search ID."""
        self.search_id = str(uuid4())
        self.query = query
        self.started_at = perf_counter()

    def trace_start_payload(self) -> dict[str, Any]:
        """Return and log the search trace start payload."""
        payload = self._base_payload("trace_start")
        _json_log(logging.INFO, payload)
        return payload

    def trace_done_payload(self, status: str = "success", **extra: Any) -> dict[str, Any]:
        """Return and log the search trace completion payload."""
        payload = self._base_payload("trace_done")
        payload.update(
            {
                "status": status,
                "duration_ms": round((perf_counter() - self.started_at) * 1000),
                **extra,
            }
        )
        _json_log(logging.INFO, payload)
        return payload

    def trace_error_payload(self, exc: Exception) -> dict[str, Any]:
        """Return and log the search trace error payload."""
        payload = self._base_payload("trace_error")
        payload.update(
            {
                "status": "error",
                "duration_ms": round((perf_counter() - self.started_at) * 1000),
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "stacktrace": traceback.format_exc(),
            }
        )
        _json_log(logging.ERROR, payload)
        return payload

    def step(self, name: str) -> SearchStep:
        """Create a timed step for the current search."""
        return SearchStep(search_id=self.search_id, name=name, query=self.query)

    def _base_payload(self, event: str) -> dict[str, Any]:
        """Build shared trace event fields."""
        return {
            "timestamp": _utc_now(),
            "event": event,
            "search_id": self.search_id,
            "query": self.query,
        }
