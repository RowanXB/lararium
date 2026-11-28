"""BTC prices without a Binance connection (design 5.7): Binance's public daily bars price the
BTC a wallet holds, from its first day held, under the wallet's own connection; the bars and
the prices are shared with Binance's connections. Every date and price here is invented."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from functools import partial
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.core.connections import (
    create_connection,
    get_connection,
    record_sync_success,
    store_credential,
)
from lararium.core.connectors import SyncContext
from lararium.db import create_engine, make_sessionmaker
from lararium.modules.finance.bitcoin.derive import build_wallet
from lararium.modules.finance.bitcoin.keys import WalletInput, parse_input
from lararium.modules.finance.bitcoin.prices import update_btc_prices
from lararium.modules.finance.bitcoin.sync import (
    DESCRIPTOR_PURPOSE,
    FLAGGED_PREFIX,
    BitcoinConnector,
    input_json,
)
from lararium.modules.finance.instruments import find_by_identifier, get_currency
from lararium.modules.finance.pricing import record_price
from lararium.modules.finance.valuation import holdings_asof
from tests.finance.binance.fixtures import DAY_MS, HOUR_MS, FakeBinance, build_row
from tests.finance.binance.syncing import Clock

from .fake_esplora import BASE, FakeEsplora, make_tx
from .test_fetch import CONNECTOR, KEY, receiving
from .test_normalize import Synced
from .test_sync import Notifier, Wallet

ADDRESSES = WalletInput("addresses", addresses=(receiving(0),))
LISTED = build_wallet(parse_input(ADDRESSES))
NO_PRICE = "no price for BTC"


def _ms(day: date) -> int:
    return int(datetime.combine(day, time(), UTC).timestamp()) * 1000


def _utc(day: date) -> datetime:
    return datetime.combine(day, time(), UTC)


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


def _receipt(n: int, day: date, sats: int = 100_000) -> dict[str, Any]:
    """A confirmed receipt to the wallet, its block stamped at noon of `day`."""
    tx = make_tx(n, [(receiving(0), sats)])
    tx["status"]["block_time"] = int(datetime.combine(day, time(12), UTC).timestamp())
    return tx


def _binance(clock: Clock, bars: Mapping[date, str]) -> FakeBinance:
    """Binance listing BTCEUR (and a pair the wallet never needs), with these daily closes."""
    fake = FakeBinance(now=clock)
    fake.add(
        "exchange-info",
        build_row("exchange-info", symbol="BTCEUR", baseAsset="BTC", quoteAsset="EUR"),
        build_row("exchange-info", symbol="AAAEUR", baseAsset="AAA", quoteAsset="EUR"),
    )
    rows = [
        build_row("klines", **{"0": _ms(d), "4": close, "6": _ms(d) + DAY_MS - 1})
        for d, close in bars.items()
    ]
    fake.add("klines", *rows, when={"symbol": "BTCEUR"})
    return fake


async def _holding(session: AsyncSession, *receipts: date) -> tuple[Synced, uuid.UUID]:
    """A wallet (no Binance connection anywhere) holding BTC from the receipts' days."""
    chain = FakeEsplora()
    for n, day in enumerate(receipts, 1):
        chain.add(_receipt(n, day))
    synced = await Synced.start(session, chain)
    await synced.sync(LISTED)
    return synced, await synced.account_id()


@asynccontextmanager
async def _pricing(synced: Synced, fake: FakeBinance) -> AsyncIterator[SyncContext]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as http:
        yield await synced.context(http)


async def _prices(session: AsyncSession) -> dict[date, Decimal]:
    """BTC's `binance` prices in EUR, found by its chain identifier."""
    rows = await session.execute(
        text(
            "select p.as_of_date, p.price from finance.prices p"
            " join finance.instrument_identifiers i on i.instrument_id = p.instrument_id"
            " where i.scheme = 'chain_native' and i.value = 'bitcoin' and p.source = 'binance'"
            " and p.quote_instrument_id = (select instrument_id from"
            "  finance.instrument_identifiers where scheme = 'iso4217' and value = 'EUR')"
        )
    )
    return {r.as_of_date: r.price for r in rows}


