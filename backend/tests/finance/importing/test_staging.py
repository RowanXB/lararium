import importlib.util
import re
import threading
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core import models as core_models
from lararium.modules.finance import models
from lararium.modules.finance.accounts import AccountRecord, close_account
from lararium.modules.finance.errors import (
    FileFormatError,
    ImportBatchNotFoundError,
    ImportNotAllowedError,
    NotACurrencyError,
)
from lararium.modules.finance.importing import staging
from lararium.modules.finance.importing.parsed import ParsedRow, ParsedStatement
from lararium.modules.finance.importing.pdf import parse_pdf
from lararium.modules.finance.importing.profiles import (
    ImportProfileRecord,
    PdfProfileConfig,
    update_profile,
)
from lararium.modules.finance.importing.staging import (
    batches_relying_on,
    get_batch,
    list_batches,
)
from lararium.modules.finance.instruments import InstrumentRecord

from .staging_helpers import (
    BALANCE_COLUMNS,
    BALANCE_HEADER,
    DECEMBER,
    HEADER,
    HSBC_ROWS,
    NO_BALANCES,
    NOVEMBER,
    Commit,
    MakeAccount,
    Run,
    Stage,
    _adjustments,
    _balance_after,
    _csv_profile,
    _dedupe_keys,
    _files,
    _ofx,
    _profile,
    _rows,
    _statuses,
)
from .synthetic_pdfs import (
    BocRow,
    BocStatement,
    HsbcStatement,
    LemfiRow,
    LemfiStatement,
    TemboRow,
    TemboStatement,
    boc_pdf,
    hsbc_pdf,
    lemfi_pdf,
    tembo_pdf,
)

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


async def test_parsing_runs_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, stage: Stage, csv_profile: ImportProfileRecord
) -> None:
    """A long PDF takes seconds to parse: the other requests the process serves meanwhile
    mustn't wait on it."""
    loop_thread = threading.get_ident()
    parsed_in: list[int] = []
    parse = staging._parse

    def recording(*args: Any) -> Any:
        parsed_in.append(threading.get_ident())
        return parse(*args)

    monkeypatch.setattr(staging, "_parse", recording)
    await stage(csv_profile, HEADER + NOVEMBER)
    assert parsed_in and parsed_in[0] != loop_thread


