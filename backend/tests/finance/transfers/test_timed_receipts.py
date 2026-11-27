"""A receipt does not come before its payment (account type catalog design §8.3 item 4).

M4 §5.8's import-coverage gate takes a one-sided counterpart on a manual account only from
the first day that account's imports list in full, asked from the start of the identified
record's window: a day before a sending side's booking date, because a date-only statement
can date a payment after the money left. When the identified record sends and its time is
real (a connector's record, or an imported row timed to the second), and the candidate is an
imported row timed to the second, no earlier than the sending instant minus the near window,
the gate asks only from that instant's day in the owner's time zone (pairs._gate_day). It
only ever lets the gate accept a pair it used to refuse: windows and tiers are unchanged.

E11 (the property): for generated records, turning their `timed` on only adds same-amount
edges, each a one-sided or weak pair the rule above lets through, and never removes or
changes one."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from hypothesis import event, given, note, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.timeutil import local_date
from lararium.modules.finance.accounts import AccountRecord
from lararium.modules.finance.identifiers import get_or_create_identifier
from lararium.modules.finance.system_accounts import system_account_ids
from lararium.modules.finance.transfers import list_transfers, load_transfer_settings
from lararium.modules.finance.transfers.pairs import (
    ONE_SIDED,
    WEAK,
    Known,
    Pairable,
    _Decisions,
    _registry_edges,
    counterpart_bounds,
    pair_relation,
)
from lararium.modules.finance.transfers.settings import DEFAULTS

from .conftest import (
    B_IDENTIFIER,
    DAY,
    ConnectorTx,
    Heal,
    ImportedBank,
    MakeConnected,
    Run,
    StatementRow,
    assert_consistent,
)

LONDON = ZoneInfo("Europe/London")  # the owner's time zone (core.timezone's default)
BEFORE = DAY - timedelta(days=1)
SUMMER = date(2026, 7, 1)  # British Summer Time: 00:30 in London is 23:30 UTC the day before


def _at(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute, second), LONDON)


async def _relation_and_transfer(session: AsyncSession, sent: UUID) -> tuple[dict, object]:
    relation = await pair_relation(
        session, await system_account_ids(session), await load_transfer_settings(session)
    )
    [transfer] = [
        t
        for t in await list_transfers(session)
        if sent in (t.out_transaction_id, t.in_transaction_id)
    ]
    return dict(relation.tiers.get(sent, {})), transfer


@pytest.mark.parametrize(
    ("sent_at", "received_at", "time_unit", "listed_from", "pairs"),
    [
        # Received 30 s after it left, timed to the second: the window started the day
        # before, which no upload lists, but the receipt can't precede the payment.
        (_at(DAY, 10), _at(DAY, 10, 0, 30), "second", DAY, True),
        # The row gives only its date (midnight), or its time to the minute: unchanged.
        (_at(DAY, 10), _at(DAY, 0), "day", DAY, False),
        (_at(DAY, 10), _at(DAY, 10, 1), "minute", DAY, False),
        # Received more than the near window before the payment left: unchanged.
        (_at(DAY, 10), _at(DAY, 9, 50), "second", DAY, False),
        # Two minutes before 00:01 is the day before, which the upload doesn't list.
        (_at(DAY, 0, 1), _at(DAY, 0, 1, 30), "second", DAY, False),
        (_at(DAY, 0, 1), _at(DAY, 0, 1, 30), "second", BEFORE, True),  # until it does
        # The day is the owner's: 00:30 in London (23:30 UTC the day before) is on SUMMER.
        (_at(SUMMER, 0, 30), _at(SUMMER, 0, 30, 30), "second", SUMMER, True),
    ],
)
async def test_a_timed_receipt_is_taken_from_its_payment_s_day(
    session: AsyncSession,
    run: Run,
    connected_bank: MakeConnected,
    connector_tx: ConnectorTx,
    imported_bank: ImportedBank,
    statement_row: StatementRow,
    heal: Heal,
    sent_at: datetime,
    received_at: datetime,
    time_unit: str,
    listed_from: date,
    pairs: bool,
) -> None:
    """A connected bank's bank transfer names the manual account B; B's one upload lists in
    full from `listed_from`, with a same-amount bank-transfer row naming nobody."""
    a = await connected_bank("A")
    b = await imported_bank("B", identifier=B_IDENTIFIER)
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    sent = await connector_tx(a, "-100", occurred_at=sent_at, identifier_id=identifier)
    row = await statement_row(
        b,
        "100",
        at=received_at,
        time_unit=time_unit,
        complete=(listed_from, listed_from + timedelta(days=60)),
    )
    await heal()

    tiers, transfer = await _relation_and_transfer(session, sent.id)
    if pairs:
        assert tiers[row] == ONE_SIDED
        assert (transfer.status, transfer.in_transaction_id) == ("matched", row)
    else:
        assert row not in tiers
        assert transfer.in_transaction_id is None
    assert await heal() == 0
    await assert_consistent(session)


@pytest.mark.parametrize(
    ("sent_at", "sent_unit", "pairs"),
    [
        (_at(DAY, 10), "second", True),  # an imported row timed to the second is timed
        (_at(DAY, 10), "minute", False),  # one timed to the minute is not
        (_at(DAY, 0), "day", False),  # nor one giving only its date
    ],
)
async def test_an_imported_sender_is_timed_only_to_the_second(
    session: AsyncSession,
    run: Run,
    imported_bank: ImportedBank,
    statement_row: StatementRow,
    heal: Heal,
    sent_at: datetime,
    sent_unit: str,
    pairs: bool,
) -> None:
    """A's imported row names B (identified); B's row, naming nobody, is timed to the
    second 30 s after 10:00; B's upload lists in full from DAY only."""
    a = await imported_bank("A")
    b: AccountRecord = await imported_bank("B", identifier=B_IDENTIFIER)
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    sent = await statement_row(a, "-100", at=sent_at, time_unit=sent_unit, identifier_id=identifier)
    row = await statement_row(
        b,
        "100",
        at=_at(DAY, 10, 0, 30),
        time_unit="second",
        complete=(DAY, DAY + timedelta(days=60)),
    )
    await heal()

    tiers, transfer = await _relation_and_transfer(session, sent)
    if pairs:
        assert tiers[row] == ONE_SIDED
        assert (transfer.status, transfer.out_transaction_id) == ("matched", sent)
        assert transfer.in_transaction_id == row
    else:
        assert row not in tiers
        assert transfer.in_transaction_id is None
    assert await heal() == 0
    await assert_consistent(session)


