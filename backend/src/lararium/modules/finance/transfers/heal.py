"""Which records heal (engine.heal_transfers) has to match again: those whose ledger state
disagrees with the rule."""

from uuid import UUID

from sqlalchemy import and_, exists, func, or_, select, union
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from lararium.modules.finance import models
from lararium.modules.finance.system_accounts import system_account_ids
from lararium.modules.finance.transfers.contra import (
    record_columns,
    rejoins,
    snapshot_own_records,
)
from lararium.modules.finance.transfers.pairs import PairRelation
from lararium.modules.finance.transfers.queries import (
    OPEN_STATUSES,
    ReviewReason,
    before_history,
    history_starts,
    stray_in_transit,
)
from lararium.modules.finance.transfers.settings import TransferSettings


async def _pair_states(session: AsyncSession, relation: PairRelation) -> set[UUID]:
    """Records whose transfer disagrees with the pair relation, which matching decides by:
    a unique pair (each the other's only candidate) not yet matched; a matched pair that
    is no longer unique; and an open transfer whose review reason is not the one its own
    record's candidates give (several_candidates if it has any of the same amount, else
    amount_differs if it has any of another, else none)."""
    tr = models.Transfer
    due: set[UUID] = set()
    matched: set[tuple[UUID, UUID]] = set()
    rows = await session.execute(
        select(tr.status, tr.out_transaction_id, tr.in_transaction_id, tr.review_reason).where(
            tr.status.in_(("matched", *OPEN_STATUSES))
        )
    )
    for row in rows:
        if row.status == "matched":
            matched.add((row.out_transaction_id, row.in_transaction_id))
            # (e) a matched transfer whose two records are no longer a unique pair.
            if relation.partner(row.out_transaction_id) != row.in_transaction_id:
                due.add(row.out_transaction_id)
            continue
        present = row.out_transaction_id or row.in_transaction_id
        if present is None or (row.out_transaction_id and row.in_transaction_id):
            continue
        expected: ReviewReason | None = None
        if relation.candidates(present):
            expected = "several_candidates"
        elif relation.candidates(present, exact=False):
            expected = "amount_differs"
        # (e) an open transfer whose review reason disagrees with its record's candidates.
        if row.review_reason != expected:
            due.add(present)
    # (c) a unique pair not yet a matched transfer.
    for record_id in relation.exact:
        partner = relation.partner(record_id)
        if (
            partner is not None
            and (record_id, partner) not in matched
            and (partner, record_id) not in matched
        ):
            due.add(record_id)
    return due


