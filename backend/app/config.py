"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


# Local project configuration should win over stale machine-level variables.
load_dotenv(override=True)


@dataclass(frozen=True)
class Settings:
    """Runtime settings used by search and model services."""

    app_name: str
    app_env: str
    frontend_origins: list[str]
    tavily_api_key: str | None
    tavily_max_results: int
    deepseek_api_key: str | None
    deepseek_base_url: str
    deepseek_model: str
    deepseek_temperature: float
    deepseek_reasoning_effort: str | None
    deepseek_thinking: bool
    use_mock_ai: bool
    ai_max_context_sources: int
    ai_max_source_content_chars: int
    ai_fast_answer: bool
    ai_generate_related_with_ai: bool
    rate_limit_per_minute: int
    ip_daily_external_quota: int
    global_daily_external_quota: int
    ip_concurrent_streams: int
    security_blocked_terms_path: str
    search_cache_ttl_seconds: int


def _split_csv(value: str) -> list[str]:
    """Split comma-separated environment values into trimmed strings."""
    return [item.strip() for item in value.split(",") if item.strip()]


def _default_security_terms_path() -> str:
    """Return the bundled sensitive-term file path."""
    return str(Path(__file__).resolve().parent / "security" / "blocked_terms.txt")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings for the running FastAPI application."""
    return Settings(
        app_name=os.getenv("APP_NAME", "neko-ai-search"),
        app_env=os.getenv("APP_ENV", "development"),
        frontend_origins=_split_csv(
            os.getenv(
                "FRONTEND_ORIGINS",
                "http://localhost:5173,http://127.0.0.1:5173",
            )
        ),
        tavily_api_key=os.getenv("TAVILY_API_KEY") or None,
        tavily_max_results=int(os.getenv("TAVILY_MAX_RESULTS", "8")),
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY") or None,
        deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro"),
        deepseek_temperature=float(os.getenv("DEEPSEEK_TEMPERATURE", "0.2")),
        deepseek_reasoning_effort=os.getenv("DEEPSEEK_REASONING_EFFORT") or None,
        deepseek_thinking=os.getenv("DEEPSEEK_THINKING", "true").lower() == "true",
        use_mock_ai=os.getenv("USE_MOCK_AI", "false").lower() == "true",
        ai_max_context_sources=int(os.getenv("AI_MAX_CONTEXT_SOURCES", "6")),
        ai_max_source_content_chars=int(os.getenv("AI_MAX_SOURCE_CONTENT_CHARS", "900")),
        ai_fast_answer=os.getenv("AI_FAST_ANSWER", "true").lower() == "true",
        ai_generate_related_with_ai=(
            os.getenv("AI_GENERATE_RELATED_WITH_AI", "false").lower() == "true"
        ),
        rate_limit_per_minute=int(os.getenv("RATE_LIMIT_PER_MINUTE", "10")),
        ip_daily_external_quota=int(os.getenv("IP_DAILY_EXTERNAL_QUOTA", "50")),
        global_daily_external_quota=int(
            os.getenv("GLOBAL_DAILY_EXTERNAL_QUOTA", "1000")
        ),
        ip_concurrent_streams=int(os.getenv("IP_CONCURRENT_STREAMS", "2")),
        security_blocked_terms_path=os.getenv(
            "SECURITY_BLOCKED_TERMS_PATH",
            _default_security_terms_path(),
        ),
        search_cache_ttl_seconds=int(os.getenv("SEARCH_CACHE_TTL_SECONDS", "1800")),
    )
