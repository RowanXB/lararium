import logging
import uuid
from datetime import UTC, date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from lararium.core.actors import Actor
from lararium.core.commands import CommandContext
from lararium.core.connections import ConnectionRecord, create_connection, get_connection
from lararium.core.connectors import SyncContext
from lararium.core.documents import LocalDocumentStore
from lararium.core.json_types import JSONValue
from lararium.modules.finance.errors import FinanceValidationError
from lararium.modules.finance.ibkr.fields import INSTRUMENT_FIELDS, SECTIONS
from lararium.modules.finance.ibkr.instruments import listing
from lararium.modules.finance.ibkr.normalize import normalize
from lararium.modules.finance.ibkr.parser import FlexDataError, Row
from lararium.modules.finance.ibkr.sync import ingest_statement
from lararium.modules.finance.ibkr.tables import (
    column_name,
    parse_moment,
    trade_detail_key,
    typed_values,
    upsert_nav,
)
from lararium.modules.finance.identifiers import assign_identifier
from lararium.modules.finance.instruments import (
    InstrumentIdentifier,
    add_identifiers,
    create_instrument,
    find_by_identifier,
    get_instrument,
)

from .fixtures import AccountSpec, RowSpec, build_statement

CONNECTOR = Actor("connector", "ibkr")
GIA = AccountSpec("U0000001")
ISA = AccountSpec("U0000002", date_opened=date(2021, 4, 6), date_closed=date(2025, 5, 2))
DAY = date(2026, 11, 10)
ISA_TYPE = "IRA-Individual Savings Account/UK"


async def _connection(run) -> ConnectionRecord:
    return await run(
        lambda ctx: create_connection(ctx, connector="ibkr", display_name="IBKR", config={})
    )


class _Notifier:
    async def send(self, text: str) -> None:
        return None


async def _ctx(session, connection, tmp_path, http) -> SyncContext:
    return SyncContext(
        command=CommandContext(session, CONNECTOR, uuid.uuid4()),
        connection=await get_connection(session, connection.id),
        sync_id=uuid.uuid4(),
        master_key=bytes(32),
        http=http,
        notifier=_Notifier(),
        session_factory=async_sessionmaker(bind=session.bind),
        documents=LocalDocumentStore(tmp_path),
    )


def _refuse(request: httpx.Request) -> httpx.Response:
    raise AssertionError("no HTTP in this test")


async def _ingest_and_normalize(session, connection, tmp_path, accounts, rows):
    body = build_statement(accounts, period=(DAY, DAY), rows=rows)
    async with httpx.AsyncClient(transport=httpx.MockTransport(_refuse)) as http:
        ctx = await _ctx(session, connection, tmp_path, http)
        request: dict[str, JSONValue] = {"upload": "x.xml"}
        ingested = await ingest_statement(ctx, body, request)
        ctx = await _ctx(session, connection, tmp_path, http)
        return await normalize(ctx, ingested.raw_record_ids, ingested.flagged)


async def _count_events(session) -> int:
    return await session.scalar(text("select count(*) from core.events"))


async def test_accounts_created_once_and_closed(session, run, tmp_path) -> None:
    connection = await _connection(run)
    rows = [RowSpec("AccountInformation", "U0000002", customerType=ISA_TYPE)]
    first = await _ingest_and_normalize(session, connection, tmp_path, [GIA, ISA], rows)
    assert (first.accounts_created, first.accounts_unchanged) == (2, 0)
    events = await _count_events(session)
    second = await _ingest_and_normalize(session, connection, tmp_path, [GIA, ISA], rows)
    assert (second.accounts_created, second.accounts_updated, second.accounts_unchanged) == (
        0,
        0,
        2,
    )
    assert await _count_events(session) == events

    accounts = (
        await session.execute(
            text(
                "select a.name, a.tax_wrapper, a.status, a.closed_on, a.opened_on, a.subtype,"
                " a.balance_mode, a.source_connection_id, i.name as institution,"
                " x.ibkr_account_id, g.value"
                " from finance.accounts a"
                " join core.entities i on i.id = a.institution_entity_id"
                " join finance.ibkr_accounts x on x.account_id = a.id"
                " join finance.account_identifiers g on g.account_id = a.id"
                " order by x.ibkr_account_id"
            )
        )
    ).all()
    gia, isa = accounts
    assert (gia.name, gia.tax_wrapper, gia.status, gia.closed_on) == (
        "IBKR ···0001",
        "gia",
        "open",
        None,
    )
    assert (isa.name, isa.tax_wrapper, isa.status, isa.closed_on) == (
        "IBKR ···0002",
        "isa",
        "closed",
        date(2025, 5, 2),
    )
    assert isa.opened_on == date(2021, 4, 6)
    assert (gia.subtype, gia.balance_mode, gia.institution) == (
        "broker",
        "transactions",
        "Interactive Brokers",
    )
    assert gia.source_connection_id == connection.id
    assert [a.value for a in accounts] == ["U0000001", "U0000002"]
    stored = (await get_connection(session, connection.id)).config["account_map"]
    assert isinstance(stored, dict) and set(stored) == {"U0000001", "U0000002"}


