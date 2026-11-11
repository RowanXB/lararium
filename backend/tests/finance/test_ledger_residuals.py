from decimal import Decimal
from uuid import uuid4

from financial_gate.modules.finance.ledger import PostingInput, compute_residuals

A, B, C, GBP, USD, ETH = (uuid4() for _ in range(6))


def test_simple_transfer_balances() -> None:
    assert (
        compute_residuals(
            [
                PostingInput(A, GBP, Decimal("-100.00")),
                PostingInput(B, GBP, Decimal("100.00")),
            ]
        )
        == {}
    )


def test_price_converts_fx_leg() -> None:
    assert (
        compute_residuals(
            [
                PostingInput(A, GBP, Decimal("-100")),
                PostingInput(
                    B, USD, Decimal("125"), price_per_unit=Decimal("0.8"), price_instrument_id=GBP
                ),
            ]
        )
        == {}
    )


def test_imbalance_reported_per_instrument() -> None:
    assert compute_residuals(
        [
            PostingInput(A, GBP, Decimal("-100")),
            PostingInput(B, GBP, Decimal("99.99")),
        ]
    ) == {GBP: Decimal("-0.01")}


def test_eighteen_decimals_sum_exactly() -> None:
    assert (
        compute_residuals(
            [
                PostingInput(A, ETH, Decimal("-1.000000000000000001")),
                PostingInput(B, ETH, Decimal("0.999999999999999000")),
                PostingInput(C, ETH, Decimal("0.000000000000001001"), is_fee=True),
            ]
        )
        == {}
    )
