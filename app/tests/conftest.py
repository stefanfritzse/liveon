"""Shared fixtures and helpers for the test suite."""

import pytest

from app.main import coach_rate_limiter


@pytest.fixture(autouse=True)
def _reset_coach_rate_limiter() -> None:
    """Keep the shared in-process rate limiter from leaking between tests."""

    coach_rate_limiter.reset()
    yield
    coach_rate_limiter.reset()
