"""Generated Trading 212 account histories for the property tests (test_properties.py).

A history is one Invest account's true events in time order, in GBP, on one US-listed stock:
deposits and withdrawals, buys and sells (with or without a currency conversion fee),
cancelled orders, dividends, interest and fees. Its cash and shares never go below zero, as
Trading 212's do not. Each is shaped as the API's history rows of the fixtures (fixtures.py),
with the sign rules records.py documents: a buy's `netValue` is what it cost, its taxes
included; a sell's what it brought, its taxes taken out; a fill's price is in the stock's
currency at a fixed `fxRate`. Times are whole seconds in UTC, as the API's and the export's
agree (Task 0, check 7).

A schedule is how the history reaches Lararium:
- syncs, each at a cut of the history (the last one at its end): its summary and positions
  report the account at the cut, and each history endpoint serves the rows up to the cut,
  newest first, in drawn page splits where a page may repeat the last rows of the one before
  (offset pagination as rows arrive), never a whole page (the walk would stop there, and
  only the weekly re-walk goes further, design §5.2);
- optionally, CSV exports (design §5.3a), each uploaded after a drawn sync, of the deposits
  and withdrawals of a drawn window that ends before the next sync's cut (the owner
  downloads it before that sync runs, and an export cannot list what has not happened),
  from the start of a drawn day (a file never starts mid-day) to a drawn event.
  The audited columns only, and only those two actions: the rows of trades are unknown
  until the owner's first one, and stored flagged (Task 13a).

Deliberately not generated: TRANSFERs (flagged until the owner's first one), pies, corporate
actions, a page that repeats the one before in full, and an export holding a row the API
never returns (a skipped page: the full re-walk is weekly, so the API side would differ)."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from hypothesis import strategies as st

from .fixtures import ACCOUNT_ID, TICKER, instrument

START = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
FX = Decimal("0.8")  # GBP per USD, every fill's
CURRENT_PRICE = Decimal("110.00")  # USD: each sync's positions price the stock at it
PRICES = (Decimal("80.00"), Decimal("100.00"), Decimal("125.50"))  # GBP per share
FEES = (Decimal(0), Decimal("0.50"), Decimal("1.20"))
CASH = (Decimal("50.00"), Decimal("250.00"), Decimal("1000.00"))

type Kind = Literal[
    "deposit", "withdrawal", "buy", "sell", "cancelled", "dividend", "interest", "fee"
]


def stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Event:
    n: int
    kind: Kind
    at: datetime
    cash: Decimal  # the account's GBP change, signed
    shares: Decimal = Decimal(0)  # signed
    price: Decimal = Decimal(0)  # GBP per share
    fee: Decimal = Decimal(0)
    ordered: Decimal = Decimal(0)  # a cancelled order's quantity, which never filled

    @property
    def endpoint(self) -> str:
        if self.kind in ("buy", "sell", "cancelled"):
            return "orders"
        return "dividends" if self.kind == "dividend" else "transactions"

    @property
    def reference(self) -> str:
        return f"00000000-0000-4000-8000-{self.n:012d}"

    def item(self) -> dict[str, Any]:
        """The API's history row."""
        if self.endpoint == "orders":
            return self._order()
        if self.kind == "dividend":
            return {
                "ticker": TICKER,
                "instrument": instrument(),
                "type": "ORDINARY",
                "amount": self.cash,
                "currency": "GBP",
                "amountInEuro": (self.cash * Decimal("1.15")).quantize(Decimal("0.01")),
                "grossAmountPerShare": Decimal("0.51"),
                "tickerCurrency": "USD",
                "quantity": Decimal(3),
                "paidOn": stamp(self.at),
                "reference": self.reference,
            }
        kind = {
            "deposit": "DEPOSIT",
            "withdrawal": "WITHDRAW",
            "interest": "INTEREST_ON_FREE_CASH",
            "fee": "FEE",
        }[self.kind]
        return {
            "type": kind,
            "amount": self.cash.copy_abs() if kind != "WITHDRAW" else self.cash,
            "currency": "GBP",
            "dateTime": stamp(self.at),
            "reference": self.reference,
        }

    def _order(self) -> dict[str, Any]:
        side = "SELL" if self.kind == "sell" else "BUY"
        quantity = self.shares.copy_abs() or self.ordered
        order = {
            "id": 500000 + self.n,
            "createdAt": stamp(self.at - timedelta(seconds=2)),
            "ticker": TICKER,
            "instrument": instrument(),
            "type": "MARKET",
            "side": side,
            "status": "CANCELLED" if self.kind == "cancelled" else "FILLED",
            "strategy": "QUANTITY",
            "quantity": quantity,
            "filledQuantity": Decimal(0) if self.kind == "cancelled" else quantity,
            "currency": "GBP",
            "extendedHours": False,
            "initiatedFrom": "WEB",
        }
        if self.kind == "cancelled":
            return {"order": order, "fill": None}
        taxes = []
        if self.fee:
            taxes.append(
                {
                    "name": "CURRENCY_CONVERSION_FEE",
                    "quantity": self.fee,
                    "currency": "GBP",
                    "chargedAt": stamp(self.at),
                }
            )
        return {
            "order": order,
            "fill": {
                "id": 700000 + self.n,
                "filledAt": stamp(self.at),
                "price": self.price / FX,
                "quantity": quantity,
                "tradingMethod": "TOTV",
                "type": "TRADE",
                "walletImpact": {
                    "currency": "GBP",
                    "fxRate": FX,
                    # What it cost (a buy) or brought (a sell), its taxes in it.
                    "netValue": self.cash.copy_abs(),
                    "realisedProfitLoss": Decimal(0),
                    "taxes": taxes,
                },
            },
        }

    def key(self) -> str | None:
        """What the cursor remembers of the row (sync._key)."""
        if self.kind == "cancelled":
            return None
        if self.endpoint == "orders":
            return str(700000 + self.n)
        return self.reference


