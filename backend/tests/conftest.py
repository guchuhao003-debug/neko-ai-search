"""Shared pytest configuration for backend tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def anyio_backend() -> str:
    """Run anyio-marked async tests on asyncio only."""
    return "asyncio"
