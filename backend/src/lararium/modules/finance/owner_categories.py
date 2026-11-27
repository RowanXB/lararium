"""The owner's own category for a transaction (income-categories design §4.1, §4.2).

It is kept apart from the source's marks and keyed by the transaction's stable source
reference (transfers.contra.source_columns), as the owner's transfer decisions are, so it
survives a rollback and re-import or a connector replay. Setting or clearing it never
touches `transactions.category` or `income_kind`.

Reports read it through OWNER_CATEGORY_SQL: a scalar subquery, derived from
`source_columns` once at import time, that yields the owner's category (or NULL) for the
finance.transactions row aliased `t` in the query it is spliced into. Splice it into the
select list or a COALESCE of a raw `text()` query; the same SQL is the join condition
(source_kind, source_ref) = source_columns(t), so there is no second definition of the
reference."""

import re
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from lararium.core.commands import CommandContext
from lararium.modules.finance import models
from lararium.modules.finance.errors import FinanceValidationError, TransactionNotFoundError
from lararium.modules.finance.transfers.contra import source_columns

MAX_CATEGORY_LENGTH = 64


def normalise_category(text: str) -> str:
    """Trim, collapse inner whitespace runs to `_`, lower-case; 1 to 64 characters."""
    category = re.sub(r"\s+", "_", text.strip()).lower()
    if not 1 <= len(category) <= MAX_CATEGORY_LENGTH:
        raise FinanceValidationError(
            f"a category is 1 to {MAX_CATEGORY_LENGTH} characters after trimming"
        )
    return category


def _owner_category_sql() -> str:
    t = aliased(models.Transaction, name="t")
    kind, ref = source_columns(t)

    def sql(expression: ColumnElement[str]) -> str:
        return str(
            expression.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        )

    table = f"finance.{models.TransactionOwnerCategory.__tablename__}"
    return (
        f"select owner_category.category from {table} owner_category"
        f" where owner_category.source_kind = {sql(kind)}"
        f" and owner_category.source_ref = {sql(ref)}"
    )


OWNER_CATEGORY_SQL = f"({_owner_category_sql()})"


async def set_owner_category(
    ctx: CommandContext, transaction_ids: Sequence[UUID], category: str | None
) -> None:
    """Give the transactions the owner's category, or clear it with None. All or nothing:
    an unknown id raises TransactionNotFoundError, a void transaction
    FinanceValidationError, and nothing is written."""
    session = ctx.session
    normalised = None if category is None else normalise_category(category)
    t = models.Transaction
    kind, ref = source_columns(t)
    ids = list(dict.fromkeys(transaction_ids))
    rows = (await session.execute(select(t.id, t.status, kind, ref).where(t.id.in_(ids)))).all()
    found = {row[0]: row for row in rows}
    for transaction_id in ids:
        if transaction_id not in found:
            raise TransactionNotFoundError(str(transaction_id))
    for transaction_id in ids:
        if found[transaction_id][1] == "void":
            raise FinanceValidationError(
                f"transaction {transaction_id} is void, so it cannot be categorised"
            )
    sources = {(row[2], row[3]) for row in rows}
    o = models.TransactionOwnerCategory
    if normalised is None:
        for source_kind, source_ref in sources:
            await session.execute(
                delete(o).where(o.source_kind == source_kind, o.source_ref == source_ref)
            )
    else:
        statement = insert(o).values(
            [
                {
                    "source_kind": source_kind,
                    "source_ref": source_ref,
                    "category": normalised,
                    "decided_by": ctx.actor.type,
                }
                for source_kind, source_ref in sorted(sources)
            ]
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[o.source_kind, o.source_ref],
                set_={
                    "category": statement.excluded.category,
                    "decided_at": func.now(),
                    "decided_by": statement.excluded.decided_by,
                },
            )
        )
    ctx.record_change("owner_category", normalised)
    ctx.record_change("transaction_count", len(ids))


async def known_owner_categories(session: AsyncSession) -> list[str]:
    """Every owner category in use, sorted."""
    o = models.TransactionOwnerCategory
    return list(await session.scalars(select(o.category).distinct().order_by(o.category)))
