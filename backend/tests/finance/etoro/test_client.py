"""The eToro client: its headers, the read-only gate's pieces, its endpoints and their paging."""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest

from lararium import __version__
from lararium.core.errors import CredentialRejectedError
from lararium.core.secrets import Secret
from lararium.modules.finance.etoro import fields
from lararium.modules.finance.etoro.client import (
    BASE,
    EtoroClient,
    ReadOnlyGateError,
    write_scopes,
)
from lararium.modules.finance.readonly_http import (
    HttpError,
    HttpForbiddenError,
    HttpRetryableError,
)

from .fixtures import (
    CASH_ACCOUNT,
    candle,
    candles_page,
    cash_item,
    cash_page,
    data_page,
    dumps,
    me_json,
    trade_row,
)

API_KEY = "a" * 32 + "-synthetic-public-key"
USER_KEY = "u" * 32 + "-synthetic-user-key"
TODAY = date(2026, 6, 15)
WALL = Decimal(int(datetime(2026, 6, 15, 12, tzinfo=UTC).timestamp()))
TRADES = "/api/v1/trading/info/trade/history"


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

    def client(self) -> EtoroClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return EtoroClient(
            http,
            Secret(API_KEY),
            Secret(USER_KEY),
            sleep=self.sleep,
            clock=self.clock,
            wall_clock=lambda: WALL,
        )

    def params(self) -> list[dict[str, str]]:
        return [dict(r.url.params) for r in self.requests]


def ok(body: Any) -> httpx.Response:
    return httpx.Response(200, text=dumps(body))


async def test_headers_and_request_id_fresh() -> None:
    env = Env(ok(me_json()))
    client = env.client()
    await client.me()
    await client.me()
    first, second = env.requests
    for request in (first, second):
        assert request.method == "GET"
        assert str(request.url) == f"{BASE}/api/v1/me"
        assert request.headers["x-api-key"] == API_KEY
        assert request.headers["x-user-key"] == USER_KEY
        assert request.headers["User-Agent"] == f"Lararium/{__version__}"
        assert uuid.UUID(request.headers["x-request-id"]).version == 4
    assert first.headers["x-request-id"] != second.headers["x-request-id"]
    # The keys are not kept as plain text on the client.
    assert API_KEY not in repr(vars(client))
    assert USER_KEY not in repr(vars(client))


@pytest.mark.parametrize(
    ("scope", "listed"),
    [
        ("trade.real:write", True),
        ("money:transfer", True),
        ("trade.real:read", False),
        # As /me returns them: under eToro's prefix.
        ("etoro-public:trade.real:write", True),
        ("etoro-public:money:transfer", True),
        ("etoro-public:money.transfer:write", True),
        ("etoro-public:real:write", True),
        ("etoro-public:trade.real:read", False),
        ("etoro-public:money.transfer:read", False),
        ("etoro-public:money.cash-transactions:read", False),
        (" Trade.Real:WRITE ", True),
    ],
)
def test_write_scopes(scope: str, listed: bool) -> None:
    assert write_scopes(["etoro-public:user-info:read", scope]) == ([scope] if listed else [])


def test_read_only_gate_error_is_a_credential_error_naming_no_scope() -> None:
    error = ReadOnlyGateError()
    assert isinstance(error, CredentialRejectedError)
    assert str(error) == (
        "This eToro key can trade or move money. Create a key with Read permission."
    )


async def test_me_needs_a_list_of_scopes() -> None:
    body = me_json()
    del body["scopes"]
    with pytest.raises(HttpError, match="scopes"):
        await Env(ok(body)).client().me()
    with pytest.raises(HttpError, match="scopes"):
        await Env(ok({**me_json(), "scopes": ["a:read", 7]})).client().me()


