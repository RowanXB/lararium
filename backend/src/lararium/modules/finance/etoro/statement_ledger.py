"""The eToro account statement in the ledger (design §5.3, §5.6-5.7, §5.12): its cash events,
the trades it enriches or posts, and every row typed.

A statement row is a raw record `etoro.statement.<sheet>` of its cells and key (Decision 10);
its statement's period is the record's request (`request["period"]`), the first upload's for a
row two statements share. A key two records hold (a corrected re-issue) is the latest one's.

**Cash.** The cash a row moves is its `Realized Equity Change`, except an "Open Position"
(- `Amount`), a "Position closed" (+ `Amount`), and a "Withdraw Fee Cancelled", which moves
nothing (its refund is a positive "Withdraw Fee" row of its own). Times are UTC (Decision 11a).
Each cash event is one connector transaction, `activity:<row key>`:
- "Transfer: GBP > USD" (+) and "Transfer: USD > GBP" (-) are legs of a transfer with eToro
  Money, identified by its `etoro_account` (the Cash account in the transfer's other currency,
  else the only one; flagged when neither is one), so the transfer engine sees both sides;
- "Deposit Conversion Fee" and "Withdrawal Conversion Fee" post nothing (the fee is netted in
  the transfer): each is its transfer's leg fee (`finance.transfer_leg_fees`, |Amount| in the
  account's currency), the transfer being the one of its direction at the same second, else
  the row right before it; one with no such transfer is flagged;
- "Withdraw Request" and "Withdraw Request Cancelled" go through transfer_contra as bank
  transfers naming no account, unless they are a cancelled pair: a "Withdraw Request
  Cancelled" cancels the latest earlier "Withdraw Request" of the opposite cash that nothing
  cancels yet;
- fees and income go against Unassigned, fees (`is_fee`) as such: "Withdraw Fee", overnight
  fees, SDRT; dividends (their withholding tax, from the Dividends sheet, a fee posting of its
  own), interest, refunds, credits, adjustments, stocks lending. A positive "Withdraw Fee" is
  a refund when a "Withdraw Fee Cancelled" row of its |Amount| is at most a second from it
  (each such row vouches for the nearest refund no other does, the earlier on a tie); a
  refund cancels the latest earlier "Withdraw Fee" of the opposite cash that nothing cancels
  yet, a cancelled pair as a request and its cancellation are;
- any other Type is flagged by name and posts nothing.
A cancelled pair (rows taken by time, then row id; one whose cash is no number is in none)
posts both rows as above but against In transit, which it leaves level, the cancelled one
first and the other naming it (`cancels_transaction_id`); a run that brings either row
re-posts the other from its stored row. When the owner has voided either, each posts as in no
pair (cancelled-withdrawal design §4.1, §4.3).
The Types the owner's statements have not shown (all but transfers, conversion fees,
withdrawals, withdrawal fees, opens and closes) are provisional: Account Summary's names.

**Trades** (records.py posts them, merge.py says from which source): a position's "Open
Position" and "Position closed" rows and its Closed Positions row. A position only the
statement has is real when Closed Positions says it is a long, unleveraged, uncopied stock or
ETF; its instrument is found by Closed Positions' ISIN in the currency its activity names
("<symbol>/<currency>"), or created so. Closed Positions' columns go onto the trade's
`etoro_trades` row (`statement_*`), and its ISIN onto the API's instrument (`isins`): merged
with another that holds it in the same currency (instruments.merge_instruments), before the
run finds any instrument.

Errors and flags name sheets, columns and Types, never a cell of Details or Notes, a name or
an amount.
"""

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import UUID

from sqlalchemy import select, text

from lararium.core import models as core_models
from lararium.core.raw_records import RawRecord, get_raw_records
from lararium.core.timeutil import local_date
from lararium.modules.finance import models
from lararium.modules.finance.connector_items import SHOWN_NAME
from lararium.modules.finance.errors import FinanceValidationError
from lararium.modules.finance.etoro import accounts
from lararium.modules.finance.etoro.fields import (
    ACCOUNT_ACTIVITY,
    ACCOUNT_SUMMARY,
    CLOSED_POSITIONS,
    DIVIDENDS,
    FINANCIAL_SUMMARY,
    LABEL,
    OCCURRENCE,
    REPEATED_LABELS,
    SECOND_VALUE,
    STATEMENT_COLUMNS,
    STATEMENT_KINDS,
    STATEMENT_SHEETS,
    SUMMARY_KINDS,
    VALUE,
)
from lararium.modules.finance.etoro.instruments import merge_instruments, statement_listing
from lararium.modules.finance.etoro.merge import Describes, TradeEvent
from lararium.modules.finance.etoro.state import LONDON, EtoroDataError, Run, ending
from lararium.modules.finance.etoro.statement import (
    STATEMENT_ZONE,
    StatementError,
    read_currencies,
    read_day,
    read_moment,
)
from lararium.modules.finance.instruments import (
    InstrumentIdentifier,
    InstrumentKind,
    add_identifiers,
    find_by_identifier,
    normalize_identifier,
)
from lararium.modules.finance.ledger import CounterpartyFields, PostingInput
from lararium.modules.finance.typed_tables import Value, typed_values, upsert_typed

