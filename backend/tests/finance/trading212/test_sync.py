"""The Trading 212 sync: every item stored as a raw record, history walked newest first."""

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
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
    set_warnings,
    store_credential,
)
from lararium.core.connectors import SyncContext
from lararium.core.errors import CredentialRejectedError
from lararium.core.json_types import JSONValue
from lararium.modules import registry
from lararium.modules.finance.trading212 import client as client_module
from lararium.modules.finance.trading212 import normalize as t212_normalize
from lararium.modules.finance.trading212 import sync as t212_sync
from lararium.modules.finance.trading212.client import Trading212Client
from lararium.modules.finance.trading212.normalize import NORMALIZER_VERSION, NormalizeReport
from lararium.modules.finance.trading212.sync import Trading212Connector
from lararium.worker import jobs

from .fixtures import (
    ACCOUNT_ID,
    dividend_item,
    dumps,
    instrument_json,
    order_item,
    page,
    pie_json,
    position_json,
    summary_json,
    transaction_item,
)

KEY = bytes(range(32))
API_KEY = "k" * 30 + "-synthetic-t212-key"
API_SECRET = "s" * 30 + "-synthetic-t212-secret"
CONNECTOR = Actor("connector", "trading212")
SETTINGS = Settings(
    database_url="postgresql+psycopg://x@localhost/x",
    auth_mode="dev",
    master_key=SecretStr(base64.b64encode(KEY).decode()),
)
API = "/api/v0/equity"
HISTORY = {kind: f"{API}/history/{kind}" for kind in ("orders", "dividends", "transactions")}
METADATA = {f"{API}/metadata/instruments", f"{API}/metadata/exchanges", f"{API}/pies"}


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> None:
        self.messages.append(text)


def orders(*fill_ids: int) -> list[dict[str, Any]]:
    return [order_item(order_id=fill_id - 200000, fill_id=fill_id) for fill_id in fill_ids]


def dividends(*refs: str) -> list[dict[str, Any]]:
    return [dividend_item(reference=ref) for ref in refs]


def transactions(*refs: str) -> list[dict[str, Any]]:
    return [transaction_item(reference=ref) for ref in refs]


def _token(items: list[dict[str, Any]]) -> str:
    """A page's cursor: its first item's id, so it stays the page's as newer pages arrive."""
    first = items[0]
    if "order" in first:
        return str((first["fill"] or first["order"])["id"])
    return str(first["reference"])


class FlakyNotifier:
    """A notifier whose first send fails."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.failed = False

    async def send(self, text: str) -> None:
        if not self.failed:
            self.failed = True
            raise RuntimeError("https://hooks.example.test/SECRET-TOKEN")
        self.messages.append(text)


class FakeTrading212:
    """Trading 212's API: each history a list of pages linked by `cursor`, newest first."""

    def __init__(self) -> None:
        self.summary = summary_json()
        self.positions = [position_json()]
        self.pending: list[dict[str, Any]] = []
        self.history: dict[str, list[list[dict[str, Any]]]] = {
            "orders": [orders(700004, 700003), orders(700002, 700001)],
            "dividends": [dividends("div-0002", "div-0001")],
            "transactions": [transactions("tx-0002", "tx-0001")],
        }
        self.instruments = [instrument_json()]
        self.exchanges = [{"id": Decimal(71), "name": "NYSE", "workingSchedules": []}]
        self.pies = [pie_json(9001)]
        self.pie_detail = {"instruments": [], "settings": {"id": Decimal(9001), "name": "P"}}
        self.refuse: dict[str, httpx.Response] = {}
        # Page tokens (`_token`) whose history page answers 404.
        self.gone: set[str] = set()
        # Page tokens whose history page names itself as the next page.
        self.looping: set[str] = set()
        self.requests: list[httpx.Request] = []
        # A fake clock that sleeping advances: pacing waits cost no real time.
        self.now = Decimal(1000)

    async def sleep(self, seconds: int) -> None:
        self.now += seconds

    def clock(self) -> Decimal:
        return self.now

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path in self.refuse:
            return self.refuse[path]
        for kind, history_path in HISTORY.items():
            if path == history_path:
                pages = self.history[kind]
                n = self._index(kind, request)
                if n > 0 and _token(pages[n]) in self.gone:
                    return httpx.Response(404, text="Not Found")
                next_path = (
                    f"limit=50&cursor={_token(pages[n + 1])}" if n + 1 < len(pages) else None
                )
                if pages[n] and _token(pages[n]) in self.looping:
                    next_path = f"limit=50&cursor={_token(pages[n])}"
                return self._ok(page(pages[n], next_path))
        bodies: dict[str, Any] = {
            f"{API}/account/summary": self.summary,
            f"{API}/positions": self.positions,
            f"{API}/orders": self.pending,
            f"{API}/metadata/instruments": self.instruments,
            f"{API}/metadata/exchanges": self.exchanges,
            f"{API}/pies": self.pies,
            f"{API}/pies/9001": self.pie_detail,
        }
        if path not in bodies:
            raise AssertionError(f"unexpected request to {path}")
        return self._ok(bodies[path])

    @staticmethod
    def _ok(body: Any) -> httpx.Response:
        return httpx.Response(200, text=dumps(body))

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def _index(self, kind: str, request: httpx.Request) -> int:
        token = request.url.params.get("cursor")
        if token is None:
            return 0
        return [_token(p) for p in self.history[kind]].index(token)

    def cursors(self, kind: str) -> list[int]:
        """The pages asked of a history, in order, by their place in it now."""
        return [self._index(kind, r) for r in self.requests if r.url.path == HISTORY[kind]]


