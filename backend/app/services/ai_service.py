"""Prompt construction and DeepSeek streaming generation."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from app.config import Settings
from app.schemas import SearchResult


ANSWER_SYSTEM_PROMPT = """
You are neko-ai-search, an accurate AI search assistant.
Use only the supplied web sources as evidence. Write in the user's language.
Every factual claim that depends on a source must include an inline citation like [1].
If sources conflict, explain the conflict and cite both sides.
Do not invent source IDs. End with a concise synthesis.
""".strip()


RELATED_SYSTEM_PROMPT = """
Generate 3 to 5 concise related follow-up questions in the same language as the user.
Return only a JSON array of strings. Do not wrap the JSON in Markdown.
""".strip()


def build_source_context(results: list[SearchResult]) -> str:
    """Build numbered source context for the model prompt."""
    blocks: list[str] = []
    for result in results:
        published = f"\nPublished: {result.published_date}" if result.published_date else ""
        blocks.append(
            (
                f"[{result.id}] {result.title}\n"
                f"URL: {result.url}{published}\n"
                f"Content: {result.content}"
            )
        )
    return "\n\n".join(blocks)


def build_answer_prompt(query: str, results: list[SearchResult]) -> str:
    """Create the user prompt that injects search results as grounded context."""
    return (
        f"User question:\n{query}\n\n"
        f"Web sources:\n{build_source_context(results)}\n\n"
        "Write a helpful Markdown answer with inline source citations."
    )


def _chunk_text(chunk: object) -> str:
    """Extract text from LangChain streaming chunks across provider variants."""
    text = getattr(chunk, "text", None)
    if callable(text):
        return str(text())

    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        return "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))

    return ""


class DeepSeekService:
    """Generate answers and related questions with DeepSeek's compatible API."""

    def __init__(self, settings: Settings) -> None:
        """Store model settings and defer LangChain imports until needed."""
        self.settings = settings

    def _create_model(self, *, streaming: bool):
        """Create a LangChain ChatOpenAI client configured for DeepSeek."""
        from langchain_openai import ChatOpenAI

        extra_body = {"thinking": {"type": "enabled"}} if self.settings.deepseek_thinking else None
        return ChatOpenAI(
            api_key=self.settings.deepseek_api_key,
            base_url=self.settings.deepseek_base_url,
            model=self.settings.deepseek_model,
            temperature=self.settings.deepseek_temperature,
            streaming=streaming,
            reasoning_effort=self.settings.deepseek_reasoning_effort,
            extra_body=extra_body,
        )

    async def stream_answer(
        self,
        query: str,
        results: list[SearchResult],
    ) -> AsyncIterator[str]:
        """Stream answer tokens from DeepSeek or a deterministic mock."""
        if self.settings.use_mock_ai or not self.settings.deepseek_api_key:
            async for token in self._mock_stream_answer(results):
                yield token
            return

        model = self._create_model(streaming=True)
        messages = [
            ("system", ANSWER_SYSTEM_PROMPT),
            ("human", build_answer_prompt(query, results)),
        ]
        async for chunk in model.astream(messages):
            text = _chunk_text(chunk)
            if text:
                yield text

    async def generate_related_questions(self, query: str, answer: str) -> list[str]:
        """Generate related questions after the final answer is available."""
        if self.settings.use_mock_ai or not self.settings.deepseek_api_key:
            return [
                f"{query} 的最新进展是什么？",
                f"{query} 有哪些关键来源值得继续阅读？",
                f"{query} 和相近方案相比有什么差异？",
            ]

        model = self._create_model(streaming=False)
        response = await model.ainvoke(
            [
                ("system", RELATED_SYSTEM_PROMPT),
                ("human", f"Question: {query}\n\nAnswer:\n{answer}"),
            ]
        )
        return _parse_related_questions(_chunk_text(response))

    async def _mock_stream_answer(self, results: list[SearchResult]) -> AsyncIterator[str]:
        """Stream a short local answer for UI and integration testing."""
        text = (
            "这是一个本地演示回答。系统会先读取 Tavily 搜索结果，再把网页摘要作为"
            f"上下文交给 DeepSeek V4 生成带引用的答案 [1]。DeepSeek API 兼容 "
            f"OpenAI Chat Completions，并支持流式输出 [2]。完整搜索结果仍会在"
            f"答案下方展示，方便继续核查未被直接引用的来源 [3]。"
        )
        for word in text:
            await asyncio.sleep(0.01)
            yield word


def _parse_related_questions(raw_text: str) -> list[str]:
    """Parse a model JSON array into a bounded list of related questions."""
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        parsed = [line.strip("- ").strip() for line in raw_text.splitlines()]

    if not isinstance(parsed, list):
        return []

    questions = [str(item).strip() for item in parsed if str(item).strip()]
    return questions[:5]
