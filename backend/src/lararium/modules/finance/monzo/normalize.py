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
  any other difference is a warning, never corrected;
- what Monzo says about a transaction, its merchant, an account or a pot is stored typed, a
  fee as a posting of its own, and a declined transaction outside the ledger (the
  structured-fields plan, Decisions 5-11); a field in a shape not mapped yet stays in the raw
  records, with a warning on the connection, and never fails the sync;
- the first sync under a new NORMALIZER_VERSION first replays the latest raw record of every
  transaction the connection ever posted, or whose latest record is declined
  (structured-fields Decision 12), so what a new version stores is filled in for the history
  an older one posted too; the replay never posts a transaction anew.
"""

import logging
import re
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from numbers import Real
from typing import Any, Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, and_, case, column, exists, func, or_, select, true
from sqlalchemy.dialects.postgresql import JSONB, distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext, command_context
from lararium.core.connections import get_connection, set_warnings, update_config
from lararium.core.connectors import SyncContext
from lararium.core.entities import PlaceDetails, get_or_create_organization, get_or_create_place
from lararium.core.json_types import JSONValue
from lararium.core.models import RawRecord
from lararium.core.raw_records import get_raw_records
from lararium.core.settings_store import get_timezone
from lararium.core.timeutil import local_date
from lararium.modules.finance import models
from lararium.modules.finance.accounts import (
    SourceDetails,
    TaxWrapper,
    close_account,
    create_account,
    get_account,
    set_source_details,
)
from lararium.modules.finance.declined import DeclinedInput, upsert_declined
from lararium.modules.finance.errors import UnknownCurrencyError
from lararium.modules.finance.instruments import InstrumentRecord, get_currency
from lararium.modules.finance.ledger import (
    CategorySplit,
    PostingInput,
    TransactionDetails,
    TransactionInput,
    TransactionStatus,
    upsert_connector_transaction,
    void_transaction,
)
from lararium.modules.finance.money import to_plain_str, tolerance
from lararium.modules.finance.snapshots import record_snapshot
from lararium.modules.finance.system_accounts import (
    SystemAccountKey,
    get_or_create_system_account,
)

logger = logging.getLogger(__name__)

NORMALIZER_VERSION = "monzo-norm-2"
# The version that last normalized the connection, in its config. Bumping NORMALIZER_VERSION
# makes the next sync replay the history already posted (see _Normalizer.replay).
NORMALIZER_VERSION_KEY = "normalizer_version"

ACCOUNT_MAP_KEY = "account_map"
# Per Monzo account: where the history its opening accounts for begins (ISO time). Recorded
# when the opening is first computed, even when none is needed (ruling R18).
OPENING_BOUNDARIES_KEY = "opening_boundaries"
# The last mismatch the owner was told about, per Monzo account, so each value is sent once.
NOTIFIED_KEY = "reconciliation_notified"
OPENING_DESCRIPTION = "Opening balance adjustment"

# By Monzo's account type; uk_rewards is the account Monzo pays rewards into.
_ACCOUNT_NAMES = {
    "uk_retail": "Monzo",
    "uk_retail_joint": "Monzo Joint",
    "uk_rewards": "Monzo Rewards",
}

# A pot's isa_wrapper, lowercased, as a tax wrapper; "" means none.
_TAX_WRAPPERS: dict[str, TaxWrapper] = {"isa": "isa", "lisa": "lisa", "sipp": "sipp", "gia": "gia"}


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


def _unmapped_warning(name: str) -> str:
    return f"Monzo field {name} is not stored yet; it stays in the raw records"


def _fields(value: object) -> dict[str, Any]:
    """A JSON object's fields; none for anything else, such as a missing or null object."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    """A JSON string; None when absent or empty."""
    return value if isinstance(value, str) and value else None


