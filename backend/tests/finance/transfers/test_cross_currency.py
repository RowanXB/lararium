"""Cross-currency pairing (eToro design §5.12): eToro Money's GBP leg and eToro Trading's USD
leg of one move, mutually identified and within the near window, are one transfer, and the
conversion between them, with its fee, is posted on In transit so that it nets to zero in
each currency."""

from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.modules.finance.identifiers import assign_identifier
from lararium.modules.finance.instruments import InstrumentRecord
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    TransactionRecord,
    upsert_connector_transaction,
)
from lararium.modules.finance.pricing import record_fx_rate
from lararium.modules.finance.system_accounts import system_account_ids
from lararium.modules.finance.transfers import (
    TransferRecord,
    list_transfers,
    load_transfer_settings,
    reject,
    transfer_contra,
    unpair,
)
from lararium.modules.finance.transfers import engine as transfer_engine
from lararium.modules.finance.transfers.pairs import pair_relation
from lararium.modules.finance.transfers.settings import DEFAULTS

from .conftest import (
    DAY,
    WHEN,
    Connected,
    MakeConnected,
    Run,
    SystemIds,
    assert_consistent,
    net_worth_on,
)

MONEY_ID = "1234567"
TRADING_ID = "7654321"


@dataclass(frozen=True)
class Etoro:
    money: Connected
    trading: Connected
    money_identifier: UUID  # eToro Money's etoro_account, which Trading's legs name
    trading_identifier: UUID  # eToro Trading's, which eToro Money's legs name


@pytest.fixture
async def etoro(run: Run, connected_bank: MakeConnected) -> Etoro:
    money = await connected_bank("eToro Money")
    trading = await connected_bank("eToro Trading")
    money_identifier = await run(assign_identifier, money.account.id, "etoro_account", MONEY_ID)
    trading_identifier = await run(
        assign_identifier, trading.account.id, "etoro_account", TRADING_ID
    )
    return Etoro(money, trading, money_identifier.id, trading_identifier.id)


