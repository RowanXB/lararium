"""The owner's categories: normalised free text kept by source reference."""

import re
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.connections import create_connection
from lararium.modules.finance.accounts import AccountRecord, create_account
from lararium.modules.finance.errors import FinanceValidationError, TransactionNotFoundError
from lararium.modules.finance.instruments import InstrumentRecord
from lararium.modules.finance.ledger import (
    PostingInput,
    TransactionDetails,
    TransactionInput,
    post_transaction,
    upsert_connector_transaction,
    void_transaction,
)
from lararium.modules.finance.owner_categories import (
    OWNER_CATEGORY_SQL,
    known_owner_categories,
    normalise_category,
    set_owner_category,
)
from tests.conftest import Run


@pytest.mark.parametrize(
    ("text_in", "expected"),
    [(" Family  money ", "family_money"), ("SALARY", "salary"), ("a\tb\nc", "a_b_c")],
)
def test_normalise_category(text_in: str, expected: str) -> None:
    assert normalise_category(text_in) == expected


@pytest.mark.parametrize("text_in", ["", "   ", "x" * 65, "uncategorised", " Uncategorised "])
def test_normalise_category_refuses(text_in: str) -> None:
    with pytest.raises(FinanceValidationError):
        normalise_category(text_in)


async def test_normalise_category_accepts_the_longest() -> None:
    assert normalise_category("x" * 64) == "x" * 64


def test_sql_reads_t_only_as_the_outer_row() -> None:
    # The import lookups are subqueries. Compiled on their own, each took a
    # `finance.transactions AS t` of its own, so `t` there named any row, not the outer one.
    assert re.search(r"transactions\s+(AS\s+)?t\b", OWNER_CATEGORY_SQL, re.IGNORECASE) is None
    assert "t.id" in OWNER_CATEGORY_SQL


async def _accounts(run: Run) -> tuple[AccountRecord, AccountRecord]:
    return (
        await run(
            create_account, name="A", ledger_type="asset", subtype="bank", balance_mode="hybrid"
        ),
        # Income, so the transaction (A +5, B -5) is income and its category is known there.
        await run(
            create_account, name="B", ledger_type="income", subtype="other", balance_mode="hybrid"
        ),
    )


async def _post(
    run: Run,
    gbp: InstrumentRecord,
    a: AccountRecord,
    b: AccountRecord,
    details: TransactionDetails | None = None,
) -> UUID:
    record = await run(
        post_transaction,
        TransactionInput(
            occurred_at=datetime(2026, 11, 10, 12, tzinfo=UTC),
            description="Interest",
            postings=[
                PostingInput(a.id, gbp.id, Decimal("5")),
                PostingInput(b.id, gbp.id, Decimal("-5")),
            ],
            details=details or TransactionDetails(),
        ),
    )
    return record.id


async def _marks(session: AsyncSession) -> list[tuple[str | None, str | None]]:
    rows = await session.execute(
        text("select category, income_kind from finance.transactions order by id")
    )
    return [(c, k) for c, k in rows]


async def _owner_rows(session: AsyncSession) -> int:
    return (
        await session.execute(text("select count(*) from finance.transaction_owner_categories"))
    ).scalar_one()


async def test_set_and_clear_leave_source_marks(
    session: AsyncSession, run: Run, gbp: InstrumentRecord
) -> None:
    a, b = await _accounts(run)
    transaction_id = await _post(
        run, gbp, a, b, TransactionDetails(category="income", income_kind="interest")
    )
    before = await _marks(session)
    assert before == [("income", "interest")]

    await run(set_owner_category, [transaction_id], "Family money")
    assert await known_owner_categories(session, "income", ()) == ["family_money"]
    assert await _marks(session) == before

    await run(set_owner_category, [transaction_id], "Gifts")
    assert await known_owner_categories(session, "income", ()) == ["gifts"]
    assert await _owner_rows(session) == 1

    await run(set_owner_category, [transaction_id], None)
    assert await _marks(session) == before
    assert await _owner_rows(session) == 0


async def test_void_transaction_refused(
    session: AsyncSession, run: Run, gbp: InstrumentRecord
) -> None:
    a, b = await _accounts(run)
    live = await _post(run, gbp, a, b)
    voided = await _post(run, gbp, a, b)
    await run(void_transaction, voided)
    with pytest.raises(FinanceValidationError, match=str(voided)):
        await run(set_owner_category, [live, voided], "salary")
    assert await _owner_rows(session) == 0