async def test_wallet_only_btc_is_priced_from_first_day(session) -> None:
    first, last = date(2024, 3, 1), date(2024, 3, 5)
    synced, account = await _holding(session, first)
    clock = Clock(_ms(last + timedelta(days=1)) + 3 * HOUR_MS)
    bars = {d: f"{50_000 + n}.5" for n, d in enumerate(_days(first - timedelta(days=3), last))}
    # The day still open has a bar too: it is never stored or priced.
    fake = _binance(clock, {**bars, last + timedelta(days=1): "1"})

    async with _pricing(synced, fake) as ctx:
        flags = await update_btc_prices(ctx, account, clock=clock, sleep=clock.sleep)

    assert flags == []
    connectors = await session.scalars(text("select connector from core.connections"))
    assert list(connectors) == ["bitcoin"]
    assert await _prices(session) == {d: Decimal(bars[d]) for d in _days(first, last)}
    # One instrument wherever BTC is held: the one Binance's finder and pricer use.
    btc = await find_by_identifier(session, "binance_asset", "BTC")
    assert btc is not None
    assert btc == await find_by_identifier(session, "chain_native", "bitcoin")
    # The days asked are kept in the wallet's own config, as a Binance connection keeps its.
    config = (await get_connection(session, synced.connection.id)).config
    assert config["bars_asked"] == {"BTCEUR": [first.isoformat(), last.isoformat()]}
    # Only the pair BTC's path needs, from the first day held; no signed query (no key).
    assert [(s.label, s.params.get("symbol")) for s in fake.requests] == [
        ("time", None),
        ("exchange-info", None),
        ("klines", "BTCEUR"),
    ]
    assert fake.calls("klines")[0].params["startTime"] == str(_ms(first))


async def test_days_before_eur_markets_are_missing(session) -> None:
    first, listed, last = date(2019, 6, 1), date(2020, 1, 3), date(2020, 1, 9)
    synced, account = await _holding(session, first)
    clock = Clock(_ms(last + timedelta(days=1)) + HOUR_MS)
    bars = {d: "7000" for d in _days(listed, last)}
    fake = _binance(clock, bars)

    for _ in range(2):
        async with _pricing(synced, fake) as ctx:
            flags = await update_btc_prices(ctx, account, clock=clock, sleep=clock.sleep)
        # Yesterday is priced, but the days before Binance's EUR market are not: one flag.
        assert flags == [NO_PRICE]

    assert await _prices(session) == {d: Decimal("7000") for d in _days(listed, last)}
    before = [h for h in await holdings_asof(session, date(2019, 12, 1)) if h.symbol == "BTC"]
    assert [(h.status, h.value) for h in before] == [("missing_price", None)]
    # Asked once: the range with no bar is not asked again.
    assert len(fake.calls("klines")) == 1


async def test_same_day_is_not_written_twice(session, run) -> None:
    first, last = date(2024, 3, 1), date(2024, 3, 5)
    synced, account = await _holding(session, first)
    clock = Clock(_ms(last + timedelta(days=1)) + HOUR_MS)
    bars = {d: f"{60_000 + n}" for n, d in enumerate(_days(first, last))}
    btc = await find_by_identifier(session, "binance_asset", "BTC")
    assert btc is not None
    eur = await get_currency(session, "EUR")
    shared = date(2024, 3, 3)
    # A Binance connection priced that day already, from the same bar.
    await run(
        record_price,
        instrument_id=btc.id,
        quote_instrument_id=eur.id,
        price=Decimal(bars[shared]),
        as_of_date=shared,
        source="binance",
    )
    before = await _rows(session, shared)

    async with _pricing(synced, _binance(clock, bars)) as ctx:
        await update_btc_prices(ctx, account, clock=clock, sleep=clock.sleep)

    assert await _rows(session, shared) == before
    assert len(before) == 1
    assert await _prices(session) == {d: Decimal(c) for d, c in bars.items()}


async def _rows(session: AsyncSession, day: date) -> list[Any]:
    rows = await session.execute(
        text("select id, recorded_at from finance.prices where as_of_date = :d"), {"d": day}
    )
    return list(rows)


async def test_nothing_held_asks_binance_nothing(session) -> None:
    synced = await Synced.start(session, FakeEsplora())
    await synced.sync(LISTED)
    clock = Clock(_ms(date(2024, 3, 6)))
    fake = _binance(clock, {})

    async with _pricing(synced, fake) as ctx:
        assert await update_btc_prices(ctx, await synced.account_id(), clock=clock) == []
    assert fake.requests == []


# Through the connector: priced in the sync, its flag published with the others.


