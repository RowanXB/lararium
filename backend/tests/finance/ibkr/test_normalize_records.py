"""IBKR records, prices, FX rates and reported figures into the ledger (design 5.7-5.10)."""

import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from lararium.core.actors import Actor
from lararium.core.commands import CommandContext
from lararium.core.connections import ConnectionRecord, create_connection, get_connection
from lararium.core.connectors import SyncContext
from lararium.core.documents import LocalDocumentStore
from lararium.core.json_types import JSONValue
from lararium.modules.finance.ibkr.normalize import NORMALIZER_VERSION, NormalizeReport, normalize
from lararium.modules.finance.ibkr.parser import FlexDataError
from lararium.modules.finance.ibkr.sync import ingest_statement
from lararium.modules.finance.instruments import InstrumentIdentifier, create_instrument
from lararium.modules.finance.ledger import find_unbalanced_transactions
from lararium.modules.finance.valuation import holdings_asof

from .fixtures import AccountSpec, RowSpec, build_statement

CONNECTOR = Actor("connector", "ibkr")
NEW_YORK = ZoneInfo("America/New_York")
DAY = date(2026, 11, 10)
# Opened on the first day of the windows below, so it needs no opening balance adjustment.
GIA = AccountSpec("U0000001", date_opened=DAY)
OTHER = AccountSpec("U0000002")
# A USD listing of an invented ETF; the ISIN's check digit is valid.
ETFA = {
    "assetCategory": "STK",
    "subCategory": "ETF",
    "symbol": "ETFA",
    "description": "Example ETF A",
    "conid": "1001",
    "isin": "IE00B3RBWM25",
    "currency": "USD",
}


def _ymd(day: date) -> str:
    return day.strftime("%Y%m%d")


async def _connection(run) -> ConnectionRecord:
    return await run(
        lambda ctx: create_connection(ctx, connector="ibkr", display_name="IBKR", config={})
    )


class _Notifier:
    async def send(self, text: str) -> None:
        return None


def _refuse(request: httpx.Request) -> httpx.Response:
    raise AssertionError("no HTTP in this test")


async def _ctx(session, connection, tmp_path, http, actor=CONNECTOR) -> SyncContext:
    return SyncContext(
        command=CommandContext(session, actor, uuid.uuid4()),
        connection=await get_connection(session, connection.id),
        sync_id=uuid.uuid4(),
        master_key=bytes(32),
        http=http,
        notifier=_Notifier(),
        session_factory=async_sessionmaker(bind=session.bind),
        documents=LocalDocumentStore(tmp_path),
    )


async def _sync(
    session,
    connection,
    tmp_path,
    rows,
    *,
    accounts=(GIA,),
    period=(DAY, DAY),
    by_day=False,
    empty_sections=(),
    request: dict[str, JSONValue] | None = None,
    actor=CONNECTOR,
) -> tuple[NormalizeReport, SyncContext]:
    """Ingest a synthetic statement and normalize it, as a sync does."""
    body = build_statement(
        list(accounts), period=period, rows=rows, by_day=by_day, empty_sections=empty_sections
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(_refuse)) as http:
        ctx = await _ctx(session, connection, tmp_path, http, actor)
        ingested = await ingest_statement(ctx, body, request or {"upload": "x.xml"})
        ctx = await _ctx(session, connection, tmp_path, http, actor)
        report = await normalize(ctx, ingested.raw_record_ids, ingested.flagged, ingested.sections)
        return report, ctx


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
            " where t.external_id = :e order by p.position"
        ),
        {"e": external_id},
    )
    return [tuple(r) for r in rows]


async def _transaction(session, external_id: str):
    return (
        await session.execute(
            text(
                "select id, occurred_at, occurred_tz, booking_date, value_date, description,"
                " bank_transfer, counterparty_identifier_id, status"
                " from finance.transactions where external_id = :e"
            ),
            {"e": external_id},
        )
    ).one()


async def _external_ids(session) -> list[str]:
    rows = await session.scalars(
        text("select external_id from finance.transactions order by external_id")
    )
    return list(rows)


async def _cash(session, symbol: str) -> Decimal:
    return await session.scalar(
        text(
            "select coalesce(sum(p.quantity), 0) from finance.postings p"
            " join finance.accounts a on a.id = p.account_id"
            " join finance.instruments i on i.id = p.instrument_id"
            " where a.name = 'IBKR ···0001' and i.symbol = :s"
        ),
        {"s": symbol},
    )


ACCOUNT = "IBKR ···0001"


async def test_buy_with_commission(session, run, tmp_path) -> None:
    connection = await _connection(run)
    rows = [
        RowSpec(
            "Trade",
            "U0000001",
            **ETFA,
            transactionID="9000001",
            quantity="10",
            tradePrice="100.00",
            proceeds="-1000.00",
            ibCommission="-1.25",
            ibCommissionCurrency="USD",
            tradeDate="20261110",
            settleDateTarget="20261112",
            tradeID="5000001",
            ibOrderID="4000001",
            ibExecID="0001f4e8.0001.01",
            buySell="BUY",
        )
    ]
    report, _ = await _sync(session, connection, tmp_path, rows)
    assert (report.created, report.updated, report.unchanged) == (1, 0, 0)
    assert await _postings(session, "trade:U0000001:9000001") == [
        (ACCOUNT, "ETFA", Decimal("10"), Decimal("100.00"), "USD", None, None, False, ""),
        (ACCOUNT, "USD", Decimal("-1000.00"), None, None, None, None, False, ""),
        (ACCOUNT, "USD", Decimal("-1.25"), None, None, None, None, True, "commission"),
        ("Unassigned", "USD", Decimal("1.25"), None, None, None, None, False, ""),
    ]
    assert await _cash(session, "USD") == Decimal("-1001.25")
    tx = await _transaction(session, "trade:U0000001:9000001")
    assert tx.occurred_at == datetime(2026, 11, 10, 9, 30, tzinfo=NEW_YORK)
    assert tx.occurred_tz == "America/New_York"
    assert (tx.booking_date, tx.value_date) == (DAY, date(2026, 11, 12))
    assert tx.bank_transfer is False
    typed = (
        await session.execute(
            text(
                "select t.ibkr_transaction_id, t.trade_id, t.ib_order_id, t.ib_exec_id,"
                " t.trade_date, t.buy_sell, t.fifo_pnl_realized, t.account_id = a.id as own"
                " from finance.ibkr_trades t join finance.accounts a on a.name = :n"
            ),
            {"n": ACCOUNT},
        )
    ).one()
    assert tuple(typed) == (
        "9000001",
        "5000001",
        "4000001",
        "0001f4e8.0001.01",
        DAY,
        "BUY",
        None,
        True,
    )