def test_statements_are_parsed_one_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each parse holds a whole statement in memory, under a cap the api's requests share:
    uploads arriving together parse in turn."""
    guard = threading.Lock()
    active = most = 0

    def slow(*_: Any) -> None:
        nonlocal active, most
        with guard:
            active += 1
            most = max(most, active)
        time.sleep(0.05)
        with guard:
            active -= 1

    monkeypatch.setattr(staging, "_parse", slow)
    uploads = [
        threading.Thread(target=staging._parse_alone, args=(b"", None, None)) for _ in range(4)
    ]
    for upload in uploads:
        upload.start()
    for upload in uploads:
        upload.join()
    assert most == 1


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

    # A repeated running-balance row is a row of its own (owner ruling 2026-11-19): the
    # balance can come back to the same figure on the same day.
    repeated = (
        BALANCE_HEADER + "02/11/2026,In,10.00,100.00\n02/11/2026,Shop,-10.00,90.00\n"
        "02/11/2026,In,10.00,100.00\n"
    )
    staged = await stage(await _csv_profile(run), repeated)
    rows = [row for row in await _rows(session, staged) if row.kind == "statement"]
    assert _statuses(rows) == ["new", "new", "new"]
    # Ordered by position, the opening adjustment (position -1) first.
    assert len(set((await _dedupe_keys(session, staged))[-3:])) == 3


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
    session: AsyncSession, stage: Stage, csv_profile: ImportProfileRecord, tmp_path: Path
) -> None:
    with pytest.raises(FileFormatError):
        await stage(csv_profile, "Foo,Bar\n1,2\n")

    assert await session.scalar(select(func.count()).select_from(models.ImportBatch)) == 0
    assert await session.scalar(select(func.count()).select_from(core_models.Document)) == 0
    # Review finding: the refused statement's bytes stayed on disk, and in every backup.
    assert _files(tmp_path) == []


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
    assert adjustment.booking_date == date(
        2026, 11, 2
    )  # an export's first day is partial: dated on it
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
    assert adjustment.booking_date == date(
        2027, 1, 5
    )  # an export's first day is partial: dated on it


async def test_older_statement_after_newer_realigns(
    session: AsyncSession,
    run: Run,
    stage: Stage,
    commit: Commit,
    account: AccountRecord,
    make_account: MakeAccount,
) -> None:
    # December first (its adjustment: 1480.00 on 30 Nov), then November.
    profile = await _csv_profile(run)
    december = await stage(profile, DECEMBER, filename="december.csv")
    await commit(december.id)
    november = await stage(profile, NOVEMBER, filename="november.csv")

    (adjustment,) = _adjustments(await _rows(session, november))
    assert adjustment.quantity == Decimal("1500.00")
    assert november.warnings == [
        "committing this changes the opening adjustment of december.csv from 1480.00 to 0.00"
    ]
    await commit(november.id)
    for day, balance in [("2026-11-02", "1482.00"), ("2026-11-03", "1480.00")]:
        assert await _balance_after(session, account.id, date.fromisoformat(day)) == Decimal(
            balance
        )
    assert await _balance_after(session, account.id, date(2026, 12, 1)) == Decimal("1450.00")
    (superseded,) = _adjustments(await _rows(session, december))
    assert (superseded.status, superseded.note) == ("rolled_back", "superseded")

    # An OFX export for 10 May-15 Jun first, then the PDF statement for 6 May-5 Jun.
    other = await make_account(name="Example HSBC", balance_mode="transactions")
    ofx = await _profile(run, format="ofx")
    export = _ofx(
        [("20270510", "1250.00", "A"), ("20270510", "-17.35", "B"), ("20270612", "-8.00", "C")],
        start="20270510",
        end="20270615",
        extra="<LEDGERBAL>\n<BALAMT>1320.45\n<DTASOF>20270615\n</LEDGERBAL>",
    )
    await commit((await stage(ofx, export, account_id=other.id, filename="export.ofx")).id)
    pdf = await _profile(run, format="pdf", layout="hsbc_uk_statement")
    statement = await stage(
        pdf,
        hsbc_pdf(HsbcStatement(pages=[HSBC_ROWS])),
        account_id=other.id,
        default=None,
        filename="statement.pdf",
    )

    rows = await _rows(session, statement)
    assert _statuses(rows) == ["new", "new", "duplicate", "duplicate"]
    assert rows[0].quantity == Decimal("100.00")
    assert statement.warnings == [
        "committing this changes the opening adjustment of export.ofx from 95.80 to 0.00"
    ]
    await commit(statement.id)
    for day, balance in [("2027-05-08", "95.80"), ("2027-05-10", "1328.45")]:
        assert await _balance_after(session, other.id, date.fromisoformat(day)) == Decimal(balance)
    assert await _balance_after(session, other.id, date(2027, 6, 15)) == Decimal("1320.45")


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
    # It lists 2-29 Nov in full, so its opening is the balance as 2 Nov starts.
    assert adjustment.booking_date == date(2026, 11, 1)
    assert batch.warnings == []
    assert batch.statement is not None
    assert batch.statement.closing_balance == Decimal("2496.50")
    assert batch.statement.closing_balance_date == date(2026, 11, 30)
    assert batch.statement.period_from == date(2026, 11, 1)

    # A LEDGERBAL dated before the last transaction can't be its closing balance.
    early = content.replace("<DTASOF>20261130", "<DTASOF>20261110")
    batch = await stage(profile, early, filename="early.ofx")
    assert _adjustments(await _rows(session, batch)) == []
    assert batch.warnings == [NO_BALANCES]


async def test_ofx_rows_keep_the_files_currency(
    session: AsyncSession, run: Run, stage: Stage, eur: InstrumentRecord
) -> None:
    profile = await _profile(run, format="ofx")
    content = _ofx(
        [("20261110", "-3.50", "EXB1"), ("20261111", "20.00", "EXB2")],
        start="20261101",
        end="20261130",
        extra="<LEDGERBAL>\n<BALAMT>116.50\n<DTASOF>20261130\n</LEDGERBAL>",
        currency="EUR",
    )
    # A picked default (GBP) doesn't override the file's CURDEF, nor does the LEDGERBAL route.
    rows = await _rows(session, await stage(profile, content, filename="eur.ofx"))

    assert {row.instrument_id for row in rows} == {eur.id}
    (adjustment,) = _adjustments(rows)
    assert adjustment.quantity == Decimal("100.00")


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


async def test_large_amounts_keep_precision(
    session: AsyncSession,
    run: Run,
    stage: Stage,
    make_account: MakeAccount,
    csv_profile: ImportProfileRecord,
) -> None:
    # 34 and 37 significant digits: far past the default 28-digit context.
    big = "1000000000000000000000000000000000.01"
    content = (
        BALANCE_HEADER
        + f"01/11/2026,Big,{big},2000000000000000000000000000000000.02\n"
        + "02/11/2026,Small,0.01,2000000000000000000000000000000000.03\n"
    )
    rows = await _rows(session, await stage(await _csv_profile(run), content))
    assert _statuses(rows) == ["new", "new", "new"]  # the chain holds
    assert rows[0].kind == "opening_adjustment"
    assert rows[0].quantity == Decimal(big)

    # Keys tell apart amounts that differ only in the last digit.
    other = await make_account(name="Example Big", balance_mode="hybrid")
    row = "10/11/2026,Transfer,1234567890123456789012345678901.11\n"
    keys = [
        await _dedupe_keys(session, await stage(csv_profile, HEADER + text, account_id=other.id))
        for text in (row, row, row.replace(".11", ".12"))
    ]
    assert keys[0] == keys[1] != keys[2]


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


async def test_payment_start_card_and_foreign_amount_stored(
    session: AsyncSession, run: Run, stage: Stage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revolut's typed fields: a CSV's start time, a PDF's card and foreign amount."""
    london = ZoneInfo("Europe/London")
    profile = await _csv_profile(
        run,
        columns={
            "date": "Completed Date",
            "started_at": "Started Date",
            "description": ["Description"],
            "amount": "Amount",
            "account_type": "Product",
        },
        date_format="%Y-%m-%d %H:%M:%S",
        timezone="Europe/London",
    )
    content = (
        "Product,Started Date,Completed Date,Description,Amount\n"
        "Current,2026-07-01 23:30:00,2026-07-02 09:15:00,Shop,-1.00\n"
    )
    batch = await stage(profile, content)
    (row,) = await _rows(session, batch)
    assert row.started_at == datetime(2026, 7, 1, 23, 30, tzinfo=london)
    assert row.occurred_at == datetime(2026, 7, 2, 9, 15, tzinfo=london)
    assert (row.card_number, row.original_amount, row.fx_rate) == (None, None, None)
    assert batch.statement is not None and batch.statement.account_type == "Current"

    paid = ParsedRow(
        position=0,
        booking_date=date(2026, 7, 3),
        occurred_at=None,
        description="Example Shop",
        quantity=Decimal("-75.00"),
        fee=Decimal(0),
        currency=None,
        balance=None,
        external_id=None,
        bank_type=None,
        raw={"Description": "Example Shop"},
        card_number="123456******1234",
        original_currency="USD",
        original_amount=Decimal("-100.00"),
        fx_rate=Decimal("1.33"),
        fx_reference_rate=Decimal("1.34"),
    )

    def parse(*_: object) -> tuple[ParsedStatement, str]:
        return ParsedStatement(rows=[paid]), "test"

    monkeypatch.setattr(staging, "_parse_alone", parse)
    batch = await stage(profile, b"ignored", filename="other.csv")
    (row,) = await _rows(session, batch)
    assert row.card_number == "123456******1234"
    assert (row.original_currency, row.original_amount) == ("USD", Decimal("-100.00"))
    assert (row.fx_rate, row.fx_reference_rate) == (Decimal("1.33"), Decimal("1.34"))
    assert row.started_at is None


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

    # LemFi: the bank holding the account, a masked counterparty account; the savings
    # statement's interest and its rate's validity.
    lemfi = await _profile(run, format="pdf", layout="lemfi_statement")
    received = LemfiRow(
        "03 May, 2027", ("Received from A Example",), ("Via EXAMPLE BANK *1234",), paid_in="9.00"
    )
    content = lemfi_pdf(LemfiStatement([[received]]))
    batch = await stage(lemfi, content, account_id=await fresh("Example LemFi"), default=None)
    (row,) = await _rows(session, batch)
    assert (row.counterparty_bank, row.counterparty_account_ending) == ("EXAMPLE BANK", "1234")
    assert row.counterparty_account_number is None
    assert row.occurred_at == datetime(2027, 5, 3, 12, 28, tzinfo=UTC)  # 13:28 in London
    assert batch.statement is not None
    assert (batch.statement.bank, batch.statement.iban) == (
        "Example Bank",
        "GB99EXAM40123412345678",
    )
    assert batch.statement.interest is None
    payout = LemfiRow("01 May, 2027", ("Savings Interest Payout",), paid_in="80.12")
    content = lemfi_pdf(LemfiStatement([[payout]], savings=True, opening="100.00"))
    parsed = parse_pdf(
        content, PdfProfileConfig(format="pdf", layout="lemfi_statement"), ZoneInfo("UTC")
    )
    batch = await stage(lemfi, content, account_id=await fresh("Example saver"), default=None)
    assert batch.statement is not None
    assert batch.statement.interest == Decimal("80.12")
    assert batch.statement.money_in == Decimal("0.00")
    assert parsed.rates and batch.statement.rates == parsed.rates
    assert batch.statement.rates[0].valid_to == date(2027, 10, 29)
    assert batch.statement.rates[0].frequency == "monthly"
    assert batch.statement.account_name == "Example Instant Saver"

    # Tembo: an account identifier that isn't a number, the open date, the available
    # balance; no opening balance or totals.
    tembo = await _profile(run, format="pdf", layout="tembo_statement")
    content = tembo_pdf(TemboStatement(pages=[[TemboRow("03/05/2027", "Deposit", "£50.00")]]))
    batch = await stage(tembo, content, account_id=await fresh("Example Tembo"), default=None)
    statement = batch.statement
    assert statement is not None
    assert statement.account_reference == "ACC-EX01AB-CD23EF"
    assert statement.account_number is None
    assert statement.account_opened_on == date(2027, 2, 3)
    assert statement.account_type == "Regular Saver"
    assert (statement.available_balance, statement.available_balance_date) == (
        Decimal("50.00"),
        date(2027, 6, 1),
    )
    assert (statement.opening_balance, statement.money_in, statement.money_out) == (
        None,
        None,
        None,
    )
    (row,) = [row for row in await _rows(session, batch) if row.kind == "statement"]
    assert (row.bank_type, row.quantity) == ("Deposit", Decimal("50.00"))


