"""Reconciling an eToro connection's accounts against what eToro reports (design §5.11,
Decision 12), after every sync and every statement upload.

Each figure is compared with the account's non-void postings that occurred at or before its
cut-off (connector_run.holdings), within the instrument's tolerance:
- **eToro Trading**: each instrument's units against the latest aggregate portfolio's
  `netUnits` (an instrument the books hold and it omits counts as reported 0), and its cash
  against PnL `credit` plus frozen cash (reports.trading_cash), each at the time its report
  arrived; and its cash against balance history's `cash` on each snapshot day since the latest
  statement's start (every day before the first statement), at the end of the day in UTC
  (Decision 11a).
- **eToro Money**: each account's currency against the latest balances' `balance`, at the
  time it arrived.
- **MoneyFarm**: nothing (a snapshots account).

**"Upload a statement" in place of a difference.** Only a statement posts Trading's transfers
with eToro Money and its fees, taxes and dividends (ruling R30). So the differences in
Trading's cash after what the latest statement covers (a snapshot day after the last day it
covers in full, covered_through; a current figure seen after its end) become one prompt,
"<Trading's name>: upload an eToro statement covering <the first day it does not cover in
full, else the account's earliest record> onward", when a statement would explain them: none
has been uploaded; or eToro
Money has an internal transfer with Trading after that end (only a statement brings Trading's
leg, so it has none yet); or the API states fees, taxes or dividends after that end (an
execution's or a close's fees or taxes, or a position's `totalFees` or
`overnightFeesAndDividends` changed since the end's London day, the day its rows are
stamped with). Any other difference is an ordinary
one. The prompt names no end (ruling R34): its text, and so its one notification, changes only
when a new statement moves its start.

The warnings are what the latest check found: each run replaces in full the reconciliation
warnings the connection's cursor records it published last (connector_flags, never matched by
their text, which holds names the owner may change), and keeps every other warning (the flags
among them). The owner is told of each warning text once, counted only when a send succeeds,
so a failed one is retried next run; a difference is never corrected. A run with no balances
yet, or whose balances do not list exactly one Trading account, cannot check: the last
check's warnings stand, with UNRECONCILED. Warnings name the account, the symbol or currency,
the day and the amount, never another value.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.connectors import SyncContext
from lararium.core.json_types import JSONValue
from lararium.modules.finance import connector_flags, models
from lararium.modules.finance.connector_run import differences, holdings
from lararium.modules.finance.errors import UnknownCurrencyError
from lararium.modules.finance.etoro.fields import STATEMENT_SHEETS
from lararium.modules.finance.etoro.reports import trading_cash
from lararium.modules.finance.etoro.state import (
    LONDON,
    EtoroDataError,
    Report,
    id_at,
    latest_sighted,
    number_at,
    objects_at,
    reports_of,
    text_at,
)
from lararium.modules.finance.identifiers import normalize_identifier
from lararium.modules.finance.instruments import (
    InstrumentRecord,
    find_by_identifier,
    get_currency,
)
from lararium.modules.finance.money import to_plain_str, tolerance
from lararium.modules.finance.report_sightings import list_sightings

logger = logging.getLogger(__name__)

PLATFORM = "eToro"
# What every reconciliation warning holds, after the account's name (Decision 12).
DIFFERS = f" differs from {PLATFORM}'s report on "
SPANS = f" differs from {PLATFORM}'s report from "
PROMPT = f": upload an {PLATFORM} statement covering "
# A run that could not check (a fixed text: it names no account the owner may have renamed).
UNRECONCILED = "eToro Trading could not be reconciled"
# eToro's units, for an instrument the books do not hold (design §5.6).
_UNIT_DECIMALS = 6


@dataclass(frozen=True)
class Difference:
    account_name: str
    symbol: str
    day: date
    difference: Decimal  # the books' less the report's
    # Trading's cash after the latest statement's end, which a statement may explain.
    explainable: bool = False
    # The last of a run of consecutive days with the same difference (history), if longer.
    last: date | None = None

    @property
    def warning(self) -> str:
        if self.last is not None and self.last != self.day:
            when = f"{SPANS}{self.day.isoformat()} to {self.last.isoformat()}"
        else:
            when = f"{DIFFERS}{self.day.isoformat()}"
        return f"{self.account_name} {self.symbol}{when} by {to_plain_str(self.difference)}"


@dataclass(frozen=True)
class Checked:
    """What one check found: the connection's reconciliation warnings (the publisher keeps
    them sorted, connector_flags)."""

    warnings: list[str]


@dataclass(frozen=True)
class Period:
    start: datetime
    end: datetime


def covered_through(end: datetime) -> date:
    """The last day a statement ending at `end` covers in full, in UTC: its end's day when it
    ends at the day's end (End Date 23:59:59), else the day before, so the rest of a day it was
    downloaded on counts as not covered (the next statement overlaps it; its rows are keyed)."""
    at = end.astimezone(UTC)
    return at.date() if at.time() >= time(23, 59, 59) else at.date() - timedelta(days=1)


def prompt(account_name: str, start: date) -> str:
    return f"{account_name}{PROMPT}{start.isoformat()} onward"


async def latest_statement(session: AsyncSession, connection_id: UUID) -> Period | None:
    """The period of the statement that reaches furthest (each row's is its upload's)."""
    rows = await session.execute(
        text(
            "select distinct request->'period'->>'from' as start,"
            " request->'period'->>'to' as finish"
            " from core.raw_records where connection_id = :c and record_type = any(:t)"
        ),
        {"c": connection_id, "t": [spec.record_type for spec in STATEMENT_SHEETS.values()]},
    )
    periods: list[Period] = []
    for row in rows:
        try:
            period = Period(datetime.fromisoformat(row.start), datetime.fromisoformat(row.finish))
        except (TypeError, ValueError):
            raise EtoroDataError("an eToro statement has a malformed period") from None
        if period.start.tzinfo is None or period.end.tzinfo is None:
            raise EtoroDataError("an eToro statement has a malformed period")
        periods.append(period)
    return max(periods, key=lambda p: (p.end, p.start), default=None)


class _Check:
    def __init__(self, session: AsyncSession, connection_id: UUID) -> None:
        self.session = session
        self.connection_id = connection_id
        self.found: list[Difference] = []

    async def compare(
        self,
        account_id: UUID,
        account_name: str,
        at: datetime,
        day: date,
        reported: Mapping[UUID, Decimal],
        *,
        only: set[UUID] | None = None,
        explainable: bool = False,
    ) -> None:
        """Each instrument (of `only`, when given) whose quantity at `at` differs from
        `reported` beyond its tolerance."""
        for symbol, difference in await differences(
            self.session, account_id, at, reported, only=only
        ):
            self.found.append(Difference(account_name, symbol, day, difference, explainable))

    async def sighted_with(self, report: Report, endpoint: str) -> Report | None:
        """The report of the endpoint the same sync saw, if any."""
        found = await reports_of(
            self.session,
            await list_sightings(self.session, self.connection_id, sync_id=report.sync_id),
        )
        matching = [r for r in found if r.endpoint == endpoint]
        return matching[-1] if matching else None

    async def units(self, trading: UUID, name: str, aggregate: Report) -> None:
        """Each instrument's units against the aggregate portfolio's."""
        reported: dict[UUID, Decimal] = {}
        for item in objects_at(aggregate.content, "instrumentAggregates"):
            etoro_id = id_at(aggregate.record, item, "instrumentId")
            units = number_at(aggregate.record, item, "netUnits")
            instrument = await find_by_identifier(self.session, "etoro_id", etoro_id)
            if instrument is not None:
                reported[instrument.id] = reported.get(instrument.id, Decimal(0)) + units
            elif units.copy_abs() > tolerance(_UNIT_DECIMALS):
                # Units the books have no instrument for (a type they do not hold, say).
                symbol = await self.session.scalar(
                    text(
                        "select symbol_full from finance.etoro_instruments"
                        " where connection_id = :c and instrument_id = :i"
                    ),
                    {"c": self.connection_id, "i": Decimal(etoro_id)},
                )
                self.found.append(
                    Difference(name, symbol or f"eToro {etoro_id}", aggregate.day, -units)
                )
        i = models.Instrument
        securities = set(
            await self.session.scalars(
                select(i.id).where(
                    i.id.in_(set(await holdings(self.session, trading, aggregate.seen_at))),
                    i.kind != "fiat",
                )
            )
        )
        await self.compare(
            trading, name, aggregate.seen_at, aggregate.day, reported, only=securities
        )

    async def history(
        self, trading: UUID, name: str, since: date | None, covered: date | None
    ) -> None:
        """Trading's cash against balance history's on each day since `since` (every day when
        None), at the end of the day (records.end_of), in one query. A day on or before
        `covered` (covered_through: the last day the latest statement covers in full) is one
        it covers, so a difference on it is ordinary. A run of consecutive days with the same
        difference is one warning, naming its first and last day."""
        rows = await self.session.execute(
            text(
                "select s.snapshot_date, s.cash, s.currency,"
                " coalesce((select sum(p.quantity) from finance.postings p"
                " join finance.transactions t on t.id = p.transaction_id"
                " join finance.instrument_identifiers ii on ii.instrument_id = p.instrument_id"
                " and ii.scheme = 'iso4217' and ii.value = upper(btrim(s.currency))"
                " where p.account_id = :a and t.status <> 'void'"
                " and t.occurred_at < cast(s.snapshot_date + 1 as timestamp) at time zone 'UTC'"
                " ), 0) as held"
                " from finance.etoro_balance_account_snapshots s"
                " where s.connection_id = :c and s.finance_account_id = :a"
                " and s.cash is not null and s.currency is not null"
                " and (cast(:since as date) is null or s.snapshot_date >= :since)"
                " order by s.snapshot_date, s.item_index"
            ),
            {"c": self.connection_id, "a": trading, "since": since},
        )
        run: Difference | None = None
        currencies: dict[str, InstrumentRecord] = {}
        for row in rows:
            if row.currency not in currencies:
                currencies[row.currency] = await _currency(self.session, row.currency)
            currency = currencies[row.currency]
            difference = Decimal(row.held) - Decimal(row.cash)
            if difference.copy_abs() <= tolerance(currency.decimals):
                continue
            explainable = covered is None or row.snapshot_date > covered
            if (
                run is not None
                and row.snapshot_date == (run.last or run.day) + timedelta(days=1)
                and (run.symbol, run.difference, run.explainable)
                == (currency.symbol, difference, explainable)
            ):
                run = replace(run, last=row.snapshot_date)
                continue
            if run is not None:
                self.found.append(run)
            run = Difference(name, currency.symbol, row.snapshot_date, difference, explainable)
        if run is not None:
            self.found.append(run)

    async def explained(self, trading: UUID, trading_etoro_id: str, end: datetime) -> bool:
        """Whether eToro has said, after the statement's end, what only a statement posts."""
        transfer = await self.session.scalar(
            text(
                "select exists (select 1 from finance.etoro_cash_transactions c"
                " join finance.transactions t on t.id = c.transaction_id"
                " join finance.account_identifiers ai on ai.id = t.counterparty_identifier_id"
                " where c.connection_id = :c and c.transaction_type = 'internalTransfer'"
                " and t.status <> 'void' and t.occurred_at > :end"
                " and ai.kind = 'etoro_account' and ai.value = :trading)"
            ),
            {"c": self.connection_id, "end": end, "trading": trading_etoro_id},
        )
        if transfer:
            return True
        stated = await self.session.scalar(
            text(
                "select exists (select 1 from finance.etoro_position_executions"
                " where connection_id = :c"
                " and (coalesce(opening_data_fees, 0) <> 0 or coalesce(opening_data_taxes, 0) <> 0)"
                " and coalesce(opening_data_execution_time, opening_data_open_time) > :end)"
                " or exists (select 1 from finance.etoro_trades where connection_id = :c"
                " and coalesce(fees, 0) <> 0 and close_timestamp > :end)"
            ),
            {"c": self.connection_id, "end": end},
        )
        if stated:
            return True
        # A London day: it is compared with the days the position rows are stamped with
        # (`as_of_date`, Report.day, the London date of their sighting). The last one that ends
        # by the statement's end (End Date 23:59:59 counting as its day's end), so every
        # sighting stamped with it or before is before the end: in BST a statement ending
        # 23:59:59 UTC ends at 00:59:59 London the next day, whose sightings are mostly after.
        day = (end + timedelta(seconds=1)).astimezone(LONDON).date() - timedelta(days=1)
        for table, column in (
            ("etoro_pnl_positions", "total_fees"),
            ("etoro_breakdown_positions", "overnight_fees_and_dividends"),
        ):
            # Each position's latest figure, against its figure on the end's day or before.
            changed = await self.session.scalar(
                text(
                    f"with rows as (select position_id, coalesce({column}, 0) as v, as_of_date"
                    f" from finance.{table} where connection_id = :c and position_id is not null),"
                    " latest as (select distinct on (position_id) position_id, v from rows"
                    " order by position_id, as_of_date desc),"
                    " before as (select distinct on (position_id) position_id, v from rows"
                    " where as_of_date <= :day order by position_id, as_of_date desc)"
                    " select exists (select 1 from latest l"
                    " left join before b on b.position_id = l.position_id"
                    " where l.v <> coalesce(b.v, 0))"
                ),
                {"c": self.connection_id, "day": day},
            )
            if changed:
                return True
        return False

    async def earliest(self, trading: UUID) -> date | None:
        """The day of the Trading account's earliest record: a posting or a snapshot day."""
        t, p = models.Transaction, models.Posting
        posted = await self.session.scalar(
            select(func.min(t.booking_date))
            .join(p, p.transaction_id == t.id)
            .where(p.account_id == trading, t.status != "void")
        )
        snapshot = await self.session.scalar(
            text(
                "select min(snapshot_date) from finance.etoro_balance_account_snapshots"
                " where connection_id = :c and finance_account_id = :a"
            ),
            {"c": self.connection_id, "a": trading},
        )
        days = [d for d in (posted, snapshot) if d is not None]
        return min(days) if days else None


