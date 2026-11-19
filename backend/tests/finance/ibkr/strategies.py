"""Generated IBKR account histories for the normalizer's property tests (test_properties.py).

A history is the truth: one account's days of buys and sells of an ETF listed in USD and in
GBP (two instruments, one ISIN), GBP/USD conversions, deposits, withdrawals, fees and at most
one split, each with IBKR's figures (proceeds are quantity times the true price rounded to
cents, tradePrice that price rounded to 4 decimals, as IBKR rounds both). Statements are
derived from it, so they are consistent by construction: each day's reported positions,
startingCash and endingCash are the running sums of what came before. Some histories start
with holdings from before the first window (the account opened earlier), which the first
sync's opening adjustment must account for.

Windows are what fetches cover: overlapping date ranges from the first day to the last, as
syncs repeat the last days of the previous fetch, each a statement broken out by day (the
owner's query) or, sometimes, one statement for the whole range.

All ids, symbols and amounts are invented."""

import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from zoneinfo import ZoneInfo

from hypothesis import strategies as st

from .fixtures import AccountSpec, RowSpec, build_statement

NEW_YORK = ZoneInfo("America/New_York")
ACCOUNT_ID = "U0000001"
FIRST_START = date(2026, 1, 5)  # histories start from here to 120 days later (summer time)
CENT = Decimal("0.01")
TICK = Decimal("0.0001")  # IBKR's tradePrice precision here
ISIN = "IE00B3RBWM25"
USD_LISTING = {
    "assetCategory": "STK",
    "subCategory": "ETF",
    "symbol": "ETFA",
    "description": "Example ETF A",
    "conid": "1001",
    "isin": ISIN,
    "currency": "USD",
}
# The same fund's GBP listing: another conid and symbol, the same ISIN.
GBP_LISTING = USD_LISTING | {"symbol": "ETFG", "conid": "1002", "currency": "GBP"}
LISTINGS = {"ETFA": USD_LISTING, "ETFG": GBP_LISTING}
CONVERSION = {
    "assetCategory": "CASH",
    "symbol": "GBP.USD",
    "description": "GBP.USD",
    "conid": "12087797",
    "currency": "USD",
}
ASSETS: tuple[str, ...] = ("GBP", "USD", "ETFA", "ETFG")
# Every section the owner's query lists, in every statement, rows or not.
SECTIONS = (
    "OpenPositions",
    "PriorPeriodPositions",
    "CashReport",
    "Trades",
    "CashTransactions",
    "ConversionRates",
    "CorporateActions",
    "Transfers",
    "EquitySummaryInBase",
)


def ymd(day: date) -> str:
    return day.strftime("%Y%m%d")


def plain(value: Decimal) -> str:
    return format(value, "f")


def moment(day: date, minute: int) -> str:
    """An IBKR dateTime on the day, 09:30 New York plus `minute`, with its zone suffix."""
    hour, rest = divmod(9 * 60 + 30 + minute, 60)
    suffix = "EDT" if NEW_YORK.dst(datetime.combine(day, time(12), NEW_YORK)) else "EST"
    return f"{ymd(day)};{hour:02d}{rest:02d}00 {suffix}"


@dataclass(frozen=True)
class Event:
    """One row IBKR records, and what it changes in the account (asset -> quantity)."""

    day: int
    section: str  # Trade, CashTransaction or CorporateAction
    attributes: dict[str, str]
    effects: dict[str, Decimal]
    label: str


