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
    HttpAuthError,
    HttpError,
    HttpForbiddenError,
    HttpRetryableError,
)

from .fixtures import (
    CASH_ACCOUNT,
    OWNER_READ_SCOPES,
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
ME = "/api/v1/me"


class Env:
    """A fake clock that sleeping advances, and a transport that answers in turn. With
    `answer_me`, `/me` is answered with read scopes outside the queue and not recorded, so
    `ready()` gives a client the gate has cleared."""

    def __init__(self, *responses: httpx.Response, answer_me: bool = True) -> None:
        self.now = Decimal(1000)
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)
        self._answer_me = answer_me

    async def sleep(self, seconds: int) -> None:
        self.now += seconds

    def clock(self) -> Decimal:
        return self.now

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self._answer_me and request.url.path == ME:
            return ok(me_json())
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

    async def ready(self) -> EtoroClient:
        client = self.client()
        await client.me()
        return client

    def params(self) -> list[dict[str, str]]:
        return [dict(r.url.params) for r in self.requests]


def ok(body: Any) -> httpx.Response:
    return httpx.Response(200, text=dumps(body))


async def test_headers_and_request_id_fresh() -> None:
    env = Env(ok(me_json()), answer_me=False)
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
        ("etoro-public:real:write", True),
        # An allow-list: a scope not ending `:read` is refused, whatever it is called.
        ("etoro-public:foo:manage", True),
        ("read", True),
        ("", True),
        ("etoro-public:trade.real:read", False),
        ("etoro-public:money.transfer:read", False),
        (" Etoro-Public:Trade.Real:READ ", False),
    ],
)
def test_write_scopes(scope: str, listed: bool) -> None:
    assert write_scopes(["etoro-public:user-info:read", scope]) == ([scope] if listed else [])


def test_the_owners_read_scopes_pass() -> None:
    assert len(OWNER_READ_SCOPES) == 16
    assert write_scopes(OWNER_READ_SCOPES) == []


async def test_nothing_but_me_is_sent_before_the_gate_clears() -> None:
    env = Env(ok({"clientPortfolio": {}}), answer_me=False)
    with pytest.raises(ReadOnlyGateError):
        await env.client().pnl()
    assert env.requests == []


async def test_a_key_with_a_write_scope_never_gets_past_me() -> None:
    body = me_json([*OWNER_READ_SCOPES, "etoro-public:trade.real:write"])
    env = Env(ok(body), answer_me=False)
    client = env.client()
    # /me is returned, so its scopes can be recorded; the gate stays shut.
    assert await client.me() == body
    for call in (client.pnl, client.balances, client.closed_events):
        with pytest.raises(ReadOnlyGateError):
            await call()
    assert [r.url.path for r in env.requests] == [ME]


async def test_a_read_key_clears_the_gate_and_a_later_write_me_shuts_it() -> None:
    env = Env(
        ok(me_json(OWNER_READ_SCOPES)),
        ok({"clientPortfolio": {}}),
        ok(me_json(["etoro-public:foo:manage"])),
        answer_me=False,
    )
    client = env.client()
    await client.me()
    await client.pnl()
    await client.me()
    with pytest.raises(ReadOnlyGateError):
        await client.pnl()
    assert len(env.requests) == 3


async def test_a_403_on_me_is_a_credential_rejection() -> None:
    env = Env(httpx.Response(403, text='{"errorCode":"Forbidden"}'), answer_me=False)
    with pytest.raises(CredentialRejectedError) as raised:
        await env.client().me()
    assert isinstance(raised.value, HttpAuthError)


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
        await Env(ok(body), answer_me=False).client().me()
    client = Env(ok({**me_json(), "scopes": ["a:read", 7]}), answer_me=False).client()
    with pytest.raises(HttpError, match="scopes"):
        await client.me()
    with pytest.raises(ReadOnlyGateError):
        await client.pnl()


async def test_trade_history_is_one_window_paged_to_an_empty_page() -> None:
    min_date = TODAY - timedelta(days=364)
    full = [trade_row(10_000 + n) for n in range(100)]
    env = Env(ok(full), ok([trade_row(20_001), trade_row(20_002)]), ok([]))
    rows = await (await env.ready()).trade_history(min_date)
    assert {r.url.path for r in env.requests} == {TRADES}
    # A short page does not end the window; only the empty page does.
    assert env.params() == [
        {"minDate": min_date.isoformat(), "page": str(n), "pageSize": "100"} for n in (1, 2, 3)
    ]
    assert [row["positionId"] for row in rows] == [*range(10_000, 10_100), 20_001, 20_002]


