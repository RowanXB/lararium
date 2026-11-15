from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core import models as core_models
from lararium.core.commands import CommandContext
from lararium.core.settings_store import get_setting
from lararium.modules.finance import models
from lararium.modules.finance.accounts import AccountRecord
from lararium.modules.finance.errors import (
    ImportBatchStaleError,
    ImportBatchStateError,
    ImportHasErrorsError,
    ImportNotAllowedError,
)
from lararium.modules.finance.importing.commit import discard_batch
from lararium.modules.finance.importing.profiles import ImportProfileRecord
from lararium.modules.finance.importing.staging import get_batch
from lararium.modules.finance.instruments import InstrumentRecord
from lararium.modules.finance.ledger import find_unbalanced_transactions, void_transaction
from lararium.modules.finance.valuation import balances_asof, net_worth_series

from .staging_helpers import (
    BALANCE_COLUMNS,
    BALANCE_HEADER,
    DECEMBER,
    HEADER,
    HSBC_ROWS,
    NOVEMBER,
    Commit,
    MakeAccount,
    Rollback,
    Run,
    Stage,
    _adjustments,
    _balance_after,
    _csv_profile,
    _files,
    _ofx,
    _profile,
    _rows,
    _statuses,
)
from .synthetic_pdfs import HsbcRow, HsbcStatement, hsbc_pdf

END = date(2027, 12, 31)
TWO_ROWS = HEADER + "10/11/2026,Coffee,-3.50\n11/11/2026,Refund,100.00\n"


async def _balance(session: AsyncSession, account_id: UUID) -> Decimal:
    rows = await balances_asof(session, END)
    return sum((row.quantity for row in rows if row.account_id == account_id), Decimal(0))


async def _net_worth(session: AsyncSession) -> Decimal:
    (point,) = await net_worth_series(session, END, END)
    return point.value


async def _system_account(session: AsyncSession, key: str) -> UUID:
    value = await get_setting(session, f"finance.system_account.{key}")
    assert isinstance(value, str)
    return UUID(value)


async def _transaction_count(session: AsyncSession) -> int:
    count = await session.scalar(select(func.count()).select_from(models.Transaction))
    assert count is not None
    return count


async def _postings(session: AsyncSession, transaction_id: UUID | None) -> list[models.Posting]:
    p = models.Posting
    rows = await session.scalars(
        select(p).where(p.transaction_id == transaction_id).order_by(p.position)
    )
    return list(rows)


async def _switch_mode(ctx: CommandContext, account_id: UUID, mode: str) -> None:
    a = models.Account
    await ctx.session.execute(update(a).where(a.id == account_id).values(balance_mode=mode))


# Commit -------------------------------------------------------------------------------


async def test_commit_posts_balanced_transactions(
    session: AsyncSession,
    stage: Stage,
    commit: Commit,
    account: AccountRecord,
    csv_profile: ImportProfileRecord,
) -> None:
    before = await _net_worth(session)
    batch = await commit((await stage(csv_profile, TWO_ROWS)).id)

    assert batch.status == "committed" and batch.committed_at is not None
    assert batch.counts["committed"] == 2
    assert await _balance(session, account.id) == Decimal("96.50")
    unassigned = await _system_account(session, "unassigned")
    assert await _balance(session, unassigned) == Decimal("-96.50")
    assert await _net_worth(session) - before == Decimal("96.50")
    assert await find_unbalanced_transactions(session) == []
    rows = await _rows(session, batch)
    assert all(row.status == "committed" and row.transaction_id for row in rows)
    event = await session.scalar(
        select(core_models.Event).where(core_models.Event.type == "finance.import.committed")
    )
    assert event is not None and event.data["batch_id"] == str(batch.id)


async def test_reimporting_same_file_marks_all_duplicate(
    session: AsyncSession, stage: Stage, commit: Commit, csv_profile: ImportProfileRecord
) -> None:
    await commit((await stage(csv_profile, TWO_ROWS)).id)
    again = await stage(csv_profile, TWO_ROWS)
    assert _statuses(await _rows(session, again)) == ["duplicate", "duplicate"]

    count = await _transaction_count(session)
    committed = await commit(again.id)
    assert committed.counts["duplicate"] == 2 and committed.counts["committed"] == 0
    assert await _transaction_count(session) == count


async def test_overlapping_export_commits_only_new_rows(
    session: AsyncSession,
    stage: Stage,
    commit: Commit,
    account: AccountRecord,
    csv_profile: ImportProfileRecord,
) -> None:
    first = HEADER + "01/11/2026,Rent,-500.00\n05/11/2026,Cafe,-3.50\n10/11/2026,Shop,-12.00\n"
    await commit((await stage(csv_profile, first)).id)
    later = HEADER + (
        "05/11/2026,Cafe,-3.50\n10/11/2026,Shop,-12.00\n12/11/2026,Train,-8.00\n"
        "15/11/2026,Pay,2000.00\n"
    )
    batch = await stage(csv_profile, later)
    assert _statuses(await _rows(session, batch)) == ["duplicate", "duplicate", "new", "new"]

    await commit(batch.id)
    assert await _balance(session, account.id) == Decimal("1476.50")


