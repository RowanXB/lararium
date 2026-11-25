"""Card deposits paired by time (eToro design §5.12, Task 16): a Monzo card payment to a
merchant registered as paying into own accounts pairs with the deposit on one of them of the
same amount, within the near window, each the only such candidate for the other. The pair
is an ordinary matched transfer, both legs in In transit."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.modules.finance import models
from lararium.modules.finance.accounts import close_account
from lararium.modules.finance.identifiers import assign_identifier, register_card_merchant
from lararium.modules.finance.system_accounts import system_account_ids
from lararium.modules.finance.transfers import (
    TransferRecord,
    list_transfers,
    load_transfer_settings,
    pair_records,
    register_card_merchant_and_rematch,
    reject,
    unpair,
)
from lararium.modules.finance.transfers.pairs import (
    ONE_SIDED,
    Pairable,
    card_partners,
    card_tier,
    pair_relation,
)
from lararium.modules.finance.transfers.settings import DEFAULTS

from .conftest import (
    DAY,
    Connected,
    ConnectorTx,
    Heal,
    ImportedBank,
    MakeConnected,
    Run,
    StatementRow,
    SystemIds,
    assert_consistent,
    contra_account,
    in_transit_balance,
)

MERCHANT = "merch_TEST212"
MONZO_IDENTIFIER = "998877:87654321"
TEN = datetime(2026, 11, 2, 10, tzinfo=UTC)
WINDOW = DEFAULTS.near_window


@pytest.fixture
async def platform(
    run: Run, connected_bank: MakeConnected
) -> tuple[Connected, Connected, Connected, UUID]:
    """Monzo, and Trading 212's Invest and ISA, each on a connection of its own, and the
    merchant's identifier, registered as paying into Invest and ISA."""
    monzo, invest, isa = (
        await connected_bank("Monzo"),
        await connected_bank("Invest"),
        await connected_bank("ISA"),
    )
    record = await run(register_card_merchant, MERCHANT, [invest.account.id, isa.account.id])
    return monzo, invest, isa, record.id


async def payment(connector_tx: ConnectorTx, monzo: Connected, merchant: UUID, at: datetime, **kw):
    """A Monzo card payment to the merchant (a refund when the quantity is positive)."""
    return await connector_tx(
        monzo,
        kw.pop("quantity", "-50.00"),
        external_id=kw.pop("external_id", f"card_{at:%H%M%S}"),
        occurred_at=at,
        identifier_id=merchant,
        bank_transfer=False,
        description="Trading 212",
        **kw,
    )


async def deposit(connector_tx: ConnectorTx, account: Connected, at: datetime, **kw):
    """A Trading 212 DEPOSIT (a WITHDRAW when the quantity is negative): no identifier, not
    a bank transfer."""
    return await connector_tx(
        account,
        kw.pop("quantity", "50.00"),
        external_id=kw.pop("external_id", f"transaction:{at:%H%M%S}"),
        occurred_at=at,
        bank_transfer=kw.pop("bank_transfer", False),
        description="Deposit",
        **kw,
    )


def pairs_of(transfers: list[TransferRecord]) -> set[tuple[UUID | None, UUID | None, str]]:
    return {(t.out_transaction_id, t.in_transaction_id, t.status) for t in transfers}


async def test_two_card_deposits_paired_by_time(
    session: AsyncSession,
    run: Run,
    platform,
    ids: SystemIds,
    connector_tx: ConnectorTx,
    heal: Heal,
) -> None:
    monzo, invest, isa, merchant = platform
    first = await payment(connector_tx, monzo, merchant, TEN)
    second = await payment(connector_tx, monzo, merchant, TEN + timedelta(minutes=8, seconds=32))
    into_invest = await deposit(connector_tx, invest, TEN + timedelta(seconds=20))
    into_isa = await deposit(connector_tx, isa, TEN + timedelta(minutes=8, seconds=50))
    await heal()

    transfers = await list_transfers(session)
    assert pairs_of(transfers) == {
        (first.id, into_invest.id, "matched"),
        (second.id, into_isa.id, "matched"),
    }
    by_out = {t.out_transaction_id: t for t in transfers}
    assert (by_out[first.id].from_account_id, by_out[first.id].to_account_id) == (
        monzo.account.id,
        invest.account.id,
    )
    assert by_out[second.id].to_account_id == isa.account.id
    assert all(t.settlement_transaction_id is None for t in transfers)  # no conversion
    for record, account in (
        (first, monzo),
        (second, monzo),
        (into_invest, invest),
        (into_isa, isa),
    ):
        assert await contra_account(session, record.id, account.account.id) == ids.in_transit
    assert await in_transit_balance(session, ids) == Decimal(0)
    await assert_consistent(session)
    assert await heal() == 0


