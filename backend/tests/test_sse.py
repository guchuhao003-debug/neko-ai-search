"""Tests for SSE frame formatting."""

from app.services.sse import format_sse


def test_format_sse_serializes_event_and_json_data() -> None:
    """SSE frames should include event and data lines separated by a blank line."""
    frame = format_sse("token", {"text": "hello"})

    assert frame.startswith("event: token\n")
    assert 'data: {"text": "hello"}' in frame
    assert frame.endswith("\n\n")
