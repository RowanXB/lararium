from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.core.connections import create_connection
from lararium.modules.finance.accounts import AccountRecord, create_account
from lararium.modules.finance.identifiers import assign_identifier
from lararium.modules.finance.instruments import InstrumentRecord
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    TransactionRecord,
    TransactionStatus,
    find_unbalanced_transactions,
    upsert_connector_transaction,
)
from lararium.modules.finance.snapshots import record_snapshot
from lararium.modules.finance.system_accounts import get_or_create_system_account
from lararium.modules.finance.transfers import (
    find_transfer_violations,
    heal_transfers,
    transfer_contra,
)
from lararium.modules.finance.valuation import balances_asof, net_worth_series

WHEN = datetime(2026, 11, 2, 12, tzinfo=UTC)
DAY = date(2026, 11, 3)  # the day after WHEN
B_IDENTIFIER = "112233:12345678"

type Run = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class SystemIds:
    unassigned: UUID
    in_transit: UUID
    opening_balances: UUID


@pytest.fixture
async def ids(run: Run) -> SystemIds:
    return SystemIds(
        unassigned=await run(get_or_create_system_account, "unassigned"),
        in_transit=await run(get_or_create_system_account, "in_transit"),
        opening_balances=await run(get_or_create_system_account, "opening_balances"),
    )


@dataclass(frozen=True)
class Connected:
    connection_id: UUID
    account: AccountRecord


type MakeConnected = Callable[..., Awaitable[Connected]]


@pytest.fixture
def connected_bank(run: Run) -> MakeConnected:
    """A bank account with a connection of its own, valued from its transactions."""

    async def _make(name: str = "A") -> Connected:
        connection = await run(create_connection, connector="monzo", display_name=name, config={})
        account = await run(
            create_account,
            name=name,
            ledger_type="asset",
            subtype="bank",
            balance_mode="transactions",
            source_connection_id=connection.id,
        )
        return Connected(connection.id, account)

    return _make


type MakeManual = Callable[..., Awaitable[AccountRecord]]


@pytest.fixture
def manual_bank(run: Run, gbp: InstrumentRecord) -> MakeManual:
    """A manual hybrid bank account, optionally registered under an identifier and with an
    opening snapshot (a day before WHEN by default)."""

    async def _make(
        name: str = "B",
        *,
        identifier: str | None = B_IDENTIFIER,
        snapshot: Decimal | None = Decimal("1000"),
        snapshot_at: datetime = WHEN - timedelta(days=1),
    ) -> AccountRecord:
        account = await run(
            create_account, name=name, ledger_type="asset", subtype="bank", balance_mode="hybrid"
        )
        if identifier is not None:
            await run(assign_identifier, account.id, "uk_bank_account", identifier)
        if snapshot is not None:
            await run(
                record_snapshot,
                account_id=account.id,
                instrument_id=gbp.id,
                quantity=snapshot,
                as_of=snapshot_at,
            )
        return account

    return _make


type ConnectorTx = Callable[..., Awaitable[TransactionRecord]]


@pytest.fixture
def connector_tx(
    session: AsyncSession, run: Run, gbp: InstrumentRecord, ids: SystemIds
) -> ConnectorTx:
    """Post (or re-post) a connector transaction as a normalizer would: its contra from
    transfer_contra, and the given counterparty fields."""

    async def _post(
        bank: Connected,
        quantity: str,
        *,
        external_id: str = "tx_1",
        occurred_at: datetime = WHEN,
        identifier_id: UUID | None = None,
        bank_transfer: bool = True,
        status: TransactionStatus = "booked",
        description: str = "Transfer",
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
                description=description,
                status=status,
                origin="connector",
                connection_id=bank.connection_id,
                external_id=external_id,
                postings=[
                    PostingInput(bank.account.id, gbp.id, amount),
                    PostingInput(contra, gbp.id, -amount),
                ],
                counterparty=CounterpartyFields(
                    identifier_id=identifier_id, name="Test Owner", bank_transfer=bank_transfer
                ),
            ),
        )
        return record

    return _post


type Heal = Callable[[], Awaitable[int]]


@pytest.fixture
def heal(session: AsyncSession) -> Heal:
    """heal_transfers in a command of its own, as the worker runs it."""

    async def _heal() -> int:
        async with command_context(
            session, Actor("system", "transfers"), "finance.heal_transfers", {}
        ) as ctx:
            return await heal_transfers(ctx)

    return _heal


async def balances_on(session: AsyncSession, day: date = DAY) -> dict[UUID, Decimal]:
    return {r.account_id: r.quantity for r in await balances_asof(session, day)}


async def net_worth_on(session: AsyncSession, day: date = DAY) -> Decimal:
    [point] = await net_worth_series(session, day, day)
    return point.value


async def contra_account(session: AsyncSession, transaction_id: UUID, own_account_id: UUID) -> Any:
    return await session.scalar(
        text(
            "select account_id from finance.postings"
            " where transaction_id = :t and account_id <> :own"
        ),
        {"t": transaction_id, "own": own_account_id},
    )


async def placeholders(session: AsyncSession, origin: str = "inference") -> list[Any]:
    """The derived transactions of one origin: placeholders, or settlements ("rule")."""
    rows = await session.execute(
        text(
            "select t.id, t.description, t.status, t.occurred_at, t.booking_date"
            " from finance.transactions t where t.origin = :origin order by t.occurred_at"
        ),
        {"origin": origin},
    )
    return list(rows)


async def postings_of(session: AsyncSession, transaction_id: UUID) -> set[tuple[UUID, Decimal]]:
    rows = await session.execute(
        text("select account_id, quantity from finance.postings where transaction_id = :t"),
        {"t": transaction_id},
    )
    return {(r.account_id, r.quantity) for r in rows}


async def assert_consistent(session: AsyncSession) -> None:
    assert await find_transfer_violations(session) == []
    assert await find_unbalanced_transactions(session) == []


A_IDENTIFIER = "445566:11112222"
C_IDENTIFIER = "778899:33334444"


@pytest.fixture
def registered(run: Run, connected_bank: MakeConnected):
    """A connected bank account with its own identifier registered to it."""

    async def _make(name: str, value: str) -> tuple[Connected, UUID]:
        bank = await connected_bank(name)
        identifier = await run(assign_identifier, bank.account.id, "uk_bank_account", value)
        return bank, identifier.id

    return _make


@pytest.fixture
async def a_and_c(registered: Any) -> tuple[Connected, UUID, Connected, UUID]:
    """A and C, each on a connection of its own and each registered."""
    a, a_id = await registered("A", A_IDENTIFIER)
    c, c_id = await registered("C", C_IDENTIFIER)
    return a, a_id, c, c_id


async def history(connector_tx: ConnectorTx, bank: Connected, days_before: int = 10) -> Any:
    """A card payment on the account's connection, so its history starts days_before WHEN."""
    return await connector_tx(
        bank,
        "-5",
        external_id="card_1",
        occurred_at=WHEN - timedelta(days=days_before),
        bank_transfer=False,
        description="Coffee",
    )


async def in_transit_balance(session: AsyncSession, ids: SystemIds) -> Decimal:
    return (await balances_on(session)).get(ids.in_transit, Decimal(0))
