"""The eToro sync and statement ingest: the read-only gate first, every item stored as a raw
record, the trade history walked back in windows, and statements checked before storing."""

import base64
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from lararium.config import Settings
from lararium.core.actors import Actor
from lararium.core.commands import CommandContext
from lararium.core.connections import (
    ConnectionRecord,
    create_connection,
    get_connection,
    store_credential,
)
from lararium.core.connectors import SyncContext
from lararium.core.documents import LocalDocumentStore
from lararium.core.json_types import JSONValue
from lararium.modules import registry
from lararium.modules.finance.etoro import sync as etoro_sync
from lararium.modules.finance.etoro.client import EtoroClient, ReadOnlyGateError
from lararium.modules.finance.etoro.normalize import NORMALIZER_VERSION, NormalizeReport
from lararium.modules.finance.etoro.sync import (
    EtoroConnector,
    IngestResult,
    StatementRejected,
    StatementTooEarly,
    ingest_statement,
)
from lararium.worker import jobs

from .fixtures import (
    CASH_ACCOUNT,
    READ_SCOPES,
    candle,
    candles_page,
    cash_item,
    cash_page,
    data_page,
    dumps,
    me_json,
    trade_row,
)
from .statement_fixtures import USERNAME, activity_row, build_statement, closed_row, summary

KEY = bytes(range(32))
API_KEY = "a" * 32 + "-synthetic-public-key"
USER_KEY = "u" * 32 + "-synthetic-user-key"
CONNECTOR = Actor("connector", "etoro")
SETTINGS = Settings(
    database_url="postgresql+psycopg://x@localhost/x",
    auth_mode="dev",
    master_key=SecretStr(base64.b64encode(KEY).decode()),
)
TRADING = "12345678"
MONEYFARM = "23456789"
INSTRUMENT = 9001
GATE = "This eToro key can trade or move money. Create a key with Read permission."

ME = "/api/v1/me"
PNL = "/api/v1/trading/info/real/pnl"
AGGREGATE = "/api/v1/trading/info/aggregate-portfolio"
BREAKDOWN = "/api/v2/trading/info/instrument-breakdown"
BALANCES = "/api/v1/balances"
HISTORY = "/api/v1/balances/history"
CASH = f"/api/v1/money/accounts/cash/{CASH_ACCOUNT}/transactions"
TRADES = "/api/v1/trading/info/trade/history"
ORDERS = "/api/v2/trading/info/orders:lookup"
CLOSED_EVENTS = "/api/v1/data/positions/closed-events/history"
STOCKBACK = "/api/v1/data/stockback/payments"
INSTRUMENTS = "/api/v1/market-data/instruments"
TYPES = "/api/v1/market-data/instrument-types"
EXCHANGES = "/api/v1/market-data/exchanges"
CANDLES = f"/api/v1/data/instruments/{INSTRUMENT}/candles"
DAILY = {CLOSED_EVENTS, STOCKBACK, INSTRUMENTS, TYPES, EXCHANGES, CANDLES}


def today() -> date:
    return datetime.now(UTC).date()


def _statement_moment(day: date, at: str = "09:15:00") -> str:
    return f"{day.strftime('%d/%m/%Y')} {at}"


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> None:
        self.messages.append(text)


def balances_json() -> dict[str, Any]:
    def account(account_id: str, kind: str, currency: str, sub: str | None) -> dict[str, Any]:
        return {
            "accountId": account_id,
            "accountType": kind,
            "subType": sub,
            "balance": Decimal("100.5"),
            "currency": currency,
            "displayBalance": Decimal("100.5"),
            "displayCurrency": "USD",
            "exchangeRate": Decimal("1"),
            "equityDetails": {"available": Decimal("100.5")},
        }

    return {
        "gcid": 90000001,
        "displayCurrency": "USD",
        "totalBalance": Decimal("301.5"),
        "balances": [
            account(TRADING, "Trading", "USD", None),
            account(CASH_ACCOUNT, "Cash", "GBP", None),
            account(MONEYFARM, "MoneyFarm", "GBP", "ISA"),
        ],
    }


