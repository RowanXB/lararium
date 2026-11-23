"""A record its source says was no transfer (`never_transfer`, a Binance withdrawal that failed)
is never a transfer's leg: not matched, not offered for pairing, and a transfer it was a leg of
is dissolved by heal. An unmarked record is matched as before."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from lararium.modules.finance.ledger import PostingInput, TransactionInput, post_transaction
from lararium.modules.finance.transfers import pair_records
from lararium.modules.finance.transfers.contra import own_leg
from lararium.modules.finance.transfers.queries import list_transfers, pair_candidates

from .conftest import WHEN, assert_consistent, contra_account, placeholders


def _matched(transfers, transaction_id):
    return [
        t
        for t in transfers
        if t.status == "matched" and transaction_id in (t.out_transaction_id, t.in_transaction_id)
    ]


@pytest.mark.parametrize("marked", [False, True])
async def test_a_marked_record_is_never_matched(
    session, ids, a_and_c, connector_tx, heal, marked: bool
) -> None:
    a, a_id, c, c_id = a_and_c
    sent = await connector_tx(a, "-100", identifier_id=c_id, never_transfer=marked)
    received = await connector_tx(c, "100", external_id="in_1", identifier_id=a_id)

    await heal()

    matched = _matched(await list_transfers(session), sent.id)
    if marked:
        assert matched == []
        assert await contra_account(session, sent.id, a.account.id) == ids.unassigned
        assert await own_leg(session, sent.id) is None
        offered = await pair_candidates(session, received.id)
        assert sent.id not in [o.transaction_id for o in offered]
    else:
        # Exactly as before the marker existed.
        assert [t.in_transaction_id for t in matched] == [received.id]
        assert await contra_account(session, sent.id, a.account.id) == ids.in_transit
        assert await own_leg(session, sent.id) is not None
    await assert_consistent(session)
    assert await heal() == 0


async def test_marking_a_paired_record_dissolves_its_transfer(
    session, run, ids, connected_bank, manual_bank, connector_tx, gbp, heal
) -> None:
    a = await connected_bank("A")
    b = await manual_bank("B", identifier=None, snapshot=None)
    sent = await connector_tx(a, "-100", external_id="out_1", bank_transfer=False)
    received = await connector_tx_by_hand(run, b, gbp, ids)
    await heal()
    await run(pair_records, sent.id, received)
    assert await contra_account(session, sent.id, a.account.id) == ids.in_transit

    await connector_tx(a, "-100", external_id="out_1", bank_transfer=False, never_transfer=True)
    await heal()

    assert _matched(await list_transfers(session), sent.id) == []
    assert await contra_account(session, sent.id, a.account.id) == ids.unassigned
    assert await placeholders(session) == []
    assert await placeholders(session, "rule") == []
    marked = await session.scalar(
        text("select never_transfer from finance.transactions where id = :t"), {"t": sent.id}
    )
    assert marked is True
    await assert_consistent(session)
    assert await heal() == 0


async def connector_tx_by_hand(run, account, gbp, ids):
    """A hand-entered +100 on the manual account, against Unassigned; its id."""
    record = await run(
        post_transaction,
        TransactionInput(
            occurred_at=WHEN + timedelta(hours=2),
            description="Hand entered",
            origin="manual",
            postings=[
                PostingInput(account.id, gbp.id, Decimal(100)),
                PostingInput(ids.unassigned, gbp.id, Decimal(-100)),
            ],
        ),
    )
    return record.id
