"""The owner pairs two fiat records whose sides differ (eToro design §5.12, "Owner pairs
records whose sides differ"): in currency (eToro Trading pays out USD that arrives at a UK
bank in GBP), settled by a conversion with the legs' fee, else 0; or in amount in one
currency (a deposit that arrives net of a platform fee), the difference a fee on In transit
against Unassigned. Either way In transit nets to zero in every currency."""

from collections import Counter
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.modules.finance.errors import FinanceValidationError
from lararium.modules.finance.identifiers import assign_identifier
from lararium.modules.finance.instruments import InstrumentRecord
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    TransactionRecord,
    upsert_connector_transaction,
)
from lararium.modules.finance.transfers import (
    TransferRecord,
    candidates,
    list_transfers,
    pair,
    pair_candidates,
    pair_records,
    reject,
    transfer_contra,
    unpair,
)

from .conftest import WHEN, Connected, MakeConnected, Run, SystemIds, assert_consistent

DAY = WHEN.date()


@pytest.fixture
def leg(session: AsyncSession, run: Run):
    """Post (or re-post) a connector's record in its instrument, its contra from
    transfer_contra, with the leg fee its connector gives (transfer_leg_fees), if any."""

    async def _post(
        bank: Connected,
        instrument: InstrumentRecord,
        quantity: str,
        *,
        external_id: str,
        identifier_id: UUID | None = None,
        occurred_at=WHEN,
        fee: tuple[InstrumentRecord, str] | None = None,
    ) -> TransactionRecord:
        contra = await transfer_contra(
            session,
            source=("connection", f"{bank.connection_id}:{external_id}"),
            own_account_id=bank.account.id,
            counterparty_identifier_id=identifier_id,
            other_postings=0,
        )
        amount = Decimal(quantity)
        record, _ = await run(
            upsert_connector_transaction,
            TransactionInput(
                occurred_at=occurred_at,
                description="Transfer",
                status="booked",
                origin="connector",
                connection_id=bank.connection_id,
                external_id=external_id,
                postings=[
                    PostingInput(bank.account.id, instrument.id, amount),
                    PostingInput(contra, instrument.id, -amount),
                ],
                counterparty=CounterpartyFields(identifier_id=identifier_id),
            ),
        )
        await session.execute(
            text("delete from finance.transfer_leg_fees where transaction_id = :t"),
            {"t": record.id},
        )
        if fee is not None:
            await session.execute(
                text(
                    "insert into finance.transfer_leg_fees (transaction_id, instrument_id, amount)"
                    " values (:t, :i, :a)"
                ),
                {"t": record.id, "i": fee[0].id, "a": Decimal(fee[1])},
            )
        return record

    return _post


async def in_transit(session: AsyncSession, ids: SystemIds) -> dict[UUID, Decimal]:
    """In transit's balance per instrument, zeros left out."""
    rows = await session.execute(
        text(
            "select instrument_id, quantity from finance.v1_balances_asof(:day)"
            " where account_id = :a"
        ),
        {"day": DAY + timedelta(days=30), "a": ids.in_transit},
    )
    return {r.instrument_id: r.quantity for r in rows if r.quantity != 0}


async def settlement_of(session: AsyncSession, transfer: TransferRecord) -> Counter[tuple]:
    """The settlement's postings: (account, instrument, quantity, price, price instrument,
    is_fee), checking it is a booked rule transaction dated as the sending record."""
    assert transfer.settlement_transaction_id is not None
    row = (
        await session.execute(
            text(
                "select s.origin, s.status, s.occurred_at = o.occurred_at as dated"
                " from finance.transactions s, finance.transactions o"
                " where s.id = :s and o.id = :o"
            ),
            {"s": transfer.settlement_transaction_id, "o": transfer.out_transaction_id},
        )
    ).one()
    assert (row.origin, row.status, row.dated) == ("rule", "booked", True)
    rows = await session.execute(
        text(
            "select account_id, instrument_id, quantity, price_per_unit, price_instrument_id,"
            " is_fee from finance.postings where transaction_id = :t"
        ),
        {"t": transfer.settlement_transaction_id},
    )
    return Counter(tuple(r) for r in rows)


async def live_rule_transactions(session: AsyncSession) -> int:
    return await session.scalar(
        text("select count(*) from finance.transactions where origin = 'rule' and status <> 'void'")
    )