def pnl_json(order_id: int = 4000000101) -> dict[str, Any]:
    return {
        "clientPortfolio": {
            "credit": Decimal("250.25"),
            "positions": [
                {
                    "positionID": 3000000101,
                    "instrumentID": INSTRUMENT,
                    "orderID": order_id,
                    "openDateTime": f"{(today() - timedelta(days=20)).isoformat()}T09:00:00Z",
                    "units": Decimal("2"),
                    "amount": Decimal("200.00"),
                }
            ],
        }
    }


class FakeEtoro:
    """eToro's API: trade history filtered by `minDate`, the rest fixed bodies."""

    def __init__(self) -> None:
        self.me = me_json()
        self.balances = balances_json()
        self.pnl = pnl_json()
        self.aggregate = {
            "accountCurrency": "USD",
            "accountTotals": {"accountTotalValue": Decimal("450.75")},
            "instrumentAggregates": [{"instrumentId": INSTRUMENT, "netUnits": Decimal("2")}],
        }
        self.breakdown = {
            "accountCurrency": "USD",
            "instruments": [{"instrumentId": INSTRUMENT, "symbol": "ZZQA", "positions": []}],
        }
        self.trades = [trade_row(3000000001, today() - timedelta(days=10))]
        self.cash = [cash_page([cash_item("80000002"), cash_item("80000001")])]
        self.orders: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []
        self.now = Decimal(1000)

    async def sleep(self, seconds: int) -> None:
        self.now += seconds

    def clock(self) -> Decimal:
        return self.now

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, params = request.url.path, request.url.params
        if path == TRADES:
            return self._ok(self._trades(params))
        if path == CASH:
            token = params.get("pageToken")
            index = 0 if token is None else int(token)
            page = dict(self.cash[index])
            more = index + 1 < len(self.cash)
            page["pagination"] = {
                "pageSize": 500,
                "nextPageToken": str(index + 1) if more else None,
                "hasNext": more,
            }
            return self._ok(page)
        if path == ORDERS:
            order_id = params["orderId"]
            body = self.orders.get(
                order_id, {"orderId": Decimal(order_id), "positionExecutions": []}
            )
            return self._ok(body)
        if path == HISTORY:
            return self._ok(
                {
                    "gcid": 90000001,
                    "fromDate": params["fromDate"],
                    "toDate": params["toDate"],
                    "snapshots": [
                        {
                            "date": params["toDate"],
                            "totalCash": Decimal("1.5"),
                            "accountSnapshots": [
                                {
                                    "accountId": TRADING,
                                    "accountType": "Trading",
                                    "currency": "USD",
                                    "total": Decimal("450.75"),
                                }
                            ],
                        }
                    ],
                }
            )
        if path == INSTRUMENTS:
            ids = params["instrumentIds"].split(",")
            return self._ok({"instrumentDisplayDatas": [{"instrumentID": int(i)} for i in ids]})
        if path == CANDLES:
            return self._ok(candles_page([candle(today() - timedelta(days=1))]))
        bodies: dict[str, Any] = {
            ME: self.me,
            PNL: self.pnl,
            AGGREGATE: self.aggregate,
            BREAKDOWN: self.breakdown,
            BALANCES: self.balances,
            CLOSED_EVENTS: data_page([{"closeYear": 2026, "assetType": "Stocks"}], 1, False),
            STOCKBACK: data_page([], 1, False),
            TYPES: {"instrumentTypes": [{"instrumentTypeID": 5}]},
            EXCHANGES: {"exchangeInfo": [{"exchangeID": 4}]},
        }
        if path not in bodies:
            raise AssertionError(f"unexpected request to {path}")
        return self._ok(bodies[path])

    def _trades(self, params: httpx.QueryParams) -> list[dict[str, Any]]:
        min_date = date.fromisoformat(params["minDate"])
        rows = [r for r in self.trades if date.fromisoformat(r["closeTimestamp"][:10]) >= min_date]
        size = int(params["pageSize"])
        start = (int(params["page"]) - 1) * size
        return rows[start : start + size]

    @staticmethod
    def _ok(body: Any) -> httpx.Response:
        return httpx.Response(200, text=dumps(body))

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def windows(self) -> list[date]:
        """The minDate of each trade-history window asked (its first page)."""
        return [
            date.fromisoformat(r.url.params["minDate"])
            for r in self.requests
            if r.url.path == TRADES and r.url.params["page"] == "1"
        ]

    def looked_up(self) -> list[str]:
        return [r.url.params["orderId"] for r in self.requests if r.url.path == ORDERS]


