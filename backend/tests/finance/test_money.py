from decimal import Decimal

from lararium.modules.finance.money import to_plain_str, tolerance


def test_tolerance_is_half_of_last_decimal_place() -> None:
    assert tolerance(2) == Decimal("0.005")
    assert tolerance(0) == Decimal("0.5")
    assert tolerance(18) == Decimal("5E-19")


def test_to_plain_str_never_uses_exponent() -> None:
    assert to_plain_str(Decimal("1E-18")) == "0.000000000000000001"
    assert to_plain_str(Decimal("1.50")) == "1.50"
    assert to_plain_str(Decimal("-2E+3")) == "-2000"
