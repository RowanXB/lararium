from datetime import timedelta
from decimal import Decimal

from sqlalchemy import insert, text

from lararium.modules.finance import models
from lararium.modules.finance.identifiers import (
    assign_identifier,
    get_or_create_identifier,
    unassign_identifier,
)
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    post_transaction,
    upsert_connector_transaction,
)
from lararium.modules.finance.snapshots import record_snapshot
from lararium.modules.finance.transfers import (
    find_transfer_violations,
    list_transfers,
    source_ref,
    transfer_contra,
)

from .conftest import (
    B_IDENTIFIER,
    DAY,
    WHEN,
    assert_consistent,
    balances_on,
    contra_account,
    net_worth_on,
    placeholders,
    postings_of,
)


async def test_outgoing_to_manual_account(
    session, run, ids, connected_bank, manual_bank, connector_tx, heal
) -> None:
    a = await connected_bank("A")
    b = await manual_bank("B")
    worth_before = await net_worth_on(session)
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)

    record = await connector_tx(a, "-100", identifier_id=identifier)
    # Identified through the registry, so it sits in In transit before matching runs.
    assert await contra_account(session, record.id, a.account.id) == ids.in_transit
    assert await net_worth_on(session) == worth_before

    assert await heal() == 1

    assert await contra_account(session, record.id, a.account.id) == ids.in_transit
    [placeholder] = await placeholders(session)
    assert (placeholder.description, placeholder.status) == ("Transfer from A", "inferred")
    assert (placeholder.occurred_at, placeholder.booking_date) == (
        record.occurred_at,
        record.booking_date,
    )
    assert await postings_of(session, placeholder.id) == {
        (b.id, Decimal("100")),
        (ids.in_transit, Decimal("-100")),
    }
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.review_reason) == ("inferred", None)
    assert (transfer.from_account_id, transfer.to_account_id, transfer.amount) == (
        a.account.id,
        b.id,
        Decimal("100"),
    )
    assert (
        transfer.out_transaction_id,
        transfer.in_transaction_id,
        transfer.placeholder_transaction_id,
    ) == (record.id, None, placeholder.id)
    assert transfer.opened_at == record.occurred_at
    balances = await balances_on(session)
    assert balances[ids.in_transit] == 0
    assert balances[b.id] == Decimal("1100")
    assert await net_worth_on(session) == worth_before
    await assert_consistent(session)


async def test_incoming_from_manual_account(
    session, run, ids, connected_bank, manual_bank, connector_tx, heal
) -> None:
    a = await connected_bank("A")
    b = await manual_bank("B")
    worth_before = await net_worth_on(session)
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)

    record = await connector_tx(a, "100", identifier_id=identifier)
    assert await heal() == 1

    assert await contra_account(session, record.id, a.account.id) == ids.in_transit
    [placeholder] = await placeholders(session)
    assert placeholder.description == "Transfer to A"
    assert await postings_of(session, placeholder.id) == {
        (b.id, Decimal("-100")),
        (ids.in_transit, Decimal("100")),
    }
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.from_account_id, transfer.to_account_id) == (
        "inferred",
        b.id,
        a.account.id,
    )
    assert (transfer.out_transaction_id, transfer.in_transaction_id) == (None, record.id)
    balances = await balances_on(session)
    assert balances[ids.in_transit] == 0
    assert balances[b.id] == Decimal("900")
    assert await net_worth_on(session) == worth_before
    await assert_consistent(session)


async def test_unidentified_stays_unassigned(
    session, run, ids, connected_bank, manual_bank, connector_tx, heal
) -> None:
    a = await connected_bank("A")
    await manual_bank("B")
    stranger = await run(get_or_create_identifier, "uk_bank_account", "998877:11223344")

    named = await connector_tx(a, "-100", external_id="tx_1", identifier_id=stranger)
    unnamed = await connector_tx(a, "-50", external_id="tx_2")
    assert await heal() == 0

    for record in (named, unnamed):
        assert await contra_account(session, record.id, a.account.id) == ids.unassigned
    assert await list_transfers(session) == []
    assert await placeholders(session) == []
    await assert_consistent(session)


async def test_identifier_of_own_account_is_not_a_transfer(
    session, run, ids, connected_bank, connector_tx, heal
) -> None:
    a = await connected_bank("A")
    own = await run(assign_identifier, a.account.id, "uk_bank_account", "223344:87654321")

    record = await connector_tx(a, "-100", identifier_id=own.id)
    assert await heal() == 0

    assert await contra_account(session, record.id, a.account.id) == ids.unassigned
    assert await list_transfers(session) == []
    await assert_consistent(session)


