"""Generated Binance account histories for the property tests (test_properties.py).

`histories()` draws a consistent account history: what the Spot, Funding and Earn wallets
held before it (the opening the records no longer explain) and a time-ordered list of records
over four assets: deposits (some pending first), withdrawals (some failing after they were
applied), spot trades (some with a rounded `quoteQty`), converts, asset dividends, flexible
Earn subscriptions, redemptions and `REALTIME` rewards (two identical ones at once, at
times), `BONUS` payouts (a reward row and the dividend that paid it, within 26 hours),
wallet-to-wallet transfers between Spot and Funding (some failed) and dust conversions on Spot
(one or two small balances into BNB, with a service charge at times). No wallet ever holds
less than nothing.

Each `Record` is what Binance lists (one or more rows, each changing as time passes: a deposit
is credited, a withdrawal completes or fails) and what it does to the balances (`Effect`s,
each at its own moment). `History.balance(at)` is the true balance then; the fake's reports
are cut from it, so they are exact (no Earn reward still accruing).

`schedule(history)` draws the moments the syncs start: some anywhere, one inside each failing
withdrawal's life (so it is seen applied before it fails), some just before a record's moment
so that the record falls inside the balance reads and they are made again, and a last one
after everything. Each sync walks windows from its own clock, so two schedules fetch the same
history in different, overlapping windows.

Every time is whole seconds after Binance's launch (the fetch floor), so the first sync's
backward walk is short; withdrawals' text times are exact.
"""

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal

from hypothesis import strategies as st

from lararium.modules.finance.binance.fetch import LAUNCH_MS
from lararium.modules.finance.binance.parse import ApiValue

from .fixtures import DAY_MS, HOUR_MS, build_row

type Rowdict = dict[str, ApiValue]
type Holding = tuple[str, str]  # (wallet key, asset)

SECOND_MS = 1_000
MINUTE_MS = 60 * SECOND_MS
# The first moment a history may start: a day after the fetch floor.
START_MS = LAUNCH_MS + DAY_MS
# BNB is what dust converts into.
DUST_TARGET = "BNB"
ASSETS = ("AAA", "BBB", "CCC", "STB", DUST_TARGET)
# What `exchange-info` lists: (symbol, base, quote).
PAIRS = (
    ("AAASTB", "AAA", "STB"),
    ("BBBSTB", "BBB", "STB"),
    ("AAABBB", "AAA", "BBB"),
    ("CCCSTB", "CCC", "STB"),
)
WALLET_KEYS = ("spot", "funding", "earn")
ACCOUNT_NAMES = {"spot": "Binance Spot", "funding": "Binance Funding", "earn": "Binance Earn"}
_WALLET_TYPE = {"spot": 0, "funding": 1}
_EARN_SOURCE = {"spot": "SPOT", "funding": "FUNDING"}
# Amounts are k / 10^4. Rule 3 links a BONUS reward to a dividend of the same asset and
# amount within 26 hours: each kind of income keeps its own last digit, so only a
# BONUS payout's two rows can ever be linked.
_UNIT = Decimal("0.0001")
_REALTIME_DIGIT, _DIVIDEND_DIGIT, _PAYOUT_DIGIT = 3, 7, 5
# The kinds whose removal leaves one (account, instrument) changed.
SINGLE_SIDED = frozenset({"deposit", "withdrawal", "dividend", "payout", "reward"})
TWO_SIDED = frozenset({"trade", "convert", "subscription", "redemption", "transfer", "dust"})


@dataclass(frozen=True)
class Effect:
    """What a record does to one wallet's asset, from `at_ms` on."""

    at_ms: int
    wallet: str
    asset: str
    delta: Decimal


