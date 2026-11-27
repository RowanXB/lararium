"""What the API connectors' normalizers (Trading 212's, eToro's) share: one run's ledger and
typed-table operations, the reported snapshot of a figure a sync saw, the holdings query, and a
trade's price per unit with the rounding residual it may leave (design §5.7).

Each connector subclasses `ConnectorRun` with its field list (`COLUMNS`, `RECORD_TYPES`) and the
shape of an enum value it names in flags (`CODE`), and keeps what is its own: accounts,
instruments and the mapping of its records.
"""

import re
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import ClassVar, Literal, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext, command_context
from lararium.core.connections import ConnectionRecord
from lararium.core.connectors import SyncContext
from lararium.core.timeutil import local_date
from lararium.modules.finance import models
from lararium.modules.finance.instruments import InstrumentRecord, get_currency, get_instrument
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    compute_residuals,
    upsert_connector_transaction,
    void_transaction,
)
from lararium.modules.finance.money import tolerance
from lararium.modules.finance.snapshots import record_snapshot
from lararium.modules.finance.system_accounts import (
    SystemAccountKey,
    get_or_create_system_account,
)
from lararium.modules.finance.transfers import transfer_contra
from lararium.modules.finance.typed_tables import Fields, Value, typed_values, upsert_typed

# The owner's reporting zone: a record's booking date and a report's day (Decision 11).
LONDON = ZoneInfo("Europe/London")
OPENING = "opening:"

# Platforms quote London listings in pence; the ledger lists them in pounds, the listing's
# own currency.
_SUBUNITS: Mapping[str, tuple[str, int]] = {"GBX": ("GBP", -2)}


def listing_currency(code: str) -> tuple[str, int]:
    """The ISO 4217 currency a listing quoted in `code` is held in, and the power of ten that
    turns a price in `code` into one in it."""
    return _SUBUNITS.get(code.strip().upper(), (code.strip().upper(), 0))


# Prices per unit ----------------------------------------------------------------------------

# A price per unit is cash over units, to this place.
PER_UNIT = Decimal(1).scaleb(-10)


def per_unit(total: Decimal, quantity: Decimal) -> Decimal:
    """total / quantity to PER_UNIT's place, without trailing zeros."""
    with localcontext(prec=80):
        value = (total / quantity).quantize(PER_UNIT, rounding=ROUND_HALF_EVEN)
    trimmed = value.normalize()
    exponent = trimmed.as_tuple().exponent
    assert isinstance(exponent, int)  # finite
    return trimmed if exponent <= 0 else trimmed.quantize(Decimal(1))


async def settle_rounding(
    session: AsyncSession,
    postings: list[PostingInput],
    unassigned: UUID,
    currency: InstrumentRecord,
    quantity: Decimal,
    failure: Callable[[], Exception],
) -> None:
    """A residual within the ledger's tolerance stays; one within what the price's rounding
    explains (|quantity| times half its last place) is posted to Unassigned (memo
    "rounding", design §5.7); more raises `failure()`."""
    allowed = tolerance(currency.decimals) + quantity.copy_abs() * PER_UNIT / 2
    for instrument_id, residual in compute_residuals(postings).items():
        decimals = (await get_instrument(session, instrument_id)).decimals
        if residual.copy_abs() <= tolerance(decimals):
            continue
        if instrument_id != currency.id or residual.copy_abs() > allowed:
            raise failure()
        postings.append(PostingInput(unassigned, instrument_id, -residual, memo="rounding"))


# Holdings and reported snapshots -------------------------------------------------------------


def not_opening() -> ColumnElement[bool]:
    """A transaction that is not a connector's opening adjustment."""
    return func.coalesce(models.Transaction.external_id, "").not_like(f"{OPENING}%")


async def holdings(
    session: AsyncSession, account_id: UUID, at: datetime, *also: ColumnElement[bool]
) -> dict[UUID, Decimal]:
    """The account's quantity of each instrument: its non-void postings that occurred at or
    before `at` (and meet `also`)."""
    t, p = models.Transaction, models.Posting
    return {
        row.instrument_id: Decimal(row.total)
        for row in await session.execute(
            select(p.instrument_id, func.sum(p.quantity).label("total"))
            .join(t, t.id == p.transaction_id)
            .where(p.account_id == account_id, t.status != "void", t.occurred_at <= at, *also)
            .group_by(p.instrument_id)
        )
    }