def _routed(chain: FakeEsplora, fake: FakeBinance) -> Any:
    def handler(request: httpx.Request) -> Any:
        if request.url.host == "api.binance.com":
            return fake.handler(request)
        return chain.handler(request)

    return handler


async def test_a_sync_prices_btc_and_publishes_its_flag_once(session) -> None:
    first, listed, last = date(2019, 12, 30), date(2020, 1, 3), date(2020, 1, 9)
    chain = FakeEsplora()
    chain.add(_receipt(1, first))
    clock = Clock(_ms(last + timedelta(days=1)) + HOUR_MS)
    fake = _binance(clock, {d: "7000" for d in _days(listed, last)})
    wallet = await Wallet.create(session, chain, ADDRESSES)
    wallet.prices = partial(update_btc_prices, clock=clock, sleep=clock.sleep)
    wallet.handler = _routed(chain, fake)

    # The first sync posts the receipt; BTC is priced from the next one on.
    await wallet.run()
    assert fake.requests == []
    for _ in range(2):
        await wallet.run(requested=True)
        assert await wallet.warnings() == [FLAGGED_PREFIX + NO_PRICE]
    assert [m for m in wallet.notifier.messages if NO_PRICE in m] == [
        f"Bitcoin wallet: {FLAGGED_PREFIX}{NO_PRICE}"
    ]
    assert set(await _prices(session)) == set(_days(listed, last))

    # The owner prices the days Binance cannot: the flag clears.
    btc = await find_by_identifier(session, "chain_native", "bitcoin")
    eur = await get_currency(session, "EUR")
    assert btc is not None
    async with command_context(session, Actor("user", "test"), "test", {}) as cmd:
        for day in _days(first, listed - timedelta(days=1)):
            await record_price(
                cmd,
                instrument_id=btc.id,
                quote_instrument_id=eur.id,
                price=Decimal("6500"),
                as_of_date=day,
                source="manual",
            )
    await wallet.run(requested=True)
    assert await wallet.warnings() == []


async def test_a_pricing_failure_keeps_the_flag_and_the_sync(session) -> None:
    first, listed, last = date(2019, 12, 30), date(2020, 1, 3), date(2020, 1, 9)
    chain = FakeEsplora()
    chain.add(_receipt(1, first))
    clock = Clock(_ms(last + timedelta(days=1)) + HOUR_MS)
    fake = _binance(clock, {d: "7000" for d in _days(listed, last)})
    wallet = await Wallet.create(session, chain, ADDRESSES)
    wallet.prices = partial(update_btc_prices, clock=clock, sleep=clock.sleep)
    wallet.handler = _routed(chain, fake)
    await wallet.run()
    await wallet.run(requested=True)
    assert await wallet.warnings() == [FLAGGED_PREFIX + NO_PRICE]

    # Binance fails (401 on a public query too): the sync goes on, and the flag stands.
    fake.fail("exchange-info", status=401, code=-2015)
    cursor = await wallet.run(requested=True)

    assert isinstance(cursor, dict)
    assert (await wallet.record()).status != "reauth_required"
    assert await wallet.warnings() == [FLAGGED_PREFIX + NO_PRICE]


# Committed for real: the price step commits on its own (its record of the days asked under
# the connection's row lock), so it must not wait on the lock the sync holds.


