"""/api/v1/finance routes for transfers between own accounts and the registry of the
owner's account identifiers. Each route's name is its OpenAPI operationId.

Every route that changes identifiers, decisions or the ledger heals the transfers in its
own transaction (Decision 9), waiting at most LOCK_TIMEOUT for a lock: the matching lock
may be held by a sync's heal, which may itself wait on the rows a sync is writing.
"""

from collections.abc import AsyncGenerator, Collection
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.actors import Actor
from lararium.core.commands import CommandContext, command_context
from lararium.core.json_types import JSONValue
from lararium.core.settings_store import get_timezone
from lararium.deps import ActorDep, SessionDep, get_actor
from lararium.modules.finance import accounts, errors, identifiers, instruments, transfers
from lararium.modules.finance.schemas import (
    AccountOut,
    BalanceModeUpdate,
    CandidateOut,
    CardMerchantDismiss,
    CardMerchantRegister,
    CardMerchantSuggestionOut,
    IdentifierCreate,
    IdentifierOut,
    IdentifierReference,
    IdentifierReferencesRegister,
    MainAccountUpdate,
    PairCandidateOut,
    SuggestionOut,
    TransferOut,
    TransferPair,
)

# A request gives up (503 query_timeout) rather than wait longer for a lock.
LOCK_TIMEOUT = "5s"

router = APIRouter(prefix="/api/v1/finance", tags=["finance"], dependencies=[Depends(get_actor)])


def install(app: FastAPI) -> None:
    # The finance errors' handlers are installed by api.install.
    app.include_router(router)


@asynccontextmanager
async def healing_command(
    session: AsyncSession, actor: Actor, command: str, args: dict[str, JSONValue]
) -> AsyncGenerator[CommandContext]:
    """command_context, with lock waits bounded by LOCK_TIMEOUT and heal_transfers run at
    the end of the command, in its transaction. The matching lock is taken first, before
    the command locks any row: one order everywhere (an import's commit and rollback take
    it, then their account), so the two can't each wait on the other."""
    async with command_context(session, actor, command, args) as ctx:
        await session.execute(select(func.set_config("lock_timeout", LOCK_TIMEOUT, True)))
        await transfers.lock_transfers(ctx)
        yield ctx
        await transfers.heal_transfers(ctx)


def _args(body: JSONValue | None = None, **path: UUID) -> dict[str, JSONValue]:
    args: dict[str, JSONValue] = {key: str(value) for key, value in path.items()}
    if isinstance(body, dict):
        args |= body
    return args


# Identifiers


def _identifier_out(record: identifiers.IdentifierRecord) -> IdentifierOut:
    sort_code, account_number = (
        identifiers.uk_bank_account_parts(record.value)
        if record.kind == "uk_bank_account"
        else (None, None)
    )
    return IdentifierOut(
        id=record.id,
        kind=record.kind,
        value=record.value,
        sort_code=sort_code,
        account_number=account_number,
        account_id=record.account_id,
        holder_account_ids=list(record.holders),
        references=[
            IdentifierReference(account_id=account_id, reference=reference)
            for account_id, reference in record.references
        ],
        dismissed_at=record.dismissed_at,
        created_by=record.created_by,
        created_at=record.created_at,
    )


@router.get("/accounts/{account_id}/identifiers", name="list_account_identifiers")
async def list_account_identifiers(account_id: UUID, session: SessionDep) -> list[IdentifierOut]:
    await accounts.get_account(session, account_id)  # 404 when there is no such account
    found = await identifiers.list_identifiers(session, account_id=account_id)
    return [_identifier_out(record) for record in found]


@router.post(
    "/accounts/{account_id}/identifiers",
    name="add_account_identifier",
    status_code=status.HTTP_201_CREATED,
)
async def add_account_identifier(
    account_id: UUID, body: IdentifierCreate, session: SessionDep, actor: ActorDep
) -> IdentifierOut:
    """Register one of the account's identifiers (a suggestion, too): every record naming
    it is matched again."""
    args = _args(body.model_dump(mode="json"), account_id=account_id)
    async with healing_command(session, actor, "add_account_identifier", args) as ctx:
        record = await identifiers.assign_identifier(ctx, account_id, body.kind, body.value)
    return _identifier_out(record)