async def test_trade_history_misses_nothing_when_etoro_caps_the_page() -> None:
    every = [trade_row(30_000 + n) for n in range(90)]
    cap = 40

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == ME:
            return ok(me_json())
        number = int(request.url.params["page"])
        return ok(every[(number - 1) * cap : number * cap])

    env = Env()
    env.handler = handler  # type: ignore[method-assign]
    rows = await (await env.ready()).trade_history(TODAY - timedelta(days=30))
    assert rows == every


async def test_trade_history_drops_exact_repeats_only() -> None:
    changed = {**trade_row(40_002), "netProfit": Decimal("9.99")}
    env = Env(
        ok([trade_row(40_001), trade_row(40_002)]),
        # The pages shifted under a new close: 40_002 again, exactly, and once with other content.
        ok([trade_row(40_002), changed, trade_row(40_003)]),
        ok([]),
    )
    rows = await (await env.ready()).trade_history(TODAY - timedelta(days=30))
    assert [row["positionId"] for row in rows] == [40_001, 40_002, 40_002, 40_003]
    assert rows[2] == changed


async def test_trade_history_a_repeated_page_fails() -> None:
    full = [trade_row(10_000 + n) for n in range(100)]
    env = Env(ok(full), ok(full))
    with pytest.raises(HttpRetryableError, match="repeated a page"):
        await (await env.ready()).trade_history(TODAY - timedelta(days=7))
    assert len(env.requests) == 2


async def test_cash_transactions_page_token() -> None:
    stored = {"80000003", "80000002"}
    env = Env(
        ok(cash_page([cash_item("80000005"), cash_item("80000004")], "tok-2")),
        ok(cash_page([cash_item("80000003"), cash_item("80000002")], "tok-3")),
        ok(cash_page([cash_item("80000001")])),
    )
    items = await (await env.ready()).cash_transactions(CASH_ACCOUNT, lambda i: i["id"] in stored)
    path = f"/api/v1/money/accounts/cash/{CASH_ACCOUNT}/transactions"
    assert [r.url.path for r in env.requests] == [path, path]
    assert env.params() == [{"pageSize": "500"}, {"pageSize": "500", "pageToken": "tok-2"}]
    assert [i["id"] for i in items] == ["80000005", "80000004", "80000003", "80000002"]


async def test_cash_transactions_end_when_there_is_no_next_page() -> None:
    env = Env(ok(cash_page([cash_item("80000002")], "tok-2")), ok(cash_page([cash_item("1")])))
    items = await (await env.ready()).cash_transactions(CASH_ACCOUNT, lambda i: False)
    assert [i["id"] for i in items] == ["80000002", "1"]
    assert len(env.requests) == 2


async def test_cash_transactions_a_repeated_token_fails() -> None:
    env = Env(
        ok(cash_page([cash_item("2")], "tok-2")),
        ok(cash_page([cash_item("1")], "tok-2")),
    )
    with pytest.raises(HttpRetryableError, match="repeated a page"):
        await (await env.ready()).cash_transactions(CASH_ACCOUNT, lambda i: False)


@pytest.mark.parametrize("account_id", ["", "../balances", "a/b", "a?b=1", "x" * 65, "%2e%2e"])
async def test_cash_account_id_must_be_a_plain_id(account_id: str) -> None:
    env = Env(ok(cash_page([])))
    with pytest.raises(ValueError, match="account id"):
        await (await env.ready()).cash_transactions(account_id, lambda i: False)
    assert env.requests == []


async def test_cloudflare_403_is_retryable() -> None:
    body = "<html><body>error code: 1010</body></html>"
    env = Env(
        httpx.Response(403, text=body, headers={"content-type": "text/html"}), answer_me=False
    )
    with pytest.raises(HttpRetryableError) as raised:
        await env.client().me()
    assert "User-Agent" in str(raised.value)
    assert not isinstance(raised.value, CredentialRejectedError)


async def test_other_403_stays_forbidden() -> None:
    env = Env(httpx.Response(403, text='{"errorCode":"Forbidden"}'))
    with pytest.raises(HttpForbiddenError):
        await (await env.ready()).balances()