async def test_rows_keep_what_their_file_gave_the_time_to(
    session: AsyncSession, run: Run, stage: Stage, commit: Commit, make_account: MakeAccount
) -> None:
    """Owner, 2026-11-21: a row's time_unit tells a time its file gave (to the minute or
    the second) from a date-only row's midnight; 0024 gives rows imported before the same
    unit staging gives them now."""
    columns = {"date": "Date", "description": ["Description"], "amount": "Amount"}
    header = "Date,Description,Amount\n"
    seconds = await _csv_profile(run, columns=columns, date_format="%d/%m/%Y %H:%M:%S")
    minutes = await _csv_profile(run, columns=columns, date_format="%d/%m/%Y %H:%M")
    days = await _csv_profile(run, columns=columns)
    ofx = await _profile(run, format="ofx")
    lemfi = await _profile(run, format="pdf", layout="lemfi_statement")
    lemfi_account = (await make_account(name="Example LemFi", balance_mode="transactions")).id
    received = LemfiRow("03 May, 2027", ("Received from A Example",), paid_in="9.00")
    files: dict[str, tuple[ImportProfileRecord, str | bytes, UUID | None]] = {
        "second": (seconds, header + "10/11/2026 00:00:00,A,1.00\n", None),
        "minute": (minutes, header + "11/11/2026 00:00,B,1.00\n", None),
        "day": (days, header + "12/11/2026,C,1.00\n", None),
        "ofx second": (
            ofx,
            _ofx([("20261113090000", "1.00", "X")], start="20261113", end="20261113"),
            None,
        ),
        "ofx day": (ofx, _ofx([("20261114", "1.00", "Y")], start="20261114", end="20261114"), None),
        "lemfi minute": (lemfi, lemfi_pdf(LemfiStatement([[received]])), lemfi_account),
        # A balance gives the file an opening adjustment, which has only its day.
        "balances": (await _csv_profile(run), BALANCE_HEADER + "20/11/2026,D,1.00,51.00\n", None),
    }
    units: dict[str, list[tuple[str, str | None]]] = {}
    for name, (profile, content, account_id) in files.items():
        default = None if account_id is not None else "gbp"
        batch = await stage(profile, content, account_id=account_id, default=default)
        units[name] = [(row.kind, row.time_unit) for row in await _rows(session, batch)]
        await commit(batch.id)
    assert units == {
        "second": [("statement", "second")],
        "minute": [("statement", "minute")],
        "day": [("statement", "day")],
        "ofx second": [("statement", "second")],
        "ofx day": [("statement", "day")],
        "lemfi minute": [("statement", "minute")],
        "balances": [("opening_adjustment", "day"), ("statement", "day")],
    }

    r = models.ImportRow
    units_by_id = select(r.id, r.time_unit)
    staged = {row_id: unit for row_id, unit in await session.execute(units_by_id)}
    migration = _migration("0024_finance_import_row_time_unit")
    # As 0024 finds them, rows have no unit; "day" stands in for none (the CHECK allows no
    # NULL beside a time), and only the backfill gives the others back.
    forgotten = "UPDATE finance.import_rows SET time_unit = 'day' WHERE occurred_at IS NOT NULL"
    await session.execute(text(forgotten))
    await session.execute(text(migration.BACKFILL))
    session.expire_all()
    assert {row_id: unit for row_id, unit in await session.execute(units_by_id)} == staged


