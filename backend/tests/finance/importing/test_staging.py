import re
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from pydantic import TypeAdapter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core import models as core_models
from lararium.core.documents import LocalDocumentStore
from lararium.modules.finance import models
from lararium.modules.finance.accounts import AccountRecord, close_account
from lararium.modules.finance.errors import (
    FileFormatError,
    ImportBatchNotFoundError,
    ImportNotAllowedError,
    NotACurrencyError,
)
from lararium.modules.finance.importing.parsed import ParsedRow
from lararium.modules.finance.importing.pdf import parse_pdf
from lararium.modules.finance.importing.profiles import (
    ImportProfileRecord,
    PdfProfileConfig,
    ProfileConfig,
    create_profile,
    update_profile,
)
from lararium.modules.finance.importing.staging import (
    ImportBatchRecord,
    ImportRowRecord,
    batches_relying_on,
    get_batch,
    list_batch_rows,
    list_batches,
    stage_import,
)
from lararium.modules.finance.instruments import InstrumentRecord

from .synthetic_pdfs import BocRow, BocStatement, HsbcRow, HsbcStatement, boc_pdf, hsbc_pdf

type Run = Callable[..., Awaitable[Any]]
type MakeAccount = Callable[..., Awaitable[AccountRecord]]
type Commit = Callable[[UUID], Awaitable[None]]

NO_BALANCES = "This file has no balances, so its opening balance can't be checked"
HEADER = "Date,Description,Amount\n"
BALANCE_HEADER = "Date,Description,Amount,Balance\n"
BALANCE_COLUMNS = {
    "date": "Date",
    "description": ["Description"],
    "amount": "Amount",
    "balance": "Balance",
}

PROFILE_CONFIG: TypeAdapter[Any] = TypeAdapter(ProfileConfig)
_names = iter(range(1_000_000))


async def _profile(run: Run, **config: Any) -> ImportProfileRecord:
    model = PROFILE_CONFIG.validate_python(config)
    return await run(create_profile, name=f"Example Bank {next(_names)}", config=model)


async def _csv_profile(run: Run, **overrides: Any) -> ImportProfileRecord:
    config: dict[str, Any] = {
        "format": "csv",
        "columns": BALANCE_COLUMNS,
        "date_format": "%d/%m/%Y",
    }
    return await _profile(run, **(config | overrides))


@pytest.fixture
async def account(make_account: MakeAccount) -> AccountRecord:
    return await make_account(name="Example Current", balance_mode="transactions")


@pytest.fixture
def stage(
    run: Run, store: LocalDocumentStore, account: AccountRecord, gbp: InstrumentRecord
) -> Callable[..., Awaitable[ImportBatchRecord]]:
    async def _stage(
        profile: ImportProfileRecord,
        content: str | bytes,
        *,
        account_id: UUID | None = None,
        default: UUID | str | None = "gbp",
        filename: str = "statement.csv",
        media_type: str = "text/csv",
    ) -> ImportBatchRecord:
        return await run(
            stage_import,
            store,
            account_id=account.id if account_id is None else account_id,
            profile_id=profile.id,
            content=content.encode() if isinstance(content, str) else content,
            filename=filename,
            media_type=media_type,
            default_instrument_id=gbp.id if default == "gbp" else default,
        )

    return _stage


type Stage = Callable[..., Awaitable[ImportBatchRecord]]


async def _rows(session: AsyncSession, batch: ImportBatchRecord) -> list[ImportRowRecord]:
    return await list_batch_rows(session, batch.id)


def _statuses(rows: list[ImportRowRecord]) -> list[str]:
    return [row.status for row in rows]


def _adjustments(rows: list[ImportRowRecord]) -> list[ImportRowRecord]:
    return [row for row in rows if row.kind == "opening_adjustment"]


async def _dedupe_keys(session: AsyncSession, batch: ImportBatchRecord) -> list[str | None]:
    r = models.ImportRow
    result = await session.scalars(
        select(r.dedupe_key).where(r.batch_id == batch.id).order_by(r.position)
    )
    return list(result)


