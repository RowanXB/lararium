"""Reconciling the ledger against IBKR's reported positions and cash (design 5.11), the
warnings and notifications that follow (Decision 13), and NAV against valuation."""

import uuid
from datetime import date
from decimal import Decimal

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from lararium.core.commands import CommandContext
from lararium.core.connections import get_connection
from lararium.core.connectors import SyncContext
from lararium.core.documents import LocalDocumentStore
from lararium.core.json_types import JSONValue
from lararium.modules.finance.ibkr.normalize import normalize
from lararium.modules.finance.ibkr.reconcile import reconcile
from lararium.modules.finance.ibkr.sync import ingest_statement
from lararium.modules.finance.instruments import get_currency
from lararium.modules.finance.valuation import holdings_asof

from .fixtures import RowSpec, build_statement
from .test_normalize_records import (
    CONNECTOR,
    DAY,
    ETFA,
    GIA,
    _connection,
    _refuse,
)

ETFB = {
    "assetCategory": "STK",
    "subCategory": "ETF",
    "symbol": "ETFB",
    "description": "Example ETF B",
    "conid": "1002",
    "isin": "GB0000000017",
    "currency": "GBP",
}
WARNING_A = "IBKR ···0001 ETFA differs from IBKR's report on 2026-11-10 by -10"


class Notifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


async def sync(
    session, connection, tmp_path, rows, notifier, *, accounts=(GIA,), period=(DAY, DAY), empty=()
):
    """Ingest a synthetic statement and normalize it, as a sync does."""
    body = build_statement(list(accounts), period=period, rows=rows, empty_sections=empty)
    async with httpx.AsyncClient(transport=httpx.MockTransport(_refuse)) as http:

        async def context() -> SyncContext:
            return SyncContext(
                command=CommandContext(session, CONNECTOR, uuid.uuid4()),
                connection=await get_connection(session, connection.id),
                sync_id=uuid.uuid4(),
                master_key=bytes(32),
                http=http,
                notifier=notifier,
                session_factory=async_sessionmaker(bind=session.bind),
                documents=LocalDocumentStore(tmp_path),
            )

        request: dict[str, JSONValue] = {"upload": "x.xml"}
        ingested = await ingest_statement(await context(), body, request)
        return await normalize(
            await context(), ingested.raw_record_ids, ingested.flagged, ingested.sections
        )


async def warnings_of(session, connection) -> list[str]:
    return (await get_connection(session, connection.id)).warnings


def buy(transaction_id: str, instrument: dict[str, str], quantity: str, price: str, proceeds: str):
    return RowSpec(
        "Trade",
        "U0000001",
        **instrument,
        transactionID=transaction_id,
        quantity=quantity,
        tradePrice=price,
        proceeds=proceeds,
        ibCommission="-1.00",
        ibCommissionCurrency=instrument["currency"],
        buySell="BUY",
    )


def position(instrument: dict[str, str], quantity: str, mark: str = "100") -> RowSpec:
    return RowSpec("OpenPosition", "U0000001", **instrument, position=quantity, markPrice=mark)


def cash(currency: str, ending: str) -> RowSpec:
    return RowSpec("CashReportCurrency", "U0000001", currency=currency, endingCash=ending)


async def test_consistent_statement_no_warning(session, run, tmp_path) -> None:
    connection = await _connection(run)
    notifier = Notifier()
    rows = [
        buy("9000001", ETFA, "10", "100.00", "-1000.00"),
        position(ETFA, "10"),
        cash("USD", "-1001.00"),
    ]
    report = await sync(session, connection, tmp_path, rows, notifier)
    assert report.mismatches == []
    assert await warnings_of(session, connection) == []
    assert notifier.sent == []


async def test_missing_trade_warns_once(session, run, tmp_path) -> None:
    connection = await _connection(run)
    notifier = Notifier()
    rows = [position(ETFA, "10")]
    report = await sync(session, connection, tmp_path, rows, notifier)
    assert report.mismatches == [WARNING_A]
    assert await warnings_of(session, connection) == [WARNING_A]
    assert len(notifier.sent) == 1
    assert WARNING_A in notifier.sent[0]
    # The same state again: the warning stays, nobody is told twice.
    for _ in range(2):
        await sync(session, connection, tmp_path, rows, notifier)
    assert await warnings_of(session, connection) == [WARNING_A]
    assert len(notifier.sent) == 1
    # The missing trade arrives: the warning goes.
    fixed = [buy("9000001", ETFA, "10", "100.00", "-1000.00"), *rows]
    await sync(session, connection, tmp_path, fixed, notifier)
    assert await warnings_of(session, connection) == []
    assert len(notifier.sent) == 1


async def test_new_difference_is_told_again(session, run, tmp_path) -> None:
    connection = await _connection(run)
    notifier = Notifier()
    await sync(session, connection, tmp_path, [position(ETFA, "10")], notifier)
    await sync(session, connection, tmp_path, [position(ETFA, "12")], notifier)
    assert len(notifier.sent) == 2
    assert (await warnings_of(session, connection))[0].endswith("by -12")


