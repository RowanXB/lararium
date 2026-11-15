from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext
from lararium.core.documents import LocalDocumentStore
from lararium.modules.finance import models
from lararium.modules.finance.importing.profiles import (
    CsvColumns,
    CsvProfileConfig,
    ImportProfileRecord,
    create_profile,
)
from lararium.modules.finance.ledger import PostingInput, TransactionInput, post_transaction
from lararium.modules.finance.system_accounts import get_or_create_system_account

type Run = Callable[..., Awaitable[Any]]


@pytest.fixture
def store(tmp_path: Path) -> LocalDocumentStore:
    return LocalDocumentStore(tmp_path)


@pytest.fixture
async def csv_profile(run: Run) -> ImportProfileRecord:
    config = CsvProfileConfig(
        format="csv",
        columns=CsvColumns(date="Date", description=["Description"], amount="Amount"),
        date_format="%d/%m/%Y",
    )
    return await run(create_profile, name="Example Bank CSV", config=config)


async def _commit(ctx: CommandContext, batch_id: UUID) -> None:
    """A stand-in for Task 6's commit_batch: posts every new row and marks the batch committed."""
    session = ctx.session
    r, b = models.ImportRow, models.ImportBatch
    batch = (await session.execute(select(b.account_id).where(b.id == batch_id))).one()
    rows = (
        await session.execute(
            select(r).where(r.batch_id == batch_id, r.status == "new").order_by(r.position)
        )
    ).scalars()
    for row in list(rows):
        key = "opening_balances" if row.kind == "opening_adjustment" else "unassigned"
        contra = await get_or_create_system_account(ctx, key)
        assert row.quantity is not None and row.instrument_id is not None
        assert row.occurred_at is not None
        postings = [
            PostingInput(batch.account_id, row.instrument_id, row.quantity),
            PostingInput(contra, row.instrument_id, -(row.quantity - row.fee)),
        ]
        if row.fee:
            postings.insert(1, PostingInput(batch.account_id, row.instrument_id, -row.fee))
        tx = await post_transaction(
            ctx,
            TransactionInput(
                occurred_at=row.occurred_at,
                booking_date=row.booking_date,
                description=row.description,
                postings=postings,
            ),
        )
        await session.execute(
            update(r).where(r.id == row.id).values(status="committed", transaction_id=tx.id)
        )
    await session.execute(
        update(b).where(b.id == batch_id).values(status="committed", committed_at=func.now())
    )


@pytest.fixture
def commit(run: Run, session: AsyncSession) -> Callable[[UUID], Awaitable[None]]:
    async def _run_commit(batch_id: UUID) -> None:
        await run(_commit, batch_id)
        session.expunge_all()

    return _run_commit