async def test_unknown_customer_type_leaves_tax_wrapper(session, run, tmp_path) -> None:
    connection = await _connection(run)
    rows = [RowSpec("AccountInformation", "U0000001", customerType="Corporation")]
    await _ingest_and_normalize(session, connection, tmp_path, [GIA], rows)
    wrapper = await session.scalar(text("select tax_wrapper from finance.accounts limit 1"))
    assert wrapper == "none"


async def test_never_takes_owner_assigned_identifier(
    session, run, make_account, tmp_path, caplog
) -> None:
    mine = await make_account(name="Mine", balance_mode="hybrid")
    await run(assign_identifier, mine.id, "ibkr_account", "u0000002")
    connection = await _connection(run)
    with caplog.at_level(logging.WARNING):
        await _ingest_and_normalize(session, connection, tmp_path, [ISA], [])
    holder = await session.scalar(
        text("select account_id from finance.account_identifiers where value = 'U0000002'")
    )
    assert holder == mine.id
    assert any("left there" in message for message in caplog.messages)
    assert "U0000002" not in caplog.text


async def test_personal_fields_are_stored_not_logged(session, run, tmp_path, caplog) -> None:
    connection = await _connection(run)
    rows = [
        RowSpec(
            "AccountInformation",
            "U0000001",
            name="Alex Example",
            city="Springfield",
            dateOfBirth="19900131",
            dateFunded="20200201",
        )
    ]
    with caplog.at_level(logging.DEBUG):
        await _ingest_and_normalize(session, connection, tmp_path, [GIA], rows)
    row = (
        await session.execute(
            text("select name, city, date_of_birth, date_funded from finance.ibkr_accounts")
        )
    ).one()
    assert tuple(row) == ("Alex Example", "Springfield", date(1990, 1, 31), date(2020, 2, 1))
    assert "Alex Example" not in caplog.text


async def test_add_identifiers_skips_existing_and_iso(session, run, gbp) -> None:
    mine = await run(
        create_instrument,
        kind="equity",
        symbol="AAA",
        name="Alpha",
        decimals=4,
        quote_instrument_id=gbp.id,
        identifiers=[InstrumentIdentifier("isin", "GB00B03MLX29")],
    )
    other = await run(
        create_instrument,
        kind="equity",
        symbol="BBB",
        name="Beta",
        decimals=4,
        quote_instrument_id=gbp.id,
        identifiers=[InstrumentIdentifier("ibkr_conid", "555")],
    )
    with pytest.raises(FinanceValidationError):
        await run(add_identifiers, mine.id, [InstrumentIdentifier("iso4217", "GBP")])
    record = await run(
        add_identifiers,
        mine.id,
        [
            InstrumentIdentifier("isin", "GB0000000009"),  # the scheme is there: kept
            InstrumentIdentifier("ibkr_conid", "555"),  # another instrument's: skipped
            InstrumentIdentifier("figi", "bbg000bdtbl9"),
        ],
    )
    assert {(i.scheme, i.value) for i in record.identifiers} == {
        ("isin", "GB00B03MLX29"),
        ("figi", "BBG000BDTBL9"),
    }
    assert (await find_by_identifier(session, "ibkr_conid", "555")).id == other.id


def _row(**attributes: str) -> Row:
    base = {
        "accountId": "U0000001",
        "currency": "GBP",
        "assetCategory": "STK",
        "symbol": "VWRL",
        "description": "Example World ETF",
        "conid": "1001",
        "isin": "IE00B3RBWM25",
        "listingExchange": "LSEETF",
        "subCategory": "ETF",
        "multiplier": "1",
    }
    return Row("OpenPosition", "U0000001", DAY, DAY, {**base, **attributes})


async def test_same_isin_two_currencies_two_instruments(session, run) -> None:
    gbp_listing = await run(listing, _row())
    usd_listing = await run(listing, _row(currency="USD", conid="1002", symbol="VWRD"))
    assert gbp_listing != usd_listing
    first = await get_instrument(session, gbp_listing)
    second = await get_instrument(session, usd_listing)
    assert first.kind == second.kind == "etf"
    assert first.decimals == 4
    assert {(i.scheme, i.value) for i in first.identifiers} == {
        ("ibkr_conid", "1001"),
        ("isin", "IE00B3RBWM25"),
    }
    assert {(i.scheme, i.value) for i in second.identifiers} == {("ibkr_conid", "1002")}
    isins = (
        await session.execute(
            text(
                "select isin, conid, listing_exchange, sub_category, multiplier"
                " from finance.ibkr_instruments order by conid"
            )
        )
    ).all()
    assert [tuple(r) for r in isins] == [
        ("IE00B3RBWM25", "1001", "LSEETF", "ETF", Decimal("1")),
        ("IE00B3RBWM25", "1002", "LSEETF", "ETF", Decimal("1")),
    ]
    # Seen again: the same instruments.
    assert await run(listing, _row()) == gbp_listing
    assert await run(listing, _row(currency="USD", conid="1002")) == usd_listing


