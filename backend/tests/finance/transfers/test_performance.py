"""Heal stays fast on a ledger of realistic size: the pair relation is computed once per
heal pass over the records that can pair at all, so card payments cost nothing."""

import time
from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.entities import get_self_entity_id
from lararium.modules.finance.identifiers import assign_identifier, get_or_create_identifier
from lararium.modules.finance.instruments import InstrumentRecord
from lararium.modules.finance.transfers import list_transfers

from .conftest import B_IDENTIFIER, WHEN, Heal, MakeConnected, MakeManual, Run, SystemIds

CARD_PAYMENTS = 9_900
TRANSFERS_TO_B = 100
# Measured on docker-test through its ssh tunnel: a heal with nothing due takes about
# 0.1 s (two set-wise scans of the own records); healing 100 newly identified transfers
# about 2.6 s, nearly all of it the ~27 small indexed statements each record's own matching
# runs (its contra, its transfer, its placeholder, their audit). Before the pair relation
# was computed set-wise, finding what was due alone took 26 s here. The bounds leave room
# for a slower link and still fail long before the 30 s statement timeout would. The
# tables have no planner statistics here (the backend role can't ANALYZE them), so the
# plans must hold up without them.
NOTHING_DUE_BOUND = 2.0
REGISTERED_BOUND = 10.0


async def _seed(
    session: AsyncSession, *, connection_id: Any, account_id: Any, gbp: Any, unassigned: Any
) -> None:
    """CARD_PAYMENTS card payments and bank payments to strangers on A over 300 days, and
    TRANSFERS_TO_B payments naming B's bank account (not yet registered), every 3 days."""
    other = await session.scalar(
        text(
            "insert into finance.account_identifiers (kind, value, created_by)"
            " values ('uk_bank_account', '998877:87654321', 'test') returning id"
        )
    )
    b = await session.scalar(
        text("select id from finance.account_identifiers where value = :v"), {"v": B_IDENTIFIER}
    )
    params = {
        "start": WHEN - timedelta(days=300),
        "owner": await get_self_entity_id(session),
        "connection": connection_id,
        "other": other,
        "b": b,
        "cards": CARD_PAYMENTS,
        "transfers": TRANSFERS_TO_B,
    }
    await session.execute(
        text(
            "create temporary table seed on commit drop as"
            # Card payments, every fifth a bank payment to a stranger; a third are incomes.
            " select uuidv7() as id, 'c' || g as external_id,"
            "   :start + g * interval '43 minutes' as occurred_at,"
            "   case when g % 3 = 0 then 100 else -12.34 end::numeric as quantity,"
            "   case when g % 5 = 0 then cast(:other as uuid) end as identifier_id,"
            "   g % 5 = 0 as bank_transfer"
            " from generate_series(1, :cards) g"
            " union all"
            " select uuidv7(), 'x' || g, :start + g * interval '3 days', -100,"
            "   cast(:b as uuid), true"
            " from generate_series(1, :transfers) g"
        ),
        params,
    )
    await session.execute(
        text(
            "insert into finance.transactions (id, occurred_at, booking_date, description,"
            "  status, origin, owner_entity_id, connection_id, external_id,"
            "  counterparty_identifier_id, counterparty_name, bank_transfer)"
            " select id, occurred_at, occurred_at::date, 'Seeded', 'booked', 'connector',"
            "  :owner, :connection, external_id, identifier_id, 'Someone', bank_transfer"
            " from seed"
        ),
        params,
    )
    await session.execute(
        text(
            "insert into finance.postings (transaction_id, position, account_id, instrument_id,"
            "  quantity)"
            " select id, 0, :account, :gbp, quantity from seed"
            " union all select id, 1, :unassigned, :gbp, -quantity from seed"
        ),
        {"account": account_id, "gbp": gbp, "unassigned": unassigned},
    )


async def test_heal_is_fast_on_a_large_ledger(
    session: AsyncSession,
    run: Run,
    gbp: InstrumentRecord,
    ids: SystemIds,
    connected_bank: MakeConnected,
    manual_bank: MakeManual,
    heal: Heal,
) -> None:
    a = await connected_bank("A")
    b = await manual_bank("B", identifier=None)
    await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    await _seed(
        session,
        connection_id=a.connection_id,
        account_id=a.account.id,
        gbp=gbp.id,
        unassigned=ids.unassigned,
    )

    started = time.monotonic()
    assert await heal() == 0
    nothing_due = time.monotonic() - started
    assert nothing_due < NOTHING_DUE_BOUND, f"a heal with nothing due took {nothing_due:.2f} s"

    await run(assign_identifier, b.id, "uk_bank_account", B_IDENTIFIER)
    started = time.monotonic()
    assert await heal() == TRANSFERS_TO_B
    registered = time.monotonic() - started
    assert registered < REGISTERED_BOUND, f"healing {TRANSFERS_TO_B} took {registered:.2f} s"
    transfers = await list_transfers(session)
    assert len(transfers) == TRANSFERS_TO_B
    assert {t.status for t in transfers} == {"inferred"}
