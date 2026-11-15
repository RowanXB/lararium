from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text

from lararium.modules.finance.errors import FinanceValidationError, TransferNotFoundError
from lararium.modules.finance.identifiers import get_or_create_identifier
from lararium.modules.finance.transfers import (
    candidates,
    get_transfer,
    list_transfers,
    pair,
    reject,
    unpair,
    unreject,
)

from .conftest import (
    B_IDENTIFIER,
    WHEN,
    assert_consistent,
    balances_on,
    contra_account,
    history,
    in_transit_balance,
    placeholders,
)


@pytest.fixture
async def inferred(session, run, connected_bank, manual_bank, connector_tx, heal):
    """A's payment of 100 to the manual account B, with B's placeholder."""
    a = await connected_bank("A")
    b = await manual_bank("B")
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    record = await connector_tx(a, "-100", identifier_id=identifier)
    await heal()
    [transfer] = await list_transfers(session)
    assert transfer.status == "inferred"
    return a, b, identifier, record, transfer


async def _decisions(session) -> list[tuple]:
    rows = await session.execute(
        text(
            "select kind, subject_kind, subject_ref, other_kind, other_ref"
            " from finance.transfer_decisions order by kind, subject_ref"
        )
    )
    return [tuple(row) for row in rows]


async def test_unknown_transfer_not_found(session, run) -> None:
    unknown = uuid4()
    with pytest.raises(TransferNotFoundError):
        await get_transfer(session, unknown)
    with pytest.raises(TransferNotFoundError):
        await candidates(session, unknown)
    for decide in (reject, unreject, unpair):
        with pytest.raises(TransferNotFoundError):
            await run(decide, unknown)
    with pytest.raises(TransferNotFoundError):
        await run(pair, unknown, uuid4())


async def test_reject_survives_heal(session, run, ids, inferred, connector_tx, heal) -> None:
    a, b, identifier, record, transfer = inferred

    rejected = await run(reject, transfer.id)

    assert (rejected.id, rejected.status, rejected.review_reason) == (
        transfer.id,
        "rejected",
        None,
    )
    assert (rejected.placeholder_transaction_id, rejected.out_transaction_id) == (None, record.id)
    assert await contra_account(session, record.id, a.account.id) == ids.unassigned
    assert await placeholders(session) == []
    assert (await balances_on(session))[b.id] == Decimal(1000)
    assert await _decisions(session) == [
        ("reject", "connection", f"{a.connection_id}:tx_1", None, None)
    ]
    await assert_consistent(session)

    # A re-sync of A's record and a heal leave it rejected.
    await connector_tx(a, "-100", identifier_id=identifier)
    assert await heal() == 0
    assert await list_transfers(session) == [rejected]
    assert await contra_account(session, record.id, a.account.id) == ids.unassigned
    await assert_consistent(session)

    with pytest.raises(FinanceValidationError):
        await run(reject, transfer.id)


async def test_unreject_restores(session, run, ids, inferred, heal) -> None:
    a, b, _, record, transfer = inferred
    await run(reject, transfer.id)

    await run(unreject, transfer.id)

    [restored] = await list_transfers(session)
    assert (restored.status, restored.out_transaction_id) == ("inferred", record.id)
    [placeholder] = await placeholders(session)
    assert restored.placeholder_transaction_id == placeholder.id
    assert await contra_account(session, record.id, a.account.id) == ids.in_transit
    assert (await balances_on(session))[b.id] == Decimal(1100)
    assert await _decisions(session) == []
    await assert_consistent(session)
    assert await heal() == 0

    # Only a rejected transfer can be unrejected.
    with pytest.raises(FinanceValidationError):
        await run(unreject, restored.id)


async def test_unpair_splits_and_never_repairs(
    session, run, ids, a_and_c, connector_tx, heal
) -> None:
    a, a_id, c, c_id = a_and_c
    out_record = await connector_tx(a, "-100", external_id="out_1", identifier_id=c_id)
    in_record = await connector_tx(
        c, "100", external_id="in_1", occurred_at=WHEN + timedelta(hours=2), identifier_id=a_id
    )
    await heal()
    [transfer] = await list_transfers(session)
    assert transfer.status == "matched"

    await run(unpair, transfer.id)

    transfers = sorted(await list_transfers(session), key=lambda t: t.opened_at)
    assert [
        (t.status, t.review_reason, t.out_transaction_id, t.in_transaction_id) for t in transfers
    ] == [
        ("awaiting", None, out_record.id, None),
        ("awaiting", None, None, in_record.id),
    ]
    out_ref, in_ref = f"{a.connection_id}:out_1", f"{c.connection_id}:in_1"
    assert sorted(await _decisions(session)) == sorted(
        [
            ("never_pair", "connection", out_ref, "connection", in_ref),
            ("never_pair", "connection", in_ref, "connection", out_ref),
        ]
    )
    # Each still offers the other, so the owner can pair them again.
    for found, other in zip(transfers, (in_record, out_record), strict=True):
        assert [(x.transaction_id, x.reason) for x in await candidates(session, found.id)] == [
            (other.id, "unpaired")
        ]
    await assert_consistent(session)
    assert await heal() == 0

    # Only a matched transfer of two own records can be unpaired.
    with pytest.raises(FinanceValidationError):
        await run(unpair, transfers[0].id)


async def test_unpair_refuses_a_placeholder_transfer(run, inferred) -> None:
    *_, transfer = inferred
    with pytest.raises(FinanceValidationError):
        await run(unpair, transfer.id)


