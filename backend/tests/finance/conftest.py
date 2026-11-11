from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from financial_gate.modules.finance.accounts import AccountRecord, create_account
from financial_gate.modules.finance.instruments import InstrumentRecord, get_currency


@pytest.fixture
async def gbp(session: AsyncSession) -> InstrumentRecord:
    return await get_currency(session, "GBP")


@pytest.fixture
async def usd(session: AsyncSession) -> InstrumentRecord:
    return await get_currency(session, "USD")


@pytest.fixture
async def eur(session: AsyncSession) -> InstrumentRecord:
    return await get_currency(session, "EUR")


type MakeAccount = Callable[..., Awaitable[AccountRecord]]


@pytest.fixture
def make_account(run: Callable[..., Awaitable[Any]]) -> MakeAccount:
    async def _make(**overrides: Any) -> AccountRecord:
        fields: dict[str, Any] = {
            "name": "Test",
            "ledger_type": "asset",
            "subtype": "bank",
            "balance_mode": "snapshots",
        }
        return await run(create_account, **(fields | overrides))

    return _make