ACTIVITY = "activity:"
OPEN_POSITION = "Open Position"
POSITION_CLOSED = "Position closed"
WITHDRAW_REQUEST = "Withdraw Request"
WITHDRAW_REQUEST_CANCELLED = "Withdraw Request Cancelled"
WITHDRAW_FEE = "Withdraw Fee"
WITHDRAW_FEE_CANCELLED = "Withdraw Fee Cancelled"
# How far from its refund a "Withdraw Fee Cancelled" row may be: eToro may write one operation
# across a second's boundary.
_REFUND_MARGIN = timedelta(seconds=1)
DIVIDEND = "dividend"
UNATTACHED_FEE = "a conversion fee row names no transfer"
NO_MONEY_ACCOUNT = "a statement transfer names no single eToro Money account"
UNPLACED = "a statement position Lararium cannot place"
ISIN_ELSEWHERE = "a statement ISIN another listing holds in another currency"
NOT_MERGED = "instruments a statement shows to be one could not be merged"
CURRENCIES_DISAGREE = "statement currencies disagree for"
_TRANSFER = re.compile(r"Transfer: ([A-Z]{3}) > ([A-Z]{3})")
_CONVERSION_FEES: Mapping[str, Literal["in", "out"]] = {
    "Deposit Conversion Fee": "in",
    "Withdrawal Conversion Fee": "out",
}
_BANK = {WITHDRAW_REQUEST: "Withdrawal", WITHDRAW_REQUEST_CANCELLED: "Withdrawal cancelled"}


@dataclass(frozen=True)
class _Kind:
    description: str
    fee: bool


# Against Unassigned, by Type in lower case.
_UNASSIGNED: Mapping[str, _Kind] = {
    "withdraw fee": _Kind("Withdrawal fee", True),
    DIVIDEND: _Kind("Dividend", False),
    "overnight fee": _Kind("Overnight fee", True),
    "rollover fee": _Kind("Overnight fee", True),
    "sdrt": _Kind("SDRT charge", True),
    "sdrt charge": _Kind("SDRT charge", True),
    "interest": _Kind("Interest", False),
    "interest payment": _Kind("Interest", False),
    "refund": _Kind("Refund", False),
    "credit": _Kind("Credit", False),
    "adjustment": _Kind("Adjustment", False),
    "stocks lending": _Kind("Stocks lending", False),
}
# Closed Positions' Type of a position the ledger holds, in lower case.
_KINDS: Mapping[str, InstrumentKind] = {"stocks": "equity", "etf": "etf"}
# What eToro writes where a number has none.
_NO_NUMBER = frozenset({"", "-", "N/A"})
# An open's or close's Details: the symbol and the asset currency.
_DETAILS = re.compile(r"([A-Za-z0-9.\-]{1,20})/([A-Z]{3})")
_SHEET_OF = {spec.record_type: name for name, spec in STATEMENT_SHEETS.items()}


# Rows ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """One statement row as stored: its raw record, sheet, key, cells, and the period of the
    statement that first brought it."""

    id: UUID
    sheet: str
    key: str
    cells: Mapping[str, str]
    period_from: datetime
    period_to: datetime
    document_sha256: str
    fetched_at: datetime

    def cell(self, column: str) -> str:
        return self.cells.get(column, "").strip()

    @property
    def type(self) -> str:
        return self.cell("Type")


def _period(raw: RawRecord, part: str) -> datetime:
    period = raw.request.get("period")
    value = period.get(part) if isinstance(period, dict) else None
    if not isinstance(value, str):
        raise EtoroDataError(f"{raw.record_type} has no statement period")
    try:
        found = datetime.fromisoformat(value)
    except ValueError:
        found = None
    if found is None or found.tzinfo is None:
        raise EtoroDataError(f"{raw.record_type} has a malformed statement period")
    return found


def _row(raw: RawRecord) -> Row | None:
    sheet = _SHEET_OF.get(raw.record_type)
    content = raw.content
    if sheet is None or not isinstance(content, dict):
        return None
    cells, key = content.get("cells"), content.get("key")
    if not isinstance(cells, dict) or not isinstance(key, str):
        raise EtoroDataError(f"{raw.record_type} has no cells or key")
    sha = raw.request.get("document_sha256")
    return Row(
        raw.id,
        sheet,
        key,
        {name: value for name, value in cells.items() if isinstance(value, str)},
        _period(raw, "from"),
        _period(raw, "to"),
        sha if isinstance(sha, str) else "",
        raw.fetched_at,
    )


def number(row: Row, column: str) -> Decimal | None:
    """A cell as a number; None for eToro's placeholders. A number in brackets is negative
    (Account Summary: "Values shown in brackets indicate negative amounts")."""
    found = _converted(row.cells.get(column, ""), "numeric", row.sheet, column)
    return None if found is None else Decimal(str(found))


def moment(row: Row, column: str) -> datetime | None:
    value = row.cell(column)
    if value in _NO_NUMBER:
        return None
    try:
        return read_moment(value, column, STATEMENT_ZONE, row.sheet)
    except StatementError as error:
        raise EtoroDataError(str(error)) from None


def _when(row: Row) -> datetime:
    found = moment(row, "Date")
    if found is None:
        raise EtoroDataError(f"{row.sheet}.Date is missing")
    return found


def _position_id(row: Row) -> str | None:
    value = number(row, "Position ID")
    if value is None:
        return None
    if value != value.to_integral_value() or value <= 0:
        raise EtoroDataError(f"{row.sheet}.Position ID is not an id")
    return str(int(value))


@dataclass
class Trade:
    """What the statement says of one position."""

    open: Row | None = None
    close: Row | None = None
    closed: Row | None = None


@dataclass
class Statement:
    """The statement rows one run applies (`given`: this run's, or every stored one on a
    replay), every stored one (`stored`, the latest copy of each key), and each position's."""

    given: list[Row]
    stored: list[Row]
    versions: list[Row]
    trades: dict[str, Trade]
    # Row key → the transaction it posts or enriches (cash_events).
    links: dict[str, UUID | None] = field(default_factory=dict[str, UUID | None])

    def position_ids(self) -> set[str]:
        """The positions this run's rows name."""
        found: set[str] = set()
        for row in self.given:
            if row.sheet == CLOSED_POSITIONS or (
                row.sheet == ACCOUNT_ACTIVITY and row.type in (OPEN_POSITION, POSITION_CLOSED)
            ):
                if (position_id := _position_id(row)) is not None:
                    found.add(position_id)
        return found

    def of(self, sheet: str) -> list[Row]:
        return [r for r in self.given if r.sheet == sheet]