async def test_unknown_id_writes_nothing(
    session: AsyncSession, run: Run, gbp: InstrumentRecord
) -> None:
    a, b = await _accounts(run)
    live = await _post(run, gbp, a, b)
    with pytest.raises(TransactionNotFoundError):
        await run(set_owner_category, [live, uuid4()], "salary")
    assert await _owner_rows(session) == 0


async def test_decided_by_is_the_actor_type(
    session: AsyncSession, run: Run, gbp: InstrumentRecord
) -> None:
    a, b = await _accounts(run)
    await run(set_owner_category, [await _post(run, gbp, a, b)], "salary")
    assert (
        await session.execute(text("select decided_by from finance.transaction_owner_categories"))
    ).scalar_one() == "user"


async def _joined(session: AsyncSession, transaction_id: UUID) -> str | None:
    return (
        await session.execute(
            text(f"select {OWNER_CATEGORY_SQL} from finance.transactions t where t.id = :id"),
            {"id": transaction_id},
        )
    ).scalar_one()


async def test_sql_finds_the_category_for_a_connector_and_a_plain_transaction(
    session: AsyncSession, run: Run, gbp: InstrumentRecord
) -> None:
    a, b = await _accounts(run)
    connection = await run(create_connection, connector="monzo", display_name="M", config={})
    connector_record, _ = await run(
        upsert_connector_transaction,
        TransactionInput(
            occurred_at=datetime(2026, 11, 10, 12, tzinfo=UTC),
            description="Reward",
            origin="connector",
            connection_id=connection.id,
            external_id="tx_1",
            postings=[
                PostingInput(a.id, gbp.id, Decimal("5")),
                PostingInput(b.id, gbp.id, Decimal("-5")),
            ],
        ),
    )
    plain = await _post(run, gbp, a, b)
    other = await _post(run, gbp, a, b)
    await run(set_owner_category, [connector_record.id], "reward_x")
    await run(set_owner_category, [plain], "gifts")
    assert await _joined(session, connector_record.id) == "reward_x"
    assert await _joined(session, plain) == "gifts"
    assert await _joined(session, other) is None
    # The connector one is stored by its connection and external id, not its id.
    kinds = (
        await session.execute(
            text("select source_kind from finance.transaction_owner_categories order by 1")
        )
    ).scalars()
    assert list(kinds) == ["connection", "transaction"]


async def test_known_categories_per_side(
    session: AsyncSession, run: Run, gbp: InstrumentRecord
) -> None:
    a, income = await _accounts(run)
    shop = await run(
        create_account, name="Shop", ledger_type="expense", subtype="other", balance_mode="hybrid"
    )

    async def post(*postings: tuple[AccountRecord, str], refund: bool = False) -> UUID:
        record = await run(
            post_transaction,
            TransactionInput(
                occurred_at=datetime(2026, 11, 10, 12, tzinfo=UTC),
                description="",
                postings=[PostingInput(acc.id, gbp.id, Decimal(q)) for acc, q in postings],
                details=TransactionDetails(refund=refund),
            ),
        )
        return record.id

    pay = await post((a, "-5"), (shop, "5"))
    wage = await post((a, "5"), (income, "-5"))
    back = await post((a, "5"), (shop, "-5"), refund=True)
    own = await post((a, "-1"), (shop, "4"), (income, "-3"))  # both sides at once
    moved = await post((a, "-1"), (income, "1"), refund=True)  # a refund posts spending
    for transaction_id, name in [
        (pay, "groceries"),
        (wage, "salary"),
        (back, "returns"),
        (own, "mixed"),
        (moved, "moved"),
    ]:
        await run(set_owner_category, [transaction_id], name)
    # Not a flow at all (asset to asset), void, and uncategorised ones are on no side.
    between = await run(
        create_account, name="C", ledger_type="asset", subtype="bank", balance_mode="hybrid"
    )
    await run(set_owner_category, [await post((a, "1"), (between, "-1"))], "nowhere")
    voided = await post((a, "-4"), (shop, "4"))
    await run(set_owner_category, [voided], "voided")
    await run(void_transaction, voided)

    assert await known_owner_categories(session, "spending", ()) == [
        "groceries",
        "mixed",
        "moved",
        "returns",
    ]
    assert await known_owner_categories(session, "income", ()) == ["mixed", "salary"]
    # With C hidden, money from it is a move into the reports: income, never a refund.
    assert await known_owner_categories(session, "income", [between.id]) == [
        "mixed",
        "nowhere",
        "salary",
    ]
    # With A hidden, what it paid and received alone is wholly on the hidden side, and its
    # money to C a move out of the reports: spending.
    assert await known_owner_categories(session, "spending", [a.id]) == ["nowhere"]
    assert await known_owner_categories(session, "income", [a.id]) == []