REAL_GAP = timedelta(seconds=512)  # between the owner's two deposits (Task 0, check 5)


@pytest.mark.parametrize(
    "steps",
    [
        pytest.param([["invest", "p1", "p2"], ["isa"]], id="invest-deposit-first"),
        pytest.param([["isa", "p1", "p2"], ["invest"]], id="isa-deposit-first"),
        pytest.param([["invest", "isa"], ["p1", "p2"]], id="trading212-first"),
    ],
)
async def test_owners_case_in_each_arrival_order(
    session: AsyncSession,
    platform,
    ids: SystemIds,
    connector_tx: ConnectorTx,
    heal: Heal,
    steps: list[list[str]],
) -> None:
    """The owner's real case: two equal payments 512 s apart, each deposit 0 s from its
    payment, Invest's first. The records arrive in syncs (each step, then a heal); after
    each, a payment whose deposit is there is matched with it, and one whose deposit isn't
    stays on Unassigned (the other deposit is 512 s away, outside the window)."""
    monzo, invest, isa, merchant = platform
    post = {
        "p1": lambda: payment(connector_tx, monzo, merchant, TEN, external_id="p1"),
        "p2": lambda: payment(connector_tx, monzo, merchant, TEN + REAL_GAP, external_id="p2"),
        "invest": lambda: deposit(connector_tx, invest, TEN, external_id="transaction:invest"),
        "isa": lambda: deposit(connector_tx, isa, TEN + REAL_GAP, external_id="transaction:isa"),
    }
    accounts = {"p1": monzo, "p2": monzo, "invest": invest, "isa": isa}
    posted: dict[str, UUID] = {}
    for step in steps:
        for key in step:
            posted[key] = (await post[key]()).id
        await heal()

        expected = {
            (posted[p], posted[d], "matched")
            for p, d in (("p1", "invest"), ("p2", "isa"))
            if p in posted and d in posted
        }
        assert pairs_of(await list_transfers(session)) == expected
        paired = {x for pair in expected for x in pair[:2]}
        for key, record_id in posted.items():
            contra = await contra_account(session, record_id, accounts[key].account.id)
            assert contra == (ids.in_transit if record_id in paired else ids.unassigned), key
        await assert_consistent(session)
    assert await in_transit_balance(session, ids) == Decimal(0)
    assert await heal() == 0


async def test_one_payment_deposits_on_both_holders_not_paired(
    session: AsyncSession,
    platform,
    ids: SystemIds,
    connector_tx: ConnectorTx,
    heal: Heal,
) -> None:
    """One payment, and a deposit of its amount on each holder account within the window:
    uniqueness is across all the merchant's holders, so nothing pairs."""
    monzo, invest, isa, merchant = platform
    paid = await payment(connector_tx, monzo, merchant, TEN)
    into_invest = await deposit(connector_tx, invest, TEN + timedelta(seconds=10))
    into_isa = await deposit(connector_tx, isa, TEN - timedelta(seconds=30))
    await heal()

    assert await list_transfers(session) == []
    for record, account in ((paid, monzo), (into_invest, invest), (into_isa, isa)):
        assert await contra_account(session, record.id, account.account.id) == ids.unassigned
    relation = await pair_relation(
        session, await system_account_ids(session), await load_transfer_settings(session)
    )
    assert relation.several(paid.id)
    assert relation.cards[paid.id] == frozenset({into_invest.id, into_isa.id})
    await assert_consistent(session)
    assert await heal() == 0