async def load(run: Run, raw_record_ids: Sequence[UUID], replay: bool) -> Statement:
    """The statement rows the run applies, and every stored one."""
    r = core_models.RawRecord
    ids = list(
        await run.session.scalars(
            select(r.id)
            .where(
                r.connection_id == run.connection_id,
                r.record_type.in_([spec.record_type for spec in STATEMENT_SHEETS.values()]),
            )
            .order_by(r.fetched_at, r.id)
        )
    )
    versions = [row for raw in await get_raw_records(run.session, ids) if (row := _row(raw))]
    latest = {row.key: row for row in versions}  # oldest first: the latest copy stays
    stored = sorted(latest.values(), key=lambda row: row.id)
    wanted = set(raw_record_ids)
    keys = {row.key for row in versions if replay or row.id in wanted}
    given = [row for row in stored if row.key in keys]
    trades: dict[str, Trade] = {}
    for row in stored:
        if row.sheet not in (CLOSED_POSITIONS, ACCOUNT_ACTIVITY):
            continue
        position_id = _position_id(row)
        if position_id is None:
            continue
        trade = trades.setdefault(position_id, Trade())
        if row.sheet == CLOSED_POSITIONS:
            trade.closed = row
        elif row.type == OPEN_POSITION:
            trade.open = row
        elif row.type == POSITION_CLOSED:
            trade.close = row
    return Statement(given, stored, versions, trades)


# Trades ----------------------------------------------------------------------------------


def _event(
    units: Decimal | None,
    cash: Decimal | None,
    profit: Decimal | None,
    at: datetime | None,
    row: Row,
    position_id: str,
    describes: Describes,
) -> TradeEvent:
    if units is None or cash is None or at is None:
        raise EtoroDataError(
            f"{row.sheet}: position {ending(position_id)} lacks its units, amount or date"
        )
    return TradeEvent(units.copy_abs(), cash, profit, local_date(at, LONDON), at, describes)


def open_event(trade: Trade | None, position_id: str) -> TradeEvent | None:
    """The statement's open of the position: Closed Positions' amount, units and open date
    once it has closed (what closed is what was opened: a partial close's "Open Position" row
    is the whole position's, its remainder a position of its own, design §6), else its "Open
    Position" row. What it describes (merge.TradeEvent.describes): the position as opened;
    or what closed, the whole position when it closed as many units as the "Open Position"
    row opened, else a part (or not known to be the whole)."""
    if trade is None:
        return None
    if trade.closed is not None:
        whole = trade.open is not None and number(trade.open, "Units / Contracts") == number(
            trade.closed, "Units / Contracts"
        )
        row, at, describes = trade.closed, "Open Date", "closed_whole" if whole else "closed_part"
    elif trade.open is not None:
        row, at, describes = trade.open, "Date", "opened"
    else:
        return None
    amount = number(row, "Amount")
    return _event(
        number(row, "Units / Contracts"),
        None if amount is None else amount.copy_abs(),
        None,
        moment(row, at),
        row,
        position_id,
        describes,
    )


# What a close describes, from either source: what closed (the two are always comparable).
CLOSE: Describes = "closed_part"


def close_event(trade: Trade | None, position_id: str) -> TradeEvent | None:
    """The statement's close of the position: the cash its "Position closed" row moves,
    else Closed Positions' amount and profit; the profit is Closed Positions' Profit(USD),
    else the close row's Realized Equity Change."""
    if trade is None:
        return None
    profit = number(trade.closed, "Profit(USD)") if trade.closed is not None else None
    if (row := trade.close) is not None:
        if profit is None:
            profit = number(row, "Realized Equity Change")
        return _event(
            number(row, "Units / Contracts"),
            number(row, "Amount"),
            profit,
            moment(row, "Date"),
            row,
            position_id,
            CLOSE,
        )
    if (row := trade.closed) is not None:
        amount = number(row, "Amount")
        cash = None if amount is None or profit is None else amount.copy_abs() + profit
        return _event(
            number(row, "Units / Contracts"),
            cash,
            profit,
            moment(row, "Close Date"),
            row,
            position_id,
            CLOSE,
        )
    return None


def settlement(trade: Trade) -> Literal["real", "unsupported", "unknown"]:
    """A position only the statement has: real when Closed Positions says it is a long,
    unleveraged, uncopied stock or ETF; unknown without its Closed Positions row."""
    closed = trade.closed
    if closed is None:
        return "unknown"
    copied = closed.cell("Copied From")
    if (
        closed.type.casefold() not in _KINDS
        or number(closed, "Leverage") != 1
        or closed.cell("Long / Short").casefold() != "long"
        or copied not in ("", "-")
    ):
        return "unsupported"
    return "real"


