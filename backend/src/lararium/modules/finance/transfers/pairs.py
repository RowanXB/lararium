"""The pair relation (design §5.7): which own records could be one transfer's two sides.

It is computed once, set-wise, from the own records that can pair at all (a bank transfer,
or one naming a bank account or a card merchant, or one on an account a card merchant pays
into or whose type is funded by bank transfer, or one the owner paired) and the owner's
decisions, and then read by matching, heal and review alike. It depends only on the records
and the decisions, never on which transfers exist, and nothing matching writes changes it
(re-pointing a contra, opening or dissolving a transfer, posting a placeholder), so one
computation holds for a whole heal pass."""

from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from lararium.modules.finance import models
from lararium.modules.finance.ledger import PostingInput
from lararium.modules.finance.system_accounts import SystemAccountKey
from lararium.modules.finance.transfers.contra import (
    LegFee,
    SourceRef,
    eligible_records,
    leg_fee_columns,
    names_source,
)
from lararium.modules.finance.transfers.settings import TransferSettings

# How strong the evidence is that two records are one transfer, strongest first: the
# owner paired them; each names the other's account; only one names the other's account
# (the other is a bank transfer naming nobody, or names nobody on an account funded by bank
# transfer); only one prints the sort code of the other's account, and nothing else of it
# (the other names no bank account at all). Two records in different currencies that name
# each other's accounts are MUTUAL too (cross_currency_tier).
type Tier = Literal[1, 2, 3, 4]
DECIDED: Tier = 1
MUTUAL: Tier = 2
ONE_SIDED: Tier = 3
WEAK: Tier = 4


@dataclass(frozen=True)
class Pairable:
    """An own record (contra.eligible_records) with what pairing reads."""

    id: UUID
    source: SourceRef
    account_id: UUID
    instrument_id: UUID
    quantity: Decimal  # signed: negative on the sending side
    occurred_at: datetime
    booking_date: date  # the window is counted in these
    holder: UUID | None  # the account its counterparty bank account is registered to
    bank_transfer: bool
    weak_holder: UUID | None = None  # the account its sort code alone names (weak_tier)
    names_account: bool = False  # it names a bank account, or prints part of one
    fiat: bool = False  # its instrument is a currency (cross_currency_tier)
    fee: LegFee | None = None  # the conversion fee its connector gives (conversion_postings)
    # The own accounts the card merchant it names pays into (contra.eligible_records'
    # holders); empty for any other record. Never `holder`: it doesn't identify the record.
    holders: frozenset[UUID] = frozenset()
    # Its account's type lists bank transfer among the ways it is funded
    # (contra.eligible_records' funded_by_transfer): a deposit naming nobody there may be
    # one, though its source doesn't say (account type catalog design §8.2).
    funded_by_transfer: bool = False

    @property
    def sends(self) -> bool:
        return self.quantity < 0

    @property
    def identified(self) -> bool:
        """Its counterparty bank account is registered to another own account."""
        return self.holder is not None and self.holder != self.account_id


def counterpart_bounds(record: Pairable, settings: TransferSettings) -> tuple[date, date]:
    """The booking dates the record's counterpart may have (Decision 4;
    queries.counterpart_window in SQL): the receiving side's from `before` calendar days
    before to `after` after the sending side's, both included. Counted in booking dates,
    not instants: a statement row carries only its date (00:00), so an instant window
    would lose up to a day of it, in one direction only."""
    before = timedelta(days=settings.window_before.days)
    after = timedelta(days=settings.window_after.days)
    if record.sends:
        return record.booking_date - before, record.booking_date + after
    return record.booking_date - after, record.booking_date + before


def _may_be_transfer(x: Pairable) -> bool:
    """Whether an unidentified record can be a transfer's side the registry pairs: it is
    marked a bank transfer, or it is on an account funded by bank transfer and names
    nobody: no own account, no card merchant, no bank account (_card_receiver's test
    without its "from a connection" clause)."""
    return x.bank_transfer or (
        x.funded_by_transfer and x.holder is None and not x.holders and not x.names_account
    )


def registry_tier(r: Pairable, c: Pairable) -> Tier | None:
    """The tier the registry gives two records of opposite sign, in the same instrument,
    the receiving one in the sending one's window, or None when it doesn't pair them: they
    must be on different accounts, at least one identified, each identified one naming the
    other's account and each other one a bank transfer, or a record naming nobody on an
    account funded by bank transfer (_may_be_transfer). A record naming a card merchant
    (holders) is none of these tiers' (card_tier pairs it). Decisions are not considered."""
    if c.account_id == r.account_id or not (r.identified or c.identified):
        return None
    if r.holders or c.holders:
        return None
    for x, other in ((r, c), (c, r)):
        if x.identified and x.holder != other.account_id:
            return None
        if not x.identified and not _may_be_transfer(x):
            return None
    return MUTUAL if r.identified and c.identified else ONE_SIDED


