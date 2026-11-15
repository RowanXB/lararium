"""Which records heal (engine.heal_transfers) has to match again: those whose ledger state
disagrees with the rule."""

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, case, exists, func, literal, null, or_, select, union
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from lararium.modules.finance import models
from lararium.modules.finance.system_accounts import SystemAccountKey, system_account_ids
from lararium.modules.finance.transfers.contra import eligible_records, record_columns
from lararium.modules.finance.transfers.queries import (
    OPEN_STATUSES,
    before_history,
    pair_candidates,
)
from lararium.modules.finance.transfers.settings import TransferSettings


def _pair_states(
    system: Mapping[SystemAccountKey, UUID], settings: TransferSettings
) -> list[Select[*tuple[Any, ...]]]:
    """Records whose transfer disagrees with pair_candidates, which matching decides by:
    a unique pair (each the other's only candidate) not yet matched; a matched pair that
    is no longer unique; and an open transfer whose review reason is not the one its own
    record's candidates give (several_candidates if it has any of the same amount, else
    amount_differs if it has any of another, else none)."""
    tr, t = models.Transfer, models.Transaction
    pairs = pair_candidates(system, settings, exact=True).cte("pairs")
    differing = pair_candidates(system, settings, exact=False).cte("differing")
    degree = (
        select(pairs.c.record_id, func.count().label("n")).group_by(pairs.c.record_id).cte("degree")
    )
    of_record, of_candidate = degree.alias("of_record"), degree.alias("of_candidate")
    unique = (
        select(pairs.c.record_id, pairs.c.candidate_id)
        .join(of_record, and_(of_record.c.record_id == pairs.c.record_id, of_record.c.n == 1))
        .join(
            of_candidate,
            and_(of_candidate.c.record_id == pairs.c.candidate_id, of_candidate.c.n == 1),
        )
        .cte("unique_pairs")
    )
    matched = tr.status == "matched"
    present = func.coalesce(tr.out_transaction_id, tr.in_transaction_id)
    expected = case(
        (
            select(pairs.c.record_id).where(pairs.c.record_id == present).exists(),
            literal("several_candidates"),
        ),
        (
            select(differing.c.record_id).where(differing.c.record_id == present).exists(),
            literal("amount_differs"),
        ),
        else_=null(),
    )
    return [
        # (c) a unique pair not yet a matched transfer.
        select(t.id.label("id"), t.occurred_at)
        .join(unique, unique.c.record_id == t.id)
        .where(
            ~exists().where(
                matched,
                or_(
                    and_(
                        tr.out_transaction_id == unique.c.record_id,
                        tr.in_transaction_id == unique.c.candidate_id,
                    ),
                    and_(
                        tr.out_transaction_id == unique.c.candidate_id,
                        tr.in_transaction_id == unique.c.record_id,
                    ),
                ),
            )
        ),
        # (e) a matched transfer whose two records are no longer a unique pair.
        select(t.id.label("id"), t.occurred_at)
        .select_from(tr)
        .join(t, t.id == tr.out_transaction_id)
        .where(
            matched,
            ~select(unique.c.record_id)
            .where(
                unique.c.record_id == tr.out_transaction_id,
                unique.c.candidate_id == tr.in_transaction_id,
            )
            .exists(),
        ),
        # (e) an open transfer whose review reason disagrees with its record's candidates.
        select(t.id.label("id"), t.occurred_at)
        .select_from(tr)
        .join(t, t.id == present)
        .where(
            tr.status.in_(OPEN_STATUSES),
            or_(tr.out_transaction_id.is_(None), tr.in_transaction_id.is_(None)),
            tr.review_reason.is_distinct_from(expected),
        ),
    ]


