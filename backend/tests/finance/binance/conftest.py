"""Fixtures for the Binance sync tests: one clock for the fake and the connector."""

import pytest

from lararium.modules import finance
from lararium.modules.finance.binance.sync import BinanceConnector

from .fixtures import FakeBinance
from .syncing import API_SECRET, Clock


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def fake(clock: Clock) -> FakeBinance:
    return FakeBinance(now=clock, secret=API_SECRET)


@pytest.fixture
def connector(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> BinanceConnector:
    """The registered connector, on the test's clock."""
    installed = BinanceConnector(clock=clock, sleep=clock.sleep)
    monkeypatch.setitem(finance.MODULE.connectors, "binance", installed)  # pyright: ignore[reportArgumentType]
    return installed
