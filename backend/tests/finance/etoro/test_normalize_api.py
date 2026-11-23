"""The eToro normalizer, API side: accounts, instruments, opens and closes, eToro Money, balance
history, prices and the typed rows (design §5.5-5.10)."""

import hashlib
import json
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from lararium.core import models as core_models
from lararium.core.actors import Actor
from lararium.core.commands import CommandContext, command_context
from lararium.core.connections import (
    ConnectionRecord,
    create_connection,
    get_connection,
    update_config,
)
from lararium.core.connectors import SyncContext
from lararium.modules.finance.accounts import create_account
from lararium.modules.finance.errors import FinanceValidationError
from lararium.modules.finance.etoro import sync as etoro_sync
from lararium.modules.finance.etoro.fields import ENDPOINTS
from lararium.modules.finance.etoro.instruments import merge_instruments
from lararium.modules.finance.etoro.normalize import (
    NORMALIZER_VERSION,
    EtoroDataError,
    NormalizeReport,
    normalize,
)
from lararium.modules.finance.identifiers import assign_identifier
from lararium.modules.finance.instruments import (
    InstrumentIdentifier,
    create_instrument,
    get_currency,
)
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    find_unbalanced_transactions,
    post_transaction,
)
from lararium.modules.finance.pricing import record_price
from lararium.modules.finance.readonly_http import loads_decimal, record_json
from lararium.modules.finance.report_sightings import record_sighting
from lararium.modules.finance.system_accounts import system_account_ids
from lararium.modules.finance.transfers import heal_transfers, list_transfers, transfer_contra
from lararium.modules.finance.transfers.contra import eligible_records
from tests.finance.trading212.fixtures import isin

from .fixtures import (
    CASH_ACCOUNT,
    INSTRUMENT_ID,
    SYMBOL,
    TRADING_ACCOUNT,
    aggregate_body,
    balance_entry,
    balances_body,
    bank_item,
    breakdown_body,
    breakdown_position,
    candle_item,
    cash_item,
    dumps,
    history_body,
    instrument_meta,
    lookup_body,
    me_json,
    pnl_body,
    pnl_position,
    trade_row,
)

CONNECTOR = Actor("connector", "etoro")
NOW = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
POSITION = 3000000101
ORDER = POSITION + 500
TRADING = "eToro Trading"
MONEY = "eToro Money GBP"
BALANCES = ("balances", balances_body())
META = ("instruments", instrument_meta())


class Notifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> None:
        self.messages.append(text)


async def _connection(run) -> ConnectionRecord:
    return await run(
        lambda ctx: create_connection(ctx, connector="etoro", display_name="eToro", config={})
    )


def _refuse(request: httpx.Request) -> httpx.Response:
    raise AssertionError("no HTTP in this test")


class Ledger:
    """One connection's raw records, normalized as a sync does."""

    def __init__(self, session, connection: ConnectionRecord) -> None:
        self.session = session
        self.connection = connection
        self.notifier = Notifier()
        self.last: tuple[uuid.UUID, list[uuid.UUID]] = (uuid.uuid4(), [])

    async def store(
        self, record_type: str, item: Any, sync_id: uuid.UUID, fetched_at: datetime
    ) -> uuid.UUID:
        r = core_models.RawRecord
        content = record_json(loads_decimal(dumps(item)))
        canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
        values = {
            "connection_id": self.connection.id,
            "record_type": record_type,
            "content_hash": hashlib.sha256(canonical.encode()).hexdigest(),
        }
        inserted = await self.session.scalar(
            insert(r)
            .values(
                **values,
                sync_id=sync_id,
                request={"endpoint": record_type, "params": {}},
                content=content,
                connector_version="etoro-sync-1",
                fetched_at=fetched_at,
            )
            .on_conflict_do_nothing(index_elements=[r.connection_id, r.record_type, r.content_hash])
            .returning(r.id)
        )
        if inserted is not None:
            return inserted
        return (
            await self.session.execute(
                select(r.id).where(*(getattr(r, k) == v for k, v in values.items()))
            )
        ).scalar_one()

    async def context(self, sync_id: uuid.UUID | None = None) -> SyncContext:
        return SyncContext(
            command=CommandContext(self.session, CONNECTOR, uuid.uuid4()),
            connection=await get_connection(self.session, self.connection.id),
            sync_id=sync_id or uuid.uuid4(),
            master_key=bytes(32),
            http=httpx.AsyncClient(transport=httpx.MockTransport(_refuse)),
            notifier=self.notifier,
            session_factory=async_sessionmaker(bind=self.session.bind),
        )

    async def normalize(self, *entries: tuple[str, Any], now: datetime = NOW) -> NormalizeReport:
        """A sync at `now` that fetched the entries; each report sighted then."""
        sync_id = uuid.uuid4()
        ids: list[uuid.UUID] = []
        for endpoint, item in entries:
            raw_record_id = await self.store(ENDPOINTS[endpoint].record_type, item, sync_id, now)
            if endpoint in etoro_sync.REPORTS:
                await record_sighting(
                    CommandContext(self.session, CONNECTOR, uuid.uuid4()),
                    connection_id=self.connection.id,
                    raw_record_id=raw_record_id,
                    sync_id=sync_id,
                    seen_at=now,
                )
            ids.append(raw_record_id)
        self.last = (sync_id, ids)
        return await normalize(await self.context(sync_id), ids)

    async def again(self) -> NormalizeReport:
        sync_id, ids = self.last
        return await normalize(await self.context(sync_id), ids)

    async def replay(self) -> NormalizeReport:
        """A new normalizer version: every stored record replayed, nothing new fetched."""
        await update_config(
            CommandContext(self.session, CONNECTOR, uuid.uuid4()),
            self.connection.id,
            {"normalizer_version": "etoro-norm-0"},
        )
        return await normalize(await self.context(), [])


@pytest.fixture
async def ledger(session, run) -> Ledger:
    return Ledger(session, await _connection(run))


async def _postings(session, external_id: str) -> list[tuple]:
    """(account, instrument, quantity, cost, cost currency, price, price currency, is_fee) of
    the live transaction's postings, in order."""
    rows = await session.execute(
        text(
            "select a.name, i.symbol, p.quantity, p.cost_per_unit, ci.symbol as cost_symbol,"
            " p.price_per_unit, pi.symbol as price_symbol, p.is_fee"
            " from finance.postings p"
            " join finance.transactions t on t.id = p.transaction_id"
            " join finance.accounts a on a.id = p.account_id"
            " join finance.instruments i on i.id = p.instrument_id"
            " left join finance.instruments ci on ci.id = p.cost_instrument_id"
            " left join finance.instruments pi on pi.id = p.price_instrument_id"
            " where t.external_id = :e and t.status <> 'void' order by p.position"
        ),
        {"e": external_id},
    )
    return [tuple(r) for r in rows]


async def _transaction(session, external_id: str):
    return (
        await session.execute(
            text(
                "select id, occurred_at, booking_date, description, bank_transfer,"
                " counterparty_identifier_id, counterparty_name, payment_reference, status"
                " from finance.transactions where external_id = :e"
            ),
            {"e": external_id},
        )
    ).one_or_none()


async def _rows(session, sql: str, **params: Any) -> list[Any]:
    return list((await session.execute(text(sql), params)).all())