@pytest.fixture
def api(monkeypatch) -> FakeEtoro:
    fake = FakeEtoro()
    monkeypatch.setattr(
        etoro_sync,
        "EtoroClient",
        partial(EtoroClient, sleep=fake.sleep, clock=fake.clock, wall_clock=fake.clock),
    )
    return fake


async def _connection(run, *, cursor: JSONValue = None, credentials: bool = True):
    async def setup(ctx: CommandContext) -> ConnectionRecord:
        connection = await create_connection(
            ctx, connector="etoro", display_name="eToro", config={}
        )
        if credentials:
            await store_credential(ctx, KEY, connection.id, "public_api_key", API_KEY, None)
            await store_credential(ctx, KEY, connection.id, "user_key", USER_KEY, None)
        await ctx.session.execute(
            text(
                "update core.connections set status = 'healthy', cursor = cast(:c as jsonb)"
                " where id = :id"
            ),
            {"c": json.dumps(cursor), "id": connection.id},
        )
        return connection

    return await run(setup)


async def _set_cursor(session, connection, cursor: JSONValue) -> None:
    await session.execute(
        text("update core.connections set cursor = cast(:c as jsonb) where id = :id"),
        {"c": json.dumps(cursor), "id": connection.id},
    )


async def _ctx(session, connection, http: httpx.AsyncClient, tmp_path=None) -> SyncContext:
    return SyncContext(
        command=CommandContext(session, CONNECTOR, uuid.uuid4()),
        connection=await get_connection(session, connection.id),
        sync_id=uuid.uuid4(),
        master_key=KEY,
        http=http,
        notifier=RecordingNotifier(),
        session_factory=async_sessionmaker(bind=session.bind),
        documents=LocalDocumentStore(tmp_path) if tmp_path is not None else None,
    )


async def _sync(session, connection, api: FakeEtoro) -> dict[str, Any]:
    """One sync, called directly, as the worker would in its command."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(api.handler)) as http:
        cursor = await EtoroConnector().sync(await _ctx(session, connection, http))
    await _set_cursor(session, connection, cursor)
    assert isinstance(cursor, dict)
    return cursor


async def _run_sync(session, connection, api: FakeEtoro, notifier=None) -> ConnectionRecord:
    """One sync through the worker job, which records its outcome."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(api.handler)) as http:
        await jobs.run_sync(
            session,
            connection.id,
            settings=SETTINGS,
            http=http,
            notifier=notifier or RecordingNotifier(),
            session_factory=async_sessionmaker(bind=session.bind),
        )
    return await get_connection(session, connection.id)


async def _ingest(session, connection, body: bytes, tmp_path, notifier=None) -> IngestResult:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500))
    ) as http:
        ctx = await _ctx(session, connection, http, tmp_path)
        if notifier is not None:
            ctx.notifier = notifier
        return await ingest_statement(ctx, body, "statement.xlsx")


async def _records(session, connection, record_type: str | None = None) -> list[Any]:
    sql = (
        "select id, record_type, request, content from core.raw_records where connection_id = :c"
        + (" and record_type = :t" if record_type else "")
        + " order by record_type, fetched_at, id"
    )
    params: dict[str, Any] = {"c": connection.id}
    if record_type:
        params["t"] = record_type
    return list((await session.execute(text(sql), params)).all())


def _counts(rows: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.record_type] = counts.get(row.record_type, 0) + 1
    return counts


def _statement(**overrides: str) -> bytes:
    created = today() - timedelta(days=30)
    fields = {
        "start": _statement_moment(created, "00:00:00"),
        "end": _statement_moment(today(), "23:59:59"),
        "created": _statement_moment(created),
        **overrides,
    }
    return build_statement(
        summary(**fields),
        [activity_row(_statement_moment(created + timedelta(days=1)), "Deposit", "100.00")],
        [closed_row()],
    )


# The read-only gate (design §5.1, Decision 13).


async def test_write_scope_stops_sync(session, run, api) -> None:
    api.me = me_json([*READ_SCOPES, "etoro-public:trade.real:write"])
    connection = await _connection(run)
    notifier = RecordingNotifier()
    after = await _run_sync(session, connection, api, notifier)
    assert after.status == "needs_reauth"
    assert after.last_error == GATE
    # Nothing but /me was asked, and the owner is told, naming no scope.
    assert api.paths() == [ME]
    [message] = notifier.messages
    assert GATE in message and "write" not in message.replace("Create a key", "")
    for secret in (API_KEY, USER_KEY):
        assert secret not in message