async def test_trade_history_windows_and_short_page() -> None:
    min_date = TODAY - timedelta(days=400)
    full = [trade_row(10_000 + n) for n in range(100)]
    env = Env(
        # Window 1 (from min_date): a full page, then a short one.
        ok(full),
        ok([trade_row(20_001), trade_row(20_002)]),
        # Window 2 (364 days on): a short page repeating a row window 1 gave.
        ok([trade_row(20_002), trade_row(20_003)]),
    )
    rows = await env.client().trade_history(min_date)
    assert {r.url.path for r in env.requests} == {TRADES}
    second = (min_date + timedelta(days=364)).isoformat()
    assert env.params() == [
        {"minDate": min_date.isoformat(), "page": "1", "pageSize": "100"},
        {"minDate": min_date.isoformat(), "page": "2", "pageSize": "100"},
        {"minDate": second, "page": "1", "pageSize": "100"},
    ]
    # Exact repeats across windows are dropped; order is kept.
    ids = [row["positionId"] for row in rows]
    assert ids == [*range(10_000, 10_100), 20_001, 20_002, 20_003]


async def test_trade_history_within_a_year_is_one_window() -> None:
    env = Env(ok([trade_row(1)]))
    rows = await env.client().trade_history(TODAY - timedelta(days=7))
    assert len(env.requests) == 1
    assert len(rows) == 1


async def test_trade_history_a_repeated_full_page_fails() -> None:
    full = [trade_row(10_000 + n) for n in range(100)]
    env = Env(ok(full), ok(full))
    with pytest.raises(HttpRetryableError, match="repeated a page"):
        await env.client().trade_history(TODAY - timedelta(days=7))
    assert len(env.requests) == 2


async def test_cash_transactions_page_token() -> None:
    stored = {"80000003", "80000002"}
    env = Env(
        ok(cash_page([cash_item("80000005"), cash_item("80000004")], "tok-2")),
        ok(cash_page([cash_item("80000003"), cash_item("80000002")], "tok-3")),
        ok(cash_page([cash_item("80000001")])),
    )
    items = await env.client().cash_transactions(CASH_ACCOUNT, lambda i: i["id"] in stored)
    path = f"/api/v1/money/accounts/cash/{CASH_ACCOUNT}/transactions"
    assert [r.url.path for r in env.requests] == [path, path]
    assert env.params() == [{"pageSize": "500"}, {"pageSize": "500", "pageToken": "tok-2"}]
    assert [i["id"] for i in items] == ["80000005", "80000004", "80000003", "80000002"]


async def test_cash_transactions_end_when_there_is_no_next_page() -> None:
    env = Env(ok(cash_page([cash_item("80000002")], "tok-2")), ok(cash_page([cash_item("1")])))
    items = await env.client().cash_transactions(CASH_ACCOUNT, lambda i: False)
    assert [i["id"] for i in items] == ["80000002", "1"]
    assert len(env.requests) == 2


async def test_cash_transactions_a_repeated_token_fails() -> None:
    env = Env(
        ok(cash_page([cash_item("2")], "tok-2")),
        ok(cash_page([cash_item("1")], "tok-2")),
    )
    with pytest.raises(HttpRetryableError, match="repeated a page"):
        await env.client().cash_transactions(CASH_ACCOUNT, lambda i: False)


@pytest.mark.parametrize("account_id", ["", "../balances", "a/b", "a?b=1", "x" * 65, "%2e%2e"])
async def test_cash_account_id_must_be_a_plain_id(account_id: str) -> None:
    env = Env(ok(cash_page([])))
    with pytest.raises(ValueError, match="account id"):
        await env.client().cash_transactions(account_id, lambda i: False)
    assert env.requests == []


async def test_cloudflare_403_is_retryable() -> None:
    body = "<html><body>error code: 1010</body></html>"
    env = Env(httpx.Response(403, text=body, headers={"content-type": "text/html"}))
    with pytest.raises(HttpRetryableError) as raised:
        await env.client().me()
    assert "User-Agent" in str(raised.value)
    assert not isinstance(raised.value, CredentialRejectedError)


async def test_other_403_stays_forbidden() -> None:
    env = Env(httpx.Response(403, text='{"errorCode":"Forbidden"}'))
    with pytest.raises(HttpForbiddenError):
        await env.client().balances()