async def _holdings(session, account: str) -> dict[str, Decimal]:
    rows = await _rows(
        session,
        "select i.symbol, sum(p.quantity) as q from finance.postings p"
        " join finance.transactions t on t.id = p.transaction_id"
        " join finance.accounts a on a.id = p.account_id"
        " join finance.instruments i on i.id = p.instrument_id"
        " where a.name = :a and t.status <> 'void' group by i.symbol",
        a=account,
    )
    return {r.symbol: Decimal(r.q) for r in rows if r.q != 0}


async def _etoro_identifier(session, value: str) -> uuid.UUID:
    return (
        await session.execute(
            text(
                "select id from finance.account_identifiers"
                " where kind = 'etoro_account' and value = :v"
            ),
            {"v": value},
        )
    ).scalar_one()


def _ledger_entries(position: int = POSITION, **overrides: Any) -> list[tuple[str, Any]]:
    """A sync that sees one open position."""
    return [
        BALANCES,
        ("pnl", pnl_body([pnl_position(position, **overrides)])),
        ("aggregate", aggregate_body([(INSTRUMENT_ID, "USD", overrides.get("units", "4"))])),
        ("breakdown", breakdown_body([breakdown_position(position)])),
        META,
    ]


# Accounts (design §5.5) ---------------------------------------------------------------------


async def test_three_accounts_created(session, ledger) -> None:
    report = await ledger.normalize(("balances", balances_body(money="40.00", moneyfarm="900.5")))
    assert report.accounts_created == 3
    rows = await _rows(
        session,
        "select a.name, a.ledger_type, a.subtype, a.balance_mode, a.tax_wrapper,"
        " a.source_connection_id from finance.accounts a where a.source_connection_id = :c"
        " order by a.name",
        c=ledger.connection.id,
    )
    assert sorted(tuple(r)[:5] for r in rows) == [
        ("MoneyFarm ISA", "asset", "broker", "snapshots", "isa"),
        ("eToro Money GBP", "asset", "bank", "transactions", "none"),
        ("eToro Trading", "asset", "broker", "transactions", "gia"),
    ]
    # Trading and eToro Money carry their eToro ids; MoneyFarm (snapshots) none.
    identifiers = await _rows(
        session,
        "select a.name, i.value from finance.account_identifiers i"
        " join finance.accounts a on a.id = i.account_id where i.kind = 'etoro_account'"
        " order by a.name",
    )
    assert sorted(tuple(r) for r in identifiers) == [
        (MONEY, CASH_ACCOUNT),
        (TRADING, TRADING_ACCOUNT),
    ]
    # MoneyFarm's balance is a reported snapshot per sighting.
    snapshots = await _rows(
        session,
        "select s.quantity, i.symbol, s.kind from finance.balance_snapshots s"
        " join finance.accounts a on a.id = s.account_id"
        " join finance.instruments i on i.id = s.instrument_id where a.name = 'MoneyFarm ISA'",
    )
    assert [tuple(r) for r in snapshots] == [(Decimal("900.5"), "GBP", "reported")]
    await ledger.normalize(
        ("balances", balances_body(money="40.00", moneyfarm="950.0")), now=NOW + timedelta(hours=1)
    )
    count = await session.scalar(
        text(
            "select count(*) from finance.balance_snapshots s join finance.accounts a"
            " on a.id = s.account_id where a.name = 'MoneyFarm ISA'"
        )
    )
    assert count == 2
    # The balances are typed per day, each entry linked to its account.
    linked = await _rows(
        session,
        "select b.account_type, a.name from finance.etoro_balance_accounts b"
        " join finance.accounts a on a.id = b.finance_account_id where b.connection_id = :c"
        " order by b.item_index",
        c=ledger.connection.id,
    )
    assert [tuple(r) for r in linked] == [
        ("Trading", TRADING),
        ("Cash", MONEY),
        ("MoneyFarm", "MoneyFarm ISA"),
    ]


async def test_unknown_account_type_flagged(session, ledger) -> None:
    extra = (balance_entry("34567890", "Options", "USD", "5"),)
    report = await ledger.normalize(("balances", balances_body(extra=extra)))
    assert "balances.balances[].accountType=Options" in report.flagged
    assert report.accounts_created == 3


# Opens and closes (design §5.7) ---------------------------------------------------------------


async def test_open_from_api_cost_in_usd(session, ledger) -> None:
    await ledger.normalize(
        *_ledger_entries(), ("order_lookup", lookup_body(ORDER, POSITION, fees="0.75"))
    )
    # The execution fee is typed, not posted: the statement posts fees and taxes (R30).
    assert await _postings(session, f"open:{POSITION}") == [
        (TRADING, SYMBOL, Decimal(4), Decimal(125), "USD", None, None, False),
        (TRADING, "USD", Decimal("-500.00"), None, None, None, None, False),
    ]
    tx = await _transaction(session, f"open:{POSITION}")
    assert tx.occurred_at == datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
    assert tx.booking_date == date(2026, 5, 1)
    assert tx.description == f"Buy 4 {SYMBOL}"
    assert await find_unbalanced_transactions(session) == []
    # The instrument: eToro's id, its type, six decimals, quoted in its asset currency.
    [instrument] = await _rows(
        session,
        "select i.kind, i.decimals, i.name, q.symbol as quote from finance.instruments i"
        " join finance.instrument_identifiers ii on ii.instrument_id = i.id"
        " join finance.instruments q on q.id = i.quote_instrument_id"
        " where ii.scheme = 'etoro_id' and ii.value = :v",
        v=str(INSTRUMENT_ID),
    )
    assert tuple(instrument) == ("equity", 6, "Synthetic Quality A", "USD")
    # The position typed for the day; the lookup's execution typed and linked to the open.
    [position] = await _rows(
        session,
        "select p.amount, p.units, p.finance_instrument_id is not null as linked"
        " from finance.etoro_pnl_positions p where p.connection_id = :c",
        c=ledger.connection.id,
    )
    assert tuple(position) == (Decimal("500.00"), Decimal(4), True)
    [execution] = await _rows(
        session,
        "select e.opening_data_avg_price, e.opening_data_avg_conversion_rate,"
        " e.opening_data_market_spread, e.opening_data_markup, e.opening_data_fees,"
        " e.opening_data_taxes, e.transaction_id from finance.etoro_position_executions e"
        " where e.connection_id = :c",
        c=ledger.connection.id,
    )
    assert tuple(execution)[:6] == (
        Decimal("125.0"),
        Decimal(1),
        Decimal("0.05"),
        Decimal("0.02"),
        Decimal("0.75"),
        Decimal(0),
    )
    assert execution.transaction_id == tx.id
    # The aggregate's units are a reported snapshot on the Trading account.
    snapshots = await _rows(
        session,
        "select s.quantity, i.symbol from finance.balance_snapshots s"
        " join finance.instruments i on i.id = s.instrument_id"
        " join finance.accounts a on a.id = s.account_id where a.name = :a",
        a=TRADING,
    )
    assert [tuple(r) for r in snapshots] == [(Decimal(4), SYMBOL)]