def _ofx(transactions: list[tuple[str, str, str]], *, start: str, end: str, extra: str = "") -> str:
    """transactions: (DTPOSTED, TRNAMT, FITID)."""
    body = "\n".join(
        f"<STMTTRN>\n<TRNTYPE>OTHER\n<DTPOSTED>{posted}\n<TRNAMT>{amount}\n<FITID>{fitid}\n"
        f"<NAME>Example Payee {fitid}\n</STMTTRN>"
        for posted, amount, fitid in transactions
    )
    return f"""OFXHEADER:100
DATA:OFXSGML
VERSION:102

<OFX>
<BANKMSGSRSV1>
<STMTTRNRS>
<STMTRS>
<CURDEF>GBP
<BANKTRANLIST>
<DTSTART>{start}
<DTEND>{end}
{body}
</BANKTRANLIST>
{extra}
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>
"""


# Counts, statuses, keys --------------------------------------------------------------


async def test_stage_counts_and_rows(
    session: AsyncSession, stage: Stage, csv_profile: ImportProfileRecord
) -> None:
    content = HEADER + "10/11/2026,Coffee,-3.50\n32/11/2026,Bad date,-1.00\n11/11/2026,Zero,0.00\n"
    batch = await stage(csv_profile, content)

    assert batch.status == "staged"
    assert batch.counts == {
        "new": 1,
        "duplicate": 0,
        "skipped": 1,
        "error": 1,
        "committed": 0,
        "rolled_back": 0,
    }
    rows = await _rows(session, batch)
    assert [row.position for row in rows] == [0, 1, 2]
    assert _statuses(rows) == ["new", "error", "skipped"]
    assert rows[0].quantity == Decimal("-3.50")
    assert rows[0].description == "Coffee"
    assert rows[0].raw == {"Date": "10/11/2026", "Description": "Coffee", "Amount": "-3.50"}
    assert rows[1].error is not None and "date" in rows[1].error
    assert rows[2].note == "zero amount"
    assert batch.filename == "statement.csv"
    assert batch.profile_id == csv_profile.id and batch.profile_version == 1

    assert await get_batch(session, batch.id) == batch
    assert [b.id for b in await list_batches(session)] == [batch.id]
    event = await session.scalar(
        select(core_models.Event).where(core_models.Event.type == "finance.import.staged")
    )
    assert event is not None and event.data["batch_id"] == str(batch.id)


async def test_get_missing_batch(session: AsyncSession) -> None:
    with pytest.raises(ImportBatchNotFoundError):
        await get_batch(session, UUID(int=1))


async def test_list_batches_newest_first(
    session: AsyncSession, stage: Stage, csv_profile: ImportProfileRecord
) -> None:
    first = await stage(csv_profile, HEADER + "10/11/2026,Coffee,-3.50\n")
    second = await stage(csv_profile, HEADER + "11/11/2026,Tea,-2.50\n")

    assert [batch.id for batch in await list_batches(session)] == [second.id, first.id]


async def test_identical_rows_in_one_file_are_both_new(
    session: AsyncSession, stage: Stage, csv_profile: ImportProfileRecord
) -> None:
    batch = await stage(csv_profile, HEADER + "10/11/2026,Coffee,-3.50\n10/11/2026,Coffee,-3.50\n")

    assert _statuses(await _rows(session, batch)) == ["new", "new"]
    keys = await _dedupe_keys(session, batch)
    assert None not in keys and keys[0] != keys[1]


async def test_external_id_duplicate_within_file(
    session: AsyncSession, run: Run, stage: Stage
) -> None:
    profile = await _profile(run, format="ofx")
    content = _ofx(
        [("20261110", "-3.50", "EXB1"), ("20261110", "-3.50", "EXB1")],
        start="20261101",
        end="20261130",
    )
    batch = await stage(profile, content, filename="statement.ofx")

    rows = await _rows(session, batch)
    assert _statuses(rows) == ["new", "error"]
    assert rows[1].error == "the file lists transaction id EXB1 twice"
    assert batch.counts["error"] == 1

    # A repeated running-balance row is just as much an error.
    repeated = BALANCE_HEADER + "02/11/2026,Shop,-10.00,90.00\n02/11/2026,Shop,-10.00,90.00\n"
    rows = await _rows(session, await stage(await _csv_profile(run), repeated))
    assert _statuses(rows) == ["new", "error"]
    assert rows[1].error is not None and "twice" in rows[1].error


# Which accounts and currencies -------------------------------------------------------


async def test_snapshots_account_refused(
    make_account: MakeAccount, stage: Stage, csv_profile: ImportProfileRecord
) -> None:
    snapshots = await make_account(name="Example Savings", balance_mode="snapshots")
    with pytest.raises(ImportNotAllowedError, match="snapshots"):
        await stage(csv_profile, HEADER + "10/11/2026,Coffee,-3.50\n", account_id=snapshots.id)