def _migration(name: str) -> Any:
    path = Path(__file__).parents[3] / "migrations" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_rows_told_apart_by_their_time(run: Run, stage: Stage, commit: Commit) -> None:
    """Money in and straight back out twice a day leaves the same amount, description and
    balance: the time of day the file gives keeps the rows apart, in the file and again."""
    profile = await _csv_profile(
        run,
        columns={
            "date": "Date",
            "description": ["Description"],
            "amount": "Amount",
            "balance": "Balance",
        },
        date_format="%d/%m/%Y %H:%M",
    )
    text = (
        "Date,Description,Amount,Balance\n"
        "10/11/2026 09:00,Top up,5.00,5.00\n"
        "10/11/2026 09:01,Sent on,-5.00,0.00\n"
        "10/11/2026 17:00,Top up,5.00,5.00\n"
        "10/11/2026 17:01,Sent on,-5.00,0.00\n"
    )
    batch = await stage(profile, text)
    assert batch.counts["new"] == 4 and batch.counts["error"] == 0
    await commit(batch.id)
    again = await stage(profile, text)
    assert again.counts["duplicate"] == 4
    same_minute = text.replace("17:00", "09:00").replace("17:01", "09:01")
    repeated = await stage(profile, same_minute)
    assert repeated.counts["error"] == 2


