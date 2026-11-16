"""Commit, discard and roll back staged import batches.

Committing turns a batch's new rows into transactions through the ledger; rolling back
voids them (Decision 7). Both re-align the account's opening adjustments (Decision 13), and
both go through M4's transfer matching (its design §5.8): a row's contra comes from
transfer_contra, its counterparty fields are the transaction's, and heal_transfers pairs
what the commit or the rollback changed.
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
    FinanceValidationError,
    ImportBatchNotFoundError,
    ImportBatchStaleError,
    ImportBatchStateError,
    ImportHasErrorsError,
)
from lararium.modules.finance.identifiers import (
    ensure_identifier,
    get_or_create_identifier,
    normalize_identifier,
)
from lararium.modules.finance.importing.profiles import is_bank_transfer
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
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    post_transaction,
    void_transaction,
)
from lararium.modules.finance.system_accounts import get_or_create_system_account
from lararium.modules.finance.transfers import (
    heal_transfers,
    lock_transfers,
    transfer_contra,
    unassign_identifier_and_rematch,
    unlink_transaction,
)

STALE = "the account changed since this file was uploaded; discard it and upload again"


def _state_error(filename: str, status: str, action: str) -> ImportBatchStateError:
    return ImportBatchStateError(
        f"the import of {filename} is {status.replace('_', ' ')}, so it can't be {action}"
    )


async def _locked(ctx: CommandContext, batch_id: UUID, allowed: BatchStatus, action: str) -> UUID:
    """Locks M4's matching, then the batch's account, then the batch, and returns the
    account id; ImportBatchStateError unless the batch's status is `allowed`.

    One order everywhere. Matching first (lock_transfers): commit and rollback match at
    their end, and matching posts and removes placeholders on manual accounts, whose foreign
    keys would otherwise wait on this account's lock while this waits on matching's. Then
    the account: its lock serialises every action on the account's batches, and
    align_openings, which runs under it, may insert rows into the account's other batches.
    The batch is locked FOR NO KEY UPDATE, which doesn't conflict with the KEY SHARE such an
    insert's foreign key takes.
    """
    session = ctx.session
    b, a = models.ImportBatch, models.Account
    await lock_transfers(ctx)
    account_id = await session.scalar(select(b.account_id).where(b.id == batch_id))
    if account_id is None:
        raise ImportBatchNotFoundError(str(batch_id))
    await session.execute(select(a.id).where(a.id == account_id).with_for_update())
    found = (
        await session.execute(
            select(b.status, b.filename).where(b.id == batch_id).with_for_update(key_share=True)
        )
    ).one()
    if found.status != allowed:
        raise _state_error(found.filename, found.status, action)
    return account_id  # a batch's account never changes


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
    if preview.conflicts:  # another file committed since staging disagrees (final review C1)
        raise ImportBatchStaleError(STALE)
    needed = {i: need for i, need in preview.needed.items() if need != 0}
    if staged != needed:
        raise ImportBatchStaleError(STALE)


async def _card_types(session: AsyncSession, batch_id: UUID) -> list[str]:
    """The batch's profile's card types (as it was at staging), for is_bank_transfer."""
    b = models.ImportBatch
    config = await session.scalar(select(b.profile_config).where(b.id == batch_id))
    assert config is not None
    found = config.get("card_types", [])
    return [str(code) for code in found] if isinstance(found, list) else []


async def _own_account(session: AsyncSession, batch_id: UUID) -> str | None:
    """The UK bank account the batch's statement says it is for, as the registry keeps it."""
    s = models.ImportStatement
    found = (
        await session.execute(select(s.sort_code, s.account_number).where(s.batch_id == batch_id))
    ).one_or_none()
    if found is None or found.sort_code is None or found.account_number is None:
        return None
    try:
        return normalize_identifier("uk_bank_account", f"{found.sort_code}:{found.account_number}")
    except FinanceValidationError:
        return None  # not a UK account number (a longer BOC one, say): nothing to register


async def _register_own_account(ctx: CommandContext, batch_id: UUID, account_id: UUID) -> None:
    """The bank account the statement says it is for, registered to the account it was
    imported into, as a connector registers its own accounts (M4, design §5.3): transfers
    naming it are then recognised. One registered already stays where it is."""
    value = await _own_account(ctx.session, batch_id)
    if value is not None:
        await ensure_identifier(ctx, account_id, "uk_bank_account", value)


