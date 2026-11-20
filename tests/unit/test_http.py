import httpx
import pytest
import respx

from copilot.ingestion.http import HttpClient, RateLimiter


@respx.mock
def test_retries_on_429_then_succeeds(http: HttpClient):
    route = respx.get("https://x.test/a").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(200)]
    )
    assert http.get("https://x.test/a").status_code == 200
    assert route.call_count == 2


@respx.mock
def test_gives_up_after_max_attempts():
    client = HttpClient(
        "t",
        rate_limiter=RateLimiter(default_rps=0),
        max_attempts=3,
        backoff_initial=0,
        backoff_max=0,
    )
    route = respx.get("https://x.test/b").mock(return_value=httpx.Response(503))
    with pytest.raises(Exception, match="503"):
        client.get("https://x.test/b")
    assert route.call_count == 3


@respx.mock
def test_404_is_not_retried(http: HttpClient):
    route = respx.get("https://x.test/c").mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        http.get("https://x.test/c")
    assert route.call_count == 1


@respx.mock
def test_conditional_get_sends_validators_and_returns_304(http: HttpClient):
    route = respx.get("https://x.test/feed").mock(return_value=httpx.Response(304))
    resp = http.get("https://x.test/feed", etag='"abc"', last_modified="Wed, 01 Jan 2026")
    assert resp.status_code == 304
    sent = route.calls.last.request.headers
    assert sent["If-None-Match"] == '"abc"'
    assert sent["If-Modified-Since"] == "Wed, 01 Jan 2026"


@respx.mock
def test_per_request_user_agent(http: HttpClient):
    route = respx.get("https://x.test/ua").mock(return_value=httpx.Response(200))
    http.get("https://x.test/ua", user_agent="Jane Doe jane@example.com")
    assert route.calls.last.request.headers["User-Agent"] == "Jane Doe jane@example.com"


def test_rate_limiter_spaces_requests_per_host():
    t = [0.0]
    slept: list[float] = []

    def sleep(d: float) -> None:
        slept.append(d)
        t[0] += d

    rl = RateLimiter({"sec": 10.0}, default_rps=0, clock=lambda: t[0], sleep=sleep)
    rl.wait("sec")
    rl.wait("sec")
    rl.wait("other")  # unlimited host: no wait
    assert slept == [pytest.approx(0.1)]
