"""Commit, discard and roll back staged import batches.

Committing turns a batch's new rows into transactions through the ledger; rolling back
voids them (Decision 7). Both re-align the account's opening adjustments (Decision 13).
"""

from decimal import MAX_PREC, Decimal, localcontext
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext
from lararium.core.json_types import JSONValue
from lararium.modules.finance import models
from lararium.modules.finance.accounts import get_account
from lararium.modules.finance.errors import (
    ImportBatchNotFoundError,
    ImportBatchStaleError,
    ImportBatchStateError,
    ImportHasErrorsError,
)
from lararium.modules.finance.importing.staging import (
    BatchStatus,
    ImportBatchRecord,
    align_openings,
    batches_relying_on,
    get_batch,
    preview_openings,
    recheck_duplicates,
    recheck_statement_account,
    require_importable,
)
from lararium.modules.finance.ledger import (
    PostingInput,
    TransactionInput,
    post_transaction,
    void_transaction,
)
from lararium.modules.finance.system_accounts import get_or_create_system_account

STALE = "the account changed since this file was uploaded; discard it and upload again"


def _state_error(filename: str, status: str, action: str) -> ImportBatchStateError:
    return ImportBatchStateError(
        f"the import of {filename} is {status.replace('_', ' ')}, so it can't be {action}"
    )


async def _locked(session: AsyncSession, batch_id: UUID, allowed: BatchStatus, action: str) -> UUID:
    """Locks the batch, then its account, so the batches of one account act one at a time;
    returns the account id. ImportBatchStateError unless the batch's status is `allowed`."""
    b = models.ImportBatch
    found = (
        await session.execute(
            select(b.account_id, b.status, b.filename).where(b.id == batch_id).with_for_update()
        )
    ).one_or_none()
    if found is None:
        raise ImportBatchNotFoundError(str(batch_id))
    a = models.Account
    await session.execute(select(a.id).where(a.id == found.account_id).with_for_update())
    if found.status != allowed:
        raise _state_error(found.filename, found.status, action)
    return found.account_id


def _counts(record: ImportBatchRecord) -> dict[str, JSONValue]:
    return {status: n for status, n in record.counts.items()}


def _publish(ctx: CommandContext, event_type: str, record: ImportBatchRecord) -> None:
    counts = _counts(record)
    ctx.emit(event_type, record.id, {"batch_id": str(record.id), "counts": counts})
    ctx.record_change(f"import_batch:{record.id}", {"status": record.status, "counts": counts})


async def _check_openings(session: AsyncSession, batch_id: UUID) -> None:
    """Decision 13: the adjustments staged must still be the ones the walk gives now."""
    r = models.ImportRow
    staged = {
        instrument_id: quantity
        for instrument_id, quantity in await session.execute(
            select(r.instrument_id, r.quantity).where(
                r.batch_id == batch_id, r.kind == "opening_adjustment", r.status == "new"
            )
        )
    }
    preview = await preview_openings(session, batch_id)
    needed = {i: need for i, need in preview.needed.items() if need != 0}
    if staged != needed:
        raise ImportBatchStaleError(STALE)


def _postings(
    account_id: UUID, contra_id: UUID, instrument_id: UUID, quantity: Decimal, fee: Decimal
) -> list[PostingInput]:
    """The account +quantity, the account -fee (a fee, Decision 11), the contra account the
    rest; zero legs are left out, as postings can't be zero."""
    with localcontext(prec=MAX_PREC):
        charged, contra = -fee, fee - quantity
    legs = [
        PostingInput(account_id, instrument_id, quantity),
        PostingInput(account_id, instrument_id, charged, is_fee=True),
        PostingInput(contra_id, instrument_id, contra),
    ]
    return [leg for leg in legs if leg.quantity != 0]