async def test_sell_weighs_by_price(session, run, tmp_path) -> None:
    connection = await _connection(run)
    rows = [
        RowSpec(
            "Trade",
            "U0000001",
            **ETFA,
            transactionID="9000002",
            quantity="-4",
            tradePrice="110.00",
            proceeds="440.00",
            ibCommission="-1.00",
            ibCommissionCurrency="USD",
            fifoPnlRealized="39.00",
            buySell="SELL",
        )
    ]
    await _sync(session, connection, tmp_path, rows)
    assert await _postings(session, "trade:U0000001:9000002") == [
        (ACCOUNT, "ETFA", Decimal("-4"), None, None, Decimal("110.00"), "USD", False, ""),
        (ACCOUNT, "USD", Decimal("440.00"), None, None, None, None, False, ""),
        (ACCOUNT, "USD", Decimal("-1.00"), None, None, None, None, True, "commission"),
        ("Unassigned", "USD", Decimal("1.00"), None, None, None, None, False, ""),
    ]
    assert await session.scalar(text("select fifo_pnl_realized from finance.ibkr_trades")) == (
        Decimal("39.00")
    )


def _conversion(**attributes: str) -> RowSpec:
    base = {
        "assetCategory": "CASH",
        "symbol": "GBP.USD",
        "description": "GBP.USD",
        "conid": "12087797",
        "currency": "USD",
        "transactionID": "9000003",
        "ibCommissionCurrency": "GBP",
        "ibCommission": "-1.70",
    }
    return RowSpec("Trade", "U0000001", **{**base, **attributes})


async def test_conversion_gbp_to_usd(session, run, tmp_path) -> None:
    connection = await _connection(run)
    rows = [_conversion(quantity="1000", tradePrice="1.27", proceeds="-1270.00")]
    await _sync(session, connection, tmp_path, rows)
    assert await _postings(session, "trade:U0000001:9000003") == [
        (ACCOUNT, "GBP", Decimal("1000"), None, None, Decimal("1.27"), "USD", False, ""),
        (ACCOUNT, "USD", Decimal("-1270.00"), None, None, None, None, False, ""),
        (ACCOUNT, "GBP", Decimal("-1.70"), None, None, None, None, True, "commission"),
        ("Unassigned", "GBP", Decimal("1.70"), None, None, None, None, False, ""),
    ]
    # A conversion makes no instrument of its own.
    assert await session.scalar(text("select count(*) from finance.ibkr_instruments")) == 0


async def test_rounding_residual_posted(session, run, tmp_path) -> None:
    # A rounded tradePrice leaves more than half a cent: 3333 x 1.2735 = 4244.5755.
    connection = await _connection(run)
    rows = [_conversion(quantity="3333", tradePrice="1.2735", proceeds="-4244.43")]
    await _sync(session, connection, tmp_path, rows)
    postings = await _postings(session, "trade:U0000001:9000003")
    rounding = [p for p in postings if p[8] == "rounding"]
    assert rounding == [
        ("Unassigned", "USD", Decimal("-0.1455"), None, None, None, None, False, "rounding")
    ]
    # The cash posting is IBKR's proceeds, and the price stays the trade's.
    assert postings[1][2] == Decimal("-4244.43")
    assert postings[0][5] == Decimal("1.2735")
    assert await find_unbalanced_transactions(session) == []


async def test_rounding_within_tolerance_not_posted(session, run, tmp_path) -> None:
    # 3333 x 1.2734567 = 4244.4311811: the residual, 0.0011811 USD, is within half a cent,
    # which the ledger's tolerance absorbs (design 5.7), so no rounding posting is added.
    connection = await _connection(run)
    rows = [_conversion(quantity="3333", tradePrice="1.2734567", proceeds="-4244.43")]
    await _sync(session, connection, tmp_path, rows)
    postings = await _postings(session, "trade:U0000001:9000003")
    assert [p for p in postings if p[8] == "rounding"] == []
    assert await find_unbalanced_transactions(session) == []


async def test_rounding_beyond_what_rounding_explains_fails(session, run, tmp_path) -> None:
    # Proceeds in pounds against a price in pence: 100 x 512.5 is not -512.50 by any rounding.
    connection = await _connection(run)
    rows = [
        RowSpec(
            "Trade",
            "U0000001",
            **(ETFA | {"currency": "GBP"}),
            quantity="100",
            tradePrice="512.5",
            proceeds="-512.50",
        )
    ]
    with pytest.raises(FlexDataError, match=r"Trade 9000001 of ···0001: ") as raised:
        await _sync(session, connection, tmp_path, rows)
    assert "512" not in str(raised.value)
    assert "U0000001" not in str(raised.value)


async def test_trade_taxes_are_a_fee_in_the_trade_currency(session, run, tmp_path) -> None:
    connection = await _connection(run)
    rows = [
        RowSpec(
            "Trade",
            "U0000001",
            **ETFA,
            quantity="10",
            tradePrice="100",
            proceeds="-1000",
            ibCommission="-1.00",
            ibCommissionCurrency="USD",
            taxes="-0.50",
        )
    ]
    await _sync(session, connection, tmp_path, rows)
    assert (await _postings(session, "trade:U0000001:9000001"))[2:] == [
        (ACCOUNT, "USD", Decimal("-1.00"), None, None, None, None, True, "commission"),
        (ACCOUNT, "USD", Decimal("-0.50"), None, None, None, None, True, "taxes"),
        ("Unassigned", "USD", Decimal("1.50"), None, None, None, None, False, ""),
    ]


