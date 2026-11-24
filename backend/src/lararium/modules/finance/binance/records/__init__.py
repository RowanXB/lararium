"""What every Binance record handler shares (design 5.7, plan Decision 10): the run, posting
and removing a connector transaction, fees, rounding, and reading a row's fields.

A handler gets the run and the row. The run knows the raw record being applied (`current`):
its label, its ledger key (Decision 5) and the key of its typed row, which the handler links
to the transaction it posts. Flags name a label, field, wallet, symbol or asset code, never a
value; errors name the record's key, never an amount.
"""

import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import BigInteger, Table, func, or_, select, update

from lararium.core import models as core_models
from lararium.core.commands import CommandContext, command_context
from lararium.core.connections import ConnectionRecord
from lararium.core.connectors import SyncContext
from lararium.core.errors import DisplayableError
from lararium.core.raw_records import RawRecord
from lararium.modules.finance import models
from lararium.modules.finance.binance import layout, tables
from lararium.modules.finance.binance.accounts import wallet_account
from lararium.modules.finance.binance.fetch import BALANCE_LABELS, record_type
from lararium.modules.finance.binance.instruments import asset
from lararium.modules.finance.binance.models import model_of
from lararium.modules.finance.binance.parse import ApiValue, Row
from lararium.modules.finance.identifiers import get_or_create_identifier
from lararium.modules.finance.instruments import get_instrument
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    compute_residuals,
    upsert_connector_transaction,
    void_transaction,
)
from lararium.modules.finance.money import tolerance
from lararium.modules.finance.system_accounts import (
    SystemAccountKey,
    get_or_create_system_account,
)

# The raw record types of balance reads: their brackets date what a sync's report saw.
_READ_TYPES = tuple(record_type(label) for label in BALANCE_LABELS)
# Unit costs computed by division keep this many decimal places.
_COST_PLACES = Decimal(1).scaleb(-30)
# A value a flag may show: a short upper-case code (as the parser's unknown enum values).
_CODE = re.compile(r"-?[A-Z0-9_]{1,32}")


class BinanceRecordError(DisplayableError):
    """A record that cannot be posted as Binance gives it; names its key, never an amount."""

    def __init__(self, key: str, problem: str) -> None:
        super().__init__(f"Binance record {key}: {problem}")
        self.key = key


@dataclass
class NormalizeReport:
    """What a normalising run changed; `new_flags` are those it added to `config["flags"]`."""

    created: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    records: int = 0
    flagged: set[str] = field(default_factory=set[str])
    new_flags: list[str] = field(default_factory=list[str])
    # The run's `Run.touched`: the intervals reconciliation checks again.
    touched: set[tuple[UUID, UUID, datetime]] = field(
        default_factory=set[tuple[UUID, UUID, datetime]]
    )


@dataclass(frozen=True)
class Current:
    """The raw record being applied."""

    label: str
    record: RawRecord
    # The ledger key of the record (Decision 5) and the primary key of its typed row.
    key: str
    typed_key: dict[str, object]