async def decisions(session: AsyncSession) -> list[str]:
    return sorted(await session.scalars(text("select kind from finance.transfer_decisions")))


@pytest.mark.parametrize("fee", [None, "5.00"])
async def test_owner_pairs_usd_withdrawal_with_gbp_receipt(
    session, run, ids, connected_bank: MakeConnected, leg, heal, gbp, usd, fee
) -> None:
    """eToro Trading's "Withdraw Request" pays out USD, which arrives at a UK bank in GBP;
    neither side names the other. The owner pairs them: the conversion takes the legs' fee
    (none: 0, all exchange rate)."""
    trading = await connected_bank("eToro Trading")
    bank = await connected_bank("UK bank")
    sent = await leg(
        trading,
        usd,
        "-1000.00",
        external_id="withdraw:1",
        fee=None if fee is None else (usd, fee),
    )
    received = await leg(
        bank, gbp, "790.00", external_id="in:1", occurred_at=WHEN + timedelta(days=2)
    )
    await heal()
    assert await list_transfers(session) == []

    transfer = await run(pair_records, received.id, sent.id)

    assert (transfer.status, transfer.out_transaction_id, transfer.in_transaction_id) == (
        "matched",
        sent.id,
        received.id,
    )
    assert (transfer.instrument_id, transfer.amount) == (usd.id, Decimal("1000.00"))
    converted = Decimal("1000.00") if fee is None else Decimal("995.00")
    expected = [
        (
            ids.in_transit,
            usd.id,
            -converted,
            Decimal("790.00") / converted,
            gbp.id,
            False,
        ),
        (ids.in_transit, gbp.id, Decimal("790.00"), None, None, False),
    ]
    if fee is not None:
        expected += [
            (ids.in_transit, usd.id, Decimal("-5.00"), None, None, True),
            (ids.unassigned, usd.id, Decimal("5.00"), None, None, False),
        ]
    assert await settlement_of(session, transfer) == Counter(expected)
    assert await in_transit(session, ids) == {}
    assert await decisions(session) == ["pair"]
    await assert_consistent(session)
    assert await heal() == 0


async def test_owner_pairs_net_of_fee_same_currency(
    session, run, ids, connected_bank: MakeConnected, leg, heal, gbp
) -> None:
    """A deposit that arrives net of a platform fee: -100.00 sent, +99.30 received, so the
    0.70 difference is a fee on In transit against Unassigned."""
    bank = await connected_bank("Bank")
    platform = await connected_bank("Platform")
    sent = await leg(bank, gbp, "-100.00", external_id="out:1")
    received = await leg(platform, gbp, "99.30", external_id="deposit:1")

    transfer = await run(pair_records, sent.id, received.id)

    assert (transfer.status, transfer.out_transaction_id, transfer.in_transaction_id) == (
        "matched",
        sent.id,
        received.id,
    )
    assert (transfer.instrument_id, transfer.amount) == (gbp.id, Decimal("100.00"))
    assert await settlement_of(session, transfer) == Counter(
        [
            (ids.in_transit, gbp.id, Decimal("-0.70"), None, None, True),
            (ids.unassigned, gbp.id, Decimal("0.70"), None, None, False),
        ]
    )
    assert await in_transit(session, ids) == {}
    await assert_consistent(session)
    assert await heal() == 0


@pytest.mark.parametrize(("fee", "fits"), [("0.70", True), ("0.30", True), ("0.80", False)])
async def test_a_leg_fee_must_not_exceed_the_difference(
    session, run, ids, connected_bank: MakeConnected, leg, heal, gbp, fee, fits
) -> None:
    """A leg fee row up to the difference still leaves the whole difference the fee; one
    larger than it is refused, and nothing is stored."""
    bank = await connected_bank("Bank")
    platform = await connected_bank("Platform")
    sent = await leg(bank, gbp, "-100.00", external_id="out:1")
    received = await leg(platform, gbp, "99.30", external_id="deposit:1", fee=(gbp, fee))
    if not fits:
        with pytest.raises(FinanceValidationError, match="fee"):
            await run(pair_records, sent.id, received.id)
        assert await decisions(session) == []
        assert await list_transfers(session) == []
        return
    transfer = await run(pair_records, sent.id, received.id)
    assert (ids.in_transit, gbp.id, Decimal("-0.70"), None, None, True) in await settlement_of(
        session, transfer
    )
    assert await in_transit(session, ids) == {}
    assert await heal() == 0


