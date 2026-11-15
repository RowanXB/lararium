from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
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
    upsert_connector_transaction,
)
from lararium.modules.finance.snapshots import record_snapshot
from lararium.modules.finance.system_accounts import get_or_create_system_account
from lararium.modules.finance.transfers import heal_transfers, transfer_contra

WHEN = datetime(2026, 11, 2, 12, tzinfo=UTC)
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
