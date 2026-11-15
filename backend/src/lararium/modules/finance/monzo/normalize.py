"""Monzo normalizer: the raw records of one sync → accounts, snapshots and ledger transactions.

Deterministic from its inputs and versioned (NORMALIZER_VERSION), so the ledger can later be
regenerated from raw records. Mapping rules (plan Decisions 5, 6 and 9):

- each Monzo account is a transactions-mode bank account; each pot a hybrid-mode child of it
  (ruling R14), valued by its moves before its first snapshot and by the pot balance fetched
  on each sync afterwards; a deleted pot is closed;
- each non-declined transaction is one ledger transaction keyed by its Monzo id, posted
  against the pot's account for a pot move, and otherwise against "In transit" when it is a
  transfer between the owner's accounts and "Unassigned" when not (transfer_contra; the
  heal after the sync does the matching); when it changes upstream it is replaced in place,
  and a pending one that comes back declined or zero is voided;
- what a transaction says about its counterparty is stored in typed fields, its bank
  account in the identifier registry; each Monzo account's own bank details are registered
  to it (M4 Decisions 10 and 11);
- after each full backfill of an account, and when an account is first seen outside one,
  the difference from Monzo's balance is posted as its opening balance adjustment, replaced
  in place, so older history fetched later corrects it instead of adding to it (ruling R18);
  any other difference is a warning, never corrected.
"""

import logging
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core import models as core_models
from lararium.core.commands import CommandContext, command_context
from lararium.core.connections import get_connection, set_warnings, update_config
from lararium.core.connectors import SyncContext
from lararium.core.entities import get_or_create_organization
from lararium.core.json_types import JSONValue
from lararium.core.raw_records import get_raw_records
from lararium.core.settings_store import get_timezone
from lararium.core.timeutil import local_date
from lararium.modules.finance import models
from lararium.modules.finance.accounts import close_account, create_account, get_account
from lararium.modules.finance.errors import FinanceValidationError
from lararium.modules.finance.identifiers import (
    ensure_identifier,
    get_or_create_identifier,
    normalize_identifier,
)
from lararium.modules.finance.instruments import InstrumentRecord, get_currency
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    TransactionStatus,
    set_counterparty_fields,
    upsert_connector_transaction,
    void_transaction,
)
from lararium.modules.finance.money import to_plain_str, tolerance
from lararium.modules.finance.snapshots import record_snapshot
from lararium.modules.finance.system_accounts import (
    SystemAccountKey,
    get_or_create_system_account,
)
from lararium.modules.finance.transfers import transfer_contra

logger = logging.getLogger(__name__)

NORMALIZER_VERSION = "monzo-norm-2"

ACCOUNT_MAP_KEY = "account_map"
# Per Monzo account: where the history its opening accounts for begins (ISO time). Recorded
# when the opening is first computed, even when none is needed (ruling R18).
OPENING_BOUNDARIES_KEY = "opening_boundaries"
# The last mismatch the owner was told about, per Monzo account, so each value is sent once.
NOTIFIED_KEY = "reconciliation_notified"
OPENING_DESCRIPTION = "Opening balance adjustment"
# Set once the transactions posted before counterparty fields existed have been given them.
COUNTERPARTY_FIELDS_KEY = "counterparty_fields_v1"
# The payment schemes that move money between bank accounts.
_BANK_TRANSFER_SCHEMES = frozenset({"payport_faster_payments", "bacs", "monzo_to_monzo"})

# By Monzo's account type; uk_rewards is the account Monzo pays rewards into.
_ACCOUNT_NAMES = {
    "uk_retail": "Monzo",
    "uk_retail_joint": "Monzo Joint",
    "uk_rewards": "Monzo Rewards",
}


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


def _bank_account(fields: dict[str, Any], whose: str) -> str | None:
    """The UK bank account (sort code and account number) the fields name, in the registry's
    form; None when they name none, or a malformed one (which is logged, never fatal)."""
    sort_code, account_number = fields.get("sort_code"), fields.get("account_number")
    if not sort_code or not account_number:
        return None
    try:
        return normalize_identifier("uk_bank_account", f"{sort_code}:{account_number}")
    except FinanceValidationError:
        # The details are the owner's or a counterparty's, so they stay out of the log.
        logger.warning("%s names a malformed bank account; stored without it", whose)
        return None