@pytest.fixture(autouse=True)
def _no_ledger(monkeypatch) -> None:
    """These tests are of the sync: its fake history is not one account's consistent books,
    so the normalizer (test_normalize, test_reconcile) is left out."""

    async def normalize(ctx: SyncContext, raw_record_ids: list[uuid.UUID]) -> NormalizeReport:
        return NormalizeReport()

    monkeypatch.setattr(t212_sync, "normalize", normalize)


@pytest.fixture
def api(monkeypatch) -> FakeTrading212:
    fake = FakeTrading212()
    monkeypatch.setattr(
        t212_sync,
        "Trading212Client",
        partial(Trading212Client, sleep=fake.sleep, clock=fake.clock, wall_clock=fake.clock),
    )
    return fake


async def _connection(run, *, cursor: JSONValue = None, credentials: bool = True):
    async def setup(ctx: CommandContext) -> ConnectionRecord:
        connection = await create_connection(
            ctx,
            connector="trading212",
            display_name="Trading 212 Invest",
            config={"account_kind": "invest"},
        )
        if credentials:
            await store_credential(ctx, KEY, connection.id, "api_key", API_KEY, None)
            await store_credential(ctx, KEY, connection.id, "api_secret", API_SECRET, None)
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


async def _set_warnings(session, connection, warnings: list[str]) -> None:
    await session.execute(
        text("update core.connections set warnings = cast(:w as jsonb) where id = :id"),
        {"w": json.dumps(warnings), "id": connection.id},
    )


async def _ctx(session, connection, http: httpx.AsyncClient) -> SyncContext:
    return SyncContext(
        command=CommandContext(session, CONNECTOR, uuid.uuid4()),
        connection=await get_connection(session, connection.id),
        sync_id=uuid.uuid4(),
        master_key=KEY,
        http=http,
        notifier=RecordingNotifier(),
        session_factory=async_sessionmaker(bind=session.bind),
    )


async def _sync(session, connection, api: FakeTrading212) -> JSONValue:
    """One sync, called directly, as the worker would in its command."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(api.handler)) as http:
        cursor = await Trading212Connector().sync(await _ctx(session, connection, http))
    await _set_cursor(session, connection, cursor)
    return cursor


async def _run_sync(session, connection, api: FakeTrading212, notifier=None) -> ConnectionRecord:
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


async def _records(session, connection, record_type: str | None = None) -> list[Any]:
    sql = (
        "select record_type, request, content from core.raw_records where connection_id = :c"
        + (" and record_type = :t" if record_type else "")
        + " order by record_type, fetched_at, id"
    )
    params: dict[str, Any] = {"c": connection.id}
    if record_type:
        params["t"] = record_type
    return list((await session.execute(text(sql), params)).all())


def _fill_ids(rows: list[Any]) -> list[str]:
    return [row.content["fill"]["id"] for row in rows]


def _ago(**delta: float) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).isoformat()


def _later_cursor(**overrides: Any) -> dict[str, Any]:
    """A cursor of a sync that has seen the fake API's default history."""
    return {
        "account_id": str(ACCOUNT_ID),
        "metadata_at": _ago(hours=1),
        "rewalk_at": _ago(days=1),
        "seen": {"orders": "700004", "dividends": "div-0002", "transactions": "tx-0002"},
        "resume": {},
        **overrides,
    }