async def posted_instrument(run: Run, position_id: str) -> UUID | None:
    """The instrument the position's open posted, else its close's."""
    found = await run.session.scalar(
        text(
            "select p.instrument_id from finance.postings p"
            " join finance.transactions t on t.id = p.transaction_id"
            " join finance.instruments i on i.id = p.instrument_id"
            " where t.connection_id = :c and t.external_id in (:o, :x) and t.status <> 'void'"
            " and i.kind <> 'fiat' order by t.external_id = :o desc limit 1"
        ),
        {"c": run.connection_id, "o": f"open:{position_id}", "x": f"close:{position_id}"},
    )
    return found if isinstance(found, UUID) else None


def _isin(row: Row) -> str | None:
    value = row.cell("ISIN")
    return normalize_identifier("isin", value) if value else None


async def instrument(run: Run, position_id: str, trade: Trade) -> UUID | None:
    """The instrument of a position only the statement has (module doc); None, flagged, when
    the statement does not say which it is."""
    if (found := await posted_instrument(run, position_id)) is not None:
        return found
    closed = trade.closed
    isin = _isin(closed) if closed is not None else None
    details = _details(run, trade)
    kind = _KINDS.get(closed.type.casefold()) if closed is not None else None
    if isin is None or details is None or kind is None:
        return None  # flagged in full (records.unplaced)
    symbol, currency = details
    return await statement_listing(run.command, isin, currency, symbol, kind)


def placeable(trade: Trade) -> bool:
    """Whether the statement says which instrument a real position only it has is on: its
    Closed Positions row's ISIN and Type, and the currency its activity names (instrument)."""
    closed = trade.closed
    return (
        closed is not None
        and _isin(closed) is not None
        and closed.type.casefold() in _KINDS
        and bool(_stated(trade))
    )


def _stated(trade: Trade) -> list[tuple[str, str]]:
    """(symbol, asset currency) each of the open's and close's Details names
    ("<symbol>/<currency>")."""
    return [
        (found.group(1), found.group(2))
        for row in (trade.open, trade.close)
        if row is not None and (found := _DETAILS.fullmatch(row.cell("Details")))
    ]


def _details(
    run: Run, trade: Trade, first: tuple[str, str] | None = None
) -> tuple[str, str] | None:
    """What the trade's Details name, the first stated (`first`, another position's of the
    instrument, else its own) kept, and a currency differing from it flagged by symbol."""
    for symbol, currency in _stated(trade):
        if first is None:
            first = (symbol, currency)
        elif currency != first[1]:
            run.flag(f"{CURRENCIES_DISAGREE} {first[0]}")
    return first


async def isins(run: Run, named: Sequence[tuple[str, Trade]]) -> None:
    """What the statement says of the API's instruments (position's eToro instrument id, its
    statement), before the run finds any: the asset currency its activity names, where the
    API has given none; and the ISIN Closed Positions brings, merging the instrument into
    another that holds it in the same currency (design §5.6). Details naming two currencies
    for one instrument (an open and its close, or two positions) keep the first, flagged by
    symbol. An ISIN another listing holds
    in another currency, and a merge the registry refuses (both hold an eToro id, say), are
    flagged and left. After a merge the run forgets the instruments it found."""
    merged = False
    stated: dict[str, tuple[str, str]] = {}
    for etoro_id, trade in named:
        if (details := _details(run, trade, stated.get(etoro_id))) is not None:
            stated[etoro_id] = details
            run.asset_currencies.setdefault(etoro_id, details[1])
        if trade.closed is None or (isin := _isin(trade.closed)) is None:
            continue
        run.isins[etoro_id] = isin
        found = await find_by_identifier(run.session, "etoro_id", etoro_id)
        if found is None or any(i.scheme == "isin" for i in found.identifiers):
            continue  # `listing` finds the ISIN's holder when it creates the instrument
        holder = await find_by_identifier(run.session, "isin", isin)
        if holder is None:
            await add_identifiers(run.command, found.id, [InstrumentIdentifier("isin", isin)])
        elif holder.quote_instrument_id != found.quote_instrument_id:
            run.flag(ISIN_ELSEWHERE)
        else:
            try:
                await merge_instruments(run.command, holder.id, found.id)
            except FinanceValidationError:
                # Refused before it wrote anything: the two stay apart.
                run.flag(NOT_MERGED)
                continue
            merged = True
    if merged:
        run.forget_instruments()


async def closed_row(
    run: Run, row: Row, position_id: str, transaction_id: UUID | None, instrument_id: UUID | None
) -> None:
    """Closed Positions' columns onto the trade's row (design §5.7: both sources kept)."""
    values = await _typed(run, row, CLOSED_POSITIONS, "etoro_trades")
    await run.upsert(
        "etoro_trades",
        {"connection_id": run.connection_id, "external_id": f"close:{position_id}"},
        values
        | {
            "statement_raw_record_id": row.id,
            "statement_row_key": row.key,
            "transaction_id": transaction_id,
            "finance_instrument_id": instrument_id,
        },
    )


# Cash events -----------------------------------------------------------------------------


def _cash_column(row: Row) -> str | None:
    """The column an Account Activity row's cash is read from (module doc); None for one that
    moves nothing."""
    if row.type in (OPEN_POSITION, POSITION_CLOSED):
        return "Amount"
    if row.type == WITHDRAW_FEE_CANCELLED:
        return None
    return "Realized Equity Change"


def cash(row: Row) -> Decimal | None:
    """The cash an Account Activity row moves (module doc); None when its column holds
    eToro's placeholder rather than a number."""
    column = _cash_column(row)
    if column is None:
        return Decimal(0)
    found = number(row, column)
    if found is None:
        return None
    return -found.copy_abs() if row.type == OPEN_POSITION else found


