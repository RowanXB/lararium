"""Properties of the family rule in the pair relation (account family identifier design §5.3),
over in-memory records through pairs._registry_edges.

A record naming an account family pairs only within the family: with a record naming the
family back, on one of the accounts it names and naming its account (MUTUAL when each is
identified to the other's account, else ONE_SIDED); or with a record there naming nobody,
when both are timed and their instants are at most the near window apart (ONE_SIDED). No
other rule gives it an edge (item 2). A record naming nobody that is a family move's
counterpart so (item 3) gets no edge from a record naming a shared payee and none through
its account's funding alone; its other edges are as without the family moves."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from hypothesis import event, example, given, settings
from hypothesis import strategies as st

from lararium.modules.finance.transfers.pairs import (
    MUTUAL,
    ONE_SIDED,
    Known,
    Pairable,
    _Decisions,
    _Edges,
    _family_pair,
    _funded_only,
    _names_nobody,
    _registry_edges,
    counterpart_bounds,
)
from lararium.modules.finance.transfers.settings import DEFAULTS

_NO_DECISIONS = _Decisions(frozenset(), frozenset(), ())
_ALL_KNOWN = Known(frozenset(), {})  # no manual account: the coverage gate takes every pair
GBP = UUID(int=1)
NEAR = DEFAULTS.near_window
# Invest, Stocks ISA and Cash ISA, of the family (all three, or the first two); Monzo, of none.
INVEST, STOCKS, CASH, MONZO = UUID(int=20), UUID(int=21), UUID(int=22), UUID(int=23)
ACCOUNTS = (INVEST, STOCKS, CASH, MONZO)
BASE = datetime(2026, 11, 26, 12, tzinfo=UTC)
# How far apart two records are, in seconds: around the near window's edge (120 s) often,
# else anything within ten minutes either way.
_SECONDS = st.sampled_from((-121, -120, -30, 0, 18, 119, 120, 121)) | st.integers(-600, 600)
# How many days apart: mostly none, else up to two either way (the window is a day before
# a payment to five after it).
_DAYS = st.sampled_from((0, 0, 0, -2, -1, 1, 2))


def _at(draw: st.DrawFn, start: datetime) -> datetime:
    return start + timedelta(days=draw(_DAYS), seconds=draw(_SECONDS))


_NAMES = ("family", "family", "payee", "account", "nobody", "nobody")
# A second payer to a receipt: most often a payment to the shared payee, which a receipt
# naming nobody near a family move must give way to (§5.3 item 3).
_COMPETING = ("payee", "payee", "account", "nobody", "family")


@st.composite
def _record(
    draw: st.DrawFn,
    family: frozenset[UUID],
    account: UUID,
    quantity: str,
    at: datetime,
    naming: tuple[str, ...] = _NAMES,
) -> Pairable:
    """A record naming the family (on a family account: §5.2's holder or set), a shared
    payee all three Trading 212 accounts hold (on an account that leaves two or more of
    them), an own account, or nobody; marked a bank transfer, funded by bank transfer and
    timed or not, as drawn."""
    names = draw(st.sampled_from(naming))
    holder: UUID | None = None
    payee_holders: frozenset[UUID] = frozenset()
    if names == "family" and account in family:
        others = family - {account}
        if len(others) == 1:
            [holder] = others
        else:
            payee_holders = others
    elif names == "payee" and len({INVEST, STOCKS, CASH} - {account}) >= 2:
        payee_holders = frozenset({INVEST, STOCKS, CASH} - {account})
    elif names == "account":
        holder = draw(st.sampled_from([x for x in ACCOUNTS if x != account]))
    else:
        names = "nobody"
    return Pairable(
        id=uuid4(),
        source=(draw(st.sampled_from(("connection", "import"))), str(uuid4())),
        account_id=account,
        instrument_id=GBP,
        quantity=Decimal(quantity),
        occurred_at=at,
        booking_date=at.date(),
        holder=holder,
        bank_transfer=draw(st.booleans()),
        names_account=names != "nobody",
        payee_holders=payee_holders,
        family=names == "family",
        funded_by_transfer=draw(st.booleans()),
        timed=draw(st.sampled_from((True, True, False))),
    )


@st.composite
def _case(draw: st.DrawFn) -> tuple[frozenset[UUID], list[Pairable]]:
    """The family's accounts (three, or two), and two to five records over INVEST, STOCKS,
    CASH and MONZO of 100 or 200 either way: one or two moves, each a payment and a receipt
    of its amount on two accounts, the receipt _DAYS and _SECONDS from the payment (the
    first move often between two family accounts, one side naming the family, the other
    nobody or the family), the first maybe with a second payer of its amount, and up to the
    rest of noise, all within two days and ten minutes or so of BASE."""
    family = draw(st.sampled_from((frozenset({INVEST, STOCKS, CASH}), frozenset({INVEST, STOCKS}))))
    records = []
    moves = draw(st.integers(min_value=1, max_value=2))
    for _ in range(moves):
        amount = draw(st.sampled_from(("100", "200")))
        sent = _at(draw, BASE)
        if not records and draw(st.booleans()):
            # A family move, its receipt naming nobody or the family, the same day.
            payer, payee = draw(st.permutations(sorted(family)))[:2]
            received = sent + timedelta(seconds=draw(_SECONDS))
            naming: tuple[tuple[str, ...], tuple[str, ...]] = (("family",), ("nobody", "family"))
        else:
            payer, payee = draw(st.permutations(ACCOUNTS))[:2]
            received = _at(draw, sent)
            naming = (_NAMES, _NAMES)
        if draw(st.booleans()):  # either way round
            payer, payee, naming = payee, payer, (naming[1], naming[0])
        records.append(draw(_record(family, payer, f"-{amount}", sent, naming[0])))
        records.append(draw(_record(family, payee, amount, received, naming[1])))
        if len(records) == 2 and draw(st.sampled_from((True, True, False))):
            other = draw(st.sampled_from([x for x in ACCOUNTS if x != payee]))
            at = _at(draw, received)
            records.append(draw(_record(family, other, f"-{amount}", at, _COMPETING)))
    for _ in range(draw(st.integers(min_value=0, max_value=5 - len(records)))):
        account = draw(st.sampled_from(ACCOUNTS))
        quantity = draw(st.sampled_from(("-100", "100", "-200", "200")))
        records.append(draw(_record(family, account, quantity, _at(draw, BASE))))
    return family, records


def _edges(edges: _Edges) -> dict[frozenset[UUID], int]:
    """Each pair once, with its tier."""
    return {
        frozenset((r, c)): tier for r, found in edges.tiers.items() for c, tier in found.items()
    }


def _same_amount(records: list[Pairable]) -> list[tuple[Pairable, Pairable]]:
    """Each sender with each receiver in its instrument of its opposite amount, booked in
    its window, on another account: the pairs _registry_edges asks registry_tier about."""
    found = []
    for s in records:
        if not s.sends:
            continue
        earliest, latest = counterpart_bounds(s, DEFAULTS)
        for c in records:
            if (
                not c.sends
                and c.account_id != s.account_id
                and c.instrument_id == s.instrument_id
                and c.quantity == -s.quantity
                and earliest <= c.booking_date <= latest
            ):
                found.append((s, c))
    return found


def _familied(records: list[Pairable]) -> set[UUID]:
    """The records naming nobody that are a family move's counterpart by §5.3 item 2."""
    return {
        x.id
        for s, c in _same_amount(records)
        for x, y in ((s, c), (c, s))
        if not x.family and _family_pair(y, x, NEAR)
    }


@settings(max_examples=300, deadline=None)
@given(case=_case())
def test_a_family_record_pairs_only_inside_its_family(
    case: tuple[frozenset[UUID], list[Pairable]],
) -> None:
    """Every same-amount edge touching a family record is between two family accounts, and
    its other side, if it names no family, names nobody, is timed and is at most the near
    window from it. Exactly, the edges are §5.3 item 2's: two family moves each naming the
    other's account (MUTUAL when each is identified to it, else ONE_SIDED), or a family
    move and a record naming nobody on an account it names, both timed and at most the
    near window apart (ONE_SIDED)."""
    family, records = case
    same, _, _ = _registry_edges(records, DEFAULTS, _NO_DECISIONS, _ALL_KNOWN)
    given_edges = {
        pair: tier
        for pair, tier in _edges(same).items()
        if any(x.family for x in records if x.id in pair)
    }
    expected: dict[frozenset[UUID], int] = {}
    for s, c in _same_amount(records):
        if not (s.family or c.family):
            continue
        pair = frozenset((s.id, c.id))
        if s.family and c.family:
            if s.account_id in (c.payee_holders | {c.holder}) and c.account_id in (
                s.payee_holders | {s.holder}
            ):
                mutual = s.holder == c.account_id and c.holder == s.account_id
                expected[pair] = MUTUAL if mutual else ONE_SIDED
            continue
        x, other = (s, c) if s.family else (c, s)
        if (
            other.account_id in (x.payee_holders | {x.holder})
            and other.holder is None
            and not other.payee_holders
            and not other.names_account
            and x.timed
            and other.timed
            and abs(x.occurred_at - other.occurred_at) <= NEAR
        ):
            expected[pair] = ONE_SIDED
    by_id = {x.id: x for x in records}
    for pair in given_edges:
        a, b = (by_id[x] for x in pair)
        assert {a.account_id, b.account_id} <= family, (a, b)
        for x, other in ((a, b), (b, a)):
            if x.family and not other.family:
                assert _names_nobody(other) and x.timed and other.timed, (x, other)
                assert abs(x.occurred_at - other.occurred_at) <= NEAR, (x, other)
    assert given_edges == expected
    event("a family edge" if given_edges else "no family edge")


def _p(account: UUID, quantity: str, seconds: int, **kw: Any) -> Pairable:
    """A timed connector's record `seconds` after BASE, naming nobody unless `kw` says."""
    at = BASE + timedelta(seconds=seconds)
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "source": ("connection", str(uuid4())),
        "instrument_id": GBP,
        "occurred_at": at,
        "booking_date": at.date(),
        "holder": None,
        "bank_transfer": False,
        "timed": True,
    }
    return Pairable(account_id=account, quantity=Decimal(quantity), **(defaults | kw))


