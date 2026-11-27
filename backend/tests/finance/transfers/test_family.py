"""A record naming an account family (account family identifier design §5.2): its row in
eligible_records resolves to the family's other accounts, as identifiers.named_accounts does.
It pairs only within the family (§5.3): with a record naming the family back, or with one
naming nobody when both are timed within the near window; and such a record naming nobody
is that move's, not a shared payee's or its account's funding's, unless its source says it
went outside the family (§5.9). Several candidates split by the only one within the near
window (§5.10); a group alike on one side pairs in time order (§5.7)."""

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.connections import create_connection
from lararium.modules.finance.accounts import AccountRecord, create_account, set_type_key
from lararium.modules.finance.identifiers import family_identifier, named_accounts
from lararium.modules.finance.system_accounts import system_account_ids
from lararium.modules.finance.transfers import list_transfers, register_holders_and_rematch
from lararium.modules.finance.transfers.contra import eligible_records
from lararium.modules.finance.transfers.pairs import (
    MUTUAL,
    ONE_SIDED,
    Known,
    Pairable,
    PairRelation,
    Tier,
    _Decisions,
    _registry_edges,
    _several,
    _twins,
    card_tier,
    registry_tier,
)
from lararium.modules.finance.transfers.settings import DEFAULTS

from .conftest import (
    WHEN,
    Connected,
    ConnectorTx,
    Heal,
    MakeConnected,
    Run,
    StatementRow,
    assert_consistent,
)

FAMILY_TYPES = ["trading212.invest", "trading212.stocks_isa", "trading212.cash_isa"]

type MakeFamily = Callable[[int], Awaitable[tuple[Connected, list[AccountRecord]]]]


@pytest.fixture
def family_accounts(run: Run) -> MakeFamily:
    """A connected Trading 212 Invest account and n - 1 manual accounts of the family's
    other types."""

    async def _make(n: int) -> tuple[Connected, list[AccountRecord]]:
        connection = await run(
            create_connection, connector="trading212", display_name="T212", config={}
        )
        first = await run(
            create_account,
            name="Invest",
            ledger_type="asset",
            subtype="broker",
            balance_mode="transactions",
            source_connection_id=connection.id,
            type_key=FAMILY_TYPES[0],
        )
        others = [
            await run(
                create_account,
                name=f"Other {key}",
                ledger_type="asset",
                subtype="broker",
                balance_mode="transactions",
                type_key=key,
            )
            for key in FAMILY_TYPES[1:n]
        ]
        return Connected(connection.id, first), others

    return _make


async def _eligible_row(session: AsyncSession, transaction_id: UUID) -> Any:
    e = eligible_records(await system_account_ids(session))
    return (await session.execute(select(e).where(e.c.transaction_id == transaction_id))).one()


@pytest.mark.parametrize("n", [1, 2, 3])
async def test_eligible_row_of_a_family_move(
    session: AsyncSession,
    run: Run,
    family_accounts: MakeFamily,
    connector_tx: ConnectorTx,
    n: int,
) -> None:
    first, others = await family_accounts(n)
    identifier_id = await run(family_identifier, "trading212")
    sent = await connector_tx(first, "-100", identifier_id=identifier_id)

    row = await _eligible_row(session, sent.id)

    holder, holders = await named_accounts(session, identifier_id, first.account.id)
    assert holders == {o.id for o in others} - {holder}
    assert row.family and row.names_account
    assert row.holder == holder
    assert (row.payee_holders is None and not holders) or set(row.payee_holders) == holders


async def test_a_retyped_account_still_excludes_itself(
    session: AsyncSession,
    run: Run,
    family_accounts: MakeFamily,
    connector_tx: ConnectorTx,
) -> None:
    first, others = await family_accounts(3)
    identifier_id = await run(family_identifier, "trading212")
    sent = await connector_tx(first, "-100", identifier_id=identifier_id)
    await run(set_type_key, first.account.id, None)

    row = await _eligible_row(session, sent.id)

    # Retyped, the account is no longer of the family: the two others are the set.
    assert row.holder is None
    assert set(row.payee_holders) == {o.id for o in others}
    assert first.account.id not in row.payee_holders


# The tier on its own (account family identifier design §5.3).

