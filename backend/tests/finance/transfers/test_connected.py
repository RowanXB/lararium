import asyncio
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, text

from lararium.core.actors import Actor
from lararium.core.commands import CommandContext, command_context
from lararium.core.connections import create_connection
from lararium.db import create_engine, make_sessionmaker
from lararium.modules.finance import models
from lararium.modules.finance.accounts import create_account
from lararium.modules.finance.identifiers import assign_identifier
from lararium.modules.finance.instruments import get_currency
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    upsert_connector_transaction,
    void_transaction,
)
from lararium.modules.finance.system_accounts import (
    get_or_create_system_account,
    system_account_ids,
)
from lararium.modules.finance.transfers import (
    candidates,
    find_transfer_violations,
    heal_transfers,
    list_transfers,
    load_transfer_settings,
    transfer_contra,
)
from lararium.modules.finance.transfers import engine as transfer_engine
from lararium.modules.finance.transfers.pairs import pair_relation

from .conftest import (
    A_IDENTIFIER,
    C_IDENTIFIER,
    WHEN,
    assert_consistent,
    contra_account,
    history,
    in_transit_balance,
    net_worth_on,
    placeholders,
    postings_of,
)


@pytest.mark.parametrize("sender_first", [True, False])
async def test_connected_both_orders_same_result(
    session, ids, a_and_c, connector_tx, heal, sender_first
) -> None:
    a, a_id, c, c_id = a_and_c
    worth = await net_worth_on(session)

    async def send():
        return await connector_tx(a, "-100", external_id="out_1", identifier_id=c_id)

    async def receive():
        return await connector_tx(
            c, "100", external_id="in_1", occurred_at=WHEN + timedelta(hours=2), identifier_id=a_id
        )

    first = await (send() if sender_first else receive())
    assert await heal() == 1
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.review_reason) == ("awaiting", None)
    assert await in_transit_balance(session, ids) == (
        Decimal(100) if sender_first else Decimal(-100)
    )
    assert await net_worth_on(session) == worth

    second = await (receive() if sender_first else send())
    assert await heal() == 1
    out_record, in_record = (first, second) if sender_first else (second, first)
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.review_reason) == ("matched", None)
    assert (transfer.from_account_id, transfer.to_account_id, transfer.amount) == (
        a.account.id,
        c.account.id,
        Decimal(100),
    )
    assert (transfer.out_transaction_id, transfer.in_transaction_id) == (
        out_record.id,
        in_record.id,
    )
    assert transfer.matched_at is not None
    # Opened when its earliest record occurred, whichever synced first.
    assert transfer.opened_at == WHEN
    assert await contra_account(session, out_record.id, a.account.id) == ids.in_transit
    assert await contra_account(session, in_record.id, c.account.id) == ids.in_transit
    assert await in_transit_balance(session, ids) == 0
    assert await net_worth_on(session) == worth
    assert await placeholders(session) == []
    await assert_consistent(session)
    assert await heal() == 0


@pytest.mark.parametrize("receiver_offset", [timedelta(hours=2), timedelta(hours=-1)])
@pytest.mark.parametrize("order", ["sender_first", "receiver_first", "one_heal"])
async def test_opened_at_is_the_earliest_record(
    session, a_and_c, connector_tx, heal, order, receiver_offset
) -> None:
    a, a_id, c, c_id = a_and_c

    async def send():
        await connector_tx(a, "-100", external_id="out_1", identifier_id=c_id)

    async def receive():
        await connector_tx(
            c, "100", external_id="in_1", occurred_at=WHEN + receiver_offset, identifier_id=a_id
        )

    if order == "one_heal":
        await send()
        await receive()
    else:
        for post in (send, receive) if order == "sender_first" else (receive, send):
            await post()
            await heal()
    await heal()

    [transfer] = await list_transfers(session)
    assert transfer.status == "matched"
    assert transfer.opened_at == min(WHEN, WHEN + receiver_offset)
    assert await heal() == 0