@router.delete(
    "/accounts/{account_id}/identifiers/{identifier_id}",
    name="remove_account_identifier",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_account_identifier(
    account_id: UUID, identifier_id: UUID, session: SessionDep, actor: ActorDep
) -> None:
    """Take the identifier off the account; the registry row stays, as transactions name
    it, and the records naming it are matched again."""
    args = _args(account_id=account_id, identifier_id=identifier_id)
    async with healing_command(session, actor, "remove_account_identifier", args) as ctx:
        await accounts.get_account(session, account_id)  # 404 when there is no such account
        held = [
            record
            for record in await identifiers.list_identifiers(session, account_id=account_id)
            if record.id == identifier_id and account_id in record.holders
        ]
        if held:
            # A card merchant: the account leaves its holders, the others stay.
            [merchant] = held
            await transfers.register_card_merchant_and_rematch(
                ctx,
                merchant.value,
                [a for a in merchant.holders if a != account_id],
                heal=False,  # healing_command heals once, at the end
            )
            return
        if await identifiers.identifier_account(session, identifier_id) != account_id:
            raise errors.AccountIdentifierNotFoundError(str(identifier_id))
        await transfers.unassign_identifier_and_rematch(ctx, identifier_id)


@router.post("/accounts/identifiers/card-merchants", name="register_card_merchant")
async def register_card_merchant(
    body: CardMerchantRegister, session: SessionDep, actor: ActorDep
) -> IdentifierOut:
    """Register a Monzo card merchant as paying into these own accounts (a suggestion, too),
    replacing the accounts it had; none removes the registration. Every record naming it is
    matched again; Monzo's next sync names it on the payments made before."""
    args = _args(body.model_dump(mode="json"))
    async with healing_command(session, actor, "register_card_merchant", args) as ctx:
        record = await transfers.register_card_merchant_and_rematch(
            ctx, body.merchant_id, body.account_ids, heal=False
        )
    return _identifier_out(record)


@router.post("/accounts/identifiers/references", name="register_identifier_references")
async def register_identifier_references(
    body: IdentifierReferencesRegister, session: SessionDep, actor: ActorDep
) -> IdentifierOut:
    """Register the own accounts a shared payee bank account pays into, each by the payment
    reference it is paid with, replacing the ones it had; none removes them. The bank account
    must be registered to no account. Every record naming it is matched again."""
    args = _args(body.model_dump(mode="json"))
    async with healing_command(session, actor, "register_identifier_references", args) as ctx:
        record = await transfers.register_references_and_rematch(
            ctx,
            body.value,
            [(reference.account_id, reference.reference) for reference in body.references],
            heal=False,  # healing_command heals once, at the end
        )
    return _identifier_out(record)


@router.post("/accounts/{account_id}/balance-mode", name="set_account_balance_mode")
async def set_account_balance_mode(
    account_id: UUID, body: BalanceModeUpdate, session: SessionDep, actor: ActorDep
) -> AccountOut:
    """Switch a snapshots account to hybrid, so it can carry identifiers."""
    args = _args(body.model_dump(mode="json"), account_id=account_id)
    async with healing_command(session, actor, "set_account_balance_mode", args) as ctx:
        record = await accounts.set_balance_mode(ctx, account_id, body.balance_mode)
    return AccountOut.model_validate(record)


@router.post("/accounts/{account_id}/main-account", name="set_main_account")
async def set_main_account(
    account_id: UUID, body: MainAccountUpdate, session: SessionDep, actor: ActorDep
) -> AccountOut:
    """Give a manual account a main account, or clear it; its transfers are matched anew."""
    args = _args(body.model_dump(mode="json"), account_id=account_id)
    async with healing_command(session, actor, "set_main_account", args) as ctx:
        record = await accounts.set_main_account(ctx, account_id, body.main_account_id)
    return AccountOut.model_validate(record)


# Transfers


async def _symbols(session: AsyncSession) -> dict[UUID, str]:
    return {
        instrument.id: instrument.symbol
        for instrument in await instruments.list_instruments(session, include_hidden=True)
    }


async def _transfers_out(
    session: AsyncSession, found: Collection[transfers.TransferRecord]
) -> list[TransferOut]:
    # Loaded even for no transfers, so a malformed setting shows on the Transfers page.
    settings = await transfers.load_transfer_settings(session)
    names = {account.id: account.name for account in await accounts.list_accounts(session)}
    symbols = await _symbols(session)
    now = datetime.now(UTC)
    timezone = await get_timezone(session)
    today = now.astimezone(timezone).date()
    weak = await transfers.weak_transfers(session, found)
    return [
        TransferOut(
            id=record.id,
            from_account_id=record.from_account_id,
            from_account_name=names[record.from_account_id],
            to_account_id=record.to_account_id,
            to_account_name=names[record.to_account_id],
            instrument_id=record.instrument_id,
            amount=record.amount,
            currency=symbols[record.instrument_id],
            status=record.status,
            review_reason=record.review_reason,
            opened_at=record.opened_at,
            matched_at=record.matched_at,
            days_in_transit=(
                (today - record.opened_at.astimezone(timezone).date()).days
                if record.status == "awaiting"
                else None
            ),
            overdue=transfers.is_overdue(record, now, settings),
            out_transaction_id=record.out_transaction_id,
            in_transaction_id=record.in_transaction_id,
            placeholder_transaction_id=record.placeholder_transaction_id,
            weak=record.id in weak,
        )
        for record in found
    ]


@router.get("/transfers", name="list_transfers")
async def list_transfers(
    session: SessionDep,
    transfer_status: Annotated[list[transfers.TransferStatus] | None, Query(alias="status")] = None,
) -> list[TransferOut]:
    """Transfers, newest first; only those of the given statuses when any are given."""
    found = await transfers.list_transfers(session, statuses=transfer_status)
    return await _transfers_out(session, found)


@router.get("/transfers/{transfer_id}/candidates", name="list_transfer_candidates")
async def list_transfer_candidates(transfer_id: UUID, session: SessionDep) -> list[CandidateOut]:
    """The own records that could be the transfer's missing side: those of the same amount
    first, then those of another, then those the owner unpaired from it (which pair can
    link again); empty for a transfer with both sides."""
    found = await transfers.candidates(session, transfer_id)
    names = {account.id: account.name for account in await accounts.list_accounts(session)}
    symbols = await _symbols(session)
    return [
        CandidateOut(
            transaction_id=c.transaction_id,
            account_id=c.account_id,
            account_name=names[c.account_id],
            quantity=c.quantity,
            currency=symbols[c.instrument_id],
            occurred_at=c.occurred_at,
            description=c.description,
            reason=c.reason,
        )
        for c in found
    ]


@router.post("/transfers/{transfer_id}/reject", name="reject_transfer")
async def reject_transfer(transfer_id: UUID, session: SessionDep, actor: ActorDep) -> TransferOut:
    """The owner says it is not a transfer: its records go back to Unassigned."""
    args = _args(transfer_id=transfer_id)
    async with healing_command(session, actor, "reject_transfer", args) as ctx:
        record = await transfers.reject(ctx, transfer_id)
    [out] = await _transfers_out(session, [record])
    return out


@router.post(
    "/transfers/{transfer_id}/unreject",
    name="unreject_transfer",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unreject_transfer(transfer_id: UUID, session: SessionDep, actor: ActorDep) -> None:
    """Withdraw a rejection; its records are matched again (under a new transfer id)."""
    args = _args(transfer_id=transfer_id)
    async with healing_command(session, actor, "unreject_transfer", args) as ctx:
        await transfers.unreject(ctx, transfer_id)


@router.post(
    "/transfers/{transfer_id}/unpair",
    name="unpair_transfer",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unpair_transfer(transfer_id: UUID, session: SessionDep, actor: ActorDep) -> None:
    """The owner says the matched transfer's two records are not one transfer; each is
    matched again, never with the other."""
    args = _args(transfer_id=transfer_id)
    async with healing_command(session, actor, "unpair_transfer", args) as ctx:
        await transfers.unpair(ctx, transfer_id)


@router.post("/transfers/{transfer_id}/pair", name="pair_transfer")
async def pair_transfer(
    transfer_id: UUID, body: TransferPair, session: SessionDep, actor: ActorDep
) -> TransferOut:
    """The owner picks the transfer's missing side among its candidates, or one they
    unpaired from it. Two currencies may differ in currency or in amount (settled by a
    conversion, or the difference a fee); anything else must be of the same instrument and
    amount."""
    args = _args(body.model_dump(mode="json"), transfer_id=transfer_id)
    async with healing_command(session, actor, "pair_transfer", args) as ctx:
        record = await transfers.pair(ctx, transfer_id, body.transaction_id)
    [out] = await _transfers_out(session, [record])
    return out


@router.get(
    "/transactions/{transaction_id}/pair-candidates", name="list_transaction_pair_candidates"
)
async def list_transaction_pair_candidates(
    transaction_id: UUID, session: SessionDep
) -> list[PairCandidateOut]:
    """The own records the record could be paired with as one transfer, on another account:
    those of the opposite and equal amount first, then those in a currency whose side
    differs (`differs`: in currency or in amount) booked within 7 days; each part nearest
    booking date first, at most 20 in all."""
    found = await transfers.pair_candidates(session, transaction_id)
    names = {account.id: account.name for account in await accounts.list_accounts(session)}
    symbols = await _symbols(session)
    return [
        PairCandidateOut(
            transaction_id=c.transaction_id,
            account_id=c.account_id,
            account_name=names[c.account_id],
            quantity=c.quantity,
            currency=symbols[c.instrument_id],
            occurred_at=c.occurred_at,
            booking_date=c.booking_date,
            description=c.description,
            differs=c.differs,
        )
        for c in found
    ]


@router.post("/transactions/{transaction_id}/pair", name="pair_transactions")
async def pair_transactions(
    transaction_id: UUID, body: TransferPair, session: SessionDep, actor: ActorDep
) -> TransferOut:
    """The owner says two own records are one transfer, whether or not either is in one:
    in one instrument of opposite, equal amounts, or two currencies of opposite sign that
    differ in currency or in amount (the receiving side not larger). Undone by unpairing
    the resulting transfer."""
    # _args merges the body over the path, so its own transaction_id would hide the path's.
    args: dict[str, JSONValue] = {
        "transaction_id": str(transaction_id),
        "other_transaction_id": str(body.transaction_id),
    }
    async with healing_command(session, actor, "pair_transactions", args) as ctx:
        record = await transfers.pair_records(ctx, transaction_id, body.transaction_id)
    [out] = await _transfers_out(session, [record])
    return out


# Suggestions


@router.get("/transfer-suggestions", name="list_transfer_suggestions")
async def list_transfer_suggestions(
    session: SessionDep,
) -> list[SuggestionOut | CardMerchantSuggestionOut]:
    """Bank accounts that own records name and that are neither registered nor dismissed,
    most named first (registering one is add_account_identifier); then card merchants on a
    connected platform's domain (kind card_merchant; registering one is
    register_card_merchant)."""
    banks: list[SuggestionOut | CardMerchantSuggestionOut] = [
        SuggestionOut.model_validate(s) for s in await transfers.suggestions(session)
    ]
    return banks + [
        CardMerchantSuggestionOut.model_validate(s)
        for s in await transfers.card_merchant_suggestions(session)
    ]


# Before dismiss_transfer_suggestion, whose {identifier_id} would otherwise take this path.
@router.post(
    "/transfer-suggestions/card-merchants/dismiss",
    name="dismiss_card_merchant_suggestion",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def dismiss_card_merchant_suggestion(
    body: CardMerchantDismiss, session: SessionDep, actor: ActorDep
) -> None:
    """The owner says the card merchant pays into none of their accounts."""
    args = _args(body.model_dump(mode="json"))
    async with healing_command(session, actor, "dismiss_card_merchant_suggestion", args) as ctx:
        await identifiers.dismiss_card_merchant(ctx, body.merchant_id)


@router.post(
    "/transfer-suggestions/{identifier_id}/dismiss",
    name="dismiss_transfer_suggestion",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def dismiss_transfer_suggestion(
    identifier_id: UUID, session: SessionDep, actor: ActorDep
) -> None:
    """The owner says the bank account is not one of theirs."""
    args = _args(identifier_id=identifier_id)
    async with healing_command(session, actor, "dismiss_transfer_suggestion", args) as ctx:
        await identifiers.dismiss_identifier(ctx, identifier_id)