# Invest, Stocks ISA and Cash ISA, of one family; Monzo, of none.
INVEST, STOCKS, CASH, MONZO = UUID(int=20), UUID(int=21), UUID(int=22), UUID(int=23)
T0 = WHEN
NEAR = DEFAULTS.near_window


def _pairable(account: UUID, quantity: str, **kw: Any) -> Pairable:
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "source": ("connection", f"{account}:{quantity}:{uuid4()}"),
        "instrument_id": UUID(int=1),
        "occurred_at": T0,
        "booking_date": T0.date(),
        "holder": None,
        "bank_transfer": False,
        "timed": True,
    }
    return Pairable(account_id=account, quantity=Decimal(quantity), **(defaults | kw))


def _move(account: UUID, quantity: str, family: set[UUID], **kw: Any) -> Pairable:
    """A record naming the family `family`, on one of its accounts (§5.2): identified to
    the one other account, or to the set of two or more."""
    others = family - {account}
    named: dict[str, Any] = (
        {"holder": next(iter(others))} if len(others) == 1 else {"payee_holders": frozenset(others)}
    )
    return _pairable(account, quantity, family=True, names_account=True, **(named | kw))


THREE = {INVEST, STOCKS, CASH}


def test_two_family_moves_pair_within_the_family() -> None:
    sent = _move(INVEST, "-100", THREE)
    received = _move(STOCKS, "100", THREE)
    assert registry_tier(sent, received) == registry_tier(received, sent) == ONE_SIDED
    # A family of two: each is identified to the other's account.
    pair = _move(INVEST, "-100", {INVEST, STOCKS}), _move(STOCKS, "100", {INVEST, STOCKS})
    assert registry_tier(*pair) == registry_tier(*reversed(pair)) == MUTUAL
    # A family move on an account outside the set.
    elsewhere = _move(MONZO, "100", {MONZO, STOCKS, CASH})
    assert registry_tier(sent, elsewhere) is None and registry_tier(elsewhere, sent) is None
    # However it is marked, a family move is no record naming nobody.
    for marked in (
        replace(received, bank_transfer=True),
        replace(received, funded_by_transfer=True),
        # Untimed, or far apart: time decides nothing between two family moves.
        replace(received, timed=False),
        replace(received, occurred_at=T0 + timedelta(days=1)),
    ):
        assert registry_tier(sent, marked) == registry_tier(marked, sent) == ONE_SIDED, marked


def test_a_family_move_takes_a_record_naming_nobody_only_within_the_near_window() -> None:
    sent = _move(INVEST, "-100", THREE)
    for pairs, other in (
        (True, _pairable(CASH, "100", bank_transfer=True, occurred_at=T0 + NEAR)),
        (
            False,
            _pairable(
                CASH, "100", bank_transfer=True, occurred_at=T0 + NEAR + timedelta(seconds=1)
            ),
        ),
        (False, _pairable(CASH, "100", bank_transfer=True, timed=False)),
        (
            True,
            _pairable(
                STOCKS, "100", funded_by_transfer=True, occurred_at=T0 + timedelta(seconds=30)
            ),
        ),
        (True, _pairable(STOCKS, "100", occurred_at=T0 - NEAR, source=("import", "k"))),
        # It names someone: an account, a bank account, a card merchant.
        (False, _pairable(CASH, "100", bank_transfer=True, holder=MONZO, names_account=True)),
        (False, _pairable(CASH, "100", bank_transfer=True, names_account=True)),
        (False, _pairable(CASH, "100", holders=frozenset({MONZO}))),
        (False, _pairable(MONZO, "100", bank_transfer=True)),  # outside the family
    ):
        expected = ONE_SIDED if pairs else None
        assert registry_tier(sent, other) == registry_tier(other, sent) == expected, other
    # The family move must be timed too.
    untimed = replace(sent, timed=False)
    assert registry_tier(untimed, _pairable(CASH, "100", bank_transfer=True)) is None
    # With one other account, the same: a record there naming nobody, near it.
    two = _move(INVEST, "-100", {INVEST, CASH})
    assert registry_tier(two, _pairable(CASH, "100", bank_transfer=True)) == ONE_SIDED
    far = _pairable(CASH, "100", bank_transfer=True, occurred_at=T0 + timedelta(hours=1))
    assert registry_tier(two, far) is None and registry_tier(far, two) is None


