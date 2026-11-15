"""The registry of bank accounts (a UK sort code and account number, for now).

A row is any bank account a source has named. With `account_id` it is one of the owner's
accounts, so a counterparty's details that resolve to it make a transaction a transfer
between own accounts; without, it is a counterparty account not (yet) known to be the
owner's; `dismissed_at` marks one the owner said is not theirs. Rows are never deleted, as
transactions reference them.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import ColumnElement, func, insert, select, update
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
    kind: IdentifierKind
    value: str
    account_id: UUID | None
    dismissed_at: datetime | None
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
        select(i.id, i.kind, i.value, i.account_id, i.dismissed_at, i.created_by, i.created_at)
        .where(*conditions)
        .order_by(i.kind, i.value)
    )
    return [
        IdentifierRecord(
            id=row.id,
            kind=cast(IdentifierKind, row.kind),
            value=row.value,
            account_id=row.account_id,
            dismissed_at=row.dismissed_at,
            created_by=row.created_by,
            created_at=row.created_at,
        )
        for row in rows
    ]


async def _get(session: AsyncSession, identifier_id: UUID) -> IdentifierRecord:
    found = await _load(session, models.AccountIdentifier.id == identifier_id)
    if not found:
        raise AccountIdentifierNotFoundError(str(identifier_id))
    return found[0]


def _optional_str(value: object | None) -> str | None:
    return None if value is None else str(value)


def _change_data(record: IdentifierRecord) -> dict[str, JSONValue]:
    return {
        "id": str(record.id),
        "kind": record.kind,
        "value": record.value,
        "account_id": _optional_str(record.account_id),
        "dismissed_at": None if record.dismissed_at is None else record.dismissed_at.isoformat(),
        "created_by": record.created_by,
    }


def _publish(ctx: CommandContext, event_type: str, record: IdentifierRecord) -> None:
    ctx.emit(
        event_type,
        record.id,
        {"identifier_id": str(record.id), "account_id": _optional_str(record.account_id)},
    )
    ctx.record_change(f"account_identifier:{record.id}", _change_data(record))


async def list_identifiers(
    session: AsyncSession, *, account_id: UUID | None = None
) -> list[IdentifierRecord]:
    if account_id is None:
        return await _load(session)
    return await _load(session, models.AccountIdentifier.account_id == account_id)


async def identifier_account(session: AsyncSession, identifier_id: UUID) -> UUID | None:
    """The owner's account the identifier is assigned to, if any."""
    return await session.scalar(
        select(models.AccountIdentifier.account_id).where(
            models.AccountIdentifier.id == identifier_id
        )
    )


async def _get_or_create(ctx: CommandContext, kind: IdentifierKind, normalized: str) -> UUID:
    i = models.AccountIdentifier
    found = await ctx.session.scalar(select(i.id).where(i.kind == kind, i.value == normalized))
    if found is not None:
        # Rows are never deleted, so an existing one needs no lock. A sync that registers its
        # own accounts in its long transaction must not hold one: another sync naming the
        # same account, in a short transaction of its own, would wait on it to the end.
        return found
    # Serialised on the identifier, so concurrent syncs naming the same account create one
    # row; the lock is held until the transaction commits.
    lock_key = f"finance.identifier.{kind}:{normalized}"
    await ctx.session.execute(select(func.pg_advisory_xact_lock(func.hashtext(lock_key))))
    found = await ctx.session.scalar(select(i.id).where(i.kind == kind, i.value == normalized))
    if found is not None:
        return found
    created = await ctx.session.scalar(
        insert(i).values(kind=kind, value=normalized, created_by=ctx.actor.type).returning(i.id)
    )
    assert created is not None
    ctx.record_change(
        f"account_identifier:{created}", _change_data(await _get(ctx.session, created))
    )
    return created


