"""Generated eToro histories for the property tests (test_properties.py).

A history is the true life of one eToro Trading account (USD) and its eToro Money account
(GBP), from the account's Date Created on: moves between the two both ways, with or without
a conversion fee (the fee row's Realized Equity Change is 0, design §5.7); opens of one
US-listed stock, closes, partial closes (the remainder a position of its own, with
`parentPositionId`, design §6); dividends with withholding tax, overnight fees; and
withdraw requests, some cancelled later. Each event happens at a whole second in UTC, no two
in the same second but a move and its fee row (Task 0: eToro stamps them, and eToro Money's
leg, with one clock). The days cross the end of British summer time (25 October 2026), and
some events fall in the hour before midnight UTC, when London is already on the next day in
summer: the API's times and the statement's are both UTC (design §5.8), so both sources give
them the same booking date.

The rulings already in the code decide which source states what (R30): the API states the
trades (PnL while open, trade history once closed, the order lookups) and eToro Money's
rows; only the statement states the Trading account's cash events (moves, fees, taxes,
dividends, withdrawals); the API never posts a fee or a tax (its `fees` are 0 here, so the
"upload a statement" prompt is never the API's doing).

A schedule is how the history reaches Lararium:
- syncs at drawn moments, the first after Date Created, the last after every event: each
  stores what the API reports then (me, balances, PnL, aggregate, breakdown, instrument
  metadata, the closed positions' trade history, the order lookups, eToro Money's settled
  rows, and the balance history of every day ended by then, from the day before Date
  Created);
- statements: a chain of pieces from Date Created's day to after the last event, each
  starting at a day's start (00:00:00) and ending at a day's end (23:59:59) or mid-day,
  each overlapping or abutting the next; sometimes a piece uploaded twice. Each is uploaded
  after a drawn sync, no earlier than its end (a statement cannot list what has not
  happened), the first upload one that starts by Date Created (sync.py refuses another).

Deliberately not generated: CFDs, leverage, copy trading (flagged, not posted); other
instruments or currencies; Withdraw Fee rows; eToro Money's bank or card rows (pinned by
test_normalize_api); Financial Summary rows (typed per statement only); a statement uploaded
before the first sync (refused); a dividend's Dividends row without its activity row."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal

from hypothesis import strategies as st

from .fixtures import (
    CASH_ACCOUNT,
    INSTRUMENT_ID,
    SYMBOL,
    TRADING_ACCOUNT,
    aggregate_body,
    balance_entry,
    breakdown_body,
    breakdown_position,
    cash_item,
    history_body,
    instrument_meta,
    lookup_body,
    me_json,
    pnl_body,
    pnl_position,
    trade_row,
)
from .statement_fixtures import (
    ISIN,
    activity_row,
    build_statement,
    closed_row,
    dividend_row,
    summary,
)

USERNAME = me_json()["username"]
DAY0 = date(2026, 10, 22)  # Date Created's day; summer time ends on the 25th
CREATED = datetime.combine(DAY0, time(9, 15), UTC)
DAYS = 8  # events happen on the days from DAY0
MONEY_OPENING = Decimal("5000.00")  # eToro Money's GBP before the history
USD_PER_GBP = Decimal("1.26")
PRICES = (Decimal("100.00"), Decimal("125.50"))  # USD per share
PROFITS = (Decimal("-12.50"), Decimal(0), Decimal("20.00"), Decimal("3.33"))
MOVES = (Decimal("250.00"), Decimal("1000.00"))  # GBP
FEES = (None, Decimal("6.30"))
FIRST_POSITION = 3000000101

type Kind = Literal[
    "in", "out", "open", "close", "partial", "dividend", "overnight", "withdraw", "cancel"
]


def statement_time(at: datetime) -> str:
    return at.strftime("%d/%m/%Y %H:%M:%S")


def api_time(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%S.000Z")


@dataclass(frozen=True)
class Position:
    position_id: int
    order_id: int
    units: Decimal
    amount: Decimal  # USD
    opened: datetime
    parent: int | None = None  # a partial close's remainder: the position it came from


@dataclass(frozen=True)
class Event:
    n: int
    kind: Kind
    at: datetime
    cash: Decimal  # Trading's USD change
    money: Decimal = Decimal(0)  # eToro Money's GBP change
    fee: Decimal | None = None  # a move's conversion fee (USD)
    position: Position | None = None  # the position opened, closed or paid on
    units: Decimal = Decimal(0)  # closed
    profit: Decimal = Decimal(0)
    tax: Decimal = Decimal(0)  # a dividend's withholding tax
    remainder: Position | None = None  # a partial close's

    @property
    def closed_amount(self) -> Decimal:
        """A close's investment: its share of the position's amount."""
        assert self.position is not None
        return self.position.amount * self.units / self.position.units


@dataclass(frozen=True)
class Piece:
    start: date  # 00:00:00 that day
    end: datetime


@dataclass(frozen=True)
class History:
    events: tuple[Event, ...]

    @property
    def last(self) -> datetime:
        return self.events[-1].at

    def upto(self, at: datetime) -> list[Event]:
        return [e for e in self.events if e.at <= at]

    def cash(self, at: datetime) -> Decimal:
        return sum((e.cash for e in self.upto(at)), Decimal(0))

    def money(self, at: datetime) -> Decimal:
        return MONEY_OPENING + sum((e.money for e in self.upto(at)), Decimal(0))

    def positions(self, at: datetime) -> list[Position]:
        """The positions open at `at`."""
        found: dict[int, Position] = {}
        for e in self.upto(at):
            assert e.position is not None or e.kind not in ("open", "close", "partial")
            if e.kind == "open" and e.position is not None:
                found[e.position.position_id] = e.position
            elif e.kind in ("close", "partial") and e.position is not None:
                del found[e.position.position_id]
                if e.remainder is not None:
                    found[e.remainder.position_id] = e.remainder
        return list(found.values())

    def closes(self, at: datetime) -> list[Event]:
        return [e for e in self.upto(at) if e.kind in ("close", "partial")]

    def opens(self) -> list[Event]:
        return [e for e in self.events if e.kind == "open"]

    # The API ----------------------------------------------------------------------------

    def api(self, at: datetime) -> list[tuple[str, Any]]:
        """What a sync at `at` stores."""
        open_now = self.positions(at)
        units = sum((p.units for p in open_now), Decimal(0))
        entries: list[tuple[str, Any]] = [
            ("me", me_json()),
            (
                "balances",
                {
                    "gcid": me_json()["gcid"],
                    "displayCurrency": "USD",
                    "totalBalance": Decimal(0),
                    "balances": [
                        balance_entry(TRADING_ACCOUNT, "Trading", "USD", str(self.cash(at))),
                        balance_entry(CASH_ACCOUNT, "Cash", "GBP", str(self.money(at))),
                    ],
                },
            ),
            ("pnl", pnl_body([self._pnl(p) for p in open_now], credit=str(self.cash(at)))),
            (
                "aggregate",
                aggregate_body([(INSTRUMENT_ID, "USD", str(units))] if open_now else []),
            ),
            ("breakdown", breakdown_body([self._breakdown(p) for p in open_now])),
            ("instruments", instrument_meta()),
        ]
        entries += [("trade_history", self._trade(e)) for e in self.closes(at)]
        entries += [
            ("order_lookup", lookup_body(e.position.order_id, e.position.position_id))
            for e in self.opens()
            if e.position is not None and e.at <= at
        ]
        entries += [("cash_transactions", self._money_row(e)) for e in self.upto(at) if e.money]
        ended = (at - timedelta(days=1)).date()
        days = [
            DAY0 - timedelta(days=1) + timedelta(days=d) for d in range((ended - DAY0).days + 2)
        ]
        entries.append(
            (
                "balance_history",
                history_body(
                    [
                        (
                            day,
                            [
                                (CASH_ACCOUNT, "Cash", "GBP", str(self.money(_end(day)))),
                                (TRADING_ACCOUNT, "Trading", "USD", str(self.cash(_end(day)))),
                            ],
                        )
                        for day in days
                    ]
                ),
            )
        )
        return entries

    @staticmethod
    def _pnl(p: Position) -> dict[str, Any]:
        extra: dict[str, Any] = {"orderID": p.order_id}
        if p.parent is not None:
            extra["parentPositionID"] = p.parent
        return pnl_position(
            p.position_id,
            units=str(p.units),
            amount=str(p.amount),
            opened=api_time(p.opened),
            **extra,
        )

    @staticmethod
    def _breakdown(p: Position) -> dict[str, Any]:
        return breakdown_position(
            p.position_id, units=str(p.units), openTime=p.opened.strftime("%Y-%m-%dT%H:%M:%S.000")
        )

    @staticmethod
    def _trade(e: Event) -> dict[str, Any]:
        assert e.position is not None
        p = e.position
        return trade_row(p.position_id) | {
            "netProfit": e.profit,
            "closeRate": ((e.closed_amount + e.profit) / e.units).quantize(Decimal("0.0001")),
            "closeTimestamp": api_time(e.at),
            "openRate": (p.amount / p.units).quantize(Decimal("0.0001")),
            "openTimestamp": api_time(p.opened),
            "orderId": p.order_id,
            "parentPositionId": p.parent or 0,
            "investment": e.closed_amount,
            "initialInvestment": p.amount,
            "fees": Decimal(0),
            "units": e.units,
        }

    @staticmethod
    def _money_row(e: Event) -> dict[str, Any]:
        sent = e.money < 0
        return cash_item(f"8000{e.n:04d}", str(e.money.copy_abs())) | {
            "transactionSubtype": "transferSent" if sent else "transferReceived",
            "direction": "debit" if sent else "credit",
            "postedAt": e.at.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
            "counterparty": {"name": "eToro Trading account", "type": "internal_account"},
            "internalTransferTransactionDetails": {"transferId": f"7000{e.n:04d}"},
        }

    # The statement ------------------------------------------------------------------------

    def statement(self, piece: Piece, corrected: int | None = None) -> bytes:
        """The statement of the piece: its events' rows, the closes' Closed Positions rows
        and the dividends' Dividends rows. `corrected`: an event whose close row says it
        brought a dollar more than the API does (a re-issued statement disagreeing)."""
        start = datetime.combine(piece.start, time(), UTC)
        events = [e for e in self.events if start <= e.at <= piece.end]
        before = self.cash(start - timedelta(seconds=1))
        activity: list[dict[str, str]] = []
        closed: list[dict[str, str]] = []
        dividends: list[dict[str, str]] = []
        balance = before
        for e in events:
            bump = Decimal(1) if e.n == corrected else Decimal(0)
            balance += e.cash + bump
            activity += self._activity(e, balance, bump)
            if e.kind in ("close", "partial"):
                closed.append(self._closed(e))
            elif e.kind == "dividend":
                assert e.position is not None
                dividends.append(
                    dividend_row(str(e.position.position_id))
                    | {
                        "Date of Payment": e.at.strftime("%d/%m/%Y"),
                        "Net Dividend Received (USD)": str(e.cash),
                        "Withholding Tax Amount (USD)": str(e.tax),
                    }
                )
        return build_statement(
            summary(
                start=statement_time(start),
                end=statement_time(piece.end),
                created=statement_time(CREATED),
                username=USERNAME,
            ),
            activity,
            closed,
            dividends,
        )

    @staticmethod
    def _activity(e: Event, balance: Decimal, bump: Decimal = Decimal(0)) -> list[dict[str, str]]:
        at, b = statement_time(e.at), str(balance)
        if e.kind in ("in", "out"):
            pair = "GBP > USD" if e.kind == "in" else "USD > GBP"
            rows = [activity_row(at, f"Transfer: {pair}", str(e.cash), details="GBP", balance=b)]
            if e.fee is not None:
                kind = "Deposit" if e.kind == "in" else "Withdrawal"
                rows.append(
                    activity_row(at, f"{kind} Conversion Fee", str(-e.fee), change="0", balance=b)
                )
            return rows
        if e.kind in ("withdraw", "cancel"):
            kind = "Withdraw Request" if e.kind == "withdraw" else "Withdraw Request Cancelled"
            return [activity_row(at, kind, str(e.cash), balance=b)]
        assert e.position is not None
        position = str(e.position.position_id)
        trade = {"details": f"{SYMBOL}/USD", "position_id": position, "asset_type": "Stocks"}
        if e.kind == "open":
            return [
                activity_row(
                    at,
                    "Open Position",
                    str(e.position.amount),
                    units=str(e.position.units),
                    change="0",
                    balance=b,
                    **trade,
                )
            ]
        if e.kind in ("close", "partial"):
            return [
                activity_row(
                    at,
                    "Position closed",
                    str(e.closed_amount + e.profit + bump),
                    units=str(e.units),
                    change=str(e.profit + bump),
                    balance=b,
                    **trade,
                )
            ]
        kind = "Dividend" if e.kind == "dividend" else "Overnight fee"
        return [activity_row(at, kind, str(e.cash), balance=b, **trade)]

    @staticmethod
    def _closed(e: Event) -> dict[str, str]:
        assert e.position is not None
        return closed_row(str(e.position.position_id), ISIN) | {
            "Amount": str(e.closed_amount),
            "Units / Contracts": str(e.units),
            "Open Date": statement_time(e.position.opened),
            "Close Date": statement_time(e.at),
            "Profit(USD)": str(e.profit),
            "Open Rate": str((e.position.amount / e.position.units).quantize(Decimal("0.01"))),
            "Close Rate": str(((e.closed_amount + e.profit) / e.units).quantize(Decimal("0.01"))),
        }

    # Narrowings pending the controller's rulings (task-18 report, F1 and F2) --------------

    def past_partial_closes(self, end: datetime) -> datetime:
        """F1: a statement ending between a position's open and its partial close lists the
        whole position's "Open Position" row and no Closed Positions row, which the merge
        compares with the API's trade history of the part closed (a review on units and
        cash). Until that is ruled on, such an end moves to just after the partial close."""
        for e in self.events:
            if e.kind == "partial" and e.position is not None and e.position.opened <= end < e.at:
                end = e.at + timedelta(seconds=1)
        return end

    def api_first(self, piece: Piece) -> datetime | None:
        """The moment a sync must follow before the piece is uploaded. F1, the other way: a
        statement holding a partial close's Closed Positions row while the API's latest PnL
        still lists the whole position is compared with it. F2: a statement holding the
        open of a position it does not close, before any sync has seen the position, flags
        "a statement position Lararium cannot place", a flag that stays until a replay."""
        start = datetime.combine(piece.start, time(), UTC)
        inside = [e for e in self.events if start <= e.at <= piece.end]
        closed = {
            e.position.position_id for e in inside if e.position and e.kind in ("close", "partial")
        }
        moments: list[datetime] = []
        for e in inside:
            if e.kind == "partial":
                moments.append(e.at)
            elif e.kind == "open" and e.position and e.position.position_id not in closed:
                moments.append(e.at)
        return max(moments, default=None)

    def describe(self) -> str:
        lines = []
        for e in self.events:
            p = e.position
            lines.append(
                f"{e.n}: {e.kind} {e.at:%a %d %H:%M:%S} cash {e.cash} money {e.money}"
                + (f" fee {e.fee}" if e.fee is not None else "")
                + (f" position {p.position_id} {p.units}u {p.amount}" if p else "")
                + (f" closed {e.units}u profit {e.profit}" if e.units else "")
                + (f" remainder {e.remainder.position_id}" if e.remainder else "")
            )
        return "\n".join(lines)


def _end(day: date) -> datetime:
    return datetime.combine(day, time(23, 59, 59), UTC)


@st.composite
def histories(draw: st.DrawFn) -> History:
    events: list[Event] = []
    cash = Decimal(0)
    open_positions: list[Position] = []
    next_position = FIRST_POSITION
    withdrawn: list[Decimal] = []
    moments = sorted(
        set(
            draw(
                st.lists(
                    st.tuples(
                        st.integers(0, DAYS - 1),
                        # Mostly working hours; some in the hour before midnight UTC.
                        st.one_of(
                            st.integers(10 * 3600, 18 * 3600),
                            st.integers(23 * 3600, 24 * 3600 - 2),
                        ),
                    ),
                    min_size=2,
                    max_size=9,
                    unique_by=lambda m: m,
                )
            )
        )
    )
    for n, (day, second) in enumerate(moments):
        at = datetime.combine(DAY0 + timedelta(days=day), time(), UTC) + timedelta(seconds=second)
        if at <= CREATED:
            at = CREATED + timedelta(minutes=5 + n)
        if events and at <= events[-1].at:
            at = events[-1].at + timedelta(minutes=7)
        options: list[Kind] = ["in"]
        if n > 0:
            if cash >= min(MOVES) * USD_PER_GBP + 10:
                options += ["out", "withdraw"]
            if cash >= max(PRICES):
                options.append("open")
            if open_positions:
                options += ["close", "dividend"] + (["overnight"] if cash >= 1 else [])
                if any(p.units > 1 for p in open_positions):
                    options.append("partial")
            if withdrawn:
                options.append("cancel")
        kind = draw(st.sampled_from(options))
        if kind in ("in", "out"):
            gbp = draw(st.sampled_from(MOVES))
            usd = (gbp * USD_PER_GBP).quantize(Decimal("0.01"))
            if kind == "out" and usd > cash - 10:
                gbp, usd = min(MOVES), (min(MOVES) * USD_PER_GBP).quantize(Decimal("0.01"))
            fee = draw(st.sampled_from(FEES))
            if kind == "in":
                event = Event(n, kind, at, usd, -gbp, fee)
            else:
                # Trading sends U; eToro Money is credited (U - F) at the rate.
                net = usd - (fee or Decimal(0))
                event = Event(n, kind, at, -usd, (net / USD_PER_GBP).quantize(Decimal("0.01")), fee)
        elif kind == "withdraw":
            amount = draw(st.sampled_from((Decimal("50.00"), Decimal("100.00"))))
            withdrawn.append(amount)
            event = Event(n, kind, at, -amount)
        elif kind == "cancel":
            event = Event(n, kind, at, withdrawn.pop(0))
        elif kind == "open":
            price = draw(st.sampled_from(PRICES))
            units = Decimal(draw(st.integers(1, max(1, min(6, int(cash // price))))))
            position = Position(next_position, next_position + 500, units, units * price, at)
            next_position += 1
            open_positions.append(position)
            event = Event(n, kind, at, -position.amount, position=position)
        elif kind in ("dividend", "overnight"):
            position = draw(st.sampled_from(open_positions))
            if kind == "dividend":
                event = Event(n, kind, at, Decimal("0.85"), position=position, tax=Decimal("0.15"))
            else:
                event = Event(n, kind, at, Decimal("-0.30"), position=position)
        else:
            choices = (
                open_positions if kind == "close" else [p for p in open_positions if p.units > 1]
            )
            position = draw(st.sampled_from(choices))
            units = (
                position.units
                if kind == "close"
                else Decimal(draw(st.integers(1, int(position.units) - 1)))
            )
            profit = draw(st.sampled_from(PROFITS))
            open_positions.remove(position)
            remainder = None
            if kind == "partial":
                remainder = Position(
                    next_position,
                    position.order_id,
                    position.units - units,
                    position.amount * (position.units - units) / position.units,
                    position.opened,
                    position.position_id,
                )
                next_position += 1
                open_positions.append(remainder)
            closed = position.amount * units / position.units
            event = Event(
                n,
                kind,
                at,
                closed + profit,
                position=position,
                units=units,
                profit=profit,
                remainder=remainder,
            )
        cash += event.cash
        events.append(event)
    return History(tuple(events))


@dataclass(frozen=True)
class Schedule:
    syncs: tuple[datetime, ...]
    # (piece, uploaded after this many syncs), in upload order.
    uploads: tuple[tuple[Piece, int], ...]

    def steps(self) -> list[tuple[Literal["sync", "upload"], Any]]:
        found: list[tuple[Literal["sync", "upload"], Any]] = []
        for n, at in enumerate(self.syncs, start=1):
            found.append(("sync", at))
            found += [("upload", piece) for piece, after in self.uploads if after == n]
        return found

    def describe(self) -> str:
        lines = []
        for kind, what in self.steps():
            if kind == "sync":
                lines.append(f"sync at {what:%a %d %H:%M:%S}")
            else:
                lines.append(f"upload {what.start:%a %d} to {what.end:%a %d %H:%M:%S}")
        return "\n".join(lines)


def _piece_end(draw: st.DrawFn, day: date) -> datetime:
    """A day's end, or a moment during it (a statement downloaded that day)."""
    if draw(st.booleans()):
        return _end(day)
    return datetime.combine(day, time(), UTC) + timedelta(seconds=draw(st.integers(1, 86398)) + 0)