def test_a_family_move_is_never_a_payee_or_card_record_s() -> None:
    payee = _pairable(
        MONZO, "-100", bank_transfer=True, names_account=True, payee_holders=frozenset(THREE)
    )
    for received in (
        _move(STOCKS, "100", THREE),
        _move(STOCKS, "100", THREE, bank_transfer=True),
        _move(STOCKS, "100", THREE, funded_by_transfer=True),
    ):
        assert registry_tier(payee, received) is None, received
        assert registry_tier(received, payee) is None, received
    card = _pairable(MONZO, "-100", holders=frozenset({STOCKS}))
    received = _move(STOCKS, "100", THREE)
    assert card_tier(card, received, NEAR) is None and card_tier(received, card, NEAR) is None


_NO_DECISIONS = _Decisions(frozenset(), frozenset(), ())
_ALL_KNOWN = Known(frozenset(), {})


def test_family_evidence_comes_first() -> None:
    """The real shape: a Cash ISA row of an internal move, a bank transfer naming nobody,
    within the near window of its TRANSFER, and a same-amount payment from Monzo to the
    shared payee the same day. The row is the TRANSFER's alone; the payment keeps its own
    deposit, two minutes or more from any family move."""
    sent = _move(INVEST, "-100", THREE)
    row = _pairable(CASH, "100", bank_transfer=True, occurred_at=T0 + timedelta(seconds=18))
    payment = _pairable(
        MONZO,
        "-100",
        bank_transfer=True,
        names_account=True,
        payee_holders=frozenset(THREE),
        occurred_at=T0 - timedelta(hours=3),
    )
    deposit = _pairable(
        STOCKS, "100", funded_by_transfer=True, occurred_at=T0 - timedelta(hours=3, seconds=-5)
    )
    same, _, _ = _registry_edges([sent, row, payment, deposit], DEFAULTS, _NO_DECISIONS, _ALL_KNOWN)
    assert same.tiers[row.id] == {sent.id: ONE_SIDED}
    assert same.tiers[payment.id] == {deposit.id: ONE_SIDED}
    # Far from the family move, the row is the payment's candidate again.
    far = replace(row, occurred_at=T0 + NEAR + timedelta(seconds=1))
    same, _, _ = _registry_edges([sent, far, payment], DEFAULTS, _NO_DECISIONS, _ALL_KNOWN)
    assert same.tiers[far.id] == {payment.id: ONE_SIDED}
    # A row it takes through its funding alone gives way too, and only to a family move:
    # an identified record naming its account keeps it.
    funded = replace(row, bank_transfer=False, funded_by_transfer=True)
    named = _pairable(MONZO, "-100", bank_transfer=True, holder=CASH, names_account=True)
    same, _, _ = _registry_edges([sent, funded, named], DEFAULTS, _NO_DECISIONS, _ALL_KNOWN)
    assert same.tiers[funded.id] == {sent.id: ONE_SIDED}
    same, _, _ = _registry_edges([sent, row, named], DEFAULTS, _NO_DECISIONS, _ALL_KNOWN)
    assert same.tiers[row.id] == {sent.id: ONE_SIDED, named.id: ONE_SIDED}


def test_an_outside_record_is_no_family_counterpart() -> None:
    """A Cash ISA row whose Notes give a transaction id (§5.9) went outside the family: near
    a family move or not, it is never its counterpart."""
    sent = _move(INVEST, "-100", THREE)
    near = T0 + timedelta(seconds=30)
    outside = _pairable(
        CASH, "100", bank_transfer=True, timed=True, outside_family=True, occurred_at=near
    )
    assert registry_tier(sent, outside) is None and registry_tier(outside, sent) is None
    inside = replace(outside, outside_family=False)
    assert registry_tier(sent, inside) == registry_tier(inside, sent) == ONE_SIDED
    # So it is no family move's first: the shared payee's payment keeps its edge to it.
    payment = _pairable(
        MONZO,
        "-100",
        bank_transfer=True,
        names_account=True,
        payee_holders=frozenset(THREE),
        occurred_at=T0 - timedelta(hours=3),
    )
    same, _, _ = _registry_edges([sent, outside, payment], DEFAULTS, _NO_DECISIONS, _ALL_KNOWN)
    assert same.tiers[outside.id] == {payment.id: ONE_SIDED}


