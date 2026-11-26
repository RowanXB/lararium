"""A read-only client for eToro's public API (one user key).

Every call goes through `ReadOnlyClient`: GET only, to public-api.etoro.com only, no redirects,
paced per eToro's rate-limit pool. The public API key and the user key are read from their
`Secret`s inside each request's headers, beside a fresh `x-request-id`; they are never kept
revealed.

The read-only gate (design §5.1, Decision 13) is structural: every call but `me()` raises
`ReadOnlyGateError` before sending until `me()` has returned scopes that all pass the allow-list
(`write_scopes` lists none). The connector calls `me()` first and refuses the key otherwise.
"""

import asyncio
import json
import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import cast

import httpx

from lararium.core.errors import CredentialRejectedError
from lararium.core.secrets import Secret
from lararium.modules.finance.readonly_http import (
    Clock,
    HttpAuthError,
    HttpError,
    HttpForbiddenError,
    HttpNotFoundError,
    HttpRetryableError,
    JSONValue,
    RateLimit,
    ReadOnlyClient,
    Sleep,
    monotonic,
    record_json,
    wall_time,
)

BASE = "https://public-api.etoro.com"
# Trade history: the sync walks back in windows whose minDate is this many days apart (eToro:
# a request's look-back at most a year less a day). A window's pages end on an empty page,
# never a short one: eToro states no page-size cap, so a short page proves nothing.
TRADE_WINDOW_DAYS = 364
TRADE_PAGE = 100
CASH_PAGE = 500
DATA_PAGE = 500
# Balance history: at most this many days, both ends included, per request.
BALANCE_HISTORY_DAYS = 365
INSTRUMENTS_PER_REQUEST = 50
CANDLE_LIMIT = 2000
# A walk that has not ended after this many pages is a pagination fault, not a history.
MAX_PAGES = 300

type Item = dict[str, JSONValue]

# eToro's pools (design §3.1): a shared default, and dedicated pools for the portfolio views,
# the aggregate portfolio and market data. An endpoint eToro gives a dedicated pool of its own
# that is not one of these (orders:lookup) is paced in the default pool, which only slows it.
LIMITS: Mapping[str, RateLimit] = {
    "default": RateLimit(60, 60),
    "portfolio": RateLimit(60, 60),
    "aggregate": RateLimit(60, 60),
    "market_data": RateLimit(120, 60),
}
# Endpoint → (path, pool). The cash-transactions and candles paths take an id and a suffix.
_ENDPOINTS: Mapping[str, tuple[str, str]] = {
    "me": ("/api/v1/me", "default"),
    "pnl": ("/api/v1/trading/info/real/pnl", "portfolio"),
    "aggregate": ("/api/v1/trading/info/aggregate-portfolio", "aggregate"),
    "breakdown": ("/api/v2/trading/info/instrument-breakdown", "portfolio"),
    "balances": ("/api/v1/balances", "default"),
    "balance_history": ("/api/v1/balances/history", "default"),
    "cash_transactions": ("/api/v1/money/accounts/cash", "default"),
    "trade_history": ("/api/v1/trading/info/trade/history", "default"),
    "order_lookup": ("/api/v2/trading/info/orders:lookup", "default"),
    "closed_events": ("/api/v1/data/positions/closed-events/history", "default"),
    "stockback": ("/api/v1/data/stockback/payments", "default"),
    "instruments": ("/api/v1/market-data/instruments", "market_data"),
    "instrument_types": ("/api/v1/market-data/instrument-types", "market_data"),
    "exchanges": ("/api/v1/market-data/exchanges", "market_data"),
    "candles": ("/api/v1/data/instruments", "market_data"),
}
# The read-only gate's refusal (Decision 13): fixed text, naming no scope.
GATE_MESSAGE = "This eToro key can trade or move money. Create a key with Read permission."
_NO_SCOPES = "This eToro key reports no permissions. Create a key with Read permission."
_CLOUDFLARE = "eToro's firewall refused this client's User-Agent (403)"
_ME_FORBIDDEN = "eToro refused the key on /me (403)"
_WHOLE = "an eToro id is a whole number"
# An id is below this, as a bigint column holds it (at most 19 digits).
_ID_BOUND = 10**19
# A cash account id goes into the path, so it is a plain token: no `/`, `.`, `%` or `?`.
_ACCOUNT_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z-]{0,63}")


