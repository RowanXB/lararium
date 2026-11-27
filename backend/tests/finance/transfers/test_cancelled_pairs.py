"""A cancelled pair (a withdrawal request and its cancellation) on In transit
(cancelled-withdrawal design §4.4)."""

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select, text

from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    TransactionRecord,
    upsert_connector_transaction,
    void_transaction,
)
from lararium.modules.finance.system_accounts import system_account_ids
from lararium.modules.finance.transfers import find_transfer_violations, list_transfers
from lararium.modules.finance.transfers.contra import snapshot_own_records

from .conftest import (
    A_IDENTIFIER,
    C_IDENTIFIER,
    WHEN,
    Connected,
    assert_consistent,
    contra_account,
    in_transit_balance,
)

type Post = Callable[..., Awaitable[TransactionRecord]]
type Pair = Callable[[Connected, str], Awaitable[tuple[TransactionRecord, TransactionRecord]]]


@pytest.fixture
def post(run, gbp, ids) -> Post:
    """Post (or re-post) a connector transaction of `quantity` on the bank's account against
    In transit, cancelling `cancels` if given."""

    async def _post(
        bank: Connected,
        external_id: str,
        quantity: str,
        occurred_at: datetime,
        cancels: UUID | None = None,
    ) -> TransactionRecord:
        amount = Decimal(quantity)
        record, _ = await run(
            upsert_connector_transaction,
            TransactionInput(
                occurred_at=occurred_at,
                description="Withdrawal",
                origin="connector",
                connection_id=bank.connection_id,
                external_id=external_id,
                postings=[
                    PostingInput(bank.account.id, gbp.id, amount),
                    PostingInput(ids.in_transit, gbp.id, -amount),
                ],
                counterparty=CounterpartyFields(bank_transfer=True),
                cancels_transaction_id=cancels,
            ),
        )
        return record

    return _post


@pytest.fixture
def pair(post: Post) -> Pair:
    """A withdrawal request of `amount` and its cancellation three minutes later."""

    async def _pair(bank: Connected, amount: str) -> tuple[TransactionRecord, TransactionRecord]:
        request = await post(bank, "request_1", f"-{amount}", WHEN)
        cancellation = await post(bank, "cancel_1", amount, WHEN + timedelta(minutes=3), request.id)
        return request, cancellation

    return _pair


async def test_a_cancelled_pair_stays_in_transit(session, ids, connected_bank, pair, heal) -> None:
    bank = await connected_bank("A")
    request, cancellation = await pair(bank, "100")

    await heal()

    assert await contra_account(session, request.id, bank.account.id) == ids.in_transit
    assert await contra_account(session, cancellation.id, bank.account.id) == ids.in_transit
    await assert_consistent(session)
    assert await in_transit_balance(session, ids) == 0
    assert await session.scalar(text("select count(*) from finance.transfers")) == 0


async def test_a_cancelled_pair_is_no_pairing_candidate(
    session, registered, connector_tx, pair, heal
) -> None:
    a, a_id = await registered("A", A_IDENTIFIER)
    c, _ = await registered("C", C_IDENTIFIER)
    request, cancellation = await pair(a, "100")
    await connector_tx(c, "100", external_id="in_1", identifier_id=a_id)

    await heal()

    paired = {request.id, cancellation.id}
    for transfer in await list_transfers(session):
        assert not paired & {transfer.out_transaction_id, transfer.in_transaction_id}
    own = await snapshot_own_records(session, await system_account_ids(session))
    assert not paired & set(await session.scalars(select(own.c.transaction_id)))


async def test_a_pair_that_leaves_money_in_transit_is_a_violation(
    session, connected_bank, pair, post
) -> None:
    bank = await connected_bank("A")
    request, cancellation = await pair(bank, "100")

    await post(bank, "cancel_1", "90", WHEN + timedelta(minutes=3), request.id)

    assert set(await find_transfer_violations(session)) == {request.id, cancellation.id}


async def test_a_pair_with_a_void_side_is_no_pair(
    session, ids, run, connected_bank, pair, heal
) -> None:
    bank = await connected_bank("A")
    request, cancellation = await pair(bank, "100")

    await run(void_transaction, cancellation.id)
    await heal()

    assert await contra_account(session, request.id, bank.account.id) == ids.unassigned
    await assert_consistent(session)