@pytest.mark.parametrize("sender_first", [True, False])
async def test_receiver_without_identifier_matched_by_fallback(
    session, ids, a_and_c, connector_tx, heal, sender_first
) -> None:
    a, _, c, c_id = a_and_c

    async def send():
        return await connector_tx(a, "-100", external_id="out_1", identifier_id=c_id)

    async def receive():
        # A broker's deposit, say: no counterparty bank account, but a bank transfer.
        return await connector_tx(c, "100", external_id="in_1", occurred_at=WHEN + timedelta(1))

    if sender_first:
        out_record = await send()
        assert await heal() == 1
        in_record = await receive()
        assert await contra_account(session, in_record.id, c.account.id) == ids.unassigned
    else:
        in_record = await receive()
        assert await heal() == 0
        out_record = await send()
    assert await heal() == 1

    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.review_reason) == ("matched", None)
    assert (transfer.out_transaction_id, transfer.in_transaction_id) == (
        out_record.id,
        in_record.id,
    )
    assert await contra_account(session, in_record.id, c.account.id) == ids.in_transit
    assert await in_transit_balance(session, ids) == 0
    await assert_consistent(session)
    assert await heal() == 0
    # The next sync computes the same contra, so it writes nothing.
    assert (
        await transfer_contra(
            session,
            source=("connection", f"{c.connection_id}:in_1"),
            own_account_id=c.account.id,
            counterparty_identifier_id=None,
            other_postings=0,
        )
        == ids.in_transit
    )


@pytest.mark.parametrize("sender_first", [True, False])
async def test_card_refund_same_amount_not_linked(
    session, ids, a_and_c, connector_tx, heal, sender_first
) -> None:
    a, _, c, c_id = a_and_c
    if sender_first:
        await connector_tx(a, "-100", external_id="out_1", identifier_id=c_id)
        assert await heal() == 1
    refund = await connector_tx(
        c, "100", external_id="refund_1", occurred_at=WHEN + timedelta(1), bank_transfer=False
    )
    if not sender_first:
        assert await heal() == 0
        await connector_tx(a, "-100", external_id="out_1", identifier_id=c_id)
    assert await heal() == (1 if not sender_first else 0)

    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.review_reason) == ("awaiting", None)
    assert transfer.in_transaction_id is None
    assert await contra_account(session, refund.id, c.account.id) == ids.unassigned
    await assert_consistent(session)


@pytest.mark.parametrize("sender_first", [True, False])
async def test_never_pair_keeps_records_apart(
    session, ids, a_and_c, connector_tx, heal, sender_first
) -> None:
    a, _, c, c_id = a_and_c
    # Unpairing them is a later task; the engine only has to honour the decision.
    await session.execute(
        insert(models.TransferDecision).values(
            kind="never_pair",
            subject_kind="connection",
            subject_ref=f"{a.connection_id}:out_1",
            other_kind="connection",
            other_ref=f"{c.connection_id}:in_1",
        )
    )

    async def send():
        return await connector_tx(a, "-100", external_id="out_1", identifier_id=c_id)

    async def receive():
        return await connector_tx(c, "100", external_id="in_1", occurred_at=WHEN + timedelta(1))

    if sender_first:
        await send()
        assert await heal() == 1
        received = await receive()
        assert await heal() == 0
    else:
        received = await receive()
        assert await heal() == 0
        await send()
        assert await heal() == 1

    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.review_reason, transfer.in_transaction_id) == (
        "awaiting",
        None,
        None,
    )
    assert await contra_account(session, received.id, c.account.id) == ids.unassigned
    # Offered only as the way back from the decision.
    assert [(x.transaction_id, x.reason) for x in await candidates(session, transfer.id)] == [
        (received.id, "unpaired")
    ]
    await assert_consistent(session)


async def test_never_pair_with_another_record_still_links(
    session, ids, a_and_c, connector_tx, heal
) -> None:
    a, _, c, c_id = a_and_c
    await history(connector_tx, c)
    await session.execute(
        insert(models.TransferDecision).values(
            kind="never_pair",
            subject_kind="connection",
            subject_ref=f"{a.connection_id}:out_1",
            other_kind="connection",
            other_ref=f"{c.connection_id}:card_1",
        )
    )
    out_record = await connector_tx(a, "-100", external_id="out_1", identifier_id=c_id)
    assert await heal() == 1
    in_record = await connector_tx(c, "100", external_id="in_1", occurred_at=WHEN + timedelta(1))
    assert await heal() == 1

    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.out_transaction_id, transfer.in_transaction_id) == (
        "matched",
        out_record.id,
        in_record.id,
    )
    await assert_consistent(session)


async def test_outside_window_not_linked(session, ids, a_and_c, connector_tx, heal) -> None:
    a, _, c, c_id = a_and_c
    await history(connector_tx, c)
    await connector_tx(a, "-100", external_id="out_1", identifier_id=c_id)
    assert await heal() == 1
    late = await connector_tx(c, "100", external_id="in_1", occurred_at=WHEN + timedelta(days=8))
    assert await heal() == 0

    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.in_transaction_id) == ("awaiting", None)
    assert await contra_account(session, late.id, c.account.id) == ids.unassigned
    await assert_consistent(session)


