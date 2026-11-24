"""Generated scenarios for the transfer engine's property tests (test_properties.py).

A scenario is the truth (each account's true movements: transfers between the owner's own
accounts, and payments in and out of them) together with the ledger input that truth
produces (connector records, statement-import rows, a manual account's opening snapshot and
its identifier's registration), so what the engine makes of the input can be checked against
the truth.

It is kept to the owner's real use (design §1, §9): one connected Monzo account, sometimes a
second Monzo connection, and one to three manual hybrid bank accounts, all in GBP, with
Faster Payments and BACS between them, a tiny set of amounts (so transfers collide), weekly
recurrences, card payments and payments to and from other people.

Where a documented caveat (design §5.8, Known caveats) would make net worth differ from the
truth, the generator either never produces it (named below) or names it in `p4_caveats`, so
that the net-worth property is checked only where the design promises it."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from hypothesis import strategies as st

LONDON = ZoneInfo("Europe/London")  # the owner's time zone (core.timezone)
START = date(2026, 10, 12)  # a Monday; summer time ends on the 25th, inside the range
DAYS = 21  # transfers are sent within three weeks of START
AMOUNTS = (Decimal(50), Decimal(100))  # tiny on purpose, to force collisions
OPENING = Decimal(1000)  # every account's balance before the scenario
# A connected account's opening balance adjustment is dated here, before its history.
OPENING_DAY = START - timedelta(days=3)
# Net worth is compared on every day from FIRST_DAY to LAST_DAY.
FIRST_DAY = OPENING_DAY
LAST_DAY = START + timedelta(days=DAYS + 7 * 2 + 7)

# Caveat "a counterpart booked later than the window" (design §5.8): never generated. Every
# receiving side is booked from 1 calendar day before to 5 after the sending side (the
# default window, settings.DEFAULTS), mostly the same day (a Faster Payment).
NEVER_LATER_THAN_THE_WINDOW = (-1, 0, 0, 0, 0, 1, 2, 3, 5)

MONZO = "Monzo"
MONZO_2 = "Monzo 2"
IDENTIFIERS = {
    MONZO: "040004:10000001",
    MONZO_2: "040004:10000002",
    "Bank 1": "200000:20000001",
    "Bank 2": "300000:30000002",
    "Bank 3": "400000:40000003",
}
STRANGERS = ("500000:50000001", "600000:60000002")  # other people's bank accounts


def local(day: date, at: time = time()) -> datetime:
    """An instant at the owner's local time on that day."""
    return datetime.combine(day, at, tzinfo=LONDON)


def booking_date(at: datetime) -> date:
    """The booking date posting gives an instant (ledger.resolve_booking_date)."""
    return at.astimezone(LONDON).date()


@dataclass(frozen=True)
class Account:
    name: str
    connected: bool
    identifier: str
    # Manual accounts only: the opening snapshot's as_of (its quantity is the true balance
    # then), and the last day the owner's statement imports cover (None: none imported).
    snapshot_at: datetime | None = None
    imported_through: date | None = None

    def covers(self, at: datetime) -> bool:
        """Whether the account's statement imports have a row for a movement at `at`: one
        after its opening snapshot (its history starts there, design §5.2: what came before
        is inside the snapshot) and on or before the last day imported."""
        assert self.snapshot_at is not None
        return (
            self.imported_through is not None
            and at > self.snapshot_at
            and booking_date(at) <= self.imported_through
        )

    def holds_from(self, earliest: date) -> bool:
        """Whether a counterpart found by amount and date may be taken in a window starting
        on earliest (pairs.Known): the account has imports, and the window doesn't start
        before the first day they list in full, the day after the opening snapshot's (that
        day's rows before it are inside the snapshot; the harness records them so)."""
        assert self.snapshot_at is not None
        return self.imported_through is not None and earliest > booking_date(self.snapshot_at)


@dataclass(frozen=True)
class Movement:
    """One true change of one account's balance."""

    account: str
    quantity: Decimal  # signed
    at: datetime

    @property
    def day(self) -> date:
        return booking_date(self.at)


type RecordKind = Literal["connector", "import"]


@dataclass(frozen=True)
class Record:
    """One record a source reports: a connector transaction, or a statement-import row (a
    manual-origin transaction dated by its day alone, naming nobody)."""

    key: str  # the connector's external id; unique in the scenario
    kind: RecordKind
    movement: Movement
    names: str | None  # the counterparty bank account it names (connector records only)
    bank_transfer: bool
    description: str
    transfer: int | None = None  # the true transfer it is a side of


