"""Reconciling the ledger against IBKR's reported positions and cash (design 5.11), the
warnings and notifications that follow (Decision 13), and NAV against valuation."""

import uuid
from datetime import date
from decimal import Decimal

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from lararium.core.commands import CommandContext
from lararium.core.connections import get_connection, set_warnings
from lararium.core.connectors import SyncContext
from lararium.core.documents import LocalDocumentStore
from lararium.core.json_types import JSONValue
from lararium.modules.finance.ibkr.normalize import normalize
from lararium.modules.finance.ibkr.reconcile import reconcile
from lararium.modules.finance.ibkr.sync import ingest_statement
from lararium.modules.finance.instruments import get_currency
from lararium.modules.finance.valuation import holdings_asof

from .fixtures import AccountSpec, RowSpec, build_statement
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
    session,
    connection,
    tmp_path,
    rows,
    notifier,
    *,
    accounts=(GIA,),
    period=(DAY, DAY),
    empty=(),
    later=(),
):
    """Ingest a synthetic statement and normalize it, as a sync does; with `later`, a
    (rows, period) per further window, all normalized in one run as one sync's windows are."""
    bodies = [
        build_statement(list(accounts), period=window, rows=window_rows, empty_sections=empty)
        for window_rows, window in [(rows, period), *later]
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(_refuse)) as http:

        async def context() -> SyncContext:
            return SyncContext(
                esplora_url="https://esplora.test/api",
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
        ids: list[uuid.UUID] = []
        flagged: set[str] = set()
        sections: set[tuple[str, str]] = set()
        for body in bodies:
            ingested = await ingest_statement(await context(), body, request)
            ids += ingested.raw_record_ids
            flagged |= ingested.flagged
            sections |= ingested.sections
        return await normalize(await context(), ids, frozenset(flagged), frozenset(sections))


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
    # An empty Open Positions section says IBKR holds nothing: the ledger's 10 is a difference
    # the owner is told of. It is no warning: no day has reported positions to keep it by.
    await sync(
        session,
        connection,
        tmp_path,
        [trade, cash("USD", "-1001.00")],
        notifier,
        empty=("OpenPositions",),
    )
    assert notifier.sent == [
        "IBKR: IBKR ···0001 ETFA differs from IBKR's report on 2026-11-10 by 10"
    ]
    assert await warnings_of(session, connection) == []


async def test_an_empty_positions_day_after_a_reported_one_is_told_only(
    session, run, tmp_path
) -> None:
    connection = await _connection(run)
    notifier = Notifier()
    trade = buy("9000001", ETFA, "10", "100.00", "-1000.00")
    await sync(session, connection, tmp_path, [trade, position(ETFA, "10")], notifier)
    await session.execute(
        text('update core.connections set cursor = \'{"history_from": "2026-11-10"}\'')
    )
    later = date(2026, 11, 12)
    await sync(
        session, connection, tmp_path, [], notifier, period=(later, later), empty=("OpenPositions",)
    )
    # The latest day with reported positions (the 10th) agrees: no warning; the 12th's
    # difference is told.
    assert await warnings_of(session, connection) == []
    assert notifier.sent == [
        "IBKR: IBKR ···0001 ETFA differs from IBKR's report on 2026-11-12 by 10"
    ]


async def test_a_later_clean_window_of_the_same_run_leaves_no_warning(
    session, run, tmp_path
) -> None:
    connection = await _connection(run)
    notifier = Notifier()
    later = date(2026, 11, 11)
    newer = [buy("9000001", ETFA, "10", "100.00", "-1000.00"), position(ETFA, "10")]
    report = await sync(
        session,
        connection,
        tmp_path,
        [position(ETFA, "10")],
        notifier,
        later=[(newer, (later, later))],
    )
    # The first window's end differs (the buy is on the 11th): told; the second window's,
    # the latest day, agrees, so nothing is kept.
    assert report.mismatches == []
    assert await warnings_of(session, connection) == []
    assert notifier.sent == [f"IBKR: {WARNING_A}"]


async def test_cash_difference_names_the_currency(session, run, tmp_path) -> None:
    connection = await _connection(run)
    notifier = Notifier()
    rows = [cash("GBP", "250.00")]
    await sync(session, connection, tmp_path, rows, notifier)
    assert await warnings_of(session, connection) == [
        "IBKR ···0001 GBP differs from IBKR's report on 2026-11-10 by -250.00"
    ]


FLAG_OPT = "IBKR flagged: OpenPosition.assetCategory=OPT"


def flags(warnings: list[str]) -> list[str]:
    return [w for w in warnings if w.startswith("IBKR flagged: ")]


async def test_flagged_items_are_replaced_and_told_once_per_new_name(
    session, run, tmp_path
) -> None:
    connection = await _connection(run)
    notifier = Notifier()
    rows = [RowSpec("OpenPosition", "U0000001", **(ETFA | {"assetCategory": "OPT"}), position="1")]
    await sync(session, connection, tmp_path, rows, notifier)
    assert flags(await warnings_of(session, connection)) == [FLAG_OPT]
    assert [FLAG_OPT in n for n in notifier.sent] == [True]
    await sync(session, connection, tmp_path, rows, notifier)
    assert len(notifier.sent) == 1
    # A flag that stops appearing goes; when it comes back it is told again.
    await sync(session, connection, tmp_path, [], notifier)
    assert flags(await warnings_of(session, connection)) == []
    await sync(session, connection, tmp_path, rows, notifier)
    assert flags(await warnings_of(session, connection)) == [FLAG_OPT]
    assert len(notifier.sent) == 2


async def test_one_run_tells_its_new_flags_and_differences_in_one_message(
    session, run, tmp_path
) -> None:
    connection = await _connection(run)
    notifier = Notifier()
    extra = {"newFieldA": "1", "newFieldB": "2"}
    rows = [RowSpec("OpenPosition", "U0000001", **(ETFA | extra), position="10")]
    await sync(session, connection, tmp_path, rows, notifier)
    assert flags(await warnings_of(session, connection)) == [
        "IBKR flagged: OpenPosition.newFieldA",
        "IBKR flagged: OpenPosition.newFieldB",
    ]
    assert notifier.sent == [
        "IBKR: IBKR flagged: OpenPosition.newFieldA, OpenPosition.newFieldB\n" + WARNING_A
    ]


async def test_flags_and_notifications_carry_no_other_values(session, run, tmp_path) -> None:
    # Only IBKR's vocabulary attributes (here assetCategory, when it looks like a category)
    # put a value in a flag; any other value is the owner's data.
    connection = await _connection(run)
    notifier = Notifier()
    odd = ETFA | {"assetCategory": "secret thing", "brandNewField": "PERSONAL-VALUE"}
    rows = [RowSpec("OpenPosition", "U0000001", **odd, position="1")]
    await sync(session, connection, tmp_path, rows, notifier)
    seen = [*await warnings_of(session, connection), *notifier.sent]
    assert "OpenPosition.assetCategory" in " ".join(seen)
    assert "OpenPosition.brandNewField" in " ".join(seen)
    assert not any("PERSONAL" in w or "secret" in w for w in seen)


async def test_same_amount_on_another_day_is_told_again(session, run, tmp_path) -> None:
    connection = await _connection(run)
    notifier = Notifier()
    await sync(session, connection, tmp_path, [position(ETFA, "10")], notifier)
    assert len(notifier.sent) == 1
    day = date(2026, 11, 11)
    for _ in range(2):  # the second time is a repeat of an identical warning
        await sync(
            session, connection, tmp_path, [position(ETFA, "10")], notifier, period=(DAY, day)
        )
    assert await warnings_of(session, connection) == [
        "IBKR ···0001 ETFA differs from IBKR's report on 2026-11-11 by -10"
    ]
    assert len(notifier.sent) == 2


async def test_older_statement_leaves_current_warnings_alone(session, run, tmp_path) -> None:
    connection = await _connection(run)
    notifier = Notifier()
    day = date(2026, 11, 11)
    await sync(session, connection, tmp_path, [position(ETFA, "10")], notifier, period=(DAY, day))
    current = ["IBKR ···0001 ETFA differs from IBKR's report on 2026-11-11 by -10"]
    assert await warnings_of(session, connection) == current
    # An older statement (uploaded late) with its own difference: the current warning stays,
    # nothing is added, and the owner hears of the new difference.
    await sync(session, connection, tmp_path, [position(ETFA, "12")], notifier)
    assert await warnings_of(session, connection) == current
    assert len(notifier.sent) == 2
    assert notifier.sent[1].endswith("on 2026-11-10 by -12")
    # A clean older statement clears nothing either.
    await sync(session, connection, tmp_path, [], notifier)
    assert await warnings_of(session, connection) == current


async def test_older_statement_that_completes_the_ledger_clears_the_current_warning(
    session, run, tmp_path
) -> None:
    # Found by the property tests: a newer statement fetched first, before the days that
    # hold the buy, warns; the older statement fetched later posts the buy, so the newest
    # report now agrees. Its warning was once kept, as only a newer statement replaced it.
    connection = await _connection(run)
    notifier = Notifier()
    # The first sync: the day the account opened, with nothing in it.
    await sync(session, connection, tmp_path, [], notifier, empty=("OpenPositions",))
    await session.execute(
        text('update core.connections set cursor = \'{"history_from": "2026-11-10"}\'')
    )
    newest, older = date(2026, 11, 12), date(2026, 11, 11)
    await sync(
        session, connection, tmp_path, [position(ETFA, "10")], notifier, period=(newest, newest)
    )
    assert await warnings_of(session, connection) == [
        "IBKR ···0001 ETFA differs from IBKR's report on 2026-11-12 by -10"
    ]
    rows = [buy("9000001", ETFA, "10", "100.00", "-1000.00"), position(ETFA, "10")]
    report = await sync(session, connection, tmp_path, rows, notifier, period=(older, older))
    assert report.mismatches == []
    assert await warnings_of(session, connection) == []
    assert len(notifier.sent) == 1


async def test_each_kind_is_replaced_against_its_own_latest_day(session, run, tmp_path) -> None:
    # Securities were last reported on the 12th, cash on the 14th. The older statement that
    # posts the missing buy rechecks securities on the 12th: that is their latest day, so the
    # stale warning goes, though cash was reported later.
    connection = await _connection(run)
    notifier = Notifier()
    await sync(session, connection, tmp_path, [], notifier, empty=("OpenPositions",))
    await session.execute(
        text('update core.connections set cursor = \'{"history_from": "2026-11-10"}\'')
    )
    securities, currencies, older = date(2026, 11, 12), date(2026, 11, 14), date(2026, 11, 11)
    await sync(
        session,
        connection,
        tmp_path,
        [position(ETFA, "10")],
        notifier,
        period=(securities, securities),
    )
    await sync(
        session,
        connection,
        tmp_path,
        [cash("USD", "-1001.00")],
        notifier,
        period=(currencies, currencies),
    )
    assert await warnings_of(session, connection) == [
        "IBKR ···0001 ETFA differs from IBKR's report on 2026-11-12 by -10",
        "IBKR ···0001 USD differs from IBKR's report on 2026-11-14 by 1001.00",
    ]
    rows = [buy("9000001", ETFA, "10", "100.00", "-1000.00"), position(ETFA, "10")]
    report = await sync(session, connection, tmp_path, rows, notifier, period=(older, older))
    assert report.mismatches == []
    assert await warnings_of(session, connection) == []


async def test_the_old_reconciliation_records_are_removed(session, run, tmp_path) -> None:
    # Warnings were once kept by a record of their own and of the days reconciled at; the
    # current warnings now follow from the stored reports alone.
    connection = await _connection(run)
    await session.execute(
        text(
            "update core.connections set config = config"
            ' || \'{"ibkr_reconciled": {"U0000001": {"securities": "2026-11-20"}},'
            ' "ibkr_warnings": []}\''
        )
    )
    report = await sync(session, connection, tmp_path, [position(ETFA, "10")], Notifier())
    assert report.mismatches == [WARNING_A]
    config = (await get_connection(session, connection.id)).config
    assert "ibkr_reconciled" not in config
    assert "ibkr_warnings" not in config


async def test_other_warnings_survive(session, run, tmp_path) -> None:
    connection = await _connection(run)
    await run(set_warnings, connection.id, ["Sync layer: something else"])
    notifier = Notifier()
    await sync(session, connection, tmp_path, [position(ETFA, "10")], notifier)
    assert await warnings_of(session, connection) == ["Sync layer: something else", WARNING_A]
    await sync(session, connection, tmp_path, [position(ETFA, "10")], notifier)
    assert await warnings_of(session, connection) == ["Sync layer: something else", WARNING_A]
    await sync(
        session,
        connection,
        tmp_path,
        [buy("9000001", ETFA, "10", "100", "-1000"), position(ETFA, "10")],
        notifier,
    )
    assert await warnings_of(session, connection) == ["Sync layer: something else"]


async def test_accounts_sharing_last_four_digits_do_not_clear_each_other(
    session, run, tmp_path
) -> None:
    connection = await _connection(run)
    notifier = Notifier()
    other = AccountSpec("X0000001", date_opened=DAY)
    accounts = (GIA, other)

    def held(account: str, quantity: str) -> RowSpec:
        return RowSpec("OpenPosition", account, **ETFA, position=quantity, markPrice="100")

    await sync(
        session,
        connection,
        tmp_path,
        [held("U0000001", "10"), held("X0000001", "7")],
        notifier,
        accounts=accounts,
    )
    assert await warnings_of(session, connection) == [WARNING_A, WARNING_A[:-3] + "-7"]
    # The first account's difference is fixed; the second's stays, though it shows the same
    # last four digits.
    fixed = buy("9000001", ETFA, "10", "100.00", "-1000.00")
    await sync(
        session,
        connection,
        tmp_path,
        [fixed, held("U0000001", "10"), held("X0000001", "7")],
        notifier,
        accounts=accounts,
    )
    assert await warnings_of(session, connection) == [WARNING_A[:-3] + "-7"]
    # A statement of the first account alone leaves the second's warning as it is: every run
    # checks every account at its latest day.
    await sync(session, connection, tmp_path, [fixed, held("U0000001", "10")], notifier)
    assert await warnings_of(session, connection) == [WARNING_A[:-3] + "-7"]


async def test_a_section_not_in_the_statement_keeps_its_warnings(session, run, tmp_path) -> None:
    connection = await _connection(run)
    notifier = Notifier()
    await sync(session, connection, tmp_path, [position(ETFA, "10")], notifier)
    # This statement has a Cash Report only: cash is reconciled (and agrees), securities not.
    await sync(session, connection, tmp_path, [cash("USD", "0")], notifier)
    assert await warnings_of(session, connection) == [WARNING_A]


async def test_a_failing_notifier_leaks_nothing(session, run, tmp_path, caplog) -> None:
    class Failing:
        async def send(self, text: str) -> None:
            raise RuntimeError("https://hooks.example.test/SECRET-TOKEN")

    connection = await _connection(run)
    await sync(session, connection, tmp_path, [position(ETFA, "10")], Failing())
    assert await warnings_of(session, connection) == [WARNING_A]
    assert "SECRET-TOKEN" not in caplog.text
    assert "RuntimeError" in caplog.text


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
