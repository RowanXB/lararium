"""The Binance connector's sync: the read-only gate, when a sync is due, the owner's "Sync now",
the account check, the balance reads and a first sync that resumes."""

import base64
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from procrastinate.testing import InMemoryConnector
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lararium.api.app import create_app
from lararium.config import Settings
from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.core.connections import (
    create_connection,
    get_connection,
    request_sync,
    store_credential,
)
from lararium.core.json_types import JSONValue
from lararium.db import create_engine, make_sessionmaker
from lararium.deps import get_session, get_session_factory
from lararium.modules.finance.binance import sync as sync_module
from lararium.modules.finance.binance.catalogue import ENDPOINTS
from lararium.modules.finance.binance.fetch import CURSORS_KEY, record_type
from lararium.modules.finance.binance.layout import TABLE_LABELS, layout
from lararium.modules.finance.binance.sync import (
    BALANCE_LABELS,
    HARMLESS_TRUE,
    HISTORY_LABELS,
    BinanceConnector,
    due,
    gate_problem,
)
from lararium.worker.app import procrastinate_app

from .fixtures import DAY_MS, HOUR_MS, NOW_MS, PERMISSIONS, FakeBinance, build_row
from .syncing import (
    API_KEY,
    API_SECRET,
    KEY,
    MINUTE_MS,
    UID,
    Clock,
    RecordingNotifier,
    binance_connection,
    later_config,
    run_binance_sync,
)

OWNER = {"Tailscale-User-Login": "me@example.com"}
OTHER_PERMISSIONS = sorted(name for name in PERMISSIONS if name not in HARMLESS_TRUE)


def test_connector_identity() -> None:
    connector = BinanceConnector()
    assert (connector.name, connector.version) == ("binance", "binance-sync-1")
    assert connector.capabilities == frozenset({"balances", "holdings", "transactions"})


def test_the_phase_a_labels() -> None:
    assert "my-trades" not in HISTORY_LABELS
    assert all(ENDPOINTS[label].phase == "A" for label in HISTORY_LABELS + BALANCE_LABELS)
    assert {"spot-account", "api-restrictions", "funding-asset"} <= set(BALANCE_LABELS)
    assert "rebate" not in HISTORY_LABELS


def test_gate_problem_at_any_depth() -> None:
    assert gate_problem(dict(PERMISSIONS)) is None
    assert gate_problem({**PERMISSIONS, "enableWithdrawals": True}) == "enableWithdrawals"
    assert gate_problem({**PERMISSIONS, "nested": {"enableNewThing": True}}) == "enableNewThing"
    assert gate_problem({**PERMISSIONS, "enableReading": False}) == "enableReading"
    assert gate_problem({"enableReading": "true"}) == "enableReading"
    assert gate_problem([]) == "enableReading"


@pytest.mark.parametrize("permission", [*OTHER_PERMISSIONS, "enableSomethingNew"])
async def test_gate_refuses_any_other_true_permission(
    session, run, fake, connector, permission: str
) -> None:
    fake.permissions[permission] = True
    connection = await binance_connection(run)
    notifier = RecordingNotifier()

    after = await run_binance_sync(session, connection.id, fake.handler, notifier=notifier)

    assert after.status == "needs_reauth"
    # The clock is read without the key; the gate is the only request that carries it.
    assert [seen.label for seen in fake.requests] == ["time", "api-restrictions"]
    [message] = notifier.messages
    assert f"this key can do more than read: {permission}" in message
    for secret in (API_KEY, API_SECRET):
        assert secret not in message
        assert secret not in (after.last_error or "")


async def test_gate_requires_reading(session, run, fake, connector) -> None:
    fake.permissions["enableReading"] = False
    connection = await binance_connection(run)
    notifier = RecordingNotifier()

    after = await run_binance_sync(session, connection.id, fake.handler, notifier=notifier)

    assert after.status == "needs_reauth"
    assert [seen.label for seen in fake.requests] == ["time", "api-restrictions"]
    assert "this key can do more than read: enableReading" in notifier.messages[0]


