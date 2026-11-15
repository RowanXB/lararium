"""The owner's decisions about transfers (design §5.9): this is not a transfer (reject),
these two are not one transfer (unpair), and these two are (pair).

Decisions are keyed by the records' source references, never by the transfer: matching
dissolves and reopens transfers (a new id each time), while a record's reference holds
across re-syncs. So each function finds the transfer's records when it is called, stores
what it decides about them, and lets matching bring the ledger in line."""

from uuid import UUID

from sqlalchemy import and_, delete, or_, update
from sqlalchemy.dialects.postgresql import insert

from lararium.core.commands import CommandContext
from lararium.modules.finance import models
from lararium.modules.finance.errors import FinanceValidationError
from lararium.modules.finance.ledger import delete_derived_transaction
from lararium.modules.finance.transfers.contra import source_ref
from lararium.modules.finance.transfers.engine import (
    decision_change_key,
    forget_decisions,
    heal_transfers,
    lock_transfers,
    match_transaction,
    publish_transfer,
)
from lararium.modules.finance.transfers.queries import (
    OPEN_STATUSES,
    TransferRecord,
    candidates,
    get_transfer,
    load_transfers,
)


def _records(transfer: TransferRecord) -> list[UUID]:
    """The transfer's own records (not its placeholder)."""
    return [
        leg for leg in (transfer.out_transaction_id, transfer.in_transaction_id) if leg is not None
    ]


async def _store(
    ctx: CommandContext,
    kind: str,
    subject: tuple[str, str],
    other: tuple[str, str] | None = None,
) -> None:
    """Store a decision, unless the same one is stored already."""
    other_kind, other_ref = (None, None) if other is None else other
    d = models.TransferDecision
    stored = await ctx.session.scalar(
        insert(d)
        .values(
            kind=kind,
            subject_kind=subject[0],
            subject_ref=subject[1],
            other_kind=other_kind,
            other_ref=other_ref,
        )
        .on_conflict_do_nothing()
        .returning(d.id)
    )
    if stored is not None:
        ctx.record_change(
            decision_change_key(kind, subject, (other_kind, other_ref)),
            {"before": None, "after": {"id": str(stored), "kind": kind}},
        )


async def reject(ctx: CommandContext, transfer_id: UUID) -> TransferRecord:
    """The owner says the transfer is not one: a reject decision for each of its own
    records, so neither opens a transfer again; the transfer stays, rejected, without its
    placeholder or settlement, and its records go back to Unassigned."""
    await lock_transfers(ctx)
    session = ctx.session
    transfer = await get_transfer(session, transfer_id)
    if transfer.status == "rejected":
        raise FinanceValidationError("the transfer is already rejected")
    records = _records(transfer)
    for record_id in records:
        await _store(ctx, "reject", await source_ref(session, record_id))
    tr = models.Transfer
    await session.execute(
        update(tr)
        .where(tr.id == transfer_id)
        .values(
            status="rejected",
            review_reason=None,
            placeholder_transaction_id=None,
            settlement_transaction_id=None,
        )
    )
    for derived in (transfer.placeholder_transaction_id, transfer.settlement_transaction_id):
        if derived is not None:
            await delete_derived_transaction(ctx, derived)
    # Matching a rejected record puts its contra back on Unassigned.
    for record_id in records:
        await match_transaction(ctx, record_id)
    rejected = await get_transfer(session, transfer_id)
    publish_transfer(ctx, "finance.transfer.rejected", transfer_id, transfer, rejected)
    await heal_transfers(ctx)
    return await get_transfer(session, transfer_id)


async def unreject(ctx: CommandContext, transfer_id: UUID) -> None:
    """Withdraw a rejection: its decisions and the rejected transfer go, and its records
    are matched again."""
    await lock_transfers(ctx)
    session = ctx.session
    transfer = await get_transfer(session, transfer_id)
    if transfer.status != "rejected":
        raise FinanceValidationError("only a rejected transfer can be unrejected")
    records = _records(transfer)
    for record_id in records:
        await forget_decisions(ctx, "reject", await source_ref(session, record_id))
    tr = models.Transfer
    await session.execute(delete(tr).where(tr.id == transfer_id))
    publish_transfer(ctx, "finance.transfer.unrejected", transfer_id, transfer, None)
    for record_id in records:
        await match_transaction(ctx, record_id)
    await heal_transfers(ctx)


async def unpair(ctx: CommandContext, transfer_id: UUID) -> None:
    """The owner says the matched transfer's two records are not one transfer, though each
    may still be one: never pair them (both ways round), and match each again."""
    await lock_transfers(ctx)
    session = ctx.session
    transfer = await get_transfer(session, transfer_id)
    if transfer.status != "matched" or len(_records(transfer)) != 2:
        raise FinanceValidationError("only a matched transfer of two own records can be unpaired")
    sent, received = _records(transfer)
    sent_ref, received_ref = await source_ref(session, sent), await source_ref(session, received)
    await forget_decisions(ctx, "pair", sent_ref, received_ref)
    await _store(ctx, "never_pair", sent_ref, received_ref)
    await _store(ctx, "never_pair", received_ref, sent_ref)
    # Matching the sender dissolves the transfer, and matches the receiver after it.
    await match_transaction(ctx, sent)
    await heal_transfers(ctx)


async def pair(ctx: CommandContext, transfer_id: UUID, transaction_id: UUID) -> TransferRecord:
    """The owner says the open transfer's missing side is this record: one of its
    candidates of the same amount, or one the owner unpaired from it (whose never_pair
    decisions this withdraws). Pairing them outranks any other candidate; when it still
    can't link them (another pair decision makes one of them ambiguous), nothing is
    stored."""
    await lock_transfers(ctx)
    session = ctx.session
    transfer = await get_transfer(session, transfer_id)
    if transfer.status not in OPEN_STATUSES:
        raise FinanceValidationError("only a transfer missing one side can be paired")
    found = {
        c.reason
        for c in await candidates(session, transfer_id)
        if c.transaction_id == transaction_id
    }
    if not found & {"exact", "unpaired"}:
        raise FinanceValidationError(
            f"transaction {transaction_id} is not a candidate of the same amount for this transfer"
        )
    [present] = _records(transfer)
    present_ref, chosen_ref = (
        await source_ref(session, present),
        await source_ref(session, transaction_id),
    )
    await forget_decisions(ctx, "never_pair", present_ref, chosen_ref)
    await _store(ctx, "pair", present_ref, chosen_ref)
    await match_transaction(ctx, present)
    await heal_transfers(ctx)
    tr = models.Transfer
    paired = await load_transfers(
        session,
        tr.status == "matched",
        or_(
            and_(tr.out_transaction_id == present, tr.in_transaction_id == transaction_id),
            and_(tr.out_transaction_id == transaction_id, tr.in_transaction_id == present),
        ),
    )
    if not paired:
        # Raising rolls the decisions back with the rest of the command.
        raise FinanceValidationError(
            f"pairing with transaction {transaction_id} did not take effect: another pair"
            " decision leaves it ambiguous"
        )
    return paired[0]