async def _unregister_own_account(ctx: CommandContext, batch_id: UUID, account_id: UUID) -> None:
    """The registration follows the statements that show it: once no committed import of
    the account names that bank account (a statement imported into the wrong account,
    rolled back), it comes off, and the next import naming it registers it again."""
    value = await _own_account(ctx.session, batch_id)
    if value is None:
        return
    b, i = models.ImportBatch, models.AccountIdentifier
    identifier_id = await ctx.session.scalar(
        select(i.id).where(
            i.kind == "uk_bank_account", i.value == value, i.account_id == account_id
        )
    )
    if identifier_id is None:
        return
    others = await ctx.session.scalars(
        select(b.id).where(b.account_id == account_id, b.status == "committed", b.id != batch_id)
    )
    if all([await _own_account(ctx.session, other) != value for other in others]):
        await unassign_identifier_and_rematch(ctx, identifier_id)


async def _counterparty(
    ctx: CommandContext, row: models.ImportRow, card_types: list[str]
) -> CounterpartyFields:
    """The transaction's counterparty fields from the row's typed ones (M4, design §5.4):
    the other side's UK bank account through the registry, its name, the payment
    reference, and whether the bank marks the row a bank transfer."""
    identifier_id: UUID | None = None
    if row.counterparty_sort_code is not None and row.counterparty_account_number is not None:
        try:
            identifier_id = await get_or_create_identifier(
                ctx,
                "uk_bank_account",
                f"{row.counterparty_sort_code}:{row.counterparty_account_number}",
            )
        except FinanceValidationError:
            pass  # not a UK account number: it stays on the row only
    return CounterpartyFields(
        identifier_id=identifier_id,
        name=row.counterparty_name,
        reference=row.payment_reference,
        bank_transfer=is_bank_transfer(card_types, row.bank_type),
    )


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
    account_id = await _locked(ctx, batch_id, "staged", "committed")
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

    await get_or_create_system_account(ctx, "unassigned")
    await get_or_create_system_account(ctx, "in_transit")  # transfer_contra needs both
    await _register_own_account(ctx, batch_id, account_id)
    card_types = await _card_types(session, batch_id)
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
        # No per-row key lookup: recheck_duplicates already turned the rows whose key a
        # committed row has into duplicates, under the account lock, and a file can't repeat
        # a key. import_rows_committed_once is the database backstop.
        counterparty = CounterpartyFields()
        if row.kind == "opening_adjustment":
            if opening_id is None:
                opening_id = await get_or_create_system_account(ctx, "opening_balances")
            postings = _postings(account_id, opening_id, row.instrument_id, row.quantity, row.fee)
        else:
            counterparty = await _counterparty(ctx, row, card_types)
            contra_id = await transfer_contra(
                session,
                source=("import", row.dedupe_key) if row.dedupe_key else None,
                own_account_id=account_id,
                counterparty_identifier_id=counterparty.identifier_id,
                other_postings=0 if row.fee == 0 else 1,
            )
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
                counterparty=counterparty,
            ),
        )
        await session.execute(
            update(r)
            .where(r.id == row.id)
            .values(status="committed", transaction_id=transaction.id)
        )
    b = models.ImportBatch
    # The time now, under the account lock, not now() (the transaction's start): the walk
    # orders ties by commit time, which must be the order the lock let the commits through.
    await session.execute(
        update(b)
        .where(b.id == batch_id)
        .values(status="committed", committed_at=func.clock_timestamp())
    )
    # With this batch committed, the others' adjustments follow (Decision 13).
    await align_openings(ctx, account_id)
    # Pair the new rows with the transfers they are a side of, replacing placeholders, and
    # whatever else the commit changed (M4, design §5.8).
    await heal_transfers(ctx)
    record = await get_batch(session, batch_id)
    _publish(ctx, "finance.import.committed", record)
    return record


async def discard_batch(ctx: CommandContext, batch_id: UUID) -> ImportBatchRecord:
    session = ctx.session
    await _locked(ctx, batch_id, "staged", "discarded")
    b = models.ImportBatch
    await session.execute(update(b).where(b.id == batch_id).values(status="discarded"))
    record = await get_batch(session, batch_id)
    _publish(ctx, "finance.import.discarded", record)
    return record


async def rollback_batch(ctx: CommandContext, batch_id: UUID) -> ImportBatchRecord:
    session = ctx.session
    account_id = await _locked(ctx, batch_id, "committed", "rolled back")
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
        # No longer a transfer's leg first: its transfer gets its placeholder back.
        await unlink_transaction(ctx, transaction_id)
        await void_transaction(ctx, transaction_id)  # leaves an already void one alone
        await session.execute(update(r).where(r.id == row_id).values(status="rolled_back"))
    b = models.ImportBatch
    await session.execute(
        update(b).where(b.id == batch_id).values(status="rolled_back", rolled_back_at=func.now())
    )
    # Without this batch, the remaining batches' adjustments follow (Decision 13).
    await align_openings(ctx, account_id)
    await _unregister_own_account(ctx, batch_id, account_id)
    await heal_transfers(ctx)
    record = await get_batch(session, batch_id)
    _publish(ctx, "finance.import.rolled_back", record)
    return record