@dataclass(frozen=True, eq=False)
class Record:
    """One thing that happened: the rows Binance lists for it, per label, each as the versions
    it shows from a moment on (`(from_ms, row)`, oldest first), and its effects."""

    kind: str
    rows: tuple[tuple[str, tuple[tuple[int, Rowdict], ...]], ...]
    effects: tuple[Effect, ...]
    failing: bool = False

    @property
    def listed_ms(self) -> int:
        """The first moment Binance lists any of its rows."""
        return min(versions[0][0] for _, versions in self.rows)

    @property
    def last_ms(self) -> int:
        """The last moment anything about it changes: a row's version or an effect."""
        moments = [at for _, versions in self.rows for at, _ in versions]
        return max([*moments, *(e.at_ms for e in self.effects)])

    def moments(self) -> list[int]:
        return sorted({at for _, versions in self.rows for at, _ in versions} | self.times())

    def times(self) -> set[int]:
        return {e.at_ms for e in self.effects}

    def row_at(self, label: str, now_ms: int) -> Rowdict | None:
        """The version of its row of `label` Binance shows at `now_ms` (None before it)."""
        shown: Rowdict | None = None
        for name, versions in self.rows:
            if name != label:
                continue
            for at, row in versions:
                if at <= now_ms:
                    shown = row
        return shown

    def net(self) -> dict[Holding, Decimal]:
        """What it changes in all, per (wallet, asset), zeros left out."""
        total: defaultdict[Holding, Decimal] = defaultdict(Decimal)
        for effect in self.effects:
            total[(effect.wallet, effect.asset)] += effect.delta
        return {holding: q for holding, q in total.items() if q != 0}

    def describe(self) -> str:
        effects = ", ".join(
            f"{e.wallet} {e.asset} {e.delta:+} @{_shown(e.at_ms)}" for e in self.effects
        )
        return f"{self.kind} listed {_shown(self.listed_ms)}: {effects or 'no effect'}"


@dataclass
class History:
    start_ms: int
    initial: dict[Holding, Decimal]
    records: list[Record] = field(default_factory=list)

    @property
    def end_ms(self) -> int:
        return max([self.start_ms, *(r.last_ms for r in self.records)])

    def effects(self) -> list[Effect]:
        return sorted((e for r in self.records for e in r.effects), key=lambda e: e.at_ms)

    def balance(self, at_ms: int) -> dict[Holding, Decimal]:
        """The true balance at `at_ms` (every effect at or before it), zeros left out: the
        balance after each record is this at its moments."""
        held: defaultdict[Holding, Decimal] = defaultdict(Decimal, self.initial)
        for effect in self.effects():
            if effect.at_ms <= at_ms:
                held[(effect.wallet, effect.asset)] += effect.delta
        return {holding: q for holding, q in held.items() if q != 0}

    def moments(self) -> list[int]:
        return sorted({at for r in self.records for at in r.moments()})

    def describe(self) -> str:
        initial = ", ".join(f"{w} {a} {q}" for (w, a), q in sorted(self.initial.items()))
        lines = [f"start {_shown(self.start_ms)}; initial: {initial or 'nothing'}"]
        lines += [f"  {i}. {r.describe()}" for i, r in enumerate(self.records)]
        return "\n".join(lines)


def _shown(ms: int) -> str:
    when = datetime.fromtimestamp(ms / 1000, UTC)
    return when.strftime("%m-%d %H:%M:%S.") + f"{ms % 1000:03d}"


