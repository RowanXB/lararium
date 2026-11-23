"""The read-only HTTP client: GET only, own host only, no redirects, paced, no secrets in errors."""

import asyncio
import itertools
from collections.abc import Callable
from decimal import Decimal

import httpx
import pytest

from lararium import __version__
from lararium.core.errors import CredentialRejectedError
from lararium.modules.finance.readonly_http import (
    HttpAuthError,
    HttpError,
    HttpForbiddenError,
    HttpRetryableError,
    RateLimit,
    ReadOnlyClient,
    ReadOnlyViolation,
    loads_decimal,
)

BASE = "https://api.example.test"
KEY = "key-secret-123"
QUERY_VALUE = "query-secret-456"
Handler = Callable[[httpx.Request], httpx.Response]


class Env:
    """A fake clock that sleeping advances, and a transport that records each request."""

    def __init__(self, *responses: httpx.Response, now: int = 1000) -> None:
        self.now = Decimal(now)
        self.sleeps: list[int] = []
        self.requests: list[tuple[Decimal, httpx.Request]] = []
        self._responses = list(responses)

    async def sleep(self, seconds: int) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self) -> Decimal:
        return self.now

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((self.now, request))
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]

    def client(self, limit: RateLimit | None = None) -> ReadOnlyClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return ReadOnlyClient(
            http,
            BASE,
            lambda: {"Authorization": KEY},
            {"default": limit or RateLimit(100, 1)},
            sleep=self.sleep,
            clock=self.clock,
            wall_clock=self.clock,
        )


def ok(body: str = "{}", **headers: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers=headers)


@pytest.mark.parametrize(
    "path",
    [
        "https://api.example.test/v1/x",
        "https://evil.test/v1/x",
        "//evil.test/v1/x",
        "v1/x",
        "",
        "/v1/x\\@evil.test",
        "/v1/x?a=1",
        "/v1/x#frag",
        "/v1/%3Fa=1",
        "/v1/%3fa=1",
        "/v1/x%23y",
        "/v1/x%23y".upper(),
        "/v1/../x",
        "/v1/./x",
        "/v1/%2e%2e/x",
        "/v1/%2E%2E/x",
        "/v1/%2e/x",
        "/..",
    ],
)
async def test_only_get_to_own_host(path: str) -> None:
    env = Env(ok())
    with pytest.raises(ReadOnlyViolation):
        await env.client().get(path, pool="default")
    assert env.requests == []


async def test_redirect_not_followed() -> None:
    env = Env(httpx.Response(302, headers={"Location": "https://evil.test/x"}), ok())
    with pytest.raises(HttpRetryableError):
        await env.client().get("/v1/x", pool="default")
    assert [r.url.host for _, r in env.requests] == ["api.example.test"]


async def test_user_agent_sent() -> None:
    env = Env(ok())
    await env.client().get("/v1/x", pool="default", params={"a": "1"})
    request = env.requests[0][1]
    assert request.method == "GET"
    assert request.headers["User-Agent"] == f"Lararium/{__version__}"
    assert request.headers["Authorization"] == KEY
    assert request.url.params["a"] == "1"


def test_numbers_are_decimal() -> None:
    value = loads_decimal('{"a": 0.1, "b": 12, "c": [1.50], "d": true, "e": null}')
    assert value == {
        "a": Decimal("0.1"),
        "b": Decimal("12"),
        "c": [Decimal("1.50")],
        "d": True,
        "e": None,
    }
    assert isinstance(value, dict)
    c = value["c"]
    assert isinstance(c, list)
    assert str(c[0]) == "1.50"


async def test_response_carries_status_text_json_headers() -> None:
    env = Env(ok('{"a": 0.1}', **{"X-Thing": "v"}))
    response = await env.client().get("/v1/x", pool="default")
    assert response.status == 200
    assert response.text == '{"a": 0.1}'
    assert response.json == {"a": Decimal("0.1")}
    assert response.headers["x-thing"] == "v"


