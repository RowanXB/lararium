"""The Trading 212 Cash ISA's History CSV profile, from the catalog (synthetic data)."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.modules.finance import models
from lararium.modules.finance.accounts import AccountRecord
from lararium.modules.finance.errors import FileFormatError
from lararium.modules.finance.identifiers import family_identifier
from lararium.modules.finance.importing.catalog import CATALOG
from lararium.modules.finance.importing.csv_parser import parse_csv
from lararium.modules.finance.importing.parsed import ParsedRow, ParsedStatement, RowError
from lararium.modules.finance.importing.profiles import CsvProfileConfig, create_profile
from lararium.modules.finance.importing.staging import (
    NO_BALANCES_WARNING,
    NO_PERIOD_IN_FILE_NAME_WARNING,
)

from .conftest import MakeAccount, Run
from .staging_helpers import Commit, Stage, _rows, _statuses

LONDON = ZoneInfo("Europe/London")
HEADER = "Action,Time (UTC),Notes,ID,Total,Currency (Total)\n"
CSV = (
    HEADER + "Deposit,2026-01-05 09:10:11+00:00,"
    '"Transaction ID: 11111111-2222-3333-4444-555555555555",'
    '01aaaaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee,25.00,"GBP"\n'
    'Withdrawal,2026-01-06 10:00:00+00:00,,01aaaaaa-bbbb-7ccc-8ddd-ffffffffffff,-10.00,"GBP"\n'
)


@pytest.fixture
async def account(make_account: MakeAccount) -> AccountRecord:
    """The account the files are staged into: a Cash ISA, of the Trading 212 family."""
    return await make_account(
        name="Cash ISA",
        subtype="savings_pot",
        tax_wrapper="isa",
        balance_mode="transactions",
        type_key="trading212.cash_isa",
    )


def _entry_config() -> CsvProfileConfig:
    [entry] = [
        e for e in CATALOG if (e.institution, e.label) == ("Trading 212 Cash ISA", "History CSV")
    ]
    assert entry.recommended
    assert isinstance(entry.config, CsvProfileConfig)
    return entry.config


def _parse(content: str) -> ParsedStatement:
    return parse_csv(content.encode(), _entry_config(), LONDON)


def test_the_history_csv_parses_through_the_catalog() -> None:
    first, second = _parse(CSV).rows
    assert isinstance(first, ParsedRow) and isinstance(second, ParsedRow)
    assert (first.booking_date, second.booking_date) == (date(2026, 1, 5), date(2026, 1, 6))
    assert first.occurred_at == datetime(2026, 1, 5, 9, 10, 11, tzinfo=UTC)
    assert (first.quantity, second.quantity) == (Decimal("25.00"), Decimal("-10.00"))
    assert (first.currency, second.currency) == ("GBP", "GBP")
    assert first.external_id == "01aaaaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee"
    assert second.external_id == "01aaaaaa-bbbb-7ccc-8ddd-ffffffffffff"
    assert first.memo == "Transaction ID: 11111111-2222-3333-4444-555555555555"
    assert not second.memo
    assert (first.bank_type, second.bank_type) == ("Deposit", "Withdrawal")
    assert (first.description, second.description) == ("Deposit", "Withdrawal")


async def test_every_row_commits_as_a_bank_transfer_and_restaging_adds_none(
    session: AsyncSession, run: Run, stage: Stage, commit: Commit
) -> None:
    profile = await run(create_profile, name="Cash ISA", config=_entry_config())
    batch = await stage(profile, CSV)
    assert _statuses(await _rows(session, batch)) == ["new", "new"]
    await commit(batch.id)
    flags = list(await session.scalars(select(models.Transaction.bank_transfer)))
    assert flags == [True, True]

    again = await stage(profile, CSV)
    assert _statuses(await _rows(session, again)) == ["duplicate", "duplicate"]
    assert await session.scalar(select(func.count()).select_from(models.Transaction)) == 2


async def test_a_trade_row_is_staged_not_posted(
    session: AsyncSession, run: Run, stage: Stage
) -> None:
    content = (
        "Action,Time (UTC),Ticker,No. of shares,Notes,ID,Total,Currency (Total)\n"
        "Market buy,2026-01-07 11:00:00+00:00,AAA,1.5,,01aaaaaa-bbbb-7ccc-8ddd-000000000001,"
        '-30.00,"GBP"\n'
    )
    profile = await run(create_profile, name="Cash ISA", config=_entry_config())
    rows = await _rows(session, await stage(profile, content))
    assert _statuses(rows) == ["new"]
    assert rows[0].bank_type == "Market buy"
    assert await session.scalar(select(func.count()).select_from(models.Transaction)) == 0


def test_a_file_without_the_time_column_is_refused() -> None:
    content = "Action,Notes,ID,Total,Currency (Total)\nDeposit,,x,1.00,GBP\n"
    with pytest.raises(FileFormatError, match=r"Time \(UTC\)"):
        _parse(content)


def test_a_bad_currency_or_a_time_without_an_offset_is_a_row_error() -> None:
    bad_currency = HEADER + "Deposit,2026-01-05 09:10:11+00:00,,id1,1.00,GB\n"
    no_offset = HEADER + "Deposit,2026-01-05 09:10:11,,id2,1.00,GBP\n"
    for content in (bad_currency, no_offset):
        [row] = _parse(content).rows
        assert isinstance(row, RowError)


ONE_DEPOSIT = (
    HEADER + 'Deposit,2026-01-05 09:10:11+00:00,,01aaaaaa-bbbb-7ccc-8ddd-000000000002,25.00,"GBP"\n'
)
# A Revolut Current CSV (synthetic), a profile without a file name pattern.
REVOLUT = (
    "Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance\n"
    "Topup,Current,2026-01-02 10:00:00,2026-01-02 10:00:00,Top-up,100.00,0.00,GBP,COMPLETED,"
    "100.00\n"
    "Transfer,Current,2026-01-05 10:00:00,2026-01-05 10:00:00,Example Payee,-10.00,0.00,GBP,"
    "COMPLETED,90.00\n"
    "Topup,Current,2026-01-08 10:00:00,2026-01-08 10:00:00,Top-up,5.00,0.00,GBP,COMPLETED,"
    "95.00\n"
)


async def _coverage(session: AsyncSession, batch_id: UUID) -> tuple[Any, ...]:
    b = models.ImportBatch
    found = await session.execute(
        select(b.covered_from, b.covered_to, b.complete_from, b.complete_to).where(b.id == batch_id)
    )
    return tuple(found.one())


def test_only_the_cash_isa_reads_its_period_from_the_file_name() -> None:
    assert _entry_config().period_from_filename is not None
    others = [
        e
        for e in CATALOG
        if isinstance(e.config, CsvProfileConfig) and e.config.period_from_filename is not None
    ]
    assert [(e.institution, e.label) for e in others] == [("Trading 212 Cash ISA", "History CSV")]


async def test_the_file_name_gives_the_period_every_day_in_full_but_the_last(
    session: AsyncSession, run: Run, stage: Stage
) -> None:
    profile = await run(create_profile, name="Cash ISA", config=_entry_config())
    batch = await stage(profile, ONE_DEPOSIT, filename="from_2026-01-01_to_2026-01-10_x.csv")
    assert await _coverage(session, batch.id) == (
        date(2026, 1, 1),
        date(2026, 1, 10),
        date(2026, 1, 1),
        date(2026, 1, 9),
    )
    assert batch.statement is not None
    assert (batch.statement.period_from, batch.statement.period_to) == (
        date(2026, 1, 1),
        date(2026, 1, 10),
    )
    assert batch.warnings == [NO_BALANCES_WARNING]


@pytest.mark.parametrize(
    "filename",
    [
        "history.csv",
        # Starts after it ends.
        "from_2026-01-10_to_2026-01-01_x.csv",
        # The row on 5 January is outside the range.
        "from_2026-01-06_to_2026-01-10_x.csv",
        "from_2026-01-01_to_2026-01-04_x.csv",
        # No such day.
        "from_2026-02-30_to_2026-03-10_x.csv",
    ],
)
async def test_a_file_name_giving_no_period_leaves_the_upload_as_read_and_warns(
    session: AsyncSession, run: Run, stage: Stage, filename: str
) -> None:
    profile = await run(create_profile, name="Cash ISA", config=_entry_config())
    batch = await stage(profile, ONE_DEPOSIT, filename=filename)
    # One row lists no day in full (M2 Decision 16).
    assert await _coverage(session, batch.id) == (date(2026, 1, 5), date(2026, 1, 5), None, None)
    assert batch.statement is None
    assert batch.warnings == [NO_PERIOD_IN_FILE_NAME_WARNING, NO_BALANCES_WARNING]
    # The warning names no value from the file name.
    assert not any(char.isdigit() for char in NO_PERIOD_IN_FILE_NAME_WARNING)


async def test_a_profile_without_the_pattern_ignores_the_file_name(
    session: AsyncSession, run: Run, stage: Stage
) -> None:
    [entry] = [e for e in CATALOG if (e.institution, e.label) == ("Revolut Current", "CSV")]
    assert isinstance(entry.config, CsvProfileConfig)
    assert entry.config.period_from_filename is None
    profile = await run(create_profile, name="Revolut CSV", config=entry.config)
    batch = await stage(profile, REVOLUT, filename="from_2026-01-01_to_2026-01-10_x.csv")
    assert await _coverage(session, batch.id) == (
        date(2026, 1, 2),
        date(2026, 1, 8),
        date(2026, 1, 3),
        date(2026, 1, 7),
    )
    assert batch.statement is not None and batch.statement.period_from is None
    assert batch.warnings == []


# What a row's Notes say (account family identifier design §5.9, owner's decision
# 2026-11-27): a transaction id, an external movement; a bank account, a withdrawal to it;
# empty, a move from or to another Trading 212 account.
NOTES = (
    HEADER + "Deposit,2026-01-05 09:10:11+00:00,"
    '"Transaction ID: 11111111-2222-3333-4444-555555555555",01aaaaaa-0000-7000-8000-000000000001,'
    '25.00,"GBP"\n'
    'Deposit,2026-01-05 09:12:00+00:00,,01aaaaaa-0000-7000-8000-000000000002,40.00,"GBP"\n'
    'Withdrawal,2026-01-06 10:00:00+00:00,,01aaaaaa-0000-7000-8000-000000000003,-10.00,"GBP"\n'
    'Withdrawal,2026-01-06 10:01:00+00:00,"Sent to Bank Account 12-34-56 / 87654321",'
    '01aaaaaa-0000-7000-8000-000000000004,-5.00,"GBP"\n'
)


def test_notes_say_whether_a_row_moved_within_the_family() -> None:
    rows = _parse(NOTES).rows
    assert all(isinstance(row, ParsedRow) for row in rows)
    assert [row.family_move for row in rows if isinstance(row, ParsedRow)] == [
        False,
        True,
        True,
        False,
    ]
    # A row of another type: the Notes say nothing of it.
    trade = (
        "Action,Time (UTC),Notes,ID,Total,Currency (Total)\n"
        "Market buy,2026-01-07 11:00:00+00:00,,01aaaaaa-0000-7000-8000-000000000005,-30.00,GBP\n"
    )
    [row] = _parse(trade).rows
    assert isinstance(row, ParsedRow) and row.family_move is None


def test_notes_of_another_form_are_a_row_error() -> None:
    content = HEADER + "Deposit,2026-01-05 09:10:11+00:00,Something new,id1,1.00,GBP\n"
    [row] = _parse(content).rows
    assert isinstance(row, RowError)
    # It names the column, never the value (the profile's Notes extract refuses it first).
    assert "Notes" in row.message
    assert "Something" not in row.message


async def _transaction(session: AsyncSession, batch_id: UUID, position: int) -> Any:
    r, t = models.ImportRow, models.Transaction
    return (
        await session.execute(
            select(r.family_move, t.counterparty_identifier_id, t.outside_family)
            .join(t, t.id == r.transaction_id)
            .where(r.batch_id == batch_id, r.position == position)
        )
    ).one()


async def test_an_internal_move_commits_naming_the_family(
    session: AsyncSession,
    run: Run,
    stage: Stage,
    commit: Commit,
    make_account: MakeAccount,
) -> None:
    profile = await run(create_profile, name="Cash ISA", config=_entry_config())
    batch = await stage(profile, NOTES)
    assert _statuses(await _rows(session, batch)) == ["new"] * 4
    await commit(batch.id)
    family_id = await run(family_identifier, "trading212")
    assert tuple(await _transaction(session, batch.id, 0)) == (False, None, True)
    assert tuple(await _transaction(session, batch.id, 1)) == (True, family_id, False)
    assert tuple(await _transaction(session, batch.id, 2)) == (True, family_id, False)
    # A withdrawal sent to a bank account names that account (the extract reads it) and is
    # outside the family.
    sent = await _transaction(session, batch.id, 3)
    assert (sent.family_move, sent.outside_family) == (False, True)
    named = await session.get(models.AccountIdentifier, sent.counterparty_identifier_id)
    assert named is not None and named.kind == "uk_bank_account"

    # On an account of no type, the rows the Notes call internal moves are row errors: the
    # account has no family to name.
    plain = await make_account(name="Untyped", balance_mode="transactions")
    other = await stage(profile, NOTES, account_id=plain.id)
    rows = await _rows(session, other)
    assert _statuses(rows) == ["new", "error", "error", "new"]
    assert [row.family_move for row in rows] == [False, True, True, False]
    assert rows[1].error is not None and "family" in rows[1].error


NOTES_CSV = (
    HEADER + "Deposit,2026-01-05 09:10:11+00:00,"
    '"Transaction ID: 11111111-2222-3333-4444-555555555555",'
    '01aaaaaa-bbbb-7ccc-8ddd-000000000001,25.00,"GBP"\n'
    'Deposit,2026-01-06 10:00:00+00:00,,01aaaaaa-bbbb-7ccc-8ddd-000000000002,1.00,"GBP"\n'
    'Withdrawal,2026-01-06 10:05:00+00:00,"Sent to Bank Account 12-34-56 / 87654321",'
    '01aaaaaa-bbbb-7ccc-8ddd-000000000003,-1.00,"GBP"\n'
)


def test_a_withdrawal_names_the_bank_account_it_was_sent_to() -> None:
    deposit, internal, withdrawal = _parse(NOTES_CSV).rows
    assert isinstance(deposit, ParsedRow) and isinstance(internal, ParsedRow)
    assert isinstance(withdrawal, ParsedRow)
    assert withdrawal.counterparty is not None
    assert (withdrawal.counterparty.sort_code, withdrawal.counterparty.account_number) == (
        "123456",
        "87654321",
    )
    assert withdrawal.memo == "Sent to Bank Account 12-34-56 / 87654321"
    # A deposit's transaction id and an internal move's empty Notes name no bank account.
    for row in (deposit, internal):
        assert row.counterparty is None or row.counterparty.sort_code is None


def test_notes_of_another_form_make_the_row_an_error() -> None:
    content = (
        HEADER + 'Deposit,2026-01-05 09:10:11+00:00,"Something new",'
        '01aaaaaa-bbbb-7ccc-8ddd-000000000004,5.00,"GBP"\n'
    )
    [row] = _parse(content).rows
    assert isinstance(row, RowError)