@dataclass
class Run:
    """One normalising run of a connection, in one transaction (`command`)."""

    ctx: SyncContext
    command: CommandContext
    connection: ConnectionRecord
    unassigned: UUID
    in_transit: UUID
    opening_balances: UUID
    report: NormalizeReport = field(default_factory=NormalizeReport)
    # (account, instrument, occurred_at) of every posting created, changed or removed.
    touched: set[tuple[UUID, UUID, datetime]] = field(
        default_factory=set[tuple[UUID, UUID, datetime]]
    )
    current: Current | None = None
    _accounts: dict[str, UUID] = field(default_factory=dict[str, UUID])
    _assets: dict[str, UUID | None] = field(default_factory=dict[str, UUID | None])
    _addresses: dict[str, UUID] = field(default_factory=dict[str, UUID])

    @classmethod
    async def start(
        cls, ctx: SyncContext, command: CommandContext, connection: ConnectionRecord
    ) -> "Run":
        async def system(key: SystemAccountKey) -> UUID:
            # In a transaction of its own: its lock is held until the transaction commits.
            async with (
                ctx.session_factory() as session,
                command_context(session, command.actor, "finance.shared_row", {}) as cmd,
            ):
                return await get_or_create_system_account(cmd, key)

        return cls(
            ctx,
            command,
            connection,
            unassigned=await system("unassigned"),
            in_transit=await system("in_transit"),
            opening_balances=await system("opening_balances"),
        )

    @property
    def session(self):
        return self.command.session

    @property
    def record(self) -> Current:
        assert self.current is not None, "no record is being applied"
        return self.current

    def flag(self, text: str) -> None:
        self.report.flagged.add(text)

    async def account(self, wallet: str) -> UUID:
        """The account of a wallet key (`spot`, `funding`, `earn`), made if new."""
        if wallet not in self._accounts:
            self._accounts[wallet] = await wallet_account(self.command, self.connection, wallet)
        return self._accounts[wallet]

    async def asset(self, code: str) -> UUID | None:
        """The instrument of an asset code; None for an `LD` mirror, which is never posted."""
        if code not in self._assets:
            self._assets[code] = await asset(self.command, self.connection.id, code, flag=self.flag)
        return self._assets[code]

    async def chain_address(self, address: str) -> UUID:
        """The registry row of a normalised chain address, made unassigned if new. In a
        transaction of its own: the row's lock must not be held until this run commits."""
        if address not in self._addresses:
            async with (
                self.ctx.session_factory() as session,
                command_context(session, self.command.actor, "finance.shared_row", {}) as cmd,
            ):
                self._addresses[address] = await get_or_create_identifier(
                    cmd, "chain_address", address
                )
        return self._addresses[address]

    def source(self, key: str) -> tuple[str, str]:
        """The transaction's stable reference for the owner's transfer decisions."""
        return ("connection", f"{self.connection.id}:{key}")

    async def typed(self, **values: object) -> None:
        """Set columns of the current record's typed row (`transaction_id`, `tx_hash`, ...)."""
        current = self.record
        table = cast(Table, model_of(layout.layout(current.label)[0].name).__table__)
        await self.session.execute(
            update(table)
            .where(
                *(table.c[name] == value for name, value in current.typed_key.items()),
                or_(*(table.c[name].is_distinct_from(value) for name, value in values.items())),
            )
            .values(values)
        )

    async def report_time(self) -> datetime:
        """When the sync that first stored the current record read the balances (the start of
        its earliest read): a refund Binance gives no time for is dated there, at or before
        every snapshot that sync cut. Reads are committed as they are made (`store_read`), so
        a run and `regenerate` find the same ones; a record no sync's read goes with (one
        stored by hand) is dated when it was stored."""
        record = self.record.record
        r = core_models.RawRecord
        first = await self.session.scalar(
            select(func.min(r.content["read_from_ms"].astext.cast(BigInteger))).where(
                r.connection_id == self.connection.id,
                r.sync_id == record.sync_id,
                r.record_type.in_(_READ_TYPES),
            )
        )
        return tables.moment(first) if first is not None else record.fetched_at


type Handler = Callable[[Run, Row], Awaitable[None]]


# Reading a row.


def text(row: Row, name: str) -> str | None:
    """A text field; absent or blank is None."""
    value = row.get(name)
    if value is None or isinstance(value, bool | list | dict):
        return None
    shown = (
        str(int(value))
        if isinstance(value, Decimal) and value == value.to_integral()
        else str(value)
    )
    return shown.strip() or None


def code(value: str | None) -> str:
    """A value as a flag shows it: a short code as it is, else only that there is one."""
    if value is None:
        return "missing"
    return value if _CODE.fullmatch(value) else "<other>"


def amount(label: str, row: Row, name: str) -> Decimal | None:
    value = row.get(name)
    if value is None:
        return None
    return tables.decimal_value(label, name, value)


def when(label: str, row: Row, name: str) -> datetime | None:
    value = row.get(name)
    if value is None or value == "":
        return None
    return tables.time_value(label, name, value)


def integer(row: Row, name: str) -> int | None:
    value: ApiValue = row.get(name)
    if isinstance(value, bool) or not isinstance(value, int | Decimal | str):
        return None
    try:
        number = Decimal(value)
    except ArithmeticError:
        return None
    return int(number) if number.is_finite() and number == number.to_integral() else None


def unit_cost(given: Decimal, received: Decimal) -> Decimal:
    """What one unit received cost in the given instrument, to 30 decimal places: the balance
    check then leaves a residual far below any instrument's tolerance."""
    with localcontext(prec=120):
        cost = (given / received).quantize(_COST_PLACES)
        return cost.quantize(Decimal(1)) if cost == cost.to_integral() else cost.normalize()


# Writing the ledger.


async def _existing(run: Run, key: str) -> tuple[UUID, str, datetime] | None:
    t = models.Transaction
    row = (
        await run.session.execute(
            select(t.id, t.status, t.occurred_at).where(
                t.connection_id == run.connection.id, t.external_id == key
            )
        )
    ).one_or_none()
    return None if row is None else (row.id, row.status, row.occurred_at)


async def posted_at(run: Run, key: str) -> datetime | None:
    """When the connection's transaction `key` happened, if it has one: a time derived from a
    sighting (a refund Binance gives no time for) is fixed by the first."""
    found = await _existing(run, key)
    return None if found is None else found[2]


async def live(run: Run, key: str) -> bool:
    """Whether the connection has a transaction `key` that is not void."""
    found = await _existing(run, key)
    return found is not None and found[1] != "void"