class ReadOnlyGateError(CredentialRejectedError):
    """The key holds a scope that is not read-only, reports none, or `me()` has not cleared it
    yet. The message is fixed text and names no scope."""

    def __init__(self, message: str = GATE_MESSAGE) -> None:
        super().__init__(message)


def endpoint_path(endpoint: str) -> str:
    """An endpoint's path, for the request a raw record notes (never a key or a header)."""
    return _ENDPOINTS[endpoint][0]


def write_scopes(scopes: Iterable[str]) -> list[str]:
    """The scopes the read-only gate refuses, as given: every one not ending `:read` (compared
    after stripping space, ignoring case). An allow-list, since eToro's write-capable scopes are
    not all named `:write` (`money:transfer`)."""
    return [scope for scope in scopes if not scope.strip().lower().endswith(":read")]


def _shape(endpoint: str, field: str | None = None) -> HttpError:
    where = f": {field}" if field else ""
    return HttpError(f"eToro's {endpoint} answer has an unexpected shape{where}")


def _object(value: JSONValue, endpoint: str) -> Item:
    if not isinstance(value, dict):
        raise _shape(endpoint)
    return value


def _objects(value: JSONValue, endpoint: str, field: str | None = None) -> list[Item]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise _shape(endpoint, field)
    return cast(list[Item], value)


def _whole(value: int | Decimal) -> int:
    """A positive whole id below _ID_BOUND (a Decimal as parsed, or an int); anything else
    raises `ValueError` rather than asking for another id. The size is checked before
    converting: a huge exponent would build a huge int."""
    if isinstance(value, bool) or (
        isinstance(value, Decimal)
        and (not value.is_finite() or value.adjusted() >= 19 or value != value.to_integral_value())
    ):
        raise ValueError(_WHOLE)
    whole = int(value)
    if not 0 < whole < _ID_BOUND:
        raise ValueError(_WHOLE)
    return whole


def _key(item: Item) -> str:
    """An item's exact content as one text, to drop an exact repeat."""
    return json.dumps(record_json(item), sort_keys=True)


class EtoroClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        api_key: Secret,
        user_key: Secret,
        *,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = monotonic,
        wall_clock: Clock = wall_time,
    ) -> None:
        self._client = ReadOnlyClient(
            http,
            BASE,
            lambda: {
                "x-api-key": api_key.reveal(),
                "x-user-key": user_key.reveal(),
                "x-request-id": str(uuid.uuid4()),
            },
            LIMITS,
            sleep=sleep,
            clock=clock,
            wall_clock=wall_clock,
        )
        # Whether `me()` has returned only read scopes; until then only `/me` is sent.
        self._cleared = False

    @property
    def cleared(self) -> bool:
        """Whether the last `me()` returned only read scopes: only then are other calls sent."""
        return self._cleared

    async def _get(
        self, endpoint: str, *, suffix: str = "", params: Mapping[str, str] | None = None
    ) -> JSONValue:
        path, pool = _ENDPOINTS[endpoint]
        if endpoint != "me" and not self._cleared:
            raise ReadOnlyGateError()
        try:
            response = await self._client.get(f"{path}{suffix}", pool=pool, params=params)
        except HttpForbiddenError as error:
            if error.cloudflare_block:
                # Cloudflare's error 1010: the User-Agent, not the key, was refused.
                raise HttpRetryableError(_CLOUDFLARE) from None
            raise
        return response.json

    async def _list(
        self, endpoint: str, field: str, *, params: Mapping[str, str] | None = None
    ) -> list[Item]:
        """The objects under `field` of an endpoint's answer."""
        body = _object(await self._get(endpoint, params=params), endpoint)
        return _objects(body.get(field), endpoint, field)

    async def me(self) -> Item:
        """The user's ids, personal fields and the key's scopes, and the gate: the other calls
        are allowed only when every scope passes `write_scopes`. A key with any other scope is
        returned too (its scopes are worth recording), but the gate stays shut. `scopes` must be
        a list of strings, else the answer is refused; an empty list raises `ReadOnlyGateError`.
        A 403 here is the key refused (design §5.1); Cloudflare's 1010 stays retryable."""
        self._cleared = False
        try:
            body = _object(await self._get("me"), "/me")
        except HttpForbiddenError:
            raise HttpAuthError(_ME_FORBIDDEN) from None
        scopes = body.get("scopes")
        if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
            raise _shape("/me", "scopes")
        if not scopes:
            # Nothing to check is not a pass: fail closed.
            raise ReadOnlyGateError(_NO_SCOPES)
        self._cleared = not write_scopes(cast(list[str], scopes))
        return body

    async def pnl(self) -> Item:
        return _object(await self._get("pnl"), "pnl")

    async def aggregate(self) -> Item:
        return _object(await self._get("aggregate"), "aggregate-portfolio")

    async def breakdown(self) -> Item:
        params = {"positionLevel": "Normal", "orderLevel": "Normal", "mirrorLevel": "Details"}
        return _object(await self._get("breakdown", params=params), "instrument-breakdown")

    async def balances(self) -> Item:
        params = {
            "includeZeroBalances": "true",
            "includeSubAccounts": "true",
            "expand": "equityDetails",
        }
        return _object(await self._get("balances", params=params), "balances")

    async def balance_history(self, from_date: date, to_date: date) -> Item:
        """End-of-day snapshots from `from_date` to `to_date`, both included: at most
        BALANCE_HISTORY_DAYS days, else `ValueError` before sending."""
        days = (to_date - from_date).days + 1
        if not 0 < days <= BALANCE_HISTORY_DAYS:
            raise ValueError(f"a balance history request covers 1 to {BALANCE_HISTORY_DAYS} days")
        params = {"fromDate": from_date.isoformat(), "toDate": to_date.isoformat()}
        return _object(await self._get("balance_history", params=params), "balances/history")

    async def cash_transactions(self, account_id: str, seen: Callable[[Item], bool]) -> list[Item]:
        """An eToro Money account's transactions, newest first, following `nextPageToken`
        until `hasNext` is false or a page holds only `seen` items (or none). `seen` is asked
        of every item of a page, in order; every page fetched is returned."""
        if not _ACCOUNT_ID.fullmatch(account_id):
            raise ValueError("an eToro cash account id is letters, digits and dashes")
        return await self._follow(
            "cash_transactions",
            "cash transactions",
            suffix=f"/{account_id}/transactions",
            query={"pageSize": str(CASH_PAGE)},
            token=("nextPageToken", "pageToken"),
            # A list, not a generator: `all` would stop asking at the first unseen item.
            done=lambda page: all([seen(item) for item in page]),
        )

    async def _follow(
        self,
        endpoint: str,
        label: str,
        *,
        suffix: str,
        query: Mapping[str, str],
        token: tuple[str, str],
        done: Callable[[list[Item]], bool] = lambda page: False,
    ) -> list[Item]:
        """Every `results` item of an endpoint paged by an opaque token: `token` is (the
        pagination field that gives it, the query parameter that takes it). It stops when
        `hasNext` is not true or `done(page)`; a repeated token, a missing one while `hasNext`
        is true, or MAX_PAGES pages raise."""
        field, parameter = token
        params = dict(query)
        items: list[Item] = []
        for _ in range(MAX_PAGES):
            body = _object(await self._get(endpoint, suffix=suffix, params=params), label)
            page = _objects(body.get("results"), label, "results")
            items.extend(page)
            pagination = body.get("pagination")
            if not isinstance(pagination, dict):
                raise _shape(label, "pagination")
            if done(page) or pagination.get("hasNext") is not True:
                return items
            next_token = pagination.get(field)
            if not isinstance(next_token, str) or not next_token:
                raise _shape(label, f"pagination.{field}")
            if next_token == params.get(parameter):
                raise HttpRetryableError(f"eToro's {label} repeated a page")
            params = {**query, parameter: next_token}
        raise HttpRetryableError(f"eToro's {label} did not end")

    async def trade_history(self, min_date: date) -> list[Item]:
        """One window: every trade eToro closed from `min_date` to now, which is the contract
        the sync's walk back relies on (it asks windows TRADE_WINDOW_DAYS apart until one adds
        no new `positionId`). Pages are asked with
        `page`/`pageSize` until an empty page; a short page does not end it, since eToro may
        cap the page below TRADE_PAGE. A row repeated exactly (the pages shifting under a new
        close) is dropped; a row that differs is kept for the caller to judge. A full page
        repeated (`page` ignored) or MAX_PAGES pages raise."""
        endpoint = "trade history"
        rows: list[Item] = []
        keys: set[str] = set()
        previous: list[Item] | None = None
        for number in range(1, MAX_PAGES + 1):
            params = {
                "minDate": min_date.isoformat(),
                "page": str(number),
                "pageSize": str(TRADE_PAGE),
            }
            page = _objects(await self._get("trade_history", params=params), endpoint)
            if not page:
                return rows
            if page == previous:
                raise HttpRetryableError(f"eToro's {endpoint} repeated a page")
            for row in page:
                key = _key(row)
                if key not in keys:
                    keys.add(key)
                    rows.append(row)
            previous = page
        raise HttpRetryableError(f"eToro's {endpoint} did not end")

    async def order_lookup(self, order_id: int | Decimal) -> Item | None:
        """An order's detail and executions, or None when eToro has no such order (404)."""
        params = {"orderId": str(_whole(order_id))}
        try:
            return _object(await self._get("order_lookup", params=params), "orders:lookup")
        except HttpNotFoundError:
            return None

    async def _paged(self, endpoint: str, label: str) -> list[Item]:
        """Every `results` item of a `page`/`pageSize` endpoint, until `hasNext` is false."""
        items: list[Item] = []
        previous: list[Item] | None = None
        for number in range(1, MAX_PAGES + 1):
            params = {"page": str(number), "pageSize": str(DATA_PAGE)}
            body = _object(await self._get(endpoint, params=params), label)
            page = _objects(body.get("results"), label, "results")
            pagination = body.get("pagination")
            if not isinstance(pagination, dict):
                raise _shape(label, "pagination")
            if page and page == previous:
                raise HttpRetryableError(f"eToro's {label} repeated a page")
            items.extend(page)
            if pagination.get("hasNext") is not True:
                return items
            previous = page
        raise HttpRetryableError(f"eToro's {label} did not end")

    async def closed_events(self) -> list[Item]:
        return await self._paged("closed_events", "closed-events")

    async def stockback(self) -> list[Item]:
        return await self._paged("stockback", "stockback")

    async def instruments(self, ids: Sequence[int | Decimal]) -> list[Item]:
        """Display data of the instruments `ids`, INSTRUMENTS_PER_REQUEST per request. No ids
        asks nothing: without the filter eToro would list every instrument."""
        wanted = [_whole(i) for i in ids]
        items: list[Item] = []
        for at in range(0, len(wanted), INSTRUMENTS_PER_REQUEST):
            chunk = wanted[at : at + INSTRUMENTS_PER_REQUEST]
            params = {"instrumentIds": ",".join(str(i) for i in chunk)}
            items.extend(await self._list("instruments", "instrumentDisplayDatas", params=params))
        return items

    async def instrument_types(self) -> list[Item]:
        return await self._list("instrument_types", "instrumentTypes")

    async def exchanges(self) -> list[Item]:
        return await self._list("exchanges", "exchangeInfo")

    async def candles(self, instrument_id: int | Decimal, start: date, end: date) -> list[Item]:
        """Daily candles from `start` to `end`, both included, following `nextCursor`."""
        if end < start:
            raise ValueError("a candle range ends on or after its start")
        query = {
            "interval": "1d",
            "from": f"{start.isoformat()}T00:00:00Z",
            # `to` is exclusive: the day after `end`.
            "to": f"{(end + timedelta(days=1)).isoformat()}T00:00:00Z",
            "limit": str(CANDLE_LIMIT),
        }
        return await self._follow(
            "candles",
            "candles",
            suffix=f"/{_whole(instrument_id)}/candles",
            query=query,
            token=("nextCursor", "cursor"),
        )