def _names_weakly(w: Pairable, other: Pairable) -> bool:
    """Whether w's sort code alone names the other's account, and the other names no bank
    account at all: a payment to someone else's account at that bank can't pair with an
    unrelated receipt there that names its sender's account (transfer evidence design
    §5.3)."""
    return w.weak_holder == other.account_id and not other.names_account


def weak_tier(r: Pairable, c: Pairable) -> Tier | None:
    """The tier a sort code alone gives two records as registry_tier takes them, or None
    when it doesn't pair them: they must be on different accounts, neither identified, both
    bank transfers, and one's sort code alone naming the other's account, which names no
    bank account at all. Weak evidence never identifies a record, so it pairs only records
    that are both there. Decisions are not considered."""
    if c.account_id == r.account_id or r.identified or c.identified:
        return None
    if not (r.bank_transfer and c.bank_transfer) or r.holders or c.holders:
        return None
    return WEAK if _names_weakly(r, c) or _names_weakly(c, r) else None


def cross_currency_tier(r: Pairable, c: Pairable) -> Tier | None:
    """The tier two records in different currencies get (eToro design §5.12), or None
    when they don't pair: on different accounts, both identified, each naming the other's
    account, of opposite sign, and in two different currencies. Amounts are not compared
    (the rate isn't known in advance), which is why both must name the other; time and
    uniqueness are the caller's (_cross_currency_edges). Decisions are not considered.

    It is not eToro's alone: any two own accounts in currencies whose records name each
    other's account pair this way. A row imported from a file giving dates only is stamped
    00:00, so for two such rows the near window (minutes) amounts to the same booking
    date."""
    if c.account_id == r.account_id or not (r.identified and c.identified):
        return None
    if r.holders or c.holders:
        return None
    if r.holder != c.account_id or c.holder != r.account_id or r.sends == c.sends:
        return None
    if not (r.fiat and c.fiat) or r.instrument_id == c.instrument_id:
        return None
    return MUTUAL


def _card_payer(x: Pairable) -> bool:
    """A connector's record naming a card merchant that pays into own accounts."""
    return bool(x.holders) and x.holder is None and x.source[0] == "connection"


def _card_receiver(x: Pairable) -> bool:
    """A connector's record that could be a card payment's other side: naming no own
    account, no card merchant and no bank account (a Trading 212 DEPOSIT or WITHDRAW)."""
    return (
        x.source[0] == "connection" and x.holder is None and not x.holders and not x.names_account
    )


def card_tier(r: Pairable, c: Pairable, window: timedelta) -> Tier | None:
    """The tier a card payment and a deposit get (eToro design §5.12, Monzo card deposits),
    or None when they don't pair: one (the payer) names a card merchant whose holders
    include the other's account; the other names nobody; same instrument, opposite signs and
    equal amounts; occurred_at at most `window` apart. Uniqueness is the caller's
    (card_partners). Decisions are not considered.

    ONE_SIDED, for the shape of its evidence: one side names the other's account (as one of
    the merchant's), the other names nobody. The value never competes with the same-amount
    tiers' (pair_relation keeps these pairs apart, as it does cross-currency ones), so it
    sets no precedence: a record any of those tiers pairs is never this one's.

    Both must be connectors' records. This rests on an assumption, not a check: that a
    connector's record carries a real time of day (Monzo's and Trading 212's do, to the
    second). An imported row from a file giving dates only is stamped 00:00 (its time_unit
    is display only, never read in matching), and a payment just after midnight would
    otherwise pair with it by coincidence, so such rows are paired by hand. An account
    whose connector posts date-only times (00:00), registered as a holder, could still
    pair by that coincidence at midnight."""
    if r.account_id == c.account_id or r.instrument_id != c.instrument_id:
        return None
    if r.sends == c.sends or r.quantity != -c.quantity:
        return None
    if abs(r.occurred_at - c.occurred_at) > window:
        return None
    for payer, other in ((r, c), (c, r)):
        if _card_payer(payer) and other.account_id in payer.holders and _card_receiver(other):
            return ONE_SIDED
    return None