async def test_card_deposits_too_close_left_to_owner(
    session: AsyncSession,
    run: Run,
    platform,
    ids: SystemIds,
    connector_tx: ConnectorTx,
    heal: Heal,
) -> None:
    """Both payments are within the window of Invest's deposit: nothing pairs. Neither has
    a transfer to carry several_candidates (no awaiting transfer is opened for a card
    payment, Task 15's ruling D), so the relation says so, and both stay on Unassigned for
    the owner to pair."""
    monzo, invest, isa, merchant = platform
    first = await payment(connector_tx, monzo, merchant, TEN)
    second = await payment(connector_tx, monzo, merchant, TEN + timedelta(minutes=1))
    into_invest = await deposit(connector_tx, invest, TEN + timedelta(seconds=20))
    into_isa = await deposit(connector_tx, isa, TEN + timedelta(minutes=8, seconds=50))
    await heal()

    assert await list_transfers(session) == []
    for record, account in ((first, monzo), (second, monzo), (into_invest, invest)):
        assert await contra_account(session, record.id, account.account.id) == ids.unassigned
    relation = await pair_relation(
        session, await system_account_ids(session), await load_transfer_settings(session)
    )
    for record in (first, second, into_invest):
        assert relation.several(record.id)
        assert relation.partner(record.id) is None
    assert not relation.several(into_isa.id)
    await assert_consistent(session)
    assert await heal() == 0

    # The owner pairs by hand.
    transfer = await run(pair_records, second.id, into_invest.id)
    assert transfer.status == "matched"
    assert await heal() == 0
    await assert_consistent(session)


async def test_amount_differs_not_paired(
    session: AsyncSession,
    run: Run,
    platform,
    ids: SystemIds,
    connector_tx: ConnectorTx,
    heal: Heal,
) -> None:
    monzo, invest, _, merchant = platform
    paid = await payment(connector_tx, monzo, merchant, TEN)
    await deposit(connector_tx, invest, TEN, quantity="49.99")
    await heal()

    assert await list_transfers(session) == []
    assert await contra_account(session, paid.id, monzo.account.id) == ids.unassigned
    await assert_consistent(session)


async def test_refund_pairs_in_reverse(
    session: AsyncSession,
    run: Run,
    platform,
    ids: SystemIds,
    connector_tx: ConnectorTx,
    heal: Heal,
) -> None:
    monzo, invest, _, merchant = platform
    withdraw = await deposit(connector_tx, invest, TEN, quantity="-50.00")
    refund = await payment(
        connector_tx, monzo, merchant, TEN + timedelta(seconds=5), quantity="50.00"
    )
    await heal()

    [transfer] = await list_transfers(session)
    assert (
        transfer.status,
        transfer.out_transaction_id,
        transfer.in_transaction_id,
        transfer.from_account_id,
        transfer.to_account_id,
    ) == ("matched", withdraw.id, refund.id, invest.account.id, monzo.account.id)
    assert await in_transit_balance(session, ids) == Decimal(0)
    await assert_consistent(session)
    assert await heal() == 0


@pytest.mark.parametrize(
    ("gap", "pairs"),
    [(timedelta(minutes=2), True), (timedelta(minutes=2, seconds=1), False)],
)
async def test_outside_window_not_paired(
    session: AsyncSession,
    run: Run,
    platform,
    connector_tx: ConnectorTx,
    heal: Heal,
    gap: timedelta,
    pairs: bool,
) -> None:
    """The window is inclusive: 2 minutes apart pairs, a second more doesn't, whichever came
    first."""
    monzo, invest, _, merchant = platform
    await payment(connector_tx, monzo, merchant, TEN + gap)
    await deposit(connector_tx, invest, TEN)
    await heal()

    assert len(await list_transfers(session)) == (1 if pairs else 0)
    await assert_consistent(session)


async def test_unpair_keeps_them_apart_and_reject_works(
    session: AsyncSession,
    run: Run,
    platform,
    ids: SystemIds,
    connector_tx: ConnectorTx,
    heal: Heal,
) -> None:
    monzo, invest, _, merchant = platform
    paid = await payment(connector_tx, monzo, merchant, TEN)
    into = await deposit(connector_tx, invest, TEN)
    await heal()
    [transfer] = await list_transfers(session)

    await run(unpair, transfer.id)
    assert await list_transfers(session) == []
    assert await contra_account(session, paid.id, monzo.account.id) == ids.unassigned
    assert await contra_account(session, into.id, invest.account.id) == ids.unassigned
    assert await heal() == 0
    await assert_consistent(session)

    # Paired again by hand, then rejected: the rejection holds both out of the tier.
    again = await run(pair_records, paid.id, into.id)
    await run(reject, again.id)
    assert await heal() == 0
    [rejected] = await list_transfers(session)
    assert rejected.status == "rejected"
    await assert_consistent(session)