async def test_conversion_selling_the_base_currency(session, run, tmp_path) -> None:
    connection = await _connection(run)
    rows = [_conversion(quantity="-1000", tradePrice="1.27", proceeds="1270.00")]
    await _sync(session, connection, tmp_path, rows)
    assert (await _postings(session, "trade:U0000001:9000003"))[:2] == [
        (ACCOUNT, "GBP", Decimal("-1000"), None, None, Decimal("1.27"), "USD", False, ""),
        (ACCOUNT, "USD", Decimal("1270.00"), None, None, None, None, False, ""),
    ]
    tx = await _transaction(session, "trade:U0000001:9000003")
    assert tx.description == "Sell 1000 GBP.USD"


async def test_deposit_is_bank_transfer(session, run, tmp_path) -> None:
    connection = await _connection(run)
    rows = [
        RowSpec(
            "CashTransaction",
            "U0000001",
            transactionID="8000001",
            amount="500",
            settleDate="20261111",
            description="CASH RECEIPTS / ELECTRONIC FUND TRANSFERS",
        )
    ]
    report, _ = await _sync(session, connection, tmp_path, rows)
    assert report.created == 1
    assert await _postings(session, "cash:U0000001:8000001") == [
        (ACCOUNT, "GBP", Decimal("500"), None, None, None, None, False, ""),
        ("Unassigned", "GBP", Decimal("-500"), None, None, None, None, False, ""),
    ]
    tx = await _transaction(session, "cash:U0000001:8000001")
    assert tx.bank_transfer is True
    assert tx.counterparty_identifier_id is None
    assert tx.description == "CASH RECEIPTS / ELECTRONIC FUND TRANSFERS"
    assert (tx.booking_date, tx.value_date) == (DAY, date(2026, 11, 11))
    typed = (
        await session.execute(
            text(
                "select ibkr_transaction_id, type, instrument_id"
                " from finance.ibkr_cash_transactions"
            )
        )
    ).one()
    assert tuple(typed) == ("8000001", "Deposits/Withdrawals", None)


async def test_dividend_and_tax_against_unassigned(session, run, tmp_path) -> None:
    connection = await _connection(run)
    rows = [
        RowSpec(
            "CashTransaction",
            "U0000001",
            **(ETFA | {"description": "ETFA CASH DIVIDEND USD 1.234 PER SHARE"}),
            transactionID="8000002",
            type="Dividends",
            amount="12.34",
        ),
        RowSpec(
            "CashTransaction",
            "U0000001",
            **(ETFA | {"description": "ETFA CASH DIVIDEND - US TAX"}),
            transactionID="8000003",
            type="Withholding Tax",
            amount="-1.85",
        ),
    ]
    await _sync(session, connection, tmp_path, rows)
    assert await _postings(session, "cash:U0000001:8000002") == [
        (ACCOUNT, "USD", Decimal("12.34"), None, None, None, None, False, ""),
        ("Unassigned", "USD", Decimal("-12.34"), None, None, None, None, False, ""),
    ]
    assert await _postings(session, "cash:U0000001:8000003") == [
        (ACCOUNT, "USD", Decimal("-1.85"), None, None, None, None, False, ""),
        ("Unassigned", "USD", Decimal("1.85"), None, None, None, None, False, ""),
    ]
    assert (await _transaction(session, "cash:U0000001:8000002")).bank_transfer is False
    # The related instrument is a typed field.
    related = await session.scalars(
        text(
            "select i.symbol from finance.ibkr_cash_transactions c"
            " join finance.instruments i on i.id = c.instrument_id order by c.ibkr_transaction_id"
        )
    )
    assert list(related) == ["ETFA", "ETFA"]


async def test_split_quantity_from_unassigned(session, run, tmp_path) -> None:
    connection = await _connection(run)
    rows = [
        RowSpec(
            "CorporateAction",
            "U0000001",
            **(ETFA | {"description": "ETFA SPLIT 4 FOR 1"}),
            transactionID="7000001",
            type="FS",
            quantity="30",
            proceeds="0",
            actionID="3000001",
        )
    ]
    await _sync(session, connection, tmp_path, rows)
    assert await _postings(session, "ca:U0000001:7000001") == [
        (ACCOUNT, "ETFA", Decimal("30"), None, None, None, None, False, ""),
        ("Unassigned", "ETFA", Decimal("-30"), None, None, None, None, False, ""),
    ]
    assert (await _transaction(session, "ca:U0000001:7000001")).description == "ETFA SPLIT 4 FOR 1"
    typed = (
        await session.execute(
            text("select ibkr_transaction_id, type, action_id from finance.ibkr_corporate_actions")
        )
    ).one()
    assert tuple(typed) == ("7000001", "FS", "3000001")


async def _identifier(session, value: str):
    return await session.scalar(
        text("select id from finance.account_identifiers where value = :v"), {"v": value}
    )


async def test_position_transfer_names_other_account(session, run, tmp_path) -> None:
    connection = await _connection(run)
    rows = [
        RowSpec(
            "Transfer",
            "U0000001",
            **(ETFA | {"description": "ETFA"}),
            transactionID="6000001",
            quantity="5",
            account="U0000002",
            direction="IN",
            transferPrice="98.50",
            positionAmount="492.50",
        )
    ]
    await _sync(session, connection, tmp_path, rows, accounts=(GIA, OTHER))
    tx = await _transaction(session, "transfer:U0000001:6000001")
    assert tx.counterparty_identifier_id == await _identifier(session, "U0000002")
    assert tx.bank_transfer is False
    # The other account is the owner's, so the contra is In transit; no cost on the legs.
    assert await _postings(session, "transfer:U0000001:6000001") == [
        (ACCOUNT, "ETFA", Decimal("5"), None, None, None, None, False, ""),
        ("In transit", "ETFA", Decimal("-5"), None, None, None, None, False, ""),
    ]
    typed = (
        await session.execute(
            text("select account, direction, transfer_price from finance.ibkr_transfers")
        )
    ).one()
    assert tuple(typed) == ("U0000002", "IN", Decimal("98.50"))