async def test_rejected_record_stays_unassigned(
    session, run, ids, connected_bank, manual_bank, connector_tx, heal
) -> None:
    a = await connected_bank("A")
    await manual_bank("B")
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    source = ("connection", f"{a.connection_id}:tx_1")
    # Writing decisions is a later task; the engine only has to honour one.
    await session.execute(
        insert(models.TransferDecision).values(
            kind="reject", subject_kind=source[0], subject_ref=source[1]
        )
    )

    record = await connector_tx(a, "-100", identifier_id=identifier)
    assert await source_ref(session, record.id) == source
    assert await contra_account(session, record.id, a.account.id) == ids.unassigned
    assert await heal() == 0
    assert await list_transfers(session) == []
    await assert_consistent(session)


async def test_heal_twice_writes_nothing(
    session, run, connected_bank, manual_bank, connector_tx, heal
) -> None:
    a = await connected_bank("A")
    await manual_bank("B")
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    await connector_tx(a, "-100", external_id="tx_1", identifier_id=identifier)
    await connector_tx(a, "40", external_id="tx_2", identifier_id=identifier)
    assert await heal() == 2

    async def state() -> tuple:
        tables = []
        for query in (
            "select id, occurred_at, booking_date, description, status, origin"
            " from finance.transactions order by id",
            "select id, transaction_id, account_id, quantity from finance.postings order by id",
            "select * from finance.transfers order by id",
            "select count(*) from core.events",
        ):
            tables.append([tuple(r) for r in await session.execute(text(query))])
        return tuple(tables)

    before = await state()
    assert await heal() == 0
    assert await state() == before
    changes = await session.scalar(
        text(
            "select changes from core.audit_log where command = 'finance.heal_transfers'"
            " order by id desc limit 1"
        )
    )
    assert changes == {}


async def test_placeholder_follows_its_record(
    session, run, ids, connected_bank, manual_bank, connector_tx, heal
) -> None:
    a = await connected_bank("A")
    b = await manual_bank("B")
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    await connector_tx(a, "-100", identifier_id=identifier)
    assert await heal() == 1

    # The upstream reports another amount and a later time for the same payment.
    later = WHEN + timedelta(hours=3)
    record = await connector_tx(a, "-120", identifier_id=identifier, occurred_at=later)
    assert await contra_account(session, record.id, a.account.id) == ids.in_transit
    assert await heal() == 1

    [placeholder] = await placeholders(session)
    assert placeholder.occurred_at == later
    assert await postings_of(session, placeholder.id) == {
        (b.id, Decimal("120")),
        (ids.in_transit, Decimal("-120")),
    }
    [transfer] = await list_transfers(session)
    assert (transfer.amount, transfer.status, transfer.out_transaction_id) == (
        Decimal("120"),
        "inferred",
        record.id,
    )
    assert (await balances_on(session))[b.id] == Decimal("1120")
    await assert_consistent(session)

    # Voided upstream: the placeholder and the transfer go, and B is back where it was.
    await connector_tx(a, "-120", identifier_id=identifier, occurred_at=later, status="void")
    assert await heal() == 1
    assert await list_transfers(session) == []
    assert await placeholders(session) == []
    balances = await balances_on(session)
    assert balances[b.id] == Decimal("1000")
    assert balances.get(ids.in_transit, Decimal(0)) == 0
    await assert_consistent(session)


async def test_placeholder_before_snapshot_absorbed(
    session, run, ids, gbp, connected_bank, manual_bank, connector_tx, heal
) -> None:
    a = await connected_bank("A")
    b = await manual_bank("B")
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    await connector_tx(a, "-100", identifier_id=identifier)
    assert await heal() == 1
    worth = await net_worth_on(session)

    # The owner reads B's balance a day later: the 100 is already in it.
    await run(
        record_snapshot,
        account_id=b.id,
        instrument_id=gbp.id,
        quantity=Decimal("1100"),
        as_of=WHEN + timedelta(days=1),
    )
    day = DAY + timedelta(days=1)
    balances = await balances_on(session, day)
    assert balances[b.id] == Decimal("1100")
    assert balances[ids.in_transit] == 0
    assert await net_worth_on(session, day) == worth
    await assert_consistent(session)


