"""The matching window is counted in booking dates (design §5.7): the receiving side's
booking date from 1 calendar day before to 5 after the sending side's, both included,
whatever the hour each side carries. Statement rows carry only a date (stored at 00:00), so
a window between instants would lose up to a day, and in one direction only."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from lararium.core.timeutil import local_date
from lararium.modules.finance.identifiers import get_or_create_identifier
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    post_transaction,
)
from lararium.modules.finance.transfers import (
    due_notifications,
    list_transfers,
    preview_match,
    transfer_contra,
)
from lararium.modules.finance.transfers.pairs import (
    Known,
    Pairable,
    _Decisions,
    _registry_edges,
    counterpart_bounds,
)
from lararium.modules.finance.transfers.settings import DEFAULTS

from .conftest import (
    B_IDENTIFIER,
    IMPORTED,
    assert_consistent,
    balances_on,
    net_worth_on,
    placeholders,
)

LONDON = ZoneInfo("Europe/London")
# A Friday, and a Friday whose next Sunday ends summer time (25 October 2026).
FRIDAY = date(2026, 11, 6)
FRIDAY_BEFORE_DST_ENDS = date(2026, 10, 23)


def _at(day: date, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=LONDON)


UUID_GBP = uuid4()
_NO_DECISIONS = _Decisions(frozenset(), frozenset(), ())
# Every account's own rows known, as a connected account's are.
_ALL_KNOWN = Known(frozenset(), {})


def _record(at: datetime, quantity: str, *, identified: bool) -> Pairable:
    account, other = uuid4(), uuid4()
    return Pairable(
        id=uuid4(),
        source=("transaction", str(uuid4())),
        account_id=account,
        instrument_id=UUID_GBP,
        quantity=Decimal(quantity),
        occurred_at=at.astimezone(UTC),
        booking_date=local_date(at, LONDON),
        holder=other if identified else None,
        bank_transfer=True,
    )


def _pairs(sender: Pairable, receiver: Pairable) -> bool:
    # The receiver names the sender's account; the sender names nobody (a statement row).
    receiver = replace(receiver, holder=sender.account_id)
    same, _ = _registry_edges([sender, receiver], DEFAULTS, _NO_DECISIONS, _ALL_KNOWN)
    return receiver.id in same.tiers.get(sender.id, {})


@pytest.mark.parametrize("friday", [FRIDAY, FRIDAY_BEFORE_DST_ENDS])
@pytest.mark.parametrize("sender_hour", [0, 15])  # date-only (00:00), or timestamped
@pytest.mark.parametrize(
    ("days", "hour", "minute", "pairs"),
    [
        (5, 23, 59, True),  # the last minute of the 5th calendar day after
        (5, 0, 0, True),  # a date-only row on that day
        (6, 0, 0, False),  # the 6th calendar day
        (-1, 0, 0, True),  # the calendar day before
        (-1, 23, 59, True),
        (-2, 23, 59, False),  # two calendar days before
    ],
)
def test_window_is_counted_in_booking_dates(friday, sender_hour, days, hour, minute, pairs):
    sender = _record(_at(friday, sender_hour), "-500", identified=False)
    receiver = _record(_at(friday + timedelta(days=days), hour, minute), "500", identified=True)
    assert _pairs(sender, receiver) is pairs


def test_bounds_are_dates_both_ways_round() -> None:
    sender = _record(_at(FRIDAY, 15), "-500", identified=False)
    receiver = _record(_at(FRIDAY, 15), "500", identified=True)
    assert counterpart_bounds(sender, DEFAULTS) == (FRIDAY - timedelta(1), FRIDAY + timedelta(5))
    assert counterpart_bounds(receiver, DEFAULTS) == (FRIDAY - timedelta(5), FRIDAY + timedelta(1))


@pytest.fixture
def statement_row(session, run, gbp, record_import):
    """A row as an M2 statement import posts it: dated by its day alone (00:00), naming
    nobody, a bank transfer."""

    async def _post(account, quantity: str, day: date):
        contra = await transfer_contra(
            session,
            source=None,
            own_account_id=account.id,
            counterparty_identifier_id=None,
            other_postings=0,
        )
        amount = Decimal(quantity)
        row = await run(
            post_transaction,
            TransactionInput(
                occurred_at=_at(day),
                description="Faster payment",
                origin="manual",
                postings=[
                    PostingInput(account.id, gbp.id, amount),
                    PostingInput(contra, gbp.id, -amount),
                ],
                counterparty=CounterpartyFields(bank_transfer=True),
            ),
        )
        await record_import(account.id, [row.id])
        return row

    return _post


async def test_weekend_bacs_from_a_statement_row_pairs(
    session, run, ids, connected_bank, manual_bank, connector_tx, statement_row, heal
) -> None:
    """B's statement row on Friday, A's Monzo credit naming B on Wednesday at 10:00: the 5th
    calendar day, so one transfer, B down 500 once, net worth unchanged."""
    a = await connected_bank("A")
    b = await manual_bank("B")
    worth_before = await net_worth_on(session, FRIDAY + timedelta(days=7))
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    row = await statement_row(b, "-500", FRIDAY)
    received = await connector_tx(
        a, "500", occurred_at=_at(FRIDAY + timedelta(days=5), 10), identifier_id=identifier
    )
    await heal()

    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.out_transaction_id, transfer.in_transaction_id) == (
        "matched",
        row.id,
        received.id,
    )
    assert await placeholders(session) == []
    later = FRIDAY + timedelta(days=7)
    balances = await balances_on(session, later)
    assert (balances[b.id], balances[a.account.id], balances[ids.in_transit]) == (
        Decimal(500),
        Decimal(500),
        0,
    )
    assert await net_worth_on(session, later) == worth_before
    await assert_consistent(session)
    assert await heal() == 0


@pytest.mark.parametrize("give_booking_date", [False, True])
async def test_preview_of_a_date_only_row_agrees_with_posting_it(
    session,
    run,
    gbp,
    connected_bank,
    manual_bank,
    connector_tx,
    statement_row,
    heal,
    give_booking_date,
) -> None:
    a = await connected_bank("A")
    b = await manual_bank("B")
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    received = await connector_tx(
        a, "500", occurred_at=_at(FRIDAY + timedelta(days=5), 10), identifier_id=identifier
    )
    await heal()

    previewed = await preview_match(
        session,
        account_id=b.id,
        instrument_id=gbp.id,
        quantity=Decimal(-500),
        occurred_at=_at(FRIDAY),
        booking_date=FRIDAY if give_booking_date else None,
        counterparty=CounterpartyFields(bank_transfer=True),
        complete=IMPORTED,
    )

    assert previewed == received.id
    row = await statement_row(b, "-500", FRIDAY)
    await heal()
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.out_transaction_id, transfer.in_transaction_id) == (
        "matched",
        row.id,
        received.id,
    )


async def test_arrival_after_the_window_on_a_manual_account_double_counts_silently(
    session, run, connected_bank, manual_bank, connector_tx, statement_row, heal
) -> None:
    """The residual (design §5.8 Known caveats): a counterpart booked more than 5 calendar
    days after is no candidate. With a manual counterpart whose row names nobody, the
    placeholder and the row both count, and nothing alerts."""
    a = await connected_bank("A")
    b = await manual_bank("B")
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    await statement_row(b, "-500", FRIDAY)
    await connector_tx(
        a, "500", occurred_at=_at(FRIDAY + timedelta(days=6), 10), identifier_id=identifier
    )
    await heal()

    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.review_reason) == ("inferred", None)
    assert (await balances_on(session, FRIDAY + timedelta(days=7)))[b.id] == 0  # 1000, -500 twice
    assert await due_notifications(session, _at(FRIDAY + timedelta(days=60))) == []
    await assert_consistent(session)


def test_imports_vouch_for_their_span() -> None:
    """Design §5.8: a manual account's row is known from the first day its imports list in
    full to the last, across the edge days of back-to-back exports no file lists in full,
    and not outside that span."""
    record = _record(_at(FRIDAY), "500", identified=False)
    other = replace(record, instrument_id=uuid4())
    november = (date(2026, 11, 2), date(2026, 11, 29))
    december = (date(2026, 12, 2), date(2026, 12, 30))
    known = Known(
        frozenset({record.account_id}), {(record.account_id, UUID_GBP): [december, november]}
    )

    assert known.lists_in_full(record, date(2026, 11, 28), date(2026, 12, 3))
    assert known.lists_in_full(record, *november)
    assert not known.lists_in_full(record, date(2026, 11, 1), date(2026, 11, 5))
    assert not known.lists_in_full(record, date(2026, 12, 28), date(2026, 12, 31))
    assert not known.lists_in_full(other, *november)  # nothing imported in it
    assert Known(frozenset(), {}).lists_in_full(record, *november)  # a connected account