async def test_429_waits_reset_then_retries() -> None:
    env = Env(httpx.Response(429, headers={"Retry-After": "3"}), ok())
    await env.client().get("/v1/x", pool="default")
    assert env.sleeps == [3]
    assert len(env.requests) == 2


async def test_429_epoch_reset_uses_the_injected_clock() -> None:
    env = Env(
        httpx.Response(429, headers={"x-ratelimit-reset": "1800000007"}),
        ok(),
        now=1_800_000_000,
    )
    await env.client().get("/v1/x", pool="default")
    assert env.sleeps == [7]


async def test_429_gives_up_after_three_retries() -> None:
    env = Env(httpx.Response(429, headers={"Retry-After": "1"}))
    with pytest.raises(HttpRetryableError):
        await env.client().get("/v1/x", pool="default")
    assert len(env.requests) == 4
    assert env.sleeps == [1, 1, 1]


async def test_401_is_auth_error() -> None:
    env = Env(httpx.Response(401))
    with pytest.raises(HttpAuthError) as caught:
        await env.client().get("/v1/x", pool="default")
    assert isinstance(caught.value, CredentialRejectedError)
    assert isinstance(caught.value, HttpError)


async def test_403_scope_parsed() -> None:
    env = Env(httpx.Response(403, text="Scope( pies:read ) missing for API key"))
    with pytest.raises(HttpForbiddenError) as caught:
        await env.client().get("/v1/x", pool="default")
    assert caught.value.scope == "pies:read"
    assert caught.value.cloudflare_block is False


async def test_403_cloudflare_block_flagged() -> None:
    env = Env(httpx.Response(403, text="error code: 1010"))
    with pytest.raises(HttpForbiddenError) as caught:
        await env.client().get("/v1/x", pool="default")
    assert caught.value.cloudflare_block is True
    assert caught.value.scope is None


@pytest.mark.parametrize("status", [500, 503])
async def test_5xx_is_retryable(status: int) -> None:
    env = Env(httpx.Response(status))
    with pytest.raises(HttpRetryableError):
        await env.client().get("/v1/x", pool="default")


async def test_timeout_is_retryable() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    client = ReadOnlyClient(http, BASE, dict, {"default": RateLimit(1, 1)})
    with pytest.raises(HttpRetryableError):
        await client.get("/v1/x", pool="default")


async def test_other_4xx_is_http_error() -> None:
    env = Env(httpx.Response(404))
    with pytest.raises(HttpError) as caught:
        await env.client().get("/v1/x", pool="default")
    assert not isinstance(caught.value, HttpRetryableError)


async def test_pacing_per_pool() -> None:
    env = Env(ok())
    http = httpx.AsyncClient(transport=httpx.MockTransport(env.handler))
    client = ReadOnlyClient(
        http,
        BASE,
        dict,
        {"slow": RateLimit(1, 5), "other": RateLimit(1, 5)},
        sleep=env.sleep,
        clock=env.clock,
    )
    await client.get("/a", pool="slow")
    await client.get("/b", pool="other")  # its own pool: no wait
    await client.get("/c", pool="slow")
    assert env.requests[2][0] - env.requests[0][0] >= 5
    assert env.requests[1][0] == env.requests[0][0]


async def test_pacing_window_allows_burst() -> None:
    env = Env(ok())
    client = env.client(RateLimit(2, 10))
    for _ in range(3):
        await client.get("/a", pool="default")
    assert env.sleeps == [10]


async def test_unknown_pool_is_a_programming_error() -> None:
    env = Env(ok())
    with pytest.raises(KeyError):
        await env.client().get("/a", pool="nope")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": f"https://x.test/?k={KEY}"}),
        httpx.Response(401, text=KEY),
        httpx.Response(403, text=f"Scope( s ) missing {KEY}"),
        httpx.Response(429, headers={"Retry-After": "1", "X-Echo": KEY}),
        httpx.Response(500, text=KEY),
        httpx.Response(404, text=KEY),
        httpx.Response(200, text=KEY),
    ],
)
async def test_errors_never_contain_secrets(response: httpx.Response) -> None:
    env = Env(response)
    with pytest.raises(HttpError) as caught:
        await env.client().get("/v1/x", pool="default", params={"q": QUERY_VALUE})
    for text in (str(caught.value), repr(caught.value)):
        assert KEY not in text
        assert QUERY_VALUE not in text


