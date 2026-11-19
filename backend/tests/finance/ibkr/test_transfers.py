"""IBKR deposits, withdrawals and internal transfers pair through the M4 transfer engine
(design 5.12); there is no IBKR-specific matching code."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.core.connections import create_connection
from lararium.modules.finance.accounts import create_account
from lararium.modules.finance.identifiers import assign_identifier
from lararium.modules.finance.instruments import get_currency
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    find_unbalanced_transactions,
    upsert_connector_transaction,
)
from lararium.modules.finance.system_accounts import get_or_create_system_account
from lararium.modules.finance.transfers import (
    find_transfer_violations,
    heal_transfers,
    list_transfers,
    transfer_contra,
)
from lararium.modules.finance.valuation import balances_asof, net_worth_series

from .fixtures import AccountSpec, RowSpec
from .test_normalize_records import DAY, ETFA, GIA, _connection, _sync

OTHER = AccountSpec("U0000002", date_opened=DAY)
BANK_IDENTIFIER = "112233:12345678"
MONZO_HISTORY = datetime(2026, 10, 31, 12, tzinfo=UTC)
ACCOUNT = "IBKR ···0001"


def at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 12, tzinfo=UTC)


async def heal(session) -> int:
    async with command_context(
        session, Actor("system", "transfers"), "finance.heal_transfers", {}
    ) as ctx:
        return await heal_transfers(ctx)


async def ibkr_account(session):
    return await session.scalar(
        text("select id from finance.accounts where name = :n"), {"n": ACCOUNT}
    )


class Monzo:
    """A synthetic connected bank account with history, paying and receiving through IBKR's
    registered bank account."""

    def __init__(self, session, run, account, connection, identifier_id, gbp) -> None:
        self.session, self.run = session, run
        self.account, self.connection = account, connection
        self.identifier_id, self.gbp = identifier_id, gbp

    async def post(
        self, external_id: str, quantity: str, occurred_at: datetime, *, transfer: bool = True
    ):
        identifier_id = self.identifier_id if transfer else None
        contra = await transfer_contra(
            self.session,
            source=("connection", f"{self.connection.id}:{external_id}"),
            own_account_id=self.account.id,
            counterparty_identifier_id=identifier_id,
            other_postings=0,
        )
        amount = Decimal(quantity)
        record, _ = await self.run(
            upsert_connector_transaction,
            TransactionInput(
                occurred_at=occurred_at,
                description="Transfer",
                status="booked",
                origin="connector",
                connection_id=self.connection.id,
                external_id=external_id,
                postings=[
                    PostingInput(self.account.id, self.gbp.id, amount),
                    PostingInput(contra, self.gbp.id, -amount),
                ],
                counterparty=CounterpartyFields(
                    identifier_id=identifier_id, name="Test Owner", bank_transfer=transfer
                ),
            ),
        )
        return record


async def setup_monzo(session, run, connection, tmp_path, accounts=(GIA,)) -> Monzo:
    """IBKR's account exists (a first sync), has the bank account registered, and a Monzo
    account with history before the transfers."""
    await _sync(session, connection, tmp_path, [], accounts=accounts, period=(DAY, DAY))
    for key in ("unassigned", "in_transit"):
        await run(get_or_create_system_account, key)
    gbp = await get_currency(session, "GBP")
    identifier = await run(
        assign_identifier, await ibkr_account(session), "uk_bank_account", BANK_IDENTIFIER
    )
    monzo_connection = await run(
        create_connection, connector="monzo", display_name="Monzo", config={}
    )
    account = await run(
        create_account,
        name="Monzo",
        ledger_type="asset",
        subtype="bank",
        balance_mode="transactions",
        source_connection_id=monzo_connection.id,
    )
    monzo = Monzo(session, run, account, monzo_connection, identifier.id, gbp)
    await monzo.post("card_1", "-5", MONZO_HISTORY, transfer=False)
    return monzo


def deposit(amount: str, day: date, transaction_id: str = "8000001", **extra: str) -> RowSpec:
    return RowSpec(
        "CashTransaction", "U0000001", day=day, transactionID=transaction_id, amount=amount, **extra
    )


async def net_worth(session, start: date, end: date) -> list[Decimal]:
    return [p.value for p in await net_worth_series(session, start, end)]


async def in_transit(session, instrument_id=None) -> Decimal:
    account = await get_or_create_in_transit(session)
    rows = await balances_asof(session, DAY + timedelta(days=30))
    return sum(
        (
            r.quantity
            for r in rows
            if r.account_id == account
            and (instrument_id is None or r.instrument_id == instrument_id)
        ),
        Decimal(0),
    )


async def get_or_create_in_transit(session):
    async with command_context(session, Actor("system", "test"), "test", {}) as ctx:
        return await get_or_create_system_account(ctx, "in_transit")


async def assert_consistent(session) -> None:
    assert await find_transfer_violations(session) == []
    assert await find_unbalanced_transactions(session) == []


@pytest.mark.parametrize("ibkr_first", [True, False])
async def test_monzo_to_ibkr_matched_both_orders(session, run, tmp_path, ibkr_first) -> None:
    connection = await _connection(run)
    monzo = await setup_monzo(session, run, connection, tmp_path)
    sent = DAY
    arrived = DAY + timedelta(days=2)
    before = (await net_worth(session, sent - timedelta(days=1), sent - timedelta(days=1)))[0]

    async def receive() -> None:
        rows = [deposit("500.00", arrived)]
        await _sync(session, connection, tmp_path, rows, period=(DAY, arrived))
        await heal(session)

    async def send() -> None:
        await monzo.post("fps_1", "-500.00", at(sent))
        await heal(session)

    for step in (receive, send) if ibkr_first else (send, receive):
        await step()
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.review_reason) == ("matched", None)
    assert (transfer.from_account_id, transfer.to_account_id, transfer.amount) == (
        monzo.account.id,
        await ibkr_account(session),
        Decimal("500.00"),
    )
    assert await in_transit(session) == 0
    assert await net_worth(session, sent, arrived) == [before] * 3
    await assert_consistent(session)


async def test_isa_to_gia_position_transfer_matched(session, run, tmp_path) -> None:
    connection = await _connection(run)
    rows = [
        RowSpec(
            "Transfer",
            "U0000001",
            **ETFA,
            transactionID="6000001",
            quantity="5",
            direction="IN",
            account="U0000002",
        ),
        RowSpec(
            "Transfer",
            "U0000002",
            **ETFA,
            transactionID="6000002",
            quantity="5",
            direction="OUT",
            account="U0000001",
        ),
    ]
    await _sync(session, connection, tmp_path, rows, accounts=(GIA, OTHER))
    assert await heal(session) >= 0
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.review_reason) == ("matched", None)
    assert transfer.amount == Decimal(5)
    etfa = await session.scalar(text("select id from finance.instruments where symbol = 'ETFA'"))
    assert transfer.instrument_id == etfa
    assert await in_transit(session, etfa) == 0
    assert await in_transit(session) == 0
    await assert_consistent(session)


async def test_internal_movement_never_pairs_with_monzo(session, run, tmp_path) -> None:
    connection = await _connection(run)
    monzo = await setup_monzo(session, run, connection, tmp_path)
    # An internal Transfer of +300 GBP into the GIA from an account whose side is not in the
    # ledger, and a Monzo payment of the same amount to IBKR's bank account the same day.
    internal = RowSpec(
        "Transfer",
        "U0000001",
        assetCategory="CASH",
        currency="GBP",
        transactionID="6000001",
        quantity="300",
        cashTransfer="300",
        direction="IN",
        account="U0000009",
        type="INTERNAL",
    )
    await _sync(session, connection, tmp_path, [internal], period=(DAY, DAY))
    paid = await monzo.post("fps_1", "-300.00", at(DAY))
    await heal(session)
    internal_id = await session.scalar(
        text("select id from finance.transactions where external_id = 'transfer:U0000001:6000001'")
    )
    assert internal_id is not None
    transfers = await list_transfers(session)
    monzo_side = [t for t in transfers if paid.id in (t.out_transaction_id, t.in_transaction_id)]
    assert [(t.status, t.in_transaction_id) for t in monzo_side] == [("awaiting", None)]
    assert all(
        {t.out_transaction_id, t.in_transaction_id} != {paid.id, internal_id} for t in transfers
    )
    await assert_consistent(session)


async def test_withdrawal_fee_not_a_leg(session, run, tmp_path) -> None:
    connection = await _connection(run)
    monzo = await setup_monzo(session, run, connection, tmp_path)
    rows = [
        deposit("-200.00", DAY, "8000001"),
        deposit("-10.00", DAY, "8000002", type="Other Fees"),
    ]
    await _sync(session, connection, tmp_path, rows, period=(DAY, DAY))
    await monzo.post("fps_1", "200.00", at(DAY + timedelta(days=1)))
    await heal(session)
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.amount) == ("matched", Decimal("200.00"))
    fee = await session.scalar(
        text("select id from finance.transactions where external_id = 'cash:U0000001:8000002'")
    )
    assert fee not in (transfer.out_transaction_id, transfer.in_transaction_id)
    unassigned = await session.scalar(
        text(
            "select p.account_id = :u from finance.postings p"
            " join finance.accounts a on a.id = p.account_id"
            " where p.transaction_id = :t and a.name <> :n"
        ),
        {"u": await get_or_create_system_account_id(session, "unassigned"), "t": fee, "n": ACCOUNT},
    )
    assert unassigned is True
    await assert_consistent(session)


async def get_or_create_system_account_id(session, key):
    async with command_context(session, Actor("system", "test"), "test", {}) as ctx:
        return await get_or_create_system_account(ctx, key)