async def test_exact_tier_takes_precedence(
    session: AsyncSession,
    run: Run,
    platform,
    ids: SystemIds,
    connector_tx: ConnectorTx,
    heal: Heal,
) -> None:
    """A deposit an exact tier pairs (a bank transfer naming nobody, which a record
    identified as Invest's takes, ONE_SIDED) is not the card's: the card payment, whose only
    candidate it is, stays for the owner."""
    monzo, invest, _, merchant = platform
    invest_identifier = await run(
        assign_identifier, invest.account.id, "uk_bank_account", MONZO_IDENTIFIER
    )
    paid = await payment(connector_tx, monzo, merchant, TEN)
    named = await connector_tx(
        monzo,
        "-50.00",
        external_id="to_invest",
        occurred_at=TEN + timedelta(seconds=30),
        identifier_id=invest_identifier.id,
    )
    into = await deposit(connector_tx, invest, TEN, bank_transfer=True)
    await heal()

    transfers = await list_transfers(session)
    assert pairs_of(transfers) == {(named.id, into.id, "matched")}
    assert await contra_account(session, paid.id, monzo.account.id) == ids.unassigned
    relation = await pair_relation(
        session, await system_account_ids(session), await load_transfer_settings(session)
    )
    assert relation.several(paid.id)  # its only candidate is paired elsewhere
    await assert_consistent(session)
    assert await heal() == 0


async def test_owner_pair_takes_precedence(
    session: AsyncSession,
    run: Run,
    platform,
    ids: SystemIds,
    connector_tx: ConnectorTx,
    heal: Heal,
) -> None:
    """The card tier would pair the payment with Invest's deposit; before any heal, the
    owner pairs it with ISA's (outside the window) instead. The pair decision alone, with
    no never_pair anywhere, holds: the owner's pair stands, and the tier takes neither
    record, Invest's deposit staying alone on Unassigned."""
    monzo, invest, isa, merchant = platform
    paid = await payment(connector_tx, monzo, merchant, TEN)
    into_invest = await deposit(connector_tx, invest, TEN)
    into_isa = await deposit(connector_tx, isa, TEN + timedelta(hours=1))
    before = await pair_relation(
        session, await system_account_ids(session), await load_transfer_settings(session)
    )
    assert before.partner(paid.id) == into_invest.id  # the tier's pair, were it healed

    await run(pair_records, paid.id, into_isa.id)

    d = models.TransferDecision
    assert set(await session.scalars(select(d.kind))) == {"pair"}
    assert pairs_of(await list_transfers(session)) == {(paid.id, into_isa.id, "matched")}
    assert await contra_account(session, into_invest.id, invest.account.id) == ids.unassigned
    after = await pair_relation(
        session, await system_account_ids(session), await load_transfer_settings(session)
    )
    assert after.by_card == {}
    assert after.partner(into_invest.id) is None
    assert await heal() == 0
    await assert_consistent(session)


async def test_day_granular_import_row_not_paired(
    session: AsyncSession,
    run: Run,
    connected_bank: MakeConnected,
    imported_bank: ImportedBank,
    statement_row: StatementRow,
    ids: SystemIds,
    connector_tx: ConnectorTx,
    heal: Heal,
) -> None:
    """A holder account whose rows are imported from a file giving dates only (stamped
    00:00): a card payment a minute after midnight doesn't pair with one by coincidence.
    The tier takes connectors' records only, assumed to carry the time of day."""
    monzo = await connected_bank("Monzo")
    imported = await imported_bank("Imported broker")
    merchant = (await run(register_card_merchant, MERCHANT, [imported.id])).id
    paid = await payment(
        connector_tx,
        monzo,
        merchant,
        datetime.combine(DAY, datetime.min.time(), UTC) + timedelta(minutes=1),
    )
    await statement_row(imported, "50.00", day=DAY, bank_transfer=False)
    await heal()

    assert await list_transfers(session) == []
    assert await contra_account(session, paid.id, monzo.account.id) == ids.unassigned
    await assert_consistent(session)


async def test_resync_keeps_the_pair(
    session: AsyncSession,
    run: Run,
    platform,
    connector_tx: ConnectorTx,
    heal: Heal,
) -> None:
    """Regeneration: both records posted again as a replay would, the pair stays, and a
    heal after it writes nothing."""
    monzo, invest, _, merchant = platform
    paid = await payment(connector_tx, monzo, merchant, TEN)
    into = await deposit(connector_tx, invest, TEN)
    await heal()
    [before] = await list_transfers(session)

    await payment(connector_tx, monzo, merchant, TEN)
    await deposit(connector_tx, invest, TEN)
    await heal()
    assert await heal() == 0
    [after] = await list_transfers(session)
    assert (after.out_transaction_id, after.in_transaction_id) == (paid.id, into.id)
    assert after.status == before.status == "matched"
    await assert_consistent(session)


