"""Decision 13's walk and final review C1 as pure functions: plan_openings, opening_conflicts
and opening_adjustments, without a database."""

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from lararium.modules.finance.importing.parsed import ParsedRow
from lararium.modules.finance.importing.staging import (
    Movement,
    OpeningClaim,
    opening_adjustments,
    opening_conflicts,
    plan_openings,
)

EUR, GBP = uuid4(), uuid4()


def _day(n: int) -> date:
    return date(2026, 11, n)


def _claim(
    batch: UUID,
    opening: str,
    first: int,
    *,
    committed: int | None,
    in_full: bool = False,
    vouched_to: int | None = None,
    own: frozenset[UUID] = frozenset(),
    first_day_sources: frozenset[UUID] = frozenset(),
) -> OpeningClaim:
    return OpeningClaim(
        batch,
        EUR,
        Decimal(opening),
        _day(first),
        None if committed is None else datetime(2026, 12, committed, tzinfo=UTC),
        own=own,
        vouched_from=_day(first),
        first_day_in_full=in_full,
        vouched_to=_day(vouched_to if vouched_to is not None else first - 1),
        first_day_sources=first_day_sources,
    )


def _conflicted(claims: list[OpeningClaim], movements: list[Movement]) -> set[UUID]:
    plan = plan_openings(claims, {EUR: movements})
    return {conflict.batch_id for conflict in opening_conflicts(claims, plan)}


def test_shared_first_row_conflicts_in_either_order() -> None:
    """Fourth review: vouching came from the batch's first day over every instrument, so an
    EUR opening that starts later was unguarded: accepted one way round, refused the other."""
    a, b, row = uuid4(), uuid4(), uuid4()
    movements = [Movement(_day(10), Decimal("-5"), row)]
    for a_first in (True, False):
        claim_a = _claim(
            a,
            "100",
            10,
            committed=1 if a_first else 2,
            own=frozenset({row}),
            first_day_sources=frozenset({row}),
        )
        claim_b = _claim(
            b,
            "150",
            10,
            committed=2 if a_first else 1,
            own=frozenset({row}),
            first_day_sources=frozenset({row}),
        )
        assert _conflicted([claim_a, claim_b], movements), a_first


def test_partly_listed_first_day_after_full_days_is_a_gap_dated_on_it() -> None:
    """Fourth review: a claim opening partway through the day after an earlier file's full
    days was taken as vouched at stage, while a rollback then posted its adjustment the day
    before, inside those full days."""
    earlier, later = uuid4(), uuid4()
    claims = [
        _claim(earlier, "0", 1, committed=1, vouched_to=4),  # lists 2-4 in full
        _claim(later, "18", 5, committed=2),  # opens partway through 5
    ]
    movements = [
        Movement(_day(1), Decimal("10"), uuid4()),
        Movement(_day(3), Decimal("5"), uuid4()),
    ]
    assert _conflicted(claims, movements) == set()  # the 3.00 is a gap, not a disagreement
    assert claims[1].adjustment_day == _day(5)
    full = replace(claims[1], first_day_in_full=True)
    assert full.adjustment_day == _day(4)
    assert _conflicted([claims[0], full], movements) == {later}  # a start of day 4 vouches


def _row(position: int, day: int, quantity: str) -> ParsedRow:
    return ParsedRow(
        position=position,
        booking_date=_day(day),
        occurred_at=None,
        description="x",
        quantity=Decimal(quantity),
        fee=Decimal(0),
        currency=None,
        balance=None,
        external_id=None,
        bank_type=None,
        raw={},
    )


def test_closing_anchors_only_its_own_currency() -> None:
    """Fourth review: an OFX closing (in CURDEF, GBP) became the opening of a chain of EUR
    rows, posting a GBP amount as EUR."""
    rows = [_row(0, 5, "-10"), _row(1, 6, "-20")]
    closing = (Decimal("500"), _day(30), GBP)
    assert opening_adjustments(rows, lambda _: EUR, closing, "oldest_first") == {}
    assert opening_adjustments(rows, lambda _: GBP, closing, "oldest_first") == {
        GBP: (Decimal("530"), _day(5))
    }


def test_same_first_day_walk_order_follows_where_openings_sit() -> None:
    """Fifth review: a first-day tie was broken by commit time, so which batch held the
    adjustment, and its day, depended on import order. A statement listing its days in full
    opens at the start of its period; an export opening partway through the day comes after."""
    pdf, csv, row = uuid4(), uuid4(), uuid4()
    movements = [Movement(_day(3), Decimal("-5"), row)]
    plans = []
    for pdf_first in (True, False):
        statement = replace(
            _claim(pdf, "110", 3, committed=1 if pdf_first else 2, own=frozenset({row})),
            first_day_in_full=True,
            vouched_from=_day(1),
            vouched_to=_day(30),
        )
        export = _claim(csv, "110", 3, committed=2 if pdf_first else 1, own=frozenset({row}))
        plans.append(plan_openings([statement, export], {EUR: movements}))
        assert opening_conflicts([statement, export], plans[-1]) == []
        assert (statement.opens_on, statement.adjustment_day) == (_day(1), date(2026, 10, 31))
    assert plans[0] == plans[1] == {(pdf, EUR): Decimal("110"), (csv, EUR): Decimal("0")}


def test_any_shared_first_day_row_vouches_whatever_the_file_order() -> None:
    """Sixth review: the 'first row' was taken by file position, so a newest-first OFX named
    its latest row and a disagreement with the earlier file went unseen."""
    earlier, later, shared, newest = uuid4(), uuid4(), uuid4(), uuid4()
    claims = [
        _claim(earlier, "100", 1, committed=1, own=frozenset({shared}), vouched_to=19),
        _claim(
            later,
            "107",
            20,
            committed=2,
            own=frozenset({shared, newest}),
            first_day_sources=frozenset({shared}),
        ),
    ]
    movements = [Movement(_day(20), Decimal("-10"), shared)]
    assert _conflicted(claims, movements) == {later}
