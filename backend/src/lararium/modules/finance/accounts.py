from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import ColumnElement, Text, delete, exists, func, insert, or_, select, update
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
            exists()
            .where(
                Setting.key.startswith(SYSTEM_ACCOUNT_SETTING_PREFIX),
                Setting.value == func.to_jsonb(sql_cast(a.id, Text)),
            )
            .label("system"),
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
            system=row.system,
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


def require_owner_account(record: AccountRecord, action: str) -> None:
    """Refuse an owner's command on one of the ledger's own accounts."""
    if record.system:
        raise FinanceValidationError(
            f"{record.name} belongs to the ledger, which keeps it; it can't be {action}"
        )


async def _require_main_account(
    session: AsyncSession, main_account_id: UUID, account: AccountRecord | None = None
) -> None:
    """Refuse a main account a manual account can't have, whether it is being created
    (`account` None) or given one: one level only, as pots have. The main account must be
    an open account of the owner's, not the account itself, with no main account of its
    own; and the account mustn't be another's main account already. A sub-account's bank
    transfers name it, so it must also be an account an identifier could be on
    (identifiers._require_can_carry_identifiers): an asset or liability whose postings
    count. AccountNotFoundError for an unknown main account."""
    main = await get_account(session, main_account_id)
    if account is not None and main.id == account.id:
        raise FinanceValidationError("an account can't be its own main account")
    if main.system:
        raise FinanceValidationError(f"{main.name} belongs to the ledger, not to you")
    if main.ledger_type not in ("asset", "liability"):
        raise FinanceValidationError(f"{main.name} isn't an asset or liability account")
    if main.balance_mode == "snapshots":
        # Valuation ignores a snapshots account's postings, so the placeholder a transfer
        # naming it may post wouldn't count, and the transfer would dip net worth.
        raise FinanceValidationError(
            f"switch {main.name} to hybrid first: a snapshots-only account ignores postings"
        )
    if main.status == "closed":
        raise FinanceValidationError(f"{main.name} is closed")
    if main.parent_account_id is not None:
        raise FinanceValidationError(f"{main.name} has a main account of its own")
    if account is not None and await session.scalar(
        select(exists().where(models.Account.parent_account_id == account.id))
    ):
        raise FinanceValidationError(f"{account.name} is already the main account of another")


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
    """A manual account's parent_account_id is its main account (set_main_account's rules);
    a connected account's is its connector's to give (a Monzo pot's main account)."""
    session = ctx.session
    if parent_account_id is not None:
        if source_connection_id is None:
            await _require_main_account(session, parent_account_id)
        elif not await session.scalar(
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


async def set_main_account(
    ctx: CommandContext, account_id: UUID, main_account_id: UUID | None
) -> AccountRecord:
    """Make a manual account a sub-account of another (None: of none).

    A bank transfer of the sub-account that names no bank account then names its main
    account, which pairs it with the main account's side. A connected account's main account
    is its connector's to set. One level only (_require_main_account).
    """
    session = ctx.session
    record = await get_account(session, account_id)
    require_owner_account(record, "given a main account")
    if record.source_kind == "connection":
        raise FinanceValidationError(f"{record.name} is connected; its connector sets it")
    if main_account_id is not None and record.parent_account_id != main_account_id:
        await _require_main_account(session, main_account_id, record)
    # Conditional update, so an unchanged value writes and publishes nothing.
    changed = await session.scalar(
        update(models.Account)
        .where(
            models.Account.id == account_id,
            models.Account.parent_account_id.is_distinct_from(main_account_id),
        )
        .values(parent_account_id=main_account_id)
        .returning(models.Account.id)
    )
    record = await get_account(session, account_id)
    if changed is not None:
        _publish(ctx, "finance.account.updated", record)
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


async def close_account(
    ctx: CommandContext, account_id: UUID, closed_on: date
) -> tuple[AccountRecord, list[UUID]]:
    """Close the account: (the closed account, the card merchants it left).

    A card merchant's holders are open accounts, so the account leaves those it held
    (_drop_card_merchant_holder). The records naming them are matched again by the heal
    after the command: every route that writes heals (the API's close re-matches them first,
    engine.rematch_identifiers), and so does the worker after each sync (worker.jobs), whose
    pair relation reads the holders as they now are."""
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
    return record, await _drop_card_merchant_holder(ctx, account_id)


async def _drop_card_merchant_holder(ctx: CommandContext, account_id: UUID) -> list[UUID]:
    """Take a closed account out of the card merchants it held (a card merchant's holders
    are open accounts, identifiers.register_card_merchant), audited as a change of their
    holders; returns those merchants' identifier ids, sorted."""
    h = models.AccountIdentifierHolder
    dropped = sorted(
        await ctx.session.scalars(
            delete(h).where(h.account_id == account_id).returning(h.identifier_id)
        )
    )
    for identifier_id in dropped:
        remaining = sorted(
            await ctx.session.scalars(select(h.account_id).where(h.identifier_id == identifier_id))
        )
        holders: list[JSONValue] = [str(a) for a in remaining]
        ctx.emit(
            "finance.account_identifier.holders_changed",
            identifier_id,
            {"identifier_id": str(identifier_id), "account_ids": holders},
        )
        ctx.record_change(
            f"account_identifier:{identifier_id}",
            {"id": str(identifier_id), "holders": holders, "closed_holder": str(account_id)},
        )
    return dropped


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
