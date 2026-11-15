import time
from decimal import Decimal

import pytest

from lararium.modules.finance.importing.parsed import StatementRate, keyed


def test_keyed_numbers_repeated_keys() -> None:
    pairs = [("A", "1"), ("B", "2"), ("A", "3"), ("A", "4")]
    assert keyed(pairs) == {"A": "1", "B": "2", "A#2": "3", "A#3": "4"}


def test_keyed_is_linear() -> None:
    started = time.perf_counter()
    values = keyed([("A", "x")] * 200_000)
    assert time.perf_counter() - started < 1
    assert len(values) == 200_000 and values["A#200000"] == "x"


def test_keyed_never_overwrites_a_key_that_looks_numbered() -> None:
    pairs = [("A", "1"), ("A", "2"), ("A#2", "3"), ("A", "4")]
    assert keyed(pairs) == {"A": "1", "A#2": "2", "A#2#2": "3", "A#3": "4"}
    assert keyed([("A#2", "1"), ("A", "2"), ("A", "3")]) == {"A#2": "1", "A": "2", "A#3": "3"}


def test_statement_rate_needs_a_rate_or_an_ear() -> None:
    assert StatementRate("Overdraft", None, Decimal(50), rate=None, ear=Decimal("35")).ear
    assert StatementRate("Credit", None, Decimal(50), rate=Decimal("1.25"), ear=None).rate
    with pytest.raises(ValueError, match="a rate or an EAR"):
        StatementRate("Overdraft", None, Decimal(50), rate=None, ear=None)