@dataclass(frozen=True)
class History:
    start: date
    days: int
    opened: date
    opening: dict[str, Decimal]  # held before the first day
    events: tuple[Event, ...]
    marks: dict[tuple[str, int], Decimal]  # (listing, day) -> mark price, day -1 included
    rates: dict[int, Decimal]  # day -> USD to GBP
    # End-of-day holdings, filled in __post_init__: day -> asset -> quantity; day -1 is the
    # opening.
    held: dict[int, dict[str, Decimal]] = field(default_factory=dict[int, dict[str, Decimal]])

    def __post_init__(self) -> None:
        running = dict(self.opening)
        self.held[-1] = dict(running)
        by_day: defaultdict[int, list[Event]] = defaultdict(list)
        for event in self.events:
            by_day[event.day].append(event)
        for day in range(self.days):
            for event in by_day[day]:
                for asset, quantity in event.effects.items():
                    running[asset] += quantity
            self.held[day] = dict(running)

    def date_of(self, day: int) -> date:
        return self.start + timedelta(days=day)

    @property
    def account(self) -> AccountSpec:
        return AccountSpec(ACCOUNT_ID, date_opened=self.opened)

    @property
    def has_opening(self) -> bool:
        return self.opened < self.start

    def describe(self) -> str:
        lines = [
            f"{self.days} days from {self.start}, opened {self.opened}, opening "
            + ", ".join(f"{a} {plain(q)}" for a, q in self.opening.items() if q)
        ]
        lines += [f"  day {e.day}: {e.label}" for e in self.events]
        return "\n".join(lines)

    # Statement rows --------------------------------------------------------------------------

    def _event_rows(self, first: int, last: int, by_day: bool) -> list[RowSpec]:
        rows: list[RowSpec] = []
        for event in self.events:
            if first <= event.day <= last:
                day = self.date_of(event.day) if by_day else None
                rows.append(RowSpec(event.section, ACCOUNT_ID, day=day, **event.attributes))
                if event.section == "CashTransaction":
                    # IBKR repeats each cash transaction at the SUMMARY level, without its
                    # id and with a date-only dateTime; the parser reads DETAIL only.
                    summary = {k: v for k, v in event.attributes.items() if k != "transactionID"}
                    on = ymd(self.date_of(event.day))
                    summary |= {"levelOfDetail": "SUMMARY", "dateTime": on}
                    rows.append(RowSpec(event.section, ACCOUNT_ID, day=day, **summary))
        return rows

    def _positions(self, day: int, by_day: bool) -> list[RowSpec]:
        on = self.date_of(day)
        return [
            RowSpec(
                "OpenPosition",
                ACCOUNT_ID,
                day=on if by_day else None,
                **listing,
                reportDate=ymd(on),
                position=plain(self.held[day][symbol]),
                markPrice=plain(self.marks[(symbol, day)]),
                levelOfDetail="SUMMARY",
                side="Long",
            )
            for symbol, listing in LISTINGS.items()
            if self.held[day][symbol] != 0
        ]

    def _cash_report(self, first: int, last: int, by_day: bool) -> list[RowSpec]:
        day = self.date_of(first) if by_day else None
        rows = [
            RowSpec(
                "CashReportCurrency",
                ACCOUNT_ID,
                day=day,
                currency=currency,
                fromDate=ymd(self.date_of(first)),
                toDate=ymd(self.date_of(last)),
                startingCash=plain(self.held[first - 1][currency]),
                endingCash=plain(self.held[last][currency]),
                levelOfDetail="Currency",
            )
            for currency in ("GBP", "USD")
        ]
        # The base-currency summary, which the normalizer skips.
        base = self.held[last]["GBP"] + (self.held[last]["USD"] * self.rates[last]).quantize(CENT)
        rows.append(
            RowSpec(
                "CashReportCurrency",
                ACCOUNT_ID,
                day=day,
                currency="BASE_SUMMARY",
                fromDate=ymd(self.date_of(first)),
                toDate=ymd(self.date_of(last)),
                endingCash=plain(base),
                levelOfDetail="BaseCurrency",
            )
        )
        return rows

    def _daily(self, day: int, by_day: bool) -> list[RowSpec]:
        """What a statement lists for each day it covers: conversion rates, the prior day's
        prices, and NAV."""
        on = self.date_of(day)
        tag = on if by_day else None
        rows = [
            RowSpec(
                "ConversionRate",
                ACCOUNT_ID,
                day=tag,
                reportDate=ymd(on),
                fromCurrency="USD",
                toCurrency="GBP",
                rate=plain(self.rates[day]),
            ),
            # A currency IBKR has no rate for that day.
            RowSpec(
                "ConversionRate",
                ACCOUNT_ID,
                day=tag,
                reportDate=ymd(on),
                fromCurrency="EUR",
                toCurrency="GBP",
                rate="-1",
            ),
        ]
        before = self.date_of(day - 1)
        rows += [
            RowSpec(
                "PriorPeriodPosition",
                ACCOUNT_ID,
                day=tag,
                **listing,
                reportDate=ymd(on),
                date=ymd(before),
                price=plain(self.marks[(symbol, day - 1)]),
            )
            for symbol, listing in LISTINGS.items()
            if self.held[day - 1][symbol] != 0
        ]
        held, rate = self.held[day], self.rates[day]
        securities = (
            held["ETFG"] * self.marks[("ETFG", day)]
            + held["ETFA"] * self.marks[("ETFA", day)] * rate
        )
        cash = held["GBP"] + held["USD"] * rate
        rows.append(
            RowSpec(
                "EquitySummaryByReportDateInBase",
                ACCOUNT_ID,
                day=tag,
                reportDate=ymd(on),
                cash=plain(cash.quantize(CENT)),
                stock=plain(securities.quantize(CENT)),
                total=plain((cash + securities).quantize(CENT)),
            )
        )
        return rows

    def statement(self, window: "Window") -> bytes:
        """The Flex statement of the window, every section listed."""
        first, last = window.first, window.last
        rows = self._event_rows(first, last, window.by_day)
        if window.by_day:
            for day in range(first, last + 1):
                rows += self._positions(day, True)
                rows += self._cash_report(day, day, True)
                rows += self._daily(day, True)
        else:
            rows += self._positions(last, False)
            rows += self._cash_report(first, last, False)
            for day in range(first, last + 1):
                rows += self._daily(day, False)
        return build_statement(
            [self.account],
            period=(self.date_of(first), self.date_of(last)),
            rows=rows,
            by_day=window.by_day,
            empty_sections=SECTIONS,
        )