def _at(ms: int) -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=ms)


def test_due_every_four_hours() -> None:
    fetched: JSONValue = {"fetched_at": NOW_MS}
    assert due(None, _at(NOW_MS), False)
    assert due({}, _at(NOW_MS), False)
    assert not due(fetched, _at(NOW_MS + 4 * HOUR_MS - 1), False)
    assert due(fetched, _at(NOW_MS + 4 * HOUR_MS), False)
    # The owner asked: due at once.
    assert due(fetched, _at(NOW_MS + MINUTE_MS), True)


async def test_not_due_makes_no_request(session, run, fake, connector, clock) -> None:
    cursor: JSONValue = {"fetched_at": NOW_MS - HOUR_MS}
    connection = await binance_connection(run, config=later_config(), cursor=cursor)

    after = await run_binance_sync(session, connection.id, fake.handler)

    assert fake.requests == []
    assert after.cursor == cursor
    assert after.status == "healthy"


async def test_uid_mismatch_fails(session, run, fake, connector) -> None:
    connection = await binance_connection(run, config={**later_config(), "uid": 999_999})

    after = await run_binance_sync(session, connection.id, fake.handler)

    assert after.status == "failing"
    assert after.last_error == "Binance answered for another account than this connection's"
    assert "999999" not in after.last_error
    labels = {seen.label for seen in fake.requests}
    assert labels == {"time", "api-restrictions", "spot-account"}


async def test_balances_are_read_with_their_bracket(session, run, fake, connector) -> None:
    connection = await binance_connection(run, config=later_config())

    after = await run_binance_sync(session, connection.id, fake.handler)

    assert after.status == "healthy"
    assert after.cursor == {"fetched_at": NOW_MS}
    rows = (
        await session.execute(
            text(
                "select record_type, request from core.raw_records"
                " where connection_id = :id and request ? 'read_from_ms' order by id"
            ),
            {"id": connection.id},
        )
    ).all()
    types = [row.record_type for row in rows]
    assert sorted(types) == sorted(record_type(label) for label in BALANCE_LABELS)
    for row in rows:
        assert row.request["read_from_ms"] <= row.request["read_to_ms"]
    # Each read has its own bracket: the reads follow one another.
    spans = [(row.request["read_from_ms"], row.request["read_to_ms"]) for row in rows]
    assert len(set(spans)) == len(spans)


async def test_first_sync_asks_every_symbol_and_is_then_done(session, run, fake, connector) -> None:
    pairs = [("AAA", "BBB"), ("CCC", "STB")]
    fake.add(
        "exchange-info",
        *[build_row("exchange-info", symbol=b + q, baseAsset=b, quoteAsset=q) for b, q in pairs],
    )
    fake.add("my-trades", build_row("my-trades", 0, time=NOW_MS - DAY_MS, quoteQty="2.25"))
    config = later_config()
    del config["first_sync_done"]
    connection = await binance_connection(run, config=config)

    after = await run_binance_sync(session, connection.id, fake.handler)

    assert after.status == "healthy", after.last_error
    assert sorted(s.params["symbol"] for s in fake.calls("my-trades")) == ["AAABBB", "CCCSTB"]
    assert after.config["first_sync_done"] is True
    assert after.config[CURSORS_KEY]["my-trades:AAABBB"] == 1000


async def test_a_later_sync_asks_the_symbols_whose_assets_it_has_seen(
    session, run, fake, connector
) -> None:
    pairs = [("AAA", "BBB"), ("CCC", "STB"), ("AAA", "STB")]
    fake.add(
        "exchange-info",
        *[build_row("exchange-info", symbol=b + q, baseAsset=b, quoteAsset=q) for b, q in pairs],
    )
    fake.add("my-trades", build_row("my-trades", 0, time=NOW_MS - DAY_MS, quoteQty="2.25"))
    connection = await binance_connection(run, config=later_config())
    other = await binance_connection(run, config=later_config())
    for owner, code in ((connection.id, "AAA"), (connection.id, "STB"), (other.id, "CCC")):
        await session.execute(
            text("insert into finance.binance_assets_seen (connection_id, asset) values (:c, :a)"),
            {"c": owner, "a": code},
        )

    after = await run_binance_sync(session, connection.id, fake.handler)

    assert after.status == "healthy", after.last_error
    # CCC was seen by another connection only; AAA and STB are both seen here.
    assert sorted(s.params["symbol"] for s in fake.calls("my-trades")) == ["AAASTB"]