async def test_write_scope_keeps_only_the_scopes_record(session, run, api) -> None:
    api.me = me_json([*READ_SCOPES, "etoro-public:money:transfer"])
    connection = await _connection(run)
    with pytest.raises(ReadOnlyGateError, match="Create a key with Read permission"):
        await _sync(session, connection, api)
    assert api.paths() == [ME]
    # The scopes the refused key held are kept, committed on their own, and nothing else: not
    # even the rest of its /me, which may be another user's.
    [record] = await _records(session, connection)
    assert record.record_type == "etoro.refused_key"
    assert record.content == {"scopes": [*READ_SCOPES, "etoro-public:money:transfer"]}


async def test_a_failure_recording_the_refusal_does_not_hide_it(
    session, run, api, monkeypatch
) -> None:
    async def broken(self: Any, scopes: list[str]) -> None:
        raise RuntimeError("the side session failed")

    monkeypatch.setattr(etoro_sync._Run, "keep_refused", broken)  # pyright: ignore[reportPrivateUsage]
    api.me = me_json([*READ_SCOPES, "etoro-public:trade.real:write"])
    connection = await _connection(run)
    with pytest.raises(ReadOnlyGateError, match="Create a key with Read permission"):
        await _sync(session, connection, api)
    assert api.paths() == [ME]


async def test_a_key_reporting_no_scopes_needs_reauth(session, run, api) -> None:
    api.me = me_json([])
    connection = await _connection(run)
    after = await _run_sync(session, connection, api)
    assert after.status == "needs_reauth"
    assert api.paths() == [ME]


async def test_another_users_key_refused(session, run, api) -> None:
    connection = await _connection(run, cursor={"gcid": "90000999", "real_cid": "90000002"})
    after = await _run_sync(session, connection, api)
    assert after.status == "needs_reauth"
    assert after.last_error == "this key belongs to another eToro user"
    assert api.paths() == [ME]


async def test_missing_credentials_are_rejected(session, run, api) -> None:
    connection = await _connection(run, credentials=False)
    after = await _run_sync(session, connection, api)
    assert after.status == "needs_reauth"
    assert api.requests == []


# The sync (design §5.2).


async def test_first_sync_fetches_every_endpoint_once(session, run, api) -> None:
    connection = await _connection(run)
    before = datetime.now(UTC)
    cursor = await _sync(session, connection, api)

    paths = api.paths()
    for path in (ME, BALANCES, PNL, AGGREGATE, BREAKDOWN, CASH, HISTORY, *DAILY):
        assert paths.count(path) == 1, path
    assert paths[0] == ME
    # One order per open position and closed trade.
    assert sorted(api.looked_up()) == ["3000000501", "4000000101"]
    assert _counts(await _records(session, connection)) == {
        "etoro.me": 1,
        "etoro.balances": 1,
        "etoro.pnl": 1,
        "etoro.aggregate": 1,
        "etoro.breakdown": 1,
        "etoro.cash_transaction": 2,
        "etoro.trade": 1,
        "etoro.order": 2,
        "etoro.balance_history": 1,
        "etoro.closed_events": 1,
        "etoro.instrument": 1,
        "etoro.instrument_type": 1,
        "etoro.exchange": 1,
        "etoro.candle": 1,
    }
    # Every number as its exact text; a candle names the instrument it was asked for.
    [trade] = await _records(session, connection, "etoro.trade")
    assert trade.content["netProfit"] == "1.25"
    assert trade.request == {
        "endpoint": TRADES,
        "params": {"minDate": (today() - timedelta(days=364)).isoformat()},
    }
    [day] = await _records(session, connection, "etoro.candle")
    assert day.content["instrumentId"] == str(INSTRUMENT)
    assert day.content["close"] == "100.40"
    assert day.request["endpoint"] == CANDLES

    stored = json.dumps([[r.request, r.content] for r in await _records(session, connection)])
    assert API_KEY not in stored and USER_KEY not in stored

    # Reports are sighted, so they are dated by the sync that saw them.
    sighted = await session.execute(
        text(
            "select r.record_type from finance.report_sightings s"
            " join core.raw_records r on r.id = s.raw_record_id where s.connection_id = :c"
        ),
        {"c": connection.id},
    )
    assert sorted(r.record_type for r in sighted) == [
        "etoro.aggregate",
        "etoro.balance_history",
        "etoro.balances",
        "etoro.breakdown",
        "etoro.me",
        "etoro.pnl",
    ]

    assert cursor["gcid"] == "90000001" and cursor["real_cid"] == "90000002"
    assert cursor["trade_history_to"] == (today() - timedelta(days=10)).isoformat()
    assert cursor["balance_history_to"] == today().isoformat()
    assert cursor["seen_cash"] == {CASH_ACCOUNT: "80000002"}
    assert sorted(cursor["looked_up"]) == ["3000000501", "4000000101"]
    assert datetime.fromisoformat(cursor["daily_at"]) >= before
    assert (cursor["flags"], cursor["notified"]) == ({}, [])