# Heal re-checks a card-tier pair's evidence: once the receiving account leaves the
# merchant's holders, the pair is dissolved (heal arm (e): no longer a unique pair), unless
# the owner paired the two.


async def test_card_pair_dissolved_when_holder_removed(
    session: AsyncSession,
    run: Run,
    platform,
    ids: SystemIds,
    connector_tx: ConnectorTx,
    heal: Heal,
) -> None:
    monzo, invest, isa, merchant = platform
    paid = await payment(connector_tx, monzo, merchant, TEN)
    into = await deposit(connector_tx, invest, TEN)
    await heal()
    assert len(await list_transfers(session)) == 1

    await run(register_card_merchant_and_rematch, MERCHANT, [isa.account.id])
    assert await list_transfers(session) == []
    assert await contra_account(session, paid.id, monzo.account.id) == ids.unassigned
    assert await contra_account(session, into.id, invest.account.id) == ids.unassigned
    await assert_consistent(session)
    assert await heal() == 0

    # Unregistering the merchant does the same to a pair made while it was registered.
    await run(register_card_merchant_and_rematch, MERCHANT, [invest.account.id])
    assert len(await list_transfers(session)) == 1
    await run(register_card_merchant_and_rematch, MERCHANT, [])
    assert await list_transfers(session) == []
    await assert_consistent(session)
    assert await heal() == 0


async def test_card_pair_stands_when_sync_closes_holder(
    session: AsyncSession,
    run: Run,
    platform,
    ids: SystemIds,
    connector_tx: ConnectorTx,
    heal: Heal,
) -> None:
    """A closed account stays a holder of its card merchant, as a bank identifier stays
    assigned: its card-deposit history stands."""
    monzo, _, isa, merchant = platform
    paid = await payment(connector_tx, monzo, merchant, TEN)
    into = await deposit(connector_tx, isa, TEN)
    await heal()
    [before] = await list_transfers(session)

    async with command_context(session, Actor("connector", "monzo"), "connection.sync", {}) as ctx:
        await close_account(ctx, isa.account.id, DAY)
    assert await heal() == 0

    assert await list_transfers(session) == [before]
    assert pairs_of([before]) == {(paid.id, into.id, "matched")}
    await assert_consistent(session)


async def test_payment_after_a_holder_closes_pairs_into_the_open_one(
    session: AsyncSession,
    run: Run,
    platform,
    connector_tx: ConnectorTx,
    heal: Heal,
) -> None:
    """The closed holder takes no new deposits, so a payment after its close date pairs
    with the deposit on the open holder, the closed one being no rival."""
    monzo, invest, isa, merchant = platform
    await run(close_account, isa.account.id, DAY)
    later = TEN + timedelta(days=3)
    paid = await payment(connector_tx, monzo, merchant, later)
    into = await deposit(connector_tx, invest, later)
    await heal()
    assert pairs_of(await list_transfers(session)) == {(paid.id, into.id, "matched")}
    await assert_consistent(session)
    assert await heal() == 0


async def test_owner_pair_stays_when_holder_removed(
    session: AsyncSession,
    run: Run,
    platform,
    connector_tx: ConnectorTx,
    heal: Heal,
) -> None:
    """A pair the card tier formed, which the owner then pairs again by hand (unpair, then
    pair_records, which withdraws the never_pair): once Invest leaves the merchant's
    holders, the owner's pair decision still holds it."""
    monzo, invest, isa, merchant = platform
    paid = await payment(connector_tx, monzo, merchant, TEN)
    into = await deposit(connector_tx, invest, TEN)
    await heal()
    [auto] = await list_transfers(session)
    assert (auto.out_transaction_id, auto.in_transaction_id) == (paid.id, into.id)
    await run(unpair, auto.id)
    await run(pair_records, paid.id, into.id)
    d = models.TransferDecision
    assert set(await session.scalars(select(d.kind))) == {"pair"}

    await run(register_card_merchant_and_rematch, MERCHANT, [isa.account.id])
    assert pairs_of(await list_transfers(session)) == {(paid.id, into.id, "matched")}
    await assert_consistent(session)
    assert await heal() == 0


