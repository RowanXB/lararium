"""A record naming an account family (account family identifier design §5.2): its row in
eligible_records resolves to the family's other accounts, as identifiers.named_accounts does."""

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.connections import create_connection
from lararium.modules.finance.accounts import AccountRecord, create_account, set_type_key
from lararium.modules.finance.identifiers import family_identifier, named_accounts
from lararium.modules.finance.system_accounts import system_account_ids
from lararium.modules.finance.transfers.contra import eligible_records

from .conftest import Connected, ConnectorTx, Run

FAMILY_TYPES = ["trading212.invest", "trading212.stocks_isa", "trading212.cash_isa"]

type MakeFamily = Callable[[int], Awaitable[tuple[Connected, list[AccountRecord]]]]


@pytest.fixture
def family_accounts(run: Run) -> MakeFamily:
    """A connected Trading 212 Invest account and n - 1 manual accounts of the family's
    other types."""

    async def _make(n: int) -> tuple[Connected, list[AccountRecord]]:
        connection = await run(
            create_connection, connector="trading212", display_name="T212", config={}
        )
        first = await run(
            create_account,
            name="Invest",
            ledger_type="asset",
            subtype="broker",
            balance_mode="transactions",
            source_connection_id=connection.id,
            type_key=FAMILY_TYPES[0],
        )
        others = [
            await run(
                create_account,
                name=f"Other {key}",
                ledger_type="asset",
                subtype="broker",
                balance_mode="transactions",
                type_key=key,
            )
            for key in FAMILY_TYPES[1:n]
        ]
        return Connected(connection.id, first), others

    return _make


async def _eligible_row(session: AsyncSession, transaction_id: UUID) -> Any:
    e = eligible_records(await system_account_ids(session))
    return (await session.execute(select(e).where(e.c.transaction_id == transaction_id))).one()


@pytest.mark.parametrize("n", [1, 2, 3])
async def test_eligible_row_of_a_family_move(
    session: AsyncSession,
    run: Run,
    family_accounts: MakeFamily,
    connector_tx: ConnectorTx,
    n: int,
) -> None:
    first, others = await family_accounts(n)
    identifier_id = await run(family_identifier, "trading212")
    sent = await connector_tx(first, "-100", identifier_id=identifier_id)

    row = await _eligible_row(session, sent.id)

    holder, holders = await named_accounts(session, identifier_id, first.account.id)
    assert holders == {o.id for o in others} - {holder}
    assert row.family and row.names_account
    assert row.holder == holder
    assert (row.payee_holders is None and not holders) or set(row.payee_holders) == holders


async def test_a_retyped_account_still_excludes_itself(
    session: AsyncSession,
    run: Run,
    family_accounts: MakeFamily,
    connector_tx: ConnectorTx,
) -> None:
    first, others = await family_accounts(3)
    identifier_id = await run(family_identifier, "trading212")
    sent = await connector_tx(first, "-100", identifier_id=identifier_id)
    await run(set_type_key, first.account.id, None)

    row = await _eligible_row(session, sent.id)

    # Retyped, the account is no longer of the family: the two others are the set.
    assert row.holder is None
    assert set(row.payee_holders) == {o.id for o in others}
    assert first.account.id not in row.payee_holders