@dataclass(frozen=True)
class Transfer:
    index: int
    sent: Movement
    received: Movement

    @property
    def amount(self) -> Decimal:
        return self.received.quantity


type Item = tuple[Literal["record", "snapshot", "register"], str]


@dataclass(frozen=True)
class Scenario:
    accounts: tuple[Account, ...]
    transfers: tuple[Transfer, ...]
    noise: tuple[Movement, ...]
    records: tuple[Record, ...]
    # Owner actions (P5), each applied after this many arrivals: (position, kind, selector).
    actions: tuple[tuple[int, str, int, int], ...] = ()
    by_name: dict[str, Account] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "by_name", {a.name: a for a in self.accounts})

    @property
    def manual(self) -> tuple[Account, ...]:
        return tuple(a for a in self.accounts if not a.connected)

    def items(self) -> list[Item]:
        """Everything that arrives, in a canonical order: every record, and each manual
        account's opening snapshot and identifier registration (the owner may create the
        account after its transfers are in the ledger)."""
        found: list[Item] = [("record", r.key) for r in self.records]
        for account in self.manual:
            found += [("snapshot", account.name), ("register", account.name)]
        return found

    def record_of(self, transfer: int, account: str) -> Record | None:
        for record in self.records:
            if record.transfer == transfer and record.movement.account == account:
                return record
        return None

    def movements(self) -> list[Movement]:
        found = list(self.noise)
        for t in self.transfers:
            found += [t.sent, t.received]
        return found

    def snapshot_quantity(self, account: Account) -> Decimal:
        """The account's true balance at its opening snapshot."""
        assert account.snapshot_at is not None
        return OPENING + sum(
            (
                m.quantity
                for m in self.movements()
                if m.account == account.name and m.at <= account.snapshot_at
            ),
            Decimal(0),
        )

    def internal(self, t: Transfer) -> bool:
        """Whether the transfer is between two accounts both in the books at its sides: a
        manual account's history starts at its opening snapshot (design §5.2), so a side at
        or before it is inside the snapshot, and to the books the other side is a payment
        in or out like any other (it is settled against Opening balances)."""
        for side in (t.sent, t.received):
            account = self.by_name[side.account]
            if not account.connected and side.at <= (account.snapshot_at or side.at):
                return False
        return True

    def true_net_worth(self, day: date) -> Decimal:
        """Net worth at the end of `day` as the truth has it: each account in the books
        (a connected one always; a manual one from its opening snapshot's day, at the
        snapshot plus what moved after it), plus the money of internal transfers on its way
        (sent and not yet received, or received before the sending bank booked it)."""
        total = Decimal(0)
        for account in self.accounts:
            mine = [m for m in self.movements() if m.account == account.name and m.day <= day]
            if account.connected:
                total += OPENING + sum((m.quantity for m in mine), Decimal(0))
                continue
            assert account.snapshot_at is not None
            if booking_date(account.snapshot_at) > day:
                continue  # not in the books yet
            total += self.snapshot_quantity(account) + sum(
                (m.quantity for m in mine if m.at > account.snapshot_at), Decimal(0)
            )
        for t in self.transfers:
            if not self.internal(t):
                continue
            sent, received = t.sent.day <= day, t.received.day <= day
            if sent and not received:
                total += t.amount
            elif received and not sent:
                total -= t.amount
        return total

    def p4_caveats(self) -> list[str]:
        """The documented caveats (design §5.8, Known caveats) under which net worth may
        differ from the truth, that apply to this scenario by construction. (Review, the
        first caveat, is read from the ledger: it depends on what the engine found.)"""
        found: list[str] = []
        for t in self.transfers:
            for manual_side, other_side in ((t.sent, t.received), (t.received, t.sent)):
                account = self.by_name[manual_side.account]
                if account.connected:
                    continue
                assert account.snapshot_at is not None
                own = self.record_of(t.index, account.name)
                other = self.record_of(t.index, other_side.account)
                earliest, latest = _window(other_side)
                one_sided = other is not None and other.names is not None
                if own is not None and (
                    own.names is not None or not one_sided or account.holds_from(earliest)
                ):
                    continue  # its own row is in the ledger and taken, not a placeholder
                if own is not None:
                    # Caveat "a window starting before the days imported" (design §5.8):
                    # its own row, found by amount and date only, isn't taken.
                    found.append(f"window_starts_before_imports: transfer {t.index}")
                    continue
                if (
                    one_sided
                    and account.holds_from(earliest)
                    and self._stand_in(t, manual_side, earliest, latest)
                ):
                    # Caveat "an unrelated row can stand in until the next statement" (design
                    # §5.8): its own row is after the last day imported, and another of the
                    # same amount is taken for it until the next statement brings it.
                    found.append(f"stand_in_until_next_statement: transfer {t.index}")
                # Caveat "a snapshot dated between the two banks' bookings of a multi-day
                # transfer": the snapshot and the placeholder (dated as the other side's
                # record) disagree about which side of it the money is on.
                if (manual_side.at <= account.snapshot_at) != (
                    other_side.at <= account.snapshot_at
                ):
                    found.append(f"snapshot_in_settlement_gap: transfer {t.index}")
        return found

    def _stand_in(self, t: Transfer, manual_side: Movement, earliest: date, latest: date) -> bool:
        """Whether another imported bank transfer of the transfer's amount, naming nobody, is
        booked on its manual side's account inside the window."""
        return any(
            r.kind == "import"
            and r.transfer != t.index
            and r.movement.account == manual_side.account
            and r.movement.quantity == manual_side.quantity
            and r.bank_transfer
            and r.names is None
            and earliest <= r.movement.day <= latest
            for r in self.records
        )


