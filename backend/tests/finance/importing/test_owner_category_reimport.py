"""The owner's category of a statement row: it follows the row rolled back and imported
again, and the reports find it on that row alone."""

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.modules.finance.accounts import AccountRecord
from lararium.modules.finance.cashflow import cash_flow
from lararium.modules.finance.importing.profiles import ImportProfileRecord
from lararium.modules.finance.income import income_report
from lararium.modules.finance.instruments import InstrumentRecord
from lararium.modules.finance.ledger import PostingInput, TransactionInput, post_transaction
from lararium.modules.finance.owner_categories import (
    OWNER_CATEGORY_SQL,
    known_owner_categories,
    set_owner_category,
)
from lararium.modules.finance.system_accounts import get_or_create_system_account

from .staging_helpers import HEADER, Commit, Rollback, Run, Stage

ONE_ROW = HEADER + "10/11/2026,Own row,1.00\n"
TWO_ROWS = HEADER + "10/11/2026,Row one,3.00\n11/11/2026,Row two,4.00\n"


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

    assert await known_owner_categories(session, "income", ()) == ["family_money"]
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


async def test_reports_find_each_transactions_own_category(
    session: AsyncSession,
    run: Run,
    stage: Stage,
    commit: Commit,
    csv_profile: ImportProfileRecord,
    account: AccountRecord,
    gbp: InstrumentRecord,
) -> None:
    await commit((await stage(csv_profile, TWO_ROWS)).id)
    rows = await session.execute(
        text(
            "select description, transaction_id from finance.import_rows"
            " where status = 'committed' and kind = 'statement'"
        )
    )
    imported = {description: transaction_id for description, transaction_id in rows}
    unassigned = await run(get_or_create_system_account, "unassigned")

    async def manual(description: str, amount: str) -> object:
        record = await run(
            post_transaction,
            TransactionInput(
                occurred_at=datetime(2026, 11, 12, 12, tzinfo=ZoneInfo("Europe/London")),
                description=description,
                postings=[
                    PostingInput(account.id, gbp.id, Decimal(amount)),
                    PostingInput(unassigned, gbp.id, -Decimal(amount)),
                ],
            ),
        )
        return record.id

    gift = await manual("Manual gift", "5.00")
    plain = await manual("Manual plain", "6.00")
    await run(set_owner_category, [imported["Row one"]], "Family money")
    await run(set_owner_category, [gift], "Gifts")

    start, end = date(2026, 11, 1), date(2026, 11, 30)
    flow = await cash_flow(session, start, end)
    assert {c.category: c.income for c in flow.categories} == {
        "family_money": Decimal("3.00"),
        "gifts": Decimal("5.00"),
        None: Decimal("10.00"),
    }
    report = await income_report(session, start, end)
    assert {e.transaction_id: e.owner_category for e in report.entries} == {
        imported["Row one"]: "family_money",
        imported["Row two"]: None,
        gift: "gifts",
        plain: None,
    }
    assert [(c.category, c.amount) for c in report.categories] == [
        (None, Decimal("10.00")),
        ("gifts", Decimal("5.00")),
        ("family_money", Decimal("3.00")),
    ]