async def _connector_transaction(
    session: AsyncSession, connection_id: UUID, external_id: str
) -> tuple[UUID, TransactionStatus, str | None] | None:
    """The transaction posted under this external id: its id, status and who voided it."""
    t = models.Transaction
    row = (
        await session.execute(
            select(t.id, t.status, t.voided_by).where(
                t.connection_id == connection_id, t.external_id == external_id
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return row.id, cast(TransactionStatus, row.status), row.voided_by


async def _earliest_connector_posting(
    session: AsyncSession, account_id: UUID, connection_id: UUID, *, excluding: str
) -> datetime | None:
    """When the earliest transaction this connection posted to the account occurred, void
    ones included (history once fetched, whatever became of it), leaving out the one with
    the external id `excluding`."""
    t, p = models.Transaction, models.Posting
    return await session.scalar(
        select(func.min(t.occurred_at))
        .join(p, p.transaction_id == t.id)
        .where(
            p.account_id == account_id,
            t.connection_id == connection_id,
            t.external_id != excluding,
        )
    )


async def _ledger_balance(
    session: AsyncSession,
    account_id: UUID,
    instrument_id: UUID,
    *,
    excluding: str | None = None,
    before: datetime | None = None,
    connection_id: UUID | None = None,
) -> Decimal:
    """Everything posted to the account in the instrument, pending included, void excluded,
    and so is the transaction with the external id `excluding`; only what occurred before
    `before`, and only what the connection `connection_id` posted, when given."""
    t, p = models.Transaction, models.Posting
    query = (
        select(func.sum(p.quantity))
        .join(t, t.id == p.transaction_id)
        .where(p.account_id == account_id, p.instrument_id == instrument_id, t.status != "void")
    )
    if excluding is not None:  # IS DISTINCT FROM NULL would drop manual postings (NULL id)
        query = query.where(t.external_id.is_distinct_from(excluding))
    if before is not None:
        query = query.where(t.occurred_at < before)
    if connection_id is not None:
        query = query.where(t.connection_id == connection_id)
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


async def _posted(
    session: AsyncSession, transaction_id: UUID, account_id: UUID
) -> tuple[Decimal, datetime]:
    """What a transaction posted to the account, and when it occurred."""
    t, p = models.Transaction, models.Posting
    row = (
        await session.execute(
            select(p.quantity, t.occurred_at)
            .join(t, t.id == p.transaction_id)
            .where(t.id == transaction_id, p.account_id == account_id)
        )
    ).one()
    return row.quantity, row.occurred_at


async def _earliest_posting(
    session: AsyncSession, account_id: UUID, *, excluding: str
) -> tuple[datetime, date] | None:
    """The earliest time anything else on the account occurred, and its earliest booking
    date, each on its own: a booking date needn't follow the time (one entered by hand, or
    booked in another time zone), and the opening must precede both."""
    t, p = models.Transaction, models.Posting
    row = (
        await session.execute(
            select(func.min(t.occurred_at), func.min(t.booking_date))
            .join(p, p.transaction_id == t.id)
            .where(
                p.account_id == account_id,
                t.status != "void",
                t.external_id.is_distinct_from(excluding),
            )
        )
    ).one()
    # min() over no rows is NULL.
    occurred_at = cast(datetime | None, row[0])
    booking_date = cast(date | None, row[1])
    if occurred_at is None or booking_date is None:
        return None  # nothing else on the account
    return occurred_at, booking_date


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
        self.session_factory = ctx.session_factory
        self.connection_id = ctx.connection.id
        self.tz = tz
        self.observed_at = datetime.now(UTC)
        stored_map = config.get(ACCOUNT_MAP_KEY)
        self.account_map: dict[str, JSONValue] = (
            dict(stored_map) if isinstance(stored_map, dict) else {}
        )
        stored_boundaries = config.get(OPENING_BOUNDARIES_KEY)
        self.opening_boundaries: dict[str, JSONValue] = (
            dict(stored_boundaries) if isinstance(stored_boundaries, dict) else {}
        )
        self.report = NormalizeReport()
        self._currencies: dict[str, InstrumentRecord] = {}
        self._shared: dict[tuple[str, ...], UUID] = {}
        # Recorded by finish(), once this sync's transactions are posted.
        self._snapshots: list[tuple[UUID, int, str, datetime]] = []
        self._deleted_pots: list[UUID] = []

    async def currency(self, code: str) -> InstrumentRecord:
        if code not in self._currencies:
            self._currencies[code] = await get_currency(self.session, code)
        return self._currencies[code]

    async def unassigned(self) -> UUID:
        return await self.system_account("unassigned")

    async def _shared_row(
        self, key: tuple[str, ...], get_or_create: Callable[[CommandContext], Awaitable[UUID]]
    ) -> UUID:
        """A row other connections share (a system account, an organisation), found or created
        in a short transaction of its own and committed at once. Its creation is serialised
        across concurrent syncs, and the lock that does it must not be held until this sync,
        which may take minutes, commits."""
        if key not in self._shared:
            async with (
                self.session_factory() as session,
                command_context(session, self.command.actor, "finance.shared_row", {}) as ctx,
            ):
                self._shared[key] = await get_or_create(ctx)
        return self._shared[key]

    async def system_account(self, key: SystemAccountKey) -> UUID:
        return await self._shared_row(
            ("system_account", key), lambda ctx: get_or_create_system_account(ctx, key)
        )

    async def organization(self, name: str, external_id: tuple[str, str] | None = None) -> UUID:
        return await self._shared_row(
            ("organization", name.lower(), *(external_id or ())),
            lambda ctx: get_or_create_organization(ctx, name, external_id=external_id),
        )

    async def identifier(self, value: str) -> UUID:
        """The registry row of a bank account (in the registry's form), found or created in a
        transaction of its own: its lock must not be held until this sync commits."""
        return await self._shared_row(
            ("identifier", "uk_bank_account", value),
            lambda ctx: get_or_create_identifier(ctx, "uk_bank_account", value),
        )

    async def own_identifier(self, account: dict[str, Any]) -> None:
        """Register the Monzo account's own bank details to its finance account, unless they
        are registered already (the owner's registration elsewhere wins)."""
        value = _bank_account(account, f"Monzo account {account['id']}")
        if value is None:
            return
        # The row exists before ensure_identifier runs, so it takes no lock in this sync's
        # transaction (identifiers._get_or_create).
        await self.identifier(value)
        await ensure_identifier(
            self.command, self.require_mapped(account["id"]), "uk_bank_account", value
        )

    async def counterparty_fields(self, tx: dict[str, Any]) -> CounterpartyFields:
        """What a transaction that is not a pot move says about the other side."""
        raw = tx.get("counterparty")
        counterparty = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
        value = _bank_account(counterparty, f"transaction {tx['id']}")
        name, notes = counterparty.get("name"), tx.get("notes")
        return CounterpartyFields(
            identifier_id=None if value is None else await self.identifier(value),
            name=str(name) if name else None,
            reference=str(notes) if notes else None,
            bank_transfer=tx.get("scheme") in _BANK_TRANSFER_SCHEMES,
        )

    async def _fields_of(self, tx: dict[str, Any]) -> CounterpartyFields:
        metadata = tx.get("metadata")
        pot_id = (
            cast(dict[str, Any], metadata).get("pot_id") if isinstance(metadata, dict) else None
        )
        return CounterpartyFields() if pot_id is not None else await self.counterparty_fields(tx)

    async def fill_counterparty_fields(self) -> None:
        """Give the transactions this connection posted before counterparty fields existed
        the fields their latest raw record gives (Decision 10). Run once, before this sync's
        transactions are posted, which come with their fields."""
        r = core_models.RawRecord
        pages = await self.session.scalars(
            select(r.content)
            .where(r.connection_id == self.connection_id, r.record_type == "transactions_page")
            .order_by(r.fetched_at, r.id)
        )
        latest: dict[str, dict[str, Any]] = {}
        for page in pages:
            for tx in cast(list[dict[str, Any]], page):
                latest[tx["id"]] = tx  # a later record of the same transaction wins
        t = models.Transaction
        posted = await self.session.execute(
            select(t.id, t.external_id)
            .where(t.connection_id == self.connection_id, t.origin == "connector")
            .order_by(t.occurred_at, t.id)
        )
        for row in list(posted):
            tx = latest.get(row.external_id)
            if tx is not None:
                await set_counterparty_fields(self.command, row.id, await self._fields_of(tx))

    async def contra(
        self,
        external_id: str,
        own_account_id: UUID,
        fields: CounterpartyFields,
        *,
        other_postings: int,
    ) -> UUID:
        """Unassigned or In transit, by the owner's decisions, the transfer the transaction
        is a leg of, its counterparty's bank account, and the postings it has besides its
        own and the contra (transfer_contra)."""
        await self.unassigned()  # both must exist for transfer_contra
        await self.system_account("in_transit")
        return await transfer_contra(
            self.session,
            source=("connection", f"{self.connection_id}:{external_id}"),
            own_account_id=own_account_id,
            counterparty_identifier_id=fields.identifier_id,
            other_postings=other_postings,
        )

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
            institution_entity_id=await self.organization("Monzo"),
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
                institution_entity_id=await self.organization("Monzo"),
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
        transaction_id, status, _ = existing
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
        own_account_id = self.require_mapped(monzo_account_id)
        metadata = tx.get("metadata")
        pot_id = (
            cast(dict[str, Any], metadata).get("pot_id") if isinstance(metadata, dict) else None
        )
        fields = await self._fields_of(tx)
        # A pot that was deleted before the connection existed has no account; its moves are
        # posted like any other payment.
        other = (self.mapped(pot_id) if isinstance(pot_id, str) else None) or (
            # A payment posts its own posting and the contra, nothing besides.
            await self.contra(external_id, own_account_id, fields, other_postings=0)
        )
        merchant = tx.get("merchant")
        counterparty: UUID | None = None
        if isinstance(merchant, dict):
            merchant_fields = cast(dict[str, Any], merchant)
            if merchant_fields.get("name"):
                counterparty = await self.organization(
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
                        own_account_id, currency.id, quantity, memo=str(tx.get("notes") or "")
                    ),
                    PostingInput(other, currency.id, -quantity),
                ],
                counterparty=fields,
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

    def opening_boundary(self, monzo_id: str) -> datetime | None:
        value = self.opening_boundaries.get(monzo_id)
        return _parse(value) if isinstance(value, str) else None

    async def _earliest(self, monzo_id: str) -> datetime | None:
        return await _earliest_connector_posting(
            self.session,
            self.require_mapped(monzo_id),
            self.connection_id,
            excluding=f"opening:{monzo_id}",
        )

    def _move_boundary(self, monzo_id: str, to: datetime) -> None:
        """The boundary only ever moves earlier: the opening accounts for everything before
        the earliest point it has covered."""
        stored = self.opening_boundary(monzo_id)
        boundary = to if stored is None else min(stored, to)
        self.opening_boundaries[monzo_id] = boundary.astimezone(UTC).isoformat()

    async def adopt_boundary(self, monzo_id: str) -> None:
        """Give an account its boundary from what this connection posted before this run
        (run before it posts anything): one reconciled before boundaries were recorded has its
        opening accounting for all of that. An account with nothing posted yet gets none, and
        its first opening is computed from Monzo's balance."""
        if self.opening_boundary(monzo_id) is not None:
            return
        earliest = await self._earliest(monzo_id)
        if earliest is not None:
            self._move_boundary(monzo_id, earliest)

    async def record_opening_boundary(self, monzo_id: str, balance_at: datetime) -> None:
        """Record that the opening accounts for everything before this connection's earliest
        posting on the account. With none, it accounts for everything in the balance read at
        `balance_at`: any transaction a later fetch finds before then is in that balance, so
        it must move the opening, however recent (the account's creation would be too early
        when this run fetched only the last 89 days)."""
        earliest = await self._earliest(monzo_id)
        self._move_boundary(monzo_id, min(earliest, balance_at) if earliest else balance_at)

    async def opening(
        self, monzo_account: dict[str, Any], minor_units: int, code: str, balance_at: datetime
    ) -> None:
        """Post the difference between Monzo's balance (read at `balance_at`) and the rest of
        the ledger, the day
        before the earliest other transaction. An opening the owner voided stays void; one no
        longer needed is voided by the connector, and posted again if it is needed again.

        It is computed from Monzo's balance only the first time, which records where the
        history it accounts for begins (the boundary), even when no opening is needed. After
        that only this connection's postings before the boundary, which a later backfill
        fetched, move it (ruling R18): anything else that changed since, such as a
        transaction the owner voided or posted by hand, is a reconciliation mismatch to
        report, never absorbed into it.
        """
        monzo_id: str = monzo_account["id"]
        external_id = f"opening:{monzo_id}"
        existing = await _connector_transaction(self.session, self.connection_id, external_id)
        if existing is not None and existing[1] == "void" and existing[2] != "connector":
            self.report.unchanged += 1
            return
        posted = existing if existing is not None and existing[1] != "void" else None
        account_id = self.require_mapped(monzo_id)
        currency = await self.currency(code)
        # None only for an account with nothing posted before this run (see adopt_boundary).
        boundary = self.opening_boundary(monzo_id)
        if boundary is None:
            balance = to_quantity(minor_units, currency.decimals)
            others = await _ledger_balance(
                self.session, account_id, currency.id, excluding=external_id
            )
            difference = balance - others
        else:
            placed = (
                (await _posted(self.session, posted[0], account_id))[0]
                if posted is not None
                else Decimal(0)
            )
            older = await _ledger_balance(
                self.session,
                account_id,
                currency.id,
                excluding=external_id,
                before=boundary,
                connection_id=self.connection_id,
            )
            difference = placed - older
        await self.record_opening_boundary(monzo_id, balance_at)
        if difference == 0:
            if posted is not None:
                await void_transaction(self.command, posted[0])
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
                        await self.system_account("opening_balances"),
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
                await n.own_identifier(account)
    # Before anything is posted, so this run's transactions can't count as earlier history.
    for monzo_id in accounts:
        await n.adopt_boundary(monzo_id)
    if connection.config.get(COUNTERPARTY_FIELDS_KEY) is not True:
        await n.fill_counterparty_fields()
    balances: dict[str, tuple[dict[str, Any], datetime]] = {}
    for record_type, request, fetched_at, content in responses:
        if record_type == "pots":
            for pot in content:
                await n.pot(pot, str(request["current_account_id"]), fetched_at)
        elif record_type == "balance":
            monzo_id = str(request["account_id"])
            balances[monzo_id] = (content, fetched_at)
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
    for monzo_id, (balance, balance_at) in balances.items():
        if monzo_id not in checked:
            continue
        if monzo_id in openings:
            await n.opening(accounts[monzo_id], balance["balance"], balance["currency"], balance_at)
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
    values: dict[str, JSONValue] = {ACCOUNT_MAP_KEY: n.account_map, COUNTERPARTY_FIELDS_KEY: True}
    if n.opening_boundaries:
        values[OPENING_BOUNDARIES_KEY] = n.opening_boundaries
    if notified:
        values[NOTIFIED_KEY] = notified
    remove = [] if notified or NOTIFIED_KEY not in connection.config else [NOTIFIED_KEY]
    if remove or any(connection.config.get(key) != value for key, value in values.items()):
        await update_config(ctx.command, connection.id, values, remove=remove)
    if warnings != connection.warnings:
        await set_warnings(ctx.command, connection.id, warnings)
    return n.report
