"""The Trading 212 Cash ISA's History CSV profile, from the catalog (synthetic data)."""

from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.modules.finance import models
from lararium.modules.finance.errors import FileFormatError
from lararium.modules.finance.importing.catalog import CATALOG
from lararium.modules.finance.importing.csv_parser import parse_csv
from lararium.modules.finance.importing.parsed import ParsedRow, ParsedStatement, RowError
from lararium.modules.finance.importing.profiles import CsvProfileConfig, create_profile

from .conftest import Run
from .staging_helpers import Commit, Stage, _rows, _statuses

LONDON = ZoneInfo("Europe/London")
HEADER = "Action,Time (UTC),Notes,ID,Total,Currency (Total)\n"
CSV = (
    HEADER + "Deposit,2026-01-05 09:10:11+00:00,"
    '"Transaction ID: 11111111-2222-3333-4444-555555555555",'
    '01aaaaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee,25.00,"GBP"\n'
    'Withdrawal,2026-01-06 10:00:00+00:00,,01aaaaaa-bbbb-7ccc-8ddd-ffffffffffff,-10.00,"GBP"\n'
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