def test_the_owner_s_decisions_win_over_family_evidence() -> None:
    """A row the owner decided never pairs with the family move near it is not that move's
    first: it keeps its shared payee's and its funding's edges (controller's ruling,
    2026-11-27)."""
    sent = _move(INVEST, "-100", THREE)
    row = _pairable(
        CASH, "100", bank_transfer=True, occurred_at=T0 + timedelta(seconds=18), source=("i", "r")
    )
    payment = _pairable(
        MONZO,
        "-100",
        bank_transfer=True,
        names_account=True,
        payee_holders=frozenset(THREE),
        occurred_at=T0 - timedelta(hours=3),
    )
    for decisions in (
        _Decisions(frozenset(), frozenset({(sent.source, row.source)}), ()),
        _Decisions(frozenset(), frozenset({(row.source, sent.source)}), ()),
        _Decisions(frozenset({sent.source}), frozenset(), ()),
    ):
        same, _, _ = _registry_edges([sent, row, payment], DEFAULTS, decisions, _ALL_KNOWN)
        assert same.tiers[row.id] == {payment.id: ONE_SIDED}, decisions
    # The row is no less the move's when the owner kept another pair apart.
    other = _Decisions(frozenset(), frozenset({(payment.source, ("i", "x"))}), ())
    same, _, _ = _registry_edges([sent, row, payment], DEFAULTS, other, _ALL_KNOWN)
    assert same.tiers[row.id] == {sent.id: ONE_SIDED}


def test_the_coverage_gate_holds_every_account_a_family_record_names() -> None:
    """Controller's ruling (2026-11-27): a pair with a family record is gated on the accounts
    the family side names (its holder or its set), the other record's from _gate_day and
    every other manual one from the start of that side's window; never on the family
    record's own account. A Cash ISA row naming the family of three (a manual account, its
    Notes empty) and an Invest TRANSFER (connected) naming it too, ten minutes apart (two
    family records pair by the date window alone): ONE_SIDED, taken only when the Cash ISA
    is listed from before the TRANSFER's window."""
    gbp = UUID(int=1)
    row = _move(
        CASH, "100", THREE, source=("import", "cash"), occurred_at=T0 + timedelta(minutes=10)
    )
    transfer = _move(INVEST, "-100", THREE)
    start = T0.date() - timedelta(days=DEFAULTS.window_before.days)

    def known(cash_from: date) -> Known:
        # Stocks ISA manual and listed for all time; Invest connected.
        return Known(frozenset({CASH, STOCKS}), {(STOCKS, gbp): date.min, (CASH, gbp): cash_from})

    for cash_from, taken in ((start, True), (start + timedelta(days=1), False)):
        same, _, refused = _registry_edges(
            [transfer, row], DEFAULTS, _NO_DECISIONS, known(cash_from)
        )
        assert same.tiers[row.id] == {transfer.id: ONE_SIDED}  # a candidate either way
        assert (frozenset((row.id, transfer.id)) not in refused) == taken, cash_from
    # The family side's other manual account (Stocks ISA, in the Cash ISA row's set) is
    # held from the start of the row's window.
    unlisted = Known(frozenset({CASH, STOCKS}), {(CASH, gbp): date.min})
    _, _, refused = _registry_edges([transfer, row], DEFAULTS, _NO_DECISIONS, unlisted)
    assert refused == {frozenset((row.id, transfer.id))}
    # The Cash ISA row sending, naming the family, to an Invest record naming nobody
    # (whatever it is marked): the Cash ISA's own listing is not asked, only the accounts the
    # row names (Invest, connected; Stocks ISA, listed).
    sent = _move(CASH, "-100", THREE, source=("import", "cash-out"))
    received = _pairable(INVEST, "100", occurred_at=T0 + timedelta(seconds=5))
    late = known(date.max)
    same, _, refused = _registry_edges([sent, received], DEFAULTS, _NO_DECISIONS, late)
    assert same.tiers[sent.id] == {received.id: ONE_SIDED} and not refused
    # A family of two: the record is identified to the one other account, and gated as any
    # identified record is, on that account.
    two = _move(CASH, "-100", {CASH, STOCKS}, source=("import", "cash-two"))
    on_stocks = _pairable(STOCKS, "100", occurred_at=T0 + timedelta(seconds=5))
    for stocks_from, taken in ((start, True), (start + timedelta(days=1), False)):
        gate = Known(frozenset({CASH, STOCKS}), {(STOCKS, gbp): stocks_from})
        same, _, refused = _registry_edges([two, on_stocks], DEFAULTS, _NO_DECISIONS, gate)
        assert same.tiers[two.id] == {on_stocks.id: ONE_SIDED}
        assert (not refused) == taken, stocks_from