async def test_internal_cash_transfer_amount(session, run, tmp_path) -> None:
    connection = await _connection(run)
    rows = [
        RowSpec(
            "Transfer",
            "U0000001",
            assetCategory="CASH",
            symbol="--",
            transactionID="6000002",
            quantity="0",
            cashTransfer="-250.00",
            account="U0000002",
            direction="OUT",
        )
    ]
    await _sync(session, connection, tmp_path, rows, accounts=(GIA, OTHER))
    assert await _postings(session, "transfer:U0000001:6000002") == [
        (ACCOUNT, "GBP", Decimal("-250.00"), None, None, None, None, False, ""),
        ("In transit", "GBP", Decimal("250.00"), None, None, None, None, False, ""),
    ]
    assert await session.scalar(text("select cash_transfer from finance.ibkr_transfers")) == (
        Decimal("-250.00")
    )


async def test_internal_transfer_sides_share_an_id(session, run, tmp_path) -> None:
    # IBKR lists both sides of an internal movement under one transactionID, one row in each
    # account, and the sides may be reported on different days.
    connection = await _connection(run)
    side = {"assetCategory": "CASH", "transactionID": "6000004", "quantity": "0"}
    rows = [
        RowSpec("Transfer", "U0000001", **side, cashTransfer="-80", account="U0000002"),
        RowSpec(
            "Transfer",
            "U0000002",
            **side,
            cashTransfer="80",
            account="U0000001",
            reportDate=_ymd(DAY + timedelta(days=1)),
        ),
    ]
    await _sync(session, connection, tmp_path, rows, accounts=(GIA, OTHER))
    assert await _external_ids(session) == [
        "transfer:U0000001:6000004",
        "transfer:U0000002:6000004",
    ]
    assert [p[:3] for p in await _postings(session, "transfer:U0000002:6000004")] == [
        ("IBKR ···0002", "GBP", Decimal("80")),
        ("In transit", "GBP", Decimal("-80")),
    ]
    assert await session.scalar(text("select count(*) from finance.ibkr_transfers")) == 2


async def test_deposit_and_internal_transfer_of_one_amount_both_posted(
    session, run, tmp_path
) -> None:
    # Every Deposits/Withdrawals row is posted, whatever Transfers lists the same day.
    connection = await _connection(run)
    rows = [
        RowSpec(
            "Transfer",
            "U0000001",
            assetCategory="CASH",
            transactionID="6000003",
            quantity="0",
            cashTransfer="-250.00",
            account="U0000002",
        ),
        RowSpec("CashTransaction", "U0000001", transactionID="8000009", amount="-250.00"),
    ]
    report, _ = await _sync(session, connection, tmp_path, rows, accounts=(GIA, OTHER))
    assert await _external_ids(session) == [
        "cash:U0000001:8000009",
        "transfer:U0000001:6000003",
    ]
    assert (await _transaction(session, "cash:U0000001:8000009")).bank_transfer is True
    assert await session.scalar(text("select count(*) from finance.ibkr_cash_transactions")) == 1
    assert not {f for f in report.flagged if f.startswith("CashTransaction")}


async def test_security_transfer_out_is_negative(session, run, tmp_path) -> None:
    connection = await _connection(run)
    rows = [
        RowSpec(
            "Transfer",
            "U0000001",
            **ETFA,
            transactionID="6000005",
            quantity="5",
            account="U0000002",
            direction="OUT",
        )
    ]
    await _sync(session, connection, tmp_path, rows, accounts=(GIA, OTHER))
    assert [p[:3] for p in await _postings(session, "transfer:U0000001:6000005")] == [
        (ACCOUNT, "ETFA", Decimal("-5")),
        ("In transit", "ETFA", Decimal("5")),
    ]


async def test_prices_and_fx_recorded(session, run, tmp_path) -> None:
    days = [DAY + timedelta(days=n) for n in range(4)]
    connection = await _connection(run)
    rows = [
        RowSpec("Trade", "U0000001", day=days[0], **ETFA, proceeds="-1000"),
        *(
            RowSpec("PriorPeriodPosition", "U0000001", day=day, **ETFA, price=f"10{n + 1}")
            for n, day in enumerate(days[:3])
        ),
        RowSpec("OpenPosition", "U0000001", day=days[3], **ETFA, position="10", markPrice="104"),
        RowSpec("ConversionRate", "U0000001", day=days[0], rate="0.78"),
        RowSpec("ConversionRate", "U0000001", day=days[1], rate="0.79"),
        # Rates IBKR has none for, and currencies the ledger does not know, are left out.
        RowSpec("ConversionRate", "U0000001", day=days[1], fromCurrency="EUR", rate="-1"),
        RowSpec("ConversionRate", "U0000001", day=days[1], fromCurrency="XAU", rate="3000"),
    ]
    await _sync(session, connection, tmp_path, rows, period=(days[0], days[3]), by_day=True)
    prices = await session.execute(
        text(
            "select p.as_of_date, p.price, q.symbol from finance.prices p"
            " join finance.instruments q on q.id = p.quote_instrument_id"
            " where p.source = 'ibkr' order by p.as_of_date"
        )
    )
    assert [tuple(r) for r in prices] == [
        (days[0], Decimal("101"), "USD"),
        (days[1], Decimal("102"), "USD"),
        (days[2], Decimal("103"), "USD"),
        (days[3], Decimal("104"), "USD"),
    ]
    rates = await session.execute(
        text(
            "select r.as_of_date, b.symbol, q.symbol as quote, r.rate from finance.fx_rates r"
            " join finance.instruments b on b.id = r.base_instrument_id"
            " join finance.instruments q on q.id = r.quote_instrument_id"
            " where r.source = 'ibkr' order by r.as_of_date"
        )
    )
    assert [tuple(r) for r in rates] == [
        (days[0], "USD", "GBP", Decimal("0.78")),
        (days[1], "USD", "GBP", Decimal("0.79")),
    ]
    held = [h for h in await holdings_asof(session, days[1]) if h.symbol == "ETFA"]
    assert [(h.quantity, h.unit_price, h.fx_rate, h.value, h.status) for h in held] == [
        (Decimal("10"), Decimal("102"), Decimal("0.79"), Decimal("805.80"), "ok")
    ]


