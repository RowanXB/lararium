"""Monzo normalizer: the raw records of one sync → accounts, snapshots and ledger transactions.

Deterministic from its inputs and versioned (NORMALIZER_VERSION), so the ledger can later be
regenerated from raw records. Mapping rules (plan Decisions 5, 6 and 9):

- each Monzo account is a transactions-mode bank account; each pot a hybrid-mode child of it
  (ruling R14), valued by its moves before its first snapshot and by the pot balance fetched
  on each sync afterwards; a deleted pot is closed;
- each non-declined transaction is one ledger transaction keyed by its Monzo id, posted
  against the pot's account for a pot move and against "Unassigned" otherwise; when it changes
  upstream it is replaced in place, and a pending one that comes back declined or zero is
  voided;
- after each full backfill of an account, and when an account is first seen outside one,
  the difference from Monzo's balance is posted as its opening balance adjustment, replaced
  in place, so older history fetched later corrects it instead of adding to it (ruling R18);
  any other difference is a warning, never corrected.
"""

import logging
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.connections import get_connection, set_warnings, update_config
from lararium.core.connectors import SyncContext
from lararium.core.entities import get_or_create_organization
from lararium.core.json_types import JSONValue
from lararium.core.raw_records import get_raw_records
from lararium.core.settings_store import get_timezone
from lararium.core.timeutil import local_date
from lararium.modules.finance import models
from lararium.modules.finance.accounts import close_account, create_account, get_account
from lararium.modules.finance.instruments import InstrumentRecord, get_currency
from lararium.modules.finance.ledger import (
    PostingInput,
    TransactionInput,
    TransactionStatus,
    upsert_connector_transaction,
    void_transaction,
)
from lararium.modules.finance.money import to_plain_str, tolerance
from lararium.modules.finance.snapshots import record_snapshot
from lararium.modules.finance.system_accounts import get_or_create_system_account

logger = logging.getLogger(__name__)

NORMALIZER_VERSION = "monzo-norm-1"

ACCOUNT_MAP_KEY = "account_map"
# The last mismatch the owner was told about, per Monzo account, so each value is sent once.
NOTIFIED_KEY = "reconciliation_notified"
OPENING_DESCRIPTION = "Opening balance adjustment"

_ACCOUNT_NAMES = {"uk_retail": "Monzo", "uk_retail_joint": "Monzo Joint"}


@dataclass(frozen=True)
class FetchedRecord:
    """One response the sync stored: its raw record, the request that fetched it, and when.

    The request and time travel with the id because raw records are deduplicated by content:
    two accounts whose balances read the same share one record, stored with the first request,
    and a response seen again keeps the record's first fetch time.
    """

    record_id: UUID
    request: dict[str, JSONValue]
    fetched_at: datetime


@dataclass
class NormalizeReport:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    voided: int = 0
    skipped_declined: int = 0
    mismatches: list[str] = field(default_factory=list[str])


def to_quantity(minor_units: int, decimals: int) -> Decimal:
    """An amount in integer minor units as an exact decimal (-1234, 2 → -12.34)."""
    return Decimal(minor_units).scaleb(-decimals)


async def _notify(ctx: SyncContext, text: str) -> None:
    """Tell the owner; a failure to send is logged and never fails the sync."""
    try:
        await ctx.notifier.send(text)
    except Exception:
        logger.warning("sending a reconciliation notification failed", exc_info=True)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _mismatch_prefix(monzo_account_id: str) -> str:
    return f"Monzo account {monzo_account_id}: reconciliation mismatch"


def _description(tx: dict[str, Any]) -> str:
    merchant = tx.get("merchant")
    if isinstance(merchant, dict) and (name := cast(dict[str, Any], merchant).get("name")):
        return str(name)
    counterparty = tx.get("counterparty")
    if isinstance(counterparty, dict) and (name := cast(dict[str, Any], counterparty).get("name")):
        return str(name)
    return str(tx.get("description") or "")


async def _connector_transaction(
    session: AsyncSession, connection_id: UUID, external_id: str
) -> tuple[UUID, TransactionStatus] | None:
    t = models.Transaction
    row = (
        await session.execute(
            select(t.id, t.status).where(
                t.connection_id == connection_id, t.external_id == external_id
            )
        )
    ).one_or_none()
    return None if row is None else (row.id, cast(TransactionStatus, row.status))


