import time
from decimal import Decimal
from typing import Any

import pytest

from lararium.modules.finance.importing.parsed import (
    Counterparty,
    StatementAccount,
    StatementRate,
    keyed,
    normalise_account_number,
    normalise_sort_code,
)


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


@pytest.mark.parametrize(
    ("normalise", "text"),
    [
        (normalise_account_number, "1234x5678"),
        (normalise_sort_code, "40-12-3x"),
        (normalise_sort_code, "40123456789"),
    ],
)
def test_bad_identifiers_show_only_their_last_four(normalise: Any, text: str) -> None:
    """Row errors are shown on the batch page: a malformed account number or sort code is
    named by its last four characters only, like every other identifier in a message."""
    with pytest.raises(ValueError) as raised:
        normalise(text)
    message = str(raised.value)
    assert repr("…" + text[-4:]) in message
    assert text not in message and text[:4] not in message


def test_blank_account_and_counterparty_fields_are_absent() -> None:
    assert StatementAccount(iban=" ", name="", branch=" Example ") == StatementAccount(
        branch="Example"
    )
    assert Counterparty(name=" ", reference="") == Counterparty()
