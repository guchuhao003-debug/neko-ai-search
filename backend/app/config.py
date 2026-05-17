"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

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


def _split_csv(value: str) -> list[str]:
    """Split comma-separated environment values into trimmed strings."""
    return [item.strip() for item in value.split(",") if item.strip()]


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
    )