async def _currency(session: AsyncSession, code: str) -> InstrumentRecord:
    try:
        return await get_currency(session, code.strip().upper())
    except UnknownCurrencyError:
        raise EtoroDataError("an eToro report names a currency Lararium does not know") from None


def _name(names: Mapping[UUID, str], account_id: UUID) -> str:
    if account_id not in names:
        raise EtoroDataError("an eToro account Lararium mapped is missing")
    return names[account_id]


async def check(ctx: SyncContext, accounts: Mapping[str, UUID]) -> Checked | None:
    """Compare the connection's accounts (eToro account id → finance account) with the latest
    reports eToro gave, as the module doc says; None when they cannot be: no balances yet, not
    exactly one Trading account among them, or data reconciliation cannot read (a malformed
    statement period, a currency Lararium does not know, a mapped account that is gone),
    which is logged by its class alone and never fails the sync or upload."""
    try:
        return await _check(ctx, accounts)
    except EtoroDataError as error:
        logger.warning("eToro reconciliation could not run (%s)", type(error).__name__)
        return None


async def _check(ctx: SyncContext, accounts: Mapping[str, UUID]) -> Checked | None:
    session = ctx.command.session
    connection_id = ctx.connection.id
    balances = await latest_sighted(session, connection_id, "balances")
    if balances is None:
        return None
    run = _Check(session, connection_id)
    names = await _names(session, set(accounts.values()))
    kinds: dict[str, tuple[str, str, Decimal]] = {}
    for entry in objects_at(balances.content, "balances"):
        etoro_id = normalize_identifier("etoro_account", text_at(entry, "accountId"))
        balance = number_at(balances.record, entry, "balance")
        kinds[etoro_id] = (text_at(entry, "accountType"), text_at(entry, "currency"), balance)

    trading_ids = [i for i, (kind, _, _) in kinds.items() if kind == "Trading" and i in accounts]
    if len(trading_ids) != 1:
        return None
    [trading_etoro_id] = trading_ids
    trading = accounts[trading_etoro_id]
    trading_name = _name(names, trading)

    # eToro Money: each account's currency against its balance.
    for etoro_id, (kind, code, balance) in sorted(kinds.items()):
        account_id = accounts.get(etoro_id)
        if kind != "Cash" or account_id is None or not code:
            continue
        currency = await _currency(session, code)
        await run.compare(
            account_id,
            _name(names, account_id),
            balances.seen_at,
            balances.day,
            {currency.id: balance},
            only={currency.id},
        )

    # eToro Trading: units, cash now, and cash on each snapshot day.
    statement = await latest_statement(session, connection_id)
    aggregate = await latest_sighted(session, connection_id, "aggregate")
    code = text_at(aggregate.content, "accountCurrency") if aggregate else ""
    code = (code or kinds[trading_etoro_id][1]).upper()
    if aggregate is not None:
        await run.units(trading, trading_name, aggregate)
    pnl = await latest_sighted(session, connection_id, "pnl")
    if pnl is not None and code:
        currency = await _currency(session, code)
        await run.compare(
            trading,
            trading_name,
            pnl.seen_at,
            pnl.day,
            {currency.id: trading_cash(pnl, await run.sighted_with(pnl, "aggregate"))},
            only={currency.id},
            explainable=statement is None or pnl.seen_at > statement.end,
        )
    await run.history(
        trading,
        trading_name,
        statement.start.astimezone(UTC).date() if statement else None,
        covered_through(statement.end) if statement else None,
    )

    explainable = [d for d in run.found if d.explainable]
    found = run.found
    asked: list[str] = []
    if explainable and (
        statement is None or await run.explained(trading, trading_etoro_id, statement.end)
    ):
        if statement is not None:
            start = covered_through(statement.end) + timedelta(days=1)
        else:
            start = await run.earliest(trading) or min(d.day for d in explainable)
        found = [d for d in found if not d.explainable]
        asked = [prompt(trading_name, start)]
    ordered = sorted(found, key=lambda d: (d.account_name, d.symbol, d.day))
    return Checked([*(d.warning for d in ordered), *asked])


async def _names(session: AsyncSession, ids: set[UUID]) -> dict[UUID, str]:
    a = models.Account
    rows = await session.execute(select(a.id, a.name).where(a.id.in_(ids)))
    return {row.id: row.name for row in rows}


async def publish(ctx: SyncContext, checked: Checked | None) -> dict[str, JSONValue]:
    """Replace the reconciliation warnings this connection last published with these, keeping
    every other warning, and tell the owner of each not told yet (connector_flags). None: the
    check could not run, so the last check's warnings stand, with UNRECONCILED. Returns the
    cursor's keys to write."""
    return await connector_flags.publish_reconciliation(
        ctx,
        None if checked is None else checked.warnings,
        label="an eToro",
        unchecked=UNRECONCILED,
    )