# The tier on its own, and its uniqueness, checked against a brute-force reading.


def _pairable(account: UUID, quantity: str, at: datetime, **kw) -> Pairable:
    defaults = {
        "id": uuid4(),
        "source": ("connection", f"{uuid4()}"),
        "instrument_id": UUID(int=1),
        "booking_date": at.date(),
        "holder": None,
        "bank_transfer": False,
        "fiat": True,
    }
    return Pairable(
        account_id=account, quantity=Decimal(quantity), occurred_at=at, **(defaults | kw)
    )


MONZO, INVEST, ISA = UUID(int=10), UUID(int=11), UUID(int=12)


def test_card_tier() -> None:
    card = _pairable(MONZO, "-50", TEN, holders=frozenset({INVEST, ISA}))
    plain = _pairable(INVEST, "50", TEN + WINDOW)
    assert card_tier(card, plain, WINDOW) == card_tier(plain, card, WINDOW) == ONE_SIDED
    refused = [
        _pairable(INVEST, "50", TEN + WINDOW + timedelta(seconds=1)),  # outside the window
        _pairable(INVEST, "49.99", TEN),  # another amount
        _pairable(INVEST, "-50", TEN),  # the same direction
        _pairable(INVEST, "50", TEN, instrument_id=UUID(int=2)),  # another instrument
        _pairable(MONZO, "50", TEN),  # not a holder
        _pairable(INVEST, "50", TEN, holder=MONZO),  # identified
        _pairable(INVEST, "50", TEN, holders=frozenset({MONZO})),  # another card payment
        _pairable(INVEST, "50", TEN, names_account=True, bank_transfer=True),  # names an account
        _pairable(INVEST, "50", TEN, source=("import", "k")),  # an imported row
        _pairable(INVEST, "50", TEN, source=("transaction", "t")),  # entered by hand
    ]
    for other in refused:
        assert card_tier(card, other, WINDOW) is None
        assert card_tier(other, card, WINDOW) is None
    # The payer is a connector's record too.
    by_hand = _pairable(MONZO, "-50", TEN, holders=frozenset({INVEST}), source=("import", "k"))
    assert card_tier(by_hand, plain, WINDOW) is None


_SECONDS = st.integers(min_value=0, max_value=400)
_record = st.tuples(
    st.sampled_from(("card", "invest", "isa")),
    st.sampled_from(("-50", "50", "-60")),
    _SECONDS,
)


@settings(max_examples=300, deadline=None)
@given(
    drawn=st.lists(_record, max_size=8),
    taken=st.sets(st.integers(min_value=0, max_value=7)),
    apart=st.sets(st.tuples(st.integers(0, 7), st.integers(0, 7))),
)
def test_card_partners_match_brute_force(
    drawn: list[tuple[str, str, int]], taken: set[int], apart: set[tuple[int, int]]
) -> None:
    """card_partners against every pair checked with card_tier: the candidates are exactly
    the pairs the tier takes that no decision keeps apart, and a record pairs exactly when
    it and its one candidate are each the other's only one and neither is taken by another
    tier (`taken`: in an exact tier, or across currencies)."""
    accounts = {"card": MONZO, "invest": INVEST, "isa": ISA}
    records = [
        _pairable(
            accounts[kind],
            quantity,
            TEN + timedelta(seconds=seconds),
            holders=frozenset({INVEST, ISA}) if kind == "card" else frozenset(),
        )
        for kind, quantity, seconds in drawn
    ]
    index = {x.id: n for n, x in enumerate(records)}
    taken_ids = frozenset(x.id for x in records if index[x.id] in taken)

    def kept_apart(r: Pairable, c: Pairable) -> bool:
        return (index[r.id], index[c.id]) in apart or (index[c.id], index[r.id]) in apart

    candidates, partners = card_partners(records, WINDOW, kept_apart, taken_ids)

    expected = {
        r.id: frozenset(
            c.id
            for c in records
            if c.id != r.id and card_tier(r, c, WINDOW) is not None and not kept_apart(r, c)
        )
        for r in records
    }
    expected = {r: found for r, found in expected.items() if found}
    assert candidates == expected
    expected_partners = {
        r: c
        for r, found in expected.items()
        for c in found
        if len(found) == 1
        and expected.get(c) == frozenset({r})
        and r not in taken_ids
        and c not in taken_ids
    }
    assert partners == expected_partners