def _cash_of(run: Run, row: Row) -> Decimal | None:
    """The row's cash, or None, flagged, when it has none: it is not posted as 0."""
    found = cash(row)
    if found is None:
        run.flag(f"{row.sheet}.{_cash_column(row)} has no number")
    return found


def _direction(row: Row, currency: str) -> tuple[Literal["in", "out"], str] | None:
    """A transfer row's direction on the Trading account and its other currency."""
    found = _TRANSFER.fullmatch(row.type)
    if found is None:
        return None
    source, target = found.group(1), found.group(2)
    if target == currency and source != currency:
        return "in", source
    if source == currency and target != currency:
        return "out", target
    return None


def _fee_links(rows: Sequence[Row], currency: str) -> tuple[dict[str, Row], set[str]]:
    """Each conversion-fee row's transfer row (module doc), and the fee rows that have none."""
    ordered = sorted(
        (r for r in rows if r.sheet == ACCOUNT_ACTIVITY), key=lambda r: (_when(r), r.id)
    )
    by_moment: defaultdict[datetime, list[Row]] = defaultdict(list)
    for row in ordered:
        by_moment[_when(row)].append(row)
    links: dict[str, Row] = {}
    unattached: set[str] = set()
    for n, row in enumerate(ordered):
        wanted = _CONVERSION_FEES.get(row.type)
        if wanted is None:
            continue
        same = [r for r in by_moment[_when(row)] if _is_leg(r, wanted, currency)]
        if len(same) == 1:
            links[row.key] = same[0]
        elif not same and n > 0 and _is_leg(ordered[n - 1], wanted, currency):
            links[row.key] = ordered[n - 1]
        else:
            unattached.add(row.key)
    return links, unattached


def _is_leg(row: Row, direction: Literal["in", "out"], currency: str) -> bool:
    found = _direction(row, currency)
    return found is not None and found[0] == direction


def _cancellations(rows: Sequence[Row]) -> dict[str, str]:
    """Each cancelling row's key to the key of the row it cancels (module doc): a "Withdraw
    Request Cancelled" its request's, a refund its fee's."""
    ordered = sorted(
        (r for r in rows if r.sheet == ACCOUNT_ACTIVITY), key=lambda r: (_when(r), r.id)
    )
    refunds = _refunds(ordered)
    # By Type: the (cash, key) of the rows nothing cancels yet, the earliest first.
    waiting: defaultdict[str, list[tuple[Decimal, str]]] = defaultdict(list)
    found: dict[str, str] = {}
    for row in ordered:
        amount = cash(row)
        if amount is None:
            continue
        if (row.type == WITHDRAW_REQUEST_CANCELLED and amount > 0) or row.key in refunds:
            cancelled = waiting[WITHDRAW_REQUEST if row.type in _BANK else WITHDRAW_FEE]
            for n in range(len(cancelled) - 1, -1, -1):
                if cancelled[n][0] == -amount:
                    found[row.key] = cancelled.pop(n)[1]
                    break
        elif row.type in (WITHDRAW_REQUEST, WITHDRAW_FEE) and amount < 0:
            waiting[row.type].append((amount, row.key))
    return found


def _refunds(ordered: Sequence[Row]) -> set[str]:
    """The keys of the positive "Withdraw Fee" rows a "Withdraw Fee Cancelled" row vouches
    for (module doc), of rows in order."""
    positive = [
        (r, a) for r in ordered if r.type == WITHDRAW_FEE and (a := cash(r)) is not None and a > 0
    ]
    vouched: set[str] = set()
    for row in ordered:
        if row.type != WITHDRAW_FEE_CANCELLED or (amount := number(row, "Amount")) is None:
            continue
        at = _when(row)
        near = [
            (abs(_when(r) - at), _when(r), r.id, r.key)
            for r, refunded in positive
            if r.key not in vouched
            and refunded == amount.copy_abs()
            and abs(_when(r) - at) <= _REFUND_MARGIN
        ]
        if near:
            vouched.add(min(near)[3])
    return vouched


def _dividend(statement: Statement, row: Row) -> Row | None:
    """The Dividends row of an activity's dividend: the same position and day, and the same
    net amount when the day has several."""
    position_id = _position_id(row)
    day = _when(row).date()
    found = [
        r
        for r in statement.stored
        if r.sheet == DIVIDENDS
        and _position_id(r) == position_id
        and read_day(r.cell("Date of Payment"), "Date of Payment", DIVIDENDS) == day
    ]
    if len(found) > 1:
        found = [r for r in found if number(r, "Net Dividend Received (USD)") == cash(row)]
    return found[0] if len(found) == 1 else None


def _flag_type(run: Run, row: Row) -> None:
    named = SHOWN_NAME.fullmatch(row.type) is not None
    run.flag(f"{ACCOUNT_ACTIVITY}.Type={row.type}" if named else f"{ACCOUNT_ACTIVITY}.Type")


async def _movement(
    run: Run,
    row: Row,
    key: str,
    currency: str,
    description: str,
    counterparty: CounterpartyFields,
) -> UUID | None:
    """A movement between the Trading account and another (transfer_contra says which): its
    cash on the account, the contra the other way."""
    amount = _cash_of(run, row)
    if amount is None or amount == 0:
        return None
    usd = await run.currency(currency)
    contra = await run.contra(key, run.trading, counterparty.identifier_id)
    return await run.post(
        key,
        _when(row),
        description,
        [PostingInput(run.trading, usd.id, amount), PostingInput(contra, usd.id, -amount)],
        counterparty,
    )


