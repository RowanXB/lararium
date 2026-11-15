"""The registry of the owner's account identifiers (a UK sort code and account number, for
now): a counterparty's details that resolve to one of them make a transaction a transfer
between the owner's own accounts."""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import ColumnElement, delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext
from lararium.core.json_types import JSONValue
from lararium.modules.finance import models
from lararium.modules.finance.accounts import get_account
from lararium.modules.finance.errors import (
    AccountIdentifierNotFoundError,
    DuplicateAccountIdentifierError,
    FinanceValidationError,
)

logger = logging.getLogger(__name__)

IdentifierKind = Literal["uk_bank_account"]

_UK_BANK_ACCOUNT = re.compile(r"([0-9]{6})[:/]?([0-9]{8})")


@dataclass(frozen=True)
class IdentifierRecord:
    id: UUID
    account_id: UUID
    kind: IdentifierKind
    value: str
    created_by: str
    created_at: datetime


def normalize_identifier(kind: IdentifierKind, value: str) -> str:
    """The stored form: `<6-digit sort code>:<8-digit account number>`."""
    # Spaces and dashes go, so whitespace between the two parts works as a separator too.
    found = _UK_BANK_ACCOUNT.fullmatch(re.sub(r"[\s-]", "", value))
    if found is None:
        raise FinanceValidationError(
            "a UK bank account is a 6-digit sort code and an 8-digit account number"
        )
    return f"{found[1]}:{found[2]}"


async def _load(session: AsyncSession, *conditions: ColumnElement[bool]) -> list[IdentifierRecord]:
    """Identifiers matching all conditions, sorted by kind and value."""
    # Select columns, not ORM objects, so a stale identity map can't hide an update.
    i = models.AccountIdentifier
    rows = await session.execute(
        select(i.id, i.account_id, i.kind, i.value, i.created_by, i.created_at)
        .where(*conditions)
        .order_by(i.kind, i.value)
    )
    return [
        IdentifierRecord(
            id=row.id,
            account_id=row.account_id,
            kind=cast(IdentifierKind, row.kind),
            value=row.value,
            created_by=row.created_by,
            created_at=row.created_at,
        )
        for row in rows
    ]


def _change_data(record: IdentifierRecord) -> dict[str, JSONValue]:
    return {
        "id": str(record.id),
        "account_id": str(record.account_id),
        "kind": record.kind,
        "value": record.value,
        "created_by": record.created_by,
    }


def _publish(ctx: CommandContext, event_type: str, record: IdentifierRecord) -> None:
    ctx.emit(
        event_type,
        record.id,
        {"identifier_id": str(record.id), "account_id": str(record.account_id)},
    )
    ctx.record_change(f"account_identifier:{record.id}", _change_data(record))


async def list_identifiers(
    session: AsyncSession, account_id: UUID | None = None
) -> list[IdentifierRecord]:
    if account_id is None:
        return await _load(session)
    return await _load(session, models.AccountIdentifier.account_id == account_id)


async def lookup_account(session: AsyncSession, kind: IdentifierKind, value: str) -> UUID | None:
    """The own account the identifier belongs to, if any."""
    try:
        normalized = normalize_identifier(kind, value)
    except FinanceValidationError:
        return None  # a value that can't be an identifier belongs to no account
    return await session.scalar(
        select(models.AccountIdentifier.account_id).where(
            models.AccountIdentifier.kind == kind,
            models.AccountIdentifier.value == normalized,
        )
    )


async def _require_can_carry_identifiers(session: AsyncSession, account_id: UUID) -> None:
    account = await get_account(session, account_id)  # raises AccountNotFoundError if missing
    if account.ledger_type not in ("asset", "liability"):
        raise FinanceValidationError("only an asset or liability account can have identifiers")
    if account.balance_mode == "snapshots":
        # Valuation ignores a snapshots account's postings, so a transfer to it would dip
        # net worth until the next snapshot.
        raise FinanceValidationError(
            "switch the account to hybrid before registering its identifiers:"
            " a snapshots-only account ignores postings"
        )


async def _insert(
    ctx: CommandContext, account_id: UUID, kind: IdentifierKind, value: str
) -> IdentifierRecord | UUID:
    """Register the identifier; when another row already has it, the account that holds it."""
    await _require_can_carry_identifiers(ctx.session, account_id)
    # ON CONFLICT rather than a check first, so a concurrent insert of the same identifier
    # can't turn into a unique violation that aborts the transaction.
    added = await ctx.session.scalar(
        insert(models.AccountIdentifier)
        .values(account_id=account_id, kind=kind, value=value, created_by=ctx.actor.type)
        .on_conflict_do_nothing(index_elements=["kind", "value"])
        .returning(models.AccountIdentifier.id)
    )
    if added is None:
        holder = await lookup_account(ctx.session, kind, value)
        # Gone only if a concurrent remove committed in between, which a single owner won't do.
        assert holder is not None
        return holder
    record = (await _load(ctx.session, models.AccountIdentifier.id == added))[0]
    _publish(ctx, "finance.account_identifier.added", record)
    return record


async def add_identifier(
    ctx: CommandContext, account_id: UUID, kind: IdentifierKind, value: str
) -> IdentifierRecord:
    result = await _insert(ctx, account_id, kind, normalize_identifier(kind, value))
    if isinstance(result, UUID):
        holder = await get_account(ctx.session, result)
        raise DuplicateAccountIdentifierError(holder.name)
    return result


async def ensure_identifier(
    ctx: CommandContext, account_id: UUID, kind: IdentifierKind, value: str
) -> bool:
    """Register the identifier unless it is registered already; True when it was added.

    For a connector registering its own accounts on every sync. An identifier on another
    account stays there: whoever registered it first (usually the owner) wins.
    """
    normalized = normalize_identifier(kind, value)
    result = await _insert(ctx, account_id, kind, normalized)
    if not isinstance(result, UUID):
        return True
    if result != account_id:
        # The value is the owner's bank details, so it stays out of the log.
        logger.warning(
            "a %s identifier of account %s is already on account %s; left there",
            kind,
            account_id,
            result,
        )
    return False


async def remove_identifier(ctx: CommandContext, identifier_id: UUID) -> IdentifierRecord:
    found = await _load(ctx.session, models.AccountIdentifier.id == identifier_id)
    if not found:
        raise AccountIdentifierNotFoundError(str(identifier_id))
    await ctx.session.execute(
        delete(models.AccountIdentifier).where(models.AccountIdentifier.id == identifier_id)
    )
    _publish(ctx, "finance.account_identifier.removed", found[0])
    return found[0]