# The rule on its own (pairs._registry_edges), and E11.

_NO_DECISIONS = _Decisions(frozenset(), frozenset(), ())
GBP = UUID(int=1)
SENDER, MANUAL = UUID(int=10), UUID(int=11)


def _pairable(account: UUID, quantity: str, at: datetime, **kw) -> Pairable:
    defaults = {
        "id": uuid4(),
        "source": ("connection", str(uuid4())),
        "instrument_id": GBP,
        "booking_date": local_date(at, LONDON),
        "holder": None,
        "bank_transfer": True,
        "timed": True,
    }
    return Pairable(
        account_id=account,
        quantity=Decimal(quantity),
        occurred_at=at.astimezone(UTC),
        **(defaults | kw),
    )


def _pairs(sender: Pairable, receiver: Pairable, known: Known) -> bool:
    same, _ = _registry_edges([sender, receiver], DEFAULTS, _NO_DECISIONS, known)
    return receiver.id in same.tiers.get(sender.id, {})


def _listed(zone: ZoneInfo | None = LONDON) -> Known:
    """MANUAL's imports list in full from DAY, in the owner's time zone (`zone`)."""
    return Known(frozenset({MANUAL}), {(MANUAL, GBP): DAY}, zone)


def test_only_an_imported_row_is_a_timed_receipt() -> None:
    """The candidate must be an imported row timed to the second: a connector's record on a
    manual account (which no connector writes) isn't one. A Known built without the owner's
    time zone relaxes nothing."""
    sender = _pairable(SENDER, "-100", _at(DAY, 10), holder=MANUAL)
    receipt = _pairable(MANUAL, "100", _at(DAY, 10, 0, 30), source=("import", "k"))
    assert _pairs(sender, receipt, _listed())
    assert not _pairs(sender, replace(receipt, timed=False), _listed())
    assert not _pairs(sender, replace(receipt, source=("connection", "c:1")), _listed())
    assert not _pairs(sender, receipt, _listed(zone=None))
    # The identified side must send: a receipt it names isn't vouched for by a payment.
    assert not _pairs(
        _pairable(MANUAL, "-100", _at(DAY, 10, 0, 30), source=("import", "k")),
        _pairable(SENDER, "100", _at(DAY, 10), holder=MANUAL),
        _listed(),
    )


