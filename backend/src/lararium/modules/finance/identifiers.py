"""The registry of account identifiers: bank accounts (a UK sort code and account number),
platform account ids, and card merchants.

A row is any account a source has named. With `account_id` it is one of the owner's
accounts, so a counterparty's details that resolve to it make a transaction a transfer
between own accounts; without, it is a counterparty account not (yet) known to be the
owner's; `dismissed_at` marks one the owner said is not theirs. Rows are never deleted, as
transactions reference them.

A card merchant (a Monzo merchant id) is the exception: it may pay into several own
accounts (Trading 212's Invest and ISA share one), so it keeps `account_id` NULL and names
its holders in finance.account_identifier_holders (M6 design §5.12). Such evidence never
identifies a record on its own: it pairs only records that are both there.

A payee bank account several own accounts are paid through (a platform's client money
account, shared by every one of the owner's accounts there) is held the same way: a
uk_bank_account row may keep `account_id` NULL and name its holders there too (account type
catalog design §8.1). A record naming it is identified to that set, never to one account. An
identifier has an account or holders, never both.
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import ColumnElement, delete, exists, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext
from lararium.core.json_types import JSONValue
from lararium.modules.finance import models
from lararium.modules.finance.accounts import get_account
from lararium.modules.finance.errors import (
    AccountIdentifierNotFoundError,
    AccountNotFoundError,
    DuplicateAccountIdentifierError,
    FinanceValidationError,
)

logger = logging.getLogger(__name__)

IdentifierKind = Literal[
    "uk_bank_account",
    "ibkr_account",
    "trading212_account",
    "etoro_account",
    "card_merchant",
    "chain_address",
]
# The kinds that may be held by several own accounts (finance.account_identifier_holders).
HeldKind = Literal["card_merchant", "uk_bank_account"]

_UK_BANK_ACCOUNT = re.compile(r"([0-9]{6})[:/]?([0-9]{8})")
_IBKR_ACCOUNT = re.compile(r"[A-Z]{1,3}[0-9]{3,12}")
# Trading 212's account id is an int64, written without leading zeros.
_TRADING212_ACCOUNT = re.compile(r"[1-9][0-9]{0,18}")
# An eToro account id: a Cash (eToro Money) account's is a UUID, a Trading or MoneyFarm
# account's 7 or 8 digits (a string, kept as eToro writes it).
_ETORO_ACCOUNT = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9]{7,8}"
)
# A Monzo merchant id, as Monzo writes it (case kept).
_CARD_MERCHANT = re.compile(r"merch_[0-9A-Za-z]+")
# What the database CHECK allows; an address is as the chain writes it.
_CHAIN_ADDRESS = re.compile(r"[A-Za-z0-9:_-]{20,128}")


@dataclass(frozen=True)
class IdentifierRecord:
    id: UUID
    kind: IdentifierKind
    value: str
    account_id: UUID | None
    dismissed_at: datetime | None
    created_by: str
    created_at: datetime
    # The own accounts holding it, sorted: a card merchant's, or a payee bank account's held
    # by several (then account_id is None); empty for every other row.
    holders: tuple[UUID, ...] = ()


def normalize_identifier(kind: IdentifierKind, value: str) -> str:
    """The stored form: `<6-digit sort code>:<8-digit account number>` for a UK bank account,
    the upper-case account id for an IBKR account, the account number for Trading 212,
    eToro's account id with a UUID in lower case, Monzo's merchant id as written, and the
    address as written for a chain address (lower-cased when it begins `0x` or `bc1`, as EVM
    and Bitcoin bech32 addresses are case-insensitive; a wallet shows bech32 in lower case)."""
    if kind == "chain_address":
        address = value.strip()
        if address.lower().startswith(("0x", "bc1")):
            address = address.lower()
        if _CHAIN_ADDRESS.fullmatch(address) is None:
            raise FinanceValidationError(
                "a chain address is 20 to 128 letters, digits, colons, dashes or underscores"
            )
        return address
    if kind == "card_merchant":
        merchant_id = value.strip()
        if _CARD_MERCHANT.fullmatch(merchant_id) is None:
            raise FinanceValidationError(
                "a card merchant is a Monzo merchant id: merch_ followed by letters and digits"
            )
        return merchant_id
    if kind == "etoro_account":
        account_id = value.strip().lower()
        if _ETORO_ACCOUNT.fullmatch(account_id) is None:
            raise FinanceValidationError("an eToro account id is a UUID or 7 to 8 digits")
        return account_id
    if kind == "trading212_account":
        account_id = value.strip()
        if _TRADING212_ACCOUNT.fullmatch(account_id) is None:
            raise FinanceValidationError(
                "a Trading 212 account id is a number of up to 19 digits, without leading zeros"
            )
        return account_id
    if kind == "ibkr_account":
        account_id = value.strip().upper()
        if _IBKR_ACCOUNT.fullmatch(account_id) is None:
            raise FinanceValidationError(
                "an IBKR account id is 1 to 3 letters followed by 3 to 12 digits"
            )
        return account_id
    # Spaces and dashes go, so whitespace between the two parts works as a separator too.
    found = _UK_BANK_ACCOUNT.fullmatch(re.sub(r"[\s-]", "", value))
    if found is None:
        raise FinanceValidationError(
            "a UK bank account is a 6-digit sort code and an 8-digit account number"
        )
    return f"{found[1]}:{found[2]}"


def uk_bank_account_parts(value: str) -> tuple[str, str]:
    """(sort code, account number) of a stored UK bank account."""
    sort_code, account_number = value.split(":")
    return sort_code, account_number


async def _load(session: AsyncSession, *conditions: ColumnElement[bool]) -> list[IdentifierRecord]:
    """Identifiers matching all conditions, sorted by kind and value."""
    # Select columns, not ORM objects, so a stale identity map can't hide an update.
    i, h = models.AccountIdentifier, models.AccountIdentifierHolder
    rows = list(
        await session.execute(
            select(i.id, i.kind, i.value, i.account_id, i.dismissed_at, i.created_by, i.created_at)
            .where(*conditions)
            .order_by(i.kind, i.value)
        )
    )
    holders: dict[UUID, list[UUID]] = {}
    if rows:
        # By the same conditions, not by the ids: the registry holds every bank account a
        # source has named.
        for identifier_id, account_id in await session.execute(
            select(h.identifier_id, h.account_id)
            .where(h.identifier_id.in_(select(i.id).where(*conditions)))
            .order_by(h.identifier_id, h.account_id)
        ):
            holders.setdefault(identifier_id, []).append(account_id)
    return [
        IdentifierRecord(
            id=row.id,
            kind=cast(IdentifierKind, row.kind),
            value=row.value,
            account_id=row.account_id,
            dismissed_at=row.dismissed_at,
            created_by=row.created_by,
            created_at=row.created_at,
            holders=tuple(holders.get(row.id, ())),
        )
        for row in rows
    ]


async def _get(session: AsyncSession, identifier_id: UUID) -> IdentifierRecord:
    found = await _load(session, models.AccountIdentifier.id == identifier_id)
    if not found:
        raise AccountIdentifierNotFoundError(str(identifier_id))
    return found[0]


async def get_identifier(session: AsyncSession, identifier_id: UUID) -> IdentifierRecord:
    """The registry row, with its holders; AccountIdentifierNotFoundError when missing."""
    return await _get(session, identifier_id)


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
        "holders": [str(holder) for holder in record.holders],
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
    """The registry, or the identifiers of one own account: those assigned to it, and those
    it is one of the holders of."""
    if account_id is None:
        return await _load(session)
    i, h = models.AccountIdentifier, models.AccountIdentifierHolder
    held = select(h.identifier_id).where(h.account_id == account_id)
    return await _load(session, or_(i.account_id == account_id, i.id.in_(held)))


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


def _require_single_holder(kind: IdentifierKind) -> None:
    if kind == "card_merchant":
        raise FinanceValidationError(
            "a card merchant is registered with the accounts it pays into, not assigned to one"
        )


_SNAPSHOTS_ONLY = (
    "switch the account to hybrid before registering its identifiers:"
    " a snapshots-only account ignores postings"
)


async def _require_can_carry_identifiers(session: AsyncSession, account_id: UUID) -> None:
    account = await get_account(session, account_id)  # raises AccountNotFoundError if missing
    if account.system:
        # In transit is an asset, but it is the ledger's own: a record naming it would be a
        # "transfer" parked there for good.
        raise FinanceValidationError("a system account can't have identifiers")
    if account.ledger_type not in ("asset", "liability"):
        raise FinanceValidationError("only an asset or liability account can have identifiers")
    if account.balance_mode == "snapshots":
        # Valuation ignores a snapshots account's postings, so a transfer to it would dip
        # net worth until the next snapshot.
        raise FinanceValidationError(_SNAPSHOTS_ONLY)


_HELD = (
    "the bank account is held by several of your accounts, so it can't be registered to one;"
    " remove its holders first"
)


def _held(identifier_id: UUID) -> ColumnElement[bool]:
    h = models.AccountIdentifierHolder
    return exists().where(h.identifier_id == identifier_id)


async def _assign(ctx: CommandContext, identifier_id: UUID, account_id: UUID) -> bool:
    """Assign an unassigned (or dismissed) row; False when it didn't: it is assigned
    already, or held by several accounts (_held)."""
    i = models.AccountIdentifier
    # Locked first, as register_holders locks it: the update below then reads, in a
    # snapshot of its own, the holders a registration that held the row committed. (A
    # condition alone would be read in the snapshot taken before it waited on the row.)
    # FOR NO KEY UPDATE (key_share), as each registration locks it: it serialises them
    # still, but not a transaction inserted naming the row (its foreign key check takes a
    # KEY SHARE lock), which FOR UPDATE would block, or deadlock with as a registration
    # rematches transfers.
    await ctx.session.execute(
        select(i.id).where(i.id == identifier_id).with_for_update(key_share=True)
    )
    # Conditional update, so a concurrent assignment elsewhere can't be overwritten.
    assigned = await ctx.session.scalar(
        update(i)
        .where(i.id == identifier_id, i.account_id.is_(None), ~_held(identifier_id))
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
    _require_single_holder(kind)
    normalized = normalize_identifier(kind, value)
    await _require_can_carry_identifiers(ctx.session, account_id)
    identifier_id = await _get_or_create(ctx, kind, normalized)
    if not await _assign(ctx, identifier_id, account_id):
        if await ctx.session.scalar(select(_held(identifier_id))):
            raise FinanceValidationError(_HELD)
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
    connector's own account is a fact. One held by several accounts (a shared payee's) is
    left as it is too.
    """
    _require_single_holder(kind)
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
    if await _assign(ctx, identifier_id, account_id):
        return True
    if await ctx.session.scalar(select(_held(identifier_id))):
        logger.warning(
            "identifier %s of account %s is held by several accounts; left there",
            identifier_id,
            account_id,
        )
    return False


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
    if current.holders:
        raise FinanceValidationError(
            "an identifier your accounts hold can't be dismissed; remove its holders first"
        )
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


