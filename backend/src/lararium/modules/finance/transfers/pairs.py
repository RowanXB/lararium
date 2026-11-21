"""The pair relation (design §5.7): which own records could be one transfer's two sides.

It is computed once, set-wise, from the own records that can pair at all (a bank transfer,
or one naming a bank account, or one the owner paired) and the owner's decisions, and then
read by matching, heal and review alike. It depends only on the records and the decisions,
never on which transfers exist, and nothing matching writes changes it (re-pointing a
contra, opening or dissolving a transfer, posting a placeholder), so one computation holds
for a whole heal pass."""

from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.modules.finance import models
from lararium.modules.finance.system_accounts import SystemAccountKey
from lararium.modules.finance.transfers.contra import SourceRef, eligible_records, names_source
from lararium.modules.finance.transfers.settings import TransferSettings

# How strong the evidence is that two records are one transfer, strongest first: the
# owner paired them; each names the other's account; only one names the other's account
# (the other is a bank transfer naming nobody).
type Tier = Literal[1, 2, 3]
DECIDED: Tier = 1
MUTUAL: Tier = 2
ONE_SIDED: Tier = 3


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


def registry_tier(r: Pairable, c: Pairable) -> Tier | None:
    """The tier the registry gives two records of opposite sign, in the same instrument,
    the receiving one in the sending one's window, or None when it doesn't pair them: they
    must be on different accounts, at least one identified, each identified one naming the
    other's account and each other one a bank transfer. Decisions are not considered."""
    if c.account_id == r.account_id or not (r.identified or c.identified):
        return None
    for x, other in ((r, c), (c, r)):
        if x.identified and x.holder != other.account_id:
            return None
        if not x.identified and not x.bank_transfer:
            return None
    return MUTUAL if r.identified and c.identified else ONE_SIDED


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
    # A batch speaks only for the instruments it has live rows in (M2's Decision 16).
    for account_id, instrument_id, first in await session.execute(
        select(b.account_id, r.instrument_id, func.min(b.complete_from))
        .join(r, r.batch_id == b.id)
        .where(
            b.status == "committed",
            b.complete_from.is_not(None),
            r.kind == "statement",
            r.status.in_(("committed", "duplicate")),
        )
        .group_by(b.account_id, r.instrument_id)
    ):
        assert instrument_id is not None and first is not None
        first_full_day[(account_id, instrument_id)] = first
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
    account or naming an own account by its number (the registry's), and those the owner
    paired. A card payment is none of these."""
    e = eligible_records(system)
    t = models.Transaction
    decided = {source for pair in decisions.paired for source in pair}
    decided_ids = (
        select(t.id).where(or_(*(names_source(t, source) for source in decided)))
        if decided
        else None
    )
    can_pair = [
        e.c.bank_transfer,
        e.c.counterparty_identifier_id.is_not(None),
        e.c.holder.is_not(None),
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


def _registry_edges(
    records: list[Pairable], settings: TransferSettings, decisions: _Decisions, known: Known
) -> tuple[_Edges, _Edges]:
    """(same amount, another amount) pairs the registry gives. Of the same amount, both
    tiers, a one-sided one only where the ledger would hold the real counterpart
    (_one_sided_known); of another amount, only mutually identified ones (Ruling 8: a
    payment naming nobody is no evidence of a differing transfer)."""
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
            if tier is None or decisions.apart(s, c):
                continue
            if tier == ONE_SIDED and not _one_sided_known(s, c, settings, known):
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
    """The pairs the owner made (Decision 6 step 2), added to `edges`: still on different
    accounts, in the same instrument and of opposite sign and the same amount; the window
    does not apply."""
    by_source = {x.source: x for x in records}
    for subject, other in decisions.paired:
        r, c = by_source.get(subject), by_source.get(other)
        if r is None or c is None:
            continue
        if (
            r.account_id != c.account_id
            and r.instrument_id == c.instrument_id
            and c.quantity == -r.quantity
            and not decisions.apart(r, c)
        ):
            edges.add(r.id, c.id, DECIDED)


@dataclass(frozen=True)
class PairRelation:
    """The pair relation over the ledger's own records."""

    records: Mapping[UUID, Pairable]  # those that can pair at all
    rejected: frozenset[UUID]  # those of them the owner said are not a transfer
    # Every pair of the same amount, both ways round, with its strongest tier.
    tiers: Mapping[UUID, Mapping[UUID, Tier]]
    # A record's candidates of the same amount: those in the strongest tier it has any in,
    # for which that is also their own strongest (Ruling 7).
    exact: Mapping[UUID, frozenset[UUID]]
    # A record's candidates of another amount: mutually identified ones.
    differing: Mapping[UUID, frozenset[UUID]]
    # Partners within groups of interchangeable candidates (_twins).
    twins: Mapping[UUID, UUID]

    def candidates(self, record_id: UUID, *, exact: bool = True) -> list[UUID]:
        """The record's candidates, of the same amount or (exact False) of another."""
        found = (self.exact if exact else self.differing).get(record_id, frozenset())
        return sorted(found)

    def partner(self, record_id: UUID) -> UUID | None:
        """The record's unique partner: its only candidate, whose only candidate it is; or,
        among interchangeable candidates, the one a fixed order gives it (_twins)."""
        found = self.exact.get(record_id, frozenset())
        if len(found) != 1:
            return self.twins.get(record_id)
        [partner] = found
        return partner if len(self.exact.get(partner, frozenset())) == 1 else None


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
    account; each other one is a bank transfer; and neither is rejected nor decided never
    to pair with the other. The owner pairs two records with a pair decision.

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
    return PairRelation(
        records={x.id: x for x in records},
        rejected=frozenset(x.id for x in records if x.source in decisions.rejected),
        tiers={r: dict(found) for r, found in same.tiers.items()},
        exact={r: found for r, found in exact.items() if found},
        differing={r: frozenset(found) for r, found in differing.tiers.items()},
        twins=_twins({x.id: x for x in records}, exact),
    )
