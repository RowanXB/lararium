import time

from lararium.modules.finance.importing.parsed import keyed


def test_keyed_numbers_repeated_keys() -> None:
    pairs = [("A", "1"), ("B", "2"), ("A", "3"), ("A", "4")]
    assert keyed(pairs) == {"A": "1", "B": "2", "A#2": "3", "A#3": "4"}


def test_keyed_is_linear() -> None:
    started = time.perf_counter()
    values = keyed([("A", "x")] * 200_000)
    assert time.perf_counter() - started < 1
    assert len(values) == 200_000 and values["A#200000"] == "x"