async def records_to_heal(session: AsyncSession, settings: TransferSettings) -> list[UUID]:
    """Records whose ledger state disagrees with the rule (Decision 8), in occurrence order:
    matching each of them (engine.match_transaction) brings it in line."""
    system = await system_account_ids(session)
    in_transit, unassigned = system.get("in_transit"), system.get("unassigned")
    if unassigned is None:
        return []  # no connector has posted anything yet
    e = eligible_records(system)
    tr, i, d, t = (
        models.Transfer,
        models.AccountIdentifier,
        models.TransferDecision,
        models.Transaction,
    )
    active = tr.status != "rejected"
    is_leg = exists().where(
        active,
        or_(
            tr.out_transaction_id == e.c.transaction_id, tr.in_transaction_id == e.c.transaction_id
        ),
    )
    identified = exists().where(
        i.id == e.c.counterparty_identifier_id,
        i.account_id.is_not(None),
        i.account_id != e.c.account_id,
    )
    rejected = exists().where(
        d.kind == "reject", d.subject_kind == e.c.source_kind, d.subject_ref == e.c.source_ref
    )
    queries = [
        # (b) identified, still on Unassigned, not rejected.
        select(e.c.transaction_id.label("id"), e.c.occurred_at).where(
            e.c.contra_account_id == unassigned, identified, ~rejected
        )
    ]
    # (c) and (e): pairs, and review reasons, that disagree with pair_candidates.
    queries += _pair_states(system, settings)
    if in_transit is not None:
        # (a) in In transit without a transfer.
        queries.append(
            select(e.c.transaction_id.label("id"), e.c.occurred_at).where(
                e.c.contra_account_id == in_transit, ~is_leg
            )
        )
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
            # (d) legs that no longer fit their transfer.
            queries.append(
                select(t.id.label("id"), t.occurred_at)
                .select_from(tr)
                .join(t, t.id == leg)
                .outerjoin(e, e.c.transaction_id == leg)
                .outerjoin(i, i.id == e.c.counterparty_identifier_id)
                .where(
                    active,
                    or_(
                        e.c.transaction_id.is_(None),  # void, or no longer shaped like a leg
                        e.c.account_id != own_account,
                        e.c.instrument_id != tr.instrument_id,
                        e.c.quantity != quantity,
                        e.c.contra_account_id != in_transit,
                        # A transfer with one own record stands on that record naming the
                        # other account; it no longer does once the identifier moves.
                        and_(other_leg.is_(None), i.account_id.is_distinct_from(counterpart)),
                    ),
                )
            )
            # (d) a lone record whose counterpart's account history now starts after the
            # counterpart's latest time, or no longer does (older history was fetched).
            predates = before_history(record_columns(e), counterpart, settings)
            queries.append(
                select(e.c.transaction_id.label("id"), e.c.occurred_at)
                .select_from(tr)
                .join(e, e.c.transaction_id == leg)
                .where(
                    other_leg.is_(None),
                    or_(
                        and_(tr.status == "before_history", ~predates),
                        and_(tr.status == "awaiting", predates),
                    ),
                )
            )
        # (d) placeholders and settlements out of step with their record: other dates,
        # void, or missing.
        mirrored, derived = aliased(t, name="mirrored"), aliased(t, name="derived")
        queries.append(
            select(mirrored.id.label("id"), mirrored.occurred_at)
            .select_from(tr)
            .join(
                mirrored,
                mirrored.id == func.coalesce(tr.out_transaction_id, tr.in_transaction_id),
            )
            .outerjoin(
                derived,
                derived.id
                == func.coalesce(tr.placeholder_transaction_id, tr.settlement_transaction_id),
            )
            .where(
                active,
                or_(
                    and_(tr.status == "inferred", tr.placeholder_transaction_id.is_(None)),
                    and_(tr.status == "before_history", tr.settlement_transaction_id.is_(None)),
                    derived.status == "void",
                    derived.occurred_at != mirrored.occurred_at,
                    derived.booking_date != mirrored.booking_date,
                ),
            )
        )
    due = union(*queries).subquery()
    return list(await session.scalars(select(due.c.id).order_by(due.c.occurred_at, due.c.id)))
