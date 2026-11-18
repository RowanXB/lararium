import os
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from hypothesis import HealthCheck, settings
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.modules.finance.accounts import AccountRecord, create_account
from lararium.modules.finance.instruments import (
    InstrumentRecord,
    create_instrument,
    get_currency,
)

settings.register_profile(
    "ci",
    derandomize=True,
    max_examples=15,
    deadline=None,  # every example runs on Postgres
    database=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
settings.register_profile(
    "deep",
    max_examples=int(os.environ.get("HYPOTHESIS_MAX_EXAMPLES", "500")),
    deadline=None,
    database=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))


@pytest.fixture
async def gbp(session: AsyncSession) -> InstrumentRecord:
    return await get_currency(session, "GBP")


@pytest.fixture
async def usd(session: AsyncSession) -> InstrumentRecord:
    return await get_currency(session, "USD")


@pytest.fixture
async def eur(session: AsyncSession) -> InstrumentRecord:
    return await get_currency(session, "EUR")


@pytest.fixture
async def btc(run: Callable[..., Awaitable[Any]], usd: InstrumentRecord) -> InstrumentRecord:
    return await run(
        create_instrument,
        kind="crypto",
        symbol="BTC",
        name="Bitcoin",
        decimals=8,
        quote_instrument_id=usd.id,
        identifiers=[],
    )


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