async def test_closed_account_refused(
    run: Run, account: AccountRecord, stage: Stage, csv_profile: ImportProfileRecord
) -> None:
    await run(close_account, account.id, date(2026, 11, 1))
    with pytest.raises(ImportNotAllowedError, match="closed"):
        await stage(csv_profile, HEADER + "10/11/2026,Coffee,-3.50\n")


async def test_default_instrument_must_be_fiat(
    stage: Stage, csv_profile: ImportProfileRecord, btc: InstrumentRecord
) -> None:
    with pytest.raises(NotACurrencyError):
        await stage(csv_profile, HEADER + "10/11/2026,Coffee,-3.50\n", default=btc.id)


async def test_file_format_error_persists_nothing(
    session: AsyncSession, stage: Stage, csv_profile: ImportProfileRecord
) -> None:
    with pytest.raises(FileFormatError):
        await stage(csv_profile, "Foo,Bar\n1,2\n")

    assert await session.scalar(select(func.count()).select_from(models.ImportBatch)) == 0
    assert await session.scalar(select(func.count()).select_from(core_models.Document)) == 0


async def test_currency_resolution(
    session: AsyncSession,
    run: Run,
    stage: Stage,
    csv_profile: ImportProfileRecord,
    gbp: InstrumentRecord,
) -> None:
    content = HEADER + "10/11/2026,Coffee,-3.50\n11/11/2026,Tea,-2.50\n"
    rows = await _rows(session, await stage(csv_profile, content, default=None))
    assert _statuses(rows) == ["error", "error"]
    assert all(row.error is not None and "currency" in row.error for row in rows)

    rows = await _rows(session, await stage(csv_profile, content))
    assert _statuses(rows) == ["new", "new"]
    assert all(row.instrument_id == gbp.id for row in rows)

    profile = await _csv_profile(
        run,
        columns={
            "date": "Date",
            "description": ["Description"],
            "amount": "Amount",
            "currency": "Currency",
        },
    )
    content = "Date,Description,Amount,Currency\n10/11/2026,Coffee,-3.50,GBP\n10/11/2026,X,-1,XYZ\n"
    rows = await _rows(session, await stage(profile, content, default=None))
    assert _statuses(rows) == ["new", "error"]
    assert rows[0].instrument_id == gbp.id
    assert rows[1].error is not None and "XYZ" in rows[1].error


async def test_date_only_rows_keep_booking_date_across_dst(
    session: AsyncSession, stage: Stage, csv_profile: ImportProfileRecord
) -> None:
    batch = await stage(csv_profile, HEADER + "29/03/2026,Coffee,-3.50\n25/10/2026,Tea,-2.50\n")

    rows = await _rows(session, batch)
    assert [row.booking_date for row in rows] == [date(2026, 3, 29), date(2026, 10, 25)]
    assert [row.occurred_at for row in rows] == [
        datetime(2026, 3, 29, 0, 0, tzinfo=UTC),
        datetime(2026, 10, 24, 23, 0, tzinfo=UTC),
    ]
    r = models.ImportRow
    zones = await session.scalars(select(r.occurred_tz).where(r.batch_id == batch.id))
    assert set(zones) == {"Europe/London"}


async def test_batch_snapshots_profile(
    session: AsyncSession, run: Run, stage: Stage, csv_profile: ImportProfileRecord
) -> None:
    batch = await stage(csv_profile, HEADER + "10/11/2026,Coffee,-3.50\n")
    changed = csv_profile.config.model_copy(update={"date_format": "%Y-%m-%d"})
    await run(update_profile, csv_profile.id, name=csv_profile.name, config=changed)

    b = models.ImportBatch
    stored = (
        await session.execute(
            select(b.profile_version, b.profile_config, b.parser_version).where(b.id == batch.id)
        )
    ).one()
    assert stored.profile_version == 1
    assert stored.profile_config == csv_profile.config.model_dump(mode="json")
    assert stored.parser_version == "csv-1"
    assert (await get_batch(session, batch.id)).profile_version == 1


# Balance chain -----------------------------------------------------------------------