async def test_close_from_trade_history(session, ledger) -> None:
    await ledger.normalize(*_ledger_entries())
    row = trade_row(POSITION, date(2026, 6, 1)) | {
        "investment": Decimal("500.00"),
        "initialInvestment": Decimal("500.00"),
        "netProfit": Decimal("20.00"),
        "units": Decimal("4"),
        "fees": Decimal("0"),
        "instrumentId": INSTRUMENT_ID,
        "orderId": ORDER,
    }
    # The position is gone from PnL; its open survives, and its close posts.
    await ledger.normalize(
        BALANCES,
        ("pnl", pnl_body([])),
        ("trade_history", row),
        ("order_lookup", lookup_body(ORDER, POSITION)),
        now=NOW + timedelta(days=1),
    )
    assert await _postings(session, f"close:{POSITION}") == [
        (TRADING, SYMBOL, Decimal(-4), None, None, Decimal(130), "USD", False),
        (TRADING, "USD", Decimal("520.00"), None, None, None, None, False),
    ]
    assert (await _transaction(session, f"close:{POSITION}")).description == f"Sell 4 {SYMBOL}"
    assert await _postings(session, f"open:{POSITION}") != []
    assert await _holdings(session, TRADING) == {"USD": Decimal("20.00")}
    [trade] = await _rows(
        session,
        "select t.net_profit, t.investment, t.transaction_id is not null as posted,"
        " t.finance_instrument_id is not null as linked from finance.etoro_trades t"
        " where t.external_id = :e",
        e=f"close:{POSITION}",
    )
    assert tuple(trade) == (Decimal("20.00"), Decimal("500.00"), True, True)


async def test_close_fees_are_typed_not_posted(session, ledger) -> None:
    # A close's `fees` are the position's overnight fees and dividends, which the statement
    # posts as rows of their own (Task 11): the close posts its proceeds alone.
    await ledger.normalize(*_ledger_entries())
    row = trade_row(POSITION) | {
        "investment": Decimal("500.00"),
        "netProfit": Decimal("20.00"),
        "units": Decimal("4"),
        "fees": Decimal("1.50"),
        "orderId": ORDER,
    }
    await ledger.normalize(
        BALANCES, ("trade_history", row), ("order_lookup", lookup_body(ORDER, POSITION))
    )
    postings = await _postings(session, f"close:{POSITION}")
    assert postings[1:] == [(TRADING, "USD", Decimal("520.00"), None, None, None, None, False)]
    fees = await session.scalar(
        text("select fees from finance.etoro_trades where external_id = :e"),
        {"e": f"close:{POSITION}"},
    )
    assert fees == Decimal("1.50")
    assert await find_unbalanced_transactions(session) == []


async def test_partial_close_two_positions(session, ledger) -> None:
    remainder = POSITION + 1
    await ledger.normalize(*_ledger_entries(units="10", amount="1000.00"))
    assert (await _postings(session, f"open:{POSITION}"))[0][2] == Decimal(10)
    # Four of ten units closed: trade history has the closed position; the rest is a new one.
    row = trade_row(POSITION) | {
        "investment": Decimal("400.00"),
        "initialInvestment": Decimal("1000.00"),
        "netProfit": Decimal("20.00"),
        "units": Decimal("4"),
        "orderId": ORDER,
        "openTimestamp": "2026-05-01T09:00:00.000Z",
    }
    await ledger.normalize(
        BALANCES,
        (
            "pnl",
            pnl_body(
                [pnl_position(remainder, units="6", amount="600.00", parentPositionID=POSITION)]
            ),
        ),
        ("aggregate", aggregate_body([(INSTRUMENT_ID, "USD", "6")])),
        ("trade_history", row),
        ("order_lookup", lookup_body(ORDER, POSITION)),
        now=NOW + timedelta(days=1),
    )
    assert [p[2] for p in await _postings(session, f"open:{POSITION}")] == [
        Decimal(4),
        Decimal("-400.00"),
    ]
    assert [p[2] for p in await _postings(session, f"close:{POSITION}")] == [
        Decimal(-4),
        Decimal("420.00"),
    ]
    assert [p[2] for p in await _postings(session, f"open:{remainder}")] == [
        Decimal(6),
        Decimal("-600.00"),
    ]
    assert await _holdings(session, TRADING) == {SYMBOL: Decimal(6), "USD": Decimal("-580.00")}


async def test_cfd_flagged_not_posted(session, ledger) -> None:
    cfd, leveraged, copied = POSITION, POSITION + 1, POSITION + 2
    report = await ledger.normalize(
        BALANCES,
        (
            "pnl",
            pnl_body(
                [
                    pnl_position(cfd, settlementTypeID=0),
                    pnl_position(leveraged, leverage=2),
                    pnl_position(copied, mirrorID=77),
                ]
            ),
        ),
        ("aggregate", aggregate_body([(INSTRUMENT_ID, "USD", "12")])),
        META,
    )
    for position in (cfd, leveraged, copied):
        assert await _transaction(session, f"open:{position}") is None
    assert "a CFD, leveraged or copied position" in report.flagged
    # A trade-history close of one is not posted either.
    row = trade_row(leveraged) | {"leverage": 2, "orderId": leveraged + 500}
    report = await ledger.normalize(BALANCES, ("trade_history", row))
    assert await _transaction(session, f"close:{leveraged}") is None
    assert "a CFD, leveraged or copied position" in report.flagged


async def test_unsupported_instrument_type_not_posted(session, ledger) -> None:
    report = await ledger.normalize(
        *_ledger_entries()[:-1], ("instruments", instrument_meta(type_id=10))
    )
    assert await _transaction(session, f"open:{POSITION}") is None
    assert "instruments.instrumentTypeID=10" in report.flagged


async def test_untyped_instrument_posts_as_other_until_typed(session, ledger) -> None:
    report = await ledger.normalize(*_ledger_entries()[:-1])
    assert await _postings(session, f"open:{POSITION}") != []
    assert report.flags_in_full == {f"instrument type unknown {SYMBOL}"}
    report = await ledger.normalize(META, now=NOW + timedelta(hours=1))
    kind = await session.scalar(
        text(
            "select i.kind from finance.instruments i join finance.instrument_identifiers ii"
            " on ii.instrument_id = i.id where ii.scheme = 'etoro_id'"
        )
    )
    assert kind == "equity"
    assert report.flags_in_full == set()


async def test_untyped_then_unsupported_is_voided(session, ledger) -> None:
    await ledger.normalize(*_ledger_entries()[:-1])
    assert await _postings(session, f"open:{POSITION}") != []
    report = await ledger.normalize(
        ("instruments", instrument_meta(type_id=10)), now=NOW + timedelta(hours=1)
    )
    assert await _postings(session, f"open:{POSITION}") == []
    assert report.flags_in_full == {f"instrument type unsupported {SYMBOL}"}


async def test_gbx_listing_and_prices_in_pounds(session, ledger) -> None:
    await ledger.normalize(
        BALANCES,
        ("pnl", pnl_body([pnl_position(POSITION)])),
        ("aggregate", aggregate_body([(INSTRUMENT_ID, "GBX", "4")])),
        # The breakdown writes it in lower case: it is still pence.
        ("breakdown", breakdown_body([breakdown_position(POSITION, currency="gbx", rate="250")])),
        META,
        ("candles", candle_item(date(2026, 5, 29), "248.0")),
    )
    quote = await session.scalar(
        text(
            "select q.symbol from finance.instruments i"
            " join finance.instruments q on q.id = i.quote_instrument_id where i.symbol = :s"
        ),
        {"s": SYMBOL},
    )
    assert quote == "GBP"
    prices = await _rows(
        session,
        "select p.as_of_date, p.price, q.symbol, p.source from finance.prices p"
        " join finance.instruments q on q.id = p.quote_instrument_id order by p.as_of_date",
    )
    assert [tuple(r) for r in prices] == [
        (date(2026, 5, 29), Decimal("2.480"), "GBP", "etoro"),
        (NOW.date(), Decimal("2.50"), "GBP", "etoro"),
    ]