async def test_order_lookup() -> None:
    env = Env(ok({"orderId": 70001, "positionExecutions": []}))
    assert await (await env.ready()).order_lookup(70001) == {
        "orderId": 70001,
        "positionExecutions": [],
    }
    assert env.requests[0].url.path == "/api/v2/trading/info/orders:lookup"
    assert env.params() == [{"orderId": "70001"}]
    # A Decimal id, as a parsed answer gives it.
    await (await env.ready()).order_lookup(Decimal("70002"))
    assert env.params()[-1] == {"orderId": "70002"}


async def test_order_lookup_404_is_none() -> None:
    assert await (await Env(httpx.Response(404, text="x")).ready()).order_lookup(70001) is None


@pytest.mark.parametrize("order_id", [0, -1, True, Decimal("1.5"), Decimal("NaN"), 10**19])
async def test_order_lookup_refuses_an_id_that_is_not_whole(order_id: Any) -> None:
    env = Env(ok({}))
    with pytest.raises(ValueError, match="whole number"):
        await (await env.ready()).order_lookup(order_id)
    assert env.requests == []


async def test_balance_history_range() -> None:
    env = Env(ok({"gcid": 1, "snapshots": []}))
    client = await env.ready()
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
    client = await env.ready()
    await client.pnl()
    await client.aggregate()
    await client.breakdown()
    env2 = Env(ok({"gcid": 1, "balances": []}))
    await (await env2.ready()).balances()
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
    items = await (await env.ready()).stockback()
    assert [i["positionId"] for i in items] == [1, 2]
    assert env.params() == [{"page": "1", "pageSize": "500"}, {"page": "2", "pageSize": "500"}]
    assert env.requests[0].url.path == "/api/v1/data/stockback/payments"

    env = Env(ok(data_page([{"closeYear": 2026, "assetType": "Stocks"}], 1, False)))
    assert await (await env.ready()).closed_events() == [{"closeYear": 2026, "assetType": "Stocks"}]
    assert env.requests[0].url.path == "/api/v1/data/positions/closed-events/history"


async def test_instruments_in_chunks_and_none_for_no_ids() -> None:
    env = Env(ok({"instrumentDisplayDatas": [{"instrumentID": 1}]}))
    client = await env.ready()
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
    client = await env.ready()
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
    items = await (await env.ready()).candles(9001, date(2026, 6, 1), date(2026, 6, 4))
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
        await (await Env(ok({"rows": []})).ready()).trade_history(TODAY)
    with pytest.raises(HttpError, match="cash transactions"):
        await (await Env(ok({"results": {}})).ready()).cash_transactions(
            CASH_ACCOUNT, lambda i: False
        )
    with pytest.raises(HttpError, match="balances"):
        await (await Env(ok([])).ready()).balances()


async def test_pools_pace_to_their_limits() -> None:
    env = Env(ok(me_json()), answer_me=False)
    client = env.client()
    for _ in range(61):
        await client.me()
    # 60 a minute in the default pool: the 61st waited for the first to leave the window.
    assert env.now >= Decimal(1060)

    env = Env(ok({"exchangeInfo": []}))
    client = await env.ready()
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


def _spec(**kinds: str) -> fields.EndpointSpec:
    spec, _ = fields._endpoint("etoro.test", "etoro_tests", "", **kinds)  # pyright: ignore[reportPrivateUsage]
    return spec


def test_fields_guard_an_own_column() -> None:
    endpoints = {**fields.ENDPOINTS, "test": _spec(text="connectionId")}
    with pytest.raises(ValueError, match="connection_id"):
        fields._columns(endpoints, fields.PERSONAL)  # pyright: ignore[reportPrivateUsage]


def test_fields_guard_an_unknown_personal_path() -> None:
    with pytest.raises(ValueError, match="personal fields not known"):
        fields._columns(fields.ENDPOINTS, fields.PERSONAL | {"nickname"})  # pyright: ignore[reportPrivateUsage]


def test_fields_guard_an_unknown_required_path() -> None:
    spec, _ = fields._endpoint("etoro.test", "etoro_tests", "missing", text="present")  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(ValueError, match="requires unknown"):
        fields._columns({"test": spec}, frozenset())  # pyright: ignore[reportPrivateUsage]


def test_fields_guard_an_array_of_objects_declared_as_a_field() -> None:
    spec, _ = fields._endpoint(  # pyright: ignore[reportPrivateUsage]
        "etoro.test",
        "etoro_tests",
        "",
        text="rows[] rows[].name",
        children={"rows[]": "etoro_test_rows"},
    )
    with pytest.raises(ValueError, match=r"rows\[\] is an array of objects"):
        fields._columns({"test": spec}, frozenset())  # pyright: ignore[reportPrivateUsage]
