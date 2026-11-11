import pytest
from sqlalchemy.ext.asyncio import AsyncSession

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