@dataclass(frozen=True)
class History:
    events: tuple[Event, ...]

    def upto(self, cut: int) -> list[Event]:
        return [e for e in self.events if e.n < cut]

    def cash(self, cut: int) -> Decimal:
        return sum((e.cash for e in self.upto(cut)), Decimal(0))

    def shares(self, cut: int) -> Decimal:
        return sum((e.shares for e in self.upto(cut)), Decimal(0))

    def cost(self, cut: int) -> Decimal:
        return sum((-e.cash for e in self.upto(cut) if e.kind == "buy"), Decimal(0))

    def summary(self, cut: int) -> dict[str, Any]:
        """account/summary at the cut: its cash all available to trade."""
        shares = self.shares(cut)
        value = (shares * CURRENT_PRICE * FX).quantize(Decimal("0.01"))
        return {
            "id": ACCOUNT_ID,
            "currency": "GBP",
            "totalValue": self.cash(cut) + value,
            "cash": {
                "availableToTrade": self.cash(cut),
                "reservedForOrders": Decimal(0),
                "inPies": Decimal(0),
            },
            "investments": {
                "currentValue": value,
                "totalCost": self.cost(cut),
                "realizedProfitLoss": Decimal(0),
                "unrealizedProfitLoss": Decimal(0),
            },
        }

    def positions(self, cut: int) -> list[dict[str, Any]]:
        shares = self.shares(cut)
        if shares == 0:
            return []
        value = (shares * CURRENT_PRICE * FX).quantize(Decimal("0.01"))
        return [
            {
                "instrument": instrument(),
                "quantity": shares,
                "quantityAvailableForTrading": shares,
                "quantityInPies": Decimal(0),
                "averagePricePaid": Decimal("100.00"),
                "currentPrice": CURRENT_PRICE,
                "createdAt": stamp(START),
                "walletImpact": {
                    "currency": "GBP",
                    "totalCost": self.cost(cut),
                    "currentValue": value,
                    "unrealizedProfitLoss": Decimal(0),
                    "fxImpact": None,
                },
            }
        ]

    def sync_time(self, cut: int) -> datetime:
        """When a sync at the cut runs: a minute after the cut's last event, so before the
        next one (events are at least three minutes apart)."""
        return self.events[cut - 1].at + timedelta(minutes=1)

    def rows(self, endpoint: str, cut: int) -> list[Event]:
        """The endpoint's rows up to the cut, newest first."""
        return [e for e in reversed(self.upto(cut)) if e.endpoint == endpoint]

    def describe(self) -> str:
        return "\n".join(
            f"{e.n}: {e.kind} {e.at:%d %H:%M:%S} cash {e.cash} shares {e.shares}"
            f" price {e.price} fee {e.fee}"
            for e in self.events
        )


