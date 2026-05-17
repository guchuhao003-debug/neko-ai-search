"""Tests for prompt construction and related-question parsing."""

from app.schemas import SearchResult
from app.services.ai_service import build_answer_prompt, build_source_context


def test_build_source_context_numbers_results() -> None:
    """Source context should preserve source IDs and URLs for citation grounding."""
    results = [
        SearchResult(id=1, title="One", url="https://example.com/1", content="Alpha"),
        SearchResult(id=2, title="Two", url="https://example.com/2", content="Beta"),
    ]

    context = build_source_context(results)

    assert "[1] One" in context
    assert "URL: https://example.com/2" in context
    assert "Content: Beta" in context


def test_build_answer_prompt_requires_markdown_citations() -> None:
    """Answer prompts should explicitly request Markdown and citations."""
    results = [
        SearchResult(id=1, title="Doc", url="https://example.com", content="Evidence"),
    ]

    prompt = build_answer_prompt("What is neko-ai-search?", results)

    assert "User question" in prompt
    assert "Write a helpful Markdown answer" in prompt
    assert "[1] Doc" in prompt