async def test_reported_snapshots_and_typed_reports(session, run, tmp_path) -> None:
    connection = await _connection(run)
    rows = [
        RowSpec(
            "OpenPosition",
            "U0000001",
            **ETFA,
            position="10",
            markPrice="104",
            costBasisPrice="100",
            costBasisMoney="1000",
            percentOfNAV="50",
            side="Long",
        ),
        RowSpec(
            "Lot",
            "U0000001",
            container="OpenPositions",
            **ETFA,
            reportDate=_ymd(DAY),
            levelOfDetail="LOT",
            position="6",
            costBasisPrice="99",
            originatingTransactionID="9000001",
        ),
        RowSpec(
            "Lot",
            "U0000001",
            container="OpenPositions",
            **ETFA,
            reportDate=_ymd(DAY),
            levelOfDetail="LOT",
            position="4",
            costBasisPrice="101.5",
        ),
        RowSpec("CashReportCurrency", "U0000001", startingCash="150", endingCash="200"),
        RowSpec(
            "CashReportCurrency", "U0000001", currency="USD", startingCash="0", endingCash="-5"
        ),
        RowSpec(
            "CashReportCurrency",
            "U0000001",
            currency="BASE_SUMMARY",
            levelOfDetail="BaseCurrency",
            endingCash="196",
        ),
        RowSpec(
            "EquitySummaryByReportDateInBase", "U0000001", total="1000", cash="200", stock="800"
        ),
    ]
    await _sync(session, connection, tmp_path, rows)
    as_of = datetime.combine(DAY, time(23, 59, 59, 999999), NEW_YORK)
    snapshots = await session.execute(
        text(
            "select i.symbol, s.quantity, s.as_of, s.as_of_date, s.kind"
            " from finance.balance_snapshots s join finance.instruments i on i.id = s.instrument_id"
            " order by i.symbol"
        )
    )
    assert [tuple(r) for r in snapshots] == [
        ("ETFA", Decimal("10"), as_of, DAY, "reported"),
        ("GBP", Decimal("200"), as_of, DAY, "reported"),
        ("USD", Decimal("-5"), as_of, DAY, "reported"),
    ]
    position = (
        await session.execute(
            text(
                "select p.mark_price, p.cost_basis_price, p.percent_of_nav, p.side"
                " from finance.ibkr_positions p"
                " join finance.balance_snapshots s on s.id = p.snapshot_id"
            )
        )
    ).one()
    assert tuple(position) == (Decimal("104"), Decimal("100"), Decimal("50"), "Long")
    lots = await session.execute(
        text(
            "select l.originating_transaction_id, l.position, l.cost_basis_price"
            " from finance.ibkr_position_lots l"
            " join finance.ibkr_positions p on p.snapshot_id = l.snapshot_id"
            " order by l.position"
        )
    )
    lots = [tuple(r) for r in lots]
    assert lots[1] == ("9000001", Decimal("6"), Decimal("99"))
    assert lots[0][0].startswith("sha256:") and lots[0][1:] == (Decimal("4"), Decimal("101.5"))
    reports = await session.execute(
        text(
            "select i.symbol, c.from_date, c.to_date, c.starting_cash, c.ending_cash"
            " from finance.ibkr_cash_reports c"
            " join finance.instruments i on i.id = c.currency_instrument_id order by i.symbol"
        )
    )
    assert [tuple(r) for r in reports] == [
        ("GBP", DAY, DAY, Decimal("150"), Decimal("200")),
        ("USD", DAY, DAY, Decimal("0"), Decimal("-5")),
    ]
    nav = (
        await session.execute(text("select report_date, total, cash, stock from finance.ibkr_nav"))
    ).one()
    assert tuple(nav) == (DAY, Decimal("1000"), Decimal("200"), Decimal("800"))
    # The mark price is the day's price as well.
    assert await session.scalar(
        text("select price from finance.prices where source = 'ibkr'")
    ) == Decimal("104")


async def test_trade_details_stored(session, run, tmp_path) -> None:
    connection = await _connection(run)
    rows = [
        RowSpec(
            "Trade",
            "U0000001",
            **ETFA,
            transactionID="9000010",
            levelOfDetail="CLOSED_LOT",
            quantity="-4",
            cost="-400",
        ),
        RowSpec(
            "Order",
            "U0000001",
            **ETFA,
            levelOfDetail="ORDER",
            ibOrderID="4000010",
            quantity="-4",
            tradePrice="110",
        ),
    ]
    report, _ = await _sync(session, connection, tmp_path, rows)
    assert report.created == 0  # only executions are ledger rows
    details = await session.execute(
        text(
            "select d.level_of_detail, d.row_key, d.quantity, i.symbol"
            " from finance.ibkr_trade_details d"
            " join finance.instruments i on i.id = d.instrument_id order by d.level_of_detail"
        )
    )
    details = [tuple(r) for r in details]
    assert details[0] == ("CLOSED_LOT", "9000010", Decimal("-4"), "ETFA")
    assert details[1][0] == "ORDER" and details[1][1].startswith("sha256:")