async def test_two_candidates_need_review(session, ids, a_and_c, connector_tx, heal) -> None:
    a, _, c, c_id = a_and_c
    await history(connector_tx, c)
    loose = [
        await connector_tx(
            c,
            "100",
            external_id=f"in_{n}",
            occurred_at=WHEN + timedelta(hours=n),
            description=f"Deposit {n}",
        )
        for n in (1, 2)
    ]
    assert await heal() == 0
    out_record = await connector_tx(a, "-100", external_id="out_1", identifier_id=c_id)
    assert await heal() == 1

    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.review_reason) == ("awaiting", "several_candidates")
    assert (transfer.out_transaction_id, transfer.in_transaction_id) == (out_record.id, None)
    # Nothing re-pointed on the candidates: they are not legs until the owner pairs one.
    for record in loose:
        assert await contra_account(session, record.id, c.account.id) == ids.unassigned
    assert await in_transit_balance(session, ids) == Decimal(100)
    found = await candidates(session, transfer.id)
    assert [(f.transaction_id, f.account_id, f.quantity, f.reason) for f in found] == [
        (record.id, c.account.id, Decimal(100), "exact") for record in loose
    ]
    assert [(f.occurred_at, f.description) for f in found] == [
        (record.occurred_at, record.description) for record in loose
    ]
    await assert_consistent(session)
    # The loose records' fallback is just as ambiguous, so heal leaves them be.
    assert await heal() == 0


async def test_fallback_with_two_transfers_flags_both(
    session, ids, a_and_c, connector_tx, heal
) -> None:
    a, _, c, c_id = a_and_c
    await history(connector_tx, c)
    for n in (1, 2):
        await connector_tx(
            a,
            "-100",
            external_id=f"out_{n}",
            occurred_at=WHEN + timedelta(hours=n),
            identifier_id=c_id,
        )
    assert await heal() == 2
    record = await connector_tx(c, "100", external_id="in_1", occurred_at=WHEN + timedelta(1))
    # Each transfer C's record could complete is matched again, now for review.
    assert await heal() == 2

    transfers = await list_transfers(session)
    assert [(t.status, t.review_reason) for t in transfers] == [
        ("awaiting", "several_candidates")
    ] * 2
    assert await contra_account(session, record.id, c.account.id) == ids.unassigned
    for transfer in transfers:
        [found] = await candidates(session, transfer.id)
        assert (found.transaction_id, found.reason) == (record.id, "exact")
    await assert_consistent(session)
    assert await heal() == 0


async def test_different_amount_needs_review(session, ids, a_and_c, connector_tx, heal) -> None:
    a, a_id, c, c_id = a_and_c
    await history(connector_tx, c)
    out_record = await connector_tx(a, "-100", external_id="out_1", identifier_id=c_id)
    assert await heal() == 1
    in_record = await connector_tx(
        c, "90", external_id="in_1", occurred_at=WHEN + timedelta(1), identifier_id=a_id
    )
    # C's record opens its transfer for review, and A's is matched again to join it.
    assert await heal() == 2

    sent, received = sorted(await list_transfers(session), key=lambda t: t.opened_at)
    assert (sent.status, sent.review_reason, sent.out_transaction_id) == (
        "awaiting",
        "amount_differs",
        out_record.id,
    )
    assert (received.status, received.review_reason, received.in_transaction_id) == (
        "awaiting",
        "amount_differs",
        in_record.id,
    )
    [found] = await candidates(session, received.id)
    assert (found.transaction_id, found.account_id, found.quantity, found.reason) == (
        out_record.id,
        a.account.id,
        Decimal(-100),
        "amount_differs",
    )
    # Both sit in In transit, so net worth does not move during review.
    assert await in_transit_balance(session, ids) == Decimal(10)
    await assert_consistent(session)
    assert await heal() == 0


async def test_before_history_settles_against_opening_balances(
    session, ids, a_and_c, connector_tx, heal
) -> None:
    a, a_id, c, c_id = a_and_c
    await history(connector_tx, c, days_before=0)
    sent_at = WHEN - timedelta(days=30)
    out_record = await connector_tx(
        a, "-100", external_id="out_1", occurred_at=sent_at, identifier_id=c_id
    )
    assert await heal() == 1

    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.review_reason) == ("before_history", None)
    [settlement] = await placeholders(session, origin="rule")
    assert transfer.settlement_transaction_id == settlement.id
    assert (settlement.description, settlement.status) == ("Transfer before C's history", "booked")
    assert (settlement.occurred_at, settlement.booking_date) == (
        out_record.occurred_at,
        out_record.booking_date,
    )
    assert await postings_of(session, settlement.id) == {
        (ids.in_transit, Decimal(-100)),
        (ids.opening_balances, Decimal(100)),
    }
    assert await in_transit_balance(session, ids) == 0
    await assert_consistent(session)
    assert await heal() == 0

    # Older history is fetched later, and the counterpart is in it.
    in_record = await connector_tx(
        c, "100", external_id="in_1", occurred_at=sent_at + timedelta(hours=1), identifier_id=a_id
    )
    assert await heal() >= 1
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.settlement_transaction_id) == ("matched", None)
    assert (transfer.out_transaction_id, transfer.in_transaction_id) == (
        out_record.id,
        in_record.id,
    )
    assert await placeholders(session, origin="rule") == []
    assert await in_transit_balance(session, ids) == 0
    await assert_consistent(session)
    assert await heal() == 0


