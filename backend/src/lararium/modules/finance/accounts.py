from dataclasses import dataclass
from datetime import date
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import ColumnElement, Text, exists, func, insert, select, update
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext
from lararium.core.entities import get_self_entity_id
from lararium.core.json_types import JSONValue
from lararium.core.models import Setting
from lararium.modules.finance import models
from lararium.modules.finance.errors import (
    AccountAlreadyClosedError,
    AccountNotFoundError,
    FinanceValidationError,
)

LedgerType = Literal["asset", "liability", "income", "expense", "equity"]
AccountSubtype = Literal[
    "bank", "savings_pot", "broker", "exchange", "wallet", "in_transit", "manual_asset", "other"
]
TaxWrapper = Literal["none", "isa", "sipp", "lisa", "gia"]
BalanceMode = Literal["transactions", "snapshots", "hybrid"]
SourceKind = Literal["manual", "connection"]
AccountStatus = Literal["open", "closed"]

# The settings remembering the ledger's own accounts (system_accounts): an account is one
# when a setting under this prefix holds its id.
SYSTEM_ACCOUNT_SETTING_PREFIX = "finance.system_account."


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
    # One the ledger keeps for itself (Unassigned, Opening balances, In transit): the owner
    # can't close, rename or snapshot it.
    system: bool


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
            exists()
            .where(
                Setting.key.startswith(SYSTEM_ACCOUNT_SETTING_PREFIX),
                Setting.value == func.to_jsonb(sql_cast(a.id, Text)),
            )
            .label("system"),
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
            system=row.system,
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


def require_owner_account(record: AccountRecord, action: str) -> None:
    """Refuse an owner's command on one of the ledger's own accounts."""
    if record.system:
        raise FinanceValidationError(
            f"{record.name} belongs to the ledger, which keeps it; it can't be {action}"
        )


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
    require_owner_account(await get_account(ctx.session, account_id), "renamed")
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


async def close_account(ctx: CommandContext, account_id: UUID, closed_on: date) -> AccountRecord:
    # Closing In transit would drop the transfers it holds from net worth.
    require_owner_account(await get_account(ctx.session, account_id), "closed")
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


async def set_balance_mode(
    ctx: CommandContext, account_id: UUID, mode: BalanceMode
) -> AccountRecord:
    """Switch a snapshots account to hybrid, the only change allowed.

    Hybrid values it exactly as before (the latest snapshot) plus whatever is posted after
    it; any other change would revalue its history. The current mode is a no-op.
    """
    record = await get_account(ctx.session, account_id)
    if record.balance_mode == mode:
        return record
    if (record.balance_mode, mode) != ("snapshots", "hybrid"):
        raise FinanceValidationError(
            f"an account can only switch from snapshots to hybrid, not from"
            f" {record.balance_mode} to {mode}"
        )
    # Conditional update, so a concurrent switch can't apply twice.
    switched = await ctx.session.scalar(
        update(models.Account)
        .where(models.Account.id == account_id, models.Account.balance_mode == "snapshots")
        .values(balance_mode=mode)
        .returning(models.Account.id)
    )
    record = await get_account(ctx.session, account_id)
    if switched is not None:
        _publish(ctx, "finance.account.updated", record)
    return record