async def get_or_create_identifier(ctx: CommandContext, kind: IdentifierKind, value: str) -> UUID:
    """The row for a bank account a source names, created unassigned if new.

    Call it in a short transaction of its own during a long sync, as the lock it takes is
    held until that transaction commits.
    """
    return await _get_or_create(ctx, kind, normalize_identifier(kind, value))


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


async def _assign(ctx: CommandContext, identifier_id: UUID, account_id: UUID) -> bool:
    """Assign an unassigned (or dismissed) row; False when it is assigned already."""
    i = models.AccountIdentifier
    # Conditional update, so a concurrent assignment elsewhere can't be overwritten.
    assigned = await ctx.session.scalar(
        update(i)
        .where(i.id == identifier_id, i.account_id.is_(None))
        .values(account_id=account_id, dismissed_at=None)
        .returning(i.id)
    )
    if assigned is None:
        return False
    _publish(ctx, "finance.account_identifier.assigned", await _get(ctx.session, identifier_id))
    return True


async def assign_identifier(
    ctx: CommandContext, account_id: UUID, kind: IdentifierKind, value: str
) -> IdentifierRecord:
    """The owner registering one of their accounts' identifiers."""
    normalized = normalize_identifier(kind, value)
    await _require_can_carry_identifiers(ctx.session, account_id)
    identifier_id = await _get_or_create(ctx, kind, normalized)
    if not await _assign(ctx, identifier_id, account_id):
        holder = await identifier_account(ctx.session, identifier_id)
        if holder != account_id:
            assert holder is not None  # rows are never deleted, and _assign saw it assigned
            raise DuplicateAccountIdentifierError((await get_account(ctx.session, holder)).name)
    return await _get(ctx.session, identifier_id)


async def ensure_identifier(
    ctx: CommandContext, account_id: UUID, kind: IdentifierKind, value: str
) -> bool:
    """Assign the identifier unless it is assigned already; True when it assigned.

    For a connector registering its own accounts on every sync. An identifier on another
    account stays there (the owner's registration wins); a dismissed one is assigned, as a
    connector's own account is a fact.
    """
    normalized = normalize_identifier(kind, value)
    await _require_can_carry_identifiers(ctx.session, account_id)
    identifier_id = await _get_or_create(ctx, kind, normalized)
    holder = await identifier_account(ctx.session, identifier_id)
    if holder == account_id:
        return False
    if holder is not None:
        # The value is the owner's bank details, so it stays out of the log.
        logger.warning(
            "identifier %s of account %s is on account %s; left there",
            identifier_id,
            account_id,
            holder,
        )
        return False
    return await _assign(ctx, identifier_id, account_id)


async def unassign_identifier(ctx: CommandContext, identifier_id: UUID) -> IdentifierRecord:
    """Take the identifier off its account; the row stays, as transactions reference it."""
    await _get(ctx.session, identifier_id)  # raises AccountIdentifierNotFoundError if missing
    i = models.AccountIdentifier
    unassigned = await ctx.session.scalar(
        update(i)
        .where(i.id == identifier_id, i.account_id.is_not(None))
        .values(account_id=None)
        .returning(i.id)
    )
    record = await _get(ctx.session, identifier_id)
    if unassigned is not None:
        _publish(ctx, "finance.account_identifier.unassigned", record)
    return record


async def dismiss_identifier(ctx: CommandContext, identifier_id: UUID) -> IdentifierRecord:
    """The owner saying an unassigned identifier is not one of theirs."""
    current = await _get(ctx.session, identifier_id)
    if current.dismissed_at is not None:
        return current
    i = models.AccountIdentifier
    dismissed = await ctx.session.scalar(
        update(i)
        .where(i.id == identifier_id, i.account_id.is_(None), i.dismissed_at.is_(None))
        .values(dismissed_at=func.now())
        .returning(i.id)
    )
    if dismissed is None:
        raise FinanceValidationError(
            "only an identifier not assigned to an account can be dismissed"
        )
    record = await _get(ctx.session, identifier_id)
    _publish(ctx, "finance.account_identifier.dismissed", record)
    return record