async def test_registering_later_matches_history(
    session, run, ids, connected_bank, manual_bank, connector_tx, heal
) -> None:
    a = await connected_bank("A")
    b = await manual_bank("B", identifier=None)
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    records = [
        await connector_tx(
            a,
            "-100",
            external_id=f"tx_{n}",
            occurred_at=WHEN - timedelta(days=30 * n),
            identifier_id=identifier,
        )
        for n in range(3)
    ]
    assert await heal() == 0
    for record in records:
        assert await contra_account(session, record.id, a.account.id) == ids.unassigned

    await run(assign_identifier, b.id, "uk_bank_account", B_IDENTIFIER)
    # The next sync computes the same contra as the engine now gives the records.
    assert (
        await transfer_contra(
            session,
            source=None,
            own_account_id=a.account.id,
            counterparty_identifier_id=identifier,
        )
        == ids.in_transit
    )
    assert await heal() == 3

    transfers = await list_transfers(session)
    assert [t.status for t in transfers] == ["inferred"] * 3
    # Newest first.
    assert [t.out_transaction_id for t in transfers] == [r.id for r in records]
    for record in records:
        assert await contra_account(session, record.id, a.account.id) == ids.in_transit
    assert len(await placeholders(session)) == 3
    await assert_consistent(session)


async def test_voided_placeholder_is_a_violation_and_healed(
    session, run, ids, connected_bank, manual_bank, connector_tx, heal
) -> None:
    a = await connected_bank("A")
    b = await manual_bank("B")
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    await connector_tx(a, "-100", identifier_id=identifier)
    assert await heal() == 1
    [placeholder] = await placeholders(session)

    # void_transaction refuses it; a raw UPDATE stands in for anything that gets past that.
    await session.execute(
        text("update finance.transactions set status = 'void' where id = :t"),
        {"t": placeholder.id},
    )
    assert await find_transfer_violations(session) == [placeholder.id]
    assert (await balances_on(session))[b.id] == Decimal("1000")

    assert await heal() == 1
    [fresh] = await placeholders(session)
    assert fresh.status == "inferred"
    [transfer] = await list_transfers(session)
    assert transfer.placeholder_transaction_id == fresh.id
    assert (await balances_on(session))[b.id] == Decimal("1100")
    await assert_consistent(session)


async def test_own_record_of_manual_account_replaces_placeholder(
    session, run, ids, gbp, connected_bank, manual_bank, connector_tx, heal
) -> None:
    a = await connected_bank("A")
    a_identifier = await run(assign_identifier, a.account.id, "uk_bank_account", "445566:11112222")
    b = await manual_bank("B")
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    out_record = await connector_tx(a, "-100", identifier_id=identifier)
    assert await heal() == 1
    [placeholder] = await placeholders(session)

    # B's own record arrives (as an import would post it), naming A's bank account.
    in_record = await run(
        post_transaction,
        TransactionInput(
            occurred_at=WHEN + timedelta(hours=5),
            description="From A",
            postings=[
                PostingInput(b.id, gbp.id, Decimal("100")),
                PostingInput(ids.unassigned, gbp.id, Decimal("-100")),
            ],
            counterparty=CounterpartyFields(identifier_id=a_identifier.id, bank_transfer=True),
        ),
    )
    assert await heal() == 1

    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.out_transaction_id, transfer.in_transaction_id) == (
        "matched",
        out_record.id,
        in_record.id,
    )
    assert transfer.placeholder_transaction_id is None
    assert await placeholders(session) == []
    assert (
        await session.scalar(
            text("select count(*) from finance.transactions where id = :t"), {"t": placeholder.id}
        )
        == 0
    )
    assert await contra_account(session, in_record.id, b.id) == ids.in_transit
    balances = await balances_on(session)
    assert balances[b.id] == Decimal("1100")  # once, not twice
    assert balances[ids.in_transit] == 0
    await assert_consistent(session)
    assert await heal() == 0


async def test_placeholder_follows_a_date_only_change(
    session, run, connected_bank, manual_bank, connector_tx, heal
) -> None:
    a = await connected_bank("A")
    b = await manual_bank("B")
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    await connector_tx(a, "-100", identifier_id=identifier)
    assert await heal() == 1

    later = WHEN + timedelta(days=1, hours=2)
    record = await connector_tx(a, "-100", identifier_id=identifier, occurred_at=later)
    assert await heal() == 1

    [placeholder] = await placeholders(session)
    assert (placeholder.occurred_at, placeholder.booking_date) == (later, record.booking_date)
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.placeholder_transaction_id) == ("inferred", placeholder.id)
    day = later.date()
    assert (await balances_on(session, day))[b.id] == Decimal("1100")
    await assert_consistent(session)
    assert await heal() == 0


