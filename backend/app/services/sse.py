"""Small helpers for writing Server-Sent Event frames."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def format_sse(event: str, data: Mapping[str, Any] | list[Any] | str) -> str:
    """Serialize an SSE event with JSON data."""
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"