async def test_order_lookup() -> None:
    env = Env(ok({"orderId": 70001, "positionExecutions": []}))
    assert await env.client().order_lookup(70001) == {"orderId": 70001, "positionExecutions": []}
    assert env.requests[0].url.path == "/api/v2/trading/info/orders:lookup"
    assert env.params() == [{"orderId": "70001"}]
    # A Decimal id, as a parsed answer gives it.
    await env.client().order_lookup(Decimal("70002"))
    assert env.params()[-1] == {"orderId": "70002"}


async def test_order_lookup_404_is_none() -> None:
    assert await Env(httpx.Response(404, text="x")).client().order_lookup(70001) is None


@pytest.mark.parametrize("order_id", [0, -1, True, Decimal("1.5"), Decimal("NaN"), 10**19])
async def test_order_lookup_refuses_an_id_that_is_not_whole(order_id: Any) -> None:
    env = Env(ok({}))
    with pytest.raises(ValueError, match="whole number"):
        await env.client().order_lookup(order_id)
    assert env.requests == []


async def test_balance_history_range() -> None:
    env = Env(ok({"gcid": 1, "snapshots": []}))
    client = env.client()
    await client.balance_history(TODAY - timedelta(days=364), TODAY)
    assert env.requests[0].url.path == "/api/v1/balances/history"
    assert env.params() == [{"fromDate": "2025-06-16", "toDate": "2026-06-15"}]
    with pytest.raises(ValueError, match="365 days"):
        await client.balance_history(TODAY - timedelta(days=365), TODAY)
    with pytest.raises(ValueError, match="365 days"):
        await client.balance_history(TODAY, TODAY - timedelta(days=1))
    assert len(env.requests) == 1


async def test_snapshot_endpoints_and_their_queries() -> None:
    env = Env(ok({"clientPortfolio": {}}), ok({"accountTotals": {}}), ok({"instruments": []}))
    client = env.client()
    await client.pnl()
    await client.aggregate()
    await client.breakdown()
    env2 = Env(ok({"gcid": 1, "balances": []}))
    await env2.client().balances()
    assert [r.url.path for r in env.requests] == [
        "/api/v1/trading/info/real/pnl",
        "/api/v1/trading/info/aggregate-portfolio",
        "/api/v2/trading/info/instrument-breakdown",
    ]
    assert env.params()[2] == {
        "positionLevel": "Normal",
        "orderLevel": "Normal",
        "mirrorLevel": "Details",
    }
    assert env2.requests[0].url.path == "/api/v1/balances"
    assert env2.params() == [
        {"includeZeroBalances": "true", "includeSubAccounts": "true", "expand": "equityDetails"}
    ]


async def test_paged_data_endpoints() -> None:
    env = Env(
        ok(data_page([{"channel": "stockback", "positionId": 1}], 1, True)),
        ok(data_page([{"channel": "cashback", "positionId": 2}], 2, False)),
    )
    items = await env.client().stockback()
    assert [i["positionId"] for i in items] == [1, 2]
    assert env.params() == [{"page": "1", "pageSize": "500"}, {"page": "2", "pageSize": "500"}]
    assert env.requests[0].url.path == "/api/v1/data/stockback/payments"

    env = Env(ok(data_page([{"closeYear": 2026, "assetType": "Stocks"}], 1, False)))
    assert await env.client().closed_events() == [{"closeYear": 2026, "assetType": "Stocks"}]
    assert env.requests[0].url.path == "/api/v1/data/positions/closed-events/history"


async def test_instruments_in_chunks_and_none_for_no_ids() -> None:
    env = Env(ok({"instrumentDisplayDatas": [{"instrumentID": 1}]}))
    client = env.client()
    assert await client.instruments([]) == []
    assert env.requests == []
    items = await client.instruments(list(range(1, 61)))
    assert len(items) == 2
    assert [r.url.path for r in env.requests] == ["/api/v1/market-data/instruments"] * 2
    assert env.params() == [
        {"instrumentIds": ",".join(str(n) for n in range(1, 51))},
        {"instrumentIds": ",".join(str(n) for n in range(51, 61))},
    ]