async def test_trade_history_windows_back_until_nothing_new(session, run, api) -> None:
    api.trades = [
        trade_row(3000000001, today() - timedelta(days=10)),
        trade_row(3000000002, today() - timedelta(days=400)),
    ]
    connection = await _connection(run)
    cursor = await _sync(session, connection, api)
    # Each window adds a position; the third adds none, and the walk stops.
    assert api.windows() == [today() - timedelta(days=364 * k) for k in (1, 2, 3)]
    assert len(await _records(session, connection, "etoro.trade")) == 2
    assert cursor["trade_history_to"] == (today() - timedelta(days=10)).isoformat()


async def test_trade_history_windows_back_to_creation(session, run, api, tmp_path) -> None:
    api.me["username"] = USERNAME
    connection = await _connection(run)
    await _sync(session, connection, api)
    created = today() - timedelta(days=900)
    await _ingest(
        session,
        connection,
        _statement(
            start=_statement_moment(created, "00:00:00"), created=_statement_moment(created)
        ),
        tmp_path,
    )
    # A trade a year and a half back, after a year with none: a window that adds nothing
    # does not stop a walk that knows the opening.
    api.trades = [
        trade_row(3000000001, today() - timedelta(days=10)),
        trade_row(3000000003, today() - timedelta(days=800)),
    ]
    cursor = (await get_connection(session, connection.id)).cursor
    assert isinstance(cursor, dict)
    await _set_cursor(session, connection, {**cursor, "trade_history_to": None})
    api.requests.clear()
    await _sync(session, connection, api)
    # The third window starts before Date Created: it is the last.
    assert api.windows() == [today() - timedelta(days=364 * k) for k in (1, 2, 3)]
    assert len(await _records(session, connection, "etoro.trade")) == 2


async def test_later_sync_asks_one_window_with_overlap(session, run, api) -> None:
    connection = await _connection(run)
    await _sync(session, connection, api)
    api.trades.append(trade_row(3000000004, today() - timedelta(days=2)))
    api.requests.clear()
    cursor = await _sync(session, connection, api)
    # From the latest stored close less 7 days.
    assert api.windows() == [today() - timedelta(days=17)]
    assert cursor["trade_history_to"] == (today() - timedelta(days=2)).isoformat()
    assert len(await _records(session, connection, "etoro.trade")) == 2


async def test_a_long_outage_is_asked_from_the_latest_close(session, run, api) -> None:
    connection = await _connection(run)
    await _sync(session, connection, api)
    latest = today() - timedelta(days=400)
    cursor = (await get_connection(session, connection.id)).cursor
    assert isinstance(cursor, dict)
    await _set_cursor(session, connection, {**cursor, "trade_history_to": latest.isoformat()})
    api.requests.clear()
    await _sync(session, connection, api)
    # No cap at a year: what closed during the outage is asked for.
    assert api.windows() == [latest - timedelta(days=7)]