async def test_pair_undoes_an_unpair(session, run, ids, a_and_c, connector_tx, heal) -> None:
    a, a_id, c, c_id = a_and_c
    out_record = await connector_tx(a, "-100", external_id="out_1", identifier_id=c_id)
    in_record = await connector_tx(
        c, "100", external_id="in_1", occurred_at=WHEN + timedelta(hours=2), identifier_id=a_id
    )
    await heal()
    [matched] = await list_transfers(session)
    await run(unpair, matched.id)
    [waiting] = [t for t in await list_transfers(session) if t.out_transaction_id is not None]

    paired = await run(pair, waiting.id, in_record.id)

    assert (paired.status, paired.out_transaction_id, paired.in_transaction_id) == (
        "matched",
        out_record.id,
        in_record.id,
    )
    assert await list_transfers(session) == [paired]
    assert await _decisions(session) == [
        ("pair", "connection", f"{a.connection_id}:out_1", "connection", f"{c.connection_id}:in_1")
    ]
    assert await contra_account(session, in_record.id, c.account.id) == ids.in_transit
    await assert_consistent(session)
    assert await heal() == 0


async def test_pair_that_does_not_take_effect_is_refused(
    session, run, a_and_c, connector_tx, heal
) -> None:
    """The owner already paired the deposit with another payment: pairing it with this one
    too leaves both ambiguous, so it is refused and nothing is stored."""
    a, a_id, c, c_id = a_and_c
    out_record = await connector_tx(a, "-100", external_id="out_1", identifier_id=c_id)
    in_record = await connector_tx(
        c, "100", external_id="in_1", occurred_at=WHEN + timedelta(hours=2), identifier_id=a_id
    )
    await connector_tx(
        a, "-100", external_id="out_2", occurred_at=WHEN + timedelta(days=30), bank_transfer=False
    )
    out_ref, in_ref = f"{a.connection_id}:out_1", f"{c.connection_id}:in_1"
    await session.execute(
        text(
            "insert into finance.transfer_decisions"
            " (kind, subject_kind, subject_ref, other_kind, other_ref) values"
            " ('pair', 'connection', :out_2, 'connection', :in_1),"
            " ('never_pair', 'connection', :out_1, 'connection', :in_1)"
        ),
        {"out_2": f"{a.connection_id}:out_2", "out_1": out_ref, "in_1": in_ref},
    )
    await heal()
    [waiting] = [t for t in await list_transfers(session) if t.status == "awaiting"]
    assert waiting.out_transaction_id == out_record.id
    before = await _decisions(session)
    assert [(x.transaction_id, x.reason) for x in await candidates(session, waiting.id)] == [
        (in_record.id, "unpaired")
    ]

    with pytest.raises(FinanceValidationError, match="did not take effect"):
        await run(pair, waiting.id, in_record.id)

    assert await _decisions(session) == before


@pytest.fixture
async def two_candidates(session, a_and_c, connector_tx, heal):
    """A's payment to C, with two deposits on C naming nobody that could each be its other
    side."""
    a, _, c, c_id = a_and_c
    await history(connector_tx, c)
    loose = [
        await connector_tx(c, "100", external_id=f"in_{n}", occurred_at=WHEN + timedelta(hours=n))
        for n in (1, 2)
    ]
    out_record = await connector_tx(a, "-100", external_id="out_1", identifier_id=c_id)
    await heal()
    [transfer] = await list_transfers(session)
    assert transfer.review_reason == "several_candidates"
    return a, c, out_record, loose, transfer


async def test_pair_picks_one_of_two_candidates(session, run, ids, two_candidates, heal) -> None:
    a, c, out_record, (chosen, other), transfer = two_candidates

    paired = await run(pair, transfer.id, chosen.id)

    assert (paired.status, paired.review_reason) == ("matched", None)
    assert (paired.out_transaction_id, paired.in_transaction_id) == (out_record.id, chosen.id)
    assert await list_transfers(session) == [paired]
    assert await contra_account(session, chosen.id, c.account.id) == ids.in_transit
    assert await contra_account(session, other.id, c.account.id) == ids.unassigned
    assert await in_transit_balance(session, ids) == 0
    assert await _decisions(session) == [
        ("pair", "connection", f"{a.connection_id}:out_1", "connection", f"{c.connection_id}:in_1")
    ]
    await assert_consistent(session)
    # The other candidate is still an exact one, but the owner's word holds against it.
    assert await heal() == 0


async def test_pair_holds_when_another_candidate_arrives(
    session, run, ids, two_candidates, connector_tx, heal
) -> None:
    _, c, out_record, (chosen, _), transfer = two_candidates
    paired = await run(pair, transfer.id, chosen.id)

    # Without the decision, a third identical deposit would make the match ambiguous.
    await connector_tx(c, "100", external_id="in_3", occurred_at=WHEN + timedelta(hours=3))
    await heal()

    [still] = await list_transfers(session)
    assert (still.id, still.status, still.out_transaction_id, still.in_transaction_id) == (
        paired.id,
        "matched",
        out_record.id,
        chosen.id,
    )
    await assert_consistent(session)
    assert await heal() == 0


async def test_pair_refuses_non_candidate(session, run, ids, two_candidates, connector_tx) -> None:
    _, c, _, _, transfer = two_candidates
    other_amount = await connector_tx(
        c, "90", external_id="in_9", occurred_at=WHEN + timedelta(hours=4)
    )
    [card] = [
        row.id
        for row in await session.execute(
            text("select id from finance.transactions where external_id = 'card_1'")
        )
    ]
    for transaction_id in (other_amount.id, card, uuid4()):
        with pytest.raises(FinanceValidationError):
            await run(pair, transfer.id, transaction_id)
    [unchanged] = await list_transfers(session)
    assert (unchanged.status, unchanged.review_reason) == ("awaiting", "several_candidates")