def _flag(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _minor_units(value: object) -> int | None:
    """A JSON integer amount; a JSON true or false is not one (bool is an int in Python)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: object) -> Decimal | None:
    """A JSON number as an exact decimal, made from its shortest form (the 51.5 the source
    wrote), never from the binary fraction the JSON parser turned it into."""
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    number = Decimal(str(value))
    return number if number.is_finite() else None


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


# A time as Monzo writes it (ISO 8601, with its offset). Anything else in `updated`, such as
# PostgreSQL's "infinity" or "now", is read as none.
_ISO_TIME = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?"
    r"(Z|[+-][0-9]{2}:?[0-9]{2})$"
)


async def _latest_transactions(
    session: AsyncSession, connection_id: UUID
) -> list[tuple[dict[str, Any], str]]:
    """The latest raw copy of each Monzo transaction the connection fetched and has posted
    (void or not), or whose latest copy is declined, with the Monzo account its page was
    fetched for. The others are left out: a replay backfills, and never posts what no sync
    posted, such as a pending payment in the pages of a discarded attempt.

    The latest has the greatest Monzo `updated` (none counts as the oldest, and so does one
    that is not a valid time in that form), then fetch time, then record id: the fetch time
    alone would mislead, as a page fetched again keeps its first one. Picked by the database,
    so memory grows with the transactions, not with the copies of them each sync stores again.
    Most recently fetched first: a merchant's place takes its details from the first
    transaction a sync sees for it.
    """
    r, t = RawRecord, models.Transaction
    elements = func.jsonb_array_elements(r.content).table_valued(column("value", JSONB))
    tx = elements.c.value
    monzo_id = tx["id"].astext
    stated = tx["updated"].astext
    # Never a cast that can fail: one malformed copy must not fail the sync.
    readable = and_(stated.regexp_match(_ISO_TIME), func.pg_input_is_valid(stated, "timestamptz"))
    updated = case((readable, stated.cast(DateTime(timezone=True))))
    latest = (
        select(tx.label("tx"), r.request["account_id"].astext.label("account"), r.fetched_at, r.id)
        .select_from(r)
        .join(elements, true())
        .where(r.connection_id == connection_id, r.record_type == "transactions_page")
        .ext(distinct_on(monzo_id))
        .order_by(monzo_id, updated.desc().nulls_last(), r.fetched_at.desc(), r.id.desc())
        .subquery()
    )
    posted = exists().where(
        t.connection_id == connection_id, t.external_id == latest.c.tx["id"].astext
    )
    # Declined in Monzo's shape, a non-empty string, which transaction() records as a decline.
    reason = latest.c.tx["decline_reason"]
    declined = and_(func.jsonb_typeof(reason) == "string", reason.astext != "")
    rows = await session.execute(
        select(latest.c.tx, latest.c.account)
        .where(or_(posted, declined))
        .order_by(latest.c.fetched_at.desc(), latest.c.id.desc(), latest.c.tx["id"].astext)
    )
    return [(row.tx, row.account) for row in rows]


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
        # Fields seen in a shape not mapped yet (structured-fields Decision 10), by their name.
        self.unmapped: set[str] = set()

    async def currency(self, code: str) -> InstrumentRecord:
        if code not in self._currencies:
            self._currencies[code] = await get_currency(self.session, code)
        return self._currencies[code]

    async def unassigned(self) -> UUID:
        return await self.system_account("unassigned")

    async def _shared_row(
        self, key: tuple[str, ...], get_or_create: Callable[[CommandContext], Awaitable[UUID]]
    ) -> UUID:
        """A row other connections share (a system account, an organisation, a place), found
        or created in a short transaction of its own and committed at once. Its creation is
        serialised across concurrent syncs, and the lock that does it must not be held until
        this sync, which may take minutes, commits."""
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
        account_id = self.mapped(monzo_id)
        if account_id is None:
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
            account_id = created.id
            self.account_map[monzo_id] = str(account_id)
        # On every sync: what Monzo says now, written only when it differs.
        await set_source_details(
            self.command,
            account_id,
            SourceDetails(
                product_type=_text(account.get("product_type")),
                owner_type=_text(account.get("owner_type")),
            ),
        )

    async def pot(self, pot: dict[str, Any], parent_monzo_id: str, fetched_at: datetime) -> None:
        pot_id: str = pot["id"]
        account_id = self.mapped(pot_id)
        if account_id is None and pot.get("deleted"):
            return  # gone before we ever saw it
        details = await self._pot_details(pot)
        if account_id is None:
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
        # On every sync, as for an account.
        await set_source_details(self.command, account_id, details)
        self.snapshot(account_id, pot["balance"], pot["currency"], fetched_at)
        if pot.get("deleted"):
            self._deleted_pots.append(account_id)

    async def _pot_details(self, pot: dict[str, Any]) -> SourceDetails:
        goal = _minor_units(pot.get("goal_amount"))
        currency = await self.currency(pot["currency"])
        return SourceDetails(
            product_type=_text(pot.get("type")),
            goal_amount=None if goal is None else to_quantity(goal, currency.decimals),
            locked=_flag(pot.get("locked")),
            round_up=_flag(pot.get("round_up")),
            round_up_multiplier=_number(pot.get("round_up_multiplier")),
            tax_wrapper=self._tax_wrapper(pot.get("isa_wrapper")),
        )

    def _tax_wrapper(self, value: object) -> TaxWrapper | None:
        """A pot's isa_wrapper as its tax wrapper: none when empty, else one Lararium knows by
        name. None leaves the account's as it is: when Monzo says nothing, and when it says
        something unknown, which warns instead of failing the sync."""
        if value is None:
            return None
        if value == "":
            return "none"
        wrapper = _TAX_WRAPPERS.get(value.lower()) if isinstance(value, str) else None
        if wrapper is None:
            self.unmapped.add("isa_wrapper")
        return wrapper

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
            await self.declined(tx, monzo_account_id)
            return
        if tx["amount"] == 0:
            # A reversed pending payment; a new zero-amount one moves no money.
            await self.void_if_posted(external_id)
            return
        currency = await self.currency(tx["currency"])
        pot_id = _fields(tx.get("metadata")).get("pot_id")
        # A pot that was deleted before the connection existed has no account; its moves stay
        # unassigned, like any other transfer.
        other = (self.mapped(pot_id) if isinstance(pot_id, str) else None) or (
            await self.unassigned()
        )
        counterparty, place = await self.merchant(tx)
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
                postings=self._postings(tx, currency, self.require_mapped(monzo_account_id), other),
                details=await self._details(tx, currency, place),
            ),
        )
        self.count(outcome)

    async def replay(self, skipping: Collection[str]) -> None:
        """Run the latest raw copy of every Monzo transaction this connection ever posted, or
        whose latest copy is declined, through transaction(), except those in `skipping`
        (structured-fields Decision 12). Through its upserts, an owner's void survives and an
        unchanged transaction is not written; nothing is posted anew."""
        for tx, monzo_account_id in await _latest_transactions(self.session, self.connection_id):
            if tx["id"] not in skipping:
                await self.transaction(tx, monzo_account_id)

    def _postings(
        self, tx: dict[str, Any], currency: InstrumentRecord, account_id: UUID, other: UUID
    ) -> list[PostingInput]:
        """Monzo's amount is what reached the balance, after its fees (structured-fields
        Decision 8): the account posts the amount before them, the other side -amount (at
        position 1, where M4 expects it), and each fee is a posting of its own on the
        account."""
        fees = {
            kind: amount
            for kind, amount in sorted(self._amounts("fees", tx.get("fees")).items())
            if amount != 0
        }
        before_fees = tx["amount"] - sum(fees.values())
        if fees and before_fees == 0:
            # All of it fees: the account's own posting would be zero, which none can be.
            self.unmapped.add("fees")
            fees, before_fees = {}, tx["amount"]
        return [
            PostingInput(
                account_id,
                currency.id,
                to_quantity(before_fees, currency.decimals),
                memo=str(tx.get("notes") or ""),
            ),
            PostingInput(other, currency.id, -to_quantity(tx["amount"], currency.decimals)),
            *(
                PostingInput(
                    account_id,
                    currency.id,
                    to_quantity(amount, currency.decimals),
                    is_fee=True,
                    memo=kind,
                )
                for kind, amount in fees.items()
            ),
        ]

    async def _details(
        self, tx: dict[str, Any], currency: InstrumentRecord, place: UUID | None
    ) -> TransactionDetails:
        if tx.get("atm_fees_detailed") is not None:
            # Always null so far: nothing shows its shape yet.
            self.unmapped.add("atm_fees_detailed")
        original_quantity, original_instrument_id = await self._original(tx)
        return TransactionDetails(
            original_quantity=original_quantity,
            original_instrument_id=original_instrument_id,
            category=_text(tx.get("category")),
            labels=self._labels(tx.get("labels")),
            include_in_spending=_flag(tx.get("include_in_spending")),
            payment_scheme=_text(tx.get("scheme")),
            place_entity_id=place,
            category_splits=self._category_splits(tx, currency.decimals),
        )

    async def _original(self, tx: dict[str, Any]) -> tuple[Decimal | None, UUID | None]:
        """The amount in the currency it was spent in, signed like the account's posting
        (structured-fields Decision 5). A currency with no instrument leaves both out, with a
        warning: its decimals, which the amount in minor units needs, are unknown."""
        code, minor_units = _text(tx.get("local_currency")), _minor_units(tx.get("local_amount"))
        if code is None or minor_units is None:
            return None, None
        try:
            local = await self.currency(code)
        except UnknownCurrencyError:
            self.unmapped.add("local_currency")
            return None, None
        return to_quantity(minor_units, local.decimals), local.id

    def _labels(self, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, list) and all(isinstance(v, str) for v in cast(list[object], value)):
            return tuple(cast(list[str], value))
        self.unmapped.add("labels")
        return ()

    def _amounts(self, name: str, value: object) -> dict[str, int]:
        """A JSON object of name -> amount in minor units ({} when absent); any other shape
        stays in the raw records, with a warning."""
        if value is None:
            return {}
        fields = _fields(value)
        amounts = {
            key: amount
            for key, item in fields.items()
            if (amount := _minor_units(item)) is not None
        }
        if isinstance(value, dict) and len(amounts) == len(fields):
            return amounts
        self.unmapped.add(name)
        return {}

    def _category_splits(self, tx: dict[str, Any], decimals: int) -> tuple[CategorySplit, ...]:
        """Monzo's split of the amount between categories, only when it is one
        (structured-fields Decision 7): more than one entry, or one other than
        {category: amount}, which is derivable."""
        amounts = self._amounts("categories", tx.get("categories"))
        if not amounts or amounts == {tx.get("category"): tx["amount"]}:
            return ()
        return tuple(
            CategorySplit(category, to_quantity(amount, decimals))
            for category, amount in sorted(amounts.items())
        )

    async def merchant(self, tx: dict[str, Any]) -> tuple[UUID | None, UUID | None]:
        """The merchant's brand, an organization merged by name, and its place, one per Monzo
        merchant id (structured-fields Decision 6): one name can cover several ids, each with
        its own address, logo and online flag."""
        merchant = _fields(tx.get("merchant"))
        name, merchant_id = _text(merchant.get("name")), _text(merchant.get("id"))
        if name is None:
            return None, None
        if merchant_id is None:
            return await self.organization(name), None
        external_id = ("monzo_merchant", merchant_id)
        organization = await self.organization(name, external_id=external_id)
        details = self._place_details(merchant, _fields(tx.get("metadata")))
        place = await self._shared_row(
            ("place", merchant_id),
            lambda ctx: get_or_create_place(ctx, name, external_id=external_id, details=details),
        )
        return organization, place

    def _place_details(self, merchant: dict[str, Any], metadata: dict[str, Any]) -> PlaceDetails:
        """The merchant, with what the transaction's metadata says about where it was paid."""
        address = _fields(merchant.get("address"))
        return PlaceDetails(
            address_street=_text(address.get("address")),
            address_city=_text(address.get("city")),
            address_region=_text(address.get("region")),
            address_postcode=_text(address.get("postcode")),
            address_country=_text(address.get("country")),
            address_formatted=_text(address.get("formatted")),
            latitude=_number(address.get("latitude")),
            longitude=_number(address.get("longitude")),
            location_approximate=_flag(address.get("approximate")),
            online=_flag(merchant.get("online")),
            mcc=self._mcc(metadata.get("mcc")),
            website=_text(_fields(merchant.get("metadata")).get("website")),
            card_acceptor_website=_text(metadata.get("card_acceptor_website")),
            logo_url=_text(merchant.get("logo")),
        )

    def _mcc(self, value: object) -> str | None:
        """The merchant category code: four ASCII digits, as the column's CHECK requires."""
        if value is None or value == "":
            return None
        if isinstance(value, str) and re.fullmatch("[0-9]{4}", value):
            return value
        self.unmapped.add("metadata.mcc")
        return None

    async def declined(self, tx: dict[str, Any], monzo_account_id: str) -> None:
        """Record a declined payment, outside the ledger (structured-fields Decision 11)."""
        currency = await self.currency(tx["currency"])
        counterparty, place = await self.merchant(tx)
        await upsert_declined(
            self.command,
            DeclinedInput(
                connection_id=self.connection_id,
                external_id=tx["id"],
                account_id=self.require_mapped(monzo_account_id),
                occurred_at=_parse(tx["created"]),
                quantity=to_quantity(tx["amount"], currency.decimals),
                instrument_id=currency.id,
                description=_description(tx),
                counterparty_entity_id=counterparty,
                place_entity_id=place,
                decline_reason=str(tx["decline_reason"]),
            ),
        )

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
    # Before anything is posted, so this run's transactions can't count as earlier history.
    for monzo_id in accounts:
        await n.adopt_boundary(monzo_id)
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
    pages = [
        (str(request["account_id"]), content)
        for record_type, request, _, content in responses
        if record_type == "transactions_page"
    ]
    # Under a new version, first the history this sync's pages don't cover, once the accounts
    # and pots it posts to are mapped (and after adopt_boundary, which must precede any posting).
    if connection.config.get(NORMALIZER_VERSION_KEY) != NORMALIZER_VERSION:
        await n.replay(skipping={tx["id"] for _, content in pages for tx in content})
    for monzo_account_id, content in pages:
        for tx in content:
            await n.transaction(tx, monzo_account_id)
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
    # Each unmapped field warns once, until the owner clears it. Added before the mismatch
    # warnings, which are taken out and added again on each sync, so the order holds.
    for name in sorted(n.unmapped):
        if (warning := _unmapped_warning(name)) not in warnings:
            warnings.append(warning)
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
    values: dict[str, JSONValue] = {
        ACCOUNT_MAP_KEY: n.account_map,
        NORMALIZER_VERSION_KEY: NORMALIZER_VERSION,
    }
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