@pytest.fixture
def leg(session: AsyncSession, run: Run):
    """Post (or re-post) a connector's transfer leg in its instrument, its contra from
    transfer_contra, with the leg fee its connector gives (transfer_leg_fees), if any."""

    async def _post(
        bank: Connected,
        instrument: InstrumentRecord,
        quantity: str,
        *,
        external_id: str,
        identifier_id: UUID | None,
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


async def histories(leg: Any, etoro: Etoro, gbp: InstrumentRecord, usd: InstrumentRecord) -> None:
    """A deposit on each account's connection, so both histories start well before WHEN."""
    for bank, instrument in ((etoro.money, gbp), (etoro.trading, usd)):
        await leg(
            bank,
            instrument,
            "2000",
            external_id="opening_deposit",
            identifier_id=None,
            occurred_at=WHEN - timedelta(days=10),
        )


async def in_transit(session: AsyncSession, ids: SystemIds, day=DAY) -> dict[UUID, Decimal]:
    """In transit's balance per instrument, zeros left out."""
    rows = await session.execute(
        text(
            "select instrument_id, quantity from finance.v1_balances_asof(:day)"
            " where account_id = :a"
        ),
        {"day": day, "a": ids.in_transit},
    )
    return {r.instrument_id: r.quantity for r in rows if r.quantity != 0}


async def conversion_of(session: AsyncSession, transfer: TransferRecord) -> Counter[tuple]:
    """The conversion's postings: (account, instrument, quantity, price, price instrument,
    is_fee), checking it is a booked rule transaction."""
    assert transfer.settlement_transaction_id is not None
    row = (
        await session.execute(
            text("select origin, status from finance.transactions where id = :t"),
            {"t": transfer.settlement_transaction_id},
        )
    ).one()
    assert (row.origin, row.status) == ("rule", "booked")
    rows = await session.execute(
        text(
            "select account_id, instrument_id, quantity, price_per_unit, price_instrument_id,"
            " is_fee from finance.postings where transaction_id = :t"
        ),
        {"t": transfer.settlement_transaction_id},
    )
    return Counter(tuple(r) for r in rows)


async def test_gbp_to_usd_with_fee(session, ids, etoro, leg, heal, gbp, usd) -> None:
    await histories(leg, etoro, gbp, usd)
    sent = await leg(
        etoro.money, gbp, "-1000.00", external_id="cash:1", identifier_id=etoro.trading_identifier
    )
    received = await leg(
        etoro.trading,
        usd,
        "1260.00",
        external_id="activity:1",
        identifier_id=etoro.money_identifier,
        fee=(usd, "6.30"),
    )
    assert await heal() >= 1
    [transfer] = [t for t in await list_transfers(session) if t.status == "matched"]
    assert (transfer.out_transaction_id, transfer.in_transaction_id) == (sent.id, received.id)
    assert (transfer.instrument_id, transfer.amount) == (gbp.id, Decimal("1000.00"))
    assert await conversion_of(session, transfer) == Counter(
        [
            (ids.in_transit, gbp.id, Decimal("-1000.00"), Decimal("1.2663"), usd.id, False),
            (ids.in_transit, usd.id, Decimal("1266.30"), None, None, False),
            (ids.in_transit, usd.id, Decimal("-6.30"), None, None, True),
            (ids.unassigned, usd.id, Decimal("6.30"), None, None, False),
        ]
    )
    assert await in_transit(session, ids) == {}
    assert len(await list_transfers(session)) == 1
    await assert_consistent(session)
    assert await heal() == 0


async def test_usd_to_gbp_with_fee(session, ids, etoro, leg, heal, gbp, usd) -> None:
    await histories(leg, etoro, gbp, usd)
    sent = await leg(
        etoro.trading,
        usd,
        "-1000.00",
        external_id="activity:1",
        identifier_id=etoro.money_identifier,
        fee=(usd, "5.00"),
    )
    received = await leg(
        etoro.money, gbp, "790.00", external_id="cash:1", identifier_id=etoro.trading_identifier
    )
    assert await heal() >= 1
    [transfer] = await list_transfers(session)
    assert transfer.status == "matched"
    assert (transfer.out_transaction_id, transfer.in_transaction_id) == (sent.id, received.id)
    assert await conversion_of(session, transfer) == Counter(
        [
            (
                ids.in_transit,
                usd.id,
                Decimal("-995.00"),
                Decimal("790.00") / Decimal("995.00"),
                gbp.id,
                False,
            ),
            (ids.in_transit, gbp.id, Decimal("790.00"), None, None, False),
            (ids.in_transit, usd.id, Decimal("-5.00"), None, None, True),
            (ids.unassigned, usd.id, Decimal("5.00"), None, None, False),
        ]
    )
    assert await in_transit(session, ids) == {}
    await assert_consistent(session)
    assert await heal() == 0


async def test_no_fee_row_fee_zero(session, ids, etoro, leg, heal, gbp, usd) -> None:
    await histories(leg, etoro, gbp, usd)
    await leg(
        etoro.money, gbp, "-1000.00", external_id="cash:1", identifier_id=etoro.trading_identifier
    )
    await leg(
        etoro.trading,
        usd,
        "1260.00",
        external_id="activity:1",
        identifier_id=etoro.money_identifier,
    )
    assert await heal() >= 1
    [transfer] = await list_transfers(session)
    assert transfer.status == "matched"
    assert await conversion_of(session, transfer) == Counter(
        [
            (ids.in_transit, gbp.id, Decimal("-1000.00"), Decimal("1.26"), usd.id, False),
            (ids.in_transit, usd.id, Decimal("1260.00"), None, None, False),
        ]
    )
    assert await in_transit(session, ids) == {}
    await assert_consistent(session)
    assert await heal() == 0


@pytest.mark.parametrize("money_first", [True, False])
async def test_either_arrival_order_same_result(
    session, ids, etoro, leg, heal, gbp, usd, money_first
) -> None:
    await histories(leg, etoro, gbp, usd)

    async def money():
        return await leg(
            etoro.money,
            gbp,
            "-1000.00",
            external_id="cash:1",
            identifier_id=etoro.trading_identifier,
        )

    async def trading():
        return await leg(
            etoro.trading,
            usd,
            "1260.00",
            external_id="activity:1",
            identifier_id=etoro.money_identifier,
            occurred_at=WHEN + timedelta(seconds=30),
            fee=(usd, "6.30"),
        )

    await (money() if money_first else trading())
    assert await heal() == 1
    [alone] = await list_transfers(session)
    assert (alone.status, alone.review_reason) == ("awaiting", None)
    await (trading() if money_first else money())
    assert await heal() >= 1
    [transfer] = await list_transfers(session)
    assert transfer.status == "matched"
    assert (transfer.instrument_id, transfer.amount) == (gbp.id, Decimal("1000.00"))
    assert len(await conversion_of(session, transfer)) == 4
    assert await in_transit(session, ids) == {}
    await assert_consistent(session)
    assert await heal() == 0


async def test_conversion_waits_for_statement(
    session, run, ids, etoro, leg, heal, gbp, usd
) -> None:
    """The GBP leg alone for 3 days: In transit holds it and net worth (at a fixed rate)
    doesn't move; once the statement's USD leg arrives, net worth differs by exactly the
    fee and the rate effect."""
    rate = Decimal("0.8")  # GBP per USD, fixed
    await run(
        record_fx_rate,
        base_instrument_id=usd.id,
        quote_instrument_id=gbp.id,
        rate=rate,
        as_of_date=WHEN.date() - timedelta(days=30),
    )
    await histories(leg, etoro, gbp, usd)
    days = [WHEN.date() + timedelta(days=n) for n in range(4)]
    before = {day: await net_worth_on(session, day) for day in days}
    assert len(set(before.values())) == 1
    [worth] = set(before.values())
    await leg(
        etoro.money, gbp, "-1000.00", external_id="cash:1", identifier_id=etoro.trading_identifier
    )
    assert await heal() == 1
    for day in days:
        assert await net_worth_on(session, day) == worth
        assert await in_transit(session, ids, day) == {gbp.id: Decimal("1000.00")}
    # Three days later the statement is uploaded: its row has the transfer's own time.
    await leg(
        etoro.trading,
        usd,
        "1260.00",
        external_id="activity:1",
        identifier_id=etoro.money_identifier,
        fee=(usd, "6.30"),
    )
    assert await heal() >= 1
    received, fee = Decimal("1260.00"), Decimal("6.30")
    rate_effect = (received + fee) * rate - Decimal("1000.00")
    for day in days:
        assert await in_transit(session, ids, day) == {}
        assert await net_worth_on(session, day) == worth + rate_effect - fee * rate
    await assert_consistent(session)


async def test_two_candidates_left_to_owner(session, ids, etoro, leg, heal, gbp, usd) -> None:
    await histories(leg, etoro, gbp, usd)
    await leg(
        etoro.money, gbp, "-1000.00", external_id="cash:1", identifier_id=etoro.trading_identifier
    )
    for n, amount in enumerate(("1260.00", "1255.00")):
        await leg(
            etoro.trading,
            usd,
            amount,
            external_id=f"activity:{n}",
            identifier_id=etoro.money_identifier,
            occurred_at=WHEN + timedelta(seconds=n),
        )
    await heal()
    transfers = await list_transfers(session)
    assert len(transfers) == 3
    assert {(t.status, t.review_reason) for t in transfers} == {("awaiting", "several_candidates")}
    assert all(t.settlement_transaction_id is None for t in transfers)
    await assert_consistent(session)
    assert await heal() == 0


async def test_outside_the_near_window_not_paired(session, ids, etoro, leg, heal, gbp, usd) -> None:
    await histories(leg, etoro, gbp, usd)
    await leg(
        etoro.money, gbp, "-1000.00", external_id="cash:1", identifier_id=etoro.trading_identifier
    )
    await leg(
        etoro.trading,
        usd,
        "1260.00",
        external_id="activity:1",
        identifier_id=etoro.money_identifier,
        occurred_at=WHEN + DEFAULTS.near_window + timedelta(seconds=1),
    )
    await heal()
    transfers = await list_transfers(session)
    assert {(t.status, t.review_reason) for t in transfers} == {("awaiting", None)}
    assert len(transfers) == 2
    await assert_consistent(session)


async def test_same_direction_or_one_sided_not_paired(
    session, ids, etoro, leg, heal, gbp, usd
) -> None:
    """Both must be identified naming each other's account, with opposite signs."""
    await histories(leg, etoro, gbp, usd)
    await leg(
        etoro.money, gbp, "-1000.00", external_id="cash:1", identifier_id=etoro.trading_identifier
    )
    # Trading's leg names nobody: a deposit from somewhere else at the same second.
    await leg(etoro.trading, usd, "1260.00", external_id="activity:1", identifier_id=None)
    # Same direction as eToro Money's.
    await leg(
        etoro.trading,
        usd,
        "-1260.00",
        external_id="activity:2",
        identifier_id=etoro.money_identifier,
    )
    await heal()
    assert all(t.status == "awaiting" for t in await list_transfers(session))
    await assert_consistent(session)


async def test_unpair_removes_conversion(session, run, ids, etoro, leg, heal, gbp, usd) -> None:
    await histories(leg, etoro, gbp, usd)
    await leg(
        etoro.money, gbp, "-1000.00", external_id="cash:1", identifier_id=etoro.trading_identifier
    )
    await leg(
        etoro.trading,
        usd,
        "1260.00",
        external_id="activity:1",
        identifier_id=etoro.money_identifier,
        fee=(usd, "6.30"),
    )
    await heal()
    [transfer] = await list_transfers(session)
    conversion = transfer.settlement_transaction_id
    assert conversion is not None
    await run(unpair, transfer.id)
    left = await session.scalar(
        text("select count(*) from finance.transactions where id = :t and status <> 'void'"),
        {"t": conversion},
    )
    assert left == 0
    transfers = await list_transfers(session)
    assert {(t.status, t.settlement_transaction_id) for t in transfers} == {("awaiting", None)}
    assert await in_transit(session, ids) == {
        gbp.id: Decimal("1000.00"),
        usd.id: Decimal("-1260.00"),
    }
    await assert_consistent(session)
    assert await heal() == 0


async def test_resync_rederives_the_conversion(session, ids, etoro, leg, heal, gbp, usd) -> None:
    """A leg re-posted with another amount, or a fee that changed, re-derives the
    conversion; heal after that writes nothing."""
    await histories(leg, etoro, gbp, usd)
    await leg(
        etoro.money, gbp, "-1000.00", external_id="cash:1", identifier_id=etoro.trading_identifier
    )

    async def trading(amount: str, fee: str | None):
        return await leg(
            etoro.trading,
            usd,
            amount,
            external_id="activity:1",
            identifier_id=etoro.money_identifier,
            fee=None if fee is None else (usd, fee),
        )

    await trading("1260.00", "6.30")
    await heal()
    await trading("1270.00", "6.30")
    assert await heal() >= 1
    [transfer] = await list_transfers(session)
    assert transfer.status == "matched"
    assert (ids.in_transit, usd.id, Decimal("1276.30"), None, None, False) in await conversion_of(
        session, transfer
    )
    assert await in_transit(session, ids) == {}
    assert await heal() == 0

    await trading("1270.00", "7.00")
    assert await heal() >= 1
    [transfer] = await list_transfers(session)
    conversion = await conversion_of(session, transfer)
    assert (ids.in_transit, usd.id, Decimal("-7.00"), None, None, True) in conversion
    assert (ids.in_transit, usd.id, Decimal("1277.00"), None, None, False) in conversion
    assert await in_transit(session, ids) == {}
    await assert_consistent(session)
    assert await heal() == 0

    await trading("1270.00", None)
    assert await heal() >= 1
    [transfer] = await list_transfers(session)
    assert len(await conversion_of(session, transfer)) == 2
    assert await in_transit(session, ids) == {}
    await assert_consistent(session)
    assert await heal() == 0


@pytest.mark.parametrize("fee", ["eur", "too_large"])
async def test_a_fee_that_cannot_be_the_conversions_is_left_for_review(
    session, ids, etoro, leg, heal, gbp, usd, eur, fee
) -> None:
    """A fee in a third currency, or larger than the amount, is a data error: the pair is
    left unmatched, for review."""
    await histories(leg, etoro, gbp, usd)
    await leg(
        etoro.trading,
        usd,
        "-1000.00",
        external_id="activity:1",
        identifier_id=etoro.money_identifier,
        fee=(eur, "5.00") if fee == "eur" else (usd, "1000.00"),
    )
    await leg(
        etoro.money, gbp, "790.00", external_id="cash:1", identifier_id=etoro.trading_identifier
    )
    await heal()
    transfers = await list_transfers(session)
    assert len(transfers) == 2
    assert {(t.status, t.review_reason) for t in transfers} == {("awaiting", "amount_differs")}
    await assert_consistent(session)
    assert await heal() == 0


async def rule_transactions(session: AsyncSession) -> int:
    """How many derived transactions booked by rule (conversions, settlements) remain."""
    return await session.scalar(
        text("select count(*) from finance.transactions where origin = 'rule'")
    )


async def test_same_currency_candidate_takes_precedence(
    session, ids, etoro, leg, heal, gbp, usd
) -> None:
    """A record with a candidate of the same amount in its own currency and one in another
    pairs with the former; the one across is left alone, for review (several_candidates:
    its only candidate is paired elsewhere, the safe side)."""
    await histories(leg, etoro, gbp, usd)
    sent = await leg(
        etoro.money, gbp, "-1000.00", external_id="cash:1", identifier_id=etoro.trading_identifier
    )
    same = await leg(
        etoro.trading,
        gbp,
        "1000.00",
        external_id="activity:gbp",
        identifier_id=etoro.money_identifier,
    )
    across = await leg(
        etoro.trading,
        usd,
        "1260.00",
        external_id="activity:usd",
        identifier_id=etoro.money_identifier,
        fee=(usd, "6.30"),
    )
    await heal()
    transfers = {
        (t.out_transaction_id, t.in_transaction_id): t for t in await list_transfers(session)
    }
    matched = transfers[(sent.id, same.id)]
    assert (matched.status, matched.settlement_transaction_id) == ("matched", None)
    alone = transfers[(None, across.id)]
    assert (alone.status, alone.review_reason) == ("awaiting", "several_candidates")
    assert await rule_transactions(session) == 0
    assert await in_transit(session, ids) == {usd.id: Decimal("-1260.00")}
    await assert_consistent(session)
    assert await heal() == 0


async def test_reject_removes_conversion_and_keeps_the_pair_apart(
    session, run, ids, etoro, leg, heal, gbp, usd
) -> None:
    await histories(leg, etoro, gbp, usd)
    await leg(
        etoro.money, gbp, "-1000.00", external_id="cash:1", identifier_id=etoro.trading_identifier
    )
    await leg(
        etoro.trading,
        usd,
        "1260.00",
        external_id="activity:1",
        identifier_id=etoro.money_identifier,
        fee=(usd, "6.30"),
    )
    await heal()
    [transfer] = await list_transfers(session)
    assert transfer.settlement_transaction_id is not None
    await run(reject, transfer.id)
    assert await heal() == 0
    [rejected] = await list_transfers(session)
    assert (rejected.id, rejected.status, rejected.settlement_transaction_id) == (
        transfer.id,
        "rejected",
        None,
    )
    assert await rule_transactions(session) == 0
    assert await in_transit(session, ids) == {}
    await assert_consistent(session)


async def test_a_leg_changed_under_matching_is_left_for_the_next_heal(
    session, run, ids, etoro, leg, heal, gbp, usd
) -> None:
    """The pass's relation took the pair, but a sync then re-posts the sending leg below
    its fee: matching with that relation links nothing and posts no conversion, and the
    next heal leaves the pair for review."""
    await histories(leg, etoro, gbp, usd)
    sent = await leg(
        etoro.trading,
        usd,
        "-1000.00",
        external_id="activity:1",
        identifier_id=etoro.money_identifier,
        fee=(usd, "5.00"),
    )
    received = await leg(
        etoro.money, gbp, "790.00", external_id="cash:1", identifier_id=etoro.trading_identifier
    )

    async def relation_now(ctx):
        settings = await load_transfer_settings(ctx.session)
        return await pair_relation(ctx.session, await system_account_ids(ctx.session), settings)

    relation = await run(relation_now)
    assert relation.partner(sent.id) == received.id
    await leg(
        etoro.trading,
        usd,
        "-4.00",
        external_id="activity:1",
        identifier_id=etoro.money_identifier,
        fee=(usd, "5.00"),
    )

    async def match_with_the_stale_relation(ctx) -> None:
        settings = await load_transfer_settings(ctx.session)
        system = await transfer_engine._system(ctx)
        await transfer_engine._match(ctx, received.id, settings, system, relation, {})

    await run(match_with_the_stale_relation)  # does not raise
    assert not [t for t in await list_transfers(session) if t.status == "matched"]
    assert await rule_transactions(session) == 0

    await heal()
    transfers = await list_transfers(session)
    assert {(t.status, t.review_reason) for t in transfers} == {("awaiting", "amount_differs")}
    await assert_consistent(session)
    assert await heal() == 0