async def _postings_of(run: Run, transaction_id: UUID) -> list[tuple[UUID, UUID]]:
    p = models.Posting
    rows = await run.session.execute(
        select(p.account_id, p.instrument_id).where(p.transaction_id == transaction_id)
    )
    return [(row.account_id, row.instrument_id) for row in rows]


async def _touch(run: Run, transaction_id: UUID, occurred_at: datetime) -> None:
    for account_id, instrument_id in await _postings_of(run, transaction_id):
        run.touched.add((account_id, instrument_id, occurred_at))


async def post(
    run: Run,
    key: str,
    *,
    occurred_at: datetime,
    description: str,
    postings: Sequence[PostingInput],
    counterparty_identifier_id: UUID | None = None,
    counterparty_name: str | None = None,
    never_transfer: bool = False,
) -> UUID | None:
    """Upsert the connector transaction `key` (UTC, booked on its UTC date) and record the
    postings it creates, changes or removes; its id. A zero posting is left out (the ledger
    has none); with fewer than two left, the transaction no longer applies: it is removed and
    None returned."""
    postings = [p for p in postings if p.quantity != 0]
    if len(postings) < 2:
        await remove(run, key)
        return None
    before = await _existing(run, key)
    if before is not None and before[1] != "void":
        old_postings = await _postings_of(run, before[0])
    else:
        old_postings = []
    utc = occurred_at.astimezone(UTC)
    record, outcome = await upsert_connector_transaction(
        run.command,
        TransactionInput(
            occurred_at=utc,
            occurred_tz="UTC",
            # Binance's day is UTC (design 5.9).
            booking_date=utc.date(),
            description=description,
            postings=postings,
            origin="connector",
            connection_id=run.connection.id,
            external_id=key,
            counterparty=CounterpartyFields(
                identifier_id=counterparty_identifier_id,
                name=counterparty_name,
                never_transfer=never_transfer,
            ),
        ),
    )
    _count(run, outcome)
    if outcome != "unchanged":
        if before is not None:
            for account_id, instrument_id in old_postings:
                run.touched.add((account_id, instrument_id, before[2]))
        await _touch(run, record.id, record.occurred_at)
    return record.id


def _count(run: Run, outcome: Literal["created", "updated", "unchanged"]) -> None:
    setattr(run.report, outcome, getattr(run.report, outcome) + 1)


async def remove(run: Run, key: str) -> None:
    """Void the connection's live transaction `key`, if there is one: what it posted no
    longer applies (a reward paid out as a dividend, a fee that is now zero)."""
    before = await _existing(run, key)
    if before is None or before[1] == "void":
        return
    await _touch(run, before[0], before[2])
    await void_transaction(run.command, before[0], voided_by="connector")
    run.report.removed += 1


def fees(
    account_id: UUID, unassigned: UUID, charged: Iterable[tuple[UUID, Decimal, str]]
) -> list[PostingInput]:
    """Each non-zero fee taken from the account (`is_fee`), then Unassigned's counterpart per
    instrument (Decision 10)."""
    postings: list[PostingInput] = []
    totals: dict[UUID, Decimal] = {}
    for instrument_id, taken, memo in charged:
        if taken == 0:
            continue
        postings.append(PostingInput(account_id, instrument_id, -taken, is_fee=True, memo=memo))
        totals[instrument_id] = totals.get(instrument_id, Decimal(0)) + taken
    postings += [PostingInput(unassigned, i, total) for i, total in totals.items() if total != 0]
    return postings


async def rounding(
    run: Run,
    postings: list[PostingInput],
    *,
    currency: UUID,
    quantity: Decimal,
    price: Decimal,
    problem: str,
) -> None:
    """The residual a rounded price leaves (M5 5.7): one within the ledger's tolerance stays;
    one in `currency` within |quantity| times half a unit of the price's last decimal place,
    plus the currency's tolerance, is posted to Unassigned (memo "rounding"); anything more
    fails the run, naming the record's key."""
    decimals = (await get_instrument(run.session, currency)).decimals
    allowed = tolerance(decimals)
    exponent = price.as_tuple().exponent
    assert isinstance(exponent, int)  # finite
    # A price written with a positive exponent (`1E+3`) is still exact to the unit.
    allowed += quantity.copy_abs() * Decimal(1).scaleb(min(exponent, 0)) / 2
    for instrument_id, residual in compute_residuals(postings).items():
        own = (await get_instrument(run.session, instrument_id)).decimals
        if residual.copy_abs() <= tolerance(own):
            continue
        if instrument_id != currency or residual.copy_abs() > allowed:
            raise BinanceRecordError(run.record.key, problem)
        postings.append(PostingInput(run.unassigned, instrument_id, -residual, memo="rounding"))
