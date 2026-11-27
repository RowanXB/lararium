"""Migration 0048's backfill: which committed statement events get the kind `interest`."""

import importlib.util
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.modules.finance.importing.catalog import CATALOG
from lararium.modules.finance.importing.profiles import (
    CsvProfileConfig,
    ImportProfileRecord,
    create_profile,
)

from .staging_helpers import HEADER, Commit, Run, Stage

MIGRATION = (
    Path(__file__).parents[3] / "migrations" / "versions" / "0048_finance_income_categories.py"
)

THREE_ROWS = (
    HEADER + "10/11/2026,Own row,1.00\n11/11/2026,Duplicate row,2.00\n12/11/2026,Plain,3.00\n"
)
SAVINGS_CSV = (
    "Date,Description,Money out,Money in,Balance\n"
    '"Oct 1, 2026",Deposit,,"£1,000.00","£1,000.00"\n'
    '"Oct 2, 2026",Gross Interest,,£0.10,"£1,000.10"\n'
)


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location(MIGRATION.stem, MIGRATION)
    assert spec is not None and spec.loader is not None
    module: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _backfill() -> str:
    return _migration().BACKFILL


async def _kinds(session: AsyncSession) -> dict[str, str | None]:
    rows = await session.execute(
        text(
            "select r.description, t.income_kind from finance.import_rows r"
            " join finance.transactions t on t.id = r.transaction_id"
            " where r.status = 'committed'"
        )
    )
    return {description: kind for description, kind in rows}


async def test_backfill_reads_committed_rows_and_their_committed_duplicates(
    session: AsyncSession, stage: Stage, commit: Commit, csv_profile: ImportProfileRecord
) -> None:
    await commit((await stage(csv_profile, THREE_ROWS)).id)
    again = await stage(csv_profile, THREE_ROWS)
    await session.execute(
        text(
            "update finance.import_rows set interest = true where description = 'Own row'"
            " and status = 'committed'"
        )
    )
    # A duplicate in a staged batch says interest, but only a committed batch counts.
    await session.execute(
        text(
            "update finance.import_rows set interest = true"
            " where batch_id = :batch and description = 'Duplicate row'"
        ),
        {"batch": again.id},
    )

    await session.execute(text(_backfill()))
    assert await _kinds(session) == {
        "Own row": "interest",
        "Duplicate row": None,
        "Plain": None,
    }

    await commit(again.id)
    await session.execute(text(_backfill()))
    assert await _kinds(session) == {
        "Own row": "interest",
        "Duplicate row": "interest",
        "Plain": None,
    }


async def test_revolut_savings_interest_rows_are_marked(
    session: AsyncSession, run: Run, stage: Stage, commit: Commit
) -> None:
    # A Revolut Savings CSV profile stored before its catalog entry named its interest rows.
    entry = next(
        e.config for e in CATALOG if (e.institution, e.label) == ("Revolut Savings", "CSV")
    )
    assert isinstance(entry, CsvProfileConfig)
    stored = entry.model_copy(update={"interest_descriptions": []})
    profile = await run(create_profile, name="Example Savings CSV", config=stored)
    await commit((await stage(profile, SAVINGS_CSV, filename="savings.csv")).id)
    assert await _kinds(session) == {"Deposit": None, "Gross Interest": None}

    for statement in _migration().DATA:
        await session.execute(text(statement))
    assert await _kinds(session) == {"Deposit": None, "Gross Interest": "interest"}
    interest = await session.execute(
        text("select description, interest from finance.import_rows order by position")
    )
    assert list(interest) == [("Deposit", False), ("Gross Interest", True)]
    config = await session.scalar(
        text(
            "select config -> 'interest_descriptions' from finance.import_profiles where id = :id"
        ),
        {"id": profile.id},
    )
    assert config == ["Gross Interest"]