async def _ledger_balance(
    session: AsyncSession, account_id: UUID, instrument_id: UUID, *, excluding: str | None = None
) -> Decimal:
    """Everything posted to the account in the instrument, pending included, void excluded,
    and so is the transaction with the external id `excluding`."""
    t, p = models.Transaction, models.Posting
    query = (
        select(func.sum(p.quantity))
        .join(t, t.id == p.transaction_id)
        .where(p.account_id == account_id, p.instrument_id == instrument_id, t.status != "void")
    )
    if excluding is not None:  # IS DISTINCT FROM NULL would drop manual postings (NULL id)
        query = query.where(t.external_id.is_distinct_from(excluding))
    total = await session.scalar(query)
    return total if total is not None else Decimal(0)


async def oldest_pending(
    session: AsyncSession, connection_id: UUID, config: dict[str, JSONValue], monzo_id: str
) -> datetime | None:
    """When the oldest transaction the ledger still holds as pending on the Monzo account was
    created; None if there is none. It must be fetched again until it settles or is voided."""
    account_map = config.get(ACCOUNT_MAP_KEY)
    account_id = account_map.get(monzo_id) if isinstance(account_map, dict) else None
    if not isinstance(account_id, str):
        return None
    t, p = models.Transaction, models.Posting
    return await session.scalar(
        select(func.min(t.occurred_at))
        .join(p, p.transaction_id == t.id)
        .where(
            t.connection_id == connection_id,
            t.status == "pending",
            p.account_id == UUID(account_id),
        )
    )


async def _earliest_posting(
    session: AsyncSession, account_id: UUID, *, excluding: str
) -> tuple[datetime, date] | None:
    t, p = models.Transaction, models.Posting
    row = (
        await session.execute(
            select(t.occurred_at, t.booking_date)
            .join(p, p.transaction_id == t.id)
            .where(
                p.account_id == account_id,
                t.status != "void",
                t.external_id.is_distinct_from(excluding),
            )
            .order_by(t.booking_date, t.occurred_at)
            .limit(1)
        )
    ).one_or_none()
    return None if row is None else (row.occurred_at, row.booking_date)


async def _snapshot_still_holds(
    session: AsyncSession, account_id: UUID, instrument_id: UUID, quantity: Decimal
) -> bool:
    """Whether the latest snapshot already says `quantity` with nothing posted after it, so a
    new one would add nothing (a hybrid account counts postings made after its snapshot)."""
    s, t, p = models.BalanceSnapshot, models.Transaction, models.Posting
    latest = (
        await session.execute(
            select(s.quantity, s.as_of)
            .where(s.account_id == account_id, s.instrument_id == instrument_id)
            .order_by(s.as_of.desc(), s.recorded_at.desc())
            .limit(1)
        )
    ).one_or_none()
    if latest is None or latest.quantity != quantity:
        return False
    posted_since = await session.scalar(
        select(func.count())
        .select_from(p)
        .join(t, t.id == p.transaction_id)
        .where(
            p.account_id == account_id,
            p.instrument_id == instrument_id,
            t.status != "void",
            t.occurred_at > latest.as_of,
        )
    )
    return posted_since == 0


