from decimal import Decimal

from lararium.modules.finance.errors import FinanceValidationError


def tolerance(decimals: int) -> Decimal:
    """Half of the last decimal place: amounts closer than this are equal at that precision."""
    return Decimal(1).scaleb(-decimals) / 2


def to_plain_str(value: Decimal) -> str:
    """Fixed-point notation, never an exponent; existing trailing zeros are kept."""
    return format(value, "f")


# Digits an amount may have on each side of the decimal point. Far more than any instrument
# needs (decimals go up to 30), and far below what PostgreSQL's numeric can store, so an
# accepted amount is always written exactly. schemas.DECIMAL_PATTERN mirrors this.
MAX_INTEGER_DIGITS = 40
MAX_FRACTION_DIGITS = 40


def require_amount(name: str, value: Decimal) -> None:
    """Reject what Decimal accepts but an amount can't be: NaN, infinities, and more digits
    than an amount may have."""
    if not value.is_finite():
        raise FinanceValidationError(f"{name} must be a finite number")
    _, digits, exponent = value.as_tuple()
    assert isinstance(exponent, int)  # finite
    fraction_digits = max(0, -exponent)
    integer_digits = max(0, len(digits) + exponent)
    if integer_digits > MAX_INTEGER_DIGITS or fraction_digits > MAX_FRACTION_DIGITS:
        raise FinanceValidationError(
            f"{name} may have at most {MAX_INTEGER_DIGITS} digits before the decimal point"
            f" and {MAX_FRACTION_DIGITS} after it"
        )