async def test_transport_error_never_contains_url_query() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    client = ReadOnlyClient(http, BASE, lambda: {"A": KEY}, {"default": RateLimit(1, 1)})
    with pytest.raises(HttpRetryableError) as caught:
        await client.get("/v1/x", pool="default", params={"q": QUERY_VALUE})
    assert QUERY_VALUE not in repr(caught.value)
    assert QUERY_VALUE not in str(caught.value)


async def test_path_escaping_the_base_path_is_refused() -> None:
    env = Env(ok())
    http = httpx.AsyncClient(transport=httpx.MockTransport(env.handler))
    client = ReadOnlyClient(http, f"{BASE}/v2", dict, {"default": RateLimit(1, 1)})
    for path in ("/../v1/x", "/%2e%2e/v1/x", "/a/../../v1/x"):
        with pytest.raises(ReadOnlyViolation):
            await client.get(path, pool="default")
    assert env.requests == []
    await client.get("/ok", pool="default")
    assert str(env.requests[0][1].url) == f"{BASE}/v2/ok"


async def test_429_wait_beyond_the_cap_raises_at_once() -> None:
    env = Env(httpx.Response(429, headers={"Retry-After": "500"}), ok())
    with pytest.raises(HttpRetryableError):
        await env.client().get("/v1/x", pool="default")
    assert env.sleeps == []
    assert len(env.requests) == 1


async def test_429_retry_after_http_date_falls_back() -> None:
    env = Env(
        httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        ok(),
    )
    await env.client().get("/v1/x", pool="default")
    assert env.sleeps == [1]


@pytest.mark.parametrize("body", ['{"a": NaN}', "[Infinity]", "-Infinity"])
async def test_nan_and_infinity_are_not_json(body: str) -> None:
    with pytest.raises(ValueError, match="not a JSON number"):
        loads_decimal(body)
    env = Env(ok(body))
    with pytest.raises(HttpError, match="not JSON"):
        await env.client().get("/v1/x", pool="default")


@pytest.mark.parametrize(
    ("body", "scope"),
    [
        ("Scope( pies:read ) missing for API key", "pies:read"),
        ("Scope( bad scope! ) missing for API key", None),
        ("Scope( <script>x</script> ) missing for API key", None),
        ("Scope( " + "a" * 101 + " ) missing for API key", None),
    ],
)
async def test_403_message_is_fixed_text(body: str, scope: str | None) -> None:
    env = Env(httpx.Response(403, text=body))
    with pytest.raises(HttpForbiddenError) as caught:
        await env.client().get("/v1/x", pool="default")
    assert caught.value.scope == scope
    assert str(caught.value) == "The API refused the request (403): the key lacks a permission"
    assert "Scope" not in str(caught.value)
    assert "script" not in repr(caught.value)


async def test_403_cloudflare_message_is_fixed_text() -> None:
    env = Env(httpx.Response(403, text="error code: 1010 <b>ray</b>"))
    with pytest.raises(HttpForbiddenError) as caught:
        await env.client().get("/v1/x", pool="default")
    assert str(caught.value) == "The API's firewall refused this client (403)"
    assert "ray" not in str(caught.value)


async def test_concurrent_callers_respect_the_limit() -> None:
    env = Env(ok())
    client = env.client(RateLimit(1, 5))
    await asyncio.gather(*(client.get("/a", pool="default") for _ in range(3)))
    times = [t for t, _ in env.requests]
    assert all(b - a >= 5 for a, b in itertools.pairwise(times))
