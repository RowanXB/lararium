from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import ColumnElement, exists, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext
from lararium.core.entities import get_self_entity_id
from lararium.core.json_types import JSONValue
from lararium.modules.finance import models
from lararium.modules.finance.errors import AccountAlreadyClosedError, AccountNotFoundError

LedgerType = Literal["asset", "liability", "income", "expense", "equity"]
AccountSubtype = Literal[
    "bank", "savings_pot", "broker", "exchange", "wallet", "in_transit", "manual_asset", "other"
]
TaxWrapper = Literal["none", "isa", "sipp", "lisa", "gia"]
BalanceMode = Literal["transactions", "snapshots", "hybrid"]
SourceKind = Literal["manual", "connection"]
AccountStatus = Literal["open", "closed"]


@dataclass(frozen=True)
class AccountRecord:
    id: UUID
    name: str
    ledger_type: LedgerType
    subtype: AccountSubtype
    institution_entity_id: UUID | None
    owner_entity_id: UUID
    tax_wrapper: TaxWrapper
    parent_account_id: UUID | None
    balance_mode: BalanceMode
    source_kind: SourceKind
    source_connection_id: UUID | None
    status: AccountStatus
    opened_on: date | None
    closed_on: date | None
    product_type: str | None
    owner_type: str | None
    goal_amount: Decimal | None
    locked: bool | None
    round_up: bool | None
    round_up_multiplier: Decimal | None


@dataclass(frozen=True)
class SourceDetails:
    """What a source says about an account. None clears a field, except tax_wrapper, which it
    leaves as it is; so does every field named in `unknown`: one the source states in a shape
    that cannot be read."""

    product_type: str | None = None
    owner_type: str | None = None
    goal_amount: Decimal | None = None
    locked: bool | None = None
    round_up: bool | None = None
    round_up_multiplier: Decimal | None = None
    tax_wrapper: TaxWrapper | None = None
    unknown: frozenset[str] = frozenset()


async def _load(session: AsyncSession, *conditions: ColumnElement[bool]) -> list[AccountRecord]:
    """Accounts matching all conditions, sorted by name (then id)."""
    # Select columns, not ORM objects, so a stale identity map can't hide an update.
    a = models.Account
    rows = await session.execute(
        select(
            a.id,
            a.name,
            a.ledger_type,
            a.subtype,
            a.institution_entity_id,
            a.owner_entity_id,
            a.tax_wrapper,
            a.parent_account_id,
            a.balance_mode,
            a.source_kind,
            a.source_connection_id,
            a.status,
            a.opened_on,
            a.closed_on,
            a.product_type,
            a.owner_type,
            a.goal_amount,
            a.locked,
            a.round_up,
            a.round_up_multiplier,
        )
        .where(*conditions)
        .order_by(a.name, a.id)
    )
    return [
        AccountRecord(
            id=row.id,
            name=row.name,
            ledger_type=cast(LedgerType, row.ledger_type),
            subtype=cast(AccountSubtype, row.subtype),
            institution_entity_id=row.institution_entity_id,
            owner_entity_id=row.owner_entity_id,
            tax_wrapper=cast(TaxWrapper, row.tax_wrapper),
            parent_account_id=row.parent_account_id,
            balance_mode=cast(BalanceMode, row.balance_mode),
            source_kind=cast(SourceKind, row.source_kind),
            source_connection_id=row.source_connection_id,
            status=cast(AccountStatus, row.status),
            opened_on=row.opened_on,
            closed_on=row.closed_on,
            product_type=row.product_type,
            owner_type=row.owner_type,
            goal_amount=row.goal_amount,
            locked=row.locked,
            round_up=row.round_up,
            round_up_multiplier=row.round_up_multiplier,
        )
        for row in rows
    ]


def _optional_str(value: object | None) -> str | None:
    return None if value is None else str(value)


def _change_data(record: AccountRecord) -> dict[str, JSONValue]:
    return {
        "id": str(record.id),
        "name": record.name,
        "ledger_type": record.ledger_type,
        "subtype": record.subtype,
        "institution_entity_id": _optional_str(record.institution_entity_id),
        "owner_entity_id": str(record.owner_entity_id),
        "tax_wrapper": record.tax_wrapper,
        "parent_account_id": _optional_str(record.parent_account_id),
        "balance_mode": record.balance_mode,
        "source_kind": record.source_kind,
        "source_connection_id": _optional_str(record.source_connection_id),
        "status": record.status,
        "opened_on": _optional_str(record.opened_on),
        "closed_on": _optional_str(record.closed_on),
        "product_type": record.product_type,
        "owner_type": record.owner_type,
        "goal_amount": _optional_str(record.goal_amount),
        "locked": record.locked,
        "round_up": record.round_up,
        "round_up_multiplier": _optional_str(record.round_up_multiplier),
    }


