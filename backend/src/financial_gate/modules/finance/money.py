from decimal import Decimal


def tolerance(decimals: int) -> Decimal:
    """Half of the last decimal place: amounts closer than this are equal at that precision."""
    return Decimal(1).scaleb(-decimals) / 2


def to_plain_str(value: Decimal) -> str:
    """Fixed-point notation, never an exponent; existing trailing zeros are kept."""
    return format(value, "f")