async def test_first_sync_walks_everything_and_stores_raw(session, run, api) -> None:
    api.pending = [{"id": Decimal(55), "ticker": "ZZQA_US_EQ", "side": "BUY", "status": "NEW"}]
    connection = await _connection(run)
    before = datetime.now(UTC)
    cursor = await _sync(session, connection, api)

    rows = await _records(session, connection)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.record_type] = counts.get(row.record_type, 0) + 1
    assert counts == {
        "trading212.summary": 1,
        "trading212.position": 1,
        "trading212.pending_order": 1,
        "trading212.order": 4,
        "trading212.dividend": 2,
        "trading212.transaction": 2,
        "trading212.instrument": 1,
        "trading212.exchange": 1,
        "trading212.pie": 1,
        "trading212.pie_detail": 1,
    }
    # The first sync reads every page.
    assert api.cursors("orders") == [0, 1]

    # The item as sent; every number its exact text (the raw record's JSON takes no Decimal).
    [summary] = await _records(session, connection, "trading212.summary")
    assert summary.content["id"] == str(ACCOUNT_ID)
    assert summary.content["totalValue"] == "1250.75"
    assert summary.content["cash"]["reservedForOrders"] == "0"
    assert summary.request == {"endpoint": f"{API}/account/summary", "params": {}}
    order_rows = await _records(session, connection, "trading212.order")
    assert sorted(_fill_ids(order_rows)) == ["700001", "700002", "700003", "700004"]
    second_page = next(r for r in order_rows if r.content["fill"]["id"] == "700001")
    assert second_page.request == {
        "endpoint": HISTORY["orders"],
        "params": {"limit": "50", "cursor": "700002"},
    }
    assert second_page.content["fill"]["walletImpact"]["fxRate"] == "0.8"
    assert second_page.content["order"]["extendedHours"] is False
    [detail] = await _records(session, connection, "trading212.pie_detail")
    assert detail.request == {"endpoint": f"{API}/pies/9001", "params": {}}

    # Never a key in what is stored.
    stored = json.dumps([[r.request, r.content] for r in rows])
    assert API_KEY not in stored and API_SECRET not in stored

    assert isinstance(cursor, dict)
    assert cursor["account_id"] == str(ACCOUNT_ID)
    assert cursor["seen"] == {
        "orders": "700004",
        "dividends": "div-0002",
        "transactions": "tx-0002",
    }
    assert cursor["resume"] == {}
    assert (cursor["flags"], cursor["notified"]) == ({}, [])
    for key in ("metadata_at", "rewalk_at"):
        assert isinstance(cursor[key], str)
        assert datetime.fromisoformat(cursor[key]) >= before


async def test_later_sync_stops_at_seen(session, run, api) -> None:
    api.history["orders"] = [
        orders(700006, 700005),
        orders(700004, 700003),
        orders(700002, 700001),
    ]
    api.history["dividends"] = [dividends("div-0003", "div-0002"), dividends("div-0001")]
    connection = await _connection(run, cursor=_later_cursor())
    cursor = await _sync(session, connection, api)

    # The page holding the newest item seen before is not all seen; the one after it is.
    assert api.cursors("orders") == [0, 1]
    assert api.cursors("dividends") == [0, 1]
    assert api.cursors("transactions") == [0]
    assert sorted(_fill_ids(await _records(session, connection, "trading212.order"))) == [
        "700003",
        "700004",
        "700005",
        "700006",
    ]
    # Metadata and pies are a day's, and fetched an hour ago.
    assert not METADATA & set(api.paths())
    assert isinstance(cursor, dict)
    assert cursor["seen"] == {
        "orders": "700006",
        "dividends": "div-0003",
        "transactions": "tx-0002",
    }


async def test_newest_order_without_a_fill_keeps_the_newest_fill(session, run, api) -> None:
    cancelled = order_item(order_id=500099, fill_id=None)
    api.history["orders"] = [[cancelled, *orders(700005, 700004)], orders(700003)]
    connection = await _connection(run, cursor=_later_cursor())
    cursor = await _sync(session, connection, api)
    assert isinstance(cursor, dict)
    assert cursor["seen"]["orders"] == "700005"
    assert api.cursors("orders") == [0, 1]


async def test_weekly_rewalk(session, run, api) -> None:
    api.history["orders"] = [orders(700004), orders(700003), orders(700002), orders(700001)]
    old = _ago(days=8)
    connection = await _connection(run, cursor=_later_cursor(rewalk_at=old))
    before = datetime.now(UTC)
    cursor = await _sync(session, connection, api)
    # Every page again, though the first holds only what was seen.
    assert api.cursors("orders") == [0, 1, 2, 3]
    assert api.cursors("dividends") == [0]
    assert isinstance(cursor, dict)
    assert isinstance(cursor["rewalk_at"], str)
    assert datetime.fromisoformat(cursor["rewalk_at"]) >= before

    # Six days on, the walk stops at what was seen again.
    api.requests.clear()
    connection = await _connection(run, cursor=_later_cursor(rewalk_at=_ago(days=6)))
    await _sync(session, connection, api)
    assert api.cursors("orders") == [0]


