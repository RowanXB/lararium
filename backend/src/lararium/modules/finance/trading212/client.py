"""A read-only client for Trading 212's public API (one account per key pair).

Every call goes through `ReadOnlyClient`: GET only, to live.trading212.com only, no
redirects, paced per endpoint. The key and secret are sent as HTTP Basic and never kept
revealed. History endpoints are walked newest first by following `nextPagePath`.
"""

import asyncio
import base64
from collections.abc import Callable, Mapping
from typing import Literal, cast
from urllib.parse import parse_qsl

import httpx

from lararium.core.secrets import Secret
from lararium.modules.finance.readonly_http import (
    Clock,
    HttpError,
    HttpForbiddenError,
    HttpRetryableError,
    JSONValue,
    RateLimit,
    ReadOnlyClient,
    Sleep,
    monotonic,
    wall_time,
)

BASE = "https://live.trading212.com"
PAGE_LIMIT = 50
# A walk that has not ended after this many pages is a pagination fault, not a history.
MAX_PAGES = 300

type Item = dict[str, JSONValue]
type HistoryKind = Literal["orders", "dividends", "transactions"]

_API = "/api/v0/equity"
# Endpoint → (path, permission). The path of a pie's detail is `pie` plus its id.
_ENDPOINTS: Mapping[str, tuple[str, str]] = {
    "summary": (f"{_API}/account/summary", "account"),
    "positions": (f"{_API}/positions", "portfolio"),
    "pending_orders": (f"{_API}/orders", "orders:read"),
    "orders": (f"{_API}/history/orders", "history:orders"),
    "dividends": (f"{_API}/history/dividends", "history:dividends"),
    "transactions": (f"{_API}/history/transactions", "history:transactions"),
    "instruments": (f"{_API}/metadata/instruments", "metadata"),
    "exchanges": (f"{_API}/metadata/exchanges", "metadata"),
    "pies": (f"{_API}/pies", "pies:read"),
    "pie": (f"{_API}/pies", "pies:read"),
}
# The OpenAPI bundle's limits (design §3.2); each endpoint is its own pool.
LIMITS: Mapping[str, RateLimit] = {
    "summary": RateLimit(1, 5),
    "positions": RateLimit(1, 1),
    "pending_orders": RateLimit(1, 5),
    "orders": RateLimit(20, 60),
    "dividends": RateLimit(20, 60),
    "transactions": RateLimit(20, 60),
    "instruments": RateLimit(1, 50),
    "exchanges": RateLimit(1, 30),
    "pies": RateLimit(1, 30),
    "pie": RateLimit(1, 5),
}
_NEXT_PAGE = "unexpected next page path"


def _basic(key: Secret, secret: Secret) -> str:
    # b64encode never wraps lines (GNU base64 does at 76 characters, which breaks the header).
    token = base64.b64encode(f"{key.reveal()}:{secret.reveal()}".encode()).decode("ascii")
    return f"Basic {token}"


def _next_params(path: str, next_page_path: str) -> dict[str, str]:
    """The query of a `nextPagePath`, given as a full path (`/api/v0/…?…`), `?query` or a bare
    query. A full path must be the endpoint's own; anything else is refused, as the client
    takes no query in a path."""
    if "#" in next_page_path:
        raise HttpRetryableError(_NEXT_PAGE)
    if next_page_path.startswith("/"):
        own, _, query = next_page_path.partition("?")
        if own != path:
            raise HttpRetryableError(_NEXT_PAGE)
    elif next_page_path.startswith("?"):
        query = next_page_path[1:]
    elif "?" in next_page_path:
        raise HttpRetryableError(_NEXT_PAGE)
    else:
        query = next_page_path
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise HttpRetryableError(_NEXT_PAGE) from None
    params = dict(pairs)
    if not params or len(params) != len(pairs):
        raise HttpRetryableError(_NEXT_PAGE)
    return params


def _objects(value: JSONValue, endpoint: str) -> list[Item]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise HttpError(f"Trading 212's {endpoint} answer has an unexpected shape")
    return cast(list[Item], value)


def _object(value: JSONValue, endpoint: str) -> Item:
    if not isinstance(value, dict):
        raise HttpError(f"Trading 212's {endpoint} answer has an unexpected shape")
    return value


class Trading212Client:
    def __init__(
        self,
        http: httpx.AsyncClient,
        key: Secret,
        secret: Secret,
        *,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = monotonic,
        wall_clock: Clock = wall_time,
    ) -> None:
        self._client = ReadOnlyClient(
            http,
            BASE,
            lambda: {"Authorization": _basic(key, secret)},
            LIMITS,
            sleep=sleep,
            clock=clock,
            wall_clock=wall_clock,
        )

    async def _get(
        self, endpoint: str, *, suffix: str = "", params: Mapping[str, str] | None = None
    ) -> JSONValue:
        path, permission = _ENDPOINTS[endpoint]
        try:
            response = await self._client.get(f"{path}{suffix}", pool=endpoint, params=params)
        except HttpForbiddenError as error:
            if error.scope is None and not error.cloudflare_block:
                # Trading 212 names the scope; when it doesn't, it is the endpoint's own.
                raise HttpForbiddenError(permission) from None
            raise
        return response.json

    async def _optional(self, endpoint: str, suffix: str = "") -> JSONValue | None:
        """The answer, or None when the key lacks this optional permission."""
        try:
            return await self._get(endpoint, suffix=suffix)
        except HttpForbiddenError as error:
            if error.cloudflare_block:
                raise
            return None

    async def summary(self) -> Item:
        return _object(await self._get("summary"), "account/summary")

    async def positions(self) -> list[Item]:
        return _objects(await self._get("positions"), "positions")

    async def pending_orders(self) -> list[Item] | None:
        found = await self._optional("pending_orders")
        return None if found is None else _objects(found, "orders")

    async def instruments(self) -> list[Item]:
        return _objects(await self._get("instruments"), "metadata/instruments")

    async def exchanges(self) -> list[Item]:
        return _objects(await self._get("exchanges"), "metadata/exchanges")

    async def pies(self) -> list[Item] | None:
        found = await self._optional("pies")
        return None if found is None else _objects(found, "pies")

    async def pie(self, pie_id: int) -> Item | None:
        found = await self._optional("pie", f"/{int(pie_id)}")
        return None if found is None else _object(found, "pies/{id}")

    async def history(self, kind: HistoryKind, seen: Callable[[Item], bool]) -> list[Item]:
        """Every item of a history endpoint, newest first, until `nextPagePath` is null, a
        page holds only `seen` items (or none), or a page repeats the one before it."""
        path, _ = _ENDPOINTS[kind]
        endpoint = f"history/{kind}"
        params: dict[str, str] = {"limit": str(PAGE_LIMIT)}
        items: list[Item] = []
        previous: list[Item] | None = None
        for _ in range(MAX_PAGES):
            body = _object(await self._get(kind, params=params), endpoint)
            page = _objects(body.get("items"), endpoint)
            next_page_path = body.get("nextPagePath")
            if page == previous:
                return items
            items.extend(page)
            if next_page_path is None or all(seen(item) for item in page):
                return items
            if not isinstance(next_page_path, str):
                raise HttpError(f"Trading 212's {endpoint} answer has an unexpected shape")
            params = _next_params(path, next_page_path)
            previous = page
        raise HttpRetryableError("history did not end")