async def test_prices_from_candles_and_current_rate(session, ledger) -> None:
    day = NOW.date()
    await ledger.normalize(
        *_ledger_entries(),
        ("candles", candle_item(day - timedelta(days=1), "124.10")),
    )
    prices = await _rows(
        session,
        "select p.as_of_date, p.price, q.symbol, p.source from finance.prices p"
        " join finance.instruments q on q.id = p.quote_instrument_id order by p.as_of_date",
    )
    assert [tuple(r) for r in prices] == [
        (day - timedelta(days=1), Decimal("124.10"), "USD", "etoro"),
        (day, Decimal("126.5"), "USD", "etoro"),
    ]
    # A day's candle wins over a current rate of that day, whichever arrives first.
    await ledger.normalize(("candles", candle_item(day, "127.00")), now=NOW + timedelta(hours=1))
    await ledger.normalize(*_ledger_entries(), now=NOW + timedelta(hours=2))
    price = await session.scalar(
        text("select price from finance.prices where as_of_date = :d"), {"d": day}
    )
    assert price == Decimal("127.00")
    # No FX rates are written.
    assert await session.scalar(text("select count(*) from finance.fx_rates")) == 0


# eToro Money (design §5.7, §5.12) -------------------------------------------------------------


async def test_cash_bank_transfer_identified_by_iban(session, run, ledger) -> None:
    monzo = await run(
        lambda ctx: create_account(
            ctx,
            name="Synthetic Bank",
            ledger_type="asset",
            subtype="bank",
            balance_mode="transactions",
        )
    )
    await run(lambda ctx: assign_identifier(ctx, monzo.id, "uk_bank_account", "040004:12345678"))
    await ledger.normalize(BALANCES, ("cash_transactions", bank_item("80000011", "250.00")))
    assert await _postings(session, "cash:80000011") == [
        (MONEY, "GBP", Decimal("250.00"), None, None, None, None, False),
        ("In transit", "GBP", Decimal("-250.00"), None, None, None, None, False),
    ]
    tx = await _transaction(session, "cash:80000011")
    assert tx.bank_transfer is True
    assert tx.payment_reference == "SYNTHREF01"
    assert tx.counterparty_identifier_id is not None
    # A debit is negative; a bank not in the UK names no identifier, and is flagged.
    report = await ledger.normalize(
        BALANCES,
        (
            "cash_transactions",
            bank_item("80000012", "40.00", "debit", ("iban", "DE00ZZZZ0000000000000000")),
        ),
    )
    assert await _postings(session, "cash:80000012") == [
        (MONEY, "GBP", Decimal("-40.00"), None, None, None, None, False),
        ("Unassigned", "GBP", Decimal("40.00"), None, None, None, None, False),
    ]
    assert (await _transaction(session, "cash:80000012")).counterparty_identifier_id is None
    assert report.flagged == {"bank transfer to a non-UK account"}


async def test_internal_transfer_names_trading(session, ledger) -> None:
    item = cash_item("80000021", "100.00") | {
        "direction": "debit",
        "transactionSubtype": "transfer",
        "counterparty": {"name": "eToro Trading account", "type": "internal_account"},
    }
    await ledger.normalize(BALANCES, ("cash_transactions", item))
    assert await _postings(session, "cash:80000021") == [
        (MONEY, "GBP", Decimal("-100.00"), None, None, None, None, False),
        ("In transit", "GBP", Decimal("100.00"), None, None, None, None, False),
    ]
    tx = await _transaction(session, "cash:80000021")
    assert tx.counterparty_identifier_id == await _etoro_identifier(session, TRADING_ACCOUNT)
    assert tx.bank_transfer is False


async def test_internal_transfer_with_moneyfarm_or_unknown(session, ledger) -> None:
    moneyfarm = cash_item("80000031", "70.00") | {
        "counterparty": {"name": "MoneyFarm Stocks ISA", "type": "internal_account"}
    }
    unknown = cash_item("80000032", "5.00") | {
        "counterparty": {"name": "Somewhere Else", "type": "internal_account"}
    }
    report = await ledger.normalize(
        BALANCES, ("cash_transactions", moneyfarm), ("cash_transactions", unknown)
    )
    for key in ("cash:80000031", "cash:80000032"):
        assert (await _postings(session, key))[1][0] == "Unassigned"
        assert (await _transaction(session, key)).counterparty_identifier_id is None
    assert report.flagged == {"an internal transfer names no account Lararium knows"}


async def test_pending_cash_row_not_posted(session, ledger) -> None:
    pending = bank_item("80000041", "12.00", status="authorized")
    await ledger.normalize(BALANCES, ("cash_transactions", pending))
    assert await _transaction(session, "cash:80000041") is None
    [row] = await _rows(
        session,
        "select status, transaction_id from finance.etoro_cash_transactions where id = :i",
        i="80000041",
    )
    assert tuple(row) == ("authorized", None)
    await ledger.normalize(
        BALANCES,
        ("cash_transactions", bank_item("80000041", "12.00")),
        now=NOW + timedelta(hours=1),
    )
    assert (await _postings(session, "cash:80000041"))[0][2] == Decimal("12.00")


async def test_card_payment_against_unassigned(session, ledger) -> None:
    card = cash_item("80000051", "9.99") | {
        "transactionType": "card",
        "transactionSubtype": "cardPayment",
        "direction": "debit",
        "counterparty": {"name": "Synthetic Cafe", "type": "merchant"},
        "internalTransferTransactionDetails": None,
        "cardTransactionDetails": {"cardId": "c1", "merchantName": "Synthetic Cafe"},
    }
    await ledger.normalize(BALANCES, ("cash_transactions", card))
    assert await _postings(session, "cash:80000051") == [
        (MONEY, "GBP", Decimal("-9.99"), None, None, None, None, False),
        ("Unassigned", "GBP", Decimal("9.99"), None, None, None, None, False),
    ]


# Balance history and the eToro Money opening (design §5.5, §5.8) ------------------------------