def _exact(
    records: list[Pairable],
) -> tuple[dict[UUID, dict[UUID, Tier]], dict[UUID, frozenset[UUID]], frozenset[frozenset[UUID]]]:
    """(tiers, exact, untakeable) as pair_relation builds them from _registry_edges: each
    record's candidates in the strongest tier it has any in, for which that is also theirs."""
    same, _, refused = _registry_edges(records, DEFAULTS, _NO_DECISIONS, _ALL_KNOWN)
    tiers = {r: dict(found) for r, found in same.tiers.items()}
    best = {r: min(found.values()) for r, found in tiers.items()}
    exact = {
        r: frozenset(c for c, tier in found.items() if tier == best[r] == best[c])
        for r, found in tiers.items()
    }
    return tiers, {r: found for r, found in exact.items() if found}, frozenset(refused)


def _relation(records: list[Pairable], untakeable: Iterable[frozenset[UUID]] = ()) -> PairRelation:
    """The relation pair_relation builds over `records` (no card or cross-currency pair),
    with `untakeable` pairs besides those the coverage gate refuses."""
    by_id = {x.id: x for x in records}
    tiers, exact, refused = _exact(records)
    untaken = refused | frozenset(untakeable)
    exact, twins = _several(by_id, tiers, exact, untaken, NEAR)
    return PairRelation(
        records=by_id,
        rejected=frozenset(),
        tiers=tiers,
        exact=exact,
        untakeable=untaken,
        differing={},
        twins=twins,
        across={},
        alone_across=frozenset(),
        converts={},
        cards={},
        by_card={},
    )


def _partners(relation: PairRelation) -> set[frozenset[UUID]]:
    return {
        frozenset((r, p))
        for r in relation.records
        if (p := relation.partner(r)) is not None and relation.partner(p) == r
    }


def test_several_candidates_split_by_the_only_one_within_the_near_window() -> None:
    """The owner's case (§5.10): two payments from Monzo to the payee all three Trading 212
    accounts hold, three days apart, each followed 40 s later by a Cash ISA deposit, a bank
    transfer naming nobody timed to the second. The first payment has both deposits as
    candidates and the second deposit both payments; each pairs with the one 40 s from it."""
    payee: dict[str, Any] = {
        "bank_transfer": True,
        "names_account": True,
        "payee_holders": frozenset(THREE),
    }
    later = timedelta(days=3)
    paid = [
        _pairable(MONZO, "-100", **payee),
        _pairable(MONZO, "-100", occurred_at=T0 + later, booking_date=(T0 + later).date(), **payee),
    ]
    rows = [
        _pairable(
            CASH,
            "100",
            bank_transfer=True,
            occurred_at=at,
            booking_date=at.date(),
            source=("import", str(at)),
        )
        for at in (T0 + timedelta(seconds=40), T0 + later + timedelta(seconds=40))
    ]
    # Without the near window, the rule can't choose: the first payment's window (a day
    # before it to five after) holds both rows, and the second row's both payments.
    _, exact, _ = _exact([*paid, *rows])
    assert exact[paid[0].id] == {x.id for x in rows}
    assert exact[rows[1].id] == {x.id for x in paid}
    assert _twins({x.id: x for x in (*paid, *rows)}, exact, ()) == {}
    relation = _relation([*paid, *rows])
    assert _partners(relation) == {
        frozenset((paid[0].id, rows[0].id)),
        frozenset((paid[1].id, rows[1].id)),
    }
    # The second row 90 s after the first payment: two candidates within the window of it.
    near = replace(rows[1], occurred_at=T0 + timedelta(seconds=90), booking_date=T0.date())
    relation = _relation([*paid, rows[0], near])
    assert _partners(relation) == set()
    assert relation.review_reason(paid[0].id) == "several_candidates"
    # Rows giving only their dates: time tells nothing.
    untimed = [
        replace(x, timed=False, occurred_at=datetime.combine(x.booking_date, time(), UTC))
        for x in rows
    ]
    assert _partners(_relation([*paid, *untimed])) == set()
    # An untimed candidate of the same amount blocks it too: its real time is unknown, so
    # it can't be shown to lie outside the window (owner's ruling, 2026-11-27). A Stocks
    # ISA row giving only its date, a bank transfer naming nobody, beside the timed pair.
    dated = _pairable(STOCKS, "100", bank_transfer=True, timed=False, source=("import", "d"))
    relation = _relation([paid[0], rows[0], dated])
    assert relation.exact[paid[0].id] == {rows[0].id, dated.id}
    assert _partners(relation) == set()
    assert relation.review_reason(paid[0].id) == "several_candidates"
    # A near pair the coverage gate refuses is not taken.
    refused = [frozenset((x.id, y.id)) for x, y in zip(paid, rows, strict=True)]
    assert _partners(_relation([*paid, *rows], untakeable=refused)) == set()
    relation = _relation([*paid, *rows], untakeable=refused[:1])
    assert _partners(relation) == {frozenset((paid[1].id, rows[1].id))}
    assert relation.review_reason(paid[0].id) == "several_candidates"
    # A candidate of a lower tier within the window of the first row blocks it too: a
    # payment from another bank whose sort code alone names the Cash ISA, 10 s after it.
    weak = _pairable(
        UUID(int=24),
        "-100",
        bank_transfer=True,
        weak_holder=CASH,
        occurred_at=T0 + timedelta(seconds=50),
    )
    relation = _relation([*paid, *rows, weak])
    assert relation.tiers[rows[0].id][weak.id] > relation.tiers[rows[0].id][paid[0].id]
    assert _partners(relation) == {frozenset((paid[1].id, rows[1].id))}


