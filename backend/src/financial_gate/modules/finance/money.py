from decimal import Decimal


def tolerance(decimals: int) -> Decimal:
    """Half of the last decimal place: amounts closer than this are equal at that precision."""
    return Decimal(1).scaleb(-decimals) / 2


def to_plain_str(value: Decimal) -> str:
    """Fixed-point notation, never an exponent; existing trailing zeros are kept."""
    return format(value, "f")


def require_finite(name: str, value: Decimal) -> None:
    """Raise ValueError for NaN and infinities, which Decimal accepts but amounts can't be."""
    if not value.is_finite():
        raise ValueError(f"{name} must be a finite number")