async def commit_batch(ctx: CommandContext, batch_id: UUID) -> ImportBatchRecord:
    session = ctx.session
    account_id = await _locked(session, batch_id, "staged", "committed")
    # The account's mode may have changed since staging.
    account = await get_account(session, account_id)
    require_importable(account)
    await recheck_statement_account(session, batch_id)
    r = models.ImportRow
    if await session.scalar(
        select(func.count()).where(r.batch_id == batch_id, r.status == "error")
    ):
        raise ImportHasErrorsError(str(batch_id))
    # A batch committed since staging may now cover this one's days (Decision 16).
    if await recheck_duplicates(session, batch_id):
        raise ImportBatchStaleError(STALE)
    # Whatever the mode: a staged adjustment on an account that no longer takes them is stale.
    await _check_openings(session, batch_id)

    contra_id = await get_or_create_system_account(ctx, "unassigned")
    opening_id: UUID | None = None
    rows = list(
        await session.scalars(
            select(r)
            .where(r.batch_id == batch_id, r.status == "new")
            .order_by(r.position)
            .execution_options(populate_existing=True)
        )
    )
    for row in rows:
        # New rows have these (the table's CHECK).
        assert row.booking_date is not None and row.occurred_at is not None
        assert row.quantity is not None and row.instrument_id is not None
        original = await session.scalar(
            select(r.id).where(
                r.account_id == account_id,
                r.status == "committed",
                r.dedupe_key == row.dedupe_key,
            )
        )
        if original is not None:
            await session.execute(
                update(r).where(r.id == row.id).values(status="duplicate", duplicate_of=original)
            )
            continue
        if row.kind == "opening_adjustment":
            if opening_id is None:
                opening_id = await get_or_create_system_account(ctx, "opening_balances")
            postings = _postings(account_id, opening_id, row.instrument_id, row.quantity, row.fee)
        else:
            postings = _postings(account_id, contra_id, row.instrument_id, row.quantity, row.fee)
        transaction = await post_transaction(
            ctx,
            TransactionInput(
                occurred_at=row.occurred_at,
                occurred_tz=row.occurred_tz,
                booking_date=row.booking_date,
                value_date=row.value_date,
                description=row.description,
                postings=postings,
            ),
        )
        await session.execute(
            update(r)
            .where(r.id == row.id)
            .values(status="committed", transaction_id=transaction.id)
        )
    b = models.ImportBatch
    await session.execute(
        update(b).where(b.id == batch_id).values(status="committed", committed_at=func.now())
    )
    # With this batch committed, the others' adjustments follow (Decision 13).
    await align_openings(ctx, account_id)
    record = await get_batch(session, batch_id)
    _publish(ctx, "finance.import.committed", record)
    return record


async def discard_batch(ctx: CommandContext, batch_id: UUID) -> ImportBatchRecord:
    session = ctx.session
    await _locked(session, batch_id, "staged", "discarded")
    b = models.ImportBatch
    await session.execute(update(b).where(b.id == batch_id).values(status="discarded"))
    record = await get_batch(session, batch_id)
    _publish(ctx, "finance.import.discarded", record)
    return record


async def rollback_batch(ctx: CommandContext, batch_id: UUID) -> ImportBatchRecord:
    session = ctx.session
    account_id = await _locked(session, batch_id, "committed", "rolled back")
    relying = await batches_relying_on(session, batch_id)
    if relying:
        names = ", ".join(filename for _, filename in relying)
        raise ImportBatchStateError(
            f"{names} skipped rows this import holds; roll back {names} first"
        )
    r = models.ImportRow
    rows = await session.execute(
        select(r.id, r.transaction_id).where(r.batch_id == batch_id, r.status == "committed")
    )
    for row_id, transaction_id in list(rows):
        assert transaction_id is not None  # committed rows have one (the table's CHECK)
        await void_transaction(ctx, transaction_id)  # leaves an already void one alone
        await session.execute(update(r).where(r.id == row_id).values(status="rolled_back"))
    b = models.ImportBatch
    await session.execute(
        update(b).where(b.id == batch_id).values(status="rolled_back", rolled_back_at=func.now())
    )
    # Without this batch, the remaining batches' adjustments follow (Decision 13).
    await align_openings(ctx, account_id)
    record = await get_batch(session, batch_id)
    _publish(ctx, "finance.import.rolled_back", record)
    return record