def card_partners(
    records: Sequence[Pairable],
    window: timedelta,
    apart: Callable[[Pairable, Pairable], bool],
    taken: Collection[UUID],
) -> tuple[dict[UUID, frozenset[UUID]], dict[UUID, UUID]]:
    """(candidates, partners) of card_tier: each record's candidates no decision keeps
    `apart`, both ways round; and the pairs (both ways round) of records that are each
    other's only candidate, neither `taken` (paired, or a candidate, in another tier: an
    exact tier's or across currencies). Uniqueness is across all of a payer's holder
    accounts, and across all card payments for a deposit. A payer meets the receivers on
    each holder account in its instrument of its opposite amount, sorted by time."""
    edges = _Edges()
    receivers: dict[tuple[UUID, UUID, Decimal], list[Pairable]] = defaultdict(list)
    for x in records:
        if _card_receiver(x):
            receivers[(x.account_id, x.instrument_id, x.quantity)].append(x)
    by_time = {
        key: sorted(found, key=lambda x: (x.occurred_at, x.id)) for key, found in receivers.items()
    }
    moments = {key: [x.occurred_at for x in found] for key, found in by_time.items()}
    for s in records:
        if not _card_payer(s):
            continue
        for holder in s.holders:
            key = (holder, s.instrument_id, -s.quantity)
            found, times = by_time.get(key), moments.get(key)
            if found is None or times is None:
                continue
            earliest, latest = s.occurred_at - window, s.occurred_at + window
            for c in found[bisect_left(times, earliest) : bisect_right(times, latest)]:
                if card_tier(s, c, window) is not None and not apart(s, c):
                    edges.add(s.id, c.id, ONE_SIDED)
    candidates = {r: frozenset(found) for r, found in edges.tiers.items()}
    partners: dict[UUID, UUID] = {}
    for r, found in candidates.items():
        if len(found) != 1 or r in taken:
            continue
        [c] = found
        if candidates.get(c) == frozenset({r}) and c not in taken:
            partners[r] = c
    return candidates, partners


class PairSide(Protocol):
    """What deciding a settlement reads of a record (Pairable, or a record the owner pairs:
    queries.load_pair_side)."""

    @property
    def account_id(self) -> UUID: ...
    @property
    def instrument_id(self) -> UUID: ...
    @property
    def quantity(self) -> Decimal: ...  # signed: negative on the sending side
    @property
    def fiat(self) -> bool: ...
    @property
    def fee(self) -> LegFee | None: ...


def leg_fees(*records: PairSide) -> list[LegFee]:
    """The conversion fees the records' connectors give (Pairable.fee), those that have one."""
    return [x.fee for x in records if x.fee is not None]


def _conversion_fee(
    sent: tuple[UUID, Decimal], received: tuple[UUID, Decimal], fees: Sequence[LegFee]
) -> tuple[bool, LegFee | None]:
    """(fits, fee): the fee a conversion of `sent` (instrument, amount) into `received`
    takes, None for none (no leg has one, or it is 0); fits is False when none can be (both
    legs have one, or it is in a third currency, or not less than the leg's amount in its
    currency: a data error)."""
    charged = [fee for fee in fees if fee.amount != 0]
    if not charged:
        return True, None
    if len(charged) > 1:
        return False, None
    [fee] = charged
    amounts = dict((sent, received))
    if fee.instrument_id not in amounts or fee.amount >= amounts[fee.instrument_id]:
        return False, None
    return True, fee


def _difference_fits(
    sent: tuple[UUID, Decimal], received: tuple[UUID, Decimal], fees: Sequence[LegFee]
) -> bool:
    """Whether the difference between `sent` and `received`, in one instrument, can be a
    fee: the receiving side is smaller, and the legs' fees, if any, are in that instrument
    and come to no more than the difference (the whole difference is the fee either way)."""
    (instrument_id, sent_amount), (_, received_amount) = sent, received
    charged = [fee for fee in fees if fee.amount != 0]
    return (
        received_amount < sent_amount
        and all(fee.instrument_id == instrument_id for fee in charged)
        and sum((fee.amount for fee in charged), Decimal(0)) <= sent_amount - received_amount
    )


def settlement_fits(
    sent: tuple[UUID, Decimal], received: tuple[UUID, Decimal], fees: Sequence[LegFee]
) -> bool:
    """Whether conversion_postings gives a settlement of `sent` into `received`."""
    if sent[0] == received[0]:
        return _difference_fits(sent, received, fees)
    return _conversion_fee(sent, received, fees)[0]


