"""The Trading 212 client: Basic auth, its endpoints, and the walk over history pages."""

import base64
from decimal import Decimal
from typing import Any

import httpx
import pytest

from lararium.core.secrets import Secret
from lararium.modules.finance.readonly_http import (
    HttpAuthError,
    HttpError,
    HttpForbiddenError,
    HttpRetryableError,
)
from lararium.modules.finance.trading212.client import BASE, MAX_PAGES, Trading212Client

from .fixtures import (
    dumps,
    order_item,
    page,
    pie_json,
    summary_json,
    transaction_item,
)

KEY = "k" * 40 + "-synthetic-key"
SECRET = "s" * 40 + "-synthetic-secret"
ORDERS = "/api/v0/equity/history/orders"


class Env:
    """A fake clock that sleeping advances, and a transport that answers in turn."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.now = Decimal(1000)
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)

    async def sleep(self, seconds: int) -> None:
        self.now += seconds

    def clock(self) -> Decimal:
        return self.now

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]

    def client(self) -> Trading212Client:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return Trading212Client(
            http,
            Secret(KEY),
            Secret(SECRET),
            sleep=self.sleep,
            clock=self.clock,
            wall_clock=self.clock,
        )

    def params(self) -> list[dict[str, str]]:
        return [dict(r.url.params) for r in self.requests]


def ok(body: Any) -> httpx.Response:
    return httpx.Response(200, text=dumps(body))


def forbidden(scope: str) -> httpx.Response:
    return httpx.Response(403, text=f"Scope( {scope} ) missing for API key")


def orders(*fill_ids: int) -> list[dict[str, Any]]:
    return [order_item(order_id=fill_id - 1000, fill_id=fill_id) for fill_id in fill_ids]


async def test_basic_auth_header() -> None:
    env = Env(ok(summary_json()))
    summary = await env.client().summary()
    assert summary["id"] == 10000001
    (request,) = env.requests
    expected = base64.b64encode(f"{KEY}:{SECRET}".encode()).decode()
    assert request.headers["Authorization"] == f"Basic {expected}"
    # GNU base64 wraps at 76 characters; the header never does.
    assert "\n" not in request.headers["Authorization"]
    assert len(expected) > 76
    assert str(request.url) == f"{BASE}/api/v0/equity/account/summary"
    assert request.method == "GET"


async def test_next_page_forms_and_repeat_stop() -> None:
    last = page(orders(7003), "limit=50&cursor=7")
    env = Env(
        ok(page(orders(7009), f"{ORDERS}?limit=50&cursor=9")),
        ok(page(orders(7008), "?limit=50&cursor=8")),
        ok(last),
        ok(last),
        ok(page(orders(7001))),
    )
    items = await env.client().history("orders", lambda item: False)
    assert len(env.requests) == 4  # the first, then three after it; the repeat is not followed
    assert {r.url.path for r in env.requests} == {ORDERS}
    assert env.params() == [
        {"limit": "50"},
        {"limit": "50", "cursor": "9"},
        {"limit": "50", "cursor": "8"},
        {"limit": "50", "cursor": "7"},
    ]
    assert [item["fill"]["id"] for item in items] == [7009, 7008, 7003]


async def test_history_stops_at_seen_page() -> None:
    stored = {7005, 7004}
    env = Env(
        ok(page(orders(7007, 7006), "?limit=50&cursor=6")),
        ok(page(orders(7005, 7004), "?limit=50&cursor=4")),
        ok(page(orders(7003))),
    )
    items = await env.client().history("orders", lambda item: item["fill"]["id"] in stored)
    assert len(env.requests) == 2
    assert [item["fill"]["id"] for item in items] == [7007, 7006, 7005, 7004]


async def test_history_ends_at_null_next_page() -> None:
    env = Env(
        ok(page([transaction_item("t-2")], "limit=50&cursor=abc")),
        ok(page([transaction_item("t-1")])),
    )
    items = await env.client().history("transactions", lambda item: False)
    assert [item["reference"] for item in items] == ["t-2", "t-1"]
    assert env.requests[1].url.path == "/api/v0/equity/history/transactions"


@pytest.mark.parametrize(
    "next_page_path",
    [
        "/api/v0/equity/history/dividends?limit=50&cursor=9",
        "/api/v0/equity/history/orders/../pies?cursor=9",
        "https://live.trading212.com/api/v0/equity/history/orders?cursor=9",
        "//evil.test/api/v0/equity/history/orders?cursor=9",
        "equity/history/orders?cursor=9",
        "?limit=50&cursor=9#x",
        "?",
        "",
        "limit=50&limit=40",
    ],
)
async def test_unexpected_next_page_path_refused(next_page_path: str) -> None:
    env = Env(ok(page(orders(7009), next_page_path)))
    with pytest.raises(HttpRetryableError, match="unexpected next page path"):
        await env.client().history("orders", lambda item: False)
    assert len(env.requests) == 1


async def test_history_that_does_not_end_fails() -> None:
    counter = iter(range(10**6))

    def handler(request: httpx.Request) -> httpx.Response:
        n = next(counter)
        return ok(page(orders(10**5 + n), f"?limit=50&cursor={n}"))

    env = Env()
    env.handler = handler  # type: ignore[method-assign]
    with pytest.raises(HttpRetryableError, match="history did not end"):
        await env.client().history("orders", lambda item: False)
    assert next(counter) == MAX_PAGES


async def test_missing_scope_names_permission() -> None:
    env = Env(forbidden("history:orders"))
    with pytest.raises(HttpForbiddenError) as raised:
        await env.client().history("orders", lambda item: False)
    assert raised.value.scope == "history:orders"

    assert await Env(forbidden("pies:read")).client().pies() is None
    assert await Env(forbidden("pies:read")).client().pie(9001) is None
    assert await Env(forbidden("orders:read")).client().pending_orders() is None


async def test_403_without_scope_names_the_endpoints_permission() -> None:
    env = Env(httpx.Response(403, text="Forbidden"))
    with pytest.raises(HttpForbiddenError) as raised:
        await env.client().positions()
    assert raised.value.scope == "portfolio"


async def test_firewall_block_is_not_a_missing_optional_permission() -> None:
    env = Env(httpx.Response(403, text="error code: 1010"))
    with pytest.raises(HttpForbiddenError) as raised:
        await env.client().pies()
    assert raised.value.cloudflare_block


async def test_401_is_auth_error() -> None:
    with pytest.raises(HttpAuthError):
        await Env(httpx.Response(401, text="x")).client().summary()


async def test_endpoints_and_shapes() -> None:
    env = Env(
        ok([pie_json()]),
        ok({"instruments": [], "settings": {"id": 9001}}),
        ok([]),
        ok([]),
        ok([]),
        ok([]),
    )
    client = env.client()
    assert await client.pies() == [pie_json()]
    assert (await client.pie(9001)) == {"instruments": [], "settings": {"id": 9001}}
    assert await client.positions() == []
    assert await client.instruments() == []
    assert await client.exchanges() == []
    assert await client.pending_orders() == []
    assert [r.url.path for r in env.requests] == [
        "/api/v0/equity/pies",
        "/api/v0/equity/pies/9001",
        "/api/v0/equity/positions",
        "/api/v0/equity/metadata/instruments",
        "/api/v0/equity/metadata/exchanges",
        "/api/v0/equity/orders",
    ]


async def test_wrong_shape_refused() -> None:
    with pytest.raises(HttpError, match="positions"):
        await Env(ok({"items": []})).client().positions()
    with pytest.raises(HttpError, match="history/orders"):
        await (
            Env(ok({"items": {}, "nextPagePath": None}))
            .client()
            .history("orders", lambda item: False)
        )


async def test_history_paced_to_its_limit() -> None:
    pages = [ok(page(orders(8000 - n), f"?limit=50&cursor={n}")) for n in range(21)]
    env = Env(*pages, ok(page(orders(1))))
    await env.client().history("orders", lambda item: False)
    assert len(env.requests) == 22
    # 20 a minute: the 21st request waited for the first to leave the window.
    assert env.now >= Decimal(1060)