async def test_balance_history_overlap_7_days(session, run, api) -> None:
    connection = await _connection(run)
    await _sync(session, connection, api)
    [first] = [r for r in api.requests if r.url.path == HISTORY]
    assert dict(first.url.params) == {
        "fromDate": (today() - timedelta(days=364)).isoformat(),
        "toDate": today().isoformat(),
    }
    last = today() - timedelta(days=3)
    cursor = (await get_connection(session, connection.id)).cursor
    assert isinstance(cursor, dict)
    await _set_cursor(session, connection, {**cursor, "balance_history_to": last.isoformat()})
    api.requests.clear()
    after = await _sync(session, connection, api)
    [second] = [r for r in api.requests if r.url.path == HISTORY]
    assert dict(second.url.params) == {
        "fromDate": (last - timedelta(days=7)).isoformat(),
        "toDate": today().isoformat(),
    }
    assert after["balance_history_to"] == today().isoformat()


async def test_orders_looked_up_once(session, run, api) -> None:
    connection = await _connection(run)
    await _sync(session, connection, api)
    assert sorted(api.looked_up()) == ["3000000501", "4000000101"]
    api.requests.clear()
    await _sync(session, connection, api)
    assert api.looked_up() == []
    # A new open position's order is the only one asked.
    api.pnl = pnl_json(order_id=4000000102)
    api.requests.clear()
    cursor = await _sync(session, connection, api)
    assert api.looked_up() == ["4000000102"]
    assert sorted(cursor["looked_up"]) == ["3000000501", "4000000101", "4000000102"]


async def test_cash_transactions_stop_at_the_newest_seen(session, run, api) -> None:
    connection = await _connection(run)
    api.cash = [
        cash_page([cash_item("80000004"), cash_item("80000003")]),
        cash_page([cash_item("80000002"), cash_item("80000001")]),
    ]
    await _sync(session, connection, api)
    assert api.paths().count(CASH) == 2
    api.cash.insert(0, cash_page([cash_item("80000005")]))
    api.requests.clear()
    cursor = await _sync(session, connection, api)
    # The new page, then the page holding the newest seen; nothing older.
    assert api.paths().count(CASH) == 2
    assert cursor["seen_cash"] == {CASH_ACCOUNT: "80000005"}
    assert len(await _records(session, connection, "etoro.cash_transaction")) == 5


async def test_daily_parts_once_a_day(session, run, api) -> None:
    connection = await _connection(run)
    await _sync(session, connection, api)
    api.requests.clear()
    await _sync(session, connection, api)
    assert not DAILY & set(api.paths())
    cursor = (await get_connection(session, connection.id)).cursor
    assert isinstance(cursor, dict)
    stale = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    await _set_cursor(session, connection, {**cursor, "daily_at": stale})
    api.requests.clear()
    await _sync(session, connection, api)
    assert DAILY <= set(api.paths())
    # Candles go on from the last day fetched, less a week.
    [request] = [r for r in api.requests if r.url.path == CANDLES]
    assert request.url.params["from"] == f"{(today() - timedelta(days=7)).isoformat()}T00:00:00Z"


async def test_normalize_is_called_with_the_records(session, run, api, monkeypatch) -> None:
    called: list[int] = []

    async def spy(ctx: SyncContext, raw_record_ids: list[uuid.UUID]) -> NormalizeReport:
        called.append(len(raw_record_ids))
        return NormalizeReport(flagged={"pnl.someNewField"})

    monkeypatch.setattr(etoro_sync, "normalize", spy)
    connection = await _connection(run)
    notifier = RecordingNotifier()
    after = await _run_sync(session, connection, api, notifier)
    assert called == [len(await _records(session, connection))]
    assert NORMALIZER_VERSION == "etoro-norm-1"
    assert after.warnings == ["eToro flagged: pnl.someNewField"]
    assert notifier.messages == ["eToro: eToro flagged: pnl.someNewField"]
    await _run_sync(session, connection, api, notifier)
    assert len(notifier.messages) == 1


def test_connector_identity_and_registration() -> None:
    connector = EtoroConnector()
    assert (connector.name, connector.version) == ("etoro", "etoro-sync-1")
    assert connector.capabilities == {"balances", "holdings", "transactions"}
    assert isinstance(registry.connector("etoro"), EtoroConnector)


# The statement upload (design §5.3).