def conversion_postings(
    sent: tuple[UUID, Decimal],
    received: tuple[UUID, Decimal],
    fees: Sequence[LegFee],
    *,
    in_transit: UUID,
    unassigned: UUID,
) -> list[PostingInput] | None:
    """The postings of the settlement of a transfer whose sides differ: `sent` (instrument,
    amount) left one own account and `received` arrived on the other, both through In
    transit, and the legs' fees (`fees`) give its fee, if any. On In transit, so it nets to
    zero there in each currency. None when no settlement fits (settlement_fits).

    In two currencies, the conversion (eToro design §5.12): the sent amount converted into
    the received one at their rate, the price carried by the sent currency's posting (which
    ledger._weight weighs in the received one), and the fee F, flagged as a fee, against
    Unassigned. A fee in the received currency was taken after the conversion, so the sent
    amount bought the received one plus F; one in the sent currency, before it, so only the
    sent amount less F was converted.

    In one currency, of a smaller received amount (a deposit net of a platform fee, which
    only the owner pairs): the difference, flagged as a fee, against Unassigned."""
    (sent_id, sent_amount), (received_id, received_amount) = sent, received
    if not settlement_fits(sent, received, fees):
        return None
    if sent_id == received_id:
        difference = sent_amount - received_amount
        return [
            PostingInput(in_transit, sent_id, -difference, is_fee=True),
            PostingInput(unassigned, sent_id, difference),
        ]
    _, fee = _conversion_fee(sent, received, fees)
    converted, bought = sent_amount, received_amount
    if fee is not None:
        if fee.instrument_id == received_id:
            bought += fee.amount
        else:
            converted -= fee.amount
    postings = [
        PostingInput(
            in_transit,
            sent_id,
            -converted,
            price_per_unit=bought / converted,
            price_instrument_id=received_id,
        ),
        PostingInput(in_transit, received_id, bought),
    ]
    if fee is not None:
        postings += [
            PostingInput(in_transit, fee.instrument_id, -fee.amount, is_fee=True),
            PostingInput(unassigned, fee.instrument_id, fee.amount),
        ]
    return postings


def settles(r: PairSide, c: PairSide) -> bool:
    """Whether a transfer of the two records needs a settlement (conversion_postings): they
    differ in instrument or amount."""
    return r.instrument_id != c.instrument_id or r.quantity != -c.quantity


def owner_pair_refusal(r: PairSide, c: PairSide) -> str | None:
    """Why the owner can't pair the two records as one transfer, or None when they can
    (eToro design §5.12): on different accounts, and either in one instrument of opposite,
    equal amounts, or both in currencies, of opposite sign, differing in currency or in
    amount with a settlement that fits (the receiving side not larger, a leg's fee no more
    than the difference or a conversion's)."""
    if r.account_id == c.account_id:
        return "the two records are on the same account"
    if not settles(r, c):
        return None
    if (r.quantity < 0) == (c.quantity < 0):
        return "the two records are in the same direction"
    if not (r.fiat and c.fiat):
        if r.instrument_id != c.instrument_id:
            return "the two records are in different instruments, not both currencies"
        return "the two records are not of opposite and equal amounts, and not in a currency"
    sent, received = (r, c) if r.quantity < 0 else (c, r)
    sent_side = (sent.instrument_id, -sent.quantity)
    received_side = (received.instrument_id, received.quantity)
    if sent.instrument_id == received.instrument_id and received.quantity > -sent.quantity:
        return "the receiving side is larger than the sending side"
    if not settlement_fits(sent_side, received_side, leg_fees(r, c)):
        return "the fee its connector gives does not fit the two records' amounts"
    return None


@dataclass(frozen=True)
class Known:
    """What the ledger knows of manual accounts' own rows (design §5.8, "An unrelated row
    can stand in"): per account and instrument, the first day committed imports list in
    full (M2's complete_from). A connected account's history is known from its
    connection, so it isn't in `manual`."""

    manual: frozenset[UUID]
    first_full_day: Mapping[tuple[UUID, UUID], date]

    def holds_from(self, record: Pairable, earliest: date) -> bool:
        """Whether the ledger would hold the record's account's own row on a day from
        earliest on, if there were one: always for a connected account; for a manual one,
        from the first day its imports list in full. Before it the row may be inside the
        account's opening, never imported, and an unrelated row taken for it would never
        be found out. After the last day imported, one would be: the next statement brings
        the real row, and the two go to review."""
        if record.account_id not in self.manual:
            return True
        first = self.first_full_day.get((record.account_id, record.instrument_id))
        return first is not None and first <= earliest


# The first day an import not yet committed would list in full: (account, instrument, day).
type Listing = tuple[UUID, UUID, date]


async def _load_known(session: AsyncSession, listing: Collection[Listing]) -> Known:
    a, b, r = models.Account, models.ImportBatch, models.ImportRow
    manual = frozenset(
        # Own records are never on a system account, so these needn't be left out.
        await session.scalars(select(a.id).where(a.source_connection_id.is_(None)))
    )
    first_full_day: dict[tuple[UUID, UUID], date] = {}
    # A batch speaks only for the instruments it has live rows in (M2's Decision 16). One
    # that lists in full from its account's opening speaks for all time before it too:
    # nothing exists before an account opens.
    s = models.ImportStatement
    from_opening = func.bool_or(b.complete_from == s.account_opened_on)
    for account_id, instrument_id, first, opened in await session.execute(
        select(b.account_id, r.instrument_id, func.min(b.complete_from), from_opening)
        .join(r, r.batch_id == b.id)
        .outerjoin(s, s.batch_id == b.id)
        .where(
            b.status == "committed",
            b.complete_from.is_not(None),
            r.kind == "statement",
            r.status.in_(("committed", "duplicate")),
        )
        .group_by(b.account_id, r.instrument_id)
    ):
        assert instrument_id is not None and first is not None
        first_full_day[(account_id, instrument_id)] = date.min if opened else first
    for account_id, instrument_id, first in listing:
        key = (account_id, instrument_id)
        first_full_day[key] = min(first, first_full_day.get(key, first))
    return Known(manual, first_full_day)