def test_one_side_alike_pairs_in_time_order() -> None:
    """The real shape (§5.7): TRANSFERs out of Stocks ISA and Invest 17 s apart, each
    naming the family of three, and two identical Cash ISA rows 18 s apart, bank transfers
    naming nobody. Each TRANSFER has both rows, each row both TRANSFERs: the senders are on
    two accounts, the rows alike, so the group pairs, in time order."""
    stocks = _move(STOCKS, "-100", THREE)
    invest = _move(INVEST, "-100", THREE, occurred_at=T0 + timedelta(seconds=17))
    rows = [
        _pairable(CASH, "100", bank_transfer=True, occurred_at=at, source=("import", str(at)))
        for at in (T0, T0 + timedelta(seconds=18))
    ]
    records = {x.id: x for x in (stocks, invest, *rows)}
    _, exact, untakeable = _exact(list(records.values()))
    assert exact[stocks.id] == exact[invest.id] == {x.id for x in rows}
    assert _twins(records, exact, untakeable) == {
        stocks.id: rows[0].id,
        rows[0].id: stocks.id,
        invest.id: rows[1].id,
        rows[1].id: invest.id,
    }
    # The senders' times swapped, the links swap.
    swapped = [replace(stocks, occurred_at=invest.occurred_at), replace(invest, occurred_at=T0)]
    records = {x.id: x for x in (*swapped, *rows)}
    _, exact, untakeable = _exact(list(records.values()))
    assert _twins(records, exact, untakeable) == {
        invest.id: rows[0].id,
        rows[0].id: invest.id,
        stocks.id: rows[1].id,
        rows[1].id: stocks.id,
    }
    # A third such row in the window: two by three, no twins.
    third = _pairable(CASH, "100", bank_transfer=True, occurred_at=T0 + timedelta(seconds=30))
    records = {x.id: x for x in (stocks, invest, *rows, third)}
    _, exact, untakeable = _exact(list(records.values()))
    assert _twins(records, exact, untakeable) == {}


def test_an_interchangeable_group_stays_whole_before_the_split() -> None:
    """Two payments from Monzo naming the Cash ISA, at T0 and four hours later, and two
    Cash ISA rows naming nobody, 30 s after the first and eight hours after it: both sides
    alike, each record has both of the other side. The first payment and the first row are
    each the other's only one within the near window, but the group pairs as
    interchangeable, in time order, and all four pair: the split is for records that
    would otherwise go to review (§5.10)."""
    named: dict[str, Any] = {"bank_transfer": True, "names_account": True, "holder": CASH}
    paid = [_pairable(MONZO, "-100", occurred_at=T0 + timedelta(hours=h), **named) for h in (0, 4)]
    rows = [
        _pairable(CASH, "100", bank_transfer=True, occurred_at=at, source=("import", str(at)))
        for at in (T0 + timedelta(seconds=30), T0 + timedelta(hours=8))
    ]
    relation = _relation([*paid, *rows])
    assert relation.exact[paid[0].id] == {x.id for x in rows}
    assert _partners(relation) == {
        frozenset((paid[0].id, rows[0].id)),
        frozenset((paid[1].id, rows[1].id)),
    }


