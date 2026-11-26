from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text

from lararium.modules.finance.errors import (
    FinanceValidationError,
    TransactionNotFoundError,
    TransferNotFoundError,
)
from lararium.modules.finance.identifiers import get_or_create_identifier
from lararium.modules.finance.ledger import (
    PostingInput,
    TransactionInput,
    post_transaction,
    void_transaction,
)
from lararium.modules.finance.transfers import (
    candidates,
    decisions,
    get_transfer,
    list_transfers,
    pair,
    pair_candidates,
    pair_records,
    reject,
    source_ref,
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

OTHER_AT = WHEN + timedelta(hours=2)


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


async def test_a_reject_stored_again_names_the_new_rejection(session, run, inferred) -> None:
    # One reject per record: storing it again (a leftover the engine never leaves) moves it
    # to the new rejection rather than being dropped.
    _, _, _, record, transfer = inferred
    await run(reject, transfer.id)
    ref = await source_ref(session, record.id)
    other = await session.scalar(
        text(
            "insert into finance.transfers"
            " (from_account_id, to_account_id, instrument_id, amount, status, opened_at)"
            " select from_account_id, to_account_id, instrument_id, amount, 'rejected', opened_at"
            " from finance.transfers where id = :t returning id"
        ),
        {"t": transfer.id},
    )

    await run(decisions._store, "reject", ref, transfer_id=other)

    named = await session.execute(
        text("select transfer_id from finance.transfer_decisions where kind = 'reject'")
    )
    assert [r.transfer_id for r in named] == [other]


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
    """The owner already paired the deposit with two other payments, so it is in no matched
    transfer: pairing it with this one too leaves it ambiguous, so it is refused and nothing
    is stored. (With one other pair decision the deposit would be matched, and pair refuses
    taking a record from a matched transfer: test_owner_pair_differs.)"""
    a, a_id, c, c_id = a_and_c
    out_record = await connector_tx(a, "-100", external_id="out_1", identifier_id=c_id)
    in_record = await connector_tx(
        c, "100", external_id="in_1", occurred_at=WHEN + timedelta(hours=2), identifier_id=a_id
    )
    for n in (2, 3):
        await connector_tx(
            a,
            "-100",
            external_id=f"out_{n}",
            occurred_at=WHEN + timedelta(days=30 * n),
            bank_transfer=False,
        )
    out_ref, in_ref = f"{a.connection_id}:out_1", f"{c.connection_id}:in_1"
    await session.execute(
        text(
            "insert into finance.transfer_decisions"
            " (kind, subject_kind, subject_ref, other_kind, other_ref) values"
            " ('pair', 'connection', :out_2, 'connection', :in_1),"
            " ('pair', 'connection', :out_3, 'connection', :in_1),"
            " ('never_pair', 'connection', :out_1, 'connection', :in_1)"
        ),
        {
            "out_2": f"{a.connection_id}:out_2",
            "out_3": f"{a.connection_id}:out_3",
            "out_1": out_ref,
            "in_1": in_ref,
        },
    )
    await heal()
    [waiting] = [t for t in await list_transfers(session) if t.out_transaction_id == out_record.id]
    assert waiting.status == "awaiting"
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


@pytest.fixture
def manual_tx(run, gbp, ids):
    """A hand-entered record on a manual account, against Unassigned."""

    async def _post(
        account,
        quantity: str,
        *,
        occurred_at=OTHER_AT,
        instrument=None,
        contra=None,
        description="Hand entered",
    ):
        amount = Decimal(quantity)
        instrument_id = (instrument or gbp).id
        return await run(
            post_transaction,
            TransactionInput(
                occurred_at=occurred_at,
                description=description,
                origin="manual",
                postings=[
                    PostingInput(account.id, instrument_id, amount),
                    PostingInput(contra or ids.unassigned, instrument_id, -amount),
                ],
            ),
        )

    return _post


@pytest.fixture
def rejected_record(session, run, connected_bank, manual_bank, connector_tx, heal):
    """A connector record the owner rejected as a transfer: it named a registered account,
    so it opened a transfer, which the owner rejected. Once per test."""

    async def _make(quantity: str):
        bank = await connected_bank("R")
        await manual_bank("Registered")
        identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
        record = await connector_tx(
            bank, quantity, external_id="rejected_1", identifier_id=identifier
        )
        await heal()
        [transfer] = [
            t
            for t in await list_transfers(session)
            if record.id in (t.out_transaction_id, t.in_transaction_id)
        ]
        await run(reject, transfer.id)
        return record

    return _make


@pytest.fixture
async def unnamed_pair(connected_bank, manual_bank, connector_tx, manual_tx, heal):
    """A connector record of -100 on A naming nobody, and a hand-entered +100 on manual B."""
    a = await connected_bank("A")
    b = await manual_bank("B", identifier=None, snapshot=None)
    out_record = await connector_tx(a, "-100", external_id="out_1", bank_transfer=False)
    in_record = await manual_tx(b, "100")
    await heal()
    return a, b, out_record, in_record


async def test_pair_records_links_two_records_that_name_nobody(
    session, run, ids, unnamed_pair, heal
) -> None:
    a, b, out_record, in_record = unnamed_pair
    assert await list_transfers(session) == []

    transfer = await run(pair_records, out_record.id, in_record.id)

    assert (transfer.status, transfer.out_transaction_id, transfer.in_transaction_id) == (
        "matched",
        out_record.id,
        in_record.id,
    )
    assert await contra_account(session, out_record.id, a.account.id) == ids.in_transit
    assert await contra_account(session, in_record.id, b.id) == ids.in_transit
    assert await heal() == 0
    assert await _decisions(session) == [
        ("pair", "connection", f"{a.connection_id}:out_1", "transaction", str(in_record.id))
    ]
    await assert_consistent(session)


async def test_pair_records_pairs_a_manual_record_with_an_imported_row(
    session, run, ids, manual_bank, manual_tx, record_import, heal
) -> None:
    a = await manual_bank("A", identifier=None, snapshot=None)
    b = await manual_bank("B", identifier=None, snapshot=None)
    imported = await manual_tx(a, "-100", description="Statement row")
    by_hand = await manual_tx(b, "100")
    await record_import(a.id, [imported.id])

    transfer = await run(pair_records, imported.id, by_hand.id)

    assert (transfer.status, transfer.out_transaction_id, transfer.in_transaction_id) == (
        "matched",
        imported.id,
        by_hand.id,
    )
    assert await contra_account(session, imported.id, a.id) == ids.in_transit
    assert await contra_account(session, by_hand.id, b.id) == ids.in_transit
    await assert_consistent(session)


async def test_pair_records_replaces_a_waiting_placeholder(
    session, run, ids, connected_bank, manual_bank, connector_tx, manual_tx, heal
) -> None:
    a = await connected_bank("A")
    b = await manual_bank("B")
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    out_record = await connector_tx(a, "-100", identifier_id=identifier)
    await heal()
    [waiting] = await list_transfers(session)
    assert waiting.status == "inferred"
    in_record = await manual_tx(b, "100")
    await heal()

    transfer = await run(pair_records, in_record.id, out_record.id)

    assert (transfer.status, transfer.out_transaction_id, transfer.in_transaction_id) == (
        "matched",
        out_record.id,
        in_record.id,
    )
    assert await placeholders(session) == []
    assert (await balances_on(session))[b.id] == Decimal("1000") + Decimal("100")
    await assert_consistent(session)
    assert await heal() == 0


async def test_pair_records_refusals_store_nothing(
    session,
    run,
    gbp,
    btc,
    ids,
    manual_bank,
    manual_tx,
    unnamed_pair,
    rejected_record,
) -> None:
    a, _b, out_record, in_record = unnamed_pair
    c = await manual_bank("C", identifier=None, snapshot=None)
    other_on_a = await manual_tx(a.account, "100")
    # Two currencies may differ only with the receiving side not larger (Task 17); a
    # non-fiat instrument may not differ at all (test_owner_pair_differs).
    wrong_amount = await manual_tx(c, "110")
    in_btc = await manual_tx(c, "100", instrument=btc)
    voided = await manual_tx(c, "100")
    await run(void_transaction, voided.id)
    with_fee = await run(
        post_transaction,
        TransactionInput(
            occurred_at=OTHER_AT,
            description="With fee",
            origin="manual",
            postings=[
                PostingInput(c.id, gbp.id, Decimal("98")),
                PostingInput(ids.unassigned, gbp.id, Decimal("-100")),
                PostingInput(ids.unassigned, gbp.id, Decimal("2")),
            ],
        ),
    )
    rejected = await rejected_record("100")
    stored = await _decisions(session)

    refusals = [
        (out_record.id, out_record.id, "itself"),
        (out_record.id, other_on_a.id, "same account"),
        (out_record.id, wrong_amount.id, "receiving side is larger"),  # -100 against +110
        (out_record.id, in_btc.id, "different instruments"),  # GBP against BTC
        (out_record.id, voided.id, "cannot be a transfer's leg"),
        (out_record.id, with_fee.id, "cannot be a transfer's leg"),
        (out_record.id, rejected.id, "undo the rejection"),  # -100 against the rejected +100
        (rejected.id, out_record.id, "undo the rejection"),
    ]
    for first, second, message in refusals:
        with pytest.raises(FinanceValidationError, match=message):
            await run(pair_records, first, second)
    assert await _decisions(session) == stored

    # A leg of a matched transfer: unpair it first.
    transfer = await run(pair_records, out_record.id, in_record.id)
    other_in = await manual_tx(c, "100")
    with pytest.raises(FinanceValidationError, match="unpair"):
        await run(pair_records, out_record.id, other_in.id)
    with pytest.raises(FinanceValidationError, match="unpair"):
        await run(pair_records, other_in.id, out_record.id)
    with pytest.raises(TransactionNotFoundError):
        await run(pair_records, out_record.id, uuid4())
    assert transfer.status == "matched"
    assert len(await _decisions(session)) == len(stored) + 1


async def test_pair_records_after_unpair(session, run, ids, unnamed_pair, heal) -> None:
    a, b, out_record, in_record = unnamed_pair
    transfer = await run(pair_records, out_record.id, in_record.id)

    await run(unpair, transfer.id)

    assert await list_transfers(session) == []
    assert await contra_account(session, out_record.id, a.account.id) == ids.unassigned
    assert await contra_account(session, in_record.id, b.id) == ids.unassigned
    assert sorted(kind for kind, *_ in await _decisions(session)) == ["never_pair", "never_pair"]
    assert [c.transaction_id for c in await pair_candidates(session, out_record.id)] == [
        in_record.id
    ]

    again = await run(pair_records, out_record.id, in_record.id)

    assert again.status == "matched"
    assert [kind for kind, *_ in await _decisions(session)] == ["pair"]
    await assert_consistent(session)
    assert await heal() == 0


async def test_pair_candidates_order_and_exclusions(
    session, run, connected_bank, manual_bank, connector_tx, manual_tx, rejected_record, heal
) -> None:
    a = await connected_bank("A")
    out_record = await connector_tx(
        a, "-100", external_id="out_1", occurred_at=WHEN, bank_transfer=False
    )
    day = out_record.booking_date
    m1 = await manual_bank("M1", identifier=None, snapshot=None)
    m2 = await manual_bank("M2", identifier=None, snapshot=None)

    def at(days: int, hours: int = 0):
        return WHEN + timedelta(days=days, hours=hours)

    plus3 = await manual_tx(m1, "100", occurred_at=at(3))
    minus1_late = await manual_tx(m2, "100", occurred_at=at(-1, 1))
    plus1 = await manual_tx(m1, "100", occurred_at=at(1))
    minus1 = await manual_tx(m2, "100", occurred_at=at(-1))
    # Excluded ones.
    await manual_tx(a.account, "100")  # the same account
    await manual_tx(m1, "110")  # larger than sent (one smaller is listed as differing)
    voided = await manual_tx(m1, "100")
    await run(void_transaction, voided.id)
    matched_leg = await manual_tx(m1, "100", occurred_at=at(0, 1))
    matched_out = await manual_tx(m2, "-100", occurred_at=at(0, 1))
    rejected = await rejected_record("100")
    await heal()
    await run(pair_records, matched_out.id, matched_leg.id)

    found = await pair_candidates(session, out_record.id)

    assert [c.transaction_id for c in found] == [
        minus1.id,
        minus1_late.id,
        plus1.id,
        plus3.id,
    ]
    first = found[0]
    assert (first.account_id, first.quantity, first.description) == (
        m2.id,
        Decimal("100"),
        "Hand entered",
    )
    assert (first.occurred_at, first.booking_date) == (at(-1), day - timedelta(days=1))

    # Many eligible ones: the nearest 20.
    for n in range(25):
        await manual_tx(m2, "100", occurred_at=at(10 + n))
    capped = await pair_candidates(session, out_record.id)
    assert len(capped) == 20
    assert [c.transaction_id for c in capped[:4]] == [minus1.id, minus1_late.id, plus1.id, plus3.id]

    with pytest.raises(FinanceValidationError, match="cannot be a transfer's leg"):
        await pair_candidates(session, voided.id)
    with pytest.raises(TransactionNotFoundError):
        await pair_candidates(session, uuid4())
    # A record that pair_records would refuse gets no list of choices it can't use, but the
    # same reason.
    with pytest.raises(FinanceValidationError, match="unpair it first"):
        await pair_candidates(session, matched_leg.id)
    with pytest.raises(FinanceValidationError, match="undo the rejection first"):
        await pair_candidates(session, rejected.id)