# "Sync now": the owner's request reaches the sync.


@pytest.fixture
def queue() -> Any:
    with procrastinate_app.replace_connector(InMemoryConnector()) as app:
        yield app.connector


@pytest.fixture
async def api(session: AsyncSession, queue) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        database_url="postgresql+psycopg://unused@localhost/unused",
        auth_mode="tailscale",
        owner_login="me@example.com",
        master_key=SecretStr(base64.b64encode(KEY).decode()),
    )
    app = create_app(settings)

    async def _test_session() -> AsyncIterator[AsyncSession]:
        yield session

    sessions = async_sessionmaker(bind=session.bind, join_transaction_mode="create_savepoint")
    app.dependency_overrides[get_session] = _test_session
    app.dependency_overrides[get_session_factory] = lambda: sessions
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_sync_now_runs_inside_the_four_hours(
    session, run, fake, connector, clock, api
) -> None:
    connection = await binance_connection(run, config=later_config())
    first = await run_binance_sync(session, connection.id, fake.handler)
    assert first.status == "healthy", first.last_error
    fake.requests.clear()

    response = await api.post(f"/api/v1/connections/{connection.id}/sync", headers=OWNER)
    assert response.status_code == 202
    clock.ms += MINUTE_MS
    started = clock.ms
    await run_binance_sync(session, connection.id, fake.handler)

    asked = {seen.label for seen in fake.requests}
    assert {"api-restrictions", "spot-account", "funding-asset", "wallet-balance"} <= asked
    after = await get_connection(session, connection.id)
    assert "sync_requested" not in after.config
    assert after.cursor == {"fetched_at": started}

    # A scheduled tick at that moment, with no request, asks for nothing.
    fake.requests.clear()
    await run_binance_sync(session, connection.id, fake.handler)
    assert fake.requests == []