async def test_metadata_and_pies_daily(session, run, api) -> None:
    connection = await _connection(run, cursor=_later_cursor(metadata_at=_ago(hours=25)))
    before = datetime.now(UTC)
    cursor = await _sync(session, connection, api)
    assert METADATA | {f"{API}/pies/9001"} <= set(api.paths())
    assert isinstance(cursor, dict)
    assert isinstance(cursor["metadata_at"], str)
    assert datetime.fromisoformat(cursor["metadata_at"]) >= before

    api.requests.clear()
    await _sync(session, connection, api)
    assert not METADATA & set(api.paths())


async def test_a_key_without_optional_permissions_skips_them(session, run, api) -> None:
    for path, scope in ((f"{API}/orders", "orders:read"), (f"{API}/pies", "pies:read")):
        api.refuse[path] = httpx.Response(403, text=f"Scope( {scope} ) missing for API key")
    connection = await _connection(run)
    after = await _run_sync(session, connection, api)
    assert after.status == "healthy"
    types = {row.record_type for row in await _records(session, connection)}
    assert "trading212.pending_order" not in types
    assert "trading212.pie" not in types
    assert "trading212.instrument" in types
    assert after.warnings == [
        "Trading 212 flagged: missing permission orders:read",
        "Trading 212 flagged: missing permission pies:read",
    ]


async def test_missing_optional_scope_flagged_once(session, run, api) -> None:
    api.refuse[f"{API}/orders"] = httpx.Response(403, text="Scope( orders:read ) missing")
    connection = await _connection(run)
    await _set_warnings(session, connection, ["Some other warning"])
    notifier = RecordingNotifier()
    await _run_sync(session, connection, api, notifier)
    after = await _run_sync(session, connection, api, notifier)
    assert notifier.messages == [
        "Trading 212 Invest: Trading 212 flagged: missing permission orders:read"
    ]
    assert after.warnings == [
        "Some other warning",
        "Trading 212 flagged: missing permission orders:read",
    ]

    # The permission ticked: the flag clears, and nothing more is said.
    del api.refuse[f"{API}/orders"]
    after = await _run_sync(session, connection, api, notifier)
    assert after.warnings == ["Some other warning"]
    assert len(notifier.messages) == 1


async def test_flags_of_other_origins_are_kept(session, run, api) -> None:
    connection = await _connection(run)
    # A field flag, as the normalizer will write: not the sync's to clear.
    kept = ["Trading 212 flagged: order.someNewField"]
    await _set_warnings(session, connection, kept)
    after = await _run_sync(session, connection, api)
    assert after.warnings == kept


async def test_removed_pies_endpoint_skipped_and_flagged(session, run, api) -> None:
    api.refuse[f"{API}/pies"] = httpx.Response(404, text="Not Found")
    connection = await _connection(run)
    notifier = RecordingNotifier()
    after = await _run_sync(session, connection, api, notifier)
    assert after.status == "healthy"
    types = {row.record_type for row in await _records(session, connection)}
    assert "trading212.pie" not in types
    assert {"trading212.instrument", "trading212.exchange", "trading212.order"} <= types
    assert after.warnings == ["Trading 212 flagged: pies endpoint removed"]
    assert notifier.messages == ["Trading 212 Invest: Trading 212 flagged: pies endpoint removed"]

    # The next sync does not ask for pies (a day's): the flag stands, and nothing more is said.
    api.requests.clear()
    after = await _run_sync(session, connection, api, notifier)
    assert f"{API}/pies" not in api.paths()
    assert after.warnings == ["Trading 212 flagged: pies endpoint removed"]
    assert len(notifier.messages) == 1


async def test_a_pie_gone_is_skipped_alone(session, run, api) -> None:
    api.refuse[f"{API}/pies/9001"] = httpx.Response(404, text="Not Found")
    connection = await _connection(run)
    after = await _run_sync(session, connection, api)
    assert after.status == "healthy"
    types = {row.record_type for row in await _records(session, connection)}
    assert "trading212.pie" in types
    assert "trading212.pie_detail" not in types
    assert after.warnings == []


async def test_a_404_on_a_required_endpoint_fails(session, run, api) -> None:
    api.refuse[f"{API}/positions"] = httpx.Response(404, text="Not Found")
    connection = await _connection(run)
    after = await _run_sync(session, connection, api)
    assert after.status == "failing"
    assert after.last_error == "The API has no such resource (404)"


