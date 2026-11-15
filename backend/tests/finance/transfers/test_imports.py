"""The interface statement imports (M2) use, with a simulated import row: a manual-origin
transaction on the manual account, naming no counterparty, marked a bank transfer."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from lararium.modules.finance.identifiers import get_or_create_identifier
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    post_transaction,
    void_transaction,
)
from lararium.modules.finance.transfers import (
    list_transfers,
    preview_match,
    reject,
    source_ref,
    transfer_contra,
    unassign_identifier_and_rematch,
    unlink_transaction,
)

from .conftest import (
    B_IDENTIFIER,
    WHEN,
    assert_consistent,
    balances_on,
    contra_account,
    placeholders,
)

ROW_AT = WHEN + timedelta(hours=5)


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
    assert (await balances_on(session))[b.id] == Decimal(1100)
    return a, b, identifier, record, transfer


@pytest.fixture
def import_row(session, run, gbp):
    """Post a row as an M2 import would: its contra from transfer_contra."""

    async def _post(account, quantity: str = "100"):
        contra = await transfer_contra(
            session, source=None, own_account_id=account.id, counterparty_identifier_id=None
        )
        amount = Decimal(quantity)
        row = await run(
            post_transaction,
            TransactionInput(
                occurred_at=ROW_AT,
                description="Faster payment from A",
                origin="manual",
                postings=[
                    PostingInput(account.id, gbp.id, amount),
                    PostingInput(contra, gbp.id, -amount),
                ],
                counterparty=CounterpartyFields(bank_transfer=True),
            ),
        )
        assert await source_ref(session, row.id) == ("transaction", str(row.id))
        return row

    return _post


@pytest.fixture
async def imported(session, inferred, import_row, heal):
    """The import row on B, matched with A's payment in place of the placeholder."""
    a, b, identifier, record, _ = inferred
    row = await import_row(b)
    await heal()
    return a, b, identifier, record, row


async def _assert_placeholder_back(session, ids, a, b, record) -> None:
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.out_transaction_id, transfer.in_transaction_id) == (
        "inferred",
        record.id,
        None,
    )
    [placeholder] = await placeholders(session)
    assert transfer.placeholder_transaction_id == placeholder.id
    assert await contra_account(session, record.id, a.account.id) == ids.in_transit
    balances = await balances_on(session)
    assert (balances[b.id], balances[ids.in_transit]) == (Decimal(1100), 0)
    await assert_consistent(session)


async def test_import_row_replaces_placeholder(session, ids, imported, heal) -> None:
    _, b, _, record, row = imported

    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.out_transaction_id, transfer.in_transaction_id) == (
        "matched",
        record.id,
        row.id,
    )
    assert transfer.placeholder_transaction_id is None
    assert await placeholders(session) == []
    assert await contra_account(session, row.id, b.id) == ids.in_transit
    balances = await balances_on(session)
    assert balances[b.id] == Decimal(1100)  # one +100, not two
    assert balances[ids.in_transit] == 0
    await assert_consistent(session)
    assert await heal() == 0


async def test_preview_match_finds_the_transfer(session, gbp, inferred) -> None:
    *_, b, _, _, transfer = inferred
    count = text("select count(*) from finance.transactions")
    before = await session.scalar(count)

    async def preview(quantity: str = "100", bank_transfer: bool = True):
        return await preview_match(
            session,
            account_id=b.id,
            instrument_id=gbp.id,
            quantity=Decimal(quantity),
            occurred_at=ROW_AT,
            counterparty=CounterpartyFields(bank_transfer=bank_transfer),
        )

    assert await preview() == transfer
    assert await preview("90") is None  # another amount
    assert await preview("-100") is None  # the wrong direction
    assert await preview(bank_transfer=False) is None  # a card refund, say
    assert await session.scalar(count) == before


async def test_preview_match_none_when_ambiguous(
    session, run, gbp, connected_bank, manual_bank, connector_tx, heal
) -> None:
    a = await connected_bank("A")
    b = await manual_bank("B")
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    for n in (1, 2):
        await connector_tx(
            a,
            "-100",
            external_id=f"tx_{n}",
            occurred_at=WHEN + timedelta(hours=n),
            identifier_id=identifier,
        )
    await heal()
    assert [t.status for t in await list_transfers(session)] == ["inferred"] * 2

    assert (
        await preview_match(
            session,
            account_id=b.id,
            instrument_id=gbp.id,
            quantity=Decimal(100),
            occurred_at=ROW_AT,
            counterparty=CounterpartyFields(bank_transfer=True),
        )
        is None
    )