def test_a_weak_pair_is_relaxed_the_same_way() -> None:
    """A sort code alone (pairs.weak_tier) names the manual account: the same gate, from the
    record whose sort code names it."""
    sender = _pairable(SENDER, "-100", _at(DAY, 10), weak_holder=MANUAL)
    receipt = _pairable(MANUAL, "100", _at(DAY, 10, 0, 30), source=("import", "k"))
    assert _pairs(sender, receipt, _listed())
    assert not _pairs(sender, replace(receipt, occurred_at=_at(DAY, 9, 50)), _listed())
    assert not _pairs(replace(sender, timed=False), receipt, _listed())


_ACCOUNTS = (SENDER, MANUAL, UUID(int=12), UUID(int=13))  # the last two manual too
_SOURCES = ("connection", "import", "transaction")
# Times of day around midnight and 10:00, so that a payment's day and the day before meet.
_NEAR = [time(0), time(0, 0, 30), time(0, 1), time(0, 2, 30), time(9, 57, 59), time(10)]
_TIME_OF_DAY = st.sampled_from(_NEAR) | st.times()
# How long after its payment a receipt is (negative: before it), in seconds: around the
# near window's edge (120 s) often, else anything in a day either way.
_DELAY = st.sampled_from((-121, -120, -119, 0, 30, 120)) | st.integers(-86400, 86400)


@st.composite
def _record(
    draw: st.DrawFn, account: UUID, quantity: str, at: datetime, named: UUID | None
) -> Pairable:
    """A record of any source, naming `named` (when given) in full or by its sort code
    alone, or naming nobody; `timed` drawn (turned off for the comparison), but never for a
    record entered by hand: eligible_records gives it only to a connector's record or an
    imported row."""
    names = "nobody" if named is None else draw(st.sampled_from(("holder", "weak", "nobody")))
    source = draw(st.sampled_from(_SOURCES))
    return _pairable(
        account,
        quantity,
        at.replace(microsecond=0),
        source=(source, str(uuid4())),
        holder=named if names == "holder" else None,
        weak_holder=named if names == "weak" else None,
        timed=source != "transaction" and draw(st.booleans()),
    )