async def test_receiving_side_larger_refused(
    session, ids, run, connected_bank: MakeConnected, leg, gbp
) -> None:
    bank = await connected_bank("Bank")
    platform = await connected_bank("Platform")
    sent = await leg(bank, gbp, "-100.00", external_id="out:1")
    received = await leg(platform, gbp, "100.70", external_id="deposit:1")
    with pytest.raises(FinanceValidationError, match="receiving side is larger"):
        await run(pair_records, sent.id, received.id)
    assert await decisions(session) == []


async def test_non_fiat_refused_422(
    session, ids, run, connected_bank: MakeConnected, leg, gbp, btc
) -> None:
    """Only two currencies may differ: a crypto (or any non-fiat) side of another amount or
    instrument is refused (FinanceValidationError is the API's 422)."""
    a = await connected_bank("A")
    b = await connected_bank("B")
    sent = await leg(a, btc, "-1.00000000", external_id="out:1")
    less = await leg(b, btc, "0.99000000", external_id="in:1")
    in_gbp = await leg(b, gbp, "50000.00", external_id="in:2")
    gbp_out = await leg(a, gbp, "-50000.00", external_id="out:2")
    btc_in = await leg(b, btc, "1.00000000", external_id="in:3")
    for first, second, message in (
        (sent.id, less.id, "opposite and equal"),
        (sent.id, in_gbp.id, "different instruments"),
        (gbp_out.id, btc_in.id, "different instruments"),
    ):
        with pytest.raises(FinanceValidationError, match=message):
            await run(pair_records, first, second)
    assert await decisions(session) == []
    # One instrument and equal amounts still pair, as before, whatever the instrument.
    transfer = await run(pair_records, sent.id, btc_in.id)
    assert (transfer.status, transfer.settlement_transaction_id) == ("matched", None)


async def test_same_direction_refused_422(
    session, ids, run, connected_bank: MakeConnected, leg, gbp, usd
) -> None:
    a = await connected_bank("A")
    b = await connected_bank("B")
    out_gbp = await leg(a, gbp, "-100.00", external_id="out:1")
    out_usd = await leg(b, usd, "-126.00", external_id="out:2")
    out_less = await leg(b, gbp, "-99.30", external_id="out:3")
    for other in (out_usd, out_less):
        with pytest.raises(FinanceValidationError, match="same direction"):
            await run(pair_records, out_gbp.id, other.id)
    on_a = await leg(a, usd, "126.00", external_id="in:1")
    with pytest.raises(FinanceValidationError, match="same account"):
        await run(pair_records, out_gbp.id, on_a.id)
    assert await decisions(session) == []


@pytest.mark.parametrize("differs", ["currency", "amount"])
async def test_unpair_voids_conversion(
    session, run, ids, connected_bank: MakeConnected, leg, heal, gbp, usd, differs
) -> None:
    a = await connected_bank("A")
    b = await connected_bank("B")
    sent = await leg(a, gbp, "-100.00", external_id="out:1")
    received = await leg(
        b,
        usd if differs == "currency" else gbp,
        "126.00" if differs == "currency" else "99.30",
        external_id="in:1",
    )
    transfer = await run(pair_records, sent.id, received.id)
    settlement = transfer.settlement_transaction_id
    assert settlement is not None

    await run(unpair, transfer.id)

    assert await live_rule_transactions(session) == 0
    assert await list_transfers(session) == []
    assert await in_transit(session, ids) == {}
    assert await decisions(session) == ["never_pair", "never_pair"]
    await assert_consistent(session)
    assert await heal() == 0
    # Still offered, so the unpair can be undone, which posts the settlement again.
    [offered] = await pair_candidates(session, sent.id)
    assert (offered.transaction_id, offered.differs) == (received.id, differs)
    again = await run(pair_records, sent.id, received.id)
    assert again.settlement_transaction_id is not None
    assert await decisions(session) == ["pair"]
    assert await in_transit(session, ids) == {}
    assert await heal() == 0