async def _transfer(run: Run, row: Row, key: str, currency: str) -> UUID | None:
    direction = _direction(row, currency)
    if direction is None:
        _flag_type(run, row)
        return None
    money = accounts.money_account(run, direction[1])
    identifier = await accounts.account_identifier(run, money) if money is not None else None
    if money is None:
        run.flag(NO_MONEY_ACCOUNT)
    description = "Transfer from eToro Money" if direction[0] == "in" else "Transfer to eToro Money"
    return await _movement(run, row, key, currency, description, CounterpartyFields(identifier))


async def _bank(run: Run, row: Row, key: str, currency: str) -> UUID | None:
    counterparty = CounterpartyFields(bank_transfer=True)
    return await _movement(run, row, key, currency, _BANK[row.type], counterparty)


async def _unassigned(
    run: Run, statement: Statement, row: Row, key: str, currency: str, kind: _Kind
) -> UUID | None:
    amount = _cash_of(run, row)
    if amount is None:
        return None
    usd = await run.currency(currency)
    unassigned = await run.system_account("unassigned")
    tax = Decimal(0)
    if row.type.casefold() == DIVIDEND and (paid := _dividend(statement, row)) is not None:
        tax = (number(paid, "Withholding Tax Amount (USD)") or Decimal(0)).copy_abs()
    if amount == 0 and tax == 0:
        return None
    gross = amount + tax
    postings = [
        PostingInput(run.trading, usd.id, gross, is_fee=kind.fee),
        PostingInput(unassigned, usd.id, -gross),
    ]
    if tax != 0:
        postings += [
            PostingInput(run.trading, usd.id, -tax, is_fee=True, memo="Withholding tax"),
            PostingInput(unassigned, usd.id, tax),
        ]
    return await run.post(key, _when(row), kind.description, postings)


async def _kept_void(run: Run, key: str) -> bool:
    """Whether the connection's transaction under the key is one the owner voided: posting
    leaves it void (upsert_connector_transaction)."""
    t = models.Transaction
    found = (
        await run.session.execute(
            select(t.voided_by).where(
                t.connection_id == run.connection_id, t.external_id == key, t.status == "void"
            )
        )
    ).one_or_none()
    return found is not None and found.voided_by != "connector"


async def _alone(run: Run, statement: Statement, row: Row, currency: str) -> UUID | None:
    """A row of a cancelled pair posted as one in no pair (cash_events)."""
    key = f"{ACTIVITY}{row.key}"
    if row.type in _BANK:
        return await _bank(run, row, key, currency)
    return await _unassigned(run, statement, row, key, currency, _UNASSIGNED[row.type.casefold()])


async def _in_transit(run: Run, row: Row, key: str, currency: str, cancels: UUID | None) -> UUID:
    """One row of a cancelled pair: posted as in no pair (_bank, _unassigned), but against In
    transit, naming the transaction it cancels."""
    amount = cash(row)
    assert amount is not None  # a row without a number is in no pair (_cancellations)
    usd = await run.currency(currency)
    in_transit = await run.system_account("in_transit")
    kind = None if row.type in _BANK else _UNASSIGNED[row.type.casefold()]
    return await run.post(
        key,
        _when(row),
        _BANK[row.type] if kind is None else kind.description,
        [
            PostingInput(run.trading, usd.id, amount, is_fee=kind is not None and kind.fee),
            PostingInput(in_transit, usd.id, -amount),
        ],
        CounterpartyFields(bank_transfer=True) if kind is None else None,
        cancels=cancels,
    )


async def _pair(
    run: Run, statement: Statement, cancelled: Row, cancelling: Row, currency: str
) -> dict[str, UUID | None]:
    """Post a cancelled pair (module doc), by row key: the cancelled row first, the
    cancelling one naming its transaction; each as in no pair when either is not live (the
    owner voided it, before or meanwhile), so a void side ends the pair."""
    keys = [f"{ACTIVITY}{cancelled.key}", f"{ACTIVITY}{cancelling.key}"]
    if not [key for key in keys if await _kept_void(run, key)]:
        first = await _in_transit(run, cancelled, keys[0], currency, None)
        second = await _in_transit(run, cancelling, keys[1], currency, first)
        if None not in [await run.live(key) for key in keys]:
            return {cancelled.key: first, cancelling.key: second}
    return {row.key: await _alone(run, statement, row, currency) for row in (cancelled, cancelling)}