async def test_identifier_unassigned_then_moved(
    session, run, ids, connected_bank, manual_bank, connector_tx, heal
) -> None:
    a = await connected_bank("A")
    b = await manual_bank("B")
    b2 = await manual_bank("B2", identifier=None)
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    record = await connector_tx(a, "-100", identifier_id=identifier)
    assert await heal() == 1

    # No longer one of the owner's accounts: not a transfer.
    await run(unassign_identifier, identifier)
    assert await heal() == 1
    assert await list_transfers(session) == []
    assert await placeholders(session) == []
    assert await contra_account(session, record.id, a.account.id) == ids.unassigned
    assert (await balances_on(session))[b.id] == Decimal("1000")
    await assert_consistent(session)

    # Registered to another account: a transfer to that one.
    await run(assign_identifier, b2.id, "uk_bank_account", B_IDENTIFIER)
    assert await heal() == 1
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.to_account_id) == ("inferred", b2.id)
    assert await contra_account(session, record.id, a.account.id) == ids.in_transit
    balances = await balances_on(session)
    assert (balances[b.id], balances[b2.id]) == (Decimal("1000"), Decimal("1100"))
    await assert_consistent(session)
    assert await heal() == 0


async def test_a_leg_that_gains_a_fee_posting_leaves_in_transit(
    session, run, gbp, ids, connected_bank, manual_bank, connector_tx, heal
) -> None:
    """A re-synced leg with a fee posting besides (3 postings, 2 on the own account) is no
    longer shaped like a leg: its transfer goes, and so does its posting on In transit."""
    a = await connected_bank("A")
    await manual_bank("B")
    worth_before = await net_worth_on(session)
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    record = await connector_tx(a, "-100", identifier_id=identifier)
    assert await heal() == 1
    contra = await transfer_contra(
        session,
        source=("connection", f"{a.connection_id}:tx_1"),
        own_account_id=a.account.id,
        counterparty_identifier_id=identifier,
    )
    assert contra == ids.in_transit  # a leg keeps its contra on In transit

    await run(
        upsert_connector_transaction,
        TransactionInput(
            occurred_at=WHEN,
            description="Transfer",
            status="booked",
            origin="connector",
            connection_id=a.connection_id,
            external_id="tx_1",
            postings=[
                PostingInput(a.account.id, gbp.id, Decimal("-100")),
                PostingInput(a.account.id, gbp.id, Decimal("-0.50"), is_fee=True),
                PostingInput(contra, gbp.id, Decimal("100.50")),
            ],
            counterparty=CounterpartyFields(identifier_id=identifier, bank_transfer=True),
        ),
    )

    assert await heal() == 1

    assert await list_transfers(session) == []
    assert await placeholders(session) == []
    assert await postings_of(session, record.id) == {
        (a.account.id, Decimal("-100")),
        (a.account.id, Decimal("-0.50")),
        (ids.unassigned, Decimal("100.50")),
    }
    assert await net_worth_on(session) == worth_before - Decimal("100.50")
    await assert_consistent(session)
    assert await heal() == 0


async def test_any_stray_in_transit_posting_is_found_and_repaired(
    session, run, gbp, ids, connected_bank, heal
) -> None:
    """The invariant covers every recorded transaction posting on In transit, whatever its
    shape: one that is no transfer's leg is a violation, and heal re-points that posting."""
    a = await connected_bank("A")
    stray, _ = await run(
        upsert_connector_transaction,
        TransactionInput(
            occurred_at=WHEN,
            description="Transfer",
            status="booked",
            origin="connector",
            connection_id=a.connection_id,
            external_id="tx_1",
            postings=[
                PostingInput(a.account.id, gbp.id, Decimal("-100")),
                PostingInput(a.account.id, gbp.id, Decimal("-0.50"), is_fee=True),
                PostingInput(ids.in_transit, gbp.id, Decimal("100.50")),
            ],
        ),
    )
    assert await find_transfer_violations(session) == [stray.id]

    assert await heal() == 1

    assert (ids.unassigned, Decimal("100.50")) in await postings_of(session, stray.id)
    await assert_consistent(session)
    assert await heal() == 0