async def test_401_needs_reauth(session, run, api) -> None:
    api.refuse[f"{API}/account/summary"] = httpx.Response(401, text="Unauthorized")
    connection = await _connection(run)
    notifier = RecordingNotifier()
    after = await _run_sync(session, connection, api, notifier)
    assert after.status == "needs_reauth"
    assert len(notifier.messages) == 1
    for secret in (API_KEY, API_SECRET):
        assert secret not in notifier.messages[0]
        assert secret not in (after.last_error or "")


async def test_missing_required_scope_fails_naming_it(session, run, api) -> None:
    api.refuse[HISTORY["orders"]] = httpx.Response(
        403, text="Scope( history:orders ) missing for API key"
    )
    connection = await _connection(run)
    notifier = RecordingNotifier()
    after = await _run_sync(session, connection, api, notifier)
    # A new key is needed: the owner is told at once (design §5.13).
    assert after.status == "needs_reauth"
    assert after.last_error == "Trading 212 key lacks the history:orders permission"
    [message] = notifier.messages
    assert "Trading 212 key lacks the history:orders permission" in message
    # The sync rolled back: nothing it stored before the refusal is kept.
    assert await _records(session, connection) == []


async def test_account_id_mismatch_refused(session, run, api) -> None:
    connection = await _connection(run, cursor=_later_cursor(account_id="10000002"))
    after = await _run_sync(session, connection, api)
    assert after.last_error == "this key belongs to another Trading 212 account"
    assert after.status == "needs_reauth"
    # Nothing past the summary is asked, and nothing stored.
    assert api.paths() == [f"{API}/account/summary"]
    assert await _records(session, connection) == []


async def test_missing_credentials_are_rejected(session, run, api) -> None:
    connection = await _connection(run, credentials=False)
    with pytest.raises(CredentialRejectedError, match="Trading 212 API key or secret"):
        await _sync(session, connection, api)
    assert api.requests == []


async def test_history_longer_than_the_cap_finishes_over_two_syncs(
    session, run, api, monkeypatch
) -> None:
    monkeypatch.setattr(client_module, "MAX_PAGES", 3)
    api.history["orders"] = [orders(700000 + 2 * n, 700000 + 2 * n - 1) for n in range(5, 0, -1)]
    every = sorted(str(item["fill"]["id"]) for p in api.history["orders"] for item in p)
    connection = await _connection(run)

    first = await _sync(session, connection, api)
    assert api.cursors("orders") == [0, 1, 2]
    assert isinstance(first, dict)
    # What was fetched is kept, and the walk goes on next time from the page after it.
    assert len(await _records(session, connection, "trading212.order")) == 6
    assert first["resume"] == {"orders": "limit=50&cursor=700004"}
    assert first["seen"]["orders"] == "700010"

    api.requests.clear()
    second = await _sync(session, connection, api)
    # Newest first, stopping at what was seen; then on from where the first sync stopped.
    assert api.cursors("orders") == [0, 3, 4]
    assert isinstance(second, dict)
    assert second["resume"] == {}
    assert second["seen"]["orders"] == "700010"
    assert sorted(_fill_ids(await _records(session, connection, "trading212.order"))) == every

    api.requests.clear()
    await _sync(session, connection, api)
    assert api.cursors("orders") == [0]


async def _ledger(session, connection) -> list[tuple[Any, ...]]:
    """The connection's transactions: (external id, time, its postings' instruments and
    quantities), whatever its accounts' ids."""
    rows = await session.execute(
        text(
            "select t.external_id, t.occurred_at,"
            " array_agg(p.instrument_id::text || ' ' || p.quantity::text order by"
            " p.instrument_id, p.quantity) as postings"
            " from finance.transactions t join finance.postings p on p.transaction_id = t.id"
            " where t.connection_id = :c and t.status <> 'void'"
            " group by t.id, t.external_id, t.occurred_at order by t.external_id"
        ),
        {"c": connection.id},
    )
    return [tuple(row) for row in rows]


async def test_each_sync_walks_at_most_the_cap_and_the_ledger_comes_out_the_same(
    session, run, api, monkeypatch
) -> None:
    """The cap bounds a sync's pages per kind, the pass from the newest and the resumed one
    together; a history longer than that finishes over several syncs, with the ledger one
    sync of it all gives."""
    monkeypatch.setattr(t212_sync, "normalize", t212_normalize.normalize)
    pages = [orders(700000 + 2 * n, 700000 + 2 * n - 1) for n in range(7, 0, -1)]
    api.history["orders"] = pages
    whole = await _connection(run)
    await _sync(session, whole, api)
    expected = await _ledger(session, whole)
    assert len(expected) >= 14

    monkeypatch.setattr(client_module, "MAX_PAGES", 3)
    capped = await _connection(run)
    walked: list[list[int]] = []
    for _ in range(4):
        api.requests.clear()
        cursor = await _sync(session, capped, api)
        walked.append(api.cursors("orders"))
        assert isinstance(cursor, dict)
        if cursor["resume"] == {}:
            break
    assert all(len(asked) <= 3 for asked in walked)
    assert walked == [[0, 1, 2], [0, 3, 4], [0, 5, 6]]
    assert await _ledger(session, capped) == expected