async def cash_events(run: Run, statement: Statement) -> None:
    """Post the cash events of the run's Account Activity rows (module doc), and the leg fees
    of their transfers."""
    if not statement.of(ACCOUNT_ACTIVITY):
        return
    if not run.account_currency:
        raise EtoroDataError("eToro has not given the Trading account's currency")
    currency = run.account_currency
    links, unattached = _fee_links(statement.stored, currency)
    by_key = {row.key: row for row in statement.stored}
    # Each row of a cancelled pair → (the cancelled row's key, the cancelling row's).
    pairs: dict[str, tuple[str, str]] = {}
    for cancelling, cancelled in _cancellations(statement.stored).items():
        pairs[cancelling] = pairs[cancelled] = (cancelled, cancelling)
    for row in sorted(statement.of(ACCOUNT_ACTIVITY), key=lambda r: (_when(r), r.id)):
        if row.type in (OPEN_POSITION, POSITION_CLOSED):
            continue  # a trade: records.position
        key = f"{ACTIVITY}{row.key}"
        transaction_id: UUID | None = None
        if row.type in _CONVERSION_FEES:
            if row.key in unattached:
                run.flag(UNATTACHED_FEE)
            if number(row, "Amount") is None:
                run.flag(f"{row.sheet}.Amount has no number")
        elif row.type == WITHDRAW_FEE_CANCELLED:
            pass  # moves nothing: its refund is a "Withdraw Fee" row of its own
        elif row.key in pairs:
            if row.key not in statement.links:  # not yet posted with the other row
                cancelled, cancelling = (by_key[key] for key in pairs[row.key])
                statement.links |= await _pair(run, statement, cancelled, cancelling, currency)
            transaction_id = statement.links[row.key]
        elif _TRANSFER.fullmatch(row.type):
            transaction_id = await _transfer(run, row, key, currency)
        elif row.type in _BANK:
            transaction_id = await _bank(run, row, key, currency)
        elif (kind := _UNASSIGNED.get(row.type.casefold())) is not None:
            transaction_id = await _unassigned(run, statement, row, key, currency, kind)
        else:
            _flag_type(run, row)
        if transaction_id is None:
            await run.void(key)
        statement.links[row.key] = transaction_id
    await _leg_fees(run, statement, links, currency)


async def _leg_fees(
    run: Run, statement: Statement, links: Mapping[str, Row], currency: str
) -> None:
    """Each transfer's conversion fee (design §5.12), for the transfers the run's rows touch:
    their own, or their fee's."""
    fees: defaultdict[str, Decimal] = defaultdict(Decimal)
    by_key = {row.key: row for row in statement.stored}
    for fee_key, transfer in links.items():
        amount = number(by_key[fee_key], "Amount")
        if amount is not None:  # one without a number is flagged (cash_events)
            fees[transfer.key] += amount.copy_abs()
    given = {row.key: row for row in statement.of(ACCOUNT_ACTIVITY)}
    touched: set[str] = set()
    for row in given.values():
        if _TRANSFER.fullmatch(row.type):
            touched.add(row.key)
        elif row.key in links:
            touched.add(links[row.key].key)
    usd = await run.currency(currency)
    for transfer_key in sorted(touched):
        transaction_id = await run.live(f"{ACTIVITY}{transfer_key}")
        if transaction_id is None:
            continue
        amount = fees.get(transfer_key)
        if amount is None:
            deleted = await run.session.scalar(
                text(
                    "delete from finance.transfer_leg_fees where transaction_id = :t"
                    " returning transaction_id"
                ),
                {"t": transaction_id},
            )
            if deleted is not None:
                run.command.record_change(f"transfer_leg_fee:{transaction_id}", None)
            continue
        written = await upsert_typed(
            run.command,
            "transfer_leg_fees",
            {"transaction_id": transaction_id},
            {"instrument_id": usd.id, "amount": amount},
        )
        if written:
            run.command.record_change(f"transfer_leg_fee:{transaction_id}", {"amount": str(amount)})
    for fee_key, transfer in links.items():
        if fee_key in given:
            statement.links[fee_key] = await run.live(f"{ACTIVITY}{transfer.key}")


# Typed rows ------------------------------------------------------------------------------


def _converted(value: str, kind: str, sheet: str, label: str) -> Value:
    """A cell's text in the form typed_values reads for its column's type; `sheet` and
    `label` name it in an error."""
    if kind == "text":
        return value
    plain = value.strip()
    if plain in _NO_NUMBER:
        return None
    try:
        if kind == "timestamptz":
            return read_moment(plain, label, STATEMENT_ZONE, sheet).isoformat()
        if kind == "date":
            return read_day(plain, label, sheet).isoformat()
    except StatementError as error:
        raise EtoroDataError(str(error)) from None
    negative = plain.startswith("(") and plain.endswith(")")
    try:
        found = Decimal(plain[1:-1] if negative else plain)
    except InvalidOperation:
        raise EtoroDataError(f"{sheet}.{label} is not a number") from None
    if not found.is_finite():
        raise EtoroDataError(f"{sheet}.{label} is not a number")
    return str(-found if negative else found)


def _cell_value(row: Row, column: str, kind: str) -> Value:
    """A row's cell in the form typed_values reads for its column's type."""
    return _converted(row.cells.get(column, ""), kind, row.sheet, column)


async def _typed(run: Run, row: Row, sheet: str, table: str) -> dict[str, object]:
    columns = STATEMENT_COLUMNS[table]
    by_column: dict[str, Value] = {}
    labels: dict[str, str] = {}
    for header, kind in STATEMENT_KINDS[sheet].items():
        by_column[columns[header]] = _cell_value(row, header, kind)
        labels[columns[header]] = f"{sheet}.{header}"
    return dict(await typed_values(run.session, table, by_column, labels))