async def test_opening_only_for_older_account(session, run, tmp_path) -> None:
    days = [DAY + timedelta(days=n) for n in range(3)]
    young = AccountSpec("U0000002", date_opened=days[0])
    connection = await _connection(run)
    rows = [
        RowSpec(
            "CashReportCurrency",
            account,
            day=days[0],
            startingCash="200",
            endingCash="200",
        )
        for account in ("U0000001", "U0000002")
    ] + [
        RowSpec("CashTransaction", "U0000001", day=days[1], transactionID="8000001", amount="50"),
        # Held throughout, so every daily statement lists it.
        *(RowSpec("OpenPosition", "U0000001", day=day, **ETFA, position="10") for day in days),
        RowSpec("OpenPosition", "U0000002", day=days[2], **ETFA, position="3"),
    ]
    await _sync(
        session,
        connection,
        tmp_path,
        rows,
        accounts=(AccountSpec("U0000001"), young),
        period=(days[0], days[2]),
        by_day=True,
    )
    openings = await session.execute(
        text(
            "select t.external_id, t.booking_date, t.description, a.name, i.symbol, p.quantity"
            " from finance.transactions t join finance.postings p on p.transaction_id = t.id"
            " join finance.accounts a on a.id = p.account_id"
            " join finance.instruments i on i.id = p.instrument_id"
            " where t.external_id like 'opening:%' order by i.symbol, p.position"
        )
    )
    openings = [tuple(r) for r in openings]
    before = days[0] - timedelta(days=1)
    assert [r[1:] for r in openings] == [
        (before, "Opening balance adjustment", ACCOUNT, "ETFA", Decimal("10")),
        (before, "Opening balance adjustment", "Opening balances", "ETFA", Decimal("-10")),
        (before, "Opening balance adjustment", ACCOUNT, "GBP", Decimal("200")),
        (before, "Opening balance adjustment", "Opening balances", "GBP", Decimal("-200")),
    ]
    assert all(r[0].startswith("opening:U0000001:") for r in openings)


async def _openings(session) -> list[tuple]:
    """(booking date, instrument symbol, quantity) of the account's live openings."""
    rows = await session.execute(
        text(
            "select t.booking_date, i.symbol, p.quantity from finance.transactions t"
            " join finance.postings p on p.transaction_id = t.id"
            " join finance.accounts a on a.id = p.account_id"
            " join finance.instruments i on i.id = p.instrument_id"
            " where t.external_id like 'opening:%' and t.status <> 'void' and a.name = :a"
            " order by i.symbol"
        ),
        {"a": ACCOUNT},
    )
    return [tuple(r) for r in rows]


async def _live_cash(session, symbol: str) -> Decimal:
    return await session.scalar(
        text(
            "select coalesce(sum(p.quantity), 0) from finance.postings p"
            " join finance.transactions t on t.id = p.transaction_id"
            " join finance.accounts a on a.id = p.account_id"
            " join finance.instruments i on i.id = p.instrument_id"
            " where a.name = :a and i.symbol = :s and t.status <> 'void'"
        ),
        {"a": ACCOUNT, "s": symbol},
    )


async def test_later_window_leaves_the_opening(session, run, tmp_path) -> None:
    days = [DAY + timedelta(days=n) for n in range(2)]
    connection = await _connection(run)
    first = [RowSpec("CashReportCurrency", "U0000001", startingCash="200", endingCash="200")]
    await _sync(
        session,
        connection,
        tmp_path,
        first,
        period=(days[0], days[0]),
        accounts=(AccountSpec("U0000001"),),
    )
    # A later window's starting cash says nothing about what came before the earliest one.
    later = [RowSpec("CashReportCurrency", "U0000001", startingCash="999", endingCash="999")]
    await _sync(
        session,
        connection,
        tmp_path,
        later,
        period=(days[1], days[1]),
        accounts=(AccountSpec("U0000001"),),
    )
    assert await _openings(session) == [(days[0] - timedelta(days=1), "GBP", Decimal("200"))]


@pytest.mark.parametrize("opened_offset", [None, 0, 1])
async def test_upload_of_earlier_history_replaces_the_opening(
    session, run, tmp_path, opened_offset
) -> None:
    """Statements reaching further back than the first sync (an upload) re-derive the
    opening from the new earliest window, so the deposits they bring aren't counted twice.
    The account opened before both windows, on the earlier one's first day, or inside it."""
    days = [DAY + timedelta(days=n) for n in range(4)]
    opened = date(2020, 1, 15) if opened_offset is None else days[opened_offset]
    account = AccountSpec("U0000001", date_opened=opened)
    connection = await _connection(run)
    later = [
        RowSpec(
            "CashReportCurrency",
            "U0000001",
            day=day,
            startingCash="10000",
            endingCash="10000",
        )
        for day in days[2:]
    ]
    await _sync(
        session,
        connection,
        tmp_path,
        later,
        accounts=(account,),
        period=(days[2], days[3]),
        by_day=True,
    )
    assert await _openings(session) == [(days[1], "GBP", Decimal("10000"))]
    assert await _live_cash(session, "GBP") == Decimal("10000")

    earlier = [
        RowSpec(
            "CashReportCurrency", "U0000001", day=days[0], startingCash="0", endingCash="10000"
        ),
        RowSpec(
            "CashReportCurrency", "U0000001", day=days[1], startingCash="10000", endingCash="10000"
        ),
        RowSpec(
            "CashTransaction", "U0000001", day=days[0], transactionID="8000001", amount="10000"
        ),
    ]
    await _sync(
        session,
        connection,
        tmp_path,
        earlier,
        accounts=(account,),
        period=(days[0], days[1]),
        by_day=True,
    )
    assert await _openings(session) == []
    assert await _live_cash(session, "GBP") == Decimal("10000")
    assert await find_unbalanced_transactions(session) == []