async def test_opening_adjustment_does_not_start_the_history(
    session, run, gbp, ids, a_and_c, connector_tx, heal
) -> None:
    """A connector dates its opening balance adjustment a day before the history it
    accounts for. A transfer whose counterpart window ends between the two is already in the
    opening balance, so it is before_history, not awaited."""
    a, _, c, c_id = a_and_c
    await history(connector_tx, c, days_before=0)  # C's first real record, at WHEN
    await run(
        upsert_connector_transaction,
        TransactionInput(
            occurred_at=WHEN - timedelta(days=1),
            description="Opening balance",
            origin="connector",
            connection_id=c.connection_id,
            external_id="opening:acc_c",
            postings=[
                PostingInput(c.account.id, gbp.id, Decimal(500)),
                PostingInput(ids.opening_balances, gbp.id, Decimal(-500)),
            ],
        ),
    )
    # The counterpart could occur until 7 days after: 12 hours before WHEN, after the
    # opening adjustment.
    sent_at = WHEN - timedelta(days=7, hours=12)
    await connector_tx(a, "-100", external_id="out_1", occurred_at=sent_at, identifier_id=c_id)
    assert await heal() == 1

    [transfer] = await list_transfers(session)
    assert transfer.status == "before_history"
    assert await in_transit_balance(session, ids) == 0
    await assert_consistent(session)
    assert await heal() == 0


async def test_older_history_without_the_counterpart_reopens_awaiting(
    session, ids, a_and_c, connector_tx, heal
) -> None:
    a, _, c, c_id = a_and_c
    await history(connector_tx, c, days_before=0)
    sent_at = WHEN - timedelta(days=30)
    await connector_tx(a, "-100", external_id="out_1", occurred_at=sent_at, identifier_id=c_id)
    assert await heal() == 1
    [transfer] = await list_transfers(session)
    assert transfer.status == "before_history"

    # C's history now starts before the transfer, so its counterpart may yet arrive.
    await connector_tx(
        c, "-5", external_id="card_0", occurred_at=sent_at - timedelta(1), bank_transfer=False
    )
    assert await heal() == 1
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.settlement_transaction_id) == ("awaiting", None)
    assert await placeholders(session, origin="rule") == []
    assert await in_transit_balance(session, ids) == Decimal(100)
    await assert_consistent(session)
    assert await heal() == 0


async def test_connected_account_without_history_awaits(
    session, a_and_c, connector_tx, heal
) -> None:
    # C has synced nothing yet: its history is unknown, not after the transfer.
    a, _, _, c_id = a_and_c
    await connector_tx(
        a, "-100", external_id="out_1", occurred_at=WHEN - timedelta(days=30), identifier_id=c_id
    )
    assert await heal() == 1
    [transfer] = await list_transfers(session)
    assert transfer.status == "awaiting"


async def test_voided_settlement_is_a_violation_and_healed(
    session, ids, a_and_c, connector_tx, heal
) -> None:
    a, _, c, c_id = a_and_c
    await history(connector_tx, c, days_before=0)
    await connector_tx(
        a, "-100", external_id="out_1", occurred_at=WHEN - timedelta(days=30), identifier_id=c_id
    )
    assert await heal() == 1
    [settlement] = await placeholders(session, origin="rule")

    await session.execute(
        text("update finance.transactions set status = 'void' where id = :t"),
        {"t": settlement.id},
    )
    assert await find_transfer_violations(session) == [settlement.id]
    assert await in_transit_balance(session, ids) == Decimal(100)

    assert await heal() == 1
    [fresh] = await placeholders(session, origin="rule")
    assert fresh.status == "booked"
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.settlement_transaction_id) == ("before_history", fresh.id)
    assert await in_transit_balance(session, ids) == 0
    await assert_consistent(session)