def _text_time(ms: int) -> str:
    return datetime.fromtimestamp(ms // 1000, UTC).strftime("%Y-%m-%d %H:%M:%S")


def _plain(quantity: Decimal) -> str:
    return format(quantity, "f")


# Drawing a history.


class _Builder:
    """The history drawn so far: what each wallet holds now (an effect still to come counts
    once it has happened), the next ids, the records."""

    def __init__(self, draw: Callable[..., object], start_ms: int) -> None:
        self.draw = draw
        self.now = start_ms
        self.held: defaultdict[Holding, Decimal] = defaultdict(Decimal)
        self.pending: list[Effect] = []
        self.records: list[Record] = []
        self.next_id = 10_000

    def id(self) -> int:
        self.next_id += 1
        return self.next_id

    def advance(self, to_ms: int) -> None:
        self.now = to_ms
        for effect in sorted(self.pending, key=lambda e: e.at_ms):
            if effect.at_ms <= to_ms:
                self.held[(effect.wallet, effect.asset)] += effect.delta
        self.pending = [e for e in self.pending if e.at_ms > to_ms]

    def add(self, record: Record) -> None:
        for effect in record.effects:
            if effect.at_ms <= self.now:
                self.held[(effect.wallet, effect.asset)] += effect.delta
                assert self.held[(effect.wallet, effect.asset)] >= 0, record.describe()
            else:
                assert effect.delta > 0, record.describe()
                self.pending.append(effect)
        self.records.append(record)

    def holdings(self, wallets: Iterable[str]) -> list[Holding]:
        return sorted(h for h, q in self.held.items() if q > 0 and h[0] in wallets)

    def amount(self, *, at_most: Decimal | None = None, digit: int | None = None) -> Decimal:
        """k / 10^4 for k ≥ 1 (≤ `at_most`), its last digit `digit` when given."""
        top = 1_000_000 if at_most is None else int(at_most / _UNIT)
        if digit is not None:
            if top < digit:
                return Decimal(0)
            k = self.draw(st.integers(0, (top - digit) // 10)) * 10 + digit
        else:
            if top < 1:
                return Decimal(0)
            k = self.draw(st.integers(1, top))
        return k * _UNIT

    def later(self, low_ms: int, high_ms: int) -> int:
        """A whole second from `low_ms` to `high_ms` after now."""
        return self.now + self.draw(st.integers(low_ms // 1000, high_ms // 1000)) * 1000


def _deposit(b: _Builder) -> Record:
    wallet = b.draw(st.sampled_from(("spot", "funding")))
    asset = b.draw(st.sampled_from(ASSETS))
    quantity = b.amount()
    pending = b.draw(st.booleans())
    credited_at = b.later(MINUTE_MS, 2 * DAY_MS) if pending else b.now
    status = b.draw(st.sampled_from((1, 6)))
    transfer_type = b.draw(st.sampled_from((0, 1)))
    base = build_row(
        "deposits",
        id=str(b.id()),
        coin=asset,
        amount=_plain(quantity),
        walletType=_WALLET_TYPE[wallet],
        transferType=transfer_type,
        insertTime=b.now,
        completeTime=credited_at,
        status=status,
    )
    versions: list[tuple[int, Rowdict]] = []
    if pending:
        versions.append((b.now, {**base, "status": 0, "completeTime": None}))
    versions.append((credited_at, base))
    return Record(
        "deposit",
        (("deposits", tuple(versions)),),
        (Effect(credited_at, wallet, asset, quantity),),
    )


def _withdrawal(b: _Builder) -> Record | None:
    choices = b.holdings(("spot", "funding"))
    if not choices:
        return None
    wallet, asset = b.draw(st.sampled_from(choices))
    total = b.held[(wallet, asset)]
    quantity = b.amount(at_most=total)
    fee = b.amount(at_most=total - quantity) if b.draw(st.booleans()) else Decimal(0)
    if quantity == 0:
        return None
    failing = b.draw(st.integers(0, 3)) == 0
    # A withdrawal that fails does so within the 7 days a later walk repeats.
    ended_at = b.later(3 * HOUR_MS, 5 * DAY_MS) if failing else b.later(MINUTE_MS, 2 * DAY_MS)
    first, last = b.draw(st.sampled_from((0, 2, 4))), 6
    if failing:
        last = b.draw(st.sampled_from((1, 3, 5)))
    base = build_row(
        "withdrawals",
        id=str(b.id()),
        coin=asset,
        amount=_plain(quantity),
        transactionFee=_plain(fee),
        walletType=_WALLET_TYPE[wallet],
        transferType=b.draw(st.sampled_from((0, 1))),
        applyTime=_text_time(b.now),
        completeTime=None,
        status=first,
    )
    ended = {**base, "status": last, "completeTime": _text_time(ended_at)}
    effects = [Effect(b.now, wallet, asset, -(quantity + fee))]
    if failing:
        effects.append(Effect(ended_at, wallet, asset, quantity + fee))
    return Record(
        "withdrawal",
        (("withdrawals", ((b.now, base), (ended_at, ended))),),
        tuple(effects),
        failing=failing,
    )


def _trade(b: _Builder) -> Record | None:
    symbol, base, quote = b.draw(st.sampled_from(PAIRS))
    buyer = b.draw(st.booleans())
    price = Decimal(b.draw(st.integers(1, 2_000_000))) * _UNIT
    paying = quote if buyer else base
    have = b.held[("spot", paying)]
    if have <= 0:
        return None
    if buyer:
        # qty times price at most what the quote holds; qty has up to 4 decimal places.
        most = int(have / price / Decimal("0.01"))
        if most < 1:
            return None
        quantity = Decimal(b.draw(st.integers(1, min(most, 100_000)))) * Decimal("0.01")
    else:
        quantity = b.amount(at_most=have)
        if quantity == 0:
            return None
    exact = quantity * price
    # Binance may round quoteQty: to 6 places is within what a price of 4 decimals explains
    # (half a unit of its last place per unit traded, M5 5.7) once 0.01 or more is traded.
    quote_quantity = exact
    if quantity >= Decimal("0.01") and b.draw(st.booleans()):
        quote_quantity = exact.quantize(Decimal("0.000001"), ROUND_HALF_EVEN)
    if buyer and quote_quantity > have:
        return None
    if quote_quantity == 0:
        return None
    fee_asset = b.draw(st.sampled_from((base, quote)))
    left = {
        base: b.held[("spot", base)] + (quantity if buyer else -quantity),
        quote: b.held[("spot", quote)] + (-quote_quantity if buyer else quote_quantity),
    }
    commission = Decimal(0)
    if b.draw(st.booleans()) and left[fee_asset] > 0:
        commission = Decimal(b.draw(st.integers(0, int(min(left[fee_asset], 1) * 10**6)))) / 10**6
    row = build_row(
        "my-trades",
        symbol=symbol,
        id=b.id(),
        orderId=b.id(),
        price=f"{price:.4f}",
        qty=_plain(quantity),
        quoteQty=_plain(quote_quantity),
        commission=_plain(commission),
        commissionAsset=fee_asset,
        time=b.now,
        isBuyer=buyer,
        isMaker=b.draw(st.booleans()),
    )
    effects = [
        Effect(b.now, "spot", base, quantity if buyer else -quantity),
        Effect(b.now, "spot", quote, -quote_quantity if buyer else quote_quantity),
    ]
    if commission:
        effects.append(Effect(b.now, "spot", fee_asset, -commission))
    return Record("trade", (("my-trades", ((b.now, row),)),), tuple(effects))


def _convert(b: _Builder) -> Record | None:
    choices = b.holdings(("spot", "funding"))
    if not choices:
        return None
    wallet, given = b.draw(st.sampled_from(choices))
    received = b.draw(st.sampled_from([a for a in ASSETS if a != given]))
    given_quantity = b.amount(at_most=b.held[(wallet, given)])
    if given_quantity == 0:
        return None
    received_quantity = b.amount()
    row = build_row(
        "convert",
        quoteId=str(b.id()),
        orderId=b.id(),
        orderStatus="SUCCESS",
        fromAsset=given,
        fromAmount=_plain(given_quantity),
        toAsset=received,
        toAmount=_plain(received_quantity),
        ratio=_plain(received_quantity / given_quantity),
        inverseRatio=_plain(given_quantity / received_quantity),
        createTime=b.now,
        walletType=_EARN_SOURCE[wallet],
    )
    effects = (
        Effect(b.now, wallet, given, -given_quantity),
        Effect(b.now, wallet, received, received_quantity),
    )
    return Record("convert", (("convert", ((b.now, row),)),), effects)


def _dividend(b: _Builder) -> Record | None:
    asset = b.draw(st.sampled_from(ASSETS))
    debit = b.draw(st.integers(0, 4)) == 0
    if debit:
        held = b.held[("spot", asset)]
        quantity = b.amount(at_most=held, digit=_DIVIDEND_DIGIT)
    else:
        quantity = b.amount(digit=_DIVIDEND_DIGIT)
    if quantity == 0:
        return None
    row = build_row(
        "dividends",
        id=b.id(),
        tranId=b.id(),
        asset=asset,
        amount=_plain(quantity),
        divTime=b.now,
        direction=-1 if debit else 1,
    )
    effect = Effect(b.now, "spot", asset, -quantity if debit else quantity)
    return Record("dividend", (("dividends", ((b.now, row),)),), (effect,))


def _payout(b: _Builder) -> Record:
    """A BONUS reward paid out as an asset dividend on Spot: two rows, one effect."""
    asset = b.draw(st.sampled_from(ASSETS))
    quantity = b.amount(digit=_PAYOUT_DIGIT)
    # The reward row is within 25 hours of the dividend, either side, never before the start.
    offset = b.draw(st.integers(-25 * 3600, 25 * 3600)) * 1000
    rewarded_at = max(b.now + offset, START_MS)
    dividend = build_row(
        "dividends",
        id=b.id(),
        tranId=b.id(),
        asset=asset,
        amount=_plain(quantity),
        divTime=b.now,
        direction=1,
    )
    reward = build_row(
        "earn-flexible-rewards",
        asset=asset,
        rewards=_plain(quantity),
        productId=f"{asset}001",
        type="BONUS",
        time=rewarded_at,
    )
    return Record(
        "payout",
        (
            ("dividends", ((b.now, dividend),)),
            ("earn-flexible-rewards", ((rewarded_at, reward),)),
        ),
        (Effect(b.now, "spot", asset, quantity),),
    )


def _reward(b: _Builder) -> Record:
    """A REALTIME reward, accrued in Earn; at times two identical rows at once."""
    asset = b.draw(st.sampled_from(ASSETS))
    quantity = b.amount(at_most=Decimal(10), digit=_REALTIME_DIGIT)
    copies = b.draw(st.sampled_from((1, 1, 1, 2)))
    row = build_row(
        "earn-flexible-rewards",
        asset=asset,
        rewards=_plain(quantity),
        productId=f"{asset}001",
        type="REALTIME",
        time=b.now,
    )
    rows = tuple(("earn-flexible-rewards", ((b.now, dict(row)),)) for _ in range(copies))
    return Record("reward", rows, (Effect(b.now, "earn", asset, quantity * copies),))


def _subscription(b: _Builder) -> Record | None:
    choices = b.holdings(("spot", "funding"))
    if not choices:
        return None
    wallet, asset = b.draw(st.sampled_from(choices))
    quantity = b.amount(at_most=b.held[(wallet, asset)])
    if quantity == 0:
        return None
    failed = b.draw(st.integers(0, 5)) == 0
    row = build_row(
        "earn-flexible-subscriptions",
        amount=_plain(quantity),
        asset=asset,
        time=b.now,
        purchaseId=b.id(),
        productId=f"{asset}001",
        type="NORMAL",
        sourceAccount=_EARN_SOURCE[wallet],
        amtFromSpot=_plain(quantity if wallet == "spot" else Decimal(0)),
        amtFromFunding=_plain(quantity if wallet == "funding" else Decimal(0)),
        status="FAILED" if failed else "SUCCESS",
    )
    effects = (
        ()
        if failed
        else (
            Effect(b.now, wallet, asset, -quantity),
            Effect(b.now, "earn", asset, quantity),
        )
    )
    return Record("subscription", (("earn-flexible-subscriptions", ((b.now, row),)),), effects)


def _redemption(b: _Builder) -> Record | None:
    choices = b.holdings(("earn",))
    if not choices:
        return None
    _, asset = b.draw(st.sampled_from(choices))
    quantity = b.amount(at_most=b.held[("earn", asset)])
    if quantity == 0:
        return None
    wallet = b.draw(st.sampled_from(("spot", "funding")))
    row = build_row(
        "earn-flexible-redemptions",
        amount=_plain(quantity),
        asset=asset,
        time=b.now,
        productId=f"{asset}001",
        redeemId=b.id(),
        destAccount=_EARN_SOURCE[wallet],
        status="PAID",
    )
    effects = (
        Effect(b.now, "earn", asset, -quantity),
        Effect(b.now, wallet, asset, quantity),
    )
    return Record("redemption", (("earn-flexible-redemptions", ((b.now, row),)),), effects)


def _transfer(b: _Builder) -> Record | None:
    """A universal transfer between Spot and Funding; a failed one moves nothing."""
    choices = b.holdings(("spot", "funding"))
    if not choices:
        return None
    wallet, asset = b.draw(st.sampled_from(choices))
    other = "funding" if wallet == "spot" else "spot"
    quantity = b.amount(at_most=b.held[(wallet, asset)])
    if quantity == 0:
        return None
    failed = b.draw(st.integers(0, 5)) == 0
    row = build_row(
        "universal-transfer",
        asset=asset,
        amount=_plain(quantity),
        type="MAIN_FUNDING" if wallet == "spot" else "FUNDING_MAIN",
        status="FAILED" if failed else "CONFIRMED",
        tranId=b.id(),
        timestamp=b.now,
    )
    effects = (
        ()
        if failed
        else (Effect(b.now, wallet, asset, -quantity), Effect(b.now, other, asset, quantity))
    )
    return Record("transfer", (("universal-transfer", ((b.now, row),)),), effects)


def _dust(b: _Builder) -> Record | None:
    """One or two small Spot balances converted into BNB, each line with a service charge at
    times: the line buys `transferedAmount + serviceChargeAmount` of BNB and the charge is
    taken out, so Spot's BNB grows by `transferedAmount`, the BNB credited."""
    choices = [h for h in b.holdings(("spot",)) if h[1] != DUST_TARGET]
    if not choices:
        return None
    picked = b.draw(st.lists(st.sampled_from(choices), min_size=1, max_size=2, unique=True))
    trans_id = b.id()
    lines: list[ApiValue] = []
    effects: list[Effect] = []
    received_total = charged_total = Decimal(0)
    for _, asset in picked:
        quantity = b.amount(at_most=b.held[("spot", asset)])
        if quantity == 0:
            continue
        received = b.amount(at_most=Decimal(1))
        charge = b.amount(at_most=Decimal("0.01")) if b.draw(st.booleans()) else Decimal(0)
        received_total += received
        charged_total += charge
        lines.append(
            {
                "transId": trans_id,
                "serviceChargeAmount": _plain(charge),
                "amount": _plain(quantity),
                "operateTime": b.now,
                "transferedAmount": _plain(received),
                "fromAsset": asset,
                "targetAsset": DUST_TARGET,
            }
        )
        effects += [
            Effect(b.now, "spot", asset, -quantity),
            Effect(b.now, "spot", DUST_TARGET, received + charge),
        ]
        if charge:
            effects.append(Effect(b.now, "spot", DUST_TARGET, -charge))
    if not lines:
        return None
    row = build_row(
        "dust",
        transId=trans_id,
        operateTime=b.now,
        totalTransferedAmount=_plain(received_total),
        totalServiceChargeAmount=_plain(charged_total),
        userAssetDribbletDetails=lines,
    )
    return Record("dust", (("dust", ((b.now, row),)),), tuple(effects))


_KINDS: dict[str, Callable[[_Builder], Record | None]] = {
    "deposit": _deposit,
    "withdrawal": _withdrawal,
    "trade": _trade,
    "convert": _convert,
    "dividend": _dividend,
    "payout": _payout,
    "reward": _reward,
    "subscription": _subscription,
    "redemption": _redemption,
    "transfer": _transfer,
    "dust": _dust,
}


@st.composite
def histories(draw: st.DrawFn, *, min_records: int = 1, max_records: int = 10) -> History:
    start = START_MS + draw(st.integers(0, 86_399)) * 1000
    initial: dict[Holding, Decimal] = {}
    for wallet in WALLET_KEYS:
        for asset in ASSETS:
            if draw(st.integers(0, 5)) == 0:
                initial[(wallet, asset)] = Decimal(draw(st.integers(1, 1_000_000))) * _UNIT
    b = _Builder(draw, start)
    b.held.update(initial)
    count = draw(st.integers(min_records, max_records))
    for _ in range(count):
        # Seconds to two days apart, and now and then a burst.
        gap = draw(st.one_of(st.integers(1, 120), st.integers(1, 2 * 86_400)))
        b.advance(b.now + gap * 1000)
        kind = draw(st.sampled_from(sorted(_KINDS)))
        record = _KINDS[kind](b) or _deposit(b)
        b.add(record)
    return History(start, initial, b.records)


# Drawing when the syncs run.


@dataclass(frozen=True)
class Schedule:
    """The moments the syncs start, oldest first; which of them were put just before a
    record's moment (so that it falls inside the reads)."""

    syncs: tuple[int, ...]
    inside: tuple[int, ...] = ()

    def describe(self) -> str:
        marks = ["*" if at in self.inside else "" for at in self.syncs]
        return ", ".join(f"{_shown(at)}{mark}" for at, mark in zip(self.syncs, marks, strict=True))


# A sync's reads and walks take seconds of Binance's clock: a sync that starts this long
# before a moment is over before it.
_CLEAR_MS = 15 * MINUTE_MS


@st.composite
def schedule(draw: st.DrawFn, history: History, *, avoid: Sequence[int] = ()) -> Schedule:
    """Sync moments for the history (see the module's docstring). No sync starts in the
    `_CLEAR_MS` before a moment of `avoid` (a record the fake does not list: no read may be
    cut while it happens, since nothing would make that read again)."""
    low, high = history.start_ms, history.end_ms
    points = draw(st.lists(st.integers(low, high), max_size=3))
    for record in history.records:
        if record.failing:
            applied, ended = record.effects[0].at_ms, record.effects[1].at_ms
            points.append(draw(st.integers(applied + 30 * MINUTE_MS, ended - 30 * MINUTE_MS)))
    inside: list[int] = []
    moments = history.moments()
    if moments:
        for at in draw(st.lists(st.sampled_from(moments), max_size=2, unique=True)):
            start = at - draw(st.integers(0, 1_500))
            inside.append(start)
    points += inside
    clear: list[int] = []
    for at in points:
        for moment in avoid:
            if moment - _CLEAR_MS <= at <= moment:
                at = moment + SECOND_MS
        clear.append(at)
    last = max([high, *clear]) + draw(st.integers(1, 48)) * HOUR_MS
    syncs = tuple(sorted({*clear, last}))
    return Schedule(syncs, tuple(a for a in inside if a in syncs))


def holdings_of(balances: Mapping[Holding, Decimal], wallet: str) -> dict[str, Decimal]:
    return {asset: q for (w, asset), q in balances.items() if w == wallet and q != 0}