async def test_upload_of_earlier_history_keeps_what_came_before_it(session, run, tmp_path) -> None:
    """An earlier window that still starts after the account opened: the opening moves to
    before it, sized by its starting cash."""
    days = [DAY + timedelta(days=n) for n in range(4)]
    connection = await _connection(run)
    later = [RowSpec("CashReportCurrency", "U0000001", startingCash="700", endingCash="700")]
    await _sync(
        session,
        connection,
        tmp_path,
        later,
        accounts=(AccountSpec("U0000001"),),
        period=(days[3], days[3]),
    )
    earlier = [
        RowSpec(
            "CashReportCurrency", "U0000001", day=days[0], startingCash="200", endingCash="700"
        ),
        RowSpec("CashTransaction", "U0000001", day=days[0], transactionID="8000001", amount="500"),
    ]
    await _sync(
        session,
        connection,
        tmp_path,
        earlier,
        accounts=(AccountSpec("U0000001"),),
        period=(days[0], days[0]),
        by_day=True,
    )
    assert await _openings(session) == [(days[0] - timedelta(days=1), "GBP", Decimal("200"))]
    assert await _live_cash(session, "GBP") == Decimal("700")


async def test_overlapping_windows_write_once(session, run, tmp_path) -> None:
    days = [DAY + timedelta(days=n) for n in range(7)]
    shared = days[2:5]
    connection = await _connection(run)
    rows = [
        RowSpec("Trade", "U0000001", day=shared[0], **ETFA, transactionID="9000001"),
        RowSpec("CashTransaction", "U0000001", day=shared[1], transactionID="8000001"),
        RowSpec(
            "Transfer",
            "U0000001",
            day=shared[2],
            assetCategory="CASH",
            transactionID="6000001",
            quantity="0",
            cashTransfer="25",
            account="U0000002",
        ),
        *(RowSpec("OpenPosition", "U0000001", day=day, **ETFA) for day in shared),
        *(RowSpec("CashReportCurrency", "U0000001", day=day) for day in shared),
        *(RowSpec("ConversionRate", "U0000001", day=day) for day in shared),
        *(RowSpec("EquitySummaryByReportDateInBase", "U0000001", day=day) for day in shared),
    ]
    first, _ = await _sync(
        session, connection, tmp_path, rows, period=(days[0], days[4]), by_day=True
    )
    assert first.created == 3
    second, ctx = await _sync(
        session, connection, tmp_path, rows, period=(days[2], days[6]), by_day=True
    )
    assert (second.created, second.updated, second.unchanged) == (0, 0, 3)
    assert await _external_ids(session) == [
        "cash:U0000001:8000001",
        "trade:U0000001:9000001",
        "transfer:U0000001:6000001",
    ]
    events = [e.type for e in ctx.command._events]
    assert "finance.transaction.updated" not in events
    assert events == []  # no snapshot, price or rate written again either
    assert await session.scalar(text("select count(*) from finance.balance_snapshots")) == 6


async def test_duplicate_key_different_content_fails(session, run, tmp_path) -> None:
    connection = await _connection(run)
    rows = [
        RowSpec("Trade", "U0000001", **ETFA, transactionID="9000001", quantity="10"),
        RowSpec("Trade", "U0000001", **ETFA, transactionID="9000001", quantity="11"),
    ]
    with pytest.raises(
        FlexDataError, match=r"Trade has two different rows for 9000001 of ···0001$"
    ):
        await _sync(session, connection, tmp_path, rows)


async def test_unsupported_category_not_posted_and_flagged(session, run, tmp_path) -> None:
    connection = await _connection(run)
    option = {
        "assetCategory": "OPT",
        "symbol": "ETFA  261218C00100000",
        "conid": "2001",
        "currency": "USD",
        "putCall": "C",
        "strike": "100",
        "expiry": "20261218",
        "multiplier": "100",
    }
    rows = [
        RowSpec("Trade", "U0000001", **option, transactionID="9000005"),
        RowSpec("OpenPosition", "U0000001", **option, position="1"),
        RowSpec("CashTransaction", "U0000001", transactionID="8000005", newField="x"),
    ]
    report, _ = await _sync(session, connection, tmp_path, rows)
    assert await _external_ids(session) == ["cash:U0000001:8000005"]
    assert {"Trade.assetCategory=OPT", "OpenPosition.assetCategory=OPT"} <= report.flagged
    # The parser's flags are carried along.
    assert "CashTransaction.newField" in report.flagged
    assert await session.scalar(text("select count(*) from finance.balance_snapshots")) == 0


async def test_merged_manual_instrument_keeps_decimals_and_posting(
    session, run, tmp_path, usd
) -> None:
    # The owner entered the listing with 0 decimals; IBKR's fractional shares still post:
    # the ledger checks balance at the weight's precision, not the instrument's digits.
    manual = await run(
        create_instrument,
        kind="etf",
        symbol="ETFA",
        name="Entered by hand",
        decimals=0,
        quote_instrument_id=usd.id,
        identifiers=[InstrumentIdentifier("isin", "IE00B3RBWM25")],
    )
    connection = await _connection(run)
    rows = [
        RowSpec(
            "Trade",
            "U0000001",
            **ETFA,
            quantity="0.5",
            tradePrice="100",
            proceeds="-50",
            ibCommission="0",
        )
    ]
    await _sync(session, connection, tmp_path, rows)
    postings = await _postings(session, "trade:U0000001:9000001")
    assert postings[0][1:3] == ("ETFA", Decimal("0.5"))
    assert (
        await session.scalar(
            text("select decimals from finance.instruments where id = :i"), {"i": manual.id}
        )
        == 0
    )


async def test_normalizer_version_stored_and_replayed(session, run, tmp_path) -> None:
    connection = await _connection(run)
    rows = [RowSpec("Trade", "U0000001", **ETFA, transactionID="9000001")]
    await _sync(session, connection, tmp_path, rows)
    stored = await get_connection(session, connection.id)
    assert stored.config["normalizer_version"] == NORMALIZER_VERSION
    # An older version that did not fill ibkr_trades: the next run, with nothing new,
    # replays every raw record of the connection and fills it in.
    await session.execute(text("delete from finance.ibkr_trades"))
    await session.execute(
        text(
            'update core.connections set config = config || \'{"normalizer_version": "old"}\''
            " where id = :i"
        ),
        {"i": connection.id},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(_refuse)) as http:
        report = await normalize(await _ctx(session, connection, tmp_path, http), [])
    assert report.unchanged == 1
    assert await session.scalar(text("select ibkr_transaction_id from finance.ibkr_trades")) == (
        "9000001"
    )
    stored = await get_connection(session, connection.id)
    assert stored.config["normalizer_version"] == NORMALIZER_VERSION