async def test_merges_by_isin_same_currency(session, run, gbp) -> None:
    manual = await run(
        create_instrument,
        kind="etf",
        symbol="VWRL",
        name="Entered by hand",
        decimals=2,
        quote_instrument_id=gbp.id,
        identifiers=[InstrumentIdentifier("isin", "IE00B3RBWM25")],
    )
    assert await run(listing, _row()) == manual.id
    record = await get_instrument(session, manual.id)
    assert ("ibkr_conid", "1001") in {(i.scheme, i.value) for i in record.identifiers}
    # A second conid for the same ISIN and currency is another listing, not a flip of the first.
    other = await run(listing, _row(conid="1003"))
    assert other != manual.id


@pytest.mark.parametrize(
    ("attributes", "kind"),
    [
        ({"subCategory": ""}, "equity"),
        ({"assetCategory": "FUND", "subCategory": ""}, "fund"),
    ],
)
async def test_kinds(session, run, attributes, kind) -> None:
    found = await run(listing, _row(**attributes))
    assert (await get_instrument(session, found)).kind == kind


async def test_unsupported_category_flagged(run) -> None:
    assert await run(listing, _row(assetCategory="OPT")) is None
    assert await run(listing, _row(assetCategory="CASH")) is None


async def test_row_without_conid_names_the_field(run) -> None:
    with pytest.raises(FlexDataError, match=r"OpenPosition\.conid"):
        await run(listing, _row(conid=""))


async def test_ibkr_account_identifier_checked_by_database(session) -> None:
    for value in ("u12", "U12", "12345", "U1234567890123"):
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "insert into finance.account_identifiers (kind, value, created_by)"
                        " values ('ibkr_account', :v, 'user')"
                    ),
                    {"v": value},
                )
    await session.execute(
        text(
            "insert into finance.account_identifiers (kind, value, created_by)"
            " values ('ibkr_account', 'U0000001', 'user')"
        )
    )
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                text(
                    "insert into finance.account_identifiers (kind, value, created_by)"
                    " values ('uk_bank_account', 'U0000009', 'user')"
                )
            )


async def test_typed_values_and_upserts(session, run, gbp, make_account) -> None:
    account = await make_account(name="X")
    values = await typed_values(
        session,
        "ibkr_nav",
        "EquitySummaryByReportDateInBase",
        {"accountId": "U0000001", "reportDate": "20261110", "total": "1234.50", "cash": ""},
    )
    assert values == {"report_date": date(2026, 11, 10), "total": Decimal("1234.50"), "cash": None}
    key = {"account_id": account.id, "report_date": values.pop("report_date")}
    assert await run(upsert_nav, key, values) is True
    assert await run(upsert_nav, key, values) is False  # unchanged: nothing written
    assert await run(upsert_nav, key, {**values, "total": Decimal("1234.5")}) is False
    assert await run(upsert_nav, key, {**values, "total": Decimal("9")}) is True
    with pytest.raises(FlexDataError, match=r"Section\.total is not a number"):
        await typed_values(session, "ibkr_nav", "Section", {"total": "NaN"})


def test_trade_detail_key_and_moments() -> None:
    assert trade_detail_key({"transactionID": "7", "tradeID": "8"}) == "7"
    assert trade_detail_key({"transactionID": "", "tradeID": "8"}) == "8"
    one = trade_detail_key({"a": "1", "b": "2"})
    assert one == trade_detail_key({"b": "2", "a": "1"}) != trade_detail_key({"a": "1"})
    moment = parse_moment("20261110;093000 EST", "Trade.dateTime")
    assert moment.isoformat() == "2026-11-10T09:30:00-05:00"
    assert parse_moment("20260710;093000 EDT", "x").utcoffset().total_seconds() == -4 * 3600
    assert parse_moment("20261110", "x").isoformat() == "2026-11-10T00:00:00-05:00"
    # The hour clocks repeat when they go back: its suffix says which of the two it is.
    utc = [
        parse_moment(f"20261101;013000 {suffix}", "x").astimezone(UTC).time().isoformat()
        for suffix in ("EDT", "EST")
    ]
    assert utc == ["05:30:00", "06:30:00"]
    london = [
        parse_moment(f"20261025;013000 {suffix}", "x").astimezone(UTC).time().isoformat()
        for suffix in ("BST", "GMT")
    ]
    assert london == ["00:30:00", "01:30:00"]
    with pytest.raises(FlexDataError, match="time zone"):
        parse_moment("20261110;093000 CET", "Trade.dateTime")