async def test_lookup_tables() -> None:
    env = Env(
        ok({"instrumentTypes": [{"instrumentTypeID": 5}]}),
        ok({"exchangeInfo": [{"exchangeID": 4}]}),
    )
    client = env.client()
    assert await client.instrument_types() == [{"instrumentTypeID": 5}]
    assert await client.exchanges() == [{"exchangeID": 4}]
    assert [r.url.path for r in env.requests] == [
        "/api/v1/market-data/instrument-types",
        "/api/v1/market-data/exchanges",
    ]


async def test_candles_follow_the_cursor() -> None:
    env = Env(
        ok(candles_page([candle(date(2026, 6, 1))], "cur-2")),
        ok(candles_page([candle(date(2026, 6, 2))])),
    )
    items = await env.client().candles(9001, date(2026, 6, 1), date(2026, 6, 4))
    assert [i["time"] for i in items] == ["2026-06-01T00:00:00Z", "2026-06-02T00:00:00Z"]
    assert [r.url.path for r in env.requests] == ["/api/v1/data/instruments/9001/candles"] * 2
    query = {
        "interval": "1d",
        "from": "2026-06-01T00:00:00Z",
        "to": "2026-06-05T00:00:00Z",
        "limit": "2000",
    }
    assert env.params() == [query, {**query, "cursor": "cur-2"}]


async def test_wrong_shape_refused() -> None:
    with pytest.raises(HttpError, match="trade history"):
        await Env(ok({"rows": []})).client().trade_history(TODAY)
    with pytest.raises(HttpError, match="cash transactions"):
        await Env(ok({"results": {}})).client().cash_transactions(CASH_ACCOUNT, lambda i: False)
    with pytest.raises(HttpError, match="balances"):
        await Env(ok([])).client().balances()


async def test_pools_pace_to_their_limits() -> None:
    env = Env(ok(me_json()))
    client = env.client()
    for _ in range(61):
        await client.me()
    # 60 a minute in the default pool: the 61st waited for the first to leave the window.
    assert env.now >= Decimal(1060)

    env = Env(ok({"exchangeInfo": []}))
    client = env.client()
    for _ in range(120):
        await client.exchanges()
    # Market data allows 120 a minute: nothing waited.
    assert env.now == Decimal(1000)


def test_personal_fields_are_known_paths() -> None:
    assert fields.PERSONAL == {
        "firstName",
        "middleName",
        "lastName",
        "dateOfBirth",
        "gender",
        "avatarUrl",
        "username",
        "counterparty.name",
        "bankTransferTransactionDetails.bankIdentifier[].value",
        "bankTransferTransactionDetails.paymentReference",
    }
    known = set().union(*(spec.known for spec in fields.ENDPOINTS.values()))
    assert fields.PERSONAL <= known


def test_fields_columns_and_child_tables() -> None:
    order = fields.COLUMNS["order_lookup"]
    executions = order["etoro_position_executions"]
    assert executions["positionExecutions[].openingData.avgPrice"] == "opening_data_avg_price"
    assert order["etoro_orders"]["status.errorMessage"] == "status_error_message"
    # A list of plain values is a child table of one column.
    assert fields.COLUMNS["me"]["etoro_user_scopes"] == {"scopes[]": "scope"}
    cash = fields.COLUMNS["cash_transactions"]
    assert cash["etoro_cash_bank_identifiers"] == {
        "bankTransferTransactionDetails.bankIdentifier[].name": "name",
        "bankTransferTransactionDetails.bankIdentifier[].value": "value",
    }
    assert fields.KINDS["breakdown"]["instruments[].positions[].openTime"] == "timestamp"
    assert fields.KINDS["cash_transactions"]["amount"] == "numeric"
    assert fields.KINDS["balance_history"]["snapshots[].date"] == "date"
    # Every known path has a column, and every endpoint's client method exists.
    for name, spec in fields.ENDPOINTS.items():
        mapped = {path for table in fields.COLUMNS[name].values() for path in table}
        assert mapped == spec.known
        assert spec.required <= spec.known
        assert callable(getattr(EtoroClient, name))