async def test_a_rewalk_that_reaches_the_end_drops_the_resume(session, run, api) -> None:
    api.history["orders"] = [orders(700004), orders(700003)]
    cursor = _later_cursor(rewalk_at=_ago(days=8), resume={"orders": "limit=50&cursor=700003"})
    connection = await _connection(run, cursor=cursor)
    after = await _sync(session, connection, api)
    assert api.cursors("orders") == [0, 1]
    assert isinstance(after, dict)
    assert after["resume"] == {}


async def test_a_repeated_item_is_stored_once(session, run, api) -> None:
    connection = await _connection(run)
    await _sync(session, connection, api)
    first = await _records(session, connection)
    # A re-walk a week on, metadata and pies due again: the same items, nothing new.
    await _set_cursor(
        session, connection, _later_cursor(rewalk_at=_ago(days=8), metadata_at=_ago(days=2))
    )
    api.requests.clear()
    await _sync(session, connection, api)
    assert api.cursors("orders") == [0, 1]
    assert await _records(session, connection) == first


def test_connector_identity_and_registration() -> None:
    connector = Trading212Connector()
    assert (connector.name, connector.version) == ("trading212", "trading212-sync-1")
    assert connector.capabilities == {"balances", "holdings", "transactions"}
    assert isinstance(registry.connector("trading212"), Trading212Connector)


async def test_check_access_is_true(session, run, api) -> None:
    connection = await _connection(run)
    async with httpx.AsyncClient(transport=httpx.MockTransport(api.handler)) as http:
        ctx = await _ctx(session, connection, http)
        assert await Trading212Connector().check_access(ctx) is True
    assert api.requests == []


async def test_normalize_is_called_and_reports_nothing_yet(session, run, api, monkeypatch) -> None:
    called: list[int] = []

    async def spy(ctx: SyncContext, raw_record_ids: list[uuid.UUID]) -> NormalizeReport:
        called.append(len(raw_record_ids))
        return NormalizeReport()

    monkeypatch.setattr(t212_sync, "normalize", spy)
    connection = await _connection(run)
    await _sync(session, connection, api)
    assert called == [len(await _records(session, connection))]
    assert NORMALIZER_VERSION == "trading212-norm-2"


async def test_warnings_written_during_the_sync_survive(session, run, api, monkeypatch) -> None:
    api.refuse[f"{API}/orders"] = httpx.Response(403, text="Scope( orders:read ) missing")
    field_flag = "Trading 212 flagged: order.someNewField"

    async def normalize(ctx: SyncContext, raw_record_ids: list[uuid.UUID]) -> NormalizeReport:
        # As Task 5's normalizer will: its own flag, written while the sync runs.
        current = await get_connection(ctx.command.session, ctx.connection.id)
        await set_warnings(ctx.command, ctx.connection.id, [*current.warnings, field_flag])
        return NormalizeReport()

    monkeypatch.setattr(t212_sync, "normalize", normalize)
    connection = await _connection(run)
    notifier = RecordingNotifier()
    after = await _run_sync(session, connection, api, notifier)
    assert after.warnings == [field_flag, "Trading 212 flagged: missing permission orders:read"]
    assert notifier.messages == [
        "Trading 212 Invest: Trading 212 flagged: missing permission orders:read"
    ]


async def test_a_failed_notification_is_sent_next_sync(session, run, api) -> None:
    api.refuse[f"{API}/orders"] = httpx.Response(403, text="Scope( orders:read ) missing")
    connection = await _connection(run)
    notifier = FlakyNotifier()
    after = await _run_sync(session, connection, api, notifier)
    assert notifier.failed and notifier.messages == []
    assert after.warnings == ["Trading 212 flagged: missing permission orders:read"]
    after = await _run_sync(session, connection, api, notifier)
    assert notifier.messages == [
        "Trading 212 Invest: Trading 212 flagged: missing permission orders:read"
    ]
    await _run_sync(session, connection, api, notifier)
    assert len(notifier.messages) == 1


