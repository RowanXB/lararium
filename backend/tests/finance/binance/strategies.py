"""Generated Binance account histories for the property tests (test_properties.py).

`histories()` draws a consistent account history: what the Spot, Funding and Earn wallets
held before it (the opening the records no longer explain) and a time-ordered list of records
over four assets: deposits (some pending first), withdrawals (some failing after they were
applied), spot trades (some with a rounded `quoteQty`), converts, asset dividends, flexible
Earn subscriptions, redemptions and `REALTIME` rewards (two identical ones at once, at
times), `BONUS` payouts (a reward row and the dividend that paid it, within 26 hours),
wallet-to-wallet transfers between Spot and Funding (some failed), dust conversions on Spot
(one or two small balances into BNB, with a service charge at times), locked Earn
subscriptions, redemptions to Spot and rewards, yield token subscriptions (swaps on Spot) and
redemptions Binance pays out later (seen pending, then arrived, a fee at times), flexible
crypto loans borrowed onto Spot (some failed) and repaid from it, margin transfers between Spot
and Cross Margin (some also listed by the universal transfer history, some pending first), margin
trades on Cross Margin (no `quoteQty`, a commission at times) and margin interest charged there,
transfers between Spot and USDⓈ-M Futures (listed by the universal transfer history and by the
futures income history as a `TRANSFER` row, rule 2) and futures income (realised profit and
funding either way, commissions paid), transfers between Spot and Options (listed by the
universal transfer history and by the options bill as a `TRANSFER` row, rule 2) and options
bill rows (premiums and exercise profit either way, fees paid), fiat deposits (some pending first)
and withdrawals by bank on Spot, card purchases (from a card, or from the cash balance) and sales
on Spot, P2P buys and sells on Funding, and Pay payments and receipts on Funding or Spot (a
payment at times drawn from both). The fiat
is EUR (`FIAT`), in cents; the other kinds never move it. Rebates are not drawn: Binance serves
them from June 2020 only, and the histories start in July 2017. No wallet ever holds less than
nothing, except Crypto Loans, whose holdings are debts: never more than nothing (a debt there
before the history is a loan whose borrow is not listed, as a VIP loan's never is).

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
# The currency of the fiat kinds: Binance marks it legal money (the World's coin-config), so it
# is the ledger's EUR. Only they move it, in cents.
FIAT = "EUR"
_CENT = Decimal("0.01")
# What `exchange-info` lists: (symbol, base, quote).
PAIRS = (
    ("AAASTB", "AAA", "STB"),
    ("BBBSTB", "BBB", "STB"),
    ("AAABBB", "AAA", "BBB"),
    ("CCCSTB", "CCC", "STB"),
)
WALLET_KEYS = ("spot", "funding", "earn", "loans", "cross_margin", "um_futures", "options")
ACCOUNT_NAMES = {
    "spot": "Binance Spot",
    "funding": "Binance Funding",
    "earn": "Binance Earn",
    "loans": "Binance Crypto Loans",
    "cross_margin": "Binance Cross Margin",
    "um_futures": "Binance USDⓈ-M Futures",
    "options": "Binance Options",
}
CROSS = "cross_margin"
FUTURES = "um_futures"
OPTIONS = "options"
# The wallet whose holdings are debts (negative).
DEBTS = "loans"
_WALLET_TYPE = {"spot": 0, "funding": 1}
_EARN_SOURCE = {"spot": "SPOT", "funding": "FUNDING"}
# Amounts are k / 10^4. Rule 3 links a BONUS reward to a dividend of the same asset and
# amount within 26 hours: each kind of income keeps its own last digit, so only a
# BONUS payout's two rows can ever be linked.
_UNIT = Decimal("0.0001")
_REALTIME_DIGIT, _DIVIDEND_DIGIT, _PAYOUT_DIGIT = 3, 7, 5
# A locked Earn reward can be any dividend's payout: its own digit keeps it from every one.
_LOCKED_DIGIT = 9
# The kinds whose removal leaves one (account, instrument) changed.
SINGLE_SIDED = frozenset(
    {
        "deposit",
        "withdrawal",
        "dividend",
        "payout",
        "reward",
        "locked_reward",
        "margin_interest",
        "futures_income",
        "options_bill",
        "fiat_deposit",
        "fiat_withdrawal",
        "card_purchase",
        "card_sale",
        "p2p",
        "pay",
    }
)
TWO_SIDED = frozenset(
    {
        "trade",
        "convert",
        "subscription",
        "redemption",
        "transfer",
        "dust",
        "locked_subscription",
        "locked_redemption",
        "token_swap",
        "delayed_redemption",
        "borrow",
        "repay",
        "margin_transfer",
        "margin_trade",
        "futures_transfer",
        "options_transfer",
        "cash_purchase",
        "split_pay",
    }
)


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
    # The request parameters its rows answer, when the row does not say them (a fiat order's
    # `transactionType`).
    asked: tuple[tuple[str, str], ...] = ()

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
                held = self.held[(effect.wallet, effect.asset)]
                assert held <= 0 if effect.wallet == DEBTS else held >= 0, record.describe()
            else:
                assert effect.delta > 0, record.describe()
                self.pending.append(effect)
        self.records.append(record)

    def holdings(self, wallets: Iterable[str], *, fiat: bool = False) -> list[Holding]:
        """What the wallets hold: the crypto assets, or with `fiat` the fiat only."""
        return sorted(
            h for h, q in self.held.items() if q > 0 and h[0] in wallets and (h[1] == FIAT) == fiat
        )

    def cents(self, *, at_most: Decimal | None = None) -> Decimal:
        """A fiat amount: k cents, k ≥ 1 (≤ `at_most`)."""
        top = 1_000_000 if at_most is None else int(at_most / _CENT)
        return self.draw(st.integers(1, top)) * _CENT if top >= 1 else Decimal(0)

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


def _locked_subscription(b: _Builder) -> Record | None:
    choices = b.holdings(("spot", "funding"))
    if not choices:
        return None
    wallet, asset = b.draw(st.sampled_from(choices))
    quantity = b.amount(at_most=b.held[(wallet, asset)])
    if quantity == 0:
        return None
    row = build_row(
        "earn-locked-subscriptions",
        purchaseId=str(b.id()),
        asset=asset,
        amount=_plain(quantity),
        time=b.now,
        type="NORMAL",
        sourceAccount=_EARN_SOURCE[wallet],
        amtFromSpot=_plain(quantity if wallet == "spot" else Decimal(0)),
        amtFromFunding=_plain(quantity if wallet == "funding" else Decimal(0)),
        status="SUCCESS",
    )
    effects = (Effect(b.now, wallet, asset, -quantity), Effect(b.now, "earn", asset, quantity))
    return Record("locked_subscription", (("earn-locked-subscriptions", ((b.now, row),)),), effects)


def _locked_redemption(b: _Builder) -> Record | None:
    """A locked position redeemed at maturity, to Spot."""
    choices = b.holdings(("earn",))
    if not choices:
        return None
    _, asset = b.draw(st.sampled_from(choices))
    quantity = b.amount(at_most=b.held[("earn", asset)])
    if quantity == 0:
        return None
    row = build_row(
        "earn-locked-redemptions",
        redeemId=b.id(),
        asset=asset,
        amount=_plain(quantity),
        time=b.now,
        type="MATURE",
        status="PAID",
    )
    effects = (Effect(b.now, "earn", asset, -quantity), Effect(b.now, "spot", asset, quantity))
    return Record("locked_redemption", (("earn-locked-redemptions", ((b.now, row),)),), effects)


def _locked_reward(b: _Builder) -> Record:
    """A locked Earn reward no dividend paid: income on Earn."""
    asset = b.draw(st.sampled_from(ASSETS))
    quantity = b.amount(at_most=Decimal(10), digit=_LOCKED_DIGIT)
    if quantity == 0:
        quantity = Decimal(_LOCKED_DIGIT) * _UNIT
    row = build_row(
        "earn-locked-rewards",
        positionId=b.id(),
        asset=asset,
        amount=_plain(quantity),
        type="Locked Rewards",
        time=b.now,
    )
    return Record(
        "locked_reward",
        (("earn-locked-rewards", ((b.now, row),)),),
        (Effect(b.now, "earn", asset, quantity),),
    )


def _token_swap(b: _Builder) -> Record | None:
    """A yield token subscription: a swap on Spot."""
    choices = b.holdings(("spot",))
    if not choices:
        return None
    _, given = b.draw(st.sampled_from(choices))
    received = b.draw(st.sampled_from([a for a in ASSETS if a != given]))
    given_quantity = b.amount(at_most=b.held[("spot", given)])
    if given_quantity == 0:
        return None
    received_quantity = b.amount()
    row = build_row(
        "bfusd-subscriptions",
        time=b.now,
        asset=given,
        amount=_plain(given_quantity),
        receiveAsset=received,
        receiveAmount=_plain(received_quantity),
        status="SUCCESS",
    )
    effects = (
        Effect(b.now, "spot", given, -given_quantity),
        Effect(b.now, "spot", received, received_quantity),
    )
    return Record("token_swap", (("bfusd-subscriptions", ((b.now, row),)),), effects)


def _delayed_redemption(b: _Builder) -> Record | None:
    """A yield token redemption Binance pays out hours later: listed pending at once, then
    arrived; what it received is net of its fee."""
    choices = b.holdings(("spot",))
    if not choices:
        return None
    _, given = b.draw(st.sampled_from(choices))
    received = b.draw(st.sampled_from([a for a in ASSETS if a != given]))
    given_quantity = b.amount(at_most=b.held[("spot", given)])
    if given_quantity == 0:
        return None
    net = b.amount()
    fee = b.amount(at_most=Decimal(1)) if b.draw(st.booleans()) else Decimal(0)
    arrives = b.later(HOUR_MS, 2 * DAY_MS)
    pending = build_row(
        "bfusd-redemptions",
        time=b.now,
        asset=given,
        amount=_plain(given_quantity),
        receiveAsset=received,
        receiveAmount=_plain(net),
        fee=_plain(fee),
        arrivalTime=arrives,
        status="PENDING",
    )
    arrived = {**pending, "status": "SUCCESS"}
    effects = (
        Effect(b.now, "spot", given, -given_quantity),
        Effect(arrives, "spot", received, net),
    )
    return Record(
        "delayed_redemption",
        (("bfusd-redemptions", ((b.now, pending), (arrives, arrived))),),
        effects,
    )


def collateral_of(coin: str) -> str:
    """The collateral a generated loan of the coin is pledged with: never the coin itself."""
    return "CCC" if coin != "CCC" else "AAA"


def _borrow(b: _Builder) -> Record:
    """A flexible loan borrowed onto Spot; a failed one moves nothing."""
    coin = b.draw(st.sampled_from(ASSETS))
    quantity = b.amount()
    failed = b.draw(st.integers(0, 5)) == 0
    row = build_row(
        "loan-flexible-borrows",
        loanCoin=coin,
        initialLoanAmount=_plain(quantity),
        collateralCoin=collateral_of(coin),
        initialCollateralAmount="1",
        borrowTime=b.now,
        status="FAILED" if failed else "SUCCESS",
    )
    effects = (
        ()
        if failed
        else (Effect(b.now, "spot", coin, quantity), Effect(b.now, DEBTS, coin, -quantity))
    )
    return Record("borrow", (("loan-flexible-borrows", ((b.now, row),)),), effects)


def _repay(b: _Builder) -> Record | None:
    """Part or all of a debt repaid from Spot."""
    held = dict(b.held)
    choices = sorted(
        (wallet, coin)
        for (wallet, coin), q in held.items()
        if wallet == DEBTS and q < 0 and held.get(("spot", coin), Decimal(0)) > 0
    )
    if not choices:
        return None
    _, coin = b.draw(st.sampled_from(choices))
    quantity = b.amount(at_most=min(-b.held[(DEBTS, coin)], b.held[("spot", coin)]))
    if quantity == 0:
        return None
    row = build_row(
        "loan-flexible-repays",
        loanCoin=coin,
        repayAmount=_plain(quantity),
        collateralCoin=collateral_of(coin),
        collateralReturn="0",
        repayStatus="REPAID",
        repayTime=b.now,
    )
    effects = (Effect(b.now, "spot", coin, -quantity), Effect(b.now, DEBTS, coin, quantity))
    return Record("repay", (("loan-flexible-repays", ((b.now, row),)),), effects)


def _margin_transfer(b: _Builder) -> Record | None:
    """A margin transfer between Spot and Cross Margin; at times also listed by the universal
    transfer history (rule 2: posted once), at times pending before it is confirmed."""
    choices = b.holdings(("spot", CROSS))
    if not choices:
        return None
    wallet, asset = b.draw(st.sampled_from(choices))
    into = wallet == "spot"
    quantity = b.amount(at_most=b.held[(wallet, asset)])
    if quantity == 0:
        return None
    tx_id = b.id()
    row = build_row(
        "margin-transfers",
        amount=_plain(quantity),
        asset=asset,
        status="CONFIRMED",
        timestamp=b.now,
        txId=tx_id,
        type="ROLL_IN" if into else "ROLL_OUT",
        transFrom="SPOT" if into else "CROSS_MARGIN",
        transTo="CROSS_MARGIN" if into else "SPOT",
        fromSymbol=None,
        toSymbol=None,
    )
    versions: tuple[tuple[int, Rowdict], ...] = ((b.now, row),)
    if b.draw(st.integers(0, 3)) == 0:
        versions = ((b.now, {**row, "status": "PENDING"}), (b.now + SECOND_MS, row))
    rows: list[tuple[str, tuple[tuple[int, Rowdict], ...]]] = [("margin-transfers", versions)]
    if b.draw(st.booleans()):
        universal = build_row(
            "universal-transfer",
            asset=asset,
            amount=_plain(quantity),
            type="MAIN_MARGIN" if into else "MARGIN_MAIN",
            status="CONFIRMED",
            tranId=tx_id,
            timestamp=b.now,
        )
        rows.append(("universal-transfer", ((b.now, universal),)))
    other = CROSS if into else "spot"
    effects = (Effect(b.now, wallet, asset, -quantity), Effect(b.now, other, asset, quantity))
    return Record("margin_transfer", tuple(rows), effects)


def _margin_trade(b: _Builder) -> Record | None:
    """A trade on Cross Margin, within what it holds: Binance documents no `quoteQty` for it,
    the quote side is `qty` times `price` exactly."""
    symbol, base, quote = b.draw(st.sampled_from(PAIRS))
    buyer = b.draw(st.booleans())
    price = Decimal(b.draw(st.integers(1, 2_000_000))) * _UNIT
    have = b.held[(CROSS, quote if buyer else base)]
    if have <= 0:
        return None
    if buyer:
        most = int(have / price / Decimal("0.01"))
        if most < 1:
            return None
        quantity = Decimal(b.draw(st.integers(1, min(most, 100_000)))) * Decimal("0.01")
    else:
        quantity = b.amount(at_most=have)
        if quantity == 0:
            return None
    quote_quantity = quantity * price
    fee_asset = b.draw(st.sampled_from((base, quote)))
    left = {
        base: b.held[(CROSS, base)] + (quantity if buyer else -quantity),
        quote: b.held[(CROSS, quote)] + (-quote_quantity if buyer else quote_quantity),
    }
    commission = Decimal(0)
    if b.draw(st.booleans()) and left[fee_asset] > 0:
        commission = Decimal(b.draw(st.integers(0, int(min(left[fee_asset], 1) * 10**6)))) / 10**6
    row = build_row(
        "margin-trades",
        symbol=symbol,
        id=b.id(),
        orderId=b.id(),
        price=f"{price:.4f}",
        qty=_plain(quantity),
        commission=_plain(commission),
        commissionAsset=fee_asset,
        time=b.now,
        isBuyer=buyer,
        isMaker=b.draw(st.booleans()),
        isIsolated=False,
    )
    effects = [
        Effect(b.now, CROSS, base, quantity if buyer else -quantity),
        Effect(b.now, CROSS, quote, -quote_quantity if buyer else quote_quantity),
    ]
    if commission:
        effects.append(Effect(b.now, CROSS, fee_asset, -commission))
    return Record("margin_trade", (("margin-trades", ((b.now, row),)),), tuple(effects))


def _margin_interest(b: _Builder) -> Record | None:
    """Interest charged on Cross Margin: a fee taken from what it holds."""
    choices = b.holdings((CROSS,))
    if not choices:
        return None
    _, asset = b.draw(st.sampled_from(choices))
    quantity = b.amount(at_most=b.held[(CROSS, asset)])
    if quantity == 0:
        return None
    row = build_row(
        "margin-interest",
        txId=b.id(),
        interestAccuredTime=b.now,
        asset=asset,
        rawAsset=asset,
        interest=_plain(quantity),
        type="PERIODIC",
        isolatedSymbol=None,
    )
    effect = Effect(b.now, CROSS, asset, -quantity)
    return Record("margin_interest", (("margin-interest", ((b.now, row),)),), (effect,))


def _futures_transfer(b: _Builder) -> Record | None:
    """A transfer between Spot and USDⓈ-M Futures: the universal transfer history lists it, and
    the futures income history as a `TRANSFER` row a few seconds apart (rule 2: posted once)."""
    choices = b.holdings(("spot", FUTURES))
    if not choices:
        return None
    wallet, asset = b.draw(st.sampled_from(choices))
    into = wallet == "spot"
    quantity = b.amount(at_most=b.held[(wallet, asset)])
    if quantity == 0:
        return None
    universal = build_row(
        "universal-transfer",
        asset=asset,
        amount=_plain(quantity),
        type="MAIN_UMFUTURE" if into else "UMFUTURE_MAIN",
        status="CONFIRMED",
        tranId=b.id(),
        timestamp=b.now,
    )
    listed_at = b.now + b.draw(st.integers(0, 30)) * SECOND_MS
    income = build_row(
        "um-income",
        symbol="",
        incomeType="TRANSFER",
        income=_plain(quantity if into else -quantity),
        asset=asset,
        info="TRANSFER",
        time=listed_at,
        tranId=b.id(),
        tradeId="",
    )
    rows = (
        ("universal-transfer", ((b.now, universal),)),
        ("um-income", ((listed_at, income),)),
    )
    other = FUTURES if into else "spot"
    effects = (Effect(b.now, wallet, asset, -quantity), Effect(b.now, other, asset, quantity))
    return Record("futures_transfer", rows, effects)


def _futures_income(b: _Builder) -> Record | None:
    """An income row of USDⓈ-M Futures against nothing else: a realised profit or a funding fee
    either way, or a commission paid; what is paid is within what the wallet holds."""
    kind = b.draw(st.sampled_from(("REALIZED_PNL", "FUNDING_FEE", "COMMISSION")))
    paid = kind == "COMMISSION" or b.draw(st.booleans())
    if paid:
        choices = b.holdings((FUTURES,))
        if not choices:
            return None
        _, asset = b.draw(st.sampled_from(choices))
        quantity = b.amount(at_most=b.held[(FUTURES, asset)])
    else:
        asset = b.draw(st.sampled_from(ASSETS))
        quantity = b.amount()
    if quantity == 0:
        return None
    delta = -quantity if paid else quantity
    row = build_row(
        "um-income",
        symbol="AAASTB",
        incomeType=kind,
        income=_plain(delta),
        asset=asset,
        info="",
        time=b.now,
        tranId=b.id(),
        tradeId=str(b.id()),
    )
    effect = Effect(b.now, FUTURES, asset, delta)
    return Record("futures_income", (("um-income", ((b.now, row),)),), (effect,))


def _options_transfer(b: _Builder) -> Record | None:
    """A transfer between Spot and Options: the universal transfer history lists it, and the
    options bill as a `TRANSFER` row (no symbol) a few seconds apart (rule 2: posted once)."""
    choices = b.holdings(("spot", OPTIONS))
    if not choices:
        return None
    wallet, asset = b.draw(st.sampled_from(choices))
    into = wallet == "spot"
    quantity = b.amount(at_most=b.held[(wallet, asset)])
    if quantity == 0:
        return None
    universal = build_row(
        "universal-transfer",
        asset=asset,
        amount=_plain(quantity),
        type="MAIN_OPTION" if into else "OPTION_MAIN",
        status="CONFIRMED",
        tranId=b.id(),
        timestamp=b.now,
    )
    listed_at = b.now + b.draw(st.integers(0, 30)) * SECOND_MS
    bill = build_row(
        "opt-bill",
        id=b.id(),
        asset=asset,
        amount=_plain(quantity if into else -quantity),
        type="TRANSFER",
        createDate=listed_at,
    )
    del bill["symbol"]
    rows = (
        ("universal-transfer", ((b.now, universal),)),
        ("opt-bill", ((listed_at, bill),)),
    )
    other = OPTIONS if into else "spot"
    effects = (Effect(b.now, wallet, asset, -quantity), Effect(b.now, other, asset, quantity))
    return Record("options_transfer", rows, effects)


def _options_bill(b: _Builder) -> Record | None:
    """A bill row of Options against nothing else: a premium or an exercise's profit either
    way, or a fee paid; what is paid is within what the wallet holds."""
    kind = b.draw(st.sampled_from(("CONTRACT", "EXERCISE_PNL", "FEE", "EXERCISE_FEE")))
    paid = kind in ("FEE", "EXERCISE_FEE") or b.draw(st.booleans())
    if paid:
        choices = b.holdings((OPTIONS,))
        if not choices:
            return None
        _, asset = b.draw(st.sampled_from(choices))
        quantity = b.amount(at_most=b.held[(OPTIONS, asset)])
    else:
        asset = b.draw(st.sampled_from(ASSETS))
        quantity = b.amount()
    if quantity == 0:
        return None
    delta = -quantity if paid else quantity
    row = build_row(
        "opt-bill",
        id=b.id(),
        asset=asset,
        amount=_plain(delta),
        symbol="AAA-261231-100-C",
        type=kind,
        createDate=b.now,
    )
    effect = Effect(b.now, OPTIONS, asset, delta)
    return Record("options_bill", (("opt-bill", ((b.now, row),)),), (effect,))


_FIAT_IN, _FIAT_OUT = "0", "1"


def _fiat_order(b: _Builder, quantity: Decimal, fee: Decimal, **fields: ApiValue) -> Rowdict:
    return build_row(
        "fiat-orders",
        orderNo=f"fo-{b.id()}",
        fiatCurrency=FIAT,
        indicatedAmount=_plain(quantity + fee),
        amount=_plain(quantity),
        totalFee=_plain(fee),
        method="BankAccount",
        **fields,
    )


def _fiat_deposit(b: _Builder) -> Record:
    """A deposit by bank credited on Spot after its fee, some processing first."""
    quantity = b.cents()
    fee = b.cents(at_most=quantity) if b.draw(st.booleans()) else Decimal(0)
    pending = b.draw(st.booleans())
    credited_at = b.later(MINUTE_MS, 2 * DAY_MS) if pending else b.now
    base = _fiat_order(
        b, quantity, fee, status="Successful", createTime=b.now, updateTime=credited_at
    )
    versions: list[tuple[int, Rowdict]] = []
    if pending:
        versions.append((b.now, {**base, "status": "Processing", "updateTime": b.now}))
    versions.append((credited_at, base))
    return Record(
        "fiat_deposit",
        (("fiat-orders", tuple(versions)),),
        (Effect(credited_at, "spot", FIAT, quantity),),
        asked=(("transactionType", _FIAT_IN),),
    )


def _fiat_withdrawal(b: _Builder) -> Record | None:
    """A withdrawal by bank from Spot: `amount` to the bank and the fee besides."""
    if not b.holdings(("spot",), fiat=True):
        return None
    total = b.held[("spot", FIAT)]
    quantity = b.cents(at_most=total)
    fee = b.cents(at_most=total - quantity) if b.draw(st.booleans()) else Decimal(0)
    if quantity == 0:
        return None
    row = _fiat_order(b, quantity, fee, status="Successful", createTime=b.now, updateTime=b.now)
    return Record(
        "fiat_withdrawal",
        (("fiat-orders", ((b.now, row),)),),
        (Effect(b.now, "spot", FIAT, -(quantity + fee)),),
        asked=(("transactionType", _FIAT_OUT),),
    )


def _card_row(
    b: _Builder, source: Decimal, crypto: str, obtained: Decimal, fee: Decimal, method: str | None
) -> Rowdict:
    return build_row(
        "fiat-payments",
        orderNo=f"fp-{b.id()}",
        sourceAmount=_plain(source),
        fiatCurrency=FIAT,
        obtainAmount=_plain(obtained),
        cryptoCurrency=crypto,
        totalFee=_plain(fee),
        price="1",
        status="Completed",
        paymentMethod=method,
        createTime=b.now,
        updateTime=b.now,
    )


def _card_purchase(b: _Builder) -> Record:
    """Crypto bought on Spot with a card: the fiat arrives and is spent at once."""
    crypto = b.draw(st.sampled_from(ASSETS))
    source = b.cents() + _CENT
    fee = b.cents(at_most=source - _CENT) if b.draw(st.booleans()) else Decimal(0)
    obtained = b.amount()
    row = _card_row(b, source, crypto, obtained, fee, "Credit Card")
    return Record(
        "card_purchase",
        (("fiat-payments", ((b.now, row),)),),
        (Effect(b.now, "spot", crypto, obtained),),
        asked=(("transactionType", _FIAT_IN),),
    )


def _cash_purchase(b: _Builder) -> Record | None:
    """Crypto bought on Spot from the cash balance: the fiat leaves Spot."""
    if not b.holdings(("spot",), fiat=True):
        return None
    source = b.cents(at_most=b.held[("spot", FIAT)])
    if source <= _CENT:
        return None
    fee = b.cents(at_most=source - _CENT) if b.draw(st.booleans()) else Decimal(0)
    crypto = b.draw(st.sampled_from(ASSETS))
    obtained = b.amount()
    row = _card_row(b, source, crypto, obtained, fee, "Cash Balance")
    effects = (Effect(b.now, "spot", FIAT, -source), Effect(b.now, "spot", crypto, obtained))
    return Record(
        "cash_purchase",
        (("fiat-payments", ((b.now, row),)),),
        effects,
        asked=(("transactionType", _FIAT_IN),),
    )


def _card_sale(b: _Builder) -> Record | None:
    """Crypto sold on Spot to a card: the fiat it buys leaves at once."""
    choices = b.holdings(("spot",))
    if not choices:
        return None
    _, crypto = b.draw(st.sampled_from(choices))
    sold = b.amount(at_most=b.held[("spot", crypto)])
    if sold == 0:
        return None
    source = b.cents()
    fee = b.cents() if b.draw(st.booleans()) else Decimal(0)
    row = _card_row(b, source, crypto, sold, fee, None)
    return Record(
        "card_sale",
        (("fiat-payments", ((b.now, row),)),),
        (Effect(b.now, "spot", crypto, -sold),),
        asked=(("transactionType", _FIAT_OUT),),
    )


def _p2p(b: _Builder) -> Record | None:
    """A P2P order on Funding paid by bank: a buy, or a sell of what Funding holds; the total is
    the amount at a whole unit price exactly, the commission in the crypto."""
    sell = b.draw(st.booleans())
    if sell:
        choices = b.holdings(("funding",))
        if not choices:
            return None
        _, crypto = b.draw(st.sampled_from(choices))
        held = b.held[("funding", crypto)]
        quantity = b.cents(at_most=held)
        commission = b.amount(at_most=held - quantity) if b.draw(st.booleans()) else Decimal(0)
        if quantity == 0:
            return None
        delta = -(quantity + commission)
    else:
        crypto = b.draw(st.sampled_from(ASSETS))
        quantity = b.cents()
        commission = b.amount(at_most=quantity - _UNIT) if b.draw(st.booleans()) else Decimal(0)
        delta = quantity - commission
    price = Decimal(b.draw(st.integers(1, 100)))
    row = build_row(
        "c2c",
        orderNumber=f"p2p-{b.id()}",
        tradeType="SELL" if sell else "BUY",
        asset=crypto,
        fiat=FIAT,
        amount=_plain(quantity),
        totalPrice=_plain(quantity * price),
        unitPrice=_plain(price),
        orderStatus="COMPLETED",
        commission=_plain(commission),
        createTime=b.now,
    )
    return Record("p2p", (("c2c", ((b.now, row),)),), (Effect(b.now, "funding", crypto, delta),))


def _pay(b: _Builder) -> Record | None:
    """A Pay payment from Funding or Spot of what it holds, or a receipt there."""
    wallet = b.draw(st.sampled_from(("funding", "spot")))
    paid = b.draw(st.booleans())
    if paid:
        choices = b.holdings((wallet,))
        if not choices:
            return None
        _, asset = b.draw(st.sampled_from(choices))
        quantity = b.amount(at_most=b.held[(wallet, asset)])
        if quantity == 0:
            return None
        cost = {"2" if wallet == "spot" else "1": _plain(quantity)}
        # Its costs name its wallet, or are left out (the row's `walletType` says it).
        costs: ApiValue = cost if b.draw(st.booleans()) else {}
        details: list[ApiValue] = [
            {"currency": asset, "amount": _plain(quantity), "walletAssetCost": costs}
        ]
    else:
        asset = b.draw(st.sampled_from(ASSETS))
        quantity = b.amount()
        details = []
    delta = -quantity if paid else quantity
    row = build_row(
        "pay",
        transactionId=f"pay-{b.id()}",
        orderType="PAY" if paid else "C2C",
        transactionTime=b.now,
        amount=_plain(delta),
        currency=asset,
        walletType=2 if wallet == "spot" else 1,
        fundsDetail=details,
    )
    return Record("pay", (("pay", ((b.now, row),)),), (Effect(b.now, wallet, asset, delta),))


def _split_pay(b: _Builder) -> Record | None:
    """A combined Pay payment of one asset, part from Funding and part from Spot
    (`walletAssetCost` by wallet type: 1 Funding, 2 Spot)."""
    on_funding = {asset for _, asset in b.holdings(("funding",))}
    choices = sorted(asset for _, asset in b.holdings(("spot",)) if asset in on_funding)
    if not choices:
        return None
    asset = b.draw(st.sampled_from(choices))
    from_funding = b.amount(at_most=b.held[("funding", asset)])
    from_spot = b.amount(at_most=b.held[("spot", asset)])
    if from_funding == 0 or from_spot == 0:
        return None
    total = from_funding + from_spot
    row = build_row(
        "pay",
        transactionId=f"pay-{b.id()}",
        orderType="PAY",
        transactionTime=b.now,
        amount=_plain(-total),
        currency=asset,
        walletType=1,
        walletTypes=[1, 2],
        fundsDetail=[
            {
                "currency": asset,
                "amount": _plain(total),
                "walletAssetCost": {"1": _plain(from_funding), "2": _plain(from_spot)},
            }
        ],
    )
    effects = (
        Effect(b.now, "funding", asset, -from_funding),
        Effect(b.now, "spot", asset, -from_spot),
    )
    return Record("split_pay", (("pay", ((b.now, row),)),), effects)


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
    "locked_subscription": _locked_subscription,
    "locked_redemption": _locked_redemption,
    "locked_reward": _locked_reward,
    "token_swap": _token_swap,
    "delayed_redemption": _delayed_redemption,
    "borrow": _borrow,
    "repay": _repay,
    "margin_transfer": _margin_transfer,
    "margin_trade": _margin_trade,
    "margin_interest": _margin_interest,
    "futures_transfer": _futures_transfer,
    "futures_income": _futures_income,
    "options_transfer": _options_transfer,
    "options_bill": _options_bill,
    "fiat_deposit": _fiat_deposit,
    "fiat_withdrawal": _fiat_withdrawal,
    "card_purchase": _card_purchase,
    "cash_purchase": _cash_purchase,
    "card_sale": _card_sale,
    "p2p": _p2p,
    "pay": _pay,
    "split_pay": _split_pay,
}


@st.composite
def histories(draw: st.DrawFn, *, min_records: int = 1, max_records: int = 10) -> History:
    start = START_MS + draw(st.integers(0, 86_399)) * 1000
    initial: dict[Holding, Decimal] = {}
    for wallet in WALLET_KEYS:
        for asset in ASSETS:
            if draw(st.integers(0, 5)) == 0:
                quantity = Decimal(draw(st.integers(1, 1_000_000))) * _UNIT
                initial[(wallet, asset)] = -quantity if wallet == DEBTS else quantity
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