@dataclass(frozen=True)
class _Decisions:
    rejected: frozenset[SourceRef]
    never: frozenset[tuple[SourceRef, SourceRef]]  # as stored, either way round
    paired: tuple[tuple[SourceRef, SourceRef], ...]

    def apart(self, r: Pairable, c: Pairable) -> bool:
        """Whether a decision keeps the two apart: either is rejected, or they are never
        to pair."""
        return (
            r.source in self.rejected
            or c.source in self.rejected
            or (r.source, c.source) in self.never
            or (c.source, r.source) in self.never
        )


async def _load_decisions(session: AsyncSession) -> _Decisions:
    d = models.TransferDecision
    rejected: set[SourceRef] = set()
    never: set[tuple[SourceRef, SourceRef]] = set()
    paired: list[tuple[SourceRef, SourceRef]] = []
    for row in await session.execute(
        select(d.kind, d.subject_kind, d.subject_ref, d.other_kind, d.other_ref)
    ):
        subject: SourceRef = (row.subject_kind, row.subject_ref)
        if row.kind == "reject":
            rejected.add(subject)
        elif row.kind == "never_pair":
            never.add((subject, (row.other_kind, row.other_ref)))
        else:
            paired.append((subject, (row.other_kind, row.other_ref)))
    return _Decisions(frozenset(rejected), frozenset(never), tuple(paired))


async def _load_records(
    session: AsyncSession,
    system: Mapping[SystemAccountKey, UUID],
    decisions: _Decisions,
    excluding: Collection[UUID],
) -> list[Pairable]:
    """The own records that can pair at all: those a bank transfer, naming a bank
    account, a card merchant or an own account by its number (the registry's), those on an
    account a card merchant pays into (card_tier's other side) or funded by bank transfer
    (registry_tier's, _may_be_transfer), and those the owner paired."""
    e = eligible_records(system)
    t = models.Transaction
    h = models.AccountIdentifierHolder
    decided = {source for pair in decisions.paired for source in pair}
    decided_ids = (
        select(t.id).where(or_(*(names_source(t, source) for source in decided)))
        if decided
        else None
    )
    fee_instrument_id, fee_amount = leg_fee_columns(e.c.transaction_id)
    i = aliased(models.Instrument, name="record_instrument")
    fiat = select(i.kind == "fiat").where(i.id == e.c.instrument_id).scalar_subquery()
    can_pair = [
        e.c.bank_transfer,
        e.c.counterparty_identifier_id.is_not(None),
        e.c.holder.is_not(None),
        e.c.account_id.in_(select(h.account_id)),
        e.c.funded_by_transfer,
    ]
    if decided_ids is not None:
        can_pair.append(e.c.transaction_id.in_(decided_ids))
    query = select(
        e.c.transaction_id,
        e.c.source_kind,
        e.c.source_ref,
        e.c.account_id,
        e.c.instrument_id,
        e.c.quantity,
        e.c.occurred_at,
        e.c.booking_date,
        e.c.holder,
        e.c.bank_transfer,
        e.c.weak_holder,
        e.c.names_account,
        e.c.holders,
        e.c.funded_by_transfer,
        fiat.label("fiat"),
        fee_instrument_id.label("fee_instrument_id"),
        fee_amount.label("fee_amount"),
    ).where(or_(*can_pair))
    if excluding:
        query = query.where(e.c.transaction_id.not_in(list(excluding)))
    return [
        Pairable(
            id=row.transaction_id,
            source=(row.source_kind, row.source_ref),
            account_id=row.account_id,
            instrument_id=row.instrument_id,
            quantity=row.quantity,
            occurred_at=row.occurred_at,
            booking_date=row.booking_date,
            holder=row.holder,
            bank_transfer=row.bank_transfer,
            weak_holder=row.weak_holder,
            names_account=row.names_account,
            holders=frozenset(row.holders or ()),
            funded_by_transfer=row.funded_by_transfer,
            fiat=bool(row.fiat),
            fee=(
                None
                if row.fee_instrument_id is None
                else LegFee(row.fee_instrument_id, row.fee_amount)
            ),
        )
        for row in await session.execute(query)
    ]