async def test_balance_chain_break_is_error(
    session: AsyncSession, run: Run, stage: Stage, make_account: MakeAccount
) -> None:
    # A hybrid account, so the chain is checked without an opening adjustment in the way.
    hybrid = await make_account(name="Example Hybrid", balance_mode="hybrid")
    profile = await _csv_profile(run)
    content = (
        BALANCE_HEADER
        + "01/11/2026,Pay,100.00,100.00\n02/11/2026,Shop,-10.00,90.00\n"
        + "03/11/2026,Refund,10.00,95.00\n"
    )
    rows = await _rows(session, await stage(profile, content, account_id=hybrid.id))

    assert _statuses(rows) == ["new", "new", "error"]
    assert rows[2].error is not None
    assert "expected 100.00" in rows[2].error and "95.00" in rows[2].error


CHAINED = (
    BALANCE_HEADER
    + "01/11/2026,Pay,100.00,100.00\n02/11/2026,Shop,-10.00,90.00\n"
    + "03/11/2026,Refund,10.00,100.00\n"
)


async def test_balance_chain_catches_wrong_sign(
    session: AsyncSession, run: Run, stage: Stage
) -> None:
    right = await _rows(session, await stage(await _csv_profile(run), CHAINED))
    assert _statuses(right) == ["new", "new", "new"]  # opening 0.00: no adjustment
    wrong = await _csv_profile(run, sign="outflow_positive")
    rows = await _rows(session, await stage(wrong, CHAINED))
    assert _statuses(rows) == ["new", "error", "error"]


async def test_balance_chain_ignores_skipped_rows(
    session: AsyncSession, run: Run, stage: Stage
) -> None:
    profile = await _csv_profile(
        run,
        columns=BALANCE_COLUMNS,
        include_only={"State": ["COMPLETED"]},
    )
    content = (
        "Date,Description,Amount,Balance,State\n"
        "01/11/2026,Pay,100.00,100.00,COMPLETED\n"
        "02/11/2026,Hotel,-20.00,,PENDING\n"
        "03/11/2026,Shop,-10.00,90.00,COMPLETED\n"
    )
    rows = await _rows(session, await stage(profile, content))

    statement_rows = [row for row in rows if row.kind == "statement"]
    assert _statuses(statement_rows) == ["new", "skipped", "new"]
    assert statement_rows[1].note == "State is PENDING"


async def test_newest_first_chain(session: AsyncSession, run: Run, stage: Stage) -> None:
    profile = await _csv_profile(run, row_order="newest_first")
    lines = CHAINED.splitlines()
    content = "\n".join([lines[0], *reversed(lines[1:])]) + "\n"
    rows = await _rows(session, await stage(profile, content))

    assert _statuses(rows) == ["new", "new", "new"]


# Opening adjustment ------------------------------------------------------------------

NOVEMBER = BALANCE_HEADER + "02/11/2026,Groceries,-18.00,1482.00\n03/11/2026,Bus,-2.00,1480.00\n"


async def test_opening_adjustment_from_first_balance(
    session: AsyncSession, run: Run, stage: Stage, gbp: InstrumentRecord
) -> None:
    batch = await stage(await _csv_profile(run), NOVEMBER)

    rows = await _rows(session, batch)
    (adjustment,) = _adjustments(rows)
    assert rows[0] == adjustment  # by position: -1 first
    assert adjustment.position == -1
    assert adjustment.status == "new"
    assert adjustment.quantity == Decimal("1500.00")
    assert adjustment.booking_date == date(2026, 11, 1)
    assert adjustment.description == "Opening balance adjustment"
    assert adjustment.instrument_id == gbp.id
    assert batch.counts["new"] == 3
    assert batch.warnings == []


async def test_no_adjustment_when_ledger_matches(
    session: AsyncSession, run: Run, stage: Stage, commit: Commit
) -> None:
    profile = await _csv_profile(run)
    await commit((await stage(profile, NOVEMBER)).id)

    december = BALANCE_HEADER + "01/12/2026,Rent,-30.00,1450.00\n"
    rows = await _rows(session, await stage(profile, december))

    assert _statuses(rows) == ["new"]
    assert _adjustments(rows) == []


async def test_gap_creates_adjustment(
    session: AsyncSession, run: Run, stage: Stage, commit: Commit
) -> None:
    profile = await _csv_profile(run)
    await commit((await stage(profile, NOVEMBER)).id)

    january = BALANCE_HEADER + "05/01/2027,Rent,-5.00,1400.00\n"
    (adjustment,) = _adjustments(await _rows(session, await stage(profile, january)))

    assert adjustment.quantity == Decimal("-75.00")  # 1405.00 opening, 1480.00 in the ledger
    assert adjustment.booking_date == date(2027, 1, 4)


