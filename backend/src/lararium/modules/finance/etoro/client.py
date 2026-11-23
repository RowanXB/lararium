"""A read-only client for eToro's public API (one user key).

Every call goes through `ReadOnlyClient`: GET only, to public-api.etoro.com only, no redirects,
paced per eToro's rate-limit pool. The public API key and the user key are read from their
`Secret`s inside each request's headers, beside a fresh `x-request-id`; they are never kept
revealed.

The read-only gate (design §5.1) is the connector's: it calls `me()` before anything else and
refuses the key when `write_scopes` lists any of its scopes.
"""

import asyncio
import json
import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast

import httpx

from lararium.core.errors import CredentialRejectedError
from lararium.core.secrets import Secret
from lararium.modules.finance.readonly_http import (
    Clock,
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
# Trade history: each window's minDate is this many days after the one before (eToro: a
# request's look-back at most a year less a day); each window's pages end on a short page.
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
_GATE = "This eToro key can trade or move money. Create a key with Read permission."
_CLOUDFLARE = "eToro's firewall refused this client's User-Agent (403)"
_WHOLE = "an eToro id is a whole number"
# An id is below this, as a bigint column holds it (at most 19 digits).
_ID_BOUND = 10**19
# A cash account id goes into the path, so it is a plain token: no `/`, `.`, `%` or `?`.
_ACCOUNT_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z-]{0,63}")


class ReadOnlyGateError(CredentialRejectedError):
    """The key holds a scope that can trade or move money. The message names no scope."""

    def __init__(self) -> None:
        super().__init__(_GATE)


def write_scopes(scopes: Iterable[str]) -> list[str]:
    """The scopes that can trade or move money: any ending `:write`, and `money:transfer`
    (bare or under eToro's `etoro-public:` prefix, as `/me` returns them). Matching ignores
    case and surrounding space, so an odd spelling fails closed; the scopes come back as given."""
    found: list[str] = []
    for scope in scopes:
        plain = scope.strip().lower()
        if (
            plain.endswith(":write")
            or plain == "money:transfer"
            or plain.endswith(":money:transfer")
        ):
            found.append(scope)
    return found


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
        self._wall_clock = wall_clock

    def _today(self) -> date:
        """Today's UTC date, by the wall clock."""
        return datetime.fromtimestamp(int(self._wall_clock()), UTC).date()

    async def _get(
        self, endpoint: str, *, suffix: str = "", params: Mapping[str, str] | None = None
    ) -> JSONValue:
        path, pool = _ENDPOINTS[endpoint]
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
        """The user's ids, personal fields and the key's scopes. `scopes` must be a list of
        strings, so the read-only gate never passes a key it could not read."""
        body = _object(await self._get("me"), "/me")
        scopes = body.get("scopes")
        if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
            raise _shape("/me", "scopes")
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
        endpoint = "cash transactions"
        suffix = f"/{account_id}/transactions"
        params = {"pageSize": str(CASH_PAGE)}
        items: list[Item] = []
        for _ in range(MAX_PAGES):
            body = _object(
                await self._get("cash_transactions", suffix=suffix, params=params), endpoint
            )
            page = _objects(body.get("results"), endpoint, "results")
            items.extend(page)
            pagination = body.get("pagination")
            if not isinstance(pagination, dict):
                raise _shape(endpoint, "pagination")
            # A list, not a generator: `all` would stop asking at the first unseen item.
            if all([seen(item) for item in page]) or pagination.get("hasNext") is not True:
                return items
            token = pagination.get("nextPageToken")
            if not isinstance(token, str) or not token:
                raise _shape(endpoint, "pagination.nextPageToken")
            if token == params.get("pageToken"):
                raise HttpRetryableError(f"eToro's {endpoint} repeated a page")
            params = {"pageSize": str(CASH_PAGE), "pageToken": token}
        raise HttpRetryableError(f"eToro's {endpoint} did not end")

    async def trade_history(self, min_date: date) -> list[Item]:
        """Closed trades since `min_date`, asked in windows: the first from `min_date`, each
        next one TRADE_WINDOW_DAYS later, while it starts by today. Each window is paged until a
        short page. eToro has no end date, so a later window can repeat rows an earlier one
        gave; an exact repeat is dropped, anything else is kept for the caller to judge."""
        today = self._today()
        rows: list[Item] = []
        keys: set[str] = set()
        start = min_date
        while True:
            for row in await self._trade_window(start):
                key = _key(row)
                if key not in keys:
                    keys.add(key)
                    rows.append(row)
            start += timedelta(days=TRADE_WINDOW_DAYS)
            if start > today:
                return rows

    async def _trade_window(self, start: date) -> list[Item]:
        endpoint = "trade history"
        rows: list[Item] = []
        previous: list[Item] | None = None
        for number in range(1, MAX_PAGES + 1):
            params = {
                "minDate": start.isoformat(),
                "page": str(number),
                "pageSize": str(TRADE_PAGE),
            }
            page = _objects(await self._get("trade_history", params=params), endpoint)
            if page == previous:
                # `page` ignored: going on would only fetch the same rows.
                raise HttpRetryableError(f"eToro's {endpoint} repeated a page")
            rows.extend(page)
            if len(page) < TRADE_PAGE:
                return rows
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
        endpoint = "candles"
        suffix = f"/{_whole(instrument_id)}/candles"
        query = {
            "interval": "1d",
            "from": f"{start.isoformat()}T00:00:00Z",
            # `to` is exclusive: the day after `end`.
            "to": f"{(end + timedelta(days=1)).isoformat()}T00:00:00Z",
            "limit": str(CANDLE_LIMIT),
        }
        params = dict(query)
        items: list[Item] = []
        for _ in range(MAX_PAGES):
            body = _object(await self._get("candles", suffix=suffix, params=params), endpoint)
            items.extend(_objects(body.get("results"), endpoint, "results"))
            pagination = body.get("pagination")
            if not isinstance(pagination, dict):
                raise _shape(endpoint, "pagination")
            if pagination.get("hasNext") is not True:
                return items
            cursor = pagination.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                raise _shape(endpoint, "pagination.nextCursor")
            if cursor == params.get("cursor"):
                raise HttpRetryableError(f"eToro's {endpoint} repeated a page")
            params = {**query, "cursor": cursor}
        raise HttpRetryableError(f"eToro's {endpoint} did not end")