async def test_missing_settlement_is_a_violation_and_healed(
    session, ids, a_and_c, connector_tx, heal
) -> None:
    a, _, c, c_id = a_and_c
    await history(connector_tx, c, days_before=0)
    out_record = await connector_tx(
        a, "-100", external_id="out_1", occurred_at=WHEN - timedelta(days=30), identifier_id=c_id
    )
    assert await heal() == 1
    [settlement] = await placeholders(session, origin="rule")

    # Raw SQL stands in for anything that loses the settlement past the engine.
    await session.execute(text("update finance.transfers set settlement_transaction_id = null"))
    await session.execute(
        text("delete from finance.transactions where id = :t"), {"t": settlement.id}
    )
    assert await find_transfer_violations(session) == [out_record.id]

    assert await heal() == 1
    [fresh] = await placeholders(session, origin="rule")
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.settlement_transaction_id) == ("before_history", fresh.id)
    assert await in_transit_balance(session, ids) == 0
    await assert_consistent(session)


async def test_candidates_of_a_matched_transfer_are_none(
    session, a_and_c, connector_tx, heal
) -> None:
    a, a_id, c, c_id = a_and_c
    await connector_tx(a, "-100", external_id="out_1", identifier_id=c_id)
    await connector_tx(c, "100", external_id="in_1", identifier_id=a_id)
    assert await heal() >= 1
    [transfer] = await list_transfers(session)
    assert transfer.status == "matched"
    assert await candidates(session, transfer.id) == []


async def test_concurrent_heals_link_once(backend_url: str, owner_url: str, migrated: None) -> None:
    # Two heals (after syncs of A's and C's connections) run at once, with real commits.
    engine, owner = create_engine(backend_url), create_engine(owner_url)
    sessions = make_sessionmaker(engine)
    actor = Actor("connector", "test")
    command = "test.concurrent_heal"
    async with owner.connect() as conn:
        started = await conn.scalar(text("select clock_timestamp()"))
        existing = set(
            await conn.scalars(
                text("select key from core.settings where key like 'finance.system_account.%'")
            )
        )

    async def run(fn, *args, **kwargs):
        async with sessions() as s, command_context(s, actor, command, {}) as ctx:
            return await fn(ctx, *args, **kwargs)

    async def bank(name: str, value: str) -> tuple[UUID, UUID, UUID]:
        connection = await run(create_connection, connector="monzo", display_name=name, config={})
        account = await run(
            create_account,
            name=name,
            ledger_type="asset",
            subtype="bank",
            balance_mode="transactions",
            source_connection_id=connection.id,
        )
        identifier = await run(assign_identifier, account.id, "uk_bank_account", value)
        return connection.id, account.id, identifier.id

    async def post(connection_id, account_id, quantity: str, identifier_id: UUID) -> None:
        async with sessions() as s, command_context(s, actor, command, {}) as ctx:
            gbp = await get_currency(s, "GBP")
            contra = await transfer_contra(
                s,
                source=("connection", f"{connection_id}:tx_1"),
                own_account_id=account_id,
                counterparty_identifier_id=identifier_id,
                other_postings=0,
            )
            amount = Decimal(quantity)
            await upsert_connector_transaction(
                ctx,
                TransactionInput(
                    occurred_at=WHEN,
                    description="Transfer",
                    origin="connector",
                    connection_id=connection_id,
                    external_id="tx_1",
                    postings=[
                        PostingInput(account_id, gbp.id, amount),
                        PostingInput(contra, gbp.id, -amount),
                    ],
                    counterparty=CounterpartyFields(
                        identifier_id=identifier_id, bank_transfer=True
                    ),
                ),
            )

    async def heal() -> None:
        async with (
            sessions() as s,
            command_context(s, Actor("system", "transfers"), command, {}) as ctx,
        ):
            await heal_transfers(ctx)
            # Still uncommitted while the other heals, as after a sync.
            await asyncio.sleep(0.5)

    connections: list[UUID] = []
    accounts: list[UUID] = []
    try:
        await run(get_or_create_system_account, "unassigned")
        await run(get_or_create_system_account, "in_transit")
        a_conn, a_account, a_id = await bank("Concurrent A", A_IDENTIFIER)
        connections.append(a_conn)
        accounts.append(a_account)
        c_conn, c_account, c_id = await bank("Concurrent C", C_IDENTIFIER)
        connections.append(c_conn)
        accounts.append(c_account)
        await post(a_conn, a_account, "-100", c_id)
        await post(c_conn, c_account, "100", a_id)

        await asyncio.gather(heal(), heal())

        async with sessions() as s:
            transfers = await list_transfers(s)
            assert [(t.status, t.from_account_id, t.to_account_id) for t in transfers] == [
                ("matched", a_account, c_account)
            ]
            assert await find_transfer_violations(s) == []
    finally:
        async with owner.begin() as conn:
            params = {"connections": connections, "accounts": accounts}
            await conn.execute(
                text(
                    "delete from finance.transfers"
                    " where from_account_id = any(:accounts) or to_account_id = any(:accounts)"
                ),
                params,
            )
            await conn.execute(
                text("delete from finance.transactions where connection_id = any(:connections)"),
                params,
            )
            await conn.execute(
                text("delete from finance.account_identifiers where value = any(:values)"),
                {"values": [A_IDENTIFIER, C_IDENTIFIER]},
            )
            await conn.execute(
                text("delete from finance.accounts where id = any(:accounts)"), params
            )
            await conn.execute(
                text("delete from core.connections where id = any(:connections)"), params
            )
            created = await conn.execute(
                text(
                    "delete from core.settings"
                    " where key like 'finance.system_account.%' and not (key = any(:existing))"
                    " returning value #>> '{}'"
                ),
                {"existing": list(existing)},
            )
            system = [UUID(row[0]) for row in created]
            await conn.execute(
                text("delete from finance.accounts where id = any(:ids)"), {"ids": system}
            )
            await conn.execute(
                text("delete from core.events where occurred_at >= :t"), {"t": started}
            )
            await conn.execute(
                text("delete from core.audit_log where command = :c"), {"c": command}
            )
        await engine.dispose()
        await owner.dispose()