async def test_balance_history_snapshots_and_money_opening(session, ledger) -> None:
    first, second = date(2026, 5, 20), date(2026, 5, 21)
    history = history_body(
        [
            (
                first,
                [(CASH_ACCOUNT, "Cash", "GBP", "300.00"), (TRADING_ACCOUNT, "Trading", "USD", "0")],
            ),
            (
                second,
                [(CASH_ACCOUNT, "Cash", "GBP", "350.00"), (TRADING_ACCOUNT, "Trading", "USD", "5")],
            ),
        ]
    )
    credit = cash_item("80000061", "50.00") | {
        "postedAt": "2026-05-21T10:00:00Z",
        "counterparty": {"name": "eToro Trading account", "type": "internal_account"},
    }
    await ledger.normalize(BALANCES, ("balance_history", history), ("cash_transactions", credit))
    # One reported snapshot per account per day, dated by the snapshot's date.
    snapshots = await _rows(
        session,
        "select a.name, s.as_of_date, s.quantity from finance.balance_snapshots s"
        " join finance.accounts a on a.id = s.account_id where s.kind = 'reported'"
        " and a.name in (:t, :m) order by a.name, s.as_of_date",
        t=TRADING,
        m=MONEY,
    )
    assert [tuple(r) for r in snapshots] == [
        (MONEY, first, Decimal("300.00")),
        (MONEY, second, Decimal("350.00")),
        # The balances' figure for eToro Money, as seen.
        (MONEY, NOW.date(), Decimal(0)),
        (TRADING, first, Decimal(0)),
        (TRADING, second, Decimal(5)),
    ]
    # eToro Money's records do not reach its earliest snapshot: one opening, the day before.
    key = f"opening:{CASH_ACCOUNT}:{(await get_currency(session, 'GBP')).id}"
    assert await _postings(session, key) == [
        (MONEY, "GBP", Decimal("300.00"), None, None, None, None, False),
        ("Opening balances", "GBP", Decimal("-300.00"), None, None, None, None, False),
    ]
    assert (await _transaction(session, key)).booking_date == first - timedelta(days=1)
    # Trading gets none: its first statement covers the account's start.
    assert (
        await session.scalar(
            text("select count(*) from finance.transactions where external_id like :k"),
            {"k": f"opening:{TRADING_ACCOUNT}:%"},
        )
        == 0
    )
    typed = await _rows(
        session,
        "select s.snapshot_date, s.cash, a.name from finance.etoro_balance_account_snapshots s"
        " join finance.accounts a on a.id = s.finance_account_id"
        " where s.snapshot_id is not null order by s.snapshot_date, s.item_index",
    )
    assert [tuple(r) for r in typed] == [
        (first, Decimal("300.00"), MONEY),
        (first, Decimal(0), TRADING),
        (second, Decimal("350.00"), MONEY),
        (second, Decimal(5), TRADING),
    ]
    # Records that reach back to the snapshot leave no opening.
    earlier = cash_item("80000060", "300.00") | {
        "postedAt": "2026-05-19T10:00:00Z",
        "counterparty": {"name": "eToro Trading account", "type": "internal_account"},
    }
    await ledger.normalize(BALANCES, ("cash_transactions", earlier), now=NOW + timedelta(hours=1))
    assert await _postings(session, key) == []


# Regeneration (parent §6.6) ------------------------------------------------------------------


async def test_renormalize_writes_nothing_and_replay_is_the_same(session, ledger) -> None:
    await ledger.normalize(
        *_ledger_entries(),
        ("order_lookup", lookup_body(ORDER, POSITION)),
        ("cash_transactions", bank_item("80000071", "10.00")),
        ("me", me_json()),
    )
    before = {key: await _postings(session, key) for key in (f"open:{POSITION}", "cash:80000071")}
    again = await ledger.again()
    assert (again.created, again.updated) == (0, 0)
    # A refused key's record is no endpoint's: the replay passes it by.
    await ledger.store("etoro.refused_key", {"scopes": ["x:write"]}, uuid.uuid4(), NOW)
    replay = await ledger.replay()
    assert replay.replayed
    assert (replay.created, replay.updated) == (0, 0)
    assert {key: await _postings(session, key) for key in before} == before
    config = (await get_connection(session, ledger.connection.id)).config
    assert config["normalizer_version"] == NORMALIZER_VERSION


async def test_unknown_field_flagged(ledger) -> None:
    pnl = pnl_body([pnl_position(POSITION, brandNewField=1)])
    report = await ledger.normalize(BALANCES, ("pnl", pnl), META)
    assert "pnl.clientPortfolio.positions[].brandNewField" in report.flagged


# Merging instruments (design §5.6) ----------------------------------------------------------


async def _references(session, instrument_id: uuid.UUID) -> list[str]:
    """Every column with a foreign key to finance.instruments that holds the id."""
    columns = await _rows(
        session,
        "select c.relname as tbl, a.attname as col from pg_constraint k"
        " join pg_class c on c.oid = k.conrelid"
        " join pg_namespace n on n.oid = c.relnamespace and n.nspname = 'finance'"
        " join pg_attribute a on a.attrelid = c.oid and a.attnum = any(k.conkey)"
        " where k.contype = 'f' and k.confrelid = 'finance.instruments'::regclass"
        # The catalog's own order can change between two reads: compared lists need one.
        " order by c.relname, a.attname",
    )
    found = []
    for row in columns:
        held = await session.scalar(
            text(f'select count(*) from finance."{row.tbl}" where "{row.col}" = :i'),
            {"i": instrument_id},
        )
        if held:
            found.append(f"{row.tbl}.{row.col}")
    return found


async def _ibkr_listing(run, session, code: str) -> uuid.UUID:
    quote = await get_currency(session, "USD")
    created = await run(
        lambda ctx: create_instrument(
            ctx,
            kind="equity",
            symbol="ZZQA.IB",
            name="Synthetic Quality A",
            decimals=4,
            quote_instrument_id=quote.id,
            identifiers=[
                InstrumentIdentifier("isin", code),
                InstrumentIdentifier("ibkr_conid", "990001"),
            ],
        )
    )
    return created.id


async def _etoro_instrument(session) -> uuid.UUID:
    return (
        await session.execute(
            text(
                "select instrument_id from finance.instrument_identifiers"
                " where scheme = 'etoro_id' and value = :v"
            ),
            {"v": str(INSTRUMENT_ID)},
        )
    ).scalar_one()


async def test_merge_after_positions_recorded(session, run, ledger) -> None:
    await ledger.normalize(
        *_ledger_entries(),
        ("order_lookup", lookup_body(ORDER, POSITION)),
        ("candles", candle_item(date(2026, 5, 29), "124.10")),
    )
    drop = await _etoro_instrument(session)
    assert set(await _references(session, drop)) >= {
        "postings.instrument_id",
        "balance_snapshots.instrument_id",
        "prices.instrument_id",
        "etoro_pnl_positions.finance_instrument_id",
        "etoro_aggregate_instruments.finance_instrument_id",
        "etoro_orders.finance_instrument_id",
        "etoro_instruments.finance_instrument_id",
        "instrument_identifiers.instrument_id",
    }
    # A statement brings an ISIN an IBKR listing in the same currency holds: one remains.
    code = isin("US00ZZQM000")
    keep = await _ibkr_listing(run, session, code)
    # keep already has an eToro price for one of the days: the dropped one's is discarded.
    usd = await get_currency(session, "USD")
    await run(
        lambda ctx: record_price(
            ctx,
            instrument_id=keep,
            quote_instrument_id=usd.id,
            price=Decimal("99"),
            as_of_date=date(2026, 5, 29),
            source="etoro",
        )
    )
    await run(lambda ctx: merge_instruments(ctx, keep, drop))
    assert await _references(session, drop) == []
    # The finer of the two precisions stays: eToro's 6 places, not the IBKR listing's 4.
    assert (
        await session.scalar(
            text("select decimals from finance.instruments where id = :i"), {"i": keep}
        )
        == 6
    )
    assert (
        await session.scalar(
            text("select count(*) from finance.instruments where id = :i"), {"i": drop}
        )
        == 0
    )
    identifiers = await _rows(
        session,
        "select scheme, value from finance.instrument_identifiers where instrument_id = :i"
        " order by scheme",
        i=keep,
    )
    assert [tuple(r) for r in identifiers] == [
        ("etoro_id", str(INSTRUMENT_ID)),
        ("ibkr_conid", "990001"),
        ("isin", code),
    ]
    assert (await _postings(session, f"open:{POSITION}"))[0][1] == "ZZQA.IB"
    prices = await _rows(
        session,
        "select as_of_date, price from finance.prices where instrument_id = :i order by as_of_date",
        i=keep,
    )
    assert [tuple(r) for r in prices] == [
        (date(2026, 5, 29), Decimal("99")),
        (NOW.date(), Decimal("126.5")),
    ]
    audit = await session.scalar(
        text("select count(*) from core.audit_log where changes ? :k"),
        {"k": f"instrument:{drop}"},
    )
    assert audit == 1
    # The next sync finds the merged instrument by its eToro id.
    await ledger.normalize(*_ledger_entries(), now=NOW + timedelta(hours=1))
    assert await _etoro_instrument(session) == keep
    assert await find_unbalanced_transactions(session) == []