_HOLDS = {"card_merchant": "a card merchant", "uk_bank_account": "a shared bank account"}


async def _require_can_hold(session: AsyncSession, kind: HeldKind, account_id: UUID) -> None:
    """The refusals of the other kinds (_require_can_carry_identifiers), narrowed: a held
    identifier pays only into an open asset account. Asked only of an account a registration
    adds: a holder that has closed since stays (close_account keeps it). An unknown account
    is refused like the others (a 422 on the request's body), not as a missing resource."""
    try:
        account = await get_account(session, account_id)
    except AccountNotFoundError:
        raise FinanceValidationError(f"account {account_id} does not exist") from None
    if account.system:
        raise FinanceValidationError("a system account can't have identifiers")
    if account.ledger_type != "asset":
        raise FinanceValidationError(f"{_HOLDS[kind]} pays only into an asset account")
    if account.balance_mode == "snapshots":
        raise FinanceValidationError(_SNAPSHOTS_ONLY)
    if account.status != "open":
        raise FinanceValidationError(f"{_HOLDS[kind]} can't pay into a closed account")


async def register_holders(
    ctx: CommandContext, kind: HeldKind, value: str, account_ids: Sequence[UUID]
) -> IdentifierRecord:
    """The owner saying payments to this identifier go into these own accounts: replaces
    its holders with `account_ids` (an empty list removes the registration; the row stays,
    as transactions reference it). `kind` is a card merchant (a Monzo merchant id) or a UK
    bank account several own accounts are paid through (account type catalog design §8.1);
    a bank account registered to one account is refused, as it names that account.
    Registering takes it back from dismissed."""
    normalized = normalize_identifier(kind, value)
    wanted = sorted(set(account_ids))
    identifier_id = await _get_or_create(ctx, kind, normalized)
    i, h = models.AccountIdentifier, models.AccountIdentifierHolder
    # Serialised on the row, so two registrations can't interleave their holder sets, and
    # an assignment (_assign, which locks it too) can't slip in between.
    await ctx.session.execute(
        select(i.id).where(i.id == identifier_id).with_for_update(key_share=True)
    )
    before = await _get(ctx.session, identifier_id)
    if before.account_id is not None:
        raise FinanceValidationError(
            "the bank account is registered to one of your accounts, so it can't have"
            " holders; remove it from that account first"
        )
    added = [account_id for account_id in wanted if account_id not in before.holders]
    for account_id in added:
        await _require_can_hold(ctx.session, kind, account_id)
    if list(before.holders) == wanted and not (wanted and before.dismissed_at is not None):
        return before
    await ctx.session.execute(
        delete(h).where(h.identifier_id == identifier_id, h.account_id.not_in(wanted))
    )
    if added:
        await ctx.session.execute(
            insert(h).values(
                [{"identifier_id": identifier_id, "kind": kind, "account_id": a} for a in added]
            )
        )
    if wanted and before.dismissed_at is not None:
        await ctx.session.execute(update(i).where(i.id == identifier_id).values(dismissed_at=None))
    record = await _get(ctx.session, identifier_id)
    ctx.emit(
        "finance.account_identifier.holders_changed",
        record.id,
        {"identifier_id": str(record.id), "account_ids": [str(a) for a in record.holders]},
    )
    ctx.record_change(f"account_identifier:{record.id}", _change_data(record))
    return record


async def register_card_merchant(
    ctx: CommandContext, merchant_id: str, account_ids: Sequence[UUID]
) -> IdentifierRecord:
    """register_holders for a card merchant: card payments to it go into these own
    accounts."""
    return await register_holders(ctx, "card_merchant", merchant_id, account_ids)


async def dismiss_card_merchant(ctx: CommandContext, merchant_id: str) -> IdentifierRecord:
    """The owner saying the card merchant pays into none of their accounts (a suggestion
    dismissed): its row, created if new, is dismissed."""
    normalized = normalize_identifier("card_merchant", merchant_id)
    return await dismiss_identifier(ctx, await _get_or_create(ctx, "card_merchant", normalized))


async def registered_card_merchants(session: AsyncSession) -> dict[str, UUID]:
    """Every card merchant registered to at least one account: its merchant id → row id."""
    i, h = models.AccountIdentifier, models.AccountIdentifierHolder
    rows = await session.execute(
        select(i.value, i.id).where(
            i.kind == "card_merchant", select(h.account_id).where(h.identifier_id == i.id).exists()
        )
    )
    return {row.value: row.id for row in rows}
