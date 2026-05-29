"""Lightweight security checks for queries, sources, and model output."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.schemas import SearchResult


FILTERED_TEXT = "[已过滤潜在风险内容]"
FALLBACK_BLOCKED_TERMS = (
    "违规词示例",
    "敏感词示例",
    "forbidden-test-term",
)


@dataclass(frozen=True)
class SecurityBlockedError(Exception):
    """Raised when user input is blocked by security policy."""

    code: str
    message: str
    reason: str

    def __str__(self) -> str:
        """Return the user-facing security message."""
        return self.message


@dataclass(frozen=True)
class SecurityCheckResult:
    """Result returned by query security checks."""

    allowed: bool
    sanitized_text: str
    reason: str | None = None


class SecurityService:
    """Apply MVP prompt-injection and sensitive-term protections."""

    def __init__(self, blocked_terms_path: str | None = None) -> None:
        """Compile reusable security patterns once for efficient matching."""
        self.injection_patterns = [
            re.compile(pattern, re.IGNORECASE)
            for pattern in (
                r"ignore\s+(all\s+)?previous\s+instructions?",
                r"disregard\s+(all\s+)?previous\s+instructions?",
                r"reveal\s+(the\s+)?(system|developer)\s+prompt",
                r"show\s+(me\s+)?(the\s+)?(system|developer)\s+prompt",
                r"system\s+prompt",
                r"developer\s+message",
                r"jailbreak",
                r"prompt\s+injection",
                r"忽略(以上|之前|所有).{0,12}(指令|规则|提示)",
                r"(告诉|给我|展示|查看).{0,12}(系统提示词|系统指令|开发者消息)",
                r"(系统提示词|系统指令|开发者消息)",
                r"泄露.{0,8}(系统提示词|系统指令|开发者消息)",
                r"绕过.{0,8}(安全|限制|规则)",
                r"越狱",
            )
        ]
        blocked_terms = load_blocked_terms(blocked_terms_path)
        self.blocked_term_patterns = [
            re.compile(re.escape(term), re.IGNORECASE)
            for term in blocked_terms
        ]

    def check_query(self, query: str) -> SecurityCheckResult:
        """Validate user query before cache lookup or paid API calls."""
        if self._has_prompt_injection(query):
            raise SecurityBlockedError(
                code="security_prompt_injection",
                message="搜索内容存在提示注入风险，已被安全策略拦截。",
                reason="prompt_injection",
            )

        if self._has_blocked_terms(query):
            raise SecurityBlockedError(
                code="security_blocked_terms",
                message="搜索内容包含敏感或违规词，已被安全策略拦截。",
                reason="blocked_terms",
            )

        return SecurityCheckResult(allowed=True, sanitized_text=query)

    def sanitize_search_results(self, results: list[SearchResult]) -> list[SearchResult]:
        """Sanitize untrusted search result text before prompt construction."""
        return [
            result.model_copy(
                update={
                    "title": self.sanitize_untrusted_text(result.title),
                    "content": self.sanitize_untrusted_text(result.content),
                }
            )
            for result in results
        ]

    def sanitize_untrusted_text(self, text: str) -> str:
        """Remove risky instructions and sensitive terms from untrusted text."""
        sanitized = text
        for pattern in self.injection_patterns:
            sanitized = pattern.sub(FILTERED_TEXT, sanitized)

        for pattern in self.blocked_term_patterns:
            sanitized = pattern.sub(FILTERED_TEXT, sanitized)

        return sanitized

    def sanitize_model_output(self, text: str) -> str:
        """Sanitize model output before returning it to the frontend."""
        return self.sanitize_untrusted_text(text)

    def _has_prompt_injection(self, text: str) -> bool:
        """Return whether text contains prompt-injection indicators."""
        return any(pattern.search(text) for pattern in self.injection_patterns)

    def _has_blocked_terms(self, text: str) -> bool:
        """Return whether text contains blocked sensitive terms."""
        return any(pattern.search(text) for pattern in self.blocked_term_patterns)


def load_blocked_terms(blocked_terms_path: str | None) -> list[str]:
    """Load sensitive terms from a UTF-8 text file."""
    terms = list(FALLBACK_BLOCKED_TERMS)
    if not blocked_terms_path:
        return terms

    path = Path(blocked_terms_path)
    if not path.exists():
        return terms

    loaded_terms = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    return _dedupe_terms([*terms, *loaded_terms])


def _dedupe_terms(terms: list[str]) -> list[str]:
    """Deduplicate terms while preserving file order."""
    seen: set[str] = set()
    deduped: list[str] = []
    for term in terms:
        normalized = term.casefold()
        if normalized in seen:
            continue

        seen.add(normalized)
        deduped.append(term)
    return deduped