async def _ledger(session, ids) -> tuple:
    """The matching outcome by external id: each transfer's status, review reason and
    legs, each record's contra kind, and In transit's balance."""
    transfers = await session.execute(
        text(
            "select tr.status, tr.review_reason, o.external_id as out_ext, i.external_id as in_ext"
            " from finance.transfers tr"
            " left join finance.transactions o on o.id = tr.out_transaction_id"
            " left join finance.transactions i on i.id = tr.in_transaction_id"
        )
    )
    contras = await session.execute(
        text(
            "select t.external_id, p.account_id from finance.transactions t"
            " join finance.postings p on p.transaction_id = t.id"
            " where t.external_id is not null and t.status <> 'void'"
            " and p.account_id in (:unassigned, :in_transit)"
        ),
        {"unassigned": ids.unassigned, "in_transit": ids.in_transit},
    )
    return (
        sorted((tuple(row) for row in transfers), key=lambda row: tuple(map(str, row))),
        sorted(
            (row.external_id, "in_transit" if row.account_id == ids.in_transit else "unassigned")
            for row in contras
        ),
        await in_transit_balance(session, ids),
    )


# Two identical payments from A to C an hour apart, and one receipt on C.
_ORDERS = {
    "senders_first": ["out_1", "out_2", "in_1"],
    "receiver_first": ["in_1", "out_1", "out_2"],
    "interleaved": ["out_1", "in_1", "out_2"],
    "one_sync": None,  # all posted, then one heal
}


@pytest.mark.parametrize("receiver_identified", [False, True])
@pytest.mark.parametrize("order", list(_ORDERS))
async def test_same_ledger_in_every_order(
    session, ids, a_and_c, connector_tx, heal, order, receiver_identified
) -> None:
    a, a_id, c, c_id = a_and_c

    async def post(external_id: str, status: str = "booked"):
        if external_id.startswith("out"):
            hours = int(external_id[-1]) - 1
            return await connector_tx(
                a,
                "-100",
                external_id=external_id,
                occurred_at=WHEN + timedelta(hours=hours),
                identifier_id=c_id,
                status=status,
            )
        return await connector_tx(
            c,
            "100",
            external_id=external_id,
            occurred_at=WHEN + timedelta(hours=3),
            identifier_id=a_id if receiver_identified else None,
        )

    steps = _ORDERS[order]
    if steps is None:
        for external_id in ("out_1", "out_2", "in_1"):
            await post(external_id)
        await heal()
    else:
        for external_id in steps:
            await post(external_id)
            await heal()

    # One receipt could be either payment: every side needs review, nothing is linked.
    expected = [
        ("awaiting", "several_candidates", "out_1", None),
        ("awaiting", "several_candidates", "out_2", None),
    ]
    if receiver_identified:
        expected.append(("awaiting", "several_candidates", None, "in_1"))
    receiver_contra = "in_transit" if receiver_identified else "unassigned"
    assert await _ledger(session, ids) == (
        sorted(expected, key=lambda row: tuple(map(str, row))),
        [("in_1", receiver_contra), ("out_1", "in_transit"), ("out_2", "in_transit")],
        Decimal(200) if not receiver_identified else Decimal(100),
    )
    await assert_consistent(session)
    assert await heal() == 0

    # The second payment is voided upstream: the pair is unique now.
    await post("out_2", status="void")
    await heal()
    assert await _ledger(session, ids) == (
        [("matched", None, "out_1", "in_1")],
        [("in_1", "in_transit"), ("out_1", "in_transit")],
        Decimal(0),
    )
    await assert_consistent(session)
    assert await heal() == 0