async def test_a_looping_next_page_is_flagged(session, run, api) -> None:
    """A page naming itself as the next one ends the walk, as a 404 does, and is flagged;
    the flag clears once a walk of that history ends as it should."""
    api.history["orders"] = [orders(700006, 700005), orders(700004, 700003), orders(700002)]
    api.looping = {"700004"}
    connection = await _connection(run)
    notifier = RecordingNotifier()
    after = await _run_sync(session, connection, api, notifier)
    assert after.status == "healthy"
    assert api.cursors("orders") == [0, 1, 1]
    flag = "Trading 212 flagged: orders history repeated a page"
    assert flag in after.warnings
    assert notifier.messages == [f"Trading 212 Invest: {flag}"]

    api.looping = set()
    api.requests.clear()
    after = await _run_sync(session, connection, api, notifier)
    assert flag not in after.warnings


async def test_a_404_deep_in_a_history_resumes_next_sync(session, run, api) -> None:
    api.history["orders"] = [orders(700006, 700005), orders(700004, 700003), orders(700002)]
    api.gone = {"700004"}
    connection = await _connection(run)
    notifier = RecordingNotifier()
    after = await _run_sync(session, connection, api, notifier)
    # What came before the missing page is kept; the walk goes on from it next time.
    assert after.status == "healthy"
    assert after.cursor["resume"] == {"orders": "limit=50&cursor=700004"}
    assert sorted(_fill_ids(await _records(session, connection, "trading212.order"))) == [
        "700005",
        "700006",
    ]
    flag = "Trading 212 flagged: orders history stopped early (404)"
    assert after.warnings == [flag]
    assert notifier.messages == [f"Trading 212 Invest: {flag}"]

    # Still missing: the same page is asked again, and nothing more is said.
    api.requests.clear()
    after = await _run_sync(session, connection, api, notifier)
    assert after.status == "healthy"
    assert api.cursors("orders") == [0, 1]
    assert after.cursor["resume"] == {"orders": "limit=50&cursor=700004"}
    assert len(notifier.messages) == 1

    # Back: the walk finishes, the flag clears, and every order is stored once.
    api.gone = set()
    api.requests.clear()
    after = await _run_sync(session, connection, api, notifier)
    assert api.cursors("orders") == [0, 1, 2]
    assert after.cursor["resume"] == {}
    assert after.warnings == []
    assert sorted(_fill_ids(await _records(session, connection, "trading212.order"))) == [
        str(n) for n in range(700002, 700007)
    ]


async def test_a_404_on_the_first_history_page_fails(session, run, api) -> None:
    api.refuse[HISTORY["dividends"]] = httpx.Response(404, text="Not Found")
    connection = await _connection(run)
    after = await _run_sync(session, connection, api)
    assert after.status == "failing"
    assert after.last_error == "The API has no such resource (404)"


async def test_new_items_while_a_resume_is_pending(session, run, api, monkeypatch) -> None:
    monkeypatch.setattr(client_module, "MAX_PAGES", 3)
    api.history["orders"] = [orders(700000 + 2 * n, 700000 + 2 * n - 1) for n in range(5, 0, -1)]
    connection = await _connection(run)
    first = await _sync(session, connection, api)
    assert isinstance(first, dict)
    assert first["resume"] == {"orders": "limit=50&cursor=700004"}

    api.history["orders"].insert(0, orders(700012, 700011))
    api.requests.clear()
    second = await _sync(session, connection, api)
    # The new page, the page holding what was seen, then on from the page the first stopped
    # at, as far as the cap: three pages in all.
    assert api.cursors("orders") == [0, 1, 4]
    assert isinstance(second, dict)
    assert second["resume"] == {"orders": "limit=50&cursor=700002"}
    assert second["seen"]["orders"] == "700012"

    api.requests.clear()
    third = await _sync(session, connection, api)
    assert api.cursors("orders") == [0, 5]
    assert isinstance(third, dict)
    assert third["resume"] == {}
    stored = _fill_ids(await _records(session, connection, "trading212.order"))
    assert sorted(stored) == [str(n) for n in range(700001, 700013)]


async def test_a_part_not_checked_keeps_its_flags_whatever_their_text(session, run, api) -> None:
    flag = "Trading 212 flagged: any reason at all"
    cursor = _later_cursor(flags={"pies": ["any reason at all"]}, notified=["any reason at all"])
    connection = await _connection(run, cursor=cursor)
    await _set_warnings(session, connection, [flag])
    notifier = RecordingNotifier()
    after = await _run_sync(session, connection, api, notifier)
    # Pies are not due: their flag stands, already told.
    assert after.warnings == [flag]
    assert after.cursor["flags"] == {"pies": ["any reason at all"]}
    assert notifier.messages == []

    # Pies due and answering: the flag clears.
    await _set_cursor(session, connection, {**after.cursor, "metadata_at": _ago(days=2)})
    after = await _run_sync(session, connection, api, notifier)
    assert after.warnings == []
    assert after.cursor["flags"] == {}