async def test_commit_rechecks_duplicates(
    session: AsyncSession,
    stage: Stage,
    commit: Commit,
    account: AccountRecord,
    csv_profile: ImportProfileRecord,
) -> None:
    a = await stage(csv_profile, TWO_ROWS)
    b = await stage(csv_profile, TWO_ROWS)
    await commit(a.id)
    committed = await commit(b.id)

    rows = await _rows(session, committed)
    assert _statuses(rows) == ["duplicate", "duplicate"]
    assert all(row.duplicate_of is not None for row in rows)
    assert await _balance(session, account.id) == Decimal("96.50")


async def test_commit_rechecks_covered_days(
    session: AsyncSession, run: Run, stage: Stage, commit: Commit, csv_profile: ImportProfileRecord
) -> None:
    # Both staged before either is committed: the OFX's 20 Nov row is only matched at commit,
    # and its extra row on a day the CSV now lists in full makes the batch stale.
    rows = "19/11/2026,Shop,-1.00\n20/11/2026,Cafe,-3.50\n21/11/2026,Shop,-2.00\n"
    csv = await stage(csv_profile, HEADER + rows, filename="nov.csv")
    ofx = await _profile(run, format="ofx")
    matched = await stage(
        ofx,
        _ofx([("20261120", "-3.50", "A")], start="20261120", end="20261120"),
        filename="a.ofx",
    )
    extra = await stage(
        ofx,
        _ofx(
            [("20261120", "-3.50", "B"), ("20261120", "-1.00", "C")],
            start="20261120",
            end="20261120",
        ),
        filename="b.ofx",
    )
    await commit(csv.id)

    committed = await commit(matched.id)
    (row,) = await _rows(session, committed)
    assert (row.status, row.note) == ("duplicate", "already imported from nov.csv")
    with pytest.raises(ImportBatchStaleError):
        await commit(extra.id)
    assert (await get_batch(session, extra.id)).status == "staged"


async def test_errors_block_commit(
    session: AsyncSession, stage: Stage, commit: Commit, csv_profile: ImportProfileRecord
) -> None:
    batch = await stage(csv_profile, HEADER + "10/11/2026,Coffee,-3.50\n32/11/2026,Bad,-1\n")

    with pytest.raises(ImportHasErrorsError):
        await commit(batch.id)
    assert (await get_batch(session, batch.id)).status == "staged"


async def test_commit_twice_refused(
    stage: Stage, commit: Commit, csv_profile: ImportProfileRecord
) -> None:
    batch = await stage(csv_profile, TWO_ROWS)
    await commit(batch.id)

    with pytest.raises(ImportBatchStateError, match="committed"):
        await commit(batch.id)


async def test_commit_refused_after_account_switched_to_snapshots(
    session: AsyncSession,
    run: Run,
    stage: Stage,
    commit: Commit,
    account: AccountRecord,
    csv_profile: ImportProfileRecord,
) -> None:
    batch = await stage(csv_profile, TWO_ROWS)
    await run(_switch_mode, account.id, "snapshots")

    with pytest.raises(ImportNotAllowedError):
        await commit(batch.id)


async def test_commit_refused_after_account_switched_to_hybrid(
    session: AsyncSession,
    run: Run,
    stage: Stage,
    commit: Commit,
    account: AccountRecord,
) -> None:
    # Staged on a transactions account, so it carries an opening adjustment; a hybrid account
    # takes none (Decision 13), so the staged batch is stale.
    batch = await stage(await _csv_profile(run), NOVEMBER)
    assert _adjustments(await _rows(session, batch))
    await run(_switch_mode, account.id, "hybrid")

    with pytest.raises(ImportBatchStaleError):
        await commit(batch.id)
    assert await _balance(session, account.id) == Decimal(0)


