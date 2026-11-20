from __future__ import annotations

from pathlib import Path

import pytest

from copilot.ingestion.http import HttpClient, RateLimiter

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def http() -> HttpClient:
    """Client with no rate limiting or backoff delay, so tests run instantly."""
    client = HttpClient(
        "test-agent", rate_limiter=RateLimiter(default_rps=0), backoff_initial=0, backoff_max=0
    )
    yield client
    client.close()