@pytest.mark.parametrize("receiver_identified", [False, True])
@pytest.mark.parametrize("sender_first", [True, False])
async def test_amount_differs_in_either_order(
    session, ids, a_and_c, connector_tx, heal, sender_first, receiver_identified
) -> None:
    a, a_id, c, c_id = a_and_c

    async def send():
        return await connector_tx(a, "-100", external_id="out_1", identifier_id=c_id)

    async def receive(status: str = "booked"):
        return await connector_tx(
            c,
            "90",
            external_id="in_1",
            occurred_at=WHEN + timedelta(1),
            identifier_id=a_id if receiver_identified else None,
            status=status,
        )

    for step in (send, receive) if sender_first else (receive, send):
        await step()
        await heal()

    # Only a record naming A's account is evidence of a differing amount: an unidentified
    # bank payment of another amount is just another payment.
    expected = [("awaiting", "amount_differs" if receiver_identified else None, "out_1", None)]
    if receiver_identified:
        expected.append(("awaiting", "amount_differs", None, "in_1"))
    receiver_contra = "in_transit" if receiver_identified else "unassigned"
    assert await _ledger(session, ids) == (
        sorted(expected, key=lambda row: tuple(map(str, row))),
        [("in_1", receiver_contra), ("out_1", "in_transit")],
        Decimal(10) if receiver_identified else Decimal(100),
    )
    await assert_consistent(session)
    assert await heal() == 0

    # The differing record is voided: nothing left to review.
    await receive(status="void")
    await heal()
    assert await _ledger(session, ids) == (
        [("awaiting", None, "out_1", None)],
        [("out_1", "in_transit")],
        Decimal(100),
    )
    await assert_consistent(session)
    assert await heal() == 0


async def test_later_arrival_makes_a_match_ambiguous(
    session, ids, a_and_c, connector_tx, heal
) -> None:
    # A matched pair is only as good as its uniqueness: a second identical payment in the
    # window turns it into a case for review, as if all three had arrived together.
    a, _, c, c_id = a_and_c
    await connector_tx(a, "-100", external_id="out_1", identifier_id=c_id)
    await connector_tx(c, "100", external_id="in_1", occurred_at=WHEN + timedelta(hours=3))
    await heal()
    [transfer] = await list_transfers(session)
    assert transfer.status == "matched"

    await connector_tx(
        a, "-100", external_id="out_2", occurred_at=WHEN + timedelta(hours=1), identifier_id=c_id
    )
    await heal()
    transfers = await list_transfers(session)
    assert (
        sorted((t.status, t.review_reason) for t in transfers)
        == [("awaiting", "several_candidates")] * 2
    )
    await assert_consistent(session)
    assert await heal() == 0


@pytest.mark.parametrize("receipt_first", [True, False])
async def test_mutual_pair_holds_against_an_unidentified_receipt(
    session, ids, a_and_c, connector_tx, heal, receipt_first
) -> None:
    # Each record names the other's account: that pair is stronger evidence than a receipt
    # on C that names nobody, so the receipt does not make it ambiguous.
    a, a_id, c, c_id = a_and_c

    async def receipt():
        return await connector_tx(
            c, "100", external_id="in_2", occurred_at=WHEN + timedelta(hours=1)
        )

    if receipt_first:
        stray = await receipt()
        await heal()
    out_record = await connector_tx(a, "-100", external_id="out_1", identifier_id=c_id)
    await heal()
    in_record = await connector_tx(
        c, "100", external_id="in_1", occurred_at=WHEN + timedelta(hours=2), identifier_id=a_id
    )
    await heal()
    if not receipt_first:
        stray = await receipt()
        await heal()

    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.review_reason) == ("matched", None)
    assert (transfer.out_transaction_id, transfer.in_transaction_id) == (
        out_record.id,
        in_record.id,
    )
    assert await contra_account(session, stray.id, c.account.id) == ids.unassigned
    assert await in_transit_balance(session, ids) == 0
    await assert_consistent(session)
    assert await heal() == 0


