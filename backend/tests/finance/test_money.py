from decimal import Decimal

import pytest

from lararium.modules.finance.errors import FinanceValidationError
from lararium.modules.finance.money import require_amount, to_plain_str, tolerance


def test_tolerance_is_half_of_last_decimal_place() -> None:
    assert tolerance(2) == Decimal("0.005")
    assert tolerance(0) == Decimal("0.5")
    assert tolerance(18) == Decimal("5E-19")


def test_to_plain_str_never_uses_exponent() -> None:
    assert to_plain_str(Decimal("1E-18")) == "0.000000000000000001"
    assert to_plain_str(Decimal("1.50")) == "1.50"
    assert to_plain_str(Decimal("-2E+3")) == "-2000"


@pytest.mark.parametrize("value", ["1" * 41, "0." + "1" * 41, "1E+40", "-" + "9" * 41])
def test_amount_with_more_digits_than_stored_is_rejected(value) -> None:
    with pytest.raises(FinanceValidationError, match="digits"):
        require_amount("quantity", Decimal(value))


@pytest.mark.parametrize("value", ["9" * 40 + "." + "9" * 40, "-0." + "0" * 39 + "1", "1E+39"])
def test_amount_at_the_digit_limits_is_accepted(value) -> None:
    require_amount("quantity", Decimal(value))


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "sNaN"])
def test_non_finite_amount_is_rejected(value) -> None:
    with pytest.raises(FinanceValidationError, match="finite"):
        require_amount("quantity", Decimal(value))