async def test_omitted_instrument_counts_as_zero_only_when_the_section_is_there(
    session, run, tmp_path
) -> None:
    connection = await _connection(run)
    notifier = Notifier()
    trade = buy("9000001", ETFA, "10", "100.00", "-1000.00")
    # No Open Positions section: securities are not checked; cash is, and agrees.
    await sync(session, connection, tmp_path, [trade, cash("USD", "-1001.00")], notifier)
    assert await warnings_of(session, connection) == []
    # An empty Open Positions section says IBKR holds nothing: the ledger's 10 is a difference.
    await sync(
        session,
        connection,
        tmp_path,
        [trade, cash("USD", "-1001.00")],
        notifier,
        empty=("OpenPositions",),
    )
    assert await warnings_of(session, connection) == [
        "IBKR ···0001 ETFA differs from IBKR's report on 2026-11-10 by 10"
    ]


async def test_cash_difference_names_the_currency(session, run, tmp_path) -> None:
    connection = await _connection(run)
    notifier = Notifier()
    rows = [cash("GBP", "250.00")]
    await sync(session, connection, tmp_path, rows, notifier)
    assert await warnings_of(session, connection) == [
        "IBKR ···0001 GBP differs from IBKR's report on 2026-11-10 by -250.00"
    ]


async def test_flagged_items_warn_and_notify_once_per_new_set(session, run, tmp_path) -> None:
    connection = await _connection(run)
    notifier = Notifier()
    rows = [RowSpec("OpenPosition", "U0000001", **(ETFA | {"assetCategory": "OPT"}), position="1")]
    await sync(session, connection, tmp_path, rows, notifier)
    flagged = [w for w in await warnings_of(session, connection) if w.startswith("IBKR flagged: ")]
    assert flagged == ["IBKR flagged: OpenPosition.assetCategory=OPT"]
    assert len(notifier.sent) == 1
    assert "IBKR flagged: OpenPosition.assetCategory=OPT" in notifier.sent[0]
    await sync(session, connection, tmp_path, rows, notifier)
    assert len(notifier.sent) == 1
    assert [w for w in await warnings_of(session, connection) if "flagged" in w] == flagged


async def test_reconcile_function(session, run, tmp_path) -> None:
    connection = await _connection(run)
    await sync(
        session, connection, tmp_path, [buy("9000001", ETFA, "10", "100", "-1000")], Notifier()
    )
    account = await session.scalar(
        text("select id from finance.accounts where name = 'IBKR ···0001'")
    )
    etfa = await session.scalar(text("select id from finance.instruments where symbol = 'ETFA'"))
    usd = (await get_currency(session, "USD")).id
    # The ledger holds 10 ETFA and -1001 USD; a report that omits both counts as zero.
    assert dict(await reconcile(session, account, DAY, {})) == {
        etfa: Decimal(10),
        usd: Decimal(-1001),
    }
    assert (
        await reconcile(session, account, DAY, {etfa: Decimal(10), usd: Decimal("-1001.004")}) == []
    )
    # Before the trade's booking date the ledger holds nothing.
    assert await reconcile(session, account, date(2026, 11, 9), {}) == []


async def test_nav_equals_valuation(session, run, tmp_path) -> None:
    connection = await _connection(run)
    rows = [
        RowSpec("CashTransaction", "U0000001", transactionID="8000001", amount="5000"),
        RowSpec(
            "CashTransaction", "U0000001", transactionID="8000002", amount="2000", currency="USD"
        ),
        buy("9000001", ETFA, "10", "100.00", "-1000.00"),
        buy("9000002", ETFB, "5", "50.00", "-250.00"),
        position(ETFA, "10", "100"),
        position(ETFB, "5", "50"),
        RowSpec("ConversionRate", "U0000001", fromCurrency="USD", toCurrency="GBP", rate="0.8"),
        cash("GBP", "4749.00"),
        cash("USD", "999.00"),
        RowSpec(
            "EquitySummaryByReportDateInBase",
            "U0000001",
            total="6602.20",
            interestAccruals="1.50",
            dividendAccruals="2.50",
        ),
    ]
    notifier = Notifier()
    await sync(session, connection, tmp_path, rows, notifier)
    assert await warnings_of(session, connection) == []
    held = await holdings_asof(session, DAY)
    account = await session.scalar(
        text("select id from finance.accounts where name = 'IBKR ···0001'")
    )
    mine = [h for h in held if h.account_id == account]
    assert len(mine) == 4
    assert {h.status for h in mine} == {"ok"}
    values = sum((h.value for h in mine if h.value is not None), Decimal(0))
    nav = await session.scalar(
        text("select total - interest_accruals - dividend_accruals from finance.ibkr_nav")
    )
    assert abs(values - nav) <= Decimal("0.01")