@pytest.mark.parametrize("listed", [False, True])
async def test_security_opening_needs_the_open_positions_section(
    session, run, tmp_path, listed
) -> None:
    # An account older than the window that bought 10 in it and reports no positions: an
    # empty Open Positions says it holds none (so 10 were held before and sold), an absent
    # one says nothing, and no opening is guessed.
    connection = await _connection(run)
    rows = [RowSpec("Trade", "U0000001", **ETFA)]
    report, _ = await _sync(
        session,
        connection,
        tmp_path,
        rows,
        accounts=(AccountSpec("U0000001"),),
        empty_sections=("OpenPositions",) if listed else (),
    )
    openings = [e for e in await _external_ids(session) if e.startswith("opening:")]
    if listed:
        assert len(openings) == 1
        assert "OpenPositions" not in report.flagged
    else:
        assert openings == []
        assert "OpenPositions" in report.flagged


async def test_rows_without_conid_are_not_one_security(session, run, tmp_path) -> None:
    # An unsupported row without a conid must not hide a stock row without one.
    connection = await _connection(run)
    stock = {k: v for k, v in ETFA.items() if k != "conid"}
    rows = [
        RowSpec(
            "Trade",
            "U0000001",
            assetCategory="OPT",
            symbol="ETFA  261218C00100000",
            currency="USD",
            transactionID="9000007",
            reportDate=_ymd(DAY - timedelta(days=1)),
        ),
        RowSpec("Trade", "U0000001", **stock, transactionID="9000008"),
    ]
    with pytest.raises(FlexDataError, match=r"Trade\.conid is missing"):
        await _sync(session, connection, tmp_path, rows)


async def test_a_row_corrected_to_nothing_voids_its_entry(session, run, tmp_path) -> None:
    connection = await _connection(run)
    deposit = {"transactionID": "8000001", "amount": "100"}
    await _sync(session, connection, tmp_path, [RowSpec("CashTransaction", "U0000001", **deposit)])
    assert await _live_cash(session, "GBP") == Decimal("100")
    corrected = {**deposit, "amount": "0"}
    report, _ = await _sync(
        session, connection, tmp_path, [RowSpec("CashTransaction", "U0000001", **corrected)]
    )
    assert report.flagged == {"CashTransaction.unposted"}
    assert await _live_cash(session, "GBP") == Decimal("0")
    assert (await _transaction(session, "cash:U0000001:8000001")).status == "void"


async def test_an_uploaded_void_is_ibkrs_so_a_later_copy_posts_again(
    session, run, tmp_path
) -> None:
    owner = Actor("user", "owner")
    connection = await _connection(run)
    row = {"transactionID": "8000001"}
    for amount in ("100", "0", "80"):
        await _sync(
            session,
            connection,
            tmp_path,
            [RowSpec("CashTransaction", "U0000001", **row, amount=amount)],
            actor=owner,
        )
    assert await _live_cash(session, "GBP") == Decimal("80")


@pytest.mark.parametrize(
    ("section", "field", "rows"),
    [
        (
            "Transfer",
            "cashTransfer",
            lambda **a: [
                RowSpec(
                    "Transfer",
                    "U0000001",
                    assetCategory="CASH",
                    transactionID="6000001",
                    quantity="0",
                    account="U0000002",
                    **a,
                )
            ],
        ),
        (
            "CashReportCurrency",
            "startingCash",
            lambda **a: [RowSpec("CashReportCurrency", "U0000001", endingCash="100", **a)],
        ),
    ],
)
async def test_an_amount_left_out_fails_and_keeps_the_entry(
    session, run, tmp_path, section, field, rows
) -> None:
    """Read as 0, a field not selected in the query would void a transfer or the opening."""
    connection = await _connection(run)
    older = (AccountSpec("U0000001"),)  # opened before the window: it gets an opening
    await _sync(session, connection, tmp_path, rows(**{field: "100"}), accounts=older)
    assert await _live_cash(session, "GBP") == Decimal("100")
    with pytest.raises(FlexDataError, match=f"^{section}.{field} is missing"):
        await _sync(session, connection, tmp_path, rows(**{field: None}), accounts=older)
    assert await _live_cash(session, "GBP") == Decimal("100")


@pytest.mark.parametrize("first", ["10", None])
async def test_opening_takes_the_earliest_positions_so_later_differences_stay(
    session, run, tmp_path, first
) -> None:
    """IBKR reports 10 (or, listing the section, nothing), then 12 with no trade between:
    the opening is what was held before the history, and the rest a reconciliation
    difference, also when all of it is normalized in one run (a replay)."""
    days = [DAY + timedelta(days=n) for n in range(2)]
    connection = await _connection(run)
    rows = [RowSpec("OpenPosition", "U0000001", day=days[1], **ETFA, position="12")]
    # As IBKR does, every daily statement has a Cash Report.
    rows += [RowSpec("CashReportCurrency", "U0000001", day=d, endingCash="0") for d in days]
    if first is not None:
        rows.append(RowSpec("OpenPosition", "U0000001", day=days[0], **ETFA, position=first))
    report, _ = await _sync(
        session,
        connection,
        tmp_path,
        rows,
        accounts=(AccountSpec("U0000001"),),
        period=(days[0], days[1]),
        by_day=True,
        empty_sections=("OpenPositions",),
    )
    before = days[0] - timedelta(days=1)
    assert await _openings(session) == ([(before, "ETFA", Decimal(first))] if first else [])
    assert [m for m in report.mismatches if "ETFA" in m]