@dataclass
class _Edges:
    """Pairs of records, both ways round."""

    tiers: dict[UUID, dict[UUID, Tier]] = field(default_factory=lambda: defaultdict(dict))

    def add(self, r: UUID, c: UUID, tier: Tier) -> None:
        for a, b in ((r, c), (c, r)):
            known = self.tiers[a].get(b)
            if known is None or tier < known:
                self.tiers[a][b] = tier


class _Timeline:
    """Records sorted by booking date, to find those inside a window."""

    def __init__(self, records: Iterable[Pairable]) -> None:
        self._records = sorted(records, key=lambda x: (x.booking_date, x.occurred_at, x.id))
        self._dates = [x.booking_date for x in self._records]

    def between(self, earliest: date, latest: date) -> list[Pairable]:
        """Those booked from earliest to latest, both included."""
        return self._records[bisect_left(self._dates, earliest) : bisect_right(self._dates, latest)]


def _one_sided_known(s: Pairable, c: Pairable, settings: TransferSettings, known: Known) -> bool:
    """Design §5.8: an identified record's counterpart found by amount and date (one naming
    nobody) is taken only if the identified record's window doesn't start before the days
    the counterpart's account's imports list (Known.holds_from): there the real row may be
    inside the opening, and an unrelated one stand in for it for good."""
    identified, other = (s, c) if s.identified else (c, s)
    return known.holds_from(other, counterpart_bounds(identified, settings)[0])


def _weak_known(s: Pairable, c: Pairable, settings: TransferSettings, known: Known) -> bool:
    """The same gate for a weak pair (weak_tier), from the record whose sort code names the
    other's account: its counterpart, found by amount and date, is taken only where the
    ledger would hold the real one."""
    w, other = (s, c) if _names_weakly(s, c) else (c, s)
    return known.holds_from(other, counterpart_bounds(w, settings)[0])


def _registry_edges(
    records: list[Pairable], settings: TransferSettings, decisions: _Decisions, known: Known
) -> tuple[_Edges, _Edges]:
    """(same amount, another amount) pairs the registry gives. Of the same amount, both
    tiers, a one-sided one only where the ledger would hold the real counterpart
    (_one_sided_known), and, where the registry gives none, a weak one under the same gate
    (_weak_known); of another amount, only mutually identified ones (Ruling 8: a payment
    naming nobody is no evidence of a differing transfer)."""
    same, differing = _Edges(), _Edges()
    # Same amount: a sender meets the receivers in its instrument of its amount.
    receivers: dict[tuple[UUID, Decimal], list[Pairable]] = defaultdict(list)
    for x in records:
        if not x.sends:
            receivers[(x.instrument_id, x.quantity)].append(x)
    by_amount = {key: _Timeline(found) for key, found in receivers.items()}
    # Another amount: a sender naming account H meets the receivers on H naming its account.
    named: dict[tuple[UUID, UUID, UUID], list[Pairable]] = defaultdict(list)
    for x in records:
        if not x.sends and x.identified and x.holder is not None:
            named[(x.instrument_id, x.holder, x.account_id)].append(x)
    by_accounts = {key: _Timeline(found) for key, found in named.items()}
    for s in records:
        if not s.sends:
            continue
        window = counterpart_bounds(s, settings)
        timeline = by_amount.get((s.instrument_id, -s.quantity))
        for c in timeline.between(*window) if timeline else ():
            tier = registry_tier(s, c)
            if tier is None:
                tier = weak_tier(s, c)
            if tier is None or decisions.apart(s, c):
                continue
            if tier == ONE_SIDED and not _one_sided_known(s, c, settings, known):
                continue
            if tier == WEAK and not _weak_known(s, c, settings, known):
                continue
            same.add(s.id, c.id, tier)
        if not s.identified or s.holder is None:
            continue
        timeline = by_accounts.get((s.instrument_id, s.account_id, s.holder))
        for c in timeline.between(*window) if timeline else ():
            if c.quantity != -s.quantity and not decisions.apart(s, c):
                differing.add(s.id, c.id, MUTUAL)
    return same, differing


def _decided_edges(records: list[Pairable], decisions: _Decisions, edges: _Edges) -> None:
    """The pairs the owner made (Decision 6 step 2), added to `edges`: still pairs the
    owner could make (owner_pair_refusal: in one instrument of opposite, equal amounts, or
    two currencies differing in currency or amount whose settlement fits); the window does
    not apply. They outrank every other tier, those across currencies and of cards
    included."""
    by_source = {x.source: x for x in records}
    for subject, other in decisions.paired:
        r, c = by_source.get(subject), by_source.get(other)
        if r is None or c is None:
            continue
        if owner_pair_refusal(r, c) is None and not decisions.apart(r, c):
            edges.add(r.id, c.id, DECIDED)