async def differences(
    session: AsyncSession,
    account_id: UUID,
    at: datetime,
    reported: Mapping[UUID, Decimal],
    *,
    only: Collection[UUID] | None = None,
) -> list[tuple[str, Decimal]]:
    """(symbol, the books' quantity less the report's) of each instrument whose holding at
    `at` differs from `reported` beyond its tolerance, by symbol: those the report names, and
    those the account holds (of `only`, when given), which a report omitting them puts at 0."""
    held = await holdings(session, account_id, at)
    ids = (set(held) if only is None else set(held) & set(only)) | set(reported)
    if not ids:
        return []
    i = models.Instrument
    described = {
        row.id: row
        for row in await session.execute(select(i.id, i.symbol, i.decimals).where(i.id.in_(ids)))
    }
    found: list[tuple[str, Decimal]] = []
    for instrument_id in ids:
        difference = held.get(instrument_id, Decimal(0)) - reported.get(instrument_id, Decimal(0))
        instrument = described[instrument_id]
        if difference.copy_abs() > tolerance(instrument.decimals):
            found.append((instrument.symbol, difference))
    return sorted(found)


async def reported_snapshot(
    ctx: CommandContext,
    account_id: UUID,
    instrument_id: UUID,
    quantity: Decimal,
    as_of: datetime,
    day: date,
) -> UUID:
    """The reported snapshot of the figure seen at `as_of`: the day's latest at or before it
    when that says the same, else a new one at `as_of`. So sightings applied again, or in
    order from the first, find the snapshots they made."""
    s = models.BalanceSnapshot
    latest = (
        await ctx.session.execute(
            select(s.id, s.quantity)
            .where(
                s.account_id == account_id,
                s.instrument_id == instrument_id,
                s.kind == "reported",
                s.as_of_date == day,
                s.as_of <= as_of,
            )
            .order_by(s.as_of.desc(), s.recorded_at.desc(), s.id.desc())
            .limit(1)
        )
    ).one_or_none()
    if latest is not None and latest.quantity == quantity:
        return latest.id
    record = await record_snapshot(
        ctx,
        account_id=account_id,
        instrument_id=instrument_id,
        quantity=quantity,
        as_of=as_of,
        as_of_date=day,
        kind="reported",
    )
    return record.id


# One run ------------------------------------------------------------------------------------


class RunReport(Protocol):
    """What a run counts and flags."""

    created: int
    updated: int
    unchanged: int
    flagged: set[str]