async def test_unlink_then_delete_restores_placeholder(session, run, ids, imported, heal) -> None:
    a, b, _, record, row = imported

    # The M2 rollback order: unlink each row, then delete it.
    await run(unlink_transaction, row.id)
    # The row is no longer a leg: back on Unassigned, until it is deleted.
    assert await contra_account(session, row.id, b.id) == ids.unassigned
    await assert_consistent(session)
    await session.execute(text("delete from finance.transactions where id = :t"), {"t": row.id})

    await _assert_placeholder_back(session, ids, a, b, record)
    assert await heal() == 0


async def test_unlink_from_a_rejected_transfer_keeps_the_rejection(
    session, run, ids, imported, heal
) -> None:
    a, b, _, record, row = imported
    [transfer] = await list_transfers(session)
    await run(reject, transfer.id)

    await run(unlink_transaction, row.id)
    await session.execute(text("delete from finance.transactions where id = :t"), {"t": row.id})

    [still] = await list_transfers(session)
    assert (still.id, still.status, still.out_transaction_id, still.in_transaction_id) == (
        transfer.id,
        "rejected",
        record.id,
        None,
    )
    assert await contra_account(session, record.id, a.account.id) == ids.unassigned
    assert (await balances_on(session))[b.id] == Decimal(1000)
    await assert_consistent(session)
    assert await heal() == 0


async def test_voiding_import_row_restores_placeholder(session, run, ids, imported, heal) -> None:
    a, b, _, record, row = imported

    await run(void_transaction, row.id)
    await heal()

    await _assert_placeholder_back(session, ids, a, b, record)
    assert await heal() == 0


async def test_delete_linked_without_unlink_fails(session, imported) -> None:
    *_, row = imported
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                text("delete from finance.transactions where id = :t"), {"t": row.id}
            )
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.in_transaction_id) == ("matched", row.id)
    await assert_consistent(session)


async def test_removing_identifier_reverts_to_unassigned(session, run, ids, inferred, heal) -> None:
    a, b, identifier, record, _ = inferred

    await run(unassign_identifier_and_rematch, identifier)

    assert await list_transfers(session) == []
    assert await placeholders(session) == []
    assert await contra_account(session, record.id, a.account.id) == ids.unassigned
    assert (await balances_on(session))[b.id] == Decimal(1000)
    await assert_consistent(session)
    assert await heal() == 0


async def test_weekly_transfers_each_match_their_own_week(
    session, run, ids, gbp, connected_bank, manual_bank, connector_tx, heal
) -> None:
    # A weekly standing order from A to B: Monzo stamps each payment at 09:00, and B's
    # statement rows carry only their date (00:00), so a receive is 9 hours before its send
    # and exactly a week minus 9 hours after the previous one.
    a = await connected_bank("A")
    b = await manual_bank("B", snapshot_at=WHEN - timedelta(days=30))
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    midnight = WHEN.replace(hour=0)
    weeks = range(4)
    sends = [
        await connector_tx(
            a,
            "-50",
            external_id=f"so_{k}",
            occurred_at=midnight + timedelta(days=7 * k, hours=9),
            identifier_id=identifier,
        )
        for k in weeks
    ]
    rows = []
    for k in weeks:
        contra = await transfer_contra(
            session, source=None, own_account_id=b.id, counterparty_identifier_id=None
        )
        rows.append(
            await run(
                post_transaction,
                TransactionInput(
                    occurred_at=midnight + timedelta(days=7 * k),
                    description="Standing order from A",
                    origin="manual",
                    postings=[
                        PostingInput(b.id, gbp.id, Decimal(50)),
                        PostingInput(contra, gbp.id, Decimal(-50)),
                    ],
                    counterparty=CounterpartyFields(bank_transfer=True),
                ),
            )
        )

    await heal()

    transfers = await list_transfers(session)
    assert sorted(
        (t.status, t.review_reason, t.out_transaction_id, t.in_transaction_id) for t in transfers
    ) == sorted(("matched", None, s.id, r.id) for s, r in zip(sends, rows, strict=True))
    assert await placeholders(session) == []
    await assert_consistent(session)
    assert await heal() == 0