async def test_hybrid_account_has_no_adjustment(
    session: AsyncSession, run: Run, stage: Stage, make_account: MakeAccount
) -> None:
    hybrid = await make_account(name="Example Hybrid", balance_mode="hybrid")
    batch = await stage(await _csv_profile(run), NOVEMBER, account_id=hybrid.id)

    rows = await _rows(session, batch)
    assert _statuses(rows) == ["new", "new"]
    assert _adjustments(rows) == []
    assert batch.warnings == []


async def test_no_balances_adds_warning(
    session: AsyncSession, stage: Stage, csv_profile: ImportProfileRecord
) -> None:
    batch = await stage(csv_profile, HEADER + "10/11/2026,Coffee,-3.50\n")

    assert batch.warnings == [NO_BALANCES]
    assert _adjustments(await _rows(session, batch)) == []


async def test_opening_from_ofx_ledgerbal(session: AsyncSession, run: Run, stage: Stage) -> None:
    profile = await _profile(run, format="ofx")
    content = _ofx(
        [("20261110", "-3.50", "EXB1"), ("20261111", "2000.00", "EXB2")],
        start="20261101",
        end="20261130",
        extra="<LEDGERBAL>\n<BALAMT>2496.50\n<DTASOF>20261130\n</LEDGERBAL>",
    )
    batch = await stage(profile, content, filename="statement.ofx")

    (adjustment,) = _adjustments(await _rows(session, batch))
    assert adjustment.quantity == Decimal("500.00")
    assert adjustment.booking_date == date(2026, 11, 9)
    assert batch.warnings == []
    assert batch.statement is not None
    assert batch.statement.closing_balance == Decimal("2496.50")
    assert batch.statement.closing_balance_date == date(2026, 11, 30)
    assert batch.statement.period_from == date(2026, 11, 1)


async def test_multi_currency_chains_separately(
    session: AsyncSession,
    run: Run,
    stage: Stage,
    gbp: InstrumentRecord,
    eur: InstrumentRecord,
) -> None:
    profile = await _csv_profile(run, columns=BALANCE_COLUMNS | {"currency": "Currency"})
    content = (
        "Date,Description,Amount,Balance,Currency\n"
        "02/11/2026,Pay,100.00,600.00,GBP\n"
        "02/11/2026,Cafe,-5.00,195.00,EUR\n"
        "03/11/2026,Shop,-10.00,590.00,GBP\n"
        "04/11/2026,Train,-15.00,180.00,EUR\n"
    )
    rows = await _rows(session, await stage(profile, content, default=None))

    assert "error" not in _statuses(rows)
    adjustments = _adjustments(rows)
    assert [(a.position, a.instrument_id, a.quantity) for a in adjustments] == [
        (-2, gbp.id, Decimal("500.00")),
        (-1, eur.id, Decimal("200.00")),
    ]


# Covered days, typed fields, the statement's account ---------------------------------


async def test_covered_days_match_across_formats(
    session: AsyncSession,
    run: Run,
    stage: Stage,
    commit: Commit,
    csv_profile: ImportProfileRecord,
) -> None:
    november = (
        HEADER
        + "01/11/2026,Rent,-500.00\n20/11/2026,Cafe,-3.50\n25/11/2026,Shop,-12.00\n"
        + "30/11/2026,Pay,2000.00\n"
    )
    await commit((await stage(csv_profile, november, filename="november.csv")).id)

    ofx = await _profile(run, format="ofx")
    content = _ofx(
        [
            ("20261120", "-3.50", "A"),
            ("20261125", "-12.00", "B"),
            ("20261125", "-7.00", "C"),
            ("20261130", "2000.00", "D"),
            ("20261203", "-9.99", "E"),
        ],
        start="20261120",
        end="20261205",
    )
    rows = await _rows(session, await stage(ofx, content, filename="export.ofx"))

    assert _statuses(rows) == ["duplicate", "duplicate", "error", "duplicate", "new"]
    assert rows[0].note == "already imported from november.csv"
    assert rows[2].error is not None and "already imported from another file" in rows[2].error