def _window(side: Movement) -> tuple[date, date]:
    """The booking dates the counterpart of a record booked as `side` may have (the default
    window, pairs.counterpart_bounds)."""
    if side.quantity < 0:
        return side.day - timedelta(days=1), side.day + timedelta(days=5)
    return side.day - timedelta(days=5), side.day + timedelta(days=1)


def _at(day: date, minute_of_day: int) -> datetime:
    return local(day, time(minute_of_day // 60, minute_of_day % 60))


@st.composite
def scenarios(draw: st.DrawFn, *, owner_actions: bool = False) -> Scenario:
    connected = [MONZO] + ([MONZO_2] if draw(st.booleans()) else [])
    manual_names = [f"Bank {n}" for n in range(1, draw(st.integers(1, 3)) + 1)]
    accounts: list[Account] = [Account(name, True, IDENTIFIERS[name]) for name in connected]
    for name in manual_names:
        # The opening snapshot: before the scenario, or inside it (transfers before it are
        # then inside it, design §5.2).
        snapshot_day = START + timedelta(days=draw(st.integers(-3, 14)))
        snapshot_at = local(snapshot_day, time(draw(st.sampled_from((9, 18)))))
        imported = draw(st.sampled_from(("none", "all", "some")))
        imported_through = {
            "none": None,
            "all": LAST_DAY,
            "some": snapshot_day + timedelta(days=draw(st.integers(0, 20))),
        }[imported]
        accounts.append(Account(name, False, IDENTIFIERS[name], snapshot_at, imported_through))
    by_name = {a.name: a for a in accounts}
    # Transfers between own accounts, at least one side connected: a transfer between two
    # manual accounts whose rows name nobody is no transfer the engine can see (§5.7).
    pairs = [
        (x, y) for x in by_name for y in by_name if x != y and (x in connected or y in connected)
    ]
    transfers: list[Transfer] = []
    for _ in range(draw(st.integers(1, 4))):
        sender, receiver = draw(st.sampled_from(pairs))
        amount = draw(st.sampled_from(AMOUNTS))
        day = START + timedelta(days=draw(st.integers(0, DAYS - 1)))
        delay = timedelta(days=draw(st.sampled_from(NEVER_LATER_THAN_THE_WINDOW)))
        sent_minute = draw(st.integers(0, 24 * 60 - 1))
        received_minute = draw(st.integers(0, 24 * 60 - 1))
        for week in range(draw(st.integers(1, 2))):  # weekly recurrences of the same amount
            sent_day = day + timedelta(weeks=week)
            # A connected account's record carries its time; a manual account's row only
            # its date (00:00).
            sent_at = _at(sent_day, sent_minute) if by_name[sender].connected else local(sent_day)
            received_day = sent_day + delay
            received_at = (
                _at(received_day, received_minute)
                if by_name[receiver].connected
                else local(received_day)
            )
            transfers.append(
                Transfer(
                    len(transfers),
                    Movement(sender, -amount, sent_at),
                    Movement(receiver, amount, received_at),
                )
            )
    noise: list[tuple[Movement, str]] = []
    for _ in range(draw(st.integers(0, 4))):
        account = by_name[draw(st.sampled_from(list(by_name)))]
        kind = draw(st.sampled_from(("card", "payee", "receipt")))
        amount = draw(st.sampled_from(AMOUNTS))
        quantity = {
            "card": amount if draw(st.booleans()) else -amount,  # a purchase, or a refund
            "payee": -amount,
            "receipt": amount,
        }[kind]
        day = START + timedelta(days=draw(st.integers(0, DAYS + 5)))
        at = _at(day, draw(st.integers(0, 24 * 60 - 1))) if account.connected else local(day)
        if not account.connected and not account.covers(at):
            # A manual account's payments are known only from its statement imports: one
            # outside them is no part of this scenario's truth.
            continue
        noise.append((Movement(account.name, quantity, at), kind))
    records: list[Record] = []
    for t in transfers:
        for side, other in ((t.sent, t.received), (t.received, t.sent)):
            account = by_name[side.account]
            key = f"t{t.index}-{'out' if side is t.sent else 'in'}"
            if account.connected:
                # A Monzo Faster Payment names the other account, both ways round (§1).
                names = by_name[other.account].identifier
                records.append(Record(key, "connector", side, names, True, "Transfer", t.index))
            elif account.covers(side.at):
                records.append(Record(key, "import", side, None, True, "Faster payment", t.index))
    for n, (m, kind) in enumerate(noise):
        account = by_name[m.account]
        bank_transfer = kind != "card"
        names = None
        if account.connected and bank_transfer:
            names = draw(st.sampled_from(STRANGERS))
        records.append(
            Record(
                f"n{n}",
                "connector" if account.connected else "import",
                m,
                names,
                bank_transfer,
                {"card": "Card payment", "payee": "To a friend", "receipt": "From a friend"}[kind],
            )
        )
    actions: list[tuple[int, str, int, int]] = []
    if owner_actions:
        arrivals = len(records) + 2 * len(manual_names)
        for _ in range(draw(st.integers(1, 6))):
            actions.append(
                (
                    # Often once everything has arrived, when there is most to decide on.
                    draw(st.one_of(st.just(arrivals), st.integers(0, arrivals))),
                    draw(st.sampled_from(("reject", "unreject", "unpair", "pair"))),
                    draw(st.integers(0, 7)),
                    draw(st.integers(0, 7)),
                )
            )
        actions.sort(key=lambda a: a[0])
    return Scenario(
        accounts=tuple(accounts),
        transfers=tuple(transfers),
        noise=tuple(m for m, _ in noise),
        records=tuple(records),
        actions=tuple(actions),
    )


def describe(scenario: Scenario) -> str:
    """The scenario, readable, for a failing example's report."""
    lines = []
    for a in scenario.accounts:
        if a.connected:
            lines.append(f"{a.name}: connected")
        else:
            lines.append(
                f"{a.name}: manual, snapshot {a.snapshot_at:%a %d %b %H:%M}"
                f" = {scenario.snapshot_quantity(a)}, imported through {a.imported_through}"
            )
    for t in scenario.transfers:
        lines.append(
            f"transfer {t.index}: {t.sent.account} -> {t.received.account} {t.amount},"
            f" sent {t.sent.at:%a %d %b %H:%M}, received {t.received.at:%a %d %b %H:%M}"
        )
    for r in scenario.records:
        lines.append(
            f"record {r.key}: {r.kind} on {r.movement.account} {r.movement.quantity}"
            f" at {r.movement.at:%a %d %b %H:%M}, names {r.names}, bank_transfer"
            f" {r.bank_transfer}"
        )
    for action in scenario.actions:
        lines.append(f"action {action}")
    return "\n".join(lines)


def by_day(scenario: Scenario) -> dict[date, Decimal]:
    """The true net worth on every day compared."""
    days = [FIRST_DAY + timedelta(days=n) for n in range((LAST_DAY - FIRST_DAY).days + 1)]
    return defaultdict(Decimal, {d: scenario.true_net_worth(d) for d in days})


# Cross-currency scenarios (eToro design §5.12) ------------------------------------------------
#
# The GBP-only scenarios above are left as they are. These are eToro's moves between eToro
# Money (GBP) and eToro Trading (USD): both legs connector records naming each other's
# account (`etoro_account`), stamped by one clock, so the two sides are at most the near
# window apart (Task 0: the same second), with the statement's conversion fee on the USD leg
# or none. Moves are hours apart, so each leg is the only candidate for the other (two within
# the window are the owner's, a case test_cross_currency pins). With `owner_actions`, there
# are also payouts the engine cannot see as transfers: a Withdraw Request of USD from Trading
# that a UK bank receives in GBP a day or so later, neither naming the other (design §5.12,
# "Owner pairs records whose sides differ"); and the owner pairs and unpairs records at
# drawn points of the arrival order.

CROSS_START = datetime(2026, 11, 2, 9, 0, tzinfo=LONDON)
GBP_AMOUNTS = (Decimal("250.50"), Decimal("1000.00"), Decimal("500.00"))
USD_PER_GBP = (Decimal("1.26"), Decimal("1.3125"))
CONVERSION_FEES = (None, Decimal("6.30"), Decimal("0.01"))
# The seconds between a move's two legs: within the near window (settings.DEFAULTS, 2 min).
LEG_GAPS = (0, 0, 0, 1, 59, 120)
MONEY = "eToro Money"
TRADING = "eToro Trading"
BANK = "Bank"


@dataclass(frozen=True)
class Leg:
    """One connector record of a cross-currency scenario."""

    key: str  # its external id, unique in the scenario
    account: str
    currency: str
    quantity: Decimal  # signed
    at: datetime
    names: str | None  # the account it names, by name (etoro_account), or None
    fee: Decimal | None = None  # the USD conversion fee its connector gives
    bank_transfer: bool = False


@dataclass(frozen=True)
class CrossTransfer:
    index: int
    kind: Literal["move", "payout"]  # a payout names nobody: only the owner can pair it
    sent: str  # leg keys
    received: str


@dataclass(frozen=True)
class CrossScenario:
    legs: tuple[Leg, ...]
    transfers: tuple[CrossTransfer, ...]
    # Owner actions, each applied after this many arrivals: (position, "pair" | "unpair",
    # transfer index).
    actions: tuple[tuple[int, str, int], ...] = ()

    def leg(self, key: str) -> Leg:
        return next(leg for leg in self.legs if leg.key == key)


def _usd(gbp: Decimal, rate: Decimal) -> Decimal:
    return (gbp * rate).quantize(Decimal("0.01"))


@st.composite
def cross_currency_scenarios(draw: st.DrawFn, *, owner_actions: bool = False) -> CrossScenario:
    legs: list[Leg] = []
    transfers: list[CrossTransfer] = []
    kinds = ["move", "payout"] if owner_actions else ["move"]
    for n in range(draw(st.integers(1, 3))):
        kind = draw(st.sampled_from(kinds))
        at = CROSS_START + timedelta(hours=3 * n, minutes=draw(st.integers(0, 59)))
        gbp = draw(st.sampled_from(GBP_AMOUNTS))
        usd = _usd(gbp, draw(st.sampled_from(USD_PER_GBP)))
        if kind == "move":
            to_trading = draw(st.booleans())
            fee = draw(st.sampled_from(CONVERSION_FEES))
            gap = timedelta(seconds=draw(st.sampled_from(LEG_GAPS)))
            # Either side may be stamped first.
            money_at, trading_at = (at, at + gap) if draw(st.booleans()) else (at + gap, at)
            money = Leg(
                f"cash:{n}",
                MONEY,
                "GBP",
                -gbp if to_trading else gbp,
                money_at,
                TRADING,
            )
            trading = Leg(
                f"activity:{n}",
                TRADING,
                "USD",
                usd if to_trading else -usd,
                trading_at,
                MONEY,
                fee,
            )
            legs += [money, trading]
            sent, received = (money, trading) if to_trading else (trading, money)
        else:
            # USD out of Trading to a UK bank, which books GBP a day or two later.
            sent = Leg(f"activity:{n}", TRADING, "USD", -usd, at, None, bank_transfer=True)
            received = Leg(
                f"bank:{n}",
                BANK,
                "GBP",
                gbp,
                at + timedelta(days=draw(st.integers(1, 2)), hours=draw(st.integers(0, 5))),
                None,
                bank_transfer=True,
            )
            legs += [sent, received]
        transfers.append(CrossTransfer(n, kind, sent.key, received.key))
    actions: list[tuple[int, str, int]] = []
    if owner_actions:
        arrivals = len(legs) + 3  # the three accounts' opening deposits arrive too
        for _ in range(draw(st.integers(1, 5))):
            actions.append(
                (
                    draw(st.one_of(st.just(arrivals), st.integers(0, arrivals))),
                    draw(st.sampled_from(("pair", "unpair"))),
                    draw(st.integers(0, len(transfers) - 1)),
                )
            )
        actions.sort(key=lambda a: a[0])
    return CrossScenario(tuple(legs), tuple(transfers), tuple(actions))


def describe_cross(scenario: CrossScenario) -> str:
    lines = [
        f"{leg.key}: {leg.account} {leg.quantity} {leg.currency} at {leg.at:%a %d %H:%M:%S},"
        f" names {leg.names}, fee {leg.fee}"
        for leg in scenario.legs
    ]
    lines += [
        f"transfer {t.index} ({t.kind}): {t.sent} -> {t.received}" for t in scenario.transfers
    ]
    lines += [f"action {a}" for a in scenario.actions]
    return "\n".join(lines)
