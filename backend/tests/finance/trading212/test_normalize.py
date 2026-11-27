"""The Trading 212 normalizer: raw records into the ledger (design §5.5-5.10)."""

import hashlib
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
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
from lararium.modules.finance import connector_run
from lararium.modules.finance.accounts import close_account, create_account
from lararium.modules.finance.instruments import InstrumentIdentifier, create_instrument
from lararium.modules.finance.ledger import find_unbalanced_transactions
from lararium.modules.finance.readonly_http import loads_decimal, record_json
from lararium.modules.finance.report_sightings import record_sighting
from lararium.modules.finance.trading212 import instruments as t212_instruments
from lararium.modules.finance.trading212 import sync as t212_sync
from lararium.modules.finance.trading212.client import Trading212Client
from lararium.modules.finance.trading212.fields import ENDPOINTS
from lararium.modules.finance.trading212.normalize import (
    NORMALIZER_VERSION,
    NormalizeReport,
    Trading212DataError,
    normalize,
)
from lararium.modules.finance.transfers import heal_transfers, list_transfers

from .fixtures import (
    ISIN,
    TICKER,
    dividend_item,
    dumps,
    instrument,
    instrument_json,
    isin,
    order_item,
    pie_json,
    position_json,
    summary_json,
    transaction_item,
)
from .test_sync import FakeTrading212, _run_sync, _sync
from .test_sync import _connection as _sync_connection

CONNECTOR = Actor("connector", "trading212")
# The sync's time: a day after the fills of the fixtures.
NOW = datetime(2026, 3, 3, 12, 0, tzinfo=UTC)
ACCOUNT = "Trading 212 Invest ···0001"
# A summary that reports nothing: a run of it alone posts no opening (asserted in
# test_account_named_registered_and_wrapped). A test that also posts a history gets the opening
# its difference makes (records.openings); the tests that look at openings say so.
NO_CASH = {"availableToTrade": Decimal(0), "reservedForOrders": Decimal(0), "inPies": Decimal(0)}
SUMMARY = ("summary", summary_json(cash=NO_CASH))
META = ("instruments", instrument_json())
GB_ISIN = isin("GB00ZZQAAAA")


class Notifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> None:
        self.messages.append(text)


async def _connection(run, kind: str = "invest") -> ConnectionRecord:
    return await run(
        lambda ctx: create_connection(
            ctx,
            connector="trading212",
            display_name="Trading 212 Invest",
            config={"account_kind": kind},
        )
    )