async def test_duplicate_of_records_the_committed_row(
    session: AsyncSession,
    run: Run,
    stage: Stage,
    commit: Commit,
    csv_profile: ImportProfileRecord,
) -> None:
    november = HEADER + "01/11/2026,Rent,-500.00\n20/11/2026,Cafe,-3.50\n"
    first = await stage(csv_profile, november, filename="november.csv")
    await commit(first.id)
    committed = {row.booking_date: row.id for row in await _rows(session, first)}

    # By key: the same file again.
    again = await _rows(session, await stage(csv_profile, november))
    assert [row.duplicate_of for row in again] == list(committed.values())

    # By covered day: an OFX export of the same days, then committed, relies on the CSV.
    ofx = await _profile(run, format="ofx")
    content = _ofx(
        [("20261120", "-3.50", "A"), ("20261203", "-9.99", "B")], start="20261120", end="20261205"
    )
    export = await stage(ofx, content, filename="export.ofx")
    rows = await _rows(session, export)
    assert _statuses(rows) == ["duplicate", "new"]
    assert rows[0].duplicate_of == committed[date(2026, 11, 20)]
    assert rows[1].duplicate_of is None
    assert await batches_relying_on(session, first.id) == []  # staged, not committed
    await commit(export.id)
    assert await batches_relying_on(session, first.id) == [(export.id, "export.ofx")]
    assert await batches_relying_on(session, export.id) == []


async def test_bank_type_stored(
    session: AsyncSession, run: Run, stage: Stage, make_account: MakeAccount
) -> None:
    profile = await _csv_profile(
        run,
        columns={"date": "Date", "description": ["Description"], "amount": "Amount", "type": "T"},
    )
    rows = await _rows(
        session, await stage(profile, "Date,T,Description,Amount\n10/11/2026,DD,X,-1\n")
    )
    assert rows[0].bank_type == "DD"

    pdf = await _profile(run, format="pdf", layout="hsbc_uk_statement")
    other = await make_account(name="Example HSBC", balance_mode="transactions")
    batch = await stage(
        pdf,
        hsbc_pdf(HsbcStatement(pages=[HSBC_ROWS])),
        account_id=other.id,
        default=None,
        filename="statement.pdf",
        media_type="application/pdf",
    )
    statement_rows = [row for row in await _rows(session, batch) if row.kind == "statement"]
    assert [row.bank_type for row in statement_rows] == ["VIS", "CR", ")))"]


HSBC_ROWS = [
    HsbcRow(
        "VIS", ("EXAMPLE CAFE", "EXAMPLETOWN"), paid_out="4.20", date="08 May 27", balance="95.80"
    ),
    HsbcRow("CR", ("EXAMPLE EMPLOYER",), paid_in="1,250.00", date="10 May 27"),
    HsbcRow(")))", ("EXAMPLE SHOP",), paid_out="17.35", balance="1,328.45"),
]
BOC_LOCAL_OUT = BocRow(
    "03/05/27",
    "04/05/27",
    (
        "/PTG2I02600000001//Local",
        "Clearing And Outward",
        "Remittance Alex Example",
        "401234 12345678 EXAMPLE",
        "BANK PLC Online Banking",
        "rent",
    ),
    paid_out="140.00",
)
BOC_TRANSFER_IN = BocRow(
    "24/05/27",
    "24/05/27",
    ("///Inward Transfer A", "EXAMPLE 51234 Example", "Bank (UK) Limited, Example", "Branch"),
    paid_in="19.45",
)
QIF = b"""!Type:Bank
D10/11/2026
T-3.50
PExample Cafe
MCard purchase
N000101
^
"""
MIDATA = (
    "Date,Type,Merchant/Description,Debit/Credit,Balance\n"
    "2026-11-03,DD,EXAMPLE ENERGY,-£40.00,£960.00\n"
    "2026-11-02,CR,EXAMPLE EMPLOYER,+£1000.00,£1000.00\n"
    "\n"
    "Arranged overdraft limit,2026-11-01,+£100.00\n"
)