async def records_to_heal(
    session: AsyncSession, settings: TransferSettings, relation: PairRelation
) -> list[UUID]:
    """Records whose ledger state disagrees with the rule (Decision 8) and `relation` (the
    pair relation as the ledger stands), in occurrence order: matching each of them
    (engine.match_transaction) brings it in line."""
    system = await system_account_ids(session)
    in_transit, unassigned = system.get("in_transit"), system.get("unassigned")
    if unassigned is None:
        return []  # no connector has posted anything yet
    # The own records once, with statistics, for the arms below to join: re-derived in each
    # arm from the ledger's tables, the plan rests on their statistics, which may be stale.
    own = await snapshot_own_records(session, system)
    tr, d, t = models.Transfer, models.TransferDecision, models.Transaction
    active = tr.status != "rejected"
    identified = and_(own.c.holder.is_not(None), own.c.holder != own.c.account_id)
    rejected = exists().where(
        d.kind == "reject", d.subject_kind == own.c.source_kind, d.subject_ref == own.c.source_ref
    )
    queries = [
        # (b) identified, still on Unassigned, not rejected.
        select(own.c.transaction_id.label("id"), own.c.occurred_at).where(
            own.c.contra_account_id == unassigned, identified, ~rejected
        ),
        # (f) rejected, but not yet back in the rejection its decision names (a row rolled
        # back and imported again).
        select(own.c.transaction_id.label("id"), own.c.occurred_at)
        .select_from(own)
        .join(
            d,
            and_(
                d.kind == "reject",
                d.subject_kind == own.c.source_kind,
                d.subject_ref == own.c.source_ref,
            ),
        )
        .join(tr, tr.id == d.transfer_id)
        .where(rejoins(own.c.transaction_id, own.c.quantity, own.c.account_id)),
    ]
    if in_transit is not None:
        # (a) posting on In transit without being a transfer's leg, whatever its shape (a
        # leg that gained a fee posting included).
        queries.append(stray_in_transit(in_transit))
        history = history_starts(system.get("opening_balances"))
        for side in ("out", "in"):
            leg, other_leg = (
                (tr.out_transaction_id, tr.in_transaction_id)
                if side == "out"
                else (tr.in_transaction_id, tr.out_transaction_id)
            )
            own_account, counterpart = (
                (tr.from_account_id, tr.to_account_id)
                if side == "out"
                else (tr.to_account_id, tr.from_account_id)
            )
            quantity = -tr.amount if side == "out" else tr.amount
            # (d) legs that no longer fit their transfer. A leg that is no own record any
            # more (void, say) has no snapshot row: its time is looked up by id.
            occurred_at = select(t.occurred_at).where(t.id == leg).scalar_subquery()
            queries.append(
                select(leg.label("id"), occurred_at.label("occurred_at"))
                .select_from(tr)
                .outerjoin(own, own.c.transaction_id == leg)
                .where(
                    active,
                    leg.is_not(None),
                    or_(
                        own.c.transaction_id.is_(None),  # void, or no longer shaped like a leg
                        own.c.account_id != own_account,
                        own.c.instrument_id != tr.instrument_id,
                        own.c.quantity != quantity,
                        own.c.contra_account_id != in_transit,
                        # A transfer with one own record stands on that record naming the
                        # other account; it no longer does once the identifier moves.
                        and_(other_leg.is_(None), own.c.holder.is_distinct_from(counterpart)),
                    ),
                )
            )
            # (d) a lone record whose counterpart's account history now starts after the
            # counterpart's latest time, or no longer does (older history was fetched).
            predates = before_history(record_columns(own), history.c.starts_at, settings)
            queries.append(
                select(own.c.transaction_id.label("id"), own.c.occurred_at)
                .select_from(tr)
                .join(own, own.c.transaction_id == leg)
                .outerjoin(history, history.c.account_id == counterpart)
                .where(
                    other_leg.is_(None),
                    or_(
                        and_(tr.status == "before_history", ~predates),
                        and_(tr.status == "awaiting", predates),
                    ),
                )
            )
        # (d) placeholders and settlements out of step with their record: other dates,
        # void, or missing. A record that is no own record any more is due as a leg above.
        derived = aliased(t, name="derived")
        derived_id = func.coalesce(tr.placeholder_transaction_id, tr.settlement_transaction_id)
        in_step = exists().where(
            derived.id == derived_id,
            derived.status != "void",
            derived.occurred_at == own.c.occurred_at,
            derived.booking_date == own.c.booking_date,
        )
        queries.append(
            select(own.c.transaction_id.label("id"), own.c.occurred_at)
            .select_from(tr)
            .join(
                own,
                own.c.transaction_id == func.coalesce(tr.out_transaction_id, tr.in_transaction_id),
            )
            .where(
                active,
                or_(
                    and_(tr.status == "inferred", tr.placeholder_transaction_id.is_(None)),
                    and_(tr.status == "before_history", tr.settlement_transaction_id.is_(None)),
                    # Under OR, so it stays a lookup per transfer rather than a join.
                    and_(derived_id.is_not(None), ~in_step),
                ),
            )
        )
    # (c) and (e): pairs, and review reasons, that disagree with the pair relation.
    paired = await _pair_states(session, relation)
    if paired:
        queries.append(select(t.id.label("id"), t.occurred_at).where(t.id.in_(paired)))
    due = union(*queries).subquery()
    return list(await session.scalars(select(due.c.id).order_by(due.c.occurred_at, due.c.id)))