async def test_every_routed_attribute_has_a_column(session) -> None:
    # A misspelt column would drop data silently, as typed_values ignores what has no column.
    tables_by_section = {
        "AccountInformation": "ibkr_accounts",
        "Trade": "ibkr_trades",
        "TradeDetail": "ibkr_trade_details",
        "CashTransaction": "ibkr_cash_transactions",
        "CorporateAction": "ibkr_corporate_actions",
        "Transfer": "ibkr_transfers",
        "OpenPosition": "ibkr_positions",
        "PositionLot": "ibkr_position_lots",
        "CashReportCurrency": "ibkr_cash_reports",
        "EquitySummaryByReportDateInBase": "ibkr_nav",
        "ChangeInNAV": "ibkr_change_in_nav",
        "StatementOfFundsLine": "ibkr_statement_of_funds",
        "MTMPerformanceSummaryUnderlying": "ibkr_mtm_performance",
        "PriorPeriodPosition": "ibkr_prior_period_positions",
    }
    # What the ledger or a caller's reference holds, rather than a column.
    elsewhere = {
        "accountId", "currency", "model", "acctAlias", "reportDate", "dateTime", "quantity",
        "tradePrice", "proceeds", "taxes", "ibCommission", "ibCommissionCurrency", "amount",
        "description", "position", "date", "settleDate", "settleDateTarget", "cashTransfer",
        "transactionID", "total", "originatingTransactionID", "dateOpened", "dateClosed",
        "actionDescription", "levelOfDetail", "fromDate", "toDate", "assetCategory", "conid",
    } | INSTRUMENT_FIELDS  # fmt: skip
    # Sections with no ledger row: all but what ibkr_accounts (alias, model) and the account
    # and currency references hold is a column, and for a prior period position its security.
    report = {"accountId", "acctAlias", "model", "currency"}
    exempt = {
        "ChangeInNAV": report,
        "StatementOfFundsLine": report,
        "MTMPerformanceSummaryUnderlying": report,
        "PriorPeriodPosition": report | INSTRUMENT_FIELDS,
    }
    rows = await session.execute(
        text(
            "select table_name, column_name from information_schema.columns"
            " where table_schema = 'finance' and table_name like 'ibkr_%'"
        )
    )
    columns = {(r.table_name, r.column_name) for r in rows}
    missing = [
        f"{section}.{attribute}"
        for section, table in tables_by_section.items()
        for attribute in sorted(SECTIONS[section].known - exempt.get(section, elsewhere))
        if (table, column_name(table, attribute)) not in columns
    ]
    assert missing == []


def test_known_attributes_map_to_distinct_columns() -> None:
    from lararium.modules.finance.ibkr.fields import (
        INSTRUMENT_FIELDS,
        PERSONAL_FIELDS,
        SECTIONS,
    )
    from lararium.modules.finance.typed_tables import column_map

    for spec in SECTIONS.values():
        column_map(sorted(spec.known | spec.required))
    column_map(sorted(INSTRUMENT_FIELDS))
    column_map(sorted(PERSONAL_FIELDS))


async def test_typed_values_refuses_attributes_that_alias_one_column(session) -> None:
    with pytest.raises(FlexDataError) as error:
        await typed_values(
            session, "ibkr_nav", "Section", {"fxRate": "111", "fx_rate": "222", "total": "333"}
        )
    assert str(error.value) == "Section: 'fxRate' and 'fx_rate' are both column fx_rate"


async def test_typed_values_collisions_use_the_table_aware_names(session) -> None:
    # On a ledger-row table IBKR's transactionID is the column ibkr_transaction_id, so an
    # attribute that is literally that name collides with it; on another table they differ.
    row = {"transactionID": "1", "ibkrTransactionId": "2"}
    with pytest.raises(FlexDataError) as error:
        await typed_values(session, "ibkr_trades", "Trade", row)
    assert (
        str(error.value)
        == "Trade: 'transactionID' and 'ibkrTransactionId' are both column ibkr_transaction_id"
    )
    assert column_name("ibkr_nav", "transactionID") == "transaction_id"
    assert column_name("ibkr_trades", "transaction_id") == "ibkr_transaction_id"
    with pytest.raises(FlexDataError, match="both column ibkr_transaction_id"):
        await typed_values(
            session, "ibkr_trades", "Trade", {"transactionID": "1", "transaction_id": "2"}
        )