async def test_typed_fields_stored(
    session: AsyncSession, run: Run, stage: Stage, make_account: MakeAccount
) -> None:
    async def fresh(name: str) -> UUID:
        return (await make_account(name=name, balance_mode="transactions")).id

    # BOC: value date, counterparty, payment fields; the summary and the account.
    boc = await _profile(run, format="pdf", layout="boc_uk_statement")
    content = boc_pdf(BocStatement(pages=[[BOC_LOCAL_OUT, BOC_TRANSFER_IN]]))
    parsed = parse_pdf(
        content, PdfProfileConfig(format="pdf", layout="boc_uk_statement"), ZoneInfo("UTC")
    )
    batch = await stage(boc, content, account_id=await fresh("Example BOC"), default=None)
    local, transfer = [row for row in await _rows(session, batch) if row.kind == "statement"]
    first = parsed.rows[0]
    assert isinstance(first, ParsedRow) and first.counterparty is not None
    assert local.value_date == date(2027, 5, 4)
    assert local.counterparty_name == first.counterparty.name == "Alex Example"
    assert local.counterparty_sort_code == "401234"
    assert local.counterparty_account_number == "12345678"
    assert local.counterparty_bank == first.counterparty.bank
    assert local.payment_reference == first.counterparty.reference
    assert local.payment_kind == "Local Clearing And Outward Remittance"
    assert local.bank_reference == "PTG2I02600000001"
    assert transfer.counterparty_bank == "Example Bank (UK) Limited, Example Branch"
    statement = batch.statement
    assert statement is not None and parsed.account is not None
    assert (statement.period_from, statement.period_to) == parsed.period
    assert statement.currency == parsed.currency == "GBP"
    assert statement.opening_balance == parsed.opening_balance
    assert statement.closing_balance == parsed.closing_balance
    assert (statement.money_in, statement.money_out) == (parsed.money_in, parsed.money_out)
    assert (statement.payments_in, statement.payments_out) == (1, 1)
    assert statement.sort_code == parsed.account.sort_code == "401234"
    assert statement.account_number == parsed.account.account_number
    assert statement.iban == parsed.account.iban
    assert statement.account_type == parsed.account.account_type
    assert statement.account_name == parsed.account.name
    assert statement.clearing_account_number == parsed.account.clearing_account_number
    assert statement.branch == parsed.account.branch
    assert statement.rates == []

    # QIF: payee, memo, check number; nothing at statement level.
    qif = await _profile(run, format="qif", date_format="%d/%m/%Y")
    batch = await stage(qif, QIF, account_id=await fresh("Example QIF"), filename="s.qif")
    (row,) = await _rows(session, batch)
    assert (row.payee, row.memo, row.check_number) == ("Example Cafe", "Card purchase", "000101")
    assert batch.statement is None

    # HSBC: the rate table.
    hsbc = await _profile(run, format="pdf", layout="hsbc_uk_statement")
    content = hsbc_pdf(HsbcStatement(pages=[HSBC_ROWS], credit_rate=True))
    parsed = parse_pdf(
        content, PdfProfileConfig(format="pdf", layout="hsbc_uk_statement"), ZoneInfo("UTC")
    )
    batch = await stage(hsbc, content, account_id=await fresh("Example HSBC"), default=None)
    assert batch.statement is not None
    assert parsed.rates and batch.statement.rates == parsed.rates
    assert batch.statement.statement_reference == parsed.statement_reference

    # midata: the overdraft limit.
    midata = await _csv_profile(
        run,
        columns={
            "date": "Date",
            "description": ["Merchant/Description"],
            "amount": "Debit/Credit",
            "balance": "Balance",
            "type": "Type",
        },
        date_format="%Y-%m-%d",
        skip_footer_rows=1,
        row_order="newest_first",
        overdraft_footer_label="Arranged overdraft limit",
    )
    batch = await stage(midata, MIDATA, account_id=await fresh("Example midata"))
    assert batch.statement is not None
    assert batch.statement.overdraft_limit == Decimal("100.00")
    assert batch.statement.overdraft_limit_date == date(2026, 11, 1)
    assert "error" not in _statuses(await _rows(session, batch))


async def test_statement_account_mismatch_refused(
    run: Run, stage: Stage, commit: Commit, gbp: InstrumentRecord
) -> None:
    profile = await _csv_profile(
        run,
        columns={
            "date": "Date",
            "description": ["Description"],
            "amount": "Amount",
            "account_number": "Account Number",
        },
    )
    header = "Date,Description,Amount,Account Number\n"
    await commit((await stage(profile, header + "10/11/2026,Coffee,-3.50,11111234\n")).id)
    # The same account again is fine.
    await stage(profile, header + "11/11/2026,Tea,-2.50,11111234\n")

    with pytest.raises(ImportNotAllowedError) as raised:
        await stage(profile, header + "12/11/2026,Cake,-4.00,22225678\n")
    assert sorted(re.findall(r"[0-9]+", str(raised.value))) == ["1234", "5678"]