def _publish(ctx: CommandContext, event_type: str, record: AccountRecord) -> None:
    ctx.emit(event_type, record.id, {"account_id": str(record.id)})
    ctx.record_change(f"account:{record.id}", _change_data(record))


async def get_account(session: AsyncSession, account_id: UUID) -> AccountRecord:
    found = await _load(session, models.Account.id == account_id)
    if not found:
        raise AccountNotFoundError(str(account_id))
    return found[0]


async def list_accounts(session: AsyncSession) -> list[AccountRecord]:
    return await _load(session)


async def create_account(
    ctx: CommandContext,
    *,
    name: str,
    ledger_type: LedgerType,
    subtype: AccountSubtype,
    balance_mode: BalanceMode,
    institution_entity_id: UUID | None = None,
    tax_wrapper: TaxWrapper = "none",
    parent_account_id: UUID | None = None,
    opened_on: date | None = None,
    source_connection_id: UUID | None = None,
) -> AccountRecord:
    session = ctx.session
    if parent_account_id is not None and not await session.scalar(
        select(exists().where(models.Account.id == parent_account_id))
    ):
        raise AccountNotFoundError(str(parent_account_id))
    account_id = (
        await session.execute(
            insert(models.Account)
            .values(
                name=name,
                ledger_type=ledger_type,
                subtype=subtype,
                institution_entity_id=institution_entity_id,
                owner_entity_id=await get_self_entity_id(session),
                tax_wrapper=tax_wrapper,
                parent_account_id=parent_account_id,
                balance_mode=balance_mode,
                source_kind="manual" if source_connection_id is None else "connection",
                source_connection_id=source_connection_id,
                opened_on=opened_on,
            )
            .returning(models.Account.id)
        )
    ).scalar_one()
    record = await get_account(session, account_id)
    _publish(ctx, "finance.account.created", record)
    return record


async def rename_account(ctx: CommandContext, account_id: UUID, name: str) -> AccountRecord:
    updated = await ctx.session.scalar(
        update(models.Account)
        .where(models.Account.id == account_id)
        .values(name=name)
        .returning(models.Account.id)
    )
    if updated is None:
        raise AccountNotFoundError(str(account_id))
    record = await get_account(ctx.session, account_id)
    _publish(ctx, "finance.account.updated", record)
    return record


async def set_source_details(ctx: CommandContext, account_id: UUID, details: SourceDetails) -> bool:
    """Write what the source says about the account; False (and no event) if nothing differs."""
    a = models.Account
    values: dict[str, object] = {
        "product_type": details.product_type,
        "owner_type": details.owner_type,
        "goal_amount": details.goal_amount,
        "locked": details.locked,
        "round_up": details.round_up,
        "round_up_multiplier": details.round_up_multiplier,
    }
    if not details.unknown <= values.keys():
        raise ValueError(f"not source detail fields: {sorted(details.unknown - values.keys())}")
    for name in details.unknown:
        del values[name]
    if details.tax_wrapper is not None:
        values["tax_wrapper"] = details.tax_wrapper
    if not values:
        await get_account(ctx.session, account_id)  # raises AccountNotFoundError if missing
        return False
    differs = or_(*(getattr(a, name).is_distinct_from(value) for name, value in values.items()))
    written = await ctx.session.scalar(
        update(a).where(a.id == account_id, differs).values(**values).returning(a.id)
    )
    if written is None:
        await get_account(ctx.session, account_id)  # raises AccountNotFoundError if missing
        return False
    _publish(ctx, "finance.account.updated", await get_account(ctx.session, account_id))
    return True


async def close_account(ctx: CommandContext, account_id: UUID, closed_on: date) -> AccountRecord:
    # Conditional update, so two concurrent closes can't both succeed.
    closed = await ctx.session.scalar(
        update(models.Account)
        .where(models.Account.id == account_id, models.Account.status == "open")
        .values(status="closed", closed_on=closed_on)
        .returning(models.Account.id)
    )
    if closed is None:
        await get_account(ctx.session, account_id)  # raises AccountNotFoundError if missing
        raise AccountAlreadyClosedError(str(account_id))
    record = await get_account(ctx.session, account_id)
    _publish(ctx, "finance.account.closed", record)
    return record
