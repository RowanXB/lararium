"""Statement imports keep `transactions.income_kind` right on commit and rollback."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.modules.finance.importing.profiles import ImportProfileRecord

from .staging_helpers import HEADER, Commit, Rollback, Stage

TWO_ROWS = HEADER + "10/11/2026,Own row,1.00\n11/11/2026,Plain,2.00\n"
# Two rows alike but for their text (and so their keys): a later file's rows are matched with
# them by day and amount alone, in file order, so its interest row may be paired with either.
ALIKE = HEADER + "10/11/2026,Example Shop,5.00\n10/11/2026,Example Savings,5.00\n"
LATER = HEADER + "10/11/2026,Example Interest,5.00\n10/11/2026,Example Card,5.00\n"


async def _kinds(session: AsyncSession) -> dict[str, str | None]:
    rows = await session.execute(
        text(
            "select r.description, t.income_kind from finance.import_rows r"
            " join finance.transactions t on t.id = r.transaction_id"
            " where r.status = 'committed'"
        )
    )
    return {description: kind for description, kind in rows}


async def _categories(session: AsyncSession) -> dict[str, str | None]:
    rows = await session.execute(
        text("select description, category from finance.transactions order by description")
    )
    return {description: category for description, category in rows}


async def _mark_interest(session: AsyncSession, batch_id: object, description: str) -> None:
    await session.execute(
        text(
            "update finance.import_rows set interest = true"
            " where batch_id = :batch and description = :description"
        ),
        {"batch": batch_id, "description": description},
    )


async def test_interest_row_marks_its_transaction(
    session: AsyncSession, stage: Stage, commit: Commit, csv_profile: ImportProfileRecord
) -> None:
    batch = await stage(csv_profile, TWO_ROWS)
    await _mark_interest(session, batch.id, "Own row")
    await commit(batch.id)
    assert await _kinds(session) == {"Own row": "interest", "Plain": None}


async def test_duplicate_interest_row_marks_the_committed_transaction(
    session: AsyncSession,
    stage: Stage,
    commit: Commit,
    rollback: Rollback,
    csv_profile: ImportProfileRecord,
) -> None:
    await commit((await stage(csv_profile, TWO_ROWS)).id)
    assert await _kinds(session) == {"Own row": None, "Plain": None}

    again = await stage(csv_profile, TWO_ROWS, filename="again.csv")
    await _mark_interest(session, again.id, "Own row")
    await commit(again.id)
    assert await _kinds(session) == {"Own row": "interest", "Plain": None}

    await rollback(again.id)
    assert await _kinds(session) == {"Own row": None, "Plain": None}


async def test_source_category_untouched(
    session: AsyncSession,
    stage: Stage,
    commit: Commit,
    rollback: Rollback,
    csv_profile: ImportProfileRecord,
) -> None:
    await session.execute(text("update finance.import_rows set interest = false"))
    first = await stage(csv_profile, TWO_ROWS)
    await commit(first.id)
    await session.execute(text("update finance.transactions set category = 'salary'"))
    before = await _categories(session)
    again = await stage(csv_profile, TWO_ROWS, filename="again.csv")
    await _mark_interest(session, again.id, "Own row")
    await commit(again.id)
    assert await _categories(session) == before
    await rollback(again.id)
    assert await _categories(session) == before


async def test_duplicate_among_alike_rows_marks_neither(
    session: AsyncSession, stage: Stage, commit: Commit, csv_profile: ImportProfileRecord
) -> None:
    await commit((await stage(csv_profile, ALIKE)).id)
    later = await stage(csv_profile, LATER, filename="later.csv")
    await _mark_interest(session, later.id, "Example Interest")
    await commit(later.id)
    assert await _kinds(session) == {"Example Shop": None, "Example Savings": None}