PAYEE = "121212:34343434"  # the payee all three Trading 212 accounts hold


@pytest.mark.parametrize("payments_first", [True, False])
async def test_repeated_top_ups_pair_one_by_one(
    session: AsyncSession,
    run: Run,
    connected_bank: MakeConnected,
    family_accounts: MakeFamily,
    connector_tx: ConnectorTx,
    statement_row: StatementRow,
    heal: Heal,
    payments_first: bool,
) -> None:
    """The owner's case (§6): two payments of 100 from Monzo to the payee the Trading 212
    accounts hold, three days apart, each followed 40 s later by a Cash ISA deposit timed to
    the second. The first payment has both deposits as candidates, yet each pairs with the
    one 40 s from it: two matched transfers."""
    m = await connected_bank("M")
    invest, (stocks, cash) = await family_accounts(3)
    payee = (
        await run(
            register_holders_and_rematch,
            "uk_bank_account",
            PAYEE,
            [invest.account.id, stocks.id, cash.id],
        )
    ).id
    await statement_row(stocks, "-7")  # Stocks ISA's imports list the days in full
    later = timedelta(days=3)

    async def payments() -> list[UUID]:
        return [
            (
                await connector_tx(
                    m,
                    "-100",
                    external_id=f"p{n}",
                    occurred_at=WHEN + n * later,
                    identifier_id=payee,
                )
            ).id
            for n in (0, 1)
        ]

    async def rows() -> list[UUID]:
        return [
            await statement_row(
                cash,
                "100",
                at=WHEN + n * later + timedelta(seconds=40),
                time_unit="second",
                key=f"row{n}",
            )
            for n in (0, 1)
        ]

    if payments_first:
        paid = await payments()
        await heal()
        received = await rows()
    else:
        received = await rows()
        await heal()
        paid = await payments()
    await heal()

    matched = await list_transfers(session, statuses=["matched"])
    assert sorted((t.out_transaction_id, t.in_transaction_id) for t in matched) == sorted(
        zip(paid, received, strict=True)
    )
    assert await heal() == 0
    await assert_consistent(session)


async def test_an_outside_deposit_near_a_family_move_pairs_with_its_payee_payment(
    session: AsyncSession,
    run: Run,
    connected_bank: MakeConnected,
    family_accounts: MakeFamily,
    connector_tx: ConnectorTx,
    statement_row: StatementRow,
    heal: Heal,
) -> None:
    """§5.9: a Cash ISA deposit whose Notes give a transaction id went outside the family.
    Thirty seconds after a TRANSFER of the same amount out of Invest, it is still the
    payment from Monzo to the payee the three accounts hold, a day earlier, and the TRANSFER
    stays unpaired."""
    m = await connected_bank("M")
    invest, (stocks, cash) = await family_accounts(3)
    payee = (
        await run(
            register_holders_and_rematch,
            "uk_bank_account",
            PAYEE,
            [invest.account.id, stocks.id, cash.id],
        )
    ).id
    # Stocks ISA's imports list in full from before the payment's window.
    listed = (WHEN.date() - timedelta(days=10), WHEN.date() + timedelta(days=60))
    await statement_row(stocks, "-7", complete=listed)
    family_id = await run(family_identifier, "trading212")
    await connector_tx(invest, "-100", external_id="move", identifier_id=family_id)
    paid = await connector_tx(
        m, "-100", external_id="pay", occurred_at=WHEN - timedelta(days=1), identifier_id=payee
    )
    received = await statement_row(
        cash,
        "100",
        at=WHEN + timedelta(seconds=30),
        time_unit="second",
        key="deposit",
        outside_family=True,
    )
    await heal()

    # The one matched transfer: the TRANSFER is in none.
    matched = await list_transfers(session, statuses=["matched"])
    assert [(t.out_transaction_id, t.in_transaction_id) for t in matched] == [(paid.id, received)]
    assert await heal() == 0
    await assert_consistent(session)