async def test_a_row_with_an_id_never_times_another_rows_key(
    session: AsyncSession, run: Run, stage: Stage, commit: Commit
) -> None:
    """Review finding: a row keyed by its id counted as a repeat of a row without one, so that
    row took the time into its key, missed its committed self, and was committed again."""
    profile = await _csv_profile(
        run,
        columns={
            "date": "Date",
            "description": ["Description"],
            "amount": "Amount",
            "balance": "Balance",
            "external_id": "Id",
        },
        date_format="%d/%m/%Y %H:%M",
    )
    header = "Date,Description,Amount,Balance,Id\n"
    evening = "10/11/2026 17:00,Top up,5.00,5.00,\n"
    first = await stage(profile, header + evening)
    await commit(first.id)
    morning = "10/11/2026 09:00,Top up,5.00,5.00,X\n10/11/2026 09:01,Sent on,-5.00,0.00,\n"
    full = await stage(profile, header + morning + evening)
    assert _statuses(await _rows(session, full)) == ["new", "new", "duplicate"]


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


async def test_covered_range_includes_rows_outside_the_period(
    session: AsyncSession, run: Run, stage: Stage
) -> None:
    """A file covers its stated period and every day it has a row on (OFX's DTSTART/DTEND
    don't bound its rows the way a PDF's period does). An export may be taken partway
    through a day, so it lists in full only the days between its first and last."""
    profile = await _profile(run, format="ofx")
    content = _ofx(
        [("20261120", "-3.50", "A"), ("20261127", "-1.00", "B"), ("20261203", "-2.00", "C")],
        start="20261125",
        end="20261130",
    )
    batch = await stage(profile, content, filename="statement.ofx")

    b = models.ImportBatch
    covered = (
        await session.execute(
            select(b.covered_from, b.covered_to, b.complete_from, b.complete_to).where(
                b.id == batch.id
            )
        )
    ).one()
    assert tuple(covered) == (
        date(2026, 11, 20),
        date(2026, 12, 3),
        date(2026, 11, 21),
        date(2026, 12, 2),
    )