async def typed(run: Run, statement: Statement) -> None:
    """Type the run's rows of Account Activity, Dividends and Financial Summary, and the
    Account Summary of each statement they belong to; link each to what it posts. Closed
    Positions' go onto trades (closed_row)."""
    for row in statement.of(ACCOUNT_ACTIVITY):
        values = await _typed(run, row, ACCOUNT_ACTIVITY, "etoro_activity")
        if row.type in (OPEN_POSITION, POSITION_CLOSED):
            position_id = _position_id(row)
            prefix = "open:" if row.type == OPEN_POSITION else "close:"
            link = await run.live(f"{prefix}{position_id}") if position_id else None
        else:
            link = statement.links.get(row.key)
        await run.upsert(
            "etoro_activity",
            {"connection_id": run.connection_id, "row_key": row.key},
            values | {"transaction_id": link, "raw_record_id": row.id},
        )
    paid: dict[str, Row] = {}
    if statement.of(DIVIDENDS):
        for activity in statement.stored:
            if activity.sheet == ACCOUNT_ACTIVITY and activity.type.casefold() == DIVIDEND:
                if (found := _dividend(statement, activity)) is not None:
                    paid[found.key] = activity
    for row in statement.of(DIVIDENDS):
        values = await _typed(run, row, DIVIDENDS, "etoro_dividends")
        position_id = _position_id(row)
        activity = paid.get(row.key)
        link = await run.live(f"{ACTIVITY}{activity.key}") if activity is not None else None
        held = await posted_instrument(run, position_id) if position_id else None
        await run.upsert(
            "etoro_dividends",
            {"connection_id": run.connection_id, "row_key": row.key},
            values
            | {"transaction_id": link, "finance_instrument_id": held, "raw_record_id": row.id},
        )
    for row in statement.of(FINANCIAL_SUMMARY):
        await _financial(run, row)
    await _summaries(run, statement)
    # Opens and closes posted later than their rows were typed (the API's, say).
    relinked = await run.session.execute(
        text(
            "update finance.etoro_activity a set transaction_id = t.id"
            " from finance.transactions t where a.connection_id = :c and t.connection_id = :c"
            " and t.status <> 'void' and a.position_id is not null"
            " and a.type in ('Open Position', 'Position closed')"
            " and t.external_id = (case a.type when 'Open Position' then 'open:'"
            " else 'close:' end) || a.position_id::text"
            " and a.transaction_id is distinct from t.id"
            " returning a.row_key, a.transaction_id"
        ),
        {"c": run.connection_id},
    )
    for row_key, transaction_id in relinked.all():
        run.command.record_change(
            f"etoro_activity:{run.connection_id}:{row_key}",
            {"transaction_id": str(transaction_id)},
        )


async def _financial(run: Run, row: Row) -> None:
    parent = {"connection_id": run.connection_id, "row_key": row.key}
    await run.upsert(
        "etoro_financial_summary",
        parent,
        {
            "period_from": row.period_from,
            "period_to": row.period_to,
            "name": row.cells.get("Name", ""),
            "raw_record_id": row.id,
        },
    )
    # A stored row keeps its cells, not their order: the columns are taken by their header.
    figures = sorted((header, value) for header, value in row.cells.items() if header != "Name")
    for n, (header, _) in enumerate(figures):
        await run.upsert(
            "etoro_financial_summary_values",
            parent | {"item_index": n},
            {"period_header": header, "figure": number(row, header), "raw_record_id": row.id},
        )
    await run.prune("etoro_financial_summary_values", parent, len(figures))


async def _summaries(run: Run, statement: Statement) -> None:
    """Each statement's Account Summary, one row per document (etoro_statements): for each
    of its labels, the document's own row, else the latest of its period."""
    periods = {(r.period_from, r.period_to) for r in statement.of(ACCOUNT_SUMMARY)}
    columns = STATEMENT_COLUMNS["etoro_statements"]
    for period in sorted(periods):
        versions = [
            r
            for r in statement.versions
            if r.sheet == ACCOUNT_SUMMARY and (r.period_from, r.period_to) == period
        ]
        for sha in sorted({r.document_sha256 for r in versions}):
            document_id = await run.session.scalar(
                text("select id from core.documents where sha256 = :s"), {"s": sha}
            )
            if not isinstance(document_id, UUID):
                continue
            chosen: dict[str, Row] = {}
            for row in versions:  # oldest first: the latest of the period, then the document's
                if row.key not in chosen or chosen[row.key].document_sha256 != sha:
                    chosen[row.key] = row
            by_label: defaultdict[str, list[Row]] = defaultdict(list)
            for row in sorted(chosen.values(), key=lambda r: r.id):
                by_label[row.cells.get(LABEL, "").strip()].append(row)
            by_column: dict[str, Value] = {}
            labels: dict[str, str] = {}
            for label, rows in by_label.items():
                kind = SUMMARY_KINDS.get(label)
                if kind is None:
                    continue  # a heading, a note, or a label flagged when the file was read
                if len(rows) > REPEATED_LABELS.get(label, 1):
                    run.flag(f"{ACCOUNT_SUMMARY}.{label} repeated")
                for n, row in enumerate(rows[: REPEATED_LABELS.get(label, 1)]):
                    name = label if n == 0 else f"{label}{OCCURRENCE}{n}"
                    by_column[columns[name]] = _converted(
                        row.cells.get(VALUE, ""), kind if n == 0 else "text", row.sheet, label
                    )
                    by_column[columns[f"{name}{SECOND_VALUE}"]] = row.cells.get(f"{VALUE} 2")
                    labels[columns[name]] = f"{ACCOUNT_SUMMARY}.{label}"
            values = dict(await typed_values(run.session, "etoro_statements", by_column, labels))
            # "Currency" is kept as given, and its two codes typed beside it (design §5.3).
            label = by_label.get("Currency")
            account, second = (None, None)
            if label:
                try:
                    account, second = read_currencies(label[0].cells.get(VALUE, ""))
                except StatementError as error:
                    raise EtoroDataError(str(error)) from None
            values |= {"account_currency": account, "second_currency": second}
            await run.upsert(
                "etoro_statements",
                {"connection_id": run.connection_id, "document_id": document_id},
                values,
            )