async def test_merge_conflict_fails_before_writing(session, run, ledger) -> None:
    await ledger.normalize(*_ledger_entries())
    drop = await _etoro_instrument(session)
    keep = await _ibkr_listing(run, session, isin("US00ZZQM000"))
    # Both are IBKR listings already: ibkr_instruments holds one row per instrument.
    for instrument_id, conid in ((keep, "990001"), (drop, "990002")):
        await session.execute(
            text(
                "insert into finance.ibkr_instruments (instrument_id, conid, asset_category)"
                " values (:i, :c, 'STK')"
            ),
            {"i": instrument_id, "c": conid},
        )
    before = await _references(session, drop)
    with pytest.raises(FinanceValidationError, match="ibkr_instruments"):
        await merge_instruments(CommandContext(session, CONNECTOR, uuid.uuid4()), keep, drop)
    assert await _references(session, drop) == before


async def test_merge_refuses_another_currency(session, run, ledger) -> None:
    await ledger.normalize(*_ledger_entries())
    drop = await _etoro_instrument(session)
    gbp = await get_currency(session, "GBP")
    other = await run(
        lambda ctx: create_instrument(
            ctx,
            kind="equity",
            symbol="ZZQA.L",
            name="Synthetic Quality A",
            decimals=4,
            quote_instrument_id=gbp.id,
            identifiers=[],
        )
    )
    with pytest.raises(FinanceValidationError):
        await run(lambda ctx: merge_instruments(ctx, other.id, drop))


# Personal fields (design §5.9) ---------------------------------------------------------------


async def test_personal_fields_stored_not_logged(session, ledger, caplog) -> None:
    me = me_json()
    item = bank_item("80000081", "15.00")
    abroad = bank_item("80000082", "5.00", identifier=("iban", "DE00ZZZZ0000000000000000"))
    unknown = cash_item("80000083", "1.00") | {
        "counterparty": {"name": "Somewhere Personal", "type": "internal_account"}
    }
    with caplog.at_level(logging.DEBUG):
        report = await ledger.normalize(
            BALANCES,
            ("me", me),
            ("cash_transactions", item),
            ("cash_transactions", abroad),
            ("cash_transactions", unknown),
        )
    # The flags name what is wrong, never whose it is.
    flags = " ".join(report.flagged | report.flags_in_full)
    assert report.flagged == {
        "bank transfer to a non-UK account",
        "an internal transfer names no account Lararium knows",
    }
    [user] = await _rows(
        session,
        "select first_name, last_name, date_of_birth, username from finance.etoro_users"
        " where connection_id = :c",
        c=ledger.connection.id,
    )
    assert tuple(user) == ("Testa", "Example", "1990-01-01T00:00:00", "synthetic-user")
    scopes = await _rows(
        session,
        "select scope from finance.etoro_user_scopes where connection_id = :c order by item_index",
        c=ledger.connection.id,
    )
    assert [r.scope for r in scopes] == me["scopes"]
    [cash] = await _rows(
        session,
        "select c.counterparty_name, b.name, b.value from finance.etoro_cash_transactions c"
        " join finance.etoro_cash_bank_identifiers b on b.cash_transaction_id = c.id"
        " where c.id = :i",
        i="80000081",
    )
    assert tuple(cash) == ("Synthetic Payer Name", "iban", "GB33ZZZZ04000412345678")
    for value in (
        "Testa",
        "Example",
        "1990-01-01",
        "synthetic-user",
        "Synthetic Payer Name",
        "GB33ZZZZ04000412345678",
        "04000412345678",
        "SYNTHREF01",
        "DE00ZZZZ0000000000000000",
        "Somewhere Personal",
    ):
        assert value not in caplog.text
        assert value not in flags


# Review fixes (Task 10, round 1) --------------------------------------------------------------


async def test_bank_transfer_by_account_number_alone(session, run, ledger) -> None:
    # The probe's shape: one identifier, `bankAccountNumber`, and no sort code anywhere.
    own = await run(
        lambda ctx: create_account(
            ctx,
            name="Synthetic Bank",
            ledger_type="asset",
            subtype="bank",
            balance_mode="transactions",
        )
    )
    await run(lambda ctx: assign_identifier(ctx, own.id, "uk_bank_account", "040004:12345678"))
    item = bank_item("80000091", "75.00", identifier=("bankAccountNumber", "12345678"))
    report = await ledger.normalize(BALANCES, ("cash_transactions", item))
    assert report.flagged == set()
    tx = await _transaction(session, "cash:80000091")
    assert tx.counterparty_identifier_id is None
    assert tx.bank_transfer is True
    number = await session.scalar(
        text("select counterparty_account_number from finance.transactions where id = :i"),
        {"i": tx.id},
    )
    assert number == "12345678"
    # The engine's account-number evidence names the own account registered under it.
    records = eligible_records(await system_account_ids(session))
    holder = await session.scalar(select(records.c.holder).where(records.c.transaction_id == tx.id))
    assert holder == own.id


async def test_settlement_unknown_has_its_own_flag(session, ledger) -> None:
    # A close whose order eToro could not look up, of a position no PnL report listed.
    row = trade_row(POSITION) | {"instrumentId": INSTRUMENT_ID, "orderId": ORDER}
    report = await ledger.normalize(
        BALANCES,
        ("aggregate", aggregate_body([(INSTRUMENT_ID, "USD", "0")])),
        META,
        ("trade_history", row),
    )
    assert await _transaction(session, f"close:{POSITION}") is None
    assert "a CFD, leveraged or copied position" not in report.flagged
    assert "position settlement unknown (1)" in report.flags_in_full
    [trade] = await _rows(
        session,
        "select transaction_id from finance.etoro_trades where external_id = :e",
        e=f"close:{POSITION}",
    )
    assert trade.transaction_id is None
    # Once its lookup arrives, it posts, and the flag clears.
    report = await ledger.normalize(
        BALANCES, ("order_lookup", lookup_body(ORDER, POSITION)), now=NOW + timedelta(hours=1)
    )
    assert await _postings(session, f"close:{POSITION}") != []
    assert not any(f.startswith("position settlement unknown") for f in report.flags_in_full)