async def test_filtered_export_lists_no_day_in_full(
    session: AsyncSession, run: Run, stage: Stage
) -> None:
    profile = await _csv_profile(
        run,
        columns={"date": "Date", "description": ["Description"], "amount": "Amount"},
        include_only={"State": ["COMPLETED"]},
    )
    content = (
        "Date,Description,Amount,State\n01/11/2026,Pay,100.00,COMPLETED\n"
        "03/11/2026,Hotel,-20.00,PENDING\n05/11/2026,Shop,-1.00,COMPLETED\n"
    )
    batch = await stage(profile, content)

    b = models.ImportBatch
    covered = (
        await session.execute(
            select(b.covered_from, b.covered_to, b.complete_from, b.complete_to).where(
                b.id == batch.id
            )
        )
    ).one()
    assert tuple(covered) == (date(2026, 11, 1), date(2026, 11, 5), None, None)


@pytest.mark.parametrize(
    ("made", "complete"),
    [
        (None, (date(2026, 10, 1), date(2026, 11, 12))),
        (date(2026, 11, 13), (date(2026, 10, 1), date(2026, 11, 12))),
        # Made on its last day (Revolut): that day may get more rows later.
        (date(2026, 11, 12), (date(2026, 10, 1), date(2026, 11, 11))),
        (date(2026, 11, 1), (date(2026, 10, 1), date(2026, 10, 31))),
        (date(2026, 10, 1), (None, None)),
    ],
)
def test_statement_lists_in_full_only_the_days_before_it_was_made(
    made: date | None, complete: tuple[date | None, date | None]
) -> None:
    statement = ParsedStatement(
        rows=[],
        period=(date(2026, 10, 1), date(2026, 11, 12)),
        period_complete=True,
        document_date=made,
    )
    coverage = staging._covered(statement, [])  # pyright: ignore[reportPrivateUsage]
    assert coverage.covered == (date(2026, 10, 1), date(2026, 11, 12))
    assert coverage.complete == complete


async def test_duplicate_that_breaks_the_balance_chain_is_an_error(
    session: AsyncSession, run: Run, stage: Stage, commit: Commit
) -> None:
    """Review finding: a row matched by covered day, then failed by the balance chain, kept
    its duplicate_of, which import_rows' CHECK refuses."""
    ofx = await _profile(run, format="ofx")
    days = [("20261101", "1.00", "A"), ("20261109", "100.00", "B"), ("20261110", "-3.50", "C")]
    days += [("20261130", "1.00", "D")]
    await commit((await stage(ofx, _ofx(days, start="20261101", end="20261130"))).id)

    csv = await _csv_profile(run)
    content = BALANCE_HEADER + "09/11/2026,Pay,100.00,100.00\n10/11/2026,Cafe,-3.50,90.00\n"
    rows = await _rows(session, await stage(csv, content))
    assert _statuses(rows) == ["duplicate", "error"]
    assert rows[1].duplicate_of is None and rows[1].note is None

    # Second review: on a day this file lists in full (10 Nov, between 9 and 30 Nov), the
    # failed row was taken for one the file doesn't list, refusing the whole file.
    content += "30/11/2026,Refund,1.00,91.00\n"
    rows = await _rows(session, await stage(csv, content))
    assert _statuses(rows) == ["duplicate", "error", "duplicate"]


