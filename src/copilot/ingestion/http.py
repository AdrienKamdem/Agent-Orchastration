"""One shared HTTP client for every connector.

Everything that makes us a well-behaved client lives here, so connectors never handle
retries or rate limits themselves:

* **Timeouts** on every request (a hung socket must not stall the whole run).
* **Per-host rate limiting** with a minimum interval between requests (SEC: max 10 req/s).
* **Retries with exponential backoff and jitter** on transient failures (network errors,
  429, 5xx), honouring ``Retry-After`` when the server sends it.
* **Conditional GET**: pass the ``ETag`` / ``Last-Modified`` from the previous run, and
  the server answers ``304 Not Modified`` with no body if nothing changed.
* **User-Agent** per request (the SEC requires one with contact details).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx
import structlog
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

log = structlog.get_logger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class RetryableHTTPError(Exception):
    def __init__(self, response: httpx.Response):
        self.response = response
        self.retry_after = _parse_retry_after(response.headers.get("Retry-After"))
        super().__init__(f"HTTP {response.status_code} for {response.request.url}")


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form: rare in practice, fall back to backoff


class RateLimiter:
    """Enforces a minimum interval between requests to the same host."""

    def __init__(
        self,
        per_host_rps: dict[str, float] | None = None,
        default_rps: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._rps = per_host_rps or {}
        self._default = default_rps
        self._last: dict[str, float] = {}
        self._clock = clock
        self._sleep = sleep

    def wait(self, host: str) -> None:
        rps = self._rps.get(host, self._default)
        if rps <= 0:
            return
        interval = 1.0 / rps
        last = self._last.get(host)
        now = self._clock()
        if last is not None and (delay := last + interval - now) > 0:
            self._sleep(delay)
            now = self._clock()
        self._last[host] = now


class HttpClient:
    def __init__(
        self,
        user_agent: str,
        *,
        timeout: float = 30.0,
        rate_limiter: RateLimiter | None = None,
        max_attempts: int = 5,
        backoff_initial: float = 1.0,
        backoff_max: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )
        self._limiter = rate_limiter or RateLimiter()
        self._max_attempts = max_attempts
        self._backoff = wait_exponential_jitter(
            multiplier=backoff_initial, jitter=backoff_initial, max=backoff_max
        )
        self._backoff_max = backoff_max

    def _wait(self, state: RetryCallState) -> float:
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, RetryableHTTPError) and exc.retry_after is not None:
            return min(exc.retry_after, self._backoff_max)
        return float(self._backoff(state))

    def get(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        user_agent: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """GET with rate limiting + retries. Returns 2xx or 304; raises otherwise."""
        h = dict(headers or {})
        if etag:
            h["If-None-Match"] = etag
        if last_modified:
            h["If-Modified-Since"] = last_modified
        if user_agent:
            h["User-Agent"] = user_agent
        host = urlsplit(url).netloc

        def _once() -> httpx.Response:
            self._limiter.wait(host)
            resp = self._client.get(url, headers=h)
            if resp.status_code in RETRYABLE_STATUS:
                log.warning("http.retryable", url=url, status=resp.status_code)
                raise RetryableHTTPError(resp)
            if resp.status_code != 304:
                resp.raise_for_status()
            return resp

        retrying = Retrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=self._wait,
            retry=retry_if_exception_type((RetryableHTTPError, httpx.TransportError)),
            reraise=True,
        )
        return retrying(_once)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
