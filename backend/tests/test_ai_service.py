"""Tests for prompt construction and related-question parsing."""

from app.schemas import SearchResult
from app.services.ai_service import (
    build_answer_prompt,
    build_source_context,
    generate_rule_based_related_questions,
)


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
    assert "Use inline source citations" in prompt
    assert "[1] Doc" in prompt


def test_build_source_context_limits_sources_and_content() -> None:
    """Source context should be compact to reduce model latency."""
    results = [
        SearchResult(id=1, title="One", url="https://example.com/1", content="Alpha"),
        SearchResult(id=2, title="Two", url="https://example.com/2", content="Beta"),
    ]

    context = build_source_context(results, max_sources=1, max_content_chars=3)

    assert "[1] One" in context
    assert "[2] Two" not in context
    assert "Content: Alp..." in context


def test_fast_answer_prompt_requests_concise_output() -> None:
    """Fast answer prompts should ask the model to avoid long answers."""
    results = [
        SearchResult(id=1, title="Doc", url="https://example.com", content="Evidence"),
    ]

    prompt = build_answer_prompt("What is neko-ai-search?", results, fast_answer=True)

    assert "Answer concisely" in prompt
    assert "Use inline source citations" in prompt


def test_rule_based_related_questions_match_query_language() -> None:
    """Rule-based related questions should avoid an extra model call."""
    chinese = generate_rule_based_related_questions("DeepSeek V4 有哪些能力？")
    english = generate_rule_based_related_questions("DeepSeek V4 capabilities?")

    assert chinese == [
        "DeepSeek V4 有哪些能力 的最新进展是什么？",
        "DeepSeek V4 有哪些能力 有哪些关键来源值得继续阅读？",
        "DeepSeek V4 有哪些能力 和同类方案相比有什么区别？",
    ]
    assert english == [
        "What are the latest developments in DeepSeek V4 capabilities?",
        "What key sources about DeepSeek V4 capabilities are worth reading next?",
        "How does DeepSeek V4 capabilities compare with similar alternatives?",
    ]