async def test_instrument_currency_unknown_waits_for_the_aggregate(session, ledger) -> None:
    # Trade history and a lookup only: the lookup's `asset.currency` is not the asset's.
    row = trade_row(POSITION) | {"instrumentId": INSTRUMENT_ID, "orderId": ORDER}
    report = await ledger.normalize(
        BALANCES, META, ("trade_history", row), ("order_lookup", lookup_body(ORDER, POSITION))
    )
    assert await _transaction(session, f"close:{POSITION}") is None
    assert (
        await session.scalar(
            text("select count(*) from finance.instrument_identifiers where scheme = 'etoro_id'")
        )
        == 0
    )
    assert report.flags_in_full == {f"instrument currency unknown {SYMBOL}"}
    # A later sync's aggregate gives it: the instrument is made and the trade posts.
    report = await ledger.normalize(
        BALANCES,
        ("aggregate", aggregate_body([(INSTRUMENT_ID, "USD", "0")])),
        now=NOW + timedelta(hours=1),
    )
    assert await _postings(session, f"open:{POSITION}") != []
    assert await _postings(session, f"close:{POSITION}") != []
    assert report.flags_in_full == set()


async def test_internal_transfer_to_trading_without_one_is_flagged(session, ledger) -> None:
    balances = balances_body()
    balances["balances"] = [b for b in balances["balances"] if b["accountType"] != "Trading"]
    item = cash_item("80000101", "20.00") | {
        "counterparty": {"name": "eToro Trading account", "type": "internal_account"}
    }
    report = await ledger.normalize(("balances", balances), ("cash_transactions", item))
    assert (await _postings(session, "cash:80000101"))[1][0] == "Unassigned"
    assert report.flagged == {"an internal transfer names no account Lararium knows"}


async def test_non_numeric_leverage_is_a_data_error(ledger) -> None:
    with pytest.raises(EtoroDataError, match="leverage is not a number"):
        await ledger.normalize(*_ledger_entries(leverage="two"))


async def test_execution_fees_and_taxes_typed_not_posted(session, ledger) -> None:
    # R30: an open's execution fees and taxes (SDRT, say) post only from the statement's rows.
    for fees, taxes in (("0.75", "1.10"), ("-0.40", "0")):
        report = await ledger.normalize(
            *_ledger_entries(),
            ("order_lookup", lookup_body(ORDER, POSITION, fees=fees, taxes=taxes)),
        )
        assert [p[7] for p in await _postings(session, f"open:{POSITION}")] == [False, False]
        assert not any("fee" in flag for flag in report.flagged)
        [typed] = await _rows(
            session,
            "select opening_data_fees, opening_data_taxes from finance.etoro_position_executions"
            " where connection_id = :c",
            c=ledger.connection.id,
        )
        assert tuple(typed) == (Decimal(fees), Decimal(taxes))


async def test_errors_name_a_position_by_its_last_digits(ledger) -> None:
    with pytest.raises(EtoroDataError) as raised:
        await ledger.normalize(*_ledger_entries(units="0"))
    assert "···0101" in str(raised.value)
    assert str(POSITION) not in str(raised.value)


# Regeneration (parent §6.6) ------------------------------------------------------------------

_TYPED_TABLES = (
    "etoro_user_scopes",
    "etoro_users",
    "etoro_pnl_positions",
    "etoro_pnl",
    "etoro_aggregate_instruments",
    "etoro_aggregates",
    "etoro_breakdown_positions",
    "etoro_breakdown_instruments",
    "etoro_breakdowns",
    "etoro_balance_accounts",
    "etoro_balances",
    "etoro_balance_account_snapshots",
    "etoro_balance_snapshots",
    "etoro_balance_history",
    "etoro_cash_bank_identifiers",
    "etoro_cash_transactions",
    "etoro_trades",
    "etoro_position_executions",
    "etoro_order_positions_to_close",
    "etoro_orders",
    "etoro_closed_events",
    "etoro_stockback_payments",
    "etoro_instrument_images",
    "etoro_instruments",
    "etoro_instrument_types",
    "etoro_exchanges",
    "etoro_candles",
    "etoro_activity",
    "etoro_dividends",
    "etoro_statements",
    "etoro_financial_summary_values",
    "etoro_financial_summary",
)


async def _ledger_state(session, connection) -> dict[str, Any]:
    """What normalize derives for the connection, by external id and content, no UUIDs of
    derived rows."""
    state: dict[str, Any] = {}
    rows = await session.execute(
        text(
            "select t.external_id, t.status, t.occurred_at, t.booking_date, t.description,"
            " t.bank_transfer, ci.value as counterparty, t.counterparty_account_number,"
            " (select json_agg(json_build_array(a.name, i.symbol, p.quantity::text,"
            "   p.cost_per_unit::text, p.price_per_unit::text, p.is_fee, p.memo)"
            "   order by p.position)"
            "  from finance.postings p join finance.accounts a on a.id = p.account_id"
            "  join finance.instruments i on i.id = p.instrument_id"
            "  where p.transaction_id = t.id) as postings"
            " from finance.transactions t"
            " left join finance.account_identifiers ci on ci.id = t.counterparty_identifier_id"
            " where t.connection_id = :c order by t.external_id"
        ),
        {"c": connection.id},
    )
    state["transactions"] = [tuple(r) for r in rows]
    prices = await session.execute(
        text(
            "select i.symbol, q.symbol, p.price, p.as_of_date from finance.prices p"
            " join finance.instruments i on i.id = p.instrument_id"
            " join finance.instruments q on q.id = p.quote_instrument_id"
            " where p.source = 'etoro' order by 1, 4"
        )
    )
    state["prices"] = [tuple(r) for r in prices]
    snapshots = await session.execute(
        text(
            "select a.name, i.symbol, s.quantity, s.as_of, s.as_of_date"
            " from finance.balance_snapshots s"
            " join finance.instruments i on i.id = s.instrument_id"
            " join finance.accounts a on a.id = s.account_id"
            " where a.source_connection_id = :c order by 1, 2, 4"
        ),
        {"c": connection.id},
    )
    state["snapshots"] = [tuple(r) for r in snapshots]
    for table in _TYPED_TABLES:
        found = await session.execute(
            text(
                "select (to_jsonb(r) - 'transaction_id' - 'snapshot_id')::text as row,"
                " t.external_id, s.as_of from finance."
                + table
                + " r left join finance.transactions t"
                " on t.id = (to_jsonb(r) ->> 'transaction_id')::uuid"
                " left join finance.balance_snapshots s"
                " on s.id = (to_jsonb(r) ->> 'snapshot_id')::uuid"
                " where r.connection_id = :c order by 1"
            ),
            {"c": connection.id},
        )
        state[table] = [tuple(r) for r in found]
    return state


async def _delete_derived(session, connection) -> None:
    """Everything normalize writes for the connection, leaving its raw records, sightings,
    accounts and instruments (parent §6.6)."""
    c = {"c": connection.id}
    for table in _TYPED_TABLES:
        await session.execute(text(f"delete from finance.{table} where connection_id = :c"), c)
    await session.execute(text("delete from finance.transactions where connection_id = :c"), c)
    await session.execute(
        text(
            "delete from finance.balance_snapshots s using finance.accounts a"
            " where a.id = s.account_id and a.source_connection_id = :c"
        ),
        c,
    )
    await session.execute(text("delete from finance.prices where source = 'etoro'"))