class Ledger:
    """One connection's raw records, normalized as a sync does."""

    def __init__(self, session, connection: ConnectionRecord) -> None:
        self.session = session
        self.connection = connection
        self.notifier = Notifier()
        self.ctx: SyncContext | None = None
        self.last: tuple[uuid.UUID, list[uuid.UUID]] = (uuid.uuid4(), [])

    async def store(
        self, endpoint: str, item: dict[str, Any], sync_id: uuid.UUID, fetched_at: datetime
    ) -> uuid.UUID:
        """As store_raw_record does, but fetched at the given time: a sync's time is the
        database's clock, and a test sets its own."""
        r = core_models.RawRecord
        # As the sync stores it: the API's text, every number its exact Decimal text.
        content = record_json(loads_decimal(dumps(item)))
        canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
        values = {
            "connection_id": self.connection.id,
            "record_type": ENDPOINTS[endpoint].record_type,
            "content_hash": hashlib.sha256(canonical.encode()).hexdigest(),
        }
        inserted = await self.session.scalar(
            insert(r)
            .values(
                **values,
                sync_id=sync_id,
                request={"endpoint": endpoint, "params": {}},
                content=content,
                connector_version="trading212-sync-1",
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
        self.ctx = SyncContext(
            command=CommandContext(self.session, CONNECTOR, uuid.uuid4()),
            connection=await get_connection(self.session, self.connection.id),
            sync_id=sync_id or uuid.uuid4(),
            master_key=bytes(32),
            http=httpx.AsyncClient(transport=httpx.MockTransport(_refuse)),
            notifier=self.notifier,
            session_factory=async_sessionmaker(bind=self.session.bind),
        )
        return self.ctx

    async def ids(
        self, sync_id: uuid.UUID, *entries: tuple[str, dict[str, Any]], now: datetime = NOW
    ) -> list[uuid.UUID]:
        """The entries stored by one sync at `now`, each report sighted then, as the sync
        does."""
        ids: list[uuid.UUID] = []
        for endpoint, item in entries:
            raw_record_id = await self.store(endpoint, item, sync_id, now)
            if endpoint in t212_sync.REPORTS:
                await record_sighting(
                    CommandContext(self.session, CONNECTOR, uuid.uuid4()),
                    connection_id=self.connection.id,
                    raw_record_id=raw_record_id,
                    sync_id=sync_id,
                    seen_at=now,
                )
            ids.append(raw_record_id)
        return ids

    async def normalize(
        self, *entries: tuple[str, dict[str, Any]], now: datetime = NOW
    ) -> NormalizeReport:
        """A sync at `now` that fetched the entries."""
        sync_id = uuid.uuid4()
        ids = await self.ids(sync_id, *entries, now=now)
        self.last = (sync_id, ids)
        return await normalize(await self.context(sync_id), ids)

    async def again(self) -> NormalizeReport:
        """The last sync's records normalized again."""
        sync_id, ids = self.last
        return await normalize(await self.context(sync_id), ids)

    async def replay(self) -> NormalizeReport:
        """A new normalizer version: every stored record replayed, nothing new fetched."""
        await update_config(
            CommandContext(self.session, CONNECTOR, uuid.uuid4()),
            self.connection.id,
            {"normalizer_version": "trading212-norm-0"},
        )
        return await normalize(await self.context(), [])

    def events(self) -> int:
        assert self.ctx is not None
        return len(self.ctx.command._events)  # pyright: ignore[reportPrivateUsage]

    def changes(self) -> int:
        assert self.ctx is not None
        return len(self.ctx.command._changes)  # pyright: ignore[reportPrivateUsage]


def _refuse(request: httpx.Request) -> httpx.Response:
    raise AssertionError("no HTTP in this test")


async def _postings(session, external_id: str) -> list[tuple]:
    """(account name, instrument symbol, quantity, cost, cost symbol, price, price symbol,
    is_fee, memo) of the transaction's postings, in order."""
    rows = await session.execute(
        text(
            "select a.name, i.symbol, p.quantity, p.cost_per_unit, ci.symbol as cost_symbol,"
            " p.price_per_unit, pi.symbol as price_symbol, p.is_fee, p.memo"
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
                " counterparty_identifier_id, status"
                " from finance.transactions where external_id = :e"
            ),
            {"e": external_id},
        )
    ).one_or_none()


async def _row(session, table: str, where: str, **params: Any):
    return (
        await session.execute(text(f"select * from finance.{table} where {where}"), params)
    ).one_or_none()


async def _identifiers(session, instrument_symbol: str) -> set[tuple[str, str]]:
    rows = await session.execute(
        text(
            "select ii.scheme, ii.value from finance.instrument_identifiers ii"
            " join finance.instruments i on i.id = ii.instrument_id where i.symbol = :s"
        ),
        {"s": instrument_symbol},
    )
    return {(r.scheme, r.value) for r in rows}


def _fill(**overrides: Any) -> dict[str, Any]:
    """An order item whose fill fields are overridden."""
    item = order_item()
    item["fill"] |= overrides
    return item


async def test_buy_usd_stock_with_fx_fee(session, run) -> None:
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    await ledger.normalize(SUMMARY, META, ("orders", order_item()))

    # The cost weighs +240.00 GBP against the cash -240.00; the fee -1.20 on the account
    # against Unassigned +1.20: the account's GBP falls by 241.20, the netValue.
    assert await _postings(session, "order:700001") == [
        (ACCOUNT, "ZZQA", Decimal(3), Decimal("80.00"), "GBP", None, None, False, ""),
        (ACCOUNT, "GBP", Decimal("-240.00"), None, None, None, None, False, ""),
        (ACCOUNT, "GBP", Decimal("-1.20"), None, None, None, None, True, "CURRENCY_CONVERSION_FEE"),
        ("Unassigned", "GBP", Decimal("1.20"), None, None, None, None, False, ""),
    ]
    posted = await _transaction(session, "order:700001")
    assert posted.description == "Buy 3 ZZQA"
    assert posted.occurred_at == datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
    assert posted.booking_date == date(2026, 3, 2)
    assert await find_unbalanced_transactions(session) == []

    order = await _row(session, "trading212_orders", "external_id = 'order:700001'")
    assert order.transaction_id == posted.id
    assert order.order_side == "BUY"
    assert order.fill_price == Decimal("100.00")
    assert order.fill_wallet_impact_fx_rate == Decimal("0.8")
    assert order.fill_wallet_impact_net_value == Decimal("241.20")
    assert order.order_instrument_isin == ISIN
    tax = await _row(session, "trading212_order_taxes", "external_id = 'order:700001'")
    assert (tax.item_index, tax.name, tax.quantity, tax.currency) == (
        0,
        "CURRENCY_CONVERSION_FEE",
        Decimal("1.20"),
        "GBP",
    )
    # The fill's price, in the instrument's currency, on its day.
    price = await _row(session, "prices", "source = 'trading212'")
    assert (price.price, price.as_of_date) == (Decimal("100.00"), date(2026, 3, 2))
    assert await _identifiers(session, "ZZQA") == {("trading212_ticker", TICKER), ("isin", ISIN)}


async def test_account_named_registered_and_wrapped(session, run) -> None:
    connection = await _connection(run)
    report = await Ledger(session, connection).normalize(SUMMARY)
    assert report.accounts_created == 1
    account = await _row(session, "accounts", "source_connection_id = :c", c=connection.id)
    assert (account.name, account.ledger_type, account.subtype) == (ACCOUNT, "asset", "broker")
    assert (account.balance_mode, account.tax_wrapper) == ("transactions", "gia")
    registered = await _row(
        session, "account_identifiers", "kind = 'trading212_account' and value = '10000001'"
    )
    assert registered.account_id == account.id
    stored = await get_connection(session, connection.id)
    assert stored.config["normalizer_version"] == NORMALIZER_VERSION

    isa = await _connection(run, "isa")
    await Ledger(session, isa).normalize(
        ("summary", summary_json(account_id=20000002, cash=NO_CASH))
    )
    account = await _row(session, "accounts", "source_connection_id = :c", c=isa.id)
    openings = text("select count(*) from finance.transactions where external_id like 'opening:%'")
    assert await session.scalar(openings) == 0
    assert (account.name, account.tax_wrapper) == ("Trading 212 ISA ···0002", "isa")


async def test_account_gets_its_type_key_and_regains_a_cleared_one(session, run) -> None:
    invest = await _connection(run)
    isa = await _connection(run, "isa")
    await Ledger(session, invest).normalize(SUMMARY)
    await Ledger(session, isa).normalize(("summary", summary_json(account_id=20000002)))
    keys = text("select type_key from finance.accounts where source_connection_id = :c")
    assert await session.scalar(keys, {"c": invest.id}) == "trading212.invest"
    assert await session.scalar(keys, {"c": isa.id}) == "trading212.stocks_isa"

    await session.execute(
        text("update finance.accounts set type_key = NULL where source_connection_id = :c"),
        {"c": invest.id},
    )
    report = await Ledger(session, invest).normalize(SUMMARY)
    assert await session.scalar(keys, {"c": invest.id}) == "trading212.invest"
    assert report.accounts_updated == 1


async def test_sell_weighs_by_price(session, run) -> None:
    connection = await _connection(run)
    sell = order_item(order_id=500002, fill_id=700002, side="SELL", net_value="238.80")
    await Ledger(session, connection).normalize(SUMMARY, META, ("orders", sell))
    assert await _postings(session, "order:700002") == [
        (ACCOUNT, "ZZQA", Decimal(-3), None, None, Decimal("80.00"), "GBP", False, ""),
        (ACCOUNT, "GBP", Decimal("240.00"), None, None, None, None, False, ""),
        (ACCOUNT, "GBP", Decimal("-1.20"), None, None, None, None, True, "CURRENCY_CONVERSION_FEE"),
        ("Unassigned", "GBP", Decimal("1.20"), None, None, None, None, False, ""),
    ]
    assert (await _transaction(session, "order:700002")).description == "Sell 3 ZZQA"


async def test_the_side_decides_the_sign(session, run) -> None:
    connection = await _connection(run)
    sell = order_item(order_id=500002, fill_id=700002, side="SELL", net_value="238.80")
    sell["fill"]["quantity"] = Decimal(-3)
    await Ledger(session, connection).normalize(SUMMARY, META, ("orders", sell))
    [instrument_leg, *_] = await _postings(session, "order:700002")
    assert instrument_leg[2] == Decimal(-3)


async def test_split_from_unassigned(session, run) -> None:
    connection = await _connection(run)
    split = _fill(type="STOCK_SPLIT", price=Decimal(0), quantity=Decimal(3))
    split["fill"]["walletImpact"] |= {"netValue": Decimal(0), "taxes": []}
    await Ledger(session, connection).normalize(SUMMARY, META, ("orders", split))
    assert await _postings(session, "order:700001") == [
        (ACCOUNT, "ZZQA", Decimal(3), None, None, None, None, False, ""),
        ("Unassigned", "ZZQA", Decimal(-3), None, None, None, None, False, ""),
    ]
    assert (await _transaction(session, "order:700001")).description == "Stock split ZZQA"
    # A split has no price.
    assert await _row(session, "prices", "source = 'trading212'") is None


async def test_dividend_against_unassigned(session, run) -> None:
    connection = await _connection(run)
    await Ledger(session, connection).normalize(SUMMARY, META, ("dividends", dividend_item()))
    assert await _postings(session, "dividend:div-0001") == [
        (ACCOUNT, "GBP", Decimal("1.23"), None, None, None, None, False, ""),
        ("Unassigned", "GBP", Decimal("-1.23"), None, None, None, None, False, ""),
    ]
    posted = await _transaction(session, "dividend:div-0001")
    assert (posted.description, posted.booking_date) == ("Dividend ZZQA", date(2026, 4, 1))
    row = await _row(session, "trading212_dividends", "reference = 'div-0001'")
    assert row.transaction_id == posted.id
    assert row.gross_amount_per_share == Decimal("0.51")


async def test_deposit_through_transfer_contra(session, run) -> None:
    connection = await _connection(run)
    await Ledger(session, connection).normalize(SUMMARY, ("transactions", transaction_item()))
    key = "transaction:00000000-0000-4000-8000-000000000001"
    # A card deposit (design §5.12): no counterparty, not a bank transfer, so Unassigned
    # until a card merchant pairs it.
    assert await _postings(session, key) == [
        (ACCOUNT, "GBP", Decimal("250.00"), None, None, None, None, False, ""),
        ("Unassigned", "GBP", Decimal("-250.00"), None, None, None, None, False, ""),
    ]
    posted = await _transaction(session, key)
    assert (posted.description, posted.bank_transfer) == ("Deposit", False)
    assert posted.counterparty_identifier_id is None
    row = await _row(
        session, "trading212_transactions", "reference = :r", r=key.removeprefix("transaction:")
    )
    assert (row.transaction_id, row.type, row.amount) == (posted.id, "DEPOSIT", Decimal("250.00"))


async def test_transaction_signs_and_transfer_flag(session, run) -> None:
    connection = await _connection(run)
    rows = {
        "w": ("WITHDRAW", "10.00", "-10.00"),
        "f": ("FEE", "-2.00", "-2.00"),
        "i": ("INTEREST_ON_FREE_CASH", "0.50", "0.50"),
        "l": ("LENDING_INTEREST", "-0.10", "0.10"),
        "t": ("TRANSFER", "-20.00", "-20.00"),
    }
    entries = [
        ("transactions", transaction_item(reference=ref, kind=kind, amount=amount))
        for ref, (kind, amount, _) in rows.items()
    ]
    report = await Ledger(session, connection).normalize(SUMMARY, *entries)
    for ref, (_, _, posted) in rows.items():
        [own, contra] = await _postings(session, f"transaction:{ref}")
        assert (own[0], own[2], contra[0], contra[2]) == (
            ACCOUNT,
            Decimal(posted),
            "Unassigned",
            -Decimal(posted),
        )
    # Until a real transfer shows its sign, the owner is told to check it.
    assert "TRANSFER" in report.flagged


async def test_cancelled_order_no_transaction(session, run) -> None:
    connection = await _connection(run)
    await Ledger(session, connection).normalize(SUMMARY, META, ("orders", order_item(fill_id=None)))
    count = await session.scalar(
        text("select count(*) from finance.transactions where external_id like 'order%'")
    )
    assert count == 0
    row = await _row(session, "trading212_orders", "external_id = 'order-none:500001'")
    assert (row.transaction_id, row.order_status) == (None, "CANCELLED")


async def test_same_isin_merges_with_ibkr_listing(session, run, gbp) -> None:
    ibkr = await run(
        lambda ctx: create_instrument(
            ctx,
            kind="etf",
            symbol="ZZQA",
            name="Zzqa Example ETF",
            decimals=4,
            quote_instrument_id=gbp.id,
            identifiers=[
                InstrumentIdentifier("ibkr_conid", "1001"),
                InstrumentIdentifier("isin", GB_ISIN),
            ],
        )
    )
    connection = await _connection(run)
    ticker = "ZZQAl_EQ"
    listing = instrument(ticker, GB_ISIN, "GBP")
    buy = order_item()
    buy["order"] |= {"ticker": ticker, "instrument": listing}
    buy["fill"]["walletImpact"]["fxRate"] = Decimal(1)
    meta = instrument_json(ticker, isin=GB_ISIN, currencyCode="GBP", type="ETF")
    await Ledger(session, connection).normalize(SUMMARY, ("instruments", meta), ("orders", buy))

    [instrument_leg, *_] = await _postings(session, "order:700001")
    assert instrument_leg[:3] == (ACCOUNT, "ZZQA", Decimal(3))
    assert await _identifiers(session, "ZZQA") == {
        ("ibkr_conid", "1001"),
        ("isin", GB_ISIN),
        ("trading212_ticker", ticker),
    }
    count = await session.scalar(
        text("select count(*) from finance.instruments where symbol = 'ZZQA'")
    )
    assert count == 1
    row = await _row(session, "trading212_instruments", "ticker = :t", t=ticker)
    assert row.instrument_id == ibkr.id


async def test_renamed_ticker_found_by_isin(session, run) -> None:
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    await ledger.normalize(SUMMARY, META, ("orders", order_item()))

    # Renamed: the metadata lists the new ticker only, and the position carries it.
    renamed = "ZZQB_US_EQ"
    position = position_json(renamed)
    await ledger.normalize(
        SUMMARY, ("instruments", instrument_json(renamed)), ("positions", position)
    )
    assert await _identifiers(session, "ZZQA") == {("trading212_ticker", renamed), ("isin", ISIN)}
    count = await session.scalar(
        text("select count(*) from finance.instruments where kind = 'equity'")
    )
    assert count == 1

    # The history keeps the old ticker: it finds the instrument by ISIN and leaves the
    # ticker the metadata lists.
    again = order_item(order_id=500003, fill_id=700003)
    await ledger.normalize(SUMMARY, ("orders", again))
    [instrument_leg, *_] = await _postings(session, "order:700003")
    assert instrument_leg[1] == "ZZQA"
    assert await _identifiers(session, "ZZQA") == {("trading212_ticker", renamed), ("isin", ISIN)}


async def test_two_held_tickers_of_one_listing_keep_their_identifier(session, run) -> None:
    """Two tickers this sync holds that share an ISIN and currency: neither takes the ticker
    identifier from the other, so finding them again writes nothing."""
    fields = {"name": "Zzqa Example Corp", "isin": ISIN, "currency": "USD", "type": "STOCK"}
    other = "ZZQB_US_EQ"
    held = frozenset({TICKER, other})
    first = await run(t212_instruments.listing, fields | {"ticker": TICKER}, current=True)
    assert (
        await run(t212_instruments.listing, fields | {"ticker": other}, current=True, held=held)
        == first
    )
    before = await _identifiers(session, "ZZQA")
    updated = text("select count(*) from core.events where type = 'finance.instrument.updated'")
    changes = await session.scalar(updated)

    for ticker in (TICKER, other, TICKER):
        found = await run(
            t212_instruments.listing, fields | {"ticker": ticker}, current=True, held=held
        )
        assert found == first

    assert (
        await _identifiers(session, "ZZQA")
        == before
        == {
            ("trading212_ticker", TICKER),
            ("isin", ISIN),
        }
    )
    assert await session.scalar(updated) == changes


async def test_unknown_type_flagged_not_posted(session, run) -> None:
    connection = await _connection(run)
    meta = instrument_json(type="WARRANT")
    report = await Ledger(session, connection).normalize(
        SUMMARY,
        ("instruments", meta),
        ("orders", order_item()),
        ("dividends", dividend_item()),
        ("positions", position_json()),
    )
    assert "instruments.type=WARRANT" in report.flagged
    count = await session.scalar(text("select count(*) from finance.transactions"))
    assert count == 0
    order = await _row(session, "trading212_orders", "external_id = 'order:700001'")
    assert order.transaction_id is None
    assert (
        await session.scalar(text("select count(*) from finance.instruments where symbol = 'ZZQA'"))
        == 0
    )


async def test_unknown_fields_and_values_flagged(session, run) -> None:
    connection = await _connection(run)
    odd = order_item()
    odd["order"]["newThing"] = "x"
    odd["fill"]["type"] = "NEW_FILL"
    report = await Ledger(session, connection).normalize(
        ("summary", summary_json(cash={**NO_CASH, "spendingPot": Decimal(1)})),
        META,
        ("orders", odd),
        ("transactions", transaction_item(kind="CASHBACK")),
    )
    assert {
        "summary.cash.spendingPot",
        "orders.order.newThing",
        "orders.fill.type=NEW_FILL",
        "transactions.type=CASHBACK",
    } <= report.flagged
    # A value the mapping does not know posts nothing; a field it does not know is kept raw.
    assert await session.scalar(text("select count(*) from finance.transactions")) == 0


async def test_an_unknown_field_is_named_only_by_plain_names(session, run) -> None:
    """The server writes the JSON keys: one that is not a plain name (letters, digits,
    underscore, of bounded length) is shown as `{}`, never as written."""
    connection = await _connection(run)
    odd = order_item()
    odd["order"]["see https://x.test/"] = "x"
    odd["order"]["n" * 65] = "x"
    odd["order"]["plain_Name2"] = {"a b": "x"}
    report = await Ledger(session, connection).normalize(SUMMARY, META, ("orders", odd))
    shown = {flag for flag in report.flagged if flag.startswith("orders.order.")}
    assert shown == {"orders.order.{}", "orders.order.plain_Name2.{}"}


async def test_an_unbalanced_fill_fails_naming_it(session, run) -> None:
    connection = await _connection(run)
    # A sell whose cash goes out: its postings cannot balance.
    sell = order_item(order_id=500009, fill_id=700009, side="SELL", net_value="-238.80")
    with pytest.raises(Trading212DataError) as failed:
        await Ledger(session, connection).normalize(SUMMARY, META, ("orders", sell))
    # Named by its fill id's last four characters (design §5.13), never in full.
    assert "···0009" in str(failed.value)
    assert "700009" not in str(failed.value)
    assert "238" not in str(failed.value)


async def test_a_trade_fill_without_quantity_fails_naming_its_ending(session, run) -> None:
    connection = await _connection(run)
    empty = order_item(order_id=500009, fill_id=700009)
    empty["fill"]["quantity"] = Decimal(0)
    with pytest.raises(Trading212DataError) as failed:
        await Ledger(session, connection).normalize(SUMMARY, META, ("orders", empty))
    assert "···0009" in str(failed.value) and "700009" not in str(failed.value)


async def test_one_key_two_contents_in_a_run_fails(session, run) -> None:
    connection = await _connection(run)
    first = order_item()
    second = order_item(net_value="241.30")
    with pytest.raises(Trading212DataError, match=r"trading212\.order") as failed:
        await Ledger(session, connection).normalize(
            SUMMARY, META, ("orders", first), ("orders", second)
        )
    assert "700001" not in str(failed.value)


async def test_a_required_field_missing_fails(session, run) -> None:
    connection = await _connection(run)
    item = transaction_item()
    del item["amount"]
    with pytest.raises(Trading212DataError, match=r"trading212\.transaction lacks amount"):
        await Ledger(session, connection).normalize(SUMMARY, ("transactions", item))


async def test_a_later_copy_reposts_in_place(session, run) -> None:
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    await ledger.normalize(SUMMARY, ("dividends", dividend_item()))
    report = await ledger.normalize(SUMMARY, ("dividends", dividend_item(amount="1.25")))
    assert report.updated == 1
    [own, _] = await _postings(session, "dividend:div-0001")
    assert own[2] == Decimal("1.25")


async def test_positions_price_the_sync_day(session, run) -> None:
    connection = await _connection(run)
    await Ledger(session, connection).normalize(
        SUMMARY, META, ("orders", order_item()), ("positions", position_json())
    )
    rows = await session.execute(
        text(
            "select p.price, q.symbol, p.as_of_date from finance.prices p"
            " join finance.instruments q on q.id = p.quote_instrument_id"
            " where p.source = 'trading212' order by p.as_of_date"
        )
    )
    assert [tuple(r) for r in rows] == [
        (Decimal("100.00"), "USD", date(2026, 3, 2)),
        (Decimal("110.00"), "USD", date(2026, 3, 3)),
    ]
    # No FX rate is written (design §5.10).
    assert await session.scalar(text("select count(*) from finance.fx_rates")) == 0


def _everything() -> list[tuple[str, dict[str, Any]]]:
    exchange = {
        "id": Decimal(7),
        "name": "NYSE",
        "workingSchedules": [
            {"id": Decimal(71), "timeEvents": [{"date": "2026-03-03T14:30:00Z", "type": "OPEN"}]}
        ],
    }
    detail = {
        "instruments": [
            {
                "ticker": TICKER,
                "ownedQuantity": Decimal("0.5"),
                "currentShare": Decimal("1"),
                "expectedShare": Decimal("1"),
                "issues": [{"name": "DELISTED", "severity": "IRREVERSIBLE"}],
            }
        ],
        "settings": {"id": Decimal(9001), "name": "P", "instrumentShares": {TICKER: Decimal(1)}},
    }
    pending = {"id": Decimal(55), "ticker": TICKER, "side": "BUY", "status": "NEW"}
    return [
        SUMMARY,
        META,
        ("exchanges", exchange),
        ("positions", position_json()),
        ("pending_orders", pending),
        ("orders", order_item()),
        ("orders", order_item(order_id=500002, fill_id=None)),
        ("dividends", dividend_item()),
        ("transactions", transaction_item()),
        ("transactions", transaction_item(reference="t-2", kind="TRANSFER", amount="-5")),
        ("pies", pie_json()),
        ("pie", detail),
    ]


async def test_every_table_is_filled(session, run) -> None:
    connection = await _connection(run)
    await Ledger(session, connection).normalize(*_everything())
    for table in (
        "trading212_accounts",
        "trading212_positions",
        "trading212_pending_orders",
        "trading212_orders",
        "trading212_order_taxes",
        "trading212_dividends",
        "trading212_transactions",
        "trading212_instruments",
        "trading212_exchanges",
        "trading212_exchange_schedules",
        "trading212_exchange_time_events",
        "trading212_pies",
        "trading212_pie_instruments",
        "trading212_pie_instrument_issues",
        "trading212_pie_instrument_shares",
    ):
        count = await session.scalar(
            text(f"select count(*) from finance.{table} where connection_id = :c"),
            {"c": connection.id},
        )
        assert count, table
    meta = await _row(session, "trading212_instruments", "ticker = :t", t=TICKER)
    assert meta.exchange_id == Decimal(7)
    pie = await _row(session, "trading212_pies", "id = 9001")
    assert (pie.status, pie.settings_name, pie.as_of_date) == ("ON_TRACK", "P", date(2026, 3, 3))
    assert pie.detail_raw_record_id is not None


async def test_renormalize_writes_nothing(session, run, monkeypatch) -> None:
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    entries = _everything()
    await ledger.normalize(*entries)

    written: list[str] = []
    upsert = connector_run.upsert_typed

    async def spy(ctx, table, key, values) -> bool:
        wrote = await upsert(ctx, table, key, values)
        if wrote:
            written.append(table)
        return wrote

    monkeypatch.setattr(connector_run, "upsert_typed", spy)
    report = await ledger.normalize(*entries, now=NOW.replace(hour=13))
    assert (report.created, report.updated) == (0, 0)
    assert report.unchanged > 0
    assert written == []
    assert (ledger.events(), ledger.changes()) == (0, 0)


async def test_flags_are_published_through_the_sync(session, run, monkeypatch) -> None:
    api = FakeTrading212()
    monkeypatch.setattr(
        t212_sync,
        "Trading212Client",
        partial(Trading212Client, sleep=api.sleep, clock=api.clock, wall_clock=api.clock),
    )
    api.history["transactions"] = [[transaction_item(reference="tx-1", kind="TRANSFER")]]
    connection = await _sync_connection(run)
    notifier = Notifier()
    after = await _run_sync(session, connection, api, notifier)
    assert after.status == "healthy"
    assert "Trading 212 flagged: TRANSFER" in after.warnings
    after = await _run_sync(session, connection, api, notifier)
    assert "Trading 212 flagged: TRANSFER" in after.warnings
    told = [m for m in notifier.messages if "TRANSFER" in m]
    assert len(told) == 1
    assert after.cursor["flags"]["fields"] == ["TRANSFER"]


def _api(monkeypatch) -> FakeTrading212:
    api = FakeTrading212()
    monkeypatch.setattr(
        t212_sync,
        "Trading212Client",
        partial(Trading212Client, sleep=api.sleep, clock=api.clock, wall_clock=api.clock),
    )
    return api


_REPORT_TABLES = (
    "trading212_accounts",
    "trading212_positions",
    "trading212_pending_orders",
    "trading212_pies",
    "trading212_pie_instruments",
    "trading212_pie_instrument_issues",
    "trading212_pie_instrument_shares",
)


async def _reports(session, connection) -> dict[str, list[Any]]:
    """The connection's report rows and reported snapshots, without their ids."""
    found: dict[str, list[Any]] = {}
    for table in _REPORT_TABLES:
        rows = await session.execute(
            text(
                f"select to_jsonb(t) - 'snapshot_id' as row, s.quantity, s.as_of"
                f" from finance.{table} t"
                + (
                    " left join finance.balance_snapshots s on s.id = t.snapshot_id"
                    if table in ("trading212_accounts", "trading212_positions")
                    else " left join (select null::numeric as quantity,"
                    " null::timestamptz as as_of) s on true"
                )
                + " where t.connection_id = :c"
            ),
            {"c": connection.id},
        )
        found[table] = sorted((str(sorted(r.row.items())), r.quantity, r.as_of) for r in rows)
    snapshots = await session.execute(
        text(
            "select s.instrument_id, s.quantity, s.as_of, s.as_of_date"
            " from finance.balance_snapshots s join finance.accounts a on a.id = s.account_id"
            " where a.source_connection_id = :c and s.kind = 'reported'"
            " order by s.instrument_id, s.as_of"
        ),
        {"c": connection.id},
    )
    found["snapshots"] = [tuple(r) for r in snapshots]
    return found


async def test_a_new_version_regenerates_every_report(session, run, monkeypatch) -> None:
    api = _api(monkeypatch)
    api.pending = [{"id": Decimal(55), "ticker": TICKER, "side": "BUY", "status": "NEW"}]
    api.pie_detail = {
        "instruments": [
            {
                "ticker": TICKER,
                "ownedQuantity": Decimal("0.5"),
                "issues": [{"name": "DELISTED", "severity": "IRREVERSIBLE"}],
            }
        ],
        "settings": {"id": Decimal(9001), "name": "P", "instrumentShares": {TICKER: Decimal(1)}},
    }
    connection = await _sync_connection(run)
    await _sync(session, connection, api)
    before = await _reports(session, connection)
    assert all(before.values()), before

    for table in ("trading212_accounts", "trading212_positions", "trading212_pending_orders"):
        await session.execute(
            text(f"delete from finance.{table} where connection_id = :c"), {"c": connection.id}
        )
    await session.execute(
        text("delete from finance.trading212_pies where connection_id = :c"), {"c": connection.id}
    )
    await session.execute(
        text(
            "delete from finance.balance_snapshots s using finance.accounts a"
            " where a.id = s.account_id and a.source_connection_id = :c"
        ),
        {"c": connection.id},
    )
    await run(
        lambda ctx: update_config(ctx, connection.id, {"normalizer_version": "trading212-norm-0"})
    )
    # The next sync fetches no pies (a day's), and its other reports are the same raw records:
    # the replay rebuilds every day from what is stored.
    await _sync(session, connection, api)
    assert await _reports(session, connection) == before


async def test_opening_when_the_history_is_short(session, run) -> None:
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    # The first sync reports 250.75 and 3 shares; the history it reaches holds a dividend.
    early = dividend_item(paidOn="2026-03-01T00:00:00Z")
    await ledger.normalize(
        ("summary", summary_json()), META, ("dividends", early), ("positions", position_json())
    )
    gbp = await _opening(session, "GBP")
    shares = await _opening(session, "ZZQA")
    assert (gbp.quantity, gbp.booking_date) == (Decimal("249.52"), date(2026, 2, 28))
    assert (shares.quantity, shares.booking_date) == (Decimal(3), date(2026, 2, 28))
    assert gbp.contra == "Opening balances"
    assert (await get_connection(session, connection.id)).warnings == []

    # The history reaches further back (a re-walk): what it explains leaves the opening.
    deposit = transaction_item(amount="490.72")
    await ledger.normalize(
        ("transactions", deposit), ("orders", order_item()), now=NOW + timedelta(days=1)
    )
    assert await _opening(session, "GBP") is None
    assert await _opening(session, "ZZQA") is None
    assert await find_unbalanced_transactions(session) == []


async def test_no_opening_with_the_full_history(session, run) -> None:
    connection = await _connection(run)
    await Ledger(session, connection).normalize(
        ("summary", summary_json()),
        META,
        ("transactions", transaction_item(amount="491.95")),
        ("orders", order_item()),
        ("positions", position_json()),
    )
    count = await session.scalar(
        text("select count(*) from finance.transactions where external_id like 'opening:%'")
    )
    assert count == 0


async def _opening(session, symbol: str):
    return (
        await session.execute(
            text(
                "select p.quantity, t.booking_date, ca.name as contra"
                " from finance.transactions t"
                " join finance.postings p on p.transaction_id = t.id and p.position = 0"
                " join finance.postings c on c.transaction_id = t.id and c.position = 1"
                " join finance.accounts ca on ca.id = c.account_id"
                " join finance.instruments i on i.id = p.instrument_id"
                " where t.external_id like 'opening:10000001:%' and t.status <> 'void'"
                " and i.symbol = :s"
            ),
            {"s": symbol},
        )
    ).one_or_none()


async def test_a_transfer_names_the_sibling_account(session, run) -> None:
    invest = Ledger(session, await _connection(run, "invest"))
    isa = Ledger(session, await _connection(run, "isa"))
    await invest.normalize(SUMMARY)
    await isa.normalize(("summary", summary_json(account_id=20000002, cash=NO_CASH)))

    later = NOW + timedelta(days=1)
    moved = {"dateTime": "2026-03-04T09:15:00Z"}
    out = transaction_item(reference="t-out", kind="TRANSFER", amount="-100.00", **moved)
    into = transaction_item(reference="t-in", kind="TRANSFER", amount="100.00", **moved)
    sent = await invest.normalize(SUMMARY, ("transactions", out), now=later)
    received = await isa.normalize(
        ("summary", summary_json(account_id=20000002, cash=NO_CASH)),
        ("transactions", into),
        now=later,
    )
    # The sign is still the owner's to check.
    assert "TRANSFER" in sent.flagged and "TRANSFER" in received.flagged
    named = await session.execute(
        text(
            "select t.external_id, i.value from finance.transactions t"
            " join finance.account_identifiers i on i.id = t.counterparty_identifier_id"
            " where t.external_id in ('transaction:t-out', 'transaction:t-in')"
            " order by t.external_id"
        )
    )
    assert [tuple(r) for r in named] == [
        ("transaction:t-in", "10000001"),
        ("transaction:t-out", "20000002"),
    ]

    async with command_context(
        session, Actor("system", "transfers"), "finance.heal_transfers", {}
    ) as ctx:
        await heal_transfers(ctx)
    [transfer] = await list_transfers(session)
    assert transfer.status == "matched"
    in_transit = await session.scalar(
        text(
            "select coalesce(sum(p.quantity), 0) from finance.postings p"
            " join finance.transactions t on t.id = p.transaction_id"
            " join finance.accounts a on a.id = p.account_id"
            " where a.name = 'In transit' and t.status <> 'void'"
        )
    )
    assert in_transit == 0


async def test_a_transfer_without_a_sibling_names_nothing(session, run) -> None:
    ledger = Ledger(session, await _connection(run))
    out = transaction_item(reference="t-out", kind="TRANSFER", amount="-100.00")
    report = await ledger.normalize(SUMMARY, ("transactions", out))
    assert "TRANSFER" in report.flagged
    posted = await _transaction(session, "transaction:t-out")
    assert posted.counterparty_identifier_id is None


async def test_an_instrument_without_metadata_is_flagged_until_typed(session, run) -> None:
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    report = await ledger.normalize(SUMMARY, ("orders", order_item()))
    # Posted, but not taken for a share.
    assert (await _transaction(session, "order:700001")) is not None
    kind = text("select kind from finance.instruments where symbol = 'ZZQA'")
    assert await session.scalar(kind) == "other"
    assert report.instrument_flags == {f"instrument type unknown {TICKER}"}

    # The metadata arrives: the instrument takes its type, and the flag is gone.
    report = await ledger.normalize(SUMMARY, META, now=NOW + timedelta(days=1))
    assert await session.scalar(kind) == "equity"
    assert report.instrument_flags == set()


# Review fixes: reports dated by sighting, regeneration, GBX, reclassification -------------


async def _ledger_state(session, connection) -> dict[str, Any]:
    """What normalize derives for the connection, by external id and content, no UUIDs."""
    state: dict[str, Any] = {}
    rows = await session.execute(
        text(
            "select t.external_id, t.status, t.occurred_at, t.booking_date, t.description,"
            " t.bank_transfer, ci.value as counterparty,"
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
            " where p.source = 'trading212' order by 1, 4"
        )
    )
    state["prices"] = [tuple(r) for r in prices]
    snapshots = await session.execute(
        text(
            "select i.symbol, s.quantity, s.as_of, s.as_of_date from finance.balance_snapshots s"
            " join finance.instruments i on i.id = s.instrument_id"
            " join finance.accounts a on a.id = s.account_id"
            " where a.source_connection_id = :c order by 1, 3"
        ),
        {"c": connection.id},
    )
    state["snapshots"] = [tuple(r) for r in snapshots]
    for table in ("trading212_orders", "trading212_dividends", "trading212_transactions"):
        rows = await session.execute(
            text(
                f"select to_jsonb(r) - 'transaction_id' as row, t.external_id"
                f" from finance.{table} r"
                f" left join finance.transactions t on t.id = r.transaction_id"
                f" where r.connection_id = :c"
            ),
            {"c": connection.id},
        )
        state[table] = sorted((str(sorted(r.row.items())), r.external_id) for r in rows)
    return state


async def _delete_derived(session, connection) -> None:
    """Everything normalize writes for the connection, leaving its raw records, sightings
    and account (parent §6.6)."""
    c = {"c": connection.id}
    for table in (
        *_REPORT_TABLES,
        "trading212_order_taxes",
        "trading212_orders",
        "trading212_dividends",
        "trading212_transactions",
        "trading212_instruments",
        "trading212_exchanges",
    ):
        await session.execute(text(f"delete from finance.{table} where connection_id = :c"), c)
    await session.execute(text("delete from finance.transactions where connection_id = :c"), c)
    await session.execute(
        text(
            "delete from finance.balance_snapshots s using finance.accounts a"
            " where a.id = s.account_id and a.source_connection_id = :c"
        ),
        c,
    )
    await session.execute(text("delete from finance.prices where source = 'trading212'"))


async def test_regenerate_the_ledger_from_raw_records(session, run) -> None:
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    await ledger.normalize(*_everything())
    later = order_item(order_id=500005, fill_id=700005, side="SELL", net_value="238.80")
    later["fill"]["filledAt"] = "2026-03-04T10:00:00Z"
    await ledger.normalize(
        ("summary", summary_json()),
        ("orders", later),
        ("positions", position_json(quantity="0.5", currentPrice=Decimal("111.00"))),
        now=NOW + timedelta(days=1),
    )
    before = await _ledger_state(session, connection)
    assert before["transactions"] and before["prices"] and before["snapshots"]

    await _delete_derived(session, connection)
    await ledger.replay()
    assert await _ledger_state(session, connection) == before


def _days(n: int) -> datetime:
    return NOW + timedelta(days=n)


async def test_a_report_that_comes_back_is_dated_by_each_sighting(session, run) -> None:
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    a = ("positions", position_json(quantity="3", currentPrice=Decimal("110.00")))
    b = ("positions", position_json(quantity="6", currentPrice=Decimal("120.00")))
    await ledger.normalize(SUMMARY, META, a, now=_days(0))
    await ledger.normalize(SUMMARY, b, now=_days(1))
    # A again: the same raw record as the first day's, seen on the third.
    await ledger.normalize(SUMMARY, a, now=_days(2))

    prices = await session.execute(
        text(
            "select as_of_date, price from finance.prices where source = 'trading212'"
            " order by as_of_date"
        )
    )
    assert [tuple(r) for r in prices] == [
        (date(2026, 3, 3), Decimal("110.00")),
        (date(2026, 3, 4), Decimal("120.00")),
        (date(2026, 3, 5), Decimal("110.00")),
    ]
    snapshots = await session.execute(
        text(
            "select s.as_of_date, s.quantity, s.as_of from finance.balance_snapshots s"
            " join finance.instruments i on i.id = s.instrument_id where i.symbol = 'ZZQA'"
            " order by s.as_of"
        )
    )
    assert [tuple(r) for r in snapshots] == [
        (date(2026, 3, 3), Decimal(3), _days(0)),
        (date(2026, 3, 4), Decimal(6), _days(1)),
        (date(2026, 3, 5), Decimal(3), _days(2)),
    ]
    rows = await session.execute(
        text("select as_of_date, quantity from finance.trading212_positions order by 1")
    )
    assert [tuple(r) for r in rows] == [
        (date(2026, 3, 3), Decimal(3)),
        (date(2026, 3, 4), Decimal(6)),
        (date(2026, 3, 5), Decimal(3)),
    ]

    state = await _ledger_state(session, connection)
    # Normalizing the last sync again, and replaying every record, write nothing.
    for again in (ledger.again, ledger.replay):
        report = await again()
        assert (report.created, report.updated) == (0, 0)
        assert (ledger.events(), ledger.changes()) == (0, 1 if again == ledger.replay else 0)
        assert await _ledger_state(session, connection) == state


GBX_TICKER = "ZZQAl_EQ"


def _gbx(**overrides: Any) -> dict[str, Any]:
    return instrument(GBX_TICKER, GB_ISIN, "GBX") | overrides


async def test_a_gbx_price_is_recorded_in_pounds(session, run) -> None:
    connection = await _connection(run)
    position = position_json(GBX_TICKER, currentPrice=Decimal("2350"))
    position["instrument"] = _gbx()
    meta = instrument_json(GBX_TICKER, isin=GB_ISIN, currencyCode="GBX", type="ETF")
    await Ledger(session, connection).normalize(
        SUMMARY, ("instruments", meta), ("positions", position)
    )
    price = (
        await session.execute(
            text(
                "select p.price, q.symbol from finance.prices p"
                " join finance.instruments q on q.id = p.quote_instrument_id"
                " where p.source = 'trading212'"
            )
        )
    ).one()
    assert (price.price, price.symbol) == (Decimal("23.50"), "GBP")
    quote = await session.scalar(
        text(
            "select q.symbol from finance.instruments i"
            " join finance.instruments q on q.id = i.quote_instrument_id where i.symbol = 'ZZQA'"
        )
    )
    assert quote == "GBP"
    # The typed row keeps what Trading 212 sent.
    row = await _row(session, "trading212_positions", "instrument_ticker = :t", t=GBX_TICKER)
    assert (row.instrument_currency, row.current_price) == ("GBX", Decimal("2350"))


async def test_a_gbx_listing_merges_with_a_gbp_listing(session, run, gbp) -> None:
    ibkr = await run(
        lambda ctx: create_instrument(
            ctx,
            kind="etf",
            symbol="ZZQA",
            name="Zzqa Example ETF",
            decimals=4,
            quote_instrument_id=gbp.id,
            identifiers=[InstrumentIdentifier("isin", GB_ISIN)],
        )
    )
    connection = await _connection(run)
    buy = order_item(price="8000")
    buy["order"] |= {"ticker": GBX_TICKER, "instrument": _gbx()}
    meta = instrument_json(GBX_TICKER, isin=GB_ISIN, currencyCode="GBX", type="ETF")
    await Ledger(session, connection).normalize(SUMMARY, ("instruments", meta), ("orders", buy))
    found = await session.scalar(
        text(
            "select instrument_id from finance.instrument_identifiers"
            " where scheme = 'trading212_ticker' and value = :t"
        ),
        {"t": GBX_TICKER},
    )
    assert found == ibkr.id
    # The fill's price, 8000 pence, is 80.00 pounds.
    price = await session.scalar(
        text("select price from finance.prices where source = 'trading212'")
    )
    assert price == Decimal("80.00")


async def test_an_isin_in_another_currency_is_not_merged(session, run, usd) -> None:
    await run(
        lambda ctx: create_instrument(
            ctx,
            kind="etf",
            symbol="ZZQU",
            name="Zzqa Example ETF (USD)",
            decimals=4,
            quote_instrument_id=usd.id,
            identifiers=[InstrumentIdentifier("isin", GB_ISIN)],
        )
    )
    connection = await _connection(run)
    buy = order_item()
    buy["order"] |= {"ticker": "ZZQAl_EQ", "instrument": instrument("ZZQAl_EQ", GB_ISIN, "GBP")}
    meta = instrument_json("ZZQAl_EQ", isin=GB_ISIN, currencyCode="GBP", type="ETF")
    await Ledger(session, connection).normalize(SUMMARY, ("instruments", meta), ("orders", buy))
    # A listing of its own, which goes without the ISIN another listing holds.
    assert await _identifiers(session, "ZZQA") == {("trading212_ticker", "ZZQAl_EQ")}
    assert await _identifiers(session, "ZZQU") == {("isin", GB_ISIN)}


async def test_a_fill_with_taxes_in_two_currencies(session, run) -> None:
    connection = await _connection(run)
    taxes = [
        {"name": "CURRENCY_CONVERSION_FEE", "quantity": Decimal("1.20"), "currency": "GBP"},
        {"name": "FINRA_FEE", "quantity": Decimal("-0.02"), "currency": "USD"},
    ]
    await Ledger(session, connection).normalize(SUMMARY, META, ("orders", order_item(taxes=taxes)))
    assert await _postings(session, "order:700001") == [
        (ACCOUNT, "ZZQA", Decimal(3), Decimal("80.00"), "GBP", None, None, False, ""),
        (ACCOUNT, "GBP", Decimal("-240.00"), None, None, None, None, False, ""),
        (ACCOUNT, "GBP", Decimal("-1.20"), None, None, None, None, True, "CURRENCY_CONVERSION_FEE"),
        (ACCOUNT, "USD", Decimal("-0.02"), None, None, None, None, True, "FINRA_FEE"),
        ("Unassigned", "GBP", Decimal("1.20"), None, None, None, None, False, ""),
        ("Unassigned", "USD", Decimal("0.02"), None, None, None, None, False, ""),
    ]
    assert await find_unbalanced_transactions(session) == []
    taxes_typed = await session.scalar(
        text(
            "select count(*) from finance.trading212_order_taxes where external_id = 'order:700001'"
        )
    )
    assert taxes_typed == 2


async def test_an_instrument_found_unsupported_is_voided_and_flagged(session, run) -> None:
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    await ledger.normalize(SUMMARY, ("orders", order_item()), ("dividends", dividend_item()))
    assert (await _transaction(session, "order:700001")).status != "void"

    # The metadata says it is a warrant: what it posted goes, and the type is flagged.
    report = await ledger.normalize(
        SUMMARY, ("instruments", instrument_json(type="WARRANT")), now=_days(1)
    )
    for key in ("order:700001", "dividend:div-0001"):
        assert (await _transaction(session, key)).status == "void"
    assert report.instrument_flags == {f"instrument type unsupported {TICKER}"}
    state = await _ledger_state(session, connection)

    # A replay decides the same.
    await ledger.replay()
    assert await _ledger_state(session, connection) == state


async def _cash_isa(run) -> uuid.UUID:
    """A manual account of the family's third type: no connection, so no Trading 212 id."""
    account = await run(
        lambda ctx: create_account(
            ctx,
            name="Cash ISA",
            ledger_type="asset",
            subtype="savings_pot",
            balance_mode="transactions",
            tax_wrapper="isa",
            type_key="trading212.cash_isa",
        )
    )
    return account.id


async def _named(session) -> dict[str, str | None]:
    """Each TRANSFER's external id → the value of the registry row it names, if any."""
    rows = await session.execute(
        text(
            "select t.external_id, i.value from finance.transactions t"
            " left join finance.account_identifiers i on i.id = t.counterparty_identifier_id"
            " where t.external_id like 'transaction:t-%' and t.status <> 'void'"
        )
    )
    return {r.external_id: r.value for r in rows}


@pytest.mark.parametrize(
    ("stocks_isa", "cash_isa", "named"),
    [
        (True, None, {"transaction:t-out": "20000002", "transaction:t-in": "10000001"}),
        (False, None, {"transaction:t-out": None}),
        (True, "open", {"transaction:t-out": None, "transaction:t-in": None}),
        (True, "closed", {"transaction:t-out": None, "transaction:t-in": None}),
        (False, "open", {"transaction:t-out": None}),
    ],
    ids=["invest-and-stocks-isa", "invest-alone", "three", "three-one-closed", "no-t212-id"],
)
async def test_a_sibling_only_with_exactly_two_family_accounts(
    session, run, stocks_isa, cash_isa, named
) -> None:
    invest = Ledger(session, await _connection(run, "invest"))
    isa = Ledger(session, await _connection(run, "isa")) if stocks_isa else None
    isa_summary = ("summary", summary_json(account_id=20000002, cash=NO_CASH))
    await invest.normalize(SUMMARY)
    if isa is not None:
        await isa.normalize(isa_summary)
    if cash_isa is not None:
        account_id = await _cash_isa(run)
        if cash_isa == "closed":
            await run(lambda ctx: close_account(ctx, account_id, date(2026, 3, 2)))

    moved = {"dateTime": "2026-03-04T09:15:00Z"}
    out = transaction_item(reference="t-out", kind="TRANSFER", amount="-100.00", **moved)
    into = transaction_item(reference="t-in", kind="TRANSFER", amount="100.00", **moved)
    report = await invest.normalize(SUMMARY, ("transactions", out), now=_days(1))
    assert "TRANSFER" in report.flagged
    if isa is not None:
        await isa.normalize(isa_summary, ("transactions", into), now=_days(1))
    assert await _named(session) == named


async def _contra(session, external_id: str) -> str:
    return await session.scalar(
        text(
            "select a.name from finance.postings p"
            " join finance.transactions t on t.id = p.transaction_id"
            " join finance.accounts a on a.id = p.account_id"
            " where t.external_id = :e and t.status <> 'void' and p.position = 1"
        ),
        {"e": external_id},
    )


async def test_a_new_family_account_strips_the_counterpart_at_the_next_sync(session, run) -> None:
    invest = Ledger(session, await _connection(run, "invest"))
    isa = Ledger(session, await _connection(run, "isa"))
    await invest.normalize(SUMMARY)
    await isa.normalize(("summary", summary_json(account_id=20000002, cash=NO_CASH)))
    out = transaction_item(
        reference="t-out", kind="TRANSFER", amount="-100.00", dateTime="2026-03-04T09:15:00Z"
    )
    await invest.normalize(SUMMARY, ("transactions", out), now=_days(1))
    assert await _named(session) == {"transaction:t-out": "20000002"}
    assert await _contra(session, "transaction:t-out") == "In transit"
    digest = (await get_connection(session, invest.connection.id)).config["family_accounts"]

    # A third account of the family: the sibling is no longer the only one it can be with.
    account_id = await _cash_isa(run)
    report = await invest.normalize(now=_days(2))
    assert report.replayed
    assert (await get_connection(session, invest.connection.id)).config["family_accounts"] != (
        digest
    )
    assert await _named(session) == {"transaction:t-out": None}
    assert await _contra(session, "transaction:t-out") == "Unassigned"

    assert not (await invest.normalize(now=_days(3))).replayed
    # Its status is not the family's: closing it changes nothing.
    await run(lambda ctx: close_account(ctx, account_id, date(2026, 3, 6)))
    assert not (await invest.normalize(now=_days(4))).replayed
    assert await _named(session) == {"transaction:t-out": None}
    assert await _contra(session, "transaction:t-out") == "Unassigned"
