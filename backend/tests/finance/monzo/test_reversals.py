"""Which Monzo reversal cancels which authorisation (card refunds design §4.3)."""

from typing import Any

from lararium.modules.finance.monzo.reversals import reversal_pairs

from .fixtures import make_transaction

LIFECYCLE = "mclifecycle_TEST1"


def _card(id: str, amount: int, created: str, **overrides: Any) -> dict[str, Any]:
    """A card transaction on LIFECYCLE: an unsettled authorisation unless overridden."""
    metadata = {"mastercard_lifecycle_id": LIFECYCLE} | overrides.pop("metadata", {})
    return (
        make_transaction(
            id=id,
            amount=amount,
            local_amount=amount,
            created=created,
            settled="",
            scheme="mastercard",
            metadata=metadata,
        )
        | overrides
    )


def _reversal(id: str, amount: int, created: str, **overrides: Any) -> dict[str, Any]:
    return _card(id, amount, created, metadata={"is_reversal": "true"}, **overrides)


def test_an_exact_reversal_pairs() -> None:
    authorisation = _card("tx_A", -1000, "2026-03-01T12:00:00.000Z")
    reversal = _reversal("tx_R", 1000, "2026-03-01T15:00:00.000Z")

    assert reversal_pairs([reversal, authorisation]) == {"tx_R": "tx_A"}


def test_a_shared_lifecycle_without_a_reversal_pairs_nothing() -> None:
    # One authorisation cleared as two settled payments under one lifecycle.
    settled = "2026-03-02T06:00:00.000Z"
    first = _card("tx_A", -700, "2026-03-01T12:00:00.000Z", settled=settled)
    second = _card("tx_B", -360, "2026-03-01T12:00:01.000Z", settled=settled)

    assert reversal_pairs([first, second]) == {}


def test_a_partial_reversal_pairs_nothing() -> None:
    authorisation = _card("tx_A", -1000, "2026-03-01T12:00:00.000Z")
    reversal = _reversal("tx_R", 700, "2026-03-01T15:00:00.000Z")

    assert reversal_pairs([authorisation, reversal]) == {}


def test_a_settled_authorisation_is_not_reversed() -> None:
    authorisation = _card(
        "tx_A", -1000, "2026-03-01T12:00:00.000Z", settled="2026-03-02T06:00:00.000Z"
    )
    reversal = _reversal("tx_R", 1000, "2026-03-01T15:00:00.000Z")

    assert reversal_pairs([authorisation, reversal]) == {}


def test_two_authorisations_of_one_amount_pair_the_latest() -> None:
    earlier = _card("tx_A1", -1000, "2026-03-01T12:00:00.000Z")
    later = _card("tx_A2", -1000, "2026-03-01T13:00:00.000Z")
    reversal = _reversal("tx_R", 1000, "2026-03-01T15:00:00.000Z")

    assert reversal_pairs([later, reversal, earlier]) == {"tx_R": "tx_A2"}


def test_a_declined_reversal_pairs_nothing() -> None:
    authorisation = _card("tx_A", -1000, "2026-03-01T12:00:00.000Z")
    reversal = _reversal(
        "tx_R", 1000, "2026-03-01T15:00:00.000Z", decline_reason="INSUFFICIENT_FUNDS"
    )

    assert reversal_pairs([authorisation, reversal]) == {}