@st.composite
def schedules(draw: st.DrawFn, history: History) -> Schedule:
    last = history.last
    # Syncs: the first after Date Created, the last after every event.
    span = int((last - CREATED).total_seconds())
    middle = sorted(set(draw(st.lists(st.integers(60, max(60, span)), min_size=0, max_size=2))))
    syncs = [CREATED + timedelta(seconds=s, microseconds=500000) for s in middle]
    syncs.append(last + timedelta(hours=draw(st.integers(1, 30)), microseconds=500000))
    # Statement pieces: a chain from DAY0 to after the last event.
    pieces: list[Piece] = []
    start = DAY0
    while True:
        end_day = draw(
            st.sampled_from(
                [start + timedelta(days=d) for d in range((last.date() - start).days + 2)]
            )
        )
        end = _piece_end(draw, end_day)
        if end < datetime.combine(start, time(), UTC):
            end = _end(start)
        end = history.past_partial_closes(end)
        pieces.append(Piece(start, end))
        if end >= last:
            break
        # The next starts by the day this one ends: they overlap or abut.
        start = draw(
            st.sampled_from(
                [start + timedelta(days=d) for d in range((end.date() - start).days + 1)]
            )
        )
    if draw(st.booleans()):
        pieces.append(draw(st.sampled_from(pieces)))  # one uploaded twice

    # Each uploaded after a sync no earlier than its end (the next sync comes after the
    # upload); the first upload is one starting on DAY0, as sync.py refuses another.
    def earliest(piece: Piece) -> int:
        found = max(1, sum(1 for s in syncs if s <= piece.end))
        if (seen := history.api_first(piece)) is not None:
            found = max(found, 1 + sum(1 for s in syncs if s <= seen))
        return found

    first = draw(st.sampled_from([i for i, p in enumerate(pieces) if p.start == DAY0]))
    after = {first: draw(st.integers(earliest(pieces[first]), len(syncs)))}
    for i, piece in enumerate(pieces):
        if i != first:
            after[i] = draw(st.integers(max(earliest(piece), after[first]), len(syncs)))
    order = draw(st.permutations([i for i in range(len(pieces)) if i != first]))
    ranked = [first, *order]
    uploads = [
        (pieces[i], after[i]) for i in sorted(ranked, key=lambda i: (after[i], ranked.index(i)))
    ]
    return Schedule(tuple(syncs), tuple(uploads))