async def test_a_sync_with_prices_completes(
    backend_url: str, owner_url: str, migrated: None
) -> None:
    engine = create_engine(backend_url)
    sessions = make_sessionmaker(engine)
    # Days no other test commits a bar for: the bars committed here are deleted afterwards.
    first, last = date(2023, 5, 1), date(2023, 5, 4)
    clock = Clock(_ms(last + timedelta(days=1)) + HOUR_MS)
    fake = _binance(clock, {d: "25000" for d in _days(first, last)})
    chain = FakeEsplora()
    chain.add(_receipt(1, first))
    connector = BitcoinConnector(
        now=lambda: datetime.fromtimestamp(chain.txs[0]["status"]["block_time"] + 3600, UTC),
        sleep=chain.sleep,
        clock=chain.clock,
        prices=partial(update_btc_prices, clock=clock, sleep=clock.sleep),
    )
    before = await _committed_before(owner_url)
    connection_id: uuid.UUID | None = None
    try:
        async with (
            sessions() as setup,
            command_context(setup, Actor("user", "dev"), "test.setup", {}) as ctx,
        ):
            connection = await create_connection(
                ctx, connector="bitcoin", display_name=f"Bitcoin {uuid.uuid4()}", config={}
            )
            connection_id = connection.id
            await store_credential(
                ctx, KEY, connection_id, DESCRIPTOR_PURPOSE, input_json(ADDRESSES), None
            )
        found = connection_id

        async def sync() -> None:
            args = {"connection_id": str(found)}
            async with (
                httpx.AsyncClient(transport=httpx.MockTransport(_routed(chain, fake))) as http,
                sessions() as session,
                command_context(session, CONNECTOR, "connection.sync", args) as cmd,
            ):
                ctx = SyncContext(
                    esplora_url=BASE,
                    command=cmd,
                    connection=await get_connection(session, found),
                    sync_id=uuid.uuid4(),
                    master_key=KEY,
                    http=http,
                    notifier=Notifier(),
                    session_factory=sessions,
                    requested=True,
                )
                await record_sync_success(cmd, found, await connector.sync(ctx))

        # Without the order the sync keeps, the second would wait on its own lock for ever.
        async with asyncio.timeout(60):
            await sync()
            await sync()

        assert fake.calls("klines")
        async with sessions() as session:
            assert set(await _prices(session)) == set(_days(first, last))
            after = await get_connection(session, found)
        assert after.config["bars_asked"] == {"BTCEUR": [first.isoformat(), last.isoformat()]}
        assert after.warnings == []
    finally:
        if connection_id is not None:
            await _delete(owner_url, connection_id, before, bars=("BTCEUR", first, last))
        await engine.dispose()


_SYSTEM = (
    "select key, value #>> '{}' as id from core.settings where key like 'finance.system_account.%'"
)


async def _committed_before(owner_url: str) -> tuple[datetime, frozenset[str]]:
    """The database's clock when the test began, and the system accounts' setting keys."""
    owner = create_engine(owner_url)
    try:
        async with owner.connect() as conn:
            at = await conn.scalar(text("select clock_timestamp()"))
            keys = frozenset(row.key for row in await conn.execute(text(_SYSTEM)))
            assert isinstance(at, datetime)
            return at, keys
    finally:
        await owner.dispose()


async def _delete(
    owner_url: str,
    connection_id: uuid.UUID,
    before: tuple[datetime, frozenset[str]],
    *,
    bars: tuple[str, date, date],
) -> None:
    """Everything the test committed: the connection's rows (its chain tables go with it), what
    its syncs made that is no connection's, and the bars and prices it stored."""
    at, system_accounts = before
    params = {"id": connection_id}
    since = {"at": at}
    owner = create_engine(owner_url)
    try:
        async with owner.begin() as conn:
            await conn.execute(
                text("delete from finance.report_sightings where connection_id = :id"), params
            )
            await conn.execute(
                text("delete from core.raw_records where connection_id = :id"), params
            )
            # Every log row written since the test began: no other test commits meanwhile.
            await conn.execute(text("delete from core.audit_log where occurred_at >= :at"), since)
            await conn.execute(text("delete from core.events where occurred_at >= :at"), since)
            await conn.execute(
                text("delete from core.credentials where connection_id = :id"), params
            )
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
                text(
                    "delete from finance.account_identifier_holders"
                    f" where account_id in ({accounts})"
                ),
                params,
            )
            await conn.execute(
                text("delete from finance.account_identifiers where created_at >= :at"), since
            )
            await conn.execute(
                text("delete from finance.accounts where source_connection_id = :id"), params
            )
            await conn.execute(text("delete from core.connections where id = :id"), params)
            for row in (await conn.execute(text(_SYSTEM))).all():
                if row.key in system_accounts:
                    continue
                made = {"id": uuid.UUID(row.id), "key": row.key}
                await conn.execute(text("delete from finance.accounts where id = :id"), made)
                await conn.execute(text("delete from core.settings where key = :key"), made)
            made = "select id from finance.instruments where created_at >= :at"
            await conn.execute(
                text(f"delete from finance.prices where instrument_id in ({made})"), since
            )
            await conn.execute(
                text("delete from finance.instruments where created_at >= :at"), since
            )
            await conn.execute(text("delete from core.entities where created_at >= :at"), since)
            symbol, low, high = bars
            await conn.execute(
                text(
                    "delete from finance.binance_klines"
                    " where symbol = :s and open_time >= :low and open_time < :high"
                ),
                {"s": symbol, "low": _utc(low), "high": _utc(high + timedelta(days=1))},
            )
    finally:
        await owner.dispose()