class _Normalizer:
    def __init__(self, ctx: SyncContext, tz: ZoneInfo, config: dict[str, JSONValue]) -> None:
        self.command = ctx.command
        self.session = ctx.command.session
        self.connection_id = ctx.connection.id
        self.tz = tz
        self.observed_at = datetime.now(UTC)
        stored_map = config.get(ACCOUNT_MAP_KEY)
        self.account_map: dict[str, JSONValue] = (
            dict(stored_map) if isinstance(stored_map, dict) else {}
        )
        self.report = NormalizeReport()
        self._currencies: dict[str, InstrumentRecord] = {}
        self._unassigned: UUID | None = None
        # Recorded by finish(), once this sync's transactions are posted.
        self._snapshots: list[tuple[UUID, int, str, datetime]] = []
        self._deleted_pots: list[UUID] = []

    async def currency(self, code: str) -> InstrumentRecord:
        if code not in self._currencies:
            self._currencies[code] = await get_currency(self.session, code)
        return self._currencies[code]

    async def unassigned(self) -> UUID:
        if self._unassigned is None:
            self._unassigned = await get_or_create_system_account(self.command, "unassigned")
        return self._unassigned

    def mapped(self, monzo_id: str) -> UUID | None:
        value = self.account_map.get(monzo_id)
        return UUID(value) if isinstance(value, str) else None

    def require_mapped(self, monzo_id: str) -> UUID:
        account_id = self.mapped(monzo_id)
        if account_id is None:
            raise ValueError(f"Monzo account {monzo_id} has no finance account")
        return account_id

    def snapshot(
        self, account_id: UUID, minor_units: int, currency_code: str, as_of: datetime
    ) -> None:
        """Note the balance reported at `as_of` (when it was fetched), for finish()."""
        self._snapshots.append((account_id, minor_units, currency_code, as_of))

    async def finish(self) -> None:
        """Record the noted snapshots, then close the deleted pots.

        Only once this sync's transactions are posted: whether the latest snapshot still holds
        depends on what was posted after it, and a pot is closed after its last moves.
        """
        for snapshot in self._snapshots:
            await self._record_snapshot(*snapshot)
        for account_id in self._deleted_pots:
            if (await get_account(self.session, account_id)).status == "open":
                await close_account(self.command, account_id, local_date(self.observed_at, self.tz))

    async def _record_snapshot(
        self, account_id: UUID, minor_units: int, currency_code: str, as_of: datetime
    ) -> None:
        """Record the balance reported at `as_of`, unless the latest snapshot already
        accounts for it."""
        currency = await self.currency(currency_code)
        quantity = to_quantity(minor_units, currency.decimals)
        if await _snapshot_still_holds(self.session, account_id, currency.id, quantity):
            return
        await record_snapshot(
            self.command,
            account_id=account_id,
            instrument_id=currency.id,
            quantity=quantity,
            as_of=as_of,
            kind="reported",
        )

    async def account(self, account: dict[str, Any]) -> None:
        monzo_id: str = account["id"]
        if self.mapped(monzo_id) is not None:
            return
        name = (
            _ACCOUNT_NAMES.get(account.get("description", ""))
            or _ACCOUNT_NAMES.get(account.get("type", ""))
            or "Monzo"
        )
        created = await create_account(
            self.command,
            name=name,
            ledger_type="asset",
            subtype="bank",
            balance_mode="transactions",
            institution_entity_id=await get_or_create_organization(self.command, "Monzo"),
            opened_on=local_date(_parse(account["created"]), self.tz),
            source_connection_id=self.connection_id,
        )
        self.account_map[monzo_id] = str(created.id)

    async def pot(self, pot: dict[str, Any], parent_monzo_id: str, fetched_at: datetime) -> None:
        pot_id: str = pot["id"]
        account_id = self.mapped(pot_id)
        if account_id is None:
            if pot.get("deleted"):
                return  # gone before we ever saw it
            created = await create_account(
                self.command,
                name=str(pot.get("name") or "Pot"),
                ledger_type="asset",
                subtype="savings_pot",
                # Hybrid: its moves value it until the first snapshot, then postings after it.
                balance_mode="hybrid",
                institution_entity_id=await get_or_create_organization(self.command, "Monzo"),
                parent_account_id=self.require_mapped(parent_monzo_id),
                opened_on=local_date(_parse(pot["created"]), self.tz),
                source_connection_id=self.connection_id,
            )
            account_id = created.id
            self.account_map[pot_id] = str(account_id)
        self.snapshot(account_id, pot["balance"], pot["currency"], fetched_at)
        if pot.get("deleted"):
            self._deleted_pots.append(account_id)

    async def void_if_posted(self, external_id: str) -> bool:
        """Void the ledger transaction posted for this Monzo transaction; False if none."""
        existing = await _connector_transaction(self.session, self.connection_id, external_id)
        if existing is None:
            return False
        transaction_id, status = existing
        if status == "void":
            self.report.unchanged += 1
        else:
            await void_transaction(self.command, transaction_id)
            self.report.voided += 1
        return True

    async def transaction(self, tx: dict[str, Any], monzo_account_id: str) -> None:
        external_id: str = tx["id"]
        if tx.get("decline_reason"):
            if not await self.void_if_posted(external_id):
                self.report.skipped_declined += 1
            return
        if tx["amount"] == 0:
            # A reversed pending payment; a new zero-amount one moves no money.
            await self.void_if_posted(external_id)
            return
        currency = await self.currency(tx["currency"])
        quantity = to_quantity(tx["amount"], currency.decimals)
        metadata = tx.get("metadata")
        pot_id = (
            cast(dict[str, Any], metadata).get("pot_id") if isinstance(metadata, dict) else None
        )
        # A pot that was deleted before the connection existed has no account; its moves stay
        # unassigned, like any other transfer.
        other = (self.mapped(pot_id) if isinstance(pot_id, str) else None) or (
            await self.unassigned()
        )
        merchant = tx.get("merchant")
        counterparty: UUID | None = None
        if isinstance(merchant, dict):
            merchant_fields = cast(dict[str, Any], merchant)
            if merchant_fields.get("name"):
                counterparty = await get_or_create_organization(
                    self.command,
                    str(merchant_fields["name"]),
                    external_id=("monzo_merchant", str(merchant_fields["id"])),
                )
        created = _parse(tx["created"])
        settled = tx.get("settled") or ""
        _, outcome = await upsert_connector_transaction(
            self.command,
            TransactionInput(
                occurred_at=created,
                booking_date=local_date(created, self.tz),
                value_date=local_date(_parse(settled), self.tz) if settled else None,
                status="booked" if settled else "pending",
                description=_description(tx),
                counterparty_entity_id=counterparty,
                origin="connector",
                connection_id=self.connection_id,
                external_id=external_id,
                postings=[
                    PostingInput(
                        self.require_mapped(monzo_account_id),
                        currency.id,
                        quantity,
                        memo=str(tx.get("notes") or ""),
                    ),
                    PostingInput(other, currency.id, -quantity),
                ],
            ),
        )
        self.count(outcome)

    def count(self, outcome: Literal["created", "updated", "unchanged"]) -> None:
        if outcome == "created":
            self.report.created += 1
        elif outcome == "updated":
            self.report.updated += 1
        else:
            self.report.unchanged += 1

    async def opening(self, monzo_account: dict[str, Any], minor_units: int, code: str) -> None:
        """Post the difference between Monzo's balance and the rest of the ledger, the day
        before the earliest other transaction, replacing the opening posted before. An opening
        the owner voided stays void; one no longer needed is voided."""
        monzo_id: str = monzo_account["id"]
        external_id = f"opening:{monzo_id}"
        existing = await _connector_transaction(self.session, self.connection_id, external_id)
        if existing is not None and existing[1] == "void":
            self.report.unchanged += 1
            return
        account_id = self.require_mapped(monzo_id)
        currency = await self.currency(code)
        balance = to_quantity(minor_units, currency.decimals)
        others = await _ledger_balance(self.session, account_id, currency.id, excluding=external_id)
        difference = balance - others
        if difference == 0:
            if existing is not None:
                await void_transaction(self.command, existing[0])
                self.report.voided += 1
            return
        earliest = await _earliest_posting(self.session, account_id, excluding=external_id)
        if earliest is None:
            created = _parse(monzo_account["created"])
            earliest = (created, local_date(created, self.tz))
        occurred_at, booking_date = earliest
        _, outcome = await upsert_connector_transaction(
            self.command,
            TransactionInput(
                occurred_at=occurred_at - timedelta(days=1),
                booking_date=booking_date - timedelta(days=1),
                description=OPENING_DESCRIPTION,
                origin="connector",
                connection_id=self.connection_id,
                external_id=external_id,
                postings=[
                    PostingInput(account_id, currency.id, difference),
                    PostingInput(
                        await get_or_create_system_account(self.command, "opening_balances"),
                        currency.id,
                        -difference,
                    ),
                ],
            ),
        )
        self.count(outcome)

    async def reconcile(self, monzo_id: str, minor_units: int, code: str) -> tuple[str, str] | None:
        """(difference, warning) for this account, or None when the ledger agrees."""
        account_id = self.require_mapped(monzo_id)
        currency = await self.currency(code)
        balance = to_quantity(minor_units, currency.decimals)
        ledger = await _ledger_balance(self.session, account_id, currency.id)
        difference = balance - ledger
        if difference.copy_abs() <= tolerance(currency.decimals):
            return None
        # Shown at the currency's precision, so an empty ledger reads 0.00, not 0.
        ledger = ledger.quantize(Decimal(1).scaleb(-currency.decimals))
        return to_plain_str(difference), (
            f"{_mismatch_prefix(monzo_id)}: Monzo reports {to_plain_str(balance)} {code},"
            f" the ledger has {to_plain_str(ledger)} {code}"
            f" (difference {to_plain_str(difference)} {code})"
        )