@st.composite
def histories(draw: st.DrawFn) -> History:
    events: list[Event] = []
    cash = shares = Decimal(0)
    at = START
    count = draw(st.integers(2, 9))
    for n in range(count):
        # Events minutes to a day apart: some days hold several fills.
        at += timedelta(minutes=draw(st.sampled_from((3, 47, 6 * 60, 24 * 60 + 11))))
        options: list[Kind] = ["deposit", "interest", "cancelled"]
        if n == 0:
            options = ["deposit"]
        else:
            if cash >= min(CASH):
                options.append("withdrawal")
            if cash >= min(PRICES) + max(FEES):
                options.append("buy")
            if cash >= max(FEES):
                options.append("fee")
            if shares > 0:
                options += ["sell", "dividend"]
        kind = draw(st.sampled_from(options))
        if kind == "deposit":
            event = Event(n, kind, at, draw(st.sampled_from(CASH)))
        elif kind == "withdrawal":
            amount = draw(st.sampled_from([c for c in CASH if c <= cash]))
            event = Event(n, kind, at, -amount)
        elif kind == "interest":
            event = Event(n, kind, at, Decimal("0.37"))
        elif kind == "fee":
            event = Event(n, kind, at, -Decimal("0.50"))
        elif kind == "dividend":
            event = Event(n, kind, at, Decimal("1.23"))
        elif kind == "cancelled":
            event = Event(n, kind, at, Decimal(0), ordered=Decimal(draw(st.integers(1, 3))))
        else:
            affordable = [p for p in PRICES if kind == "sell" or p + max(FEES) <= cash]
            price = draw(st.sampled_from(affordable))
            fee = draw(st.sampled_from(FEES))
            if kind == "buy":
                most = int((cash - fee) // price)
                quantity = Decimal(draw(st.integers(1, min(most, 5))))
                event = Event(n, kind, at, -(quantity * price + fee), quantity, price, fee)
            else:
                quantity = Decimal(draw(st.integers(1, int(shares))))
                event = Event(n, kind, at, quantity * price - fee, -quantity, price, fee)
        cash += event.cash
        shares += event.shares
        events.append(event)
    return History(tuple(events))


@dataclass(frozen=True)
class Sync:
    cut: int  # the events before it have happened
    # Per history endpoint, its pages: (first row's place, how many rows) in the newest-first
    # list of rows up to the cut.
    pages: dict[str, list[tuple[int, int]]] = field(
        default_factory=dict[str, list[tuple[int, int]]]
    )


@dataclass(frozen=True)
class Export:
    after: int  # uploaded after this many syncs (1: after the first)
    first: int  # the window of events it lists: from this one...
    last: int  # ...to this one, both included


@dataclass(frozen=True)
class Schedule:
    syncs: tuple[Sync, ...]
    exports: tuple[Export, ...]

    def describe(self) -> str:
        lines = [f"sync at cut {s.cut}: {s.pages}" for s in self.syncs]
        lines += [f"export after sync {e.after}: events {e.first}-{e.last}" for e in self.exports]
        return "\n".join(lines)


def _pages(draw: st.DrawFn, count: int) -> list[tuple[int, int]]:
    """A newest-first walk of `count` rows: pages of 1 to 4 rows, each after the first
    possibly starting on the last rows of the one before (never all of them)."""
    if count == 0:
        return [(0, 0)]
    pages: list[tuple[int, int]] = []
    start = 0
    while start < count:
        size = min(draw(st.integers(1, 4)), count - start)
        pages.append((start, size))
        overlap = draw(st.integers(0, size - 1)) if start + size < count else 0
        start += size - overlap
    return pages


@st.composite
def schedules(draw: st.DrawFn, history: History, *, exports: bool | None = None) -> Schedule:
    """Two to four syncs at drawn cuts, the last at the history's end; and, unless `exports`
    is False, none to two exports (`exports` True: at least one)."""
    total = len(history.events)
    middle = sorted(set(draw(st.lists(st.integers(1, total), min_size=1, max_size=3))))
    cuts = [c for c in middle if c < total] + [total]
    syncs = tuple(
        Sync(
            cut,
            {
                endpoint: _pages(draw, len(history.rows(endpoint, cut)))
                for endpoint in ("orders", "dividends", "transactions")
            },
        )
        for cut in cuts
    )
    found: list[Export] = []
    wanted = 0 if exports is False else draw(st.integers(1 if exports else 0, 2))
    for _ in range(wanted):
        after = draw(st.integers(1, len(syncs)))
        # It lists nothing after the next sync's cut (or the end).
        reach = syncs[after].cut if after < len(syncs) else total
        # It starts at a day's start (files never start mid-day, they may end mid-day): the
        # first event on or after 00:00 of a drawn day, through one before the reach.
        days = sorted({e.at.date() for e in history.events[:reach]})
        day = draw(st.sampled_from(days))
        first = next(e.n for e in history.events if e.at.date() >= day)
        last = draw(st.integers(first, reach - 1))
        found.append(Export(after, first, last))
    return Schedule(syncs, tuple(found))