def _cross_currency_edges(
    records: list[Pairable], settings: TransferSettings, decisions: _Decisions
) -> _Edges:
    """The pairs cross_currency_tier gives whose occurred_at are at most the near window
    apart (settings.near_window), and which no decision keeps apart. A sender naming
    account H meets the receivers on H naming its account, sorted by time."""
    edges = _Edges()
    named: dict[tuple[UUID, UUID], list[Pairable]] = defaultdict(list)
    for x in records:
        if not x.sends and x.fiat and x.identified and x.holder is not None:
            named[(x.account_id, x.holder)].append(x)
    by_time = {
        key: sorted(found, key=lambda x: (x.occurred_at, x.id)) for key, found in named.items()
    }
    moments = {key: [x.occurred_at for x in found] for key, found in by_time.items()}
    for s in records:
        if not s.sends or not s.fiat or not s.identified or s.holder is None:
            continue
        key = (s.holder, s.account_id)
        found, times = by_time.get(key), moments.get(key)
        if found is None or times is None:
            continue
        earliest, latest = (
            s.occurred_at - settings.near_window,
            s.occurred_at + settings.near_window,
        )
        for c in found[bisect_left(times, earliest) : bisect_right(times, latest)]:
            if cross_currency_tier(s, c) is not None and not decisions.apart(s, c):
                edges.add(s.id, c.id, MUTUAL)
    return edges


@dataclass(frozen=True)
class PairRelation:
    """The pair relation over the ledger's own records."""

    records: Mapping[UUID, Pairable]  # those that can pair at all
    rejected: frozenset[UUID]  # those of them the owner said are not a transfer
    # Every pair of the same amount, and every pair the owner made (DECIDED, whose sides
    # may differ: _decided_edges), both ways round, with its strongest tier.
    tiers: Mapping[UUID, Mapping[UUID, Tier]]
    # A record's candidates of the same amount, or the owner's: those in the strongest tier
    # it has any in, for which that is also their own strongest (Ruling 7).
    exact: Mapping[UUID, frozenset[UUID]]
    # A record's candidates of another amount: mutually identified ones.
    differing: Mapping[UUID, frozenset[UUID]]
    # Partners within groups of interchangeable candidates (_twins).
    twins: Mapping[UUID, UUID]
    # A record's candidates in another currency (_cross_currency_edges).
    across: Mapping[UUID, frozenset[UUID]]
    # The records whose only candidate is one in another currency, and whose only
    # candidate they are, with no candidate of the same amount either: matched with it when
    # the conversion's fee fits (`converts`, both ways round), else of another amount.
    alone_across: frozenset[UUID]
    converts: Mapping[UUID, UUID]
    # A record's candidates as a card payment or the deposit it paid (card_partners), and
    # the pairs, both ways round, of those each the other's only one, with no candidate in
    # another tier either.
    cards: Mapping[UUID, frozenset[UUID]]
    by_card: Mapping[UUID, UUID]

    def candidates(self, record_id: UUID, *, exact: bool = True) -> list[UUID]:
        """The record's candidates, of the same amount or (exact False) of another."""
        found = (self.exact if exact else self.differing).get(record_id, frozenset())
        return sorted(found)

    def partner(self, record_id: UUID) -> UUID | None:
        """The record's unique partner: its only candidate, whose only candidate it is; or,
        among interchangeable candidates, the one a fixed order gives it (_twins). With no
        candidate of the same amount, the one it converts with across currencies
        (`converts`), else the card payment or deposit it pairs with (`by_card`)."""
        found = self.exact.get(record_id, frozenset())
        if not found:
            across = self.converts.get(record_id)
            return across if across is not None else self.by_card.get(record_id)
        if len(found) != 1:
            return self.twins.get(record_id)
        [partner] = found
        return partner if len(self.exact.get(partner, frozenset())) == 1 else None

    def several(self, record_id: UUID) -> bool:
        """Whether the record has candidates the rule can't choose between: any of the same
        amount, or in another currency or as a card payment's other side when it and one of
        them aren't each other's only one (several_candidates). Only a record with a
        transfer carries the reason: an unpaired card payment and its deposits have none
        (no awaiting transfer is opened for either), so for them it is the relation's word
        alone, and they stay on Unassigned for the owner to pair."""
        if self.exact.get(record_id):
            return True
        if self.across.get(record_id) and record_id not in self.alone_across:
            return True
        return bool(self.cards.get(record_id)) and record_id not in self.by_card