async def test_each_currency_speaks_for_its_own_days(
    session: AsyncSession,
    run: Run,
    stage: Stage,
    commit: Commit,
    csv_profile: ImportProfileRecord,
    eur: InstrumentRecord,
) -> None:
    """Second review: the days a file lists in full were the account's, whatever the
    currency, so a multi-currency account's per-currency statements refused each other."""
    month = HEADER + "01/11/2026,Pay,100.00\n10/11/2026,Cafe,-3.50\n30/11/2026,Shop,-1.00\n"
    await commit((await stage(csv_profile, month, default=eur.id, filename="eur.csv")).id)

    gbp_month = HEADER + "01/11/2026,Pay,50.00\n12/11/2026,Bus,-2.00\n30/11/2026,Tea,-1.50\n"
    rows = await _rows(session, await stage(csv_profile, gbp_month, filename="gbp.csv"))
    assert _statuses(rows) == ["new", "new", "new"]
    assert [row.note for row in rows] == [None, None, None]


async def test_nul_character_refuses_the_file_naming_its_row(
    stage: Stage, csv_profile: ImportProfileRecord
) -> None:
    """Third review: a NUL reached the insert, which failed with a message naming nothing."""
    content = HEADER + "10/11/2026,Coffee,-3.50\n11/11/2026,Example\x00Shop,-1.00\n"
    with pytest.raises(FileFormatError, match="row 2 has a NUL character"):
        await stage(csv_profile, content)


async def test_same_id_with_another_amount_is_an_error(
    session: AsyncSession, run: Run, stage: Stage, commit: Commit
) -> None:
    """Fourth review: a row matched by external id became a duplicate whatever its amount,
    so a settled amount that differed from the pending one left the ledger 10.00 out."""
    ofx = await _profile(run, format="ofx")
    first = _ofx([("20261105", "-50.00", "T1")], start="20261101", end="20261130")
    await commit((await stage(ofx, first, filename="a.ofx")).id)
    again = _ofx([("20261105", "-60.00", "T1")], start="20261101", end="20261130")
    (row,) = await _rows(session, await stage(ofx, again, filename="b.ofx"))
    assert row.status == "error" and row.error is not None
    assert "T1" in row.error and "a.ofx" in row.error and "-60.00" in row.error


async def test_only_assets_and_liabilities_take_imports(
    stage: Stage, csv_profile: ImportProfileRecord, make_account: MakeAccount
) -> None:
    """Fourth review: the system 'Opening balances' account (equity) could be chosen, so its
    adjustment posted to itself and the walk counted money that moved nowhere."""
    equity = await make_account(
        name="Example Equity", ledger_type="equity", balance_mode="transactions"
    )
    with pytest.raises(ImportNotAllowedError, match="equity"):
        await stage(csv_profile, HEADER + "10/11/2026,Coffee,-3.50\n", account_id=equity.id)


async def test_a_time_leaves_the_key_of_a_row_nothing_else_is_like(
    session: AsyncSession, run: Run, stage: Stage, commit: Commit
) -> None:
    """Review finding: a row's time went into its key, so a row committed before 0022 kept
    another key; imported again, it was paired by date and amount alone, with another row."""
    columns = {"date": "Date", "description": ["Description"], "amount": "Amount"}
    header = "Date,Description,Amount\n"
    dated = await _csv_profile(run, columns=columns)
    timed = await _csv_profile(run, columns=columns, date_format="%d/%m/%Y %H:%M")
    old = await stage(dated, header + "10/11/2026,A,10.00\n10/11/2026,B,10.00\n")
    await commit(old.id)
    b = (await _rows(session, old))[1]
    again = await stage(timed, header + "10/11/2026 09:00,B,10.00\n")
    [row] = await _rows(session, again)
    assert (row.status, row.duplicate_of) == ("duplicate", b.id)
