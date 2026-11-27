"""The owner's category follows a statement row rolled back and imported again."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.modules.finance.importing.profiles import ImportProfileRecord
from lararium.modules.finance.owner_categories import (
    OWNER_CATEGORY_SQL,
    known_owner_categories,
    set_owner_category,
)

from .staging_helpers import HEADER, Commit, Rollback, Run, Stage

ONE_ROW = HEADER + "10/11/2026,Own row,1.00\n"


async def _transaction_id(session: AsyncSession) -> object:
    return (
        await session.execute(
            text(
                "select transaction_id from finance.import_rows"
                " where status = 'committed' and description = 'Own row'"
            )
        )
    ).scalar_one()


async def test_category_survives_reimport(
    session: AsyncSession,
    run: Run,
    stage: Stage,
    commit: Commit,
    rollback: Rollback,
    csv_profile: ImportProfileRecord,
) -> None:
    batch = await commit((await stage(csv_profile, ONE_ROW)).id)
    first = await _transaction_id(session)
    await run(set_owner_category, [first], "Family money")

    await rollback(batch.id)
    await commit((await stage(csv_profile, ONE_ROW, filename="again.csv")).id)
    second = await _transaction_id(session)
    assert second != first

    assert await known_owner_categories(session) == ["family_money"]
    found = (
        await session.execute(
            text(f"select {OWNER_CATEGORY_SQL} from finance.transactions t where t.id = :id"),
            {"id": second},
        )
    ).scalar_one()
    assert found == "family_money"
    kind = (
        await session.execute(text("select source_kind from finance.transaction_owner_categories"))
    ).scalar_one()
    assert kind == "import"