class ConnectorRun:
    """One normalize run of a connection: its command, the shared rows it finds or creates in
    transactions of their own, its typed-table writes, and its connector transactions."""

    # Endpoint → table → known path → column, and endpoint → record type (fields.py).
    COLUMNS: ClassVar[Mapping[str, Mapping[str, Mapping[str, str]]]]
    RECORD_TYPES: ClassVar[Mapping[str, str]]
    # An enum value fit to name in a flag.
    CODE: ClassVar[re.Pattern[str]]

    def __init__(self, ctx: SyncContext, connection: ConnectionRecord, report: RunReport) -> None:
        self.ctx = ctx
        self.command = ctx.command
        self.session = ctx.command.session
        self.connection_id = connection.id
        self.config = connection.config
        # This sync: its reports are its sightings (report_sightings).
        self.sync_id = ctx.sync_id
        self.counts = report
        self._symbols: dict[UUID, str] = {}
        self._currencies: dict[str, InstrumentRecord] = {}
        self._shared: dict[tuple[str, ...], UUID] = {}

    def flag(self, name: str) -> None:
        self.counts.flagged.add(name)

    def flag_value(self, endpoint: str, path: str, value: str) -> None:
        """An enum value the mapping does not know, named when it looks like one."""
        named = self.CODE.fullmatch(value) is not None
        self.flag(f"{endpoint}.{path}={value}" if named else f"{endpoint}.{path}")

    def count(self, outcome: Literal["created", "updated", "unchanged"]) -> None:
        setattr(self.counts, outcome, getattr(self.counts, outcome) + 1)

    async def shared(
        self, key: tuple[str, ...], work: Callable[[CommandContext], Awaitable[UUID]]
    ) -> UUID:
        """A row other connections share, found or created in a transaction of its own and
        committed at once: its lock must not be held until this sync commits."""
        if key not in self._shared:
            async with (
                self.ctx.session_factory() as session,
                command_context(session, self.command.actor, "finance.shared_row", {}) as cmd,
            ):
                self._shared[key] = await work(cmd)
        return self._shared[key]

    async def system_account(self, key: SystemAccountKey) -> UUID:
        return await self.shared(
            ("system_account", key), lambda cmd: get_or_create_system_account(cmd, key)
        )

    async def currency(self, code: str) -> InstrumentRecord:
        if code not in self._currencies:
            self._currencies[code] = await get_currency(self.session, code)
        return self._currencies[code]

    async def price(self, code: str, price: Decimal) -> tuple[UUID, Decimal]:
        """A price the platform gives in an instrument's currency, as finance.prices keeps
        it: (the listing's currency, the price in it); pence (GBX) are pounds / 100."""
        listed, exponent = listing_currency(code)
        return (await self.currency(listed)).id, price.scaleb(exponent)

    async def symbol(self, instrument_id: UUID) -> str:
        if instrument_id not in self._symbols:
            self._symbols[instrument_id] = (
                await get_instrument(self.session, instrument_id)
            ).symbol
        return self._symbols[instrument_id]

    # Typed tables ---------------------------------------------------------------------------

    async def typed(
        self, endpoint: str, table: str, values: Mapping[str, Value], under: str = ""
    ) -> dict[str, object]:
        """The values that are known fields of the table's rows, by column, typed. `under`:
        the path of the array or map the values are an entry of."""
        columns = self.COLUMNS[endpoint][table]
        by_column: dict[str, Value] = {}
        labels: dict[str, str] = {}
        for path, value in values.items():
            column = columns.get(f"{under}{path}")
            if column is not None:
                by_column[column] = value
                labels[column] = f"{self.RECORD_TYPES[endpoint]} {under}{path}"
        return dict(await typed_values(self.session, table, by_column, labels))

    async def upsert(self, table: str, key: Fields, values: Fields) -> None:
        await upsert_typed(self.command, table, key, values)

    async def prune(self, table: str, parent: Mapping[str, object], keep: int) -> None:
        """Delete the parent's child rows from index `keep` on: an array that shrank."""
        where = " and ".join(f"{column} = :{column}" for column in parent)
        await self.session.execute(
            text(f"delete from finance.{table} where {where} and item_index >= :keep"),
            {**parent, "keep": keep},
        )

    # The ledger -----------------------------------------------------------------------------

    async def live(self, key: str) -> UUID | None:
        """The connection's live transaction under the key."""
        t = models.Transaction
        return await self.session.scalar(
            select(t.id).where(
                t.connection_id == self.connection_id, t.external_id == key, t.status != "void"
            )
        )

    async def void(self, key: str) -> None:
        """Void the connection's live transaction under the key, if there is one: a record
        the platform changed to one that posts nothing leaves no entry."""
        posted = await self.live(key)
        if posted is not None:
            await void_transaction(self.command, posted, voided_by="connector")

    async def post(
        self,
        key: str,
        occurred_at: datetime,
        description: str,
        postings: Sequence[PostingInput],
        counterparty: CounterpartyFields | None = None,
    ) -> UUID:
        record, outcome = await upsert_connector_transaction(
            self.command,
            TransactionInput(
                occurred_at=occurred_at,
                booking_date=local_date(occurred_at, LONDON),
                description=description,
                postings=postings,
                origin="connector",
                connection_id=self.connection_id,
                external_id=key,
                counterparty=counterparty or CounterpartyFields(),
            ),
        )
        self.count(outcome)
        return record.id

    async def contra_for(
        self,
        key: str,
        account_id: UUID,
        other_postings: int,
        identifier_id: UUID | None,
        payment_reference: str | None = None,
    ) -> UUID:
        """Unassigned or In transit for the account's record under the key, as
        transfer_contra says (`payment_reference`, the record's, for a bank account that
        names an own account by its reference)."""
        await self.system_account("unassigned")
        await self.system_account("in_transit")
        return await transfer_contra(
            self.session,
            source=("connection", f"{self.connection_id}:{key}"),
            own_account_id=account_id,
            counterparty_identifier_id=identifier_id,
            payment_reference=payment_reference,
            other_postings=other_postings,
        )
