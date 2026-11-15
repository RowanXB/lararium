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
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import or_, select
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
    holder: UUID | None  # the account its counterparty bank account is registered to
    bank_transfer: bool

    @property
    def sends(self) -> bool:
        return self.quantity < 0

    @property
    def identified(self) -> bool:
        """Its counterparty bank account is registered to another own account."""
        return self.holder is not None and self.holder != self.account_id


def counterpart_bounds(record: Pairable, settings: TransferSettings) -> tuple[datetime, datetime]:
    """When the record's counterpart may occur (Decision 4; queries.counterpart_window in
    SQL): the receiving side from `before` before to `after` after the sending side."""
    if record.sends:
        return (
            record.occurred_at - settings.window_before,
            record.occurred_at + settings.window_after,
        )
    return record.occurred_at - settings.window_after, record.occurred_at + settings.window_before


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
    """The own records that can pair at all: those a bank transfer or naming a bank
    account (the registry's), and those the owner paired. A card payment is neither."""
    e = eligible_records(system)
    t = models.Transaction
    decided = {source for pair in decisions.paired for source in pair}
    decided_ids = (
        select(t.id).where(or_(*(names_source(t, source) for source in decided)))
        if decided
        else None
    )
    can_pair = [e.c.bank_transfer, e.c.counterparty_identifier_id.is_not(None)]
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
    """Records sorted by when they occurred, to find those inside a window."""

    def __init__(self, records: Iterable[Pairable]) -> None:
        self._records = sorted(records, key=lambda x: (x.occurred_at, x.id))
        self._times = [x.occurred_at for x in self._records]

    def between(self, earliest: datetime, latest: datetime) -> list[Pairable]:
        """Those occurring from earliest to latest, both included."""
        return self._records[bisect_left(self._times, earliest) : bisect_right(self._times, latest)]


def _registry_edges(
    records: list[Pairable], settings: TransferSettings, decisions: _Decisions
) -> tuple[_Edges, _Edges]:
    """(same amount, another amount) pairs the registry gives. Of the same amount, both
    tiers; of another amount, only mutually identified ones (Ruling 8: a payment naming
    nobody is no evidence of a differing transfer)."""
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
            if tier is not None and not decisions.apart(s, c):
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

    def candidates(self, record_id: UUID, *, exact: bool = True) -> list[UUID]:
        """The record's candidates, of the same amount or (exact False) of another."""
        found = (self.exact if exact else self.differing).get(record_id, frozenset())
        return sorted(found)

    def partner(self, record_id: UUID) -> UUID | None:
        """The record's unique partner: its only candidate, whose only candidate it is."""
        found = self.exact.get(record_id, frozenset())
        if len(found) != 1:
            return None
        [partner] = found
        return partner if len(self.exact.get(partner, frozenset())) == 1 else None


async def pair_relation(
    session: AsyncSession,
    system: Mapping[SystemAccountKey, UUID],
    settings: TransferSettings,
    *,
    excluding: Collection[UUID] = (),
    adding: Collection[Pairable] = (),
) -> PairRelation:
    """The pair relation as the ledger stands: the single definition matching, heal and
    review all use.

    The registry pairs two records when they are on different accounts, in the same
    instrument, of opposite sign; the receiving one occurs in the sending one's window; at
    least one is identified through the registry; each identified one names the other's
    account; each other one is a bank transfer; and neither is rejected nor decided never
    to pair with the other. The owner pairs two records with a pair decision.

    `excluding` leaves records out as if they were gone (a row about to be deleted);
    `adding` puts records in as if they were posted (a row being staged), decisions about
    their references included."""
    decisions = await _load_decisions(session)
    records = await _load_records(session, system, decisions, excluding) + list(adding)
    same, differing = _registry_edges(records, settings, decisions)
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
    )
