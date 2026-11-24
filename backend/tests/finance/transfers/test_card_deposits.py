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
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.actors import Actor
from lararium.core.commands import command_context
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
async def platform(run: Run, connected_bank: MakeConnected) -> tuple[Connected, ...]:
    """Monzo, and Trading 212's Invest and ISA, each on a connection of its own, with the
    merchant registered as paying into Invest and ISA."""
    monzo, invest, isa = (
        await connected_bank("Monzo"),
        await connected_bank("Invest"),
        await connected_bank("ISA"),
    )
    await run(register_card_merchant, MERCHANT, [invest.account.id, isa.account.id])
    return monzo, invest, isa


async def merchant_id(run: Run, platform: tuple[Connected, ...]) -> UUID:
    _, invest, isa = platform
    return (await run(register_card_merchant, MERCHANT, [invest.account.id, isa.account.id])).id


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
    monzo, invest, isa = platform
    merchant = await merchant_id(run, platform)
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
    monzo, invest, isa = platform
    merchant = await merchant_id(run, platform)
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
    monzo, invest, _ = platform
    merchant = await merchant_id(run, platform)
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
    monzo, invest, _ = platform
    merchant = await merchant_id(run, platform)
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
    monzo, invest, _ = platform
    merchant = await merchant_id(run, platform)
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
    monzo, invest, _ = platform
    merchant = await merchant_id(run, platform)
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
    monzo, invest, _ = platform
    merchant = await merchant_id(run, platform)
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
    connector_tx: ConnectorTx,
    heal: Heal,
) -> None:
    """The owner pairs the card payment with ISA's deposit (outside the window): Invest's,
    which the tier would have taken, stays alone."""
    monzo, invest, isa = platform
    merchant = await merchant_id(run, platform)
    paid = await payment(connector_tx, monzo, merchant, TEN)
    into_invest = await deposit(connector_tx, invest, TEN)
    into_isa = await deposit(connector_tx, isa, TEN + timedelta(hours=1))
    await heal()
    [auto] = await list_transfers(session)
    assert (auto.out_transaction_id, auto.in_transaction_id) == (paid.id, into_invest.id)

    await run(unpair, auto.id)
    await run(pair_records, paid.id, into_isa.id)
    assert await heal() == 0
    assert pairs_of(await list_transfers(session)) == {(paid.id, into_isa.id, "matched")}
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
    The tier takes connectors' records only, which carry the time of day."""
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
    monzo, invest, _ = platform
    merchant = await merchant_id(run, platform)
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
    monzo, invest, isa = platform
    merchant = await merchant_id(run, platform)
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


async def test_card_pair_dissolved_when_sync_closes_holder(
    session: AsyncSession,
    run: Run,
    platform,
    ids: SystemIds,
    connector_tx: ConnectorTx,
    heal: Heal,
) -> None:
    monzo, _, isa = platform
    merchant = await merchant_id(run, platform)
    paid = await payment(connector_tx, monzo, merchant, TEN)
    into = await deposit(connector_tx, isa, TEN)
    await heal()
    assert len(await list_transfers(session)) == 1

    async with command_context(session, Actor("connector", "monzo"), "connection.sync", {}) as ctx:
        await close_account(ctx, isa.account.id, DAY)
    await heal()

    assert await list_transfers(session) == []
    assert await contra_account(session, paid.id, monzo.account.id) == ids.unassigned
    assert await contra_account(session, into.id, isa.account.id) == ids.unassigned
    await assert_consistent(session)
    assert await heal() == 0


async def test_owner_pair_stays_when_holder_removed(
    session: AsyncSession,
    run: Run,
    platform,
    connector_tx: ConnectorTx,
    heal: Heal,
) -> None:
    monzo, invest, isa = platform
    merchant = await merchant_id(run, platform)
    paid = await payment(connector_tx, monzo, merchant, TEN)
    into = await deposit(connector_tx, invest, TEN + timedelta(hours=1))  # outside the window
    await heal()
    await run(pair_records, paid.id, into.id)

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