async def test_statement_stored_as_document_and_rows(
    session, run, api, tmp_path, monkeypatch
) -> None:
    called: list[list[uuid.UUID]] = []

    async def spy(ctx: SyncContext, raw_record_ids: list[uuid.UUID]) -> NormalizeReport:
        called.append(list(raw_record_ids))
        return NormalizeReport()

    api.me["username"] = USERNAME
    connection = await _connection(run)
    await _sync(session, connection, api)
    monkeypatch.setattr(etoro_sync, "normalize", spy)
    result = await _ingest(session, connection, _statement(), tmp_path)

    rows = await _records(session, connection)
    statement_rows = [r for r in rows if r.record_type.startswith("etoro.statement.")]
    assert called == [result.raw_record_ids]
    assert sorted(r.id for r in statement_rows) == sorted(result.raw_record_ids)
    counts = _counts(statement_rows)
    assert counts["etoro.statement.closed_position"] == 1
    assert counts["etoro.statement.account_activity"] == 1
    [closed] = [r for r in statement_rows if r.record_type == "etoro.statement.closed_position"]
    # The period is the upload's: a row two statements share is one record.
    assert set(closed.content) == {"cells", "key"}
    assert closed.content["cells"]["ISIN"] == closed_row()["ISIN"]
    assert len(closed.content["key"]) == 32
    created = today() - timedelta(days=30)
    assert closed.request["period"] == {
        "from": datetime.combine(created, datetime.min.time(), UTC).isoformat(),
        "to": datetime.combine(
            today(), datetime.max.time().replace(microsecond=0), UTC
        ).isoformat(),
    }
    document = (
        await session.execute(
            text("select source, original_filename from core.documents where sha256 = :s"),
            {"s": closed.request["document_sha256"]},
        )
    ).one()
    assert tuple(document) == ("finance.etoro", "statement.xlsx")
    assert closed.request["upload"] == "statement.xlsx"


async def test_statement_other_user_refused(session, run, api, tmp_path) -> None:
    connection = await _connection(run)
    await _sync(session, connection, api)
    # The fixtures' /me username is not the statement's.
    with pytest.raises(StatementRejected) as refused:
        await _ingest(session, connection, _statement(), tmp_path)
    assert USERNAME not in str(refused.value) and "synthetic-user" not in str(refused.value)
    assert not [r for r in await _records(session, connection) if "statement" in r.record_type]
    assert (await session.scalar(text("select count(*) from core.documents"))) == 0


async def test_statement_other_currency_refused(session, run, api, tmp_path) -> None:
    api.me["username"] = USERNAME
    connection = await _connection(run)
    await _sync(session, connection, api)
    with pytest.raises(StatementRejected, match="currency"):
        await _ingest(session, connection, _statement(currency="GBP"), tmp_path)


async def test_statement_before_the_first_sync_refused(session, run, api, tmp_path) -> None:
    connection = await _connection(run)
    # Its own type: the upload route answers it 409, not 422 (design §5.3).
    with pytest.raises(StatementTooEarly, match="first sync"):
        await _ingest(session, connection, _statement(), tmp_path)


async def test_first_statement_must_cover_creation(session, run, api, tmp_path) -> None:
    api.me["username"] = USERNAME
    connection = await _connection(run)
    await _sync(session, connection, api)
    created = today() - timedelta(days=30)
    late = _statement(start=_statement_moment(created + timedelta(days=1), "00:00:00"))
    with pytest.raises(StatementRejected) as refused:
        await _ingest(session, connection, late, tmp_path)
    # The date it must start by, the day only (R27).
    assert str(refused.value) == (
        f"The first eToro statement must start on or before {created.isoformat()}."
    )
    assert not [r for r in await _records(session, connection) if "statement" in r.record_type]
    # Starting the moment the account was created is covering it.
    await _ingest(session, connection, _statement(start=_statement_moment(created)), tmp_path)


async def test_later_partial_statement_accepted(session, run, api, tmp_path) -> None:
    api.me["username"] = USERNAME
    connection = await _connection(run)
    await _sync(session, connection, api)
    await _ingest(session, connection, _statement(), tmp_path)
    later = _statement(start=_statement_moment(today() - timedelta(days=5), "00:00:00"))
    result = await _ingest(session, connection, later, tmp_path)
    assert result.raw_record_ids


def _rows_of(rows: list[Any]) -> list[Any]:
    """The table sheets' records: those a statement shares with another covering its rows."""
    kinds = ("etoro.statement.account_activity", "etoro.statement.closed_position")
    return [r for r in rows if r.record_type in kinds]


