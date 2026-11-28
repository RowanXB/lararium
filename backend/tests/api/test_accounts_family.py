"""An account joining or leaving a family heals the transfers at once (account family
identifier design §5.8): the accounts a family's records name changed with it."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.core.connections import create_connection
from lararium.modules.finance.accounts import AccountRecord, create_account
from lararium.modules.finance.identifiers import family_identifier
from lararium.modules.finance.instruments import get_currency
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    TransactionRecord,
    upsert_connector_transaction,
)
from lararium.modules.finance.system_accounts import get_or_create_system_account
from lararium.modules.finance.transfers import (
    find_transfer_violations,
    heal_transfers,
    list_transfers,
    transfer_contra,
)

FINANCE = "/api/v1/finance"
WHEN = datetime(2026, 11, 2, 12, tzinfo=UTC)
CASH_ISA = {
    "name": "Cash ISA",
    "ledger_type": "asset",
    "subtype": "savings_pot",
    "balance_mode": "transactions",
    "tax_wrapper": "isa",
    "type_key": "trading212.cash_isa",
}


@pytest.fixture(autouse=True)
async def system_accounts(run) -> None:
    for key in ("unassigned", "in_transit", "opening_balances"):
        await run(get_or_create_system_account, key)


async def _connected(run, name: str, type_key: str) -> tuple[UUID, AccountRecord]:
    connection = await run(create_connection, connector="trading212", display_name=name, config={})
    account = await run(
        create_account,
        name=name,
        ledger_type="asset",
        subtype="broker",
        balance_mode="transactions",
        source_connection_id=connection.id,
        type_key=type_key,
    )
    return connection.id, account


async def _move(
    session, run, bank: tuple[UUID, AccountRecord], quantity: str, family_id: UUID
) -> TransactionRecord:
    """A TRANSFER of Trading 212's connector, naming the family (trading212-norm-4)."""
    connection_id, account = bank
    gbp = await get_currency(session, "GBP")
    external_id = f"move-{account.name}"
    contra = await transfer_contra(
        session,
        source=("connection", f"{connection_id}:{external_id}"),
        own_account_id=account.id,
        counterparty_identifier_id=family_id,
        other_postings=0,
    )
    amount = Decimal(quantity)
    record, _ = await run(
        upsert_connector_transaction,
        TransactionInput(
            occurred_at=WHEN,
            description="Transfer",
            status="booked",
            origin="connector",
            connection_id=connection_id,
            external_id=external_id,
            postings=[
                PostingInput(account.id, gbp.id, amount),
                PostingInput(contra, gbp.id, -amount),
            ],
            counterparty=CounterpartyFields(identifier_id=family_id),
        ),
    )
    return record


async def _heal(session) -> int:
    async with command_context(session, Actor("system", "transfers"), "heal", {}) as ctx:
        return await heal_transfers(ctx)


async def _pair_and_check(session, run) -> None:
    """Invest and Stocks ISA, a TRANSFER out of one and into the other."""
    family_id = await run(family_identifier, "trading212")
    invest = await _connected(run, "Invest", "trading212.invest")
    stocks = await _connected(run, "Stocks ISA", "trading212.stocks_isa")
    await _move(session, run, invest, "-100", family_id)
    await _move(session, run, stocks, "100", family_id)
    await _heal(session)


async def _assert_healed(session, *, matched: bool) -> None:
    """Nothing is left for a heal, and the TRANSFERs are a matched pair or none."""
    assert await _heal(session) == 0
    found = await list_transfers(session, statuses=["matched"])
    assert len(found) == matched
    assert await find_transfer_violations(session) == []


async def test_a_family_account_joining_or_leaving_heals_at_once(client, session, run) -> None:
    """Invest and Stocks ISA, a family of two, hold a matched pair of TRANSFERs. A Cash ISA
    joining makes it a family of three whose third, never listed, may hold the counterpart:
    the pair is not matched, at once. The Cash ISA leaving, the pair is matched again."""
    await _pair_and_check(session, run)
    await _assert_healed(session, matched=True)

    created = await client.post(f"{FINANCE}/accounts", json=CASH_ISA)
    assert created.status_code == 201
    await _assert_healed(session, matched=False)

    url = f"{FINANCE}/accounts/{created.json()['id']}"
    assert (await client.patch(url, json={"type_key": None})).status_code == 200
    await _assert_healed(session, matched=True)

    # A manual account of no type takes the type through PATCH.
    plain = {k: v for k, v in CASH_ISA.items() if k != "type_key"} | {"name": "Other"}
    other = (await client.post(f"{FINANCE}/accounts", json=plain)).json()["id"]
    await _assert_healed(session, matched=True)
    typed = await client.patch(
        f"{FINANCE}/accounts/{other}", json={"type_key": CASH_ISA["type_key"]}
    )
    assert typed.status_code == 200
    await _assert_healed(session, matched=False)