_THREE = frozenset({INVEST, STOCKS, CASH})
# A move out of Invest naming the family of three.
_MOVE = _p(INVEST, "-100", 0, family=True, names_account=True, payee_holders=_THREE - {INVEST})


# The real shape: the Cash ISA row of the move (a bank transfer naming nobody, 18 s after
# it), and a payment from Monzo to the shared payee the same day.
@example(
    case=(
        _THREE,
        [
            _MOVE,
            _p(CASH, "100", 18, bank_transfer=True, source=("import", "k")),
            _p(MONZO, "-100", -3600, bank_transfer=True, names_account=True, payee_holders=_THREE),
        ],
    )
)
# The row is a deposit on a funded account, and a payment from Monzo names its account.
@example(
    case=(
        _THREE,
        [
            _MOVE,
            _p(CASH, "100", 18, funded_by_transfer=True),
            _p(MONZO, "-100", -3600, bank_transfer=True, names_account=True, holder=CASH),
        ],
    )
)
@settings(max_examples=300, deadline=None)
@given(case=_case())
def test_family_evidence_comes_first(case: tuple[frozenset[UUID], list[Pairable]]) -> None:
    """§5.3 item 3, exactly: the edges between two records naming no family are those they
    have without the family moves, less those where one is a family move's counterpart
    (_familied) and the other names a shared payee or it takes the edge through its
    account's funding alone; and those keep their tiers."""
    _, records = case
    familied = _familied(records)
    same, _, _ = _registry_edges(records, DEFAULTS, _NO_DECISIONS, _ALL_KNOWN)
    plain = [x for x in records if not x.family]
    alone, _, _ = _registry_edges(plain, DEFAULTS, _NO_DECISIONS, _ALL_KNOWN)
    by_id = {x.id: x for x in records}

    def dropped(pair: frozenset[UUID]) -> bool:
        a, b = (by_id[x] for x in pair)
        return any(
            x.id in familied and (_funded_only(x) or bool(y.payee_holders))
            for x, y in ((a, b), (b, a))
        )

    with_family = {
        pair: tier for pair, tier in _edges(same).items() if not any(by_id[x].family for x in pair)
    }
    without = _edges(alone)
    assert with_family == {pair: tier for pair, tier in without.items() if not dropped(pair)}
    for x in familied:
        assert _names_nobody(by_id[x])
    event("an edge gave way" if len(with_family) < len(without) else "none gave way")


@settings(max_examples=300, deadline=None)
@given(case=_case())
def test_time_only_narrows_family_pairs(case: tuple[frozenset[UUID], list[Pairable]]) -> None:
    """With every record's `timed` off, the edges touching a family record are exactly
    those between two family records, as with it on: time decides only whether a family
    move pairs with a record naming nobody."""
    _, records = case
    untimed = [replace(x, timed=False) for x in records]
    on, _, _ = _registry_edges(records, DEFAULTS, _NO_DECISIONS, _ALL_KNOWN)
    off, _, _ = _registry_edges(untimed, DEFAULTS, _NO_DECISIONS, _ALL_KNOWN)
    family = {x.id for x in records if x.family}

    def touching(edges: _Edges) -> dict[frozenset[UUID], int]:
        return {pair: tier for pair, tier in _edges(edges).items() if pair & family}

    assert touching(off) == {pair: tier for pair, tier in touching(on).items() if pair <= family}