async def test_the_same_statement_twice_adds_nothing(session, run, api, tmp_path) -> None:
    api.me["username"] = USERNAME
    connection = await _connection(run)
    await _sync(session, connection, api)
    body = _statement()
    first = await _ingest(session, connection, body, tmp_path)
    before = await _records(session, connection)
    second = await _ingest(session, connection, body, tmp_path)
    assert await _records(session, connection) == before
    assert sorted(second.raw_record_ids) == sorted(first.raw_record_ids)


async def test_an_overlapping_statement_adds_only_its_new_rows(session, run, api, tmp_path) -> None:
    api.me["username"] = USERNAME
    connection = await _connection(run)
    await _sync(session, connection, api)
    created = today() - timedelta(days=30)

    def row(days: int, amount: str) -> dict[str, str]:
        return activity_row(_statement_moment(created + timedelta(days=days)), "Deposit", amount)

    def statement(end: date, activity: list[dict[str, str]]) -> bytes:
        return build_statement(
            summary(
                start=_statement_moment(created, "00:00:00"),
                end=_statement_moment(end, "23:59:59"),
                created=_statement_moment(created),
            ),
            activity,
            [closed_row()],
        )

    await _ingest(
        session,
        connection,
        statement(today() - timedelta(days=10), [row(1, "10"), row(2, "20")]),
        tmp_path,
    )
    first = _rows_of(await _records(session, connection))
    assert len(first) == 3
    await _ingest(
        session,
        connection,
        statement(today(), [row(1, "10"), row(2, "20"), row(25, "30")]),
        tmp_path,
    )
    second = _rows_of(await _records(session, connection))
    # The two shared deposits and the closed position are the records already stored.
    assert len(second) == 4
    assert {r.id for r in first} <= {r.id for r in second}
    [new] = [r for r in second if r.id not in {f.id for f in first}]
    assert new.content["cells"]["Amount"] == "30"


async def test_the_upload_merges_only_its_flags_into_the_cursor(
    session, run, api, tmp_path, monkeypatch
) -> None:
    api.me["username"] = USERNAME
    connection = await _connection(run)
    await _sync(session, connection, api)
    publish = etoro_sync.publish_flags

    async def meanwhile(ctx: SyncContext, found: Any, checked: Any) -> dict[str, JSONValue]:
        flags = await publish(ctx, found, checked)
        # A sync writing its own cursor key between the upload's read and its write.
        await session.execute(
            text(
                'update core.connections set cursor = cursor || \'{"daily_at": "later"}\''
                " where id = :id"
            ),
            {"id": connection.id},
        )
        return flags

    monkeypatch.setattr(etoro_sync, "publish_flags", meanwhile)
    created = today() - timedelta(days=30)
    body = build_statement(
        summary(
            start=_statement_moment(created, "00:00:00"),
            end=_statement_moment(today(), "23:59:59"),
            created=_statement_moment(created),
        ),
        [],
        [{**closed_row(), "Brand New Column": "7"}],
    )
    await _ingest(session, connection, body, tmp_path)
    cursor = (await get_connection(session, connection.id)).cursor
    assert isinstance(cursor, dict)
    assert cursor["daily_at"] == "later"
    assert cursor["flags"] == {"statement": ["Closed Positions.Brand New Column"]}
    assert cursor["gcid"] == "90000001"


async def test_statement_flags_published_once(session, run, api, tmp_path) -> None:
    api.me["username"] = USERNAME
    connection = await _connection(run)
    await _sync(session, connection, api)
    created = today() - timedelta(days=30)
    body = build_statement(
        summary(
            start=_statement_moment(created, "00:00:00"),
            end=_statement_moment(today(), "23:59:59"),
            created=_statement_moment(created),
        ),
        [],
        [{**closed_row(), "Brand New Column": "7"}],
    )
    notifier = RecordingNotifier()
    result = await _ingest(session, connection, body, tmp_path, notifier)
    flag = "eToro flagged: Closed Positions.Brand New Column"
    assert result.flagged == frozenset({"Closed Positions.Brand New Column"})
    after = await get_connection(session, connection.id)
    assert after.warnings == [flag]
    assert notifier.messages == [f"eToro: {flag}"]
    # A sync keeps the statement's flag, and does not tell it again.
    sync_notifier = RecordingNotifier()
    after = await _run_sync(session, connection, api, sync_notifier)
    assert after.warnings == [flag]
    assert sync_notifier.messages == []