async def test_regenerate_the_ledger_from_raw_records(session, ledger) -> None:
    first, second = date(2026, 5, 30), date(2026, 5, 31)
    closed, partial, remainder = POSITION + 10, POSITION, POSITION + 1
    history = history_body(
        [
            (
                first,
                [(CASH_ACCOUNT, "Cash", "GBP", "300.00"), (TRADING_ACCOUNT, "Trading", "USD", "0")],
            ),
            (
                second,
                [(CASH_ACCOUNT, "Cash", "GBP", "350.00"), (TRADING_ACCOUNT, "Trading", "USD", "5")],
            ),
        ]
    )
    # Day one: two positions open; balance history, candles, the current rate, eToro Money.
    await ledger.normalize(
        BALANCES,
        ("me", me_json()),
        (
            "pnl",
            pnl_body(
                [
                    pnl_position(partial, units="10", amount="1000.00"),
                    pnl_position(closed, units="2", amount="250.00"),
                ]
            ),
        ),
        ("aggregate", aggregate_body([(INSTRUMENT_ID, "USD", "12")])),
        ("breakdown", breakdown_body([breakdown_position(partial), breakdown_position(closed)])),
        META,
        ("order_lookup", lookup_body(ORDER, partial, fees="0.75")),
        ("order_lookup", lookup_body(closed + 500, closed)),
        ("balance_history", history),
        ("candles", candle_item(first, "124.10")),
        ("candles", candle_item(second, "125.20")),
        ("cash_transactions", bank_item("80000111", "50.00")),
        (
            "cash_transactions",
            bank_item("80000112", "10.00", "debit", ("bankAccountNumber", "12345678")),
        ),
    )
    # Day two: one closed in full, the other in part, its remainder a position of its own.
    partial_row = trade_row(partial) | {
        "investment": Decimal("400.00"),
        "netProfit": Decimal("20.00"),
        "units": Decimal("4"),
        "orderId": ORDER,
    }
    closed_row = trade_row(closed) | {
        "investment": Decimal("250.00"),
        "netProfit": Decimal("-10.00"),
        "units": Decimal("2"),
        "fees": Decimal("0.50"),
        "orderId": closed + 500,
    }
    await ledger.normalize(
        BALANCES,
        (
            "pnl",
            pnl_body(
                [pnl_position(remainder, units="6", amount="600.00", parentPositionID=partial)]
            ),
        ),
        ("aggregate", aggregate_body([(INSTRUMENT_ID, "USD", "6")])),
        ("breakdown", breakdown_body([breakdown_position(remainder, rate="127.5")])),
        ("trade_history", partial_row),
        ("trade_history", closed_row),
        ("candles", candle_item(NOW.date(), "126.00")),
        now=NOW + timedelta(days=1),
    )
    before = await _ledger_state(session, ledger.connection)
    keys = [t[0] for t in before["transactions"]]
    for key in (f"open:{partial}", f"close:{partial}", f"open:{remainder}", f"close:{closed}"):
        assert key in keys
    assert any(k.startswith(f"opening:{CASH_ACCOUNT}:") for k in keys)
    assert before["prices"] and before["snapshots"]

    await _delete_derived(session, ledger.connection)
    await ledger.replay()
    assert await _ledger_state(session, ledger.connection) == before


# Review fixes (Task 10, round 2) --------------------------------------------------------------


async def test_a_breakdown_position_without_its_instrument_id(session, ledger) -> None:
    # The breakdown's position names no instrumentId: it is its parent instrument's.
    position = breakdown_position(POSITION)
    del position["instrumentId"]
    other = 9002
    row = trade_row(POSITION + 7) | {"instrumentId": other, "orderId": POSITION + 507}
    report = await ledger.normalize(
        BALANCES,
        ("pnl", pnl_body([pnl_position(POSITION)])),
        (
            "breakdown",
            {
                "accountCurrency": "USD",
                "instruments": [
                    {"instrumentId": INSTRUMENT_ID, "symbol": SYMBOL, "positions": [position]}
                ],
            },
        ),
        META,
        ("trade_history", row),
    )
    # Its NULL instrumentId does not hide another instrument's unknown currency.
    assert "instrument currency unknown eToro 9002" in report.flags_in_full
    [linked] = await _rows(
        session,
        "select p.finance_instrument_id from finance.etoro_breakdown_positions p"
        " where p.connection_id = :c",
        c=ledger.connection.id,
    )
    assert linked.finance_instrument_id == await _etoro_instrument(session)
    assert await _postings(session, f"open:{POSITION}") != []


async def test_a_leveraged_close_is_not_counted_as_settlement_unknown(session, ledger) -> None:
    # No source states its settlement, but its leverage already makes it unsupported.
    row = trade_row(POSITION) | {"leverage": 2, "instrumentId": INSTRUMENT_ID, "orderId": ORDER}
    report = await ledger.normalize(
        BALANCES,
        ("aggregate", aggregate_body([(INSTRUMENT_ID, "USD", "0")])),
        META,
        ("trade_history", row),
    )
    assert await _transaction(session, f"close:{POSITION}") is None
    assert "a CFD, leveraged or copied position" in report.flagged
    assert not any(f.startswith("position settlement unknown") for f in report.flags_in_full)


async def test_an_account_number_row_pairs_with_a_monzo_payment(session, run, ledger) -> None:
    # The owner's bank (registered) pays eToro Money (registered too); eToro's row gives the
    # bank's account number alone.
    bank = await run(
        lambda ctx: create_account(
            ctx,
            name="Synthetic Bank",
            ledger_type="asset",
            subtype="bank",
            balance_mode="transactions",
        )
    )
    await run(lambda ctx: assign_identifier(ctx, bank.id, "uk_bank_account", "040004:12345678"))
    item = bank_item("80000121", "75.00", identifier=("bankAccountNumber", "12345678")) | {
        "postedAt": "2026-06-01T10:00:00Z"
    }
    await ledger.normalize(BALANCES, ("cash_transactions", item))
    assert (await _postings(session, "cash:80000121"))[1][0] == "Unassigned"
    money = await session.scalar(
        text("select id from finance.accounts where name = :n"), {"n": MONEY}
    )
    await run(lambda ctx: assign_identifier(ctx, money, "uk_bank_account", "608371:87654321"))
    names_money = await session.scalar(
        text("select id from finance.account_identifiers where value = '608371:87654321'")
    )
    gbp = await get_currency(session, "GBP")
    contra = await transfer_contra(
        session,
        source=None,
        own_account_id=bank.id,
        counterparty_identifier_id=names_money,
        other_postings=0,
    )
    await run(
        lambda ctx: post_transaction(
            ctx,
            TransactionInput(
                occurred_at=datetime(2026, 6, 1, 9, 59, tzinfo=UTC),
                description="Faster payment",
                postings=[
                    PostingInput(bank.id, gbp.id, Decimal("-75.00")),
                    PostingInput(contra, gbp.id, Decimal("75.00")),
                ],
                counterparty=CounterpartyFields(identifier_id=names_money, bank_transfer=True),
            ),
        )
    )
    async with command_context(
        session, Actor("system", "transfers"), "finance.heal_transfers", {}
    ) as ctx:
        await heal_transfers(ctx)
    assert (await _postings(session, "cash:80000121"))[1][0] == "In transit"
    [transfer] = await list_transfers(session)
    assert (transfer.from_account_id, transfer.status) == (bank.id, "matched")