async def normalize(
    ctx: SyncContext,
    fetched: Sequence[FetchedRecord],
    *,
    reconciled: Collection[str] = (),
    openings: Collection[str] = (),
) -> NormalizeReport:
    """Apply one sync's responses to the ledger.

    `openings`: the Monzo accounts whose opening balance adjustment is (re)computed in this
    sync: those whose full backfill it completed, and those it saw first. `reconciled`: the
    accounts that already have one, which are checked against Monzo's balance (as are
    `openings`, after their adjustment).
    """
    session = ctx.command.session
    connection = await get_connection(session, ctx.connection.id)
    n = _Normalizer(ctx, await get_timezone(session), connection.config)
    records = await get_raw_records(session, [f.record_id for f in fetched])
    responses = [
        (record.record_type, f.request, f.fetched_at, cast(Any, record.content))
        for f, record in zip(fetched, records, strict=True)
    ]

    accounts: dict[str, dict[str, Any]] = {}
    for record_type, _, _, content in responses:
        if record_type == "accounts":
            for account in content:
                accounts[account["id"]] = account
                await n.account(account)
    balances: dict[str, dict[str, Any]] = {}
    for record_type, request, fetched_at, content in responses:
        if record_type == "pots":
            for pot in content:
                await n.pot(pot, str(request["current_account_id"]), fetched_at)
        elif record_type == "balance":
            monzo_id = str(request["account_id"])
            balances[monzo_id] = content
            n.snapshot(
                n.require_mapped(monzo_id), content["balance"], content["currency"], fetched_at
            )
    for record_type, request, _, content in responses:
        if record_type == "transactions_page":
            for tx in content:
                await n.transaction(tx, str(request["account_id"]))
    await n.finish()

    checked = set(reconciled) | set(openings)
    mismatches: dict[str, tuple[str, str] | None] = {}
    for monzo_id, balance in balances.items():
        if monzo_id not in checked:
            continue
        if monzo_id in openings:
            await n.opening(accounts[monzo_id], balance["balance"], balance["currency"])
        mismatches[monzo_id] = await n.reconcile(monzo_id, balance["balance"], balance["currency"])

    # Warnings and notifications: one warning per mismatched account; the owner hears about
    # each new difference once, and a fixed one is forgotten.
    stored_notified = connection.config.get(NOTIFIED_KEY)
    notified: dict[str, JSONValue] = (
        dict(stored_notified) if isinstance(stored_notified, dict) else {}
    )
    warnings = [
        w
        for w in connection.warnings
        if not any(w.startswith(_mismatch_prefix(m)) for m in mismatches)
    ]
    for monzo_id, mismatch in mismatches.items():
        if mismatch is None:
            notified.pop(monzo_id, None)
            continue
        difference, warning = mismatch
        warnings.append(warning)
        n.report.mismatches.append(warning)
        if notified.get(monzo_id) != difference:
            notified[monzo_id] = difference
            await _notify(ctx, f"{connection.display_name}: {warning}")

    # Only this normaliser's keys: others (such as an OAuth state) may have changed meanwhile.
    values: dict[str, JSONValue] = {ACCOUNT_MAP_KEY: n.account_map}
    if notified:
        values[NOTIFIED_KEY] = notified
    remove = [] if notified or NOTIFIED_KEY not in connection.config else [NOTIFIED_KEY]
    if remove or any(connection.config.get(key) != value for key, value in values.items()):
        await update_config(ctx.command, connection.id, values, remove=remove)
    if warnings != connection.warnings:
        await set_warnings(ctx.command, connection.id, warnings)
    return n.report