async def test_reject_removes_the_fee(
    session, run, ids, connected_bank: MakeConnected, leg, heal, gbp
) -> None:
    a = await connected_bank("A")
    b = await connected_bank("B")
    sent = await leg(a, gbp, "-100.00", external_id="out:1")
    received = await leg(b, gbp, "99.30", external_id="in:1")
    transfer = await run(pair_records, sent.id, received.id)
    await run(reject, transfer.id)
    assert await live_rule_transactions(session) == 0
    assert await in_transit(session, ids) == {}
    await assert_consistent(session)
    assert await heal() == 0


async def test_resync_rederives_the_settlement(
    session, run, ids, connected_bank: MakeConnected, leg, heal, gbp
) -> None:
    """The decision replays: a leg re-posted with another amount re-derives the fee; one
    re-posted larger than the sending side leaves the pair unmatched."""
    a = await connected_bank("A")
    b = await connected_bank("B")
    sent = await leg(a, gbp, "-100.00", external_id="out:1")
    received = await leg(b, gbp, "99.30", external_id="in:1")
    await run(pair_records, sent.id, received.id)

    await leg(b, gbp, "99.50", external_id="in:1")
    assert await heal() >= 1
    [transfer] = await list_transfers(session)
    assert transfer.status == "matched"
    assert (ids.in_transit, gbp.id, Decimal("-0.50"), None, None, True) in await settlement_of(
        session, transfer
    )
    assert await in_transit(session, ids) == {}
    await assert_consistent(session)
    assert await heal() == 0

    await leg(b, gbp, "100.00", external_id="in:1")
    assert await heal() >= 1
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.settlement_transaction_id) == ("matched", None)
    assert await live_rule_transactions(session) == 0
    assert await heal() == 0

    await leg(b, gbp, "100.10", external_id="in:1")
    assert await heal() >= 1
    assert await list_transfers(session) == []
    assert await live_rule_transactions(session) == 0
    assert await in_transit(session, ids) == {}
    await assert_consistent(session)
    assert await heal() == 0


async def test_candidates_mark_differs(
    session, ids, connected_bank: MakeConnected, leg, heal, gbp, usd, eur, btc
) -> None:
    """The equal ones first, as before; then the fiat records that differ, within 7 days,
    nearest booking date first, each marked; never one that pairing would refuse."""
    a = await connected_bank("A")
    b = await connected_bank("B")
    c = await connected_bank("C")
    subject = await leg(a, gbp, "-100.00", external_id="out:1")

    def at(days: int):
        return WHEN + timedelta(days=days)

    equal_far = await leg(b, gbp, "100.00", external_id="eq:far", occurred_at=at(30))
    amount_near = await leg(b, gbp, "99.30", external_id="amount:1", occurred_at=at(1))
    currency_nearer = await leg(c, usd, "126.00", external_id="usd:1", occurred_at=at(0))
    amount_edge = await leg(c, gbp, "99.00", external_id="amount:7", occurred_at=at(-7))
    # Left out: beyond 7 days, larger than sent, not fiat, the same direction, the same
    # account, and a fee that can't fit.
    await leg(b, gbp, "99.00", external_id="amount:8", occurred_at=at(8))
    await leg(b, gbp, "100.70", external_id="larger", occurred_at=at(1))
    await leg(b, btc, "0.00100000", external_id="btc", occurred_at=at(1))
    await leg(b, usd, "-126.00", external_id="usd:out", occurred_at=at(1))
    await leg(a, usd, "126.00", external_id="usd:own", occurred_at=at(1))
    await leg(c, usd, "127.00", external_id="usd:fee", occurred_at=at(1), fee=(eur, "1.00"))
    await heal()

    found = await pair_candidates(session, subject.id)

    assert [(x.transaction_id, x.differs) for x in found] == [
        (equal_far.id, None),
        (currency_nearer.id, "currency"),
        (amount_near.id, "amount"),
        (amount_edge.id, "amount"),
    ]
    # The cap of 20 is overall: the equal ones fill it first.
    for n in range(19):
        await leg(b, gbp, "100.00", external_id=f"eq:{n}", occurred_at=at(40 + n))
    capped = await pair_candidates(session, subject.id)
    assert len(capped) == 20
    assert [x.differs for x in capped] == [None] * 20
    # A non-fiat record gets its equal ones only.
    coin = await leg(a, btc, "-0.00200000", external_id="btc:out", occurred_at=at(1))
    assert await pair_candidates(session, coin.id) == []
    coin_in = await leg(c, btc, "0.00200000", external_id="btc:in", occurred_at=at(9))
    assert [(x.transaction_id, x.differs) for x in await pair_candidates(session, coin.id)] == [
        (coin_in.id, None)
    ]