@st.composite
def _records(draw: st.DrawFn) -> list[Pairable]:
    """One to four transfers between SENDER (connected) and three manual accounts, of 100
    or 50, each a payment from the day before DAY to the day after it and a receipt
    `_DELAY` after it, either naming the other's account or nobody; and up to three records
    of noise of the same amounts, each naming any account or nobody."""
    records = []
    for _ in range(draw(st.integers(min_value=1, max_value=4))):
        payer, payee = draw(st.permutations(_ACCOUNTS))[:2]
        amount = draw(st.sampled_from(("100", "50")))
        day = DAY + timedelta(days=draw(st.integers(min_value=-1, max_value=1)))
        sent = datetime.combine(day, draw(_TIME_OF_DAY), LONDON)
        received = sent + timedelta(seconds=draw(_DELAY))
        records.append(draw(_record(payer, f"-{amount}", sent, draw(st.none() | st.just(payee)))))
        records.append(draw(_record(payee, amount, received, draw(st.none() | st.just(payer)))))
    for _ in range(draw(st.integers(min_value=0, max_value=3))):
        account = draw(st.sampled_from(_ACCOUNTS))
        named = draw(st.none() | st.sampled_from([x for x in _ACCOUNTS if x != account]))
        day = DAY + timedelta(days=draw(st.integers(min_value=-1, max_value=1)))
        at = datetime.combine(day, draw(_TIME_OF_DAY), LONDON)
        quantity = draw(st.sampled_from(("-100", "100", "-50", "50")))
        records.append(draw(_record(account, quantity, at, named)))
    return records


@st.composite
def _known(draw: st.DrawFn) -> Known:
    """The manual accounts' first days listed in full: none, or from the day before DAY to
    the day after it."""
    first = {}
    for account in _ACCOUNTS[1:]:
        offset = draw(st.none() | st.integers(min_value=-1, max_value=1))
        if offset is not None:
            first[(account, GBP)] = DAY + timedelta(days=offset)
    return Known(frozenset(_ACCOUNTS[1:]), first, LONDON)


def _relaxed(sender: Pairable, other: Pairable, known: Known) -> bool:
    """Whether the rule lets the pair through the gate it would otherwise not pass: the
    gate's sender (`sender`: the identified side, or the one whose sort code names the
    other's account) sends and is timed, the other is an imported row timed to the second
    no earlier than the sending instant less the near window, and its account is listed in
    full from that instant's day (in the owner's time zone) but not from the window's
    start."""
    sent = sender.occurred_at - DEFAULTS.near_window
    return (
        sender.sends
        and sender.timed
        and other.timed
        and other.source[0] != "connection"
        and other.occurred_at >= sent
        and known.holds_from(other, local_date(sent, LONDON))
        and not known.holds_from(other, counterpart_bounds(sender, DEFAULTS)[0])
    )


@settings(max_examples=300, deadline=None)
@given(records=_records(), known=_known())
def test_turning_timed_on_only_adds_pairs_a_timed_receipt_lets_through(
    records: list[Pairable], known: Known
) -> None:
    """E11: the same-amount edges with every record's `timed` off are kept, with their
    tiers, when it is on; each one added is ONE_SIDED or WEAK, and the rule lets it
    through (_relaxed)."""
    by_id = {x.id: x for x in records}
    untimed = [replace(x, timed=False) for x in records]
    off, _ = _registry_edges(untimed, DEFAULTS, _NO_DECISIONS, known)
    on, _ = _registry_edges(records, DEFAULTS, _NO_DECISIONS, known)

    def edges(found: dict[UUID, dict[UUID, int]]) -> dict[tuple[UUID, UUID], int]:
        return {(r, c): tier for r, others in found.items() for c, tier in others.items()}

    before, after = edges(off.tiers), edges(on.tiers)
    note(f"edges off {len(before)}, on {len(after)}")
    assert {pair: after.get(pair) for pair in before} == before
    added = {pair: tier for pair, tier in after.items() if pair not in before}
    for (r, c), tier in added.items():
        x, y = by_id[r], by_id[c]
        assert tier in (ONE_SIDED, WEAK), (x, y, tier)
        s, c = (x, y) if x.sends else (y, x)
        if tier == ONE_SIDED:
            sender, other = (x, y) if x.identified else (y, x)
        else:  # the sending side, when its sort code names the other's account (_weak_known)
            sender, other = (s, c) if s.weak_holder == c.account_id else (c, s)
        assert _relaxed(sender, other, known), (sender, other)
    event("a timed receipt added a pair" if added else "no pair added")