async def test_commit_rechecks_the_statement_account(
    run: Run, stage: Stage, commit: Commit
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
    first = await stage(profile, header + "10/11/2026,Coffee,-3.50,11111234\n")
    second = await stage(profile, header + "12/11/2026,Cake,-4.00,22225678\n")
    await commit(first.id)

    with pytest.raises(ImportNotAllowedError, match="5678"):
        await commit(second.id)


async def test_liability_import_signs(
    session: AsyncSession,
    run: Run,
    stage: Stage,
    commit: Commit,
    make_account: MakeAccount,
) -> None:
    card = await make_account(
        name="Example Card", ledger_type="liability", subtype="other", balance_mode="transactions"
    )
    profile = await _csv_profile(
        run,
        columns={
            "date": "Date",
            "description": ["Description"],
            "inflow": "Paid in",
            "outflow": "Paid out",
        },
    )
    content = (
        "Date,Description,Paid in,Paid out\n10/11/2026,Shop,,25.00\n12/11/2026,Payment,100.00,\n"
    )
    batch = await stage(profile, content, account_id=card.id)

    assert [row.quantity for row in await _rows(session, batch)] == [
        Decimal("-25.00"),
        Decimal("100.00"),
    ]
    await commit(batch.id)
    assert await _balance(session, card.id) == Decimal("75.00")


async def test_contra_account_reused(
    session: AsyncSession, stage: Stage, commit: Commit, csv_profile: ImportProfileRecord
) -> None:
    first = await commit((await stage(csv_profile, HEADER + "10/11/2026,Coffee,-3.50\n")).id)
    second = await commit((await stage(csv_profile, HEADER + "12/11/2026,Tea,-2.50\n")).id)

    contras: set[UUID] = set()
    for batch in (first, second):
        (row,) = await _rows(session, batch)
        postings = await _postings(session, row.transaction_id)
        contras |= {p.account_id for p in postings if p.account_id != batch.account_id}
    assert contras == {await _system_account(session, "unassigned")}


async def test_fee_posting(
    session: AsyncSession, run: Run, stage: Stage, commit: Commit, account: AccountRecord
) -> None:
    profile = await _csv_profile(
        run,
        columns={"date": "Date", "description": ["Description"], "amount": "Amount", "fee": "Fee"},
    )
    batch = await commit(
        (await stage(profile, "Date,Description,Amount,Fee\n10/11/2026,Transfer,-10.00,0.50\n")).id
    )

    (row,) = await _rows(session, batch)
    postings = await _postings(session, row.transaction_id)
    assert [(p.account_id == account.id, p.quantity, p.is_fee) for p in postings] == [
        (True, Decimal("-10.00"), False),
        (True, Decimal("-0.50"), True),
        (False, Decimal("10.50"), False),
    ]
    assert await _balance(session, account.id) == Decimal("-10.50")


# Opening adjustments ------------------------------------------------------------------


async def test_opening_adjustment_committed_and_rolled_back(
    session: AsyncSession,
    run: Run,
    stage: Stage,
    commit: Commit,
    rollback: Rollback,
    account: AccountRecord,
) -> None:
    before = await _net_worth(session)
    batch = await commit((await stage(await _csv_profile(run), NOVEMBER)).id)

    assert await _balance(session, account.id) == Decimal("1480.00")
    opening = await _system_account(session, "opening_balances")
    assert await _balance(session, opening) == Decimal("-1500.00")
    assert await _net_worth(session) - before == Decimal("1480.00")

    rolled_back = await rollback(batch.id)
    assert rolled_back.status == "rolled_back" and rolled_back.rolled_back_at is not None
    (adjustment,) = _adjustments(await _rows(session, rolled_back))
    assert adjustment.status == "rolled_back"
    assert await _balance(session, account.id) == Decimal(0)
    assert await _balance(session, opening) == Decimal(0)


async def test_stale_batch_refused(
    session: AsyncSession, run: Run, stage: Stage, commit: Commit
) -> None:
    profile = await _csv_profile(run)
    december = await stage(profile, DECEMBER)
    assert _adjustments(await _rows(session, december))  # November is missing
    await commit((await stage(profile, NOVEMBER)).id)

    with pytest.raises(ImportBatchStaleError, match="discard it and upload again"):
        await commit(december.id)
    assert _adjustments(await _rows(session, await stage(profile, DECEMBER))) == []


async def test_rollback_realigns_adjustments(
    session: AsyncSession,
    run: Run,
    stage: Stage,
    commit: Commit,
    rollback: Rollback,
    account: AccountRecord,
) -> None:
    profile = await _csv_profile(run)
    november = await commit((await stage(profile, NOVEMBER, filename="november.csv")).id)
    december = await stage(profile, DECEMBER, filename="december.csv")
    assert _adjustments(await _rows(session, december)) == []  # November's closing matches
    await commit(december.id)

    await rollback(november.id)

    (adjustment,) = _adjustments(await _rows(session, december))
    assert (adjustment.status, adjustment.quantity) == ("committed", Decimal("1480.00"))
    assert adjustment.booking_date == date(2026, 11, 30)
    assert adjustment.position == -1
    assert await _balance(session, account.id) == Decimal("1450.00")


async def test_gap_flag_when_an_earlier_statement_is_staged_after_a_later_one(
    session: AsyncSession, run: Run, stage: Stage, commit: Commit
) -> None:
    profile = await _csv_profile(run)
    december = await commit((await stage(profile, DECEMBER, filename="december.csv")).id)
    (adjustment,) = _adjustments(await _rows(session, december))
    assert adjustment.is_gap is False  # nothing earlier: the account's opening

    november = await stage(profile, NOVEMBER, filename="november.csv")
    (adjustment,) = _adjustments(await _rows(session, november))
    assert adjustment.is_gap is False  # earlier than every committed statement
    statement_rows = [row for row in await _rows(session, november) if row.kind == "statement"]
    assert {row.is_gap for row in statement_rows} == {None}


async def test_gap_flag_when_the_first_statement_is_viewed_after_later_ones(
    session: AsyncSession, run: Run, stage: Stage, commit: Commit
) -> None:
    profile = await _csv_profile(run)
    november = await commit((await stage(profile, NOVEMBER, filename="november.csv")).id)
    # December's balance doesn't follow from November's closing 1480.00: a period is missing.
    gap = BALANCE_HEADER + "01/12/2026,Rent,-30.00,1400.00\n"
    december = await stage(profile, gap, filename="december.csv")
    (adjustment,) = _adjustments(await _rows(session, december))
    assert adjustment.is_gap is True
    await commit(december.id)

    (opening,) = _adjustments(await _rows(session, november))
    assert opening.is_gap is False
    (adjustment,) = _adjustments(await _rows(session, december))
    assert adjustment.is_gap is True


# Pending rows, discard, rollback ------------------------------------------------------


async def test_pending_then_completed_counted_once(
    session: AsyncSession, run: Run, stage: Stage, commit: Commit, account: AccountRecord
) -> None:
    profile = await _csv_profile(
        run,
        columns={"date": "Date", "description": ["Description"], "amount": "Amount"},
        include_only={"State": ["COMPLETED"]},
    )
    header = "Date,Description,Amount,State\n"
    first = await stage(
        profile, header + "01/11/2026,Pay,100.00,COMPLETED\n03/11/2026,Hotel,-20.00,PENDING\n"
    )
    assert _statuses(await _rows(session, first)) == ["new", "skipped"]
    await commit(first.id)

    later = await stage(
        profile, header + "01/11/2026,Pay,100.00,COMPLETED\n03/11/2026,Hotel,-20.00,COMPLETED\n"
    )
    assert _statuses(await _rows(session, later)) == ["duplicate", "new"]
    await commit(later.id)
    assert await _balance(session, account.id) == Decimal("80.00")


async def test_pending_mid_file_then_completed_counted_once(
    session: AsyncSession, run: Run, stage: Stage, commit: Commit, account: AccountRecord
) -> None:
    """Review finding: a pending row between completed ones left its day covered, so the
    next export's completed copy was refused as an unmatched row on a covered day."""
    profile = await _csv_profile(
        run,
        columns={"date": "Date", "description": ["Description"], "amount": "Amount"},
        include_only={"State": ["COMPLETED"]},
    )
    header = "Date,Description,Amount,State\n"
    first = header + (
        "01/11/2026,Pay,100.00,COMPLETED\n03/11/2026,Hotel,-20.00,PENDING\n"
        "05/11/2026,Shop,-1.00,COMPLETED\n"
    )
    await commit((await stage(profile, first)).id)

    later = await stage(profile, first.replace("PENDING", "COMPLETED"))
    assert _statuses(await _rows(session, later)) == ["duplicate", "new", "duplicate"]
    await commit(later.id)
    assert await _balance(session, account.id) == Decimal("79.00")


async def test_export_taken_mid_day_then_rest_of_day_counted_once(
    session: AsyncSession,
    stage: Stage,
    commit: Commit,
    account: AccountRecord,
    csv_profile: ImportProfileRecord,
) -> None:
    """Review finding: an export's last day may be partial, so a later export's extra row
    on that day is new, not an error."""
    morning = HEADER + "01/11/2026,Pay,100.00\n03/11/2026,Coffee,-3.00\n05/11/2026,Bus,-2.00\n"
    await commit((await stage(csv_profile, morning)).id)

    later = await stage(
        csv_profile,
        HEADER + "05/11/2026,Bus,-2.00\n05/11/2026,Lunch,-8.00\n07/11/2026,Shop,-1.00\n",
    )
    rows = await _rows(session, later)
    assert _statuses(rows) == ["duplicate", "new", "new"]
    assert rows[1].note is not None and "lists 2026-11-05 only in part" in rows[1].note
    assert rows[2].note is None
    await commit(later.id)
    assert await _balance(session, account.id) == Decimal("86.00")


async def test_discard(
    session: AsyncSession, run: Run, stage: Stage, commit: Commit, csv_profile: ImportProfileRecord
) -> None:
    batch = await stage(csv_profile, TWO_ROWS)
    discarded = await run(discard_batch, batch.id)

    assert discarded.status == "discarded"
    with pytest.raises(ImportBatchStateError, match="discarded"):
        await commit(batch.id)
    with pytest.raises(ImportBatchStateError):
        await run(discard_batch, batch.id)


async def test_rollback_voids_and_allows_reimport(
    session: AsyncSession,
    stage: Stage,
    commit: Commit,
    rollback: Rollback,
    account: AccountRecord,
    csv_profile: ImportProfileRecord,
) -> None:
    batch = await commit((await stage(csv_profile, TWO_ROWS)).id)
    rolled_back = await rollback(batch.id)

    assert await _balance(session, account.id) == Decimal(0)
    assert _statuses(await _rows(session, rolled_back)) == ["rolled_back", "rolled_back"]
    assert _statuses(await _rows(session, await stage(csv_profile, TWO_ROWS))) == ["new", "new"]
    with pytest.raises(ImportBatchStateError, match="rolled back"):
        await rollback(batch.id)


async def test_rollback_skips_already_void_transactions(
    session: AsyncSession,
    run: Run,
    stage: Stage,
    commit: Commit,
    rollback: Rollback,
    csv_profile: ImportProfileRecord,
) -> None:
    batch = await commit((await stage(csv_profile, TWO_ROWS)).id)
    first, _ = await _rows(session, batch)
    assert first.transaction_id is not None
    await run(void_transaction, first.transaction_id)

    rolled_back = await rollback(batch.id)

    assert _statuses(await _rows(session, rolled_back)) == ["rolled_back", "rolled_back"]
    assert rolled_back.counts["rolled_back"] == 2
    e = core_models.Event
    event = await session.scalar(select(e).where(e.type == "finance.import.rolled_back"))
    assert event is not None and event.data["batch_id"] == str(batch.id)
    assert event.data["counts"]["rolled_back"] == 2
    voids = await session.scalar(
        select(func.count()).where(
            e.type == "finance.transaction.voided", e.subject_id == first.transaction_id
        )
    )
    assert voids == 1


# Locking ------------------------------------------------------------------------------


async def test_actions_lock_the_account_before_the_batch(
    session: AsyncSession,
    run: Run,
    stage: Stage,
    commit: Commit,
    rollback: Rollback,
    csv_profile: ImportProfileRecord,
) -> None:
    """One lock order everywhere (account, then batch), so align_openings' inserts into other
    batches of the account can't deadlock with another action."""
    locks: list[str] = []

    def record(*args: Any) -> None:
        statement = str(args[2])
        if " FOR " in statement and "UPDATE" in statement:
            table = "account" if "FROM finance.accounts" in statement else "other"
            if "FROM finance.import_batches" in statement:
                table = "batch"
            locks.append(table)

    discarded = await stage(csv_profile, TWO_ROWS)
    committed = await stage(csv_profile, HEADER + "12/11/2026,Tea,-2.50\n")
    connection = await session.connection()
    event.listen(connection.sync_connection, "before_cursor_execute", record)
    try:
        for action in (
            lambda: run(discard_batch, discarded.id),
            lambda: commit(committed.id),
            lambda: rollback(committed.id),
        ):
            locks.clear()
            await action()
            assert locks[:2] == ["account", "batch"]
    finally:
        event.remove(connection.sync_connection, "before_cursor_execute", record)


# PDF and OFX overlaps -----------------------------------------------------------------

# The synthetic HSBC statement for 6 May-5 Jun 2027, with a row in its last week.
PDF_ROWS = [
    *HSBC_ROWS,
    HsbcRow("DD", ("EXAMPLE ENERGY",), paid_out="40.00", date="03 Jun 27", balance="1,288.45"),
]
# An OFX export for 1-15 Jun 2027: the 3 Jun row again, and one after the statement.
EXPORT = _ofx(
    [("20270603", "-40.00", "A"), ("20270612", "-8.00", "B")],
    start="20270601",
    end="20270615",
    extra="<LEDGERBAL>\n<BALAMT>1280.45\n<DTASOF>20270615\n</LEDGERBAL>",
)


async def _pdf_and_ofx(
    run: Run, stage: Stage, make_account: MakeAccount
) -> tuple[UUID, ImportProfileRecord, ImportProfileRecord]:
    account = await make_account(name="Example HSBC", balance_mode="transactions")
    pdf = await _profile(run, format="pdf", layout="hsbc_uk_statement")
    ofx = await _profile(run, format="ofx")
    return account.id, pdf, ofx


async def test_pdf_then_ofx_overlap_commits_once(
    session: AsyncSession,
    run: Run,
    stage: Stage,
    commit: Commit,
    rollback: Rollback,
    make_account: MakeAccount,
) -> None:
    account_id, pdf, ofx = await _pdf_and_ofx(run, stage, make_account)
    statement = await commit(
        (
            await stage(
                pdf,
                hsbc_pdf(HsbcStatement(pages=[PDF_ROWS])),
                account_id=account_id,
                default=None,
                filename="statement.pdf",
            )
        ).id
    )
    assert await _balance(session, account_id) == Decimal("1288.45")

    export = await stage(ofx, EXPORT, account_id=account_id, filename="export.ofx")
    assert _statuses(await _rows(session, export)) == ["duplicate", "new"]
    assert export.warnings == []  # its opening already holds
    await commit(export.id)
    assert await _balance(session, account_id) == Decimal("1280.45")

    # The export relies on the statement for 3 Jun: the statement can't go first.
    with pytest.raises(ImportBatchStateError, match=r"export\.ofx"):
        await rollback(statement.id)


async def test_ofx_then_pdf_overlap_commits_once(
    session: AsyncSession, run: Run, stage: Stage, commit: Commit, make_account: MakeAccount
) -> None:
    account_id, pdf, ofx = await _pdf_and_ofx(run, stage, make_account)
    export = await commit(
        (await stage(ofx, EXPORT, account_id=account_id, filename="export.ofx")).id
    )
    assert await _balance(session, account_id) == Decimal("1280.45")  # its opening: 1328.45

    statement = await stage(
        pdf,
        hsbc_pdf(HsbcStatement(pages=[PDF_ROWS])),
        account_id=account_id,
        default=None,
        filename="statement.pdf",
    )
    rows = await _rows(session, statement)
    assert _statuses(rows) == ["new", "new", "new", "new", "duplicate"]
    assert statement.warnings == [
        "committing this changes the opening adjustment of export.ofx from 1328.45 to 0.00"
    ]
    await commit(statement.id)

    assert await _balance(session, account_id) == Decimal("1280.45")
    for day, balance in [("2027-05-08", "95.80"), ("2027-06-03", "1288.45")]:
        assert await _balance_after(session, account_id, date.fromisoformat(day)) == Decimal(
            balance
        )
    (superseded,) = _adjustments(await _rows(session, export))
    assert (superseded.status, superseded.note) == ("rolled_back", "superseded")


async def test_rollback_frees_covered_days(
    session: AsyncSession,
    run: Run,
    stage: Stage,
    commit: Commit,
    rollback: Rollback,
    make_account: MakeAccount,
) -> None:
    account_id, pdf, ofx = await _pdf_and_ofx(run, stage, make_account)
    content = hsbc_pdf(HsbcStatement(pages=[PDF_ROWS]))
    statement = await commit((await stage(pdf, content, account_id=account_id, default=None)).id)
    export = await commit(
        (await stage(ofx, EXPORT, account_id=account_id, filename="export.ofx")).id
    )

    with pytest.raises(ImportBatchStateError, match=r"export\.ofx"):
        await rollback(statement.id)
    await rollback(export.id)
    await rollback(statement.id)

    again = await stage(pdf, content, account_id=account_id, default=None)
    assert _statuses(await _rows(session, again)) == ["new"] * 5  # with its adjustment
    assert await _balance(session, account_id) == Decimal(0)


async def test_hybrid_commit_has_no_adjustment(
    session: AsyncSession,
    run: Run,
    stage: Stage,
    commit: Commit,
    make_account: MakeAccount,
    gbp: InstrumentRecord,
) -> None:
    hybrid = await make_account(name="Example Hybrid", balance_mode="hybrid")
    profile = await _csv_profile(run, columns=BALANCE_COLUMNS)
    batch = await commit((await stage(profile, NOVEMBER, account_id=hybrid.id)).id)

    assert _adjustments(await _rows(session, batch)) == []
    assert await _balance(session, hybrid.id) == Decimal("-20.00")


# An export for 1-15 Jun with a 4 Jun row the statement doesn't list.
CONTRADICTING = _ofx(
    [("20270603", "-40.00", "A"), ("20270604", "-5.00", "X"), ("20270612", "-8.00", "B")],
    start="20270601",
    end="20270615",
    extra="<LEDGERBAL>\n<BALAMT>1275.45\n<DTASOF>20270615\n</LEDGERBAL>",
)


async def test_committed_row_missing_from_new_file_refused(
    session: AsyncSession, run: Run, stage: Stage, commit: Commit, make_account: MakeAccount
) -> None:
    """Decision 16 both ways: on a day both files cover, every committed row must be in the
    new file too, or the ledger would contradict one of the two statements."""
    account_id, pdf, ofx = await _pdf_and_ofx(run, stage, make_account)
    await commit((await stage(ofx, CONTRADICTING, account_id=account_id, filename="export.ofx")).id)

    with pytest.raises(ImportNotAllowedError) as raised:
        await stage(
            pdf,
            hsbc_pdf(HsbcStatement(pages=[PDF_ROWS])),
            account_id=account_id,
            default=None,
            filename="statement.pdf",
        )
    message = str(raised.value)
    assert "export.ofx" in message and "2027-06-04" in message and "-5.00" in message
    assert "roll export.ofx back first" in message
    assert await _balance(session, account_id) == Decimal("1275.45")


async def test_commit_refuses_a_committed_row_missing_from_the_file(
    session: AsyncSession, run: Run, stage: Stage, commit: Commit, make_account: MakeAccount
) -> None:
    account_id, pdf, ofx = await _pdf_and_ofx(run, stage, make_account)
    statement = await stage(
        pdf, hsbc_pdf(HsbcStatement(pages=[PDF_ROWS])), account_id=account_id, default=None
    )
    await commit((await stage(ofx, CONTRADICTING, account_id=account_id, filename="export.ofx")).id)

    with pytest.raises(ImportBatchStaleError):
        await commit(statement.id)
    assert await _balance(session, account_id) == Decimal("1275.45")


async def test_duplicate_of_a_rolled_back_row_is_posted(
    session: AsyncSession,
    stage: Stage,
    commit: Commit,
    rollback: Rollback,
    account: AccountRecord,
    csv_profile: ImportProfileRecord,
) -> None:
    """A row staged as a duplicate whose original was rolled back since must not be lost."""
    first = await commit((await stage(csv_profile, TWO_ROWS)).id)
    again = await stage(csv_profile, TWO_ROWS)
    assert _statuses(await _rows(session, again)) == ["duplicate", "duplicate"]
    await rollback(first.id)

    rows = await _rows(session, await commit(again.id))
    assert _statuses(rows) == ["committed", "committed"]
    assert all(row.duplicate_of is None and row.note is None for row in rows)
    assert await _balance(session, account.id) == Decimal("96.50")


async def test_duplicate_rematched_when_another_file_now_covers_the_day(
    session: AsyncSession,
    run: Run,
    stage: Stage,
    commit: Commit,
    rollback: Rollback,
    account: AccountRecord,
    csv_profile: ImportProfileRecord,
) -> None:
    first = await commit(
        (await stage(csv_profile, HEADER + "20/11/2026,Cafe,-3.50\n", filename="x.csv")).id
    )
    ofx = await _profile(run, format="ofx")
    export = await stage(ofx, _ofx([("20261120", "-3.50", "A")], start="20261120", end="20261120"))
    await rollback(first.id)
    # Another file for the day, committed after the rollback.
    other = await commit(
        (await stage(csv_profile, HEADER + "20/11/2026,Example Cafe,-3.50\n", filename="w.csv")).id
    )

    (row,) = await _rows(session, await commit(export.id))
    (original,) = await _rows(session, other)
    assert (row.status, row.duplicate_of, row.note) == (
        "duplicate",
        original.id,
        "already imported from w.csv",
    )
    assert await _balance(session, account.id) == Decimal("-3.50")


# Openings two files disagree on (final review C1) ------------------------------------

# Balances 100.00, 95.00 and 87.00 over 10-28 Nov: its opening is 0.00.
WITH_BALANCES = BALANCE_HEADER + (
    "10/11/2026,Pay,100.00,100.00\n20/11/2026,Cafe,-5.00,95.00\n28/11/2026,Shop,-8.00,87.00\n"
)
# The same 20 and 28 Nov rows and one more, but a LEDGERBAL 3.00 lower than the CSV implies:
# its opening on 20 Nov is 97.00 where the CSV's balances give 100.00.
LOWER_LEDGERBAL = _ofx(
    [("20261120", "-5.00", "A"), ("20261128", "-8.00", "B"), ("20261203", "-2.00", "C")],
    start="20261120",
    end="20261205",
    extra="<LEDGERBAL>\n<BALAMT>82.00\n<DTASOF>20261205\n</LEDGERBAL>",
)


async def test_overlapping_file_with_another_opening_refused(
    session: AsyncSession,
    run: Run,
    stage: Stage,
    commit: Commit,
    account: AccountRecord,
    tmp_path: Path,
) -> None:
    """An adjustment may stand only where no other statement vouches for the balance: on a
    day both files cover, a different opening is a contradiction, not a gap."""
    await commit((await stage(await _csv_profile(run), WITH_BALANCES, filename="nov.csv")).id)
    ofx = await _profile(run, format="ofx")

    with pytest.raises(ImportNotAllowedError) as raised:
        await stage(ofx, LOWER_LEDGERBAL, filename="export.ofx")
    message = str(raised.value)
    assert "export.ofx" in message and "nov.csv" in message
    assert "97.00" in message and "100.00" in message and "2026-11-20" in message
    assert await _balance_after(session, account.id, date(2026, 11, 30)) == Decimal("87.00")
    # Refused while staging its openings: only nov.csv's bytes are stored.
    assert len(_files(tmp_path)) == 1


async def test_older_file_with_another_opening_refused(
    session: AsyncSession, run: Run, stage: Stage, commit: Commit, account: AccountRecord
) -> None:
    # The same contradiction the other way round: the OFX first, then the CSV.
    ofx = await _profile(run, format="ofx")
    await commit((await stage(ofx, LOWER_LEDGERBAL, filename="export.ofx")).id)

    with pytest.raises(ImportNotAllowedError) as raised:
        await stage(await _csv_profile(run), WITH_BALANCES, filename="nov.csv")
    message = str(raised.value)
    assert "export.ofx" in message and "nov.csv" in message
    assert "97.00" in message and "100.00" in message
    assert await _balance(session, account.id) == Decimal("82.00")


async def test_same_first_day_with_another_opening_refused(
    run: Run, stage: Stage, commit: Commit
) -> None:
    # Both start on 2 Nov; the CSV says 1500.00 before it, the OFX 1497.00 (task 5's N1).
    await commit((await stage(await _csv_profile(run), NOVEMBER, filename="nov.csv")).id)
    ofx = await _profile(run, format="ofx")
    content = _ofx(
        [("20261102", "-18.00", "A"), ("20261103", "-2.00", "B")],
        start="20261102",
        end="20261103",
        extra="<LEDGERBAL>\n<BALAMT>1477.00\n<DTASOF>20261103\n</LEDGERBAL>",
    )

    with pytest.raises(ImportNotAllowedError, match=r"1497\.00"):
        await stage(ofx, content, filename="export.ofx")


async def test_commit_refuses_an_opening_another_file_now_contradicts(
    session: AsyncSession, run: Run, stage: Stage, commit: Commit, account: AccountRecord
) -> None:
    ofx = await _profile(run, format="ofx")
    export = await stage(ofx, LOWER_LEDGERBAL, filename="export.ofx")  # nothing to contradict yet
    await commit((await stage(await _csv_profile(run), WITH_BALANCES, filename="nov.csv")).id)

    with pytest.raises(ImportBatchStaleError):
        await commit(export.id)
    assert await _balance_after(session, account.id, date(2026, 11, 30)) == Decimal("87.00")


async def test_superseded_adjustment_is_not_a_gap(
    session: AsyncSession, run: Run, stage: Stage, commit: Commit, make_account: MakeAccount
) -> None:
    account_id, pdf, ofx = await _pdf_and_ofx(run, stage, make_account)
    export = await commit(
        (await stage(ofx, EXPORT, account_id=account_id, filename="export.ofx")).id
    )
    await commit(
        (
            await stage(
                pdf, hsbc_pdf(HsbcStatement(pages=[PDF_ROWS])), account_id=account_id, default=None
            )
        ).id
    )

    (superseded,) = _adjustments(await _rows(session, export))
    assert superseded.status == "rolled_back"
    assert superseded.is_gap is None


async def test_row_before_dtstart_still_checks_its_days(
    session: AsyncSession, run: Run, stage: Stage, commit: Commit, account: AccountRecord
) -> None:
    """Final review test 2: an OFX whose DTSTART (25 Nov) comes after one of its rows
    (20 Nov) covers that row's day too, so it can't skip the CSV's 22 Nov row unnoticed."""
    csv = BALANCE_HEADER + (
        "10/11/2026,Pay,100.00,100.00\n20/11/2026,Cafe,-5.00,95.00\n"
        "22/11/2026,Taxi,-10.00,85.00\n28/11/2026,Shop,-8.00,77.00\n"
    )
    await commit((await stage(await _csv_profile(run), csv, filename="nov.csv")).id)
    ofx = await _profile(run, format="ofx")
    content = _ofx(
        [("20261120", "-5.00", "A"), ("20261128", "-8.00", "B"), ("20261203", "-7.00", "C")],
        start="20261125",
        end="20261205",
        extra="<LEDGERBAL>\n<BALAMT>70.00\n<DTASOF>20261205\n</LEDGERBAL>",
    )

    with pytest.raises(ImportNotAllowedError, match=r"nov\.csv has -10\.00 on 2026-11-22"):
        await stage(ofx, content, filename="export.ofx")
    assert await _balance_after(session, account.id, date(2026, 11, 30)) == Decimal("77.00")