async def test_pair_from_an_open_transfer_net_of_fee(
    session, run, ids, a_and_c, connector_tx, heal, gbp
) -> None:
    """A's payment names C and C's receipt names A, but it arrived net of a fee: the
    transfer is for review (amount_differs), and the owner pairs it from its candidates."""
    a, a_id, c, c_id = a_and_c
    out_record = await connector_tx(a, "-100.00", external_id="out_1", identifier_id=c_id)
    in_record = await connector_tx(c, "99.30", external_id="in_1", identifier_id=a_id)
    await heal()
    waiting = [t for t in await list_transfers(session) if t.out_transaction_id == out_record.id]
    [transfer] = waiting
    assert transfer.review_reason == "amount_differs"
    assert [(x.transaction_id, x.reason) for x in await candidates(session, transfer.id)] == [
        (in_record.id, "amount_differs")
    ]

    paired = await run(pair, transfer.id, in_record.id)

    assert (paired.status, paired.out_transaction_id, paired.in_transaction_id) == (
        "matched",
        out_record.id,
        in_record.id,
    )
    assert await settlement_of(session, paired) == Counter(
        [
            (ids.in_transit, gbp.id, Decimal("-0.70"), None, None, True),
            (ids.unassigned, gbp.id, Decimal("0.70"), None, None, False),
        ]
    )
    assert await list_transfers(session) == [paired]
    assert await in_transit(session, ids) == {}
    await assert_consistent(session)
    assert await heal() == 0


async def test_pair_from_an_open_transfer_refuses_larger_receipt(
    session, run, a_and_c, connector_tx, heal
) -> None:
    a, a_id, c, c_id = a_and_c
    out_record = await connector_tx(a, "-100.00", external_id="out_1", identifier_id=c_id)
    in_record = await connector_tx(c, "100.70", external_id="in_1", identifier_id=a_id)
    await heal()
    [transfer] = [t for t in await list_transfers(session) if t.out_transaction_id == out_record.id]
    with pytest.raises(FinanceValidationError, match="receiving side is larger"):
        await run(pair, transfer.id, in_record.id)
    assert await decisions(session) == []


async def test_a_decided_pair_outranks_the_cross_currency_tier(
    session, run, ids, connected_bank: MakeConnected, leg, heal, gbp, usd
) -> None:
    """eToro Money's GBP leg and Trading's USD leg would pair across currencies by
    themselves; the owner's pair of Money's leg with a bank's receipt net of a fee holds."""
    money = await connected_bank("eToro Money")
    trading = await connected_bank("eToro Trading")
    bank = await connected_bank("Bank")
    money_id = (await run(assign_identifier, money.account.id, "etoro_account", "1234567")).id
    trading_id = (await run(assign_identifier, trading.account.id, "etoro_account", "7654321")).id
    for account, instrument in ((money, gbp), (trading, usd)):
        await leg(
            account,
            instrument,
            "2000",
            external_id="opening",
            occurred_at=WHEN - timedelta(days=10),
        )
    sent = await leg(money, gbp, "-1000.00", external_id="cash:1", identifier_id=trading_id)
    across = await leg(trading, usd, "1260.00", external_id="activity:1", identifier_id=money_id)
    chosen = await leg(bank, gbp, "995.00", external_id="in:1")
    await session.execute(
        text(
            "insert into finance.transfer_decisions"
            " (kind, subject_kind, subject_ref, other_kind, other_ref)"
            " values ('pair', 'connection', :s, 'connection', :o)"
        ),
        {"s": f"{money.connection_id}:cash:1", "o": f"{bank.connection_id}:in:1"},
    )
    await heal()

    transfers = {
        (t.out_transaction_id, t.in_transaction_id): t for t in await list_transfers(session)
    }
    matched = transfers[(sent.id, chosen.id)]
    assert matched.status == "matched"
    assert (ids.in_transit, gbp.id, Decimal("-5.00"), None, None, True) in await settlement_of(
        session, matched
    )
    assert transfers[(None, across.id)].status == "awaiting"
    await assert_consistent(session)
    assert await heal() == 0