def _twins(
    records: Mapping[UUID, Pairable], exact: Mapping[UUID, frozenset[UUID]]
) -> dict[UUID, UUID]:
    """Partners for groups of interchangeable records: n records whose candidates are
    exactly the same n others, and the reverse, each side alike (account, instrument,
    amount, booking date: a transfer split into equal parts, say). Any pairing of them
    gives the same ledger, so they pair in a fixed order, by time and then by reference,
    which doesn't depend on which side arrived first. Anything less alike stays for the
    owner's review. A side naming no account is found by amount and date, as a single
    counterpart is: an unrelated row can be among it until the real one arrives, and the
    group then no longer matches (design §5.8)."""

    def alike(group: frozenset[UUID]) -> bool:
        return (
            len(
                {
                    (r.account_id, r.instrument_id, r.quantity, r.booking_date)
                    for r in (records[x] for x in group)
                }
            )
            == 1
        )

    def order(group: frozenset[UUID]) -> list[UUID]:
        return sorted(group, key=lambda x: (records[x].occurred_at, records[x].source))

    twins: dict[UUID, UUID] = {}
    checked: set[UUID] = set()
    for record_id, found in exact.items():
        if len(found) < 2 or record_id in checked:
            continue
        others = exact.get(next(iter(found)), frozenset())
        checked |= found | others  # a group is checked once, whichever member comes first
        if (
            len(others) == len(found)
            and all(exact.get(x) == others for x in found)
            and all(exact.get(x) == found for x in others)
            and alike(found)
            and alike(others)
        ):
            for x, y in zip(order(others), order(found), strict=True):
                twins[x], twins[y] = y, x
    return twins


async def pair_relation(
    session: AsyncSession,
    system: Mapping[SystemAccountKey, UUID],
    settings: TransferSettings,
    *,
    excluding: Collection[UUID] = (),
    adding: Collection[Pairable] = (),
    listing: Collection[Listing] = (),
) -> PairRelation:
    """The pair relation as the ledger stands: the single definition matching, heal and
    review all use.

    The registry pairs two records when they are on different accounts, in the same
    instrument, of opposite sign; the receiving one is booked in the sending one's window; at
    least one is identified through the registry; each identified one names the other's
    account; each other one is a bank transfer, or names nobody on an account funded by bank
    transfer (_may_be_transfer); and neither is rejected nor decided never to pair with the
    other. Where the registry doesn't pair two records, a sort code alone may, as the
    weakest tier (weak_tier). Two records in different currencies pair when
    cross_currency_tier takes them, within the near window, each the other's only such
    candidate and neither with one of the same amount; their conversion's fee must fit
    (conversion_postings), else they are for review as of another amount. A card payment
    and a deposit on one of its merchant's holder accounts pair when card_tier takes them,
    each the other's only such candidate and neither with a candidate in another tier
    (card_partners). The owner pairs two records with a pair decision.

    `excluding` leaves records out as if they were gone (a row about to be deleted);
    `adding` puts records in as if they were posted (a row being staged), decisions about
    their references included; `listing`, the days their import would list in full."""
    decisions = await _load_decisions(session)
    records = await _load_records(session, system, decisions, excluding) + list(adding)
    same, differing = _registry_edges(
        records, settings, decisions, await _load_known(session, listing)
    )
    _decided_edges(records, decisions, same)
    best = {r: min(found.values()) for r, found in same.tiers.items()}
    exact = {
        r: frozenset(c for c, tier in found.items() if tier == best[r] == best[c])
        for r, found in same.tiers.items()
    }
    exact = {r: found for r, found in exact.items() if found}
    by_id = {x.id: x for x in records}
    across = {
        r: frozenset(found)
        for r, found in _cross_currency_edges(records, settings, decisions).tiers.items()
    }
    alone: set[UUID] = set()
    converts: dict[UUID, UUID] = {}
    # A neighbour already paired in its own currency (in `exact`) still counts as a
    # candidate across: the record then has several, and is left for review rather than
    # paired around the neighbour, which is the safe side.
    for r, found in across.items():
        if len(found) != 1 or r in exact:
            continue
        [c] = found
        if across.get(c) != frozenset({r}) or c in exact:
            continue
        alone.add(r)
        s, other = (by_id[r], by_id[c]) if by_id[r].sends else (by_id[c], by_id[r])
        fits, _ = _conversion_fee(
            (s.instrument_id, -s.quantity),
            (other.instrument_id, other.quantity),
            leg_fees(s, other),
        )
        if not fits:
            differing.add(r, c, MUTUAL)  # for review, as a counterpart of another amount
        else:
            converts[r] = c
    # As across: a record any other tier gives a candidate never pairs as a card's, and a
    # card candidate that has one leaves the record for the owner.
    cards, by_card = card_partners(
        records, settings.near_window, decisions.apart, exact.keys() | across.keys()
    )
    return PairRelation(
        records={x.id: x for x in records},
        rejected=frozenset(x.id for x in records if x.source in decisions.rejected),
        tiers={r: dict(found) for r, found in same.tiers.items()},
        exact=exact,
        differing={r: frozenset(found) for r, found in differing.tiers.items()},
        twins=_twins(by_id, exact),
        across=across,
        alone_across=frozenset(alone),
        converts=converts,
        cards=cards,
        by_card=by_card,
    )