async def test_each_report_is_sighted_every_sync(session, run, api) -> None:
    connection = await _connection(run)
    before = datetime.now(UTC)
    await _sync(session, connection, api)
    await _sync(session, connection, api)
    # The same summary twice is one raw record, seen by both syncs.
    assert len(await _records(session, connection, "trading212.summary")) == 1
    rows = await session.execute(
        text(
            "select r.record_type, count(*) as n, min(s.seen_at) as first"
            " from finance.report_sightings s join core.raw_records r on r.id = s.raw_record_id"
            " where s.connection_id = :c group by r.record_type"
        ),
        {"c": connection.id},
    )
    counts = {row.record_type: row.n for row in rows}
    assert counts["trading212.summary"] == 2
    assert counts["trading212.position"] == 2
    # Pies are fetched on metadata days only: the first sync's.
    assert counts["trading212.pie"] == counts["trading212.pie_detail"] == 1
    # History and metadata are not reports.
    assert "trading212.order" not in counts and "trading212.instrument" not in counts
    first = await session.scalar(
        text("select min(seen_at) from finance.report_sightings where connection_id = :c"),
        {"c": connection.id},
    )
    assert first >= before


async def test_reconciliation_record_survives_the_worker(session, run, api, monkeypatch) -> None:
    """The sync's cursor replaces the row's (record_sync_success): it must carry what
    reconciliation recorded, so the next run replaces its warnings rather than adding to them."""
    monkeypatch.setattr(t212_sync, "normalize", t212_normalize.normalize)
    connection = await _connection(run)
    notifier = RecordingNotifier()
    await _run_sync(session, connection, api, notifier)
    # Later, Trading 212 reports a position the history does not explain.
    api.positions = [position_json(quantity="99")]
    first = await _run_sync(session, connection, api, notifier)
    assert isinstance(first.cursor, dict)
    found = first.cursor["reconciliation"]
    assert isinstance(found, dict) and found["checked"]
    assert first.cursor["reconciliation_notified"] == found["checked"]
    told = len(notifier.messages)
    second = await _run_sync(session, connection, api, notifier)
    assert second.warnings == first.warnings
    assert len(second.warnings) == len(set(second.warnings))
    assert isinstance(second.cursor, dict)
    assert second.cursor["reconciliation"] == found
    assert len(notifier.messages) == told


async def test_normalization_runs_under_the_connection_lock(session, run, api, monkeypatch) -> None:
    # A sync and an export upload of one connection normalize one at a time: the sync takes
    # the connection's lock before it normalizes, and publishes its flags under it.
    connection = await _connection(run)
    events: list[str] = []
    lock, publish = t212_sync.lock_connection, t212_sync.publish_flags

    async def locking(ctx: Any, connection_id: uuid.UUID) -> Any:
        events.append("lock")
        return await lock(ctx, connection_id)

    async def publishing(ctx: SyncContext, *args: Any, **kwargs: Any) -> dict[str, JSONValue]:
        events.append("publish")
        return await publish(ctx, *args, **kwargs)

    async def normalizing(ctx: SyncContext, raw_record_ids: list[uuid.UUID]) -> NormalizeReport:
        events.append("normalize")
        return NormalizeReport()

    monkeypatch.setattr(t212_sync, "lock_connection", locking)
    monkeypatch.setattr(t212_sync, "publish_flags", publishing)
    monkeypatch.setattr(t212_sync, "normalize", normalizing)
    await _run_sync(session, connection, api)
    assert events == ["lock", "normalize", "publish"]


async def test_an_upload_during_a_sync_keeps_its_flag(session, run, api, monkeypatch) -> None:
    connection = await _connection(run)
    flag = "export column Brand New Column"
    lock = t212_sync.lock_connection
    uploaded: list[bool] = []

    async def locking(ctx: Any, connection_id: uuid.UUID) -> Any:
        # The owner uploads an export while the sync fetches, after it read its cursor and
        # before it takes the lock: the upload commits its flag first.
        if not uploaded:
            uploaded.append(True)
            current = (await get_connection(session, connection_id)).cursor
            await _set_warnings(session, connection, [f"Trading 212 flagged: {flag}"])
            await _set_cursor(
                session,
                connection,
                (current if isinstance(current, dict) else {})
                | {"flags": {"export": [flag]}, "notified": [flag]},
            )
        return await lock(ctx, connection_id)

    monkeypatch.setattr(t212_sync, "lock_connection", locking)
    after = await _run_sync(session, connection, api)
    assert f"Trading 212 flagged: {flag}" in after.warnings
    assert isinstance(after.cursor, dict) and after.cursor["flags"]["export"] == [flag]