async def test_request_survives_a_failed_sync(session, run, fake, connector, clock) -> None:
    connection = await binance_connection(
        run, config=later_config(), cursor={"fetched_at": NOW_MS - HOUR_MS}
    )
    await run(request_sync, connection.id)

    def unreachable(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/account":
            raise httpx.ConnectError("unreachable")
        return fake.handler(request)

    failed = await run_binance_sync(session, connection.id, unreachable)
    assert failed.status == "failing"
    assert failed.config["sync_requested"] is True

    # The next tick, with no new request, still runs the sync the owner asked for.
    clock.ms += MINUTE_MS
    fake.requests.clear()
    after = await run_binance_sync(session, connection.id, fake.handler)
    assert after.status == "healthy", after.last_error
    assert "spot-account" in {seen.label for seen in fake.requests}
    assert "sync_requested" not in after.config


async def test_sync_now_leaves_ibkr_as_it_is(session, run) -> None:
    cursor: JSONValue = {
        "history_from": "2025-11-18",
        "fetched_to": "2026-11-17",
        "fetched_at": datetime.now(UTC).isoformat(),
    }

    async def setup(ctx):
        connection = await create_connection(
            ctx, connector="ibkr", display_name="IBKR", config={"query_id": "111111"}
        )
        await store_credential(ctx, KEY, connection.id, "flex_token", "tok-TEST", None)
        await ctx.session.execute(
            text(
                "update core.connections set status = 'healthy', cursor = cast(:c as jsonb)"
                " where id = :id"
            ),
            {"c": json.dumps(cursor), "id": connection.id},
        )
        return connection

    connection = await run(setup)
    await run(request_sync, connection.id)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request to {request.url.path}")

    after = await run_binance_sync(session, connection.id, refuse)

    assert after.status == "healthy"
    assert after.cursor == cursor


# A first sync that is interrupted continues where it stopped. Real commits through their own
# engine: each window commits on its own, outside the sync's transaction, which rolls back.


async def test_first_sync_resumes(
    backend_url: str, owner_url: str, migrated: None, fake: FakeBinance, connector, clock: Clock
) -> None:
    engine = create_engine(backend_url)
    sessions = make_sessionmaker(engine)
    owner = Actor("user", "dev")
    connection_id: uuid.UUID | None = None
    interrupted = HISTORY_LABELS[4]
    fake.add(interrupted, build_row(interrupted, 0, time=NOW_MS - 400 * DAY_MS))
    asked: list[int] = []
    before = await _committed_before(owner_url)

    def failing(request: httpx.Request) -> httpx.Response:
        if request.url.path == ENDPOINTS[interrupted].path:
            asked.append(1)
            if len(asked) == 3:
                raise httpx.ConnectError("unreachable")
        return fake.handler(request)

    try:
        async with sessions() as setup, command_context(setup, owner, "test.setup", {}) as ctx:
            connection = await create_connection(
                ctx,
                connector="binance",
                display_name=f"Binance {uuid.uuid4()}",
                config={"key_type": "hmac", "uid": UID},
            )
            connection_id = connection.id
            await store_credential(ctx, KEY, connection_id, "api_key", API_KEY, None)
            await store_credential(ctx, KEY, connection_id, "api_secret", API_SECRET, None)
            await ctx.session.execute(
                text("update core.connections set status = 'healthy' where id = :id"),
                {"id": connection_id},
            )

        async with sessions() as session:
            failed = await run_binance_sync(
                session, connection_id, failing, session_factory=sessions
            )
        assert failed.status == "failing"
        cursors = failed.config[CURSORS_KEY]
        assert all(cursors[label]["complete"] for label in HISTORY_LABELS[:4])
        stopped = cursors[interrupted]
        assert stopped["complete"] is False
        assert all(label not in cursors for label in HISTORY_LABELS[5:])
        first_windows = _windows(fake)

        fake.requests.clear()
        clock.ms += MINUTE_MS
        async with sessions() as session:
            after = await run_binance_sync(
                session, connection_id, fake.handler, session_factory=sessions
            )
        assert after.status == "healthy", after.last_error
        again = [
            s
            for s in fake.calls(interrupted)
            if int(s.params["endTime"]) >= stopped["backfilled_to"]
        ]
        assert again == []
        assert fake.calls(interrupted)
        assert _windows(fake) & first_windows == set()
        assert after.config[CURSORS_KEY][interrupted]["complete"] is True
        stored = await _count(sessions, connection_id, record_type(interrupted))
        assert stored == 1
    finally:
        if connection_id is not None:
            await _delete(engine, owner_url, connection_id, before)
        await engine.dispose()


async def test_a_flag_is_told_after_a_sync_that_failed_later(
    backend_url: str,
    owner_url: str,
    migrated: None,
    fake: FakeBinance,
    connector,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Committed for real: the normaliser's run commits on its own, the sync's rolls back.
    engine = create_engine(backend_url)
    sessions = make_sessionmaker(engine)
    connection_id: uuid.UUID | None = None
    before = await _committed_before(owner_url)
    at = NOW_MS - 2 * HOUR_MS
    row = build_row("deposits", status=1, walletType=0, insertTime=at, completeTime=at, coin="AAA")
    fake.add("deposits", {**row, "extraField": "secret-value-4242"})
    flag = "deposits.extraField (a field the catalogue does not list)"
    notifier = RecordingNotifier()
    real_opening = sync_module.opening

    async def broken(ctx) -> None:
        raise RuntimeError("a later step failed")

    async def sync() -> Any:
        async with sessions() as session:
            return await run_binance_sync(
                session, connection_id, fake.handler, notifier=notifier, session_factory=sessions
            )

    try:
        async with (
            sessions() as setup,
            command_context(setup, Actor("user", "dev"), "test.setup", {}) as ctx,
        ):
            connection = await create_connection(
                ctx,
                connector="binance",
                display_name=f"Binance {uuid.uuid4()}",
                config={"key_type": "hmac", "uid": UID, **later_config()},
            )
            connection_id = connection.id
            await store_credential(ctx, KEY, connection_id, "api_key", API_KEY, None)
            await store_credential(ctx, KEY, connection_id, "api_secret", API_SECRET, None)
            await ctx.session.execute(
                text("update core.connections set status = 'healthy' where id = :id"),
                {"id": connection_id},
            )
        monkeypatch.setattr(sync_module, "opening", broken)

        failed = await sync()

        assert failed.status == "failing"
        # The run that found the flag committed it as known; nothing was told yet.
        assert failed.config["flags"] == [flag]
        assert notifier.messages == []

        monkeypatch.setattr(sync_module, "opening", real_opening)
        clock.ms += 5 * HOUR_MS
        told = await sync()
        clock.ms += 5 * HOUR_MS
        again = await sync()

        assert told.status == again.status == "healthy", again.last_error
        # Besides the worker's own "has recovered" message.
        [message] = [m for m in notifier.messages if "flagged" in m]
        assert flag in message and "secret-value" not in message
        assert "pending_flags" not in again.config
    finally:
        if connection_id is not None:
            await _delete(engine, owner_url, connection_id, before)
        await engine.dispose()


def _windows(fake: FakeBinance) -> set[tuple[str, str, str, str]]:
    """The history windows asked: label, variant, start, end."""
    return {
        (s.label, s.params.get("type", ""), s.params["startTime"], s.params["endTime"])
        for s in fake.requests
        if s.label in HISTORY_LABELS
    }


async def _count(sessions, connection_id: uuid.UUID, record_type: str) -> int:
    async with sessions() as session:
        return await session.scalar(
            text(
                "select count(*) from core.raw_records"
                " where connection_id = :id and record_type = :t"
            ),
            {"id": connection_id, "t": record_type},
        )


_SYSTEM = (
    "select key, value #>> '{}' as id from core.settings where key like 'finance.system_account.%'"
)


@dataclass(frozen=True)
class Before:
    """What was committed when a test that commits for real began: the database's clock then,
    and the system accounts' setting keys."""

    at: datetime
    system_accounts: frozenset[str]


async def _committed_before(owner_url: str) -> Before:
    owner = create_engine(owner_url)
    try:
        async with owner.connect() as conn:
            at = await conn.scalar(text("select clock_timestamp()"))
            keys = frozenset(row.key for row in await conn.execute(text(_SYSTEM)))
            assert isinstance(at, datetime)
            return Before(at, keys)
    finally:
        await owner.dispose()


async def _delete(engine, owner_url: str, connection_id: uuid.UUID, before: Before) -> None:
    """Everything the test committed: the connection's rows, and what its syncs made that is
    no connection's (instruments, entities, system accounts, every event and audit entry)."""
    params = {"id": connection_id}
    since = {"at": before.at}
    owner = create_engine(owner_url)
    try:
        # Raw records and the audit log are append-only for the backend role.
        async with owner.begin() as conn:
            await conn.execute(
                text("delete from core.raw_records where connection_id = :id"), params
            )
            # Every log row written since the test began: no other test commits meanwhile.
            await conn.execute(text("delete from core.audit_log where occurred_at >= :at"), since)
            await conn.execute(text("delete from core.events where occurred_at >= :at"), since)
            await conn.execute(
                text("delete from core.credentials where connection_id = :id"), params
            )
            # The typed tables (child rows go with their parent), and what the reports wrote.
            # (The bars are market data: no connection's.)
            roots = [
                t.name
                for label in TABLE_LABELS
                for t in layout(label)
                if t.parent is None and label != "klines"
            ]
            for table in [*roots, "binance_assets_seen"]:
                await conn.execute(
                    text(f"delete from finance.{table} where connection_id = :id"), params
                )
            # What the records posted.
            posted = "select id from finance.transactions where connection_id = :id"
            await conn.execute(
                text(f"delete from finance.postings where transaction_id in ({posted})"), params
            )
            await conn.execute(
                text("delete from finance.transactions where connection_id = :id"), params
            )
            accounts = "select id from finance.accounts where source_connection_id = :id"
            await conn.execute(
                text(f"delete from finance.balance_snapshots where account_id in ({accounts})"),
                params,
            )
            await conn.execute(
                text("delete from finance.binance_account_reports where connection_id = :id"),
                params,
            )
            await conn.execute(
                text(
                    "delete from finance.binance_reconciliation_differences"
                    f" where account_id in ({accounts})"
                ),
                params,
            )
            await conn.execute(
                text("delete from finance.accounts where source_connection_id = :id"), params
            )
            await conn.execute(text("delete from core.connections where id = :id"), params)
            # The system accounts the normaliser made for this test (committed, unlike others').
            for row in (await conn.execute(text(_SYSTEM))).all():
                if row.key in before.system_accounts:
                    continue
                made = {"id": uuid.UUID(row.id), "key": row.key}
                await conn.execute(text("delete from finance.accounts where id = :id"), made)
                await conn.execute(text("delete from core.settings where key = :key"), made)
            # The instruments (their identifiers go with them) and entities it made.
            await conn.execute(
                text("delete from finance.instruments where created_at >= :at"), since
            )
            await conn.execute(text("delete from core.entities where created_at >= :at"), since)
    finally:
        await owner.dispose()


# Whatever the parser finds unknown in any answer the sync reads is flagged.


async def test_an_unknown_field_in_any_answer_is_flagged_once(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    fake.set_rows("spot-account", [build_row("spot-account", asset="AAA", free="5", locked="0")])
    fake.add(
        "exchange-info",
        build_row("exchange-info", symbol="AAAEUR", baseAsset="AAA", quoteAsset="EUR"),
    )
    fake.add(
        "coin-config", {**build_row("coin-config", coin="AAA", isLegalMoney=False), "rowExtra": "v"}
    )
    bar = build_row("klines", **{"0": NOW_MS // DAY_MS * DAY_MS})
    fake.add("klines", {**bar, str(len(bar)): "v"}, when={"symbol": "AAAEUR"})
    # Beside the records or the balances: the answer's own top-level fields.
    extra = {"/api/v3/account": "accountExtra", "/sapi/v1/convert/tradeFlow": "pageExtra"}

    def handler(request: httpx.Request) -> httpx.Response:
        answer = fake.handler(request)
        name = extra.get(request.url.path)
        if name is None or answer.status_code != 200:
            return answer
        body = json.loads(answer.content)
        return httpx.Response(200, content=json.dumps({**body, name: "v"}).encode())

    connection = await binance_connection(run, config=later_config())
    notifier = RecordingNotifier()

    # Bars are asked once the asset was held a whole day; the last sync sees it all again.
    for step in (0, DAY_MS, 5 * HOUR_MS):
        clock.ms += step
        after = await run_binance_sync(session, connection.id, handler, notifier=notifier)

    assert after.status == "healthy", after.last_error
    assert fake.calls("klines")
    unknown = "(a field the catalogue does not list)"
    expected = {
        f"spot-account.accountExtra {unknown}",
        f"convert.pageExtra {unknown}",
        f"coin-config.rowExtra {unknown}",
        f"klines.[][{len(bar)}] {unknown}",
    }
    assert expected <= set(after.config["flags"])  # pyright: ignore[reportArgumentType]
    # Each told once.
    told = [m for m in notifier.messages if "flagged" in m]
    assert all(sum(flag in m for m in told) == 1 for flag in expected)


# Every Phase A label is fetched by a sync.


async def test_a_first_sync_asks_every_phase_a_label(
    session, run, fake: FakeBinance, connector
) -> None:
    # Enough for every step to ask: an asset held since a daily snapshot two days ago, with a
    # EUR market (bars) and a symbol of its own (trades).
    day = NOW_MS - 2 * DAY_MS - 1_000
    snapshot = build_row("snapshot", type="spot", updateTime=day)
    data = dict(snapshot["data"])  # pyright: ignore[reportArgumentType,reportCallIssue]
    data["balances"] = [{"asset": "AAA", "free": "2", "locked": "0"}]
    fake.add("snapshot", {**snapshot, "data": data})
    fake.set_rows("spot-account", [build_row("spot-account", asset="AAA", free="2", locked="0")])
    fake.add(
        "exchange-info",
        build_row("exchange-info", symbol="AAAEUR", baseAsset="AAA", quoteAsset="EUR"),
    )
    connection = await binance_connection(run)

    after = await run_binance_sync(session, connection.id, fake.handler)

    assert after.status == "healthy", after.last_error
    phase_a = {label for label, endpoint in ENDPOINTS.items() if endpoint.phase == "A"}
    asked = {seen.label for seen in fake.requests}
    assert sorted(phase_a - asked) == []
    assert await session.scalar(
        text("select count(*) from finance.binance_asset_detail where connection_id = :c"),
        {"c": connection.id},
    )


# A row that cannot be typed is never stored: every stored raw record can be normalised.


async def _raw(session, connection_id, label: str) -> int:
    return await session.scalar(
        text("select count(*) from core.raw_records where connection_id = :c and record_type = :t"),
        {"c": connection_id, "t": record_type(label)},
    )


async def test_a_history_row_that_cannot_be_typed_is_not_stored(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    moment = NOW_MS - 2 * HOUR_MS
    row = build_row("deposits", coin="AAA", status=1, walletType=0, insertTime=moment)
    fake.add("deposits", {**row, "completeTime": 10**20})
    fake.set_rows("spot-account", [build_row("spot-account", asset="AAA", free="1.5", locked="0")])
    connection = await binance_connection(run, config=later_config())
    before = connection.config[CURSORS_KEY]["deposits"]  # pyright: ignore[reportArgumentType,reportCallIssue,reportIndexIssue]

    after = await run_binance_sync(session, connection.id, fake.handler)

    assert after.status == "failing"
    assert "deposits" in (after.last_error or "") and "[].completeTime" in (after.last_error or "")
    assert str(10**20) not in (after.last_error or "")
    assert await _raw(session, connection.id, "deposits") == 0
    assert after.config[CURSORS_KEY]["deposits"] == before  # pyright: ignore[reportArgumentType,reportCallIssue,reportIndexIssue]

    # Binance answers a readable row: the next sync stores it and normalises cleanly.
    fake.set_rows("deposits", [{**row, "completeTime": moment}])
    clock.ms += 4 * HOUR_MS
    again = await run_binance_sync(session, connection.id, fake.handler)

    assert again.status == "healthy", again.last_error
    assert await _raw(session, connection.id, "deposits") == 1
    posted = await session.scalar(
        text("select count(*) from finance.transactions where connection_id = :c"),
        {"c": connection.id},
    )
    assert posted >= 1


async def test_a_balance_answer_that_cannot_be_typed_is_not_stored(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    connection = await binance_connection(run, config=later_config())
    broken = {"on": True}

    def handler(request: httpx.Request) -> httpx.Response:
        answer = fake.handler(request)
        if request.url.path != "/api/v3/account" or not broken["on"]:
            return answer
        body = json.loads(answer.content)
        return httpx.Response(200, content=json.dumps({**body, "updateTime": 10**20}).encode())

    after = await run_binance_sync(session, connection.id, handler)

    assert after.status == "failing"
    assert "spot-account" in (after.last_error or "") and "updateTime" in (after.last_error or "")
    assert await _raw(session, connection.id, "spot-account") == 0

    broken["on"] = False
    clock.ms += 4 * HOUR_MS
    again = await run_binance_sync(session, connection.id, handler)

    assert again.status == "healthy", again.last_error
    assert await _raw(session, connection.id, "spot-account") >= 1


# A fiat currency Lararium has no instrument for is left out and flagged, never failing a sync.


async def test_a_fiat_currency_lararium_lacks_is_not_counted_and_the_sync_goes_on(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    from lararium.modules.finance.binance.reconcile import asset_code, open_differences

    fake.add("coin-config", build_row("coin-config", coin="XFA", isLegalMoney=True))
    fake.add("coin-config", build_row("coin-config", coin="AAA", isLegalMoney=False))
    fake.add(
        "exchange-info",
        build_row("exchange-info", symbol="AAAXFA", baseAsset="AAA", quoteAsset="XFA"),
    )
    # A day holding both, and the first sync's read; then 1 AAA bought with XFA (its
    # commission in XFA too) before the second sync.
    day = NOW_MS - 2 * DAY_MS - 1_000
    snapshot = build_row("snapshot", type="spot", updateTime=day)
    data = dict(snapshot["data"])  # pyright: ignore[reportArgumentType,reportCallIssue]
    data["balances"] = [
        {"asset": "AAA", "free": "2", "locked": "0"},
        {"asset": "XFA", "free": "500", "locked": "0"},
    ]
    fake.add("snapshot", {**snapshot, "data": data})

    def spot(aaa: str, xfa: str) -> None:
        fake.set_rows(
            "spot-account",
            [
                build_row("spot-account", asset="AAA", free=aaa, locked="0"),
                build_row("spot-account", asset="XFA", free=xfa, locked="0"),
            ],
        )

    spot("2", "500")
    connection = await binance_connection(run)

    first = await run_binance_sync(session, connection.id, fake.handler)

    assert first.status == "healthy", first.last_error
    assert first.config.get("first_sync_done") is True
    flag = "XFA is a currency Lararium does not have: it is not counted"
    flags = first.config["flags"]
    assert isinstance(flags, list) and flags.count(flag) == 1
    assert not any("has a balance but lists no asset" in f for f in flags)  # pyright: ignore[reportOperatorIssue]
    assert not any("disagrees" in f for f in flags)  # pyright: ignore[reportOperatorIssue]

    clock.ms += 4 * HOUR_MS
    trade = build_row(
        "my-trades",
        symbol="AAAXFA",
        isBuyer=True,
        qty="1",
        price="100",
        quoteQty="100",
        commission="0.5",
        commissionAsset="XFA",
        time=clock.ms - HOUR_MS,
    )
    fake.add("my-trades", trade)
    spot("3", "399.5")
    fake.requests.clear()

    second = await run_binance_sync(session, connection.id, fake.handler)

    assert second.status == "healthy", second.last_error
    assert second.config["flags"] == flags
    assert "no price for XFA" not in flags
    # The trade is stored typed and not posted: no one-sided transaction.
    typed = await session.scalar(
        text("select count(*) from finance.binance_my_trades where connection_id = :c"),
        {"c": connection.id},
    )
    assert typed == 1
    posted = await session.scalar(
        text("select count(*) from finance.transactions where external_id like 'trade:%'")
    )
    assert posted == 0
    # No XFA instrument, so no XFA snapshot; the day was written for AAA.
    symbols = await session.scalars(
        text(
            "select distinct i.symbol from finance.balance_snapshots s"
            " join finance.instruments i on i.id = s.instrument_id"
            " join finance.accounts a on a.id = s.account_id where a.source_connection_id = :c"
        ),
        {"c": connection.id},
    )
    assert set(symbols) == {"AAA"}
    # The other side of the unposted trade is the visible consequence: a difference on AAA,
    # which sends for the sweep once.
    differences = await open_differences(session, connection.id)
    assert {await asset_code(session, d.instrument_id) for d in differences} == {"AAA"}
    assert any("startTime" in seen.params for seen in fake.calls("my-trades"))

    fake.requests.clear()
    clock.ms += 4 * HOUR_MS
    third = await run_binance_sync(session, connection.id, fake.handler)

    assert third.status == "healthy", third.last_error
    assert third.config["flags"] == flags
    assert not any("startTime" in seen.params for seen in fake.calls("my-trades"))