@dataclass(frozen=True)
class Window:
    first: int
    last: int
    by_day: bool = True

    def days(self) -> range:
        return range(self.first, self.last + 1)


# Strategies ----------------------------------------------------------------------------------

KINDS = ("buy", "sell", "convert", "deposit", "withdrawal", "fee")
type RawEvent = tuple[str, bool, int, int, int, bool, int, int]


@dataclass(frozen=True)
class Split:
    """A forward split, ratio for 1, of a listing held then (none if nothing is held)."""

    ratio: int


_raw_events = st.tuples(
    st.sampled_from(KINDS),
    st.booleans(),  # which listing (USD or GBP)
    st.integers(1, 2_000),  # quantity in thousandths: whole shares and fractional ones
    st.integers(50_000_000, 150_000_000),  # true price in millionths
    st.integers(0, 500),  # commission in cents
    st.booleans(),  # commission in GBP rather than the trade's currency
    st.integers(-500_000, 500_000),  # GBP converted (cents), or a cash amount
    st.integers(1_200_000, 1_400_000),  # true GBP.USD rate in millionths
)


def _quantity(thousandths: int) -> Decimal:
    # Mostly whole shares; every fifth draw is fractional.
    if thousandths % 5:
        return Decimal(thousandths // 20 + 1)
    return Decimal(thousandths) / 1000


@st.composite
def histories(draw: st.DrawFn) -> History:
    days = draw(st.integers(10, 60), label="days")
    start = FIRST_START + timedelta(days=draw(st.integers(0, 120), label="start offset"))
    opening = dict.fromkeys(ASSETS, Decimal(0))
    opened = start
    if draw(st.booleans(), label="held before the first day"):
        opened = start - timedelta(days=200)
        opening["GBP"] = Decimal(draw(st.integers(0, 2_000_000))) / 100
        opening["USD"] = Decimal(draw(st.integers(0, 500_000))) / 100
        opening["ETFA"] = Decimal(draw(st.integers(0, 40)))
        opening["ETFG"] = _quantity(draw(st.integers(0, 2_000)))
    seed = draw(st.integers(0, 2**32 - 1), label="prices seed")
    rng = random.Random(seed)
    marks: dict[tuple[str, int], Decimal] = {}
    rates: dict[int, Decimal] = {}
    for day in range(-1, days):
        for symbol in LISTINGS:
            marks[(symbol, day)] = Decimal(rng.randint(5_000_000, 15_000_000)) / 100_000
        rates[day] = Decimal(rng.randint(7_000, 8_500)) / 10_000

    raw = draw(
        st.lists(st.tuples(st.integers(0, days - 1), _raw_events), min_size=1, max_size=30),
        label="events",
    )
    # Late in the history, when something is more likely held.
    split = draw(
        st.none() | st.tuples(st.integers(days // 2, days - 1), st.integers(2, 4)), label="split"
    )
    planned: list[tuple[int, int, RawEvent | Split]] = [
        (day, n, spec) for n, (day, spec) in enumerate(raw)
    ]
    if split is not None:
        planned.append((split[0], len(raw), Split(split[1])))
    planned.sort(key=lambda p: (p[0], p[1]))

    held = dict(opening)
    events: list[Event] = []
    next_id = 10_000_001
    minute: defaultdict[int, int] = defaultdict(int)
    for day, _, spec in planned:
        on = start + timedelta(days=day)
        at = moment(on, minute[day])
        minute[day] += 1
        transaction_id = str(next_id)
        common = {"transactionID": transaction_id, "reportDate": ymd(on), "dateTime": at}
        event = _event(spec, day, on, held, common)
        if event is None:
            continue
        next_id += 1
        for asset, quantity in event.effects.items():
            held[asset] += quantity
        events.append(event)
    return History(start, days, opened, opening, tuple(events), marks, rates)


def _event(
    spec: "RawEvent | Split", day: int, on: date, held: dict[str, Decimal], common: dict[str, str]
) -> Event | None:
    if isinstance(spec, Split):
        # The listing held, the USD one first: a split of nothing is not a row IBKR sends.
        symbol = next((s for s in ("ETFA", "ETFG") if held[s] != 0), None)
        if symbol is None:
            return None
        ratio = spec.ratio
        added = held[symbol] * (ratio - 1)
        return Event(
            day,
            "CorporateAction",
            LISTINGS[symbol]
            | common
            | {
                "description": f"{symbol} SPLIT {ratio} FOR 1",
                "type": "FS",
                "quantity": plain(added),
                "proceeds": "0",
                "actionID": f"3{common['transactionID']}",
            },
            {symbol: added},
            f"split {ratio} for 1 (+{plain(added)} {symbol})",
        )
    kind, usd, thousandths, price_micro, commission_cents, in_gbp, amount, rate_micro = spec
    if kind in ("buy", "sell"):
        symbol = "ETFA" if usd else "ETFG"
        listing = LISTINGS[symbol]
        currency = listing["currency"]
        quantity = _quantity(thousandths)
        if kind == "sell":
            quantity = -min(quantity, held[symbol])
            if quantity == 0:
                return None
        true_price = Decimal(price_micro) / 1_000_000
        proceeds = -(quantity * true_price).quantize(CENT, ROUND_HALF_EVEN)
        commission = -Decimal(commission_cents) / 100
        commission_currency = "GBP" if in_gbp else currency
        effects = {symbol: quantity, currency: proceeds}
        effects[commission_currency] = effects.get(commission_currency, Decimal(0)) + commission
        settle = on + timedelta(days=2)
        return Event(
            day,
            "Trade",
            listing
            | common
            | {
                "quantity": plain(quantity),
                "tradePrice": plain(true_price.quantize(TICK, ROUND_HALF_EVEN)),
                "proceeds": plain(proceeds),
                "ibCommission": plain(commission),
                "ibCommissionCurrency": commission_currency,
                "buySell": "BUY" if quantity > 0 else "SELL",
                "tradeDate": ymd(on),
                "settleDateTarget": ymd(settle),
                "tradeID": f"5{common['transactionID']}",
                "ibOrderID": f"4{common['transactionID']}",
                "levelOfDetail": "EXECUTION",
            },
            effects,
            f"{kind} {plain(quantity)} {symbol} at {plain(true_price)}, fee {plain(commission)}"
            f" {commission_currency}",
        )
    if kind == "convert":
        quantity = Decimal(amount) / 100
        if quantity == 0:
            return None
        true_rate = Decimal(rate_micro) / 1_000_000
        proceeds = -(quantity * true_rate).quantize(CENT, ROUND_HALF_EVEN)
        commission = -Decimal(commission_cents % 300) / 100
        return Event(
            day,
            "Trade",
            CONVERSION
            | common
            | {
                "quantity": plain(quantity),
                "tradePrice": plain(true_rate.quantize(TICK, ROUND_HALF_EVEN)),
                "proceeds": plain(proceeds),
                "ibCommission": plain(commission),
                "ibCommissionCurrency": "GBP",
                "buySell": "BUY" if quantity > 0 else "SELL",
                "tradeDate": ymd(on),
                "levelOfDetail": "EXECUTION",
            },
            {"GBP": quantity + commission, "USD": proceeds},
            f"convert {plain(quantity)} GBP at {plain(true_rate)}",
        )
    cash = Decimal(abs(amount) or 1) / 100
    if kind == "deposit":
        description, type_, value = (
            "CASH RECEIPTS / ELECTRONIC FUND TRANSFERS",
            "Deposits/Withdrawals",
            cash,
        )
    elif kind == "withdrawal":
        description, type_, value = ("DISBURSEMENT", "Deposits/Withdrawals", -cash)
    else:
        description, type_, value = (
            "MINIMUM ACTIVITY FEE",
            "Other Fees",
            -(Decimal(commission_cents or 1) / 100),
        )
    return Event(
        day,
        "CashTransaction",
        common
        | {
            "currency": "GBP",
            "amount": plain(value),
            "type": type_,
            "description": description,
            "settleDate": ymd(on),
            "levelOfDetail": "DETAIL",
        },
        {"GBP": value},
        f"{kind} {plain(value)} GBP",
    )


def one_trade(quantity: Decimal, price: Decimal, proceeds: Decimal) -> History:
    """A one-day history of a single buy of the USD listing with the given figures."""
    start = FIRST_START
    on = ymd(start)
    attributes = USD_LISTING | {
        "transactionID": "10000001",
        "reportDate": on,
        "dateTime": moment(start, 0),
        "quantity": plain(quantity),
        "tradePrice": plain(price),
        "proceeds": plain(proceeds),
        "ibCommission": "0",
        "ibCommissionCurrency": "USD",
        "levelOfDetail": "EXECUTION",
    }
    trade = Event(0, "Trade", attributes, {"ETFA": quantity, "USD": proceeds}, "buy")
    marks = {(symbol, day): Decimal(100) for symbol in LISTINGS for day in (-1, 0)}
    rates = {-1: Decimal("0.8"), 0: Decimal("0.8")}
    return History(start, 1, start, dict.fromkeys(ASSETS, Decimal(0)), (trade,), marks, rates)


@st.composite
def windowings(draw: st.DrawFn, days: int) -> list[Window]:
    """Windows from the first day to the last, of 1 to 30 days, each overlapping the one
    before by 0 to 7 days."""
    windows: list[Window] = []
    first = 0
    while True:
        length = draw(st.integers(1, 30), label="length")
        last = min(first + length - 1, days - 1)
        # Mostly broken out by day, as the owner's query is.
        by_day = draw(st.sampled_from((True, True, True, False)), label="by day")
        windows.append(Window(first, last, by_day))
        if last == days - 1:
            return windows
        overlap = draw(st.integers(0, min(7, last - first)), label="overlap")
        first = last + 1 - overlap


@st.composite
def fetches(draw: st.DrawFn, days: int) -> list[list[Window]]:
    """The windows in the order they are fetched, grouped by the normalize run each is in.

    The first run is the first sync: it holds the earliest window (with others after it, as
    a first sync fetches several), for a first sync starts at the account's history (Decision
    5). The rest arrive in any order, one or more per run (a later sync, a late upload)."""
    windows = draw(windowings(days))
    first_batch = draw(st.just(1) | st.integers(1, len(windows)), label="first sync windows")
    rest = draw(st.permutations(windows[first_batch:]), label="order")
    runs = [windows[:first_batch]]
    while rest:
        size = draw(st.just(1) | st.integers(1, len(rest)), label="run size")
        runs.append(rest[:size])
        rest = rest[size:]
    return runs