async def test_unidentified_payment_of_another_amount_is_no_evidence(
    session, ids, a_and_c, connector_tx, heal
) -> None:
    a, _, c, c_id = a_and_c
    await history(connector_tx, c)
    await connector_tx(c, "90", external_id="in_1", occurred_at=WHEN + timedelta(hours=1))
    await connector_tx(a, "-100", external_id="out_1", identifier_id=c_id)
    await heal()

    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.review_reason) == ("awaiting", None)
    assert await candidates(session, transfer.id) == []
    await assert_consistent(session)
    assert await heal() == 0


async def test_partner_gone_since_the_relation_is_left_to_the_next_pass(
    session, run, a_and_c, connector_tx, heal
) -> None:
    a, a_id, c, c_id = a_and_c
    sent = await connector_tx(a, "-100", external_id="out_1", identifier_id=c_id)
    received = await connector_tx(c, "100", external_id="in_1", identifier_id=a_id)

    async def match_with_a_stale_relation(ctx) -> None:
        settings = await load_transfer_settings(ctx.session)
        relation = await pair_relation(ctx.session, await system_account_ids(ctx.session), settings)
        assert relation.partner(sent.id) == received.id
        # A sync voids the partner after the heal pass computed its relation.
        await void_transaction(ctx, received.id)
        system = await transfer_engine._system(ctx)
        await transfer_engine._match(ctx, sent.id, settings, system, relation, {})

    await run(match_with_a_stale_relation)  # does not raise

    await heal()
    [transfer] = await list_transfers(session)
    assert (transfer.out_transaction_id, transfer.in_transaction_id) == (sent.id, None)
    await assert_consistent(session)


async def test_heal_holds_no_system_account_lock(
    backend_url: str, owner_url: str, migrated: None
) -> None:
    # A long heal must not hold up a sync, whose first step looks up the system accounts in
    # a short transaction of its own.
    engine, owner = create_engine(backend_url), create_engine(owner_url)
    sessions = make_sessionmaker(engine)
    command = "test.system_account_lock"
    async with owner.connect() as conn:
        started = await conn.scalar(text("select clock_timestamp()"))
        existing = set(
            await conn.scalars(
                text("select key from core.settings where key like 'finance.system_account.%'")
            )
        )

    async def get(key) -> UUID:
        async with (
            sessions() as s,
            command_context(s, Actor("connector", "test"), command, {}) as ctx,
        ):
            await s.execute(text("set local lock_timeout = '1s'"))
            return await get_or_create_system_account(ctx, key)

    try:
        unassigned = await get("unassigned")
        await get("in_transit")
        async with sessions() as s:
            # A heal with something to match (all rolled back at the end): A's payment to B.
            ctx = CommandContext(s, Actor("system", "transfers"), uuid4())
            gbp = await get_currency(s, "GBP")
            connection = await create_connection(
                ctx, connector="monzo", display_name="Lock A", config={}
            )
            a = await create_account(
                ctx,
                name="Lock A",
                ledger_type="asset",
                subtype="bank",
                balance_mode="transactions",
                source_connection_id=connection.id,
            )
            b = await create_account(
                ctx, name="Lock B", ledger_type="asset", subtype="bank", balance_mode="hybrid"
            )
            b_identifier = await assign_identifier(ctx, b.id, "uk_bank_account", C_IDENTIFIER)
            await upsert_connector_transaction(
                ctx,
                TransactionInput(
                    occurred_at=WHEN,
                    description="Transfer",
                    origin="connector",
                    connection_id=connection.id,
                    external_id="tx_1",
                    postings=[
                        PostingInput(a.id, gbp.id, Decimal(-100)),
                        PostingInput(await get("in_transit"), gbp.id, Decimal(100)),
                    ],
                    counterparty=CounterpartyFields(
                        identifier_id=b_identifier.id, bank_transfer=True
                    ),
                ),
            )
            assert await heal_transfers(ctx) == 1
            # The heal is still uncommitted while the sync asks.
            assert await get("unassigned") == unassigned
            await s.rollback()
    finally:
        async with owner.begin() as conn:
            created = await conn.execute(
                text(
                    "delete from core.settings"
                    " where key like 'finance.system_account.%' and not (key = any(:existing))"
                    " returning value #>> '{}'"
                ),
                {"existing": list(existing)},
            )
            system = [UUID(row[0]) for row in created]
            await conn.execute(
                text("delete from finance.accounts where id = any(:ids)"), {"ids": system}
            )
            await conn.execute(
                text("delete from core.events where occurred_at >= :t"), {"t": started}
            )
            await conn.execute(
                text("delete from core.audit_log where command = :c"), {"c": command}
            )
        await engine.dispose()
        await owner.dispose()
