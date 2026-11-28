"""Heal stays fast on a ledger of realistic size: the pair relation is computed once per
heal pass over the records that can pair at all, so card payments cost nothing, each
record's evidence is looked up among its own import rows only, and the plans hold up
whatever the planner's statistics say."""

import time
from datetime import timedelta
from typing import Any, Literal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.entities import get_self_entity_id
from lararium.db import create_engine
from lararium.modules.finance.identifiers import assign_identifier, get_or_create_identifier
from lararium.modules.finance.instruments import InstrumentRecord
from lararium.modules.finance.transfers import list_transfers

from .conftest import (
    B_IDENTIFIER,
    WHEN,
    Heal,
    ImportedBank,
    MakeConnected,
    MakeManual,
    Run,
    SystemIds,
    insert_batch,
)

# 10,000 records: card payments and bank payments to strangers on A; rows a statement import
# posted on a manual account; transfers to B.
CARD_PAYMENTS = 5_000
IMPORTED_ROWS = 4_900
TRANSFERS_TO_B = 100
# Measured on docker-test through its ssh tunnel, either way the statistics stand: a heal
# with nothing due takes 0.5-1 s, most of it the one set-wise scan of the own records into
# the snapshot that the pair relation and what is due both read (some 6 index lookups for
# each of the 10,000 records, slower without statistics, as bitmap scans); healing 100
# newly identified transfers 5-11 s, nearly all of it the ~27 small indexed statements
# each record's own matching runs (its contra, its transfer, its placeholder, their
# audit), each a round trip through the tunnel. With JIT on (lararium.db turns it off), a
# heal with nothing due took 5.5 s without statistics, 2.4 s of it compiling the scan
# whose estimated cost the missing statistics inflate; with JIT off but the scan run twice
# (once for the pair relation, once for the snapshot), 1.1-2.3 s, and up to 3.4 s on CI.
# Before the snapshot, with stale statistics, finding what was due alone hit the 30 s
# statement timeout (and took 26 s before the pair relation was computed set-wise); so did
# reading what the imported rows print while rows and batches were joined (and took 11 s
# without statistics). The bounds allow a CI runner several times slower (CI has taken up
# to 3x what docker-test does) and still fail long before that timeout would.
NOTHING_DUE_BOUND = 6.0
REGISTERED_BOUND = 15.0
TABLES = (
    "transactions",
    "postings",
    "account_identifiers",
    "accounts",
    "transfers",
    "import_rows",
    "import_batches",
    "import_statements",
)

# The planner's statistics on the seeded tables. The backend role can't ANALYZE them, and
# the seed is uncommitted, so they can be: none, as on a fresh database; or stale, as when
# autovacuum analyzes them while the bulk insert is uncommitted (it happened on CI): the
# rows are invisible to it, so it records the pages they fill as holding none, and every
# estimate on the tables collapses to one row.
type Statistics = Literal["none", "stale"]


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
            "   :start + g * interval '86 minutes' as occurred_at,"
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


async def _seed_imported(
    session: AsyncSession, run: Run, *, account_id: Any, gbp: Any, unassigned: Any
) -> None:
    """IMPORTED_ROWS rows on the manual account I over 300 days, as a statement import posts
    them: each a committed row of one batch listing them all in full, a third incomes and a
    quarter bank transfers. Some print part of a bank account: a sort code alone (B's, which
    names B weakly once B is registered), a stranger's account number alone, or an ending;
    and every fifth is duplicated by a second batch's row, half of those printing what the
    committed one doesn't. None names an own account, so heal has nothing more to match."""
    complete = (WHEN.date() - timedelta(days=301), WHEN.date() + timedelta(days=1))
    batches = [await insert_batch(session, run, account_id, complete) for _ in range(2)]
    params = {
        "start": WHEN - timedelta(days=300),
        "owner": await get_self_entity_id(session),
        "account": account_id,
        "gbp": gbp,
        "unassigned": unassigned,
        "rows": IMPORTED_ROWS,
        "first": batches[0],
        "second": batches[1],
        "from": complete[0],
        "to": complete[1],
    }
    await session.execute(
        text(
            "create temporary table imported on commit drop as"
            " select uuidv7() as id, uuidv7() as row_id, g,"
            "   :start + g * interval '88 minutes' as occurred_at,"
            "   case when g % 3 = 0 then 100 else -12.34 end::numeric as quantity,"
            "   g % 4 = 0 as bank_transfer,"
            "   case when g % 7 = 0 then '112233' end as sort_code,"
            "   case when g % 11 = 0 then '87654321' end as account_number,"
            "   case when g % 13 = 0 then '4321' end as account_ending"
            " from generate_series(1, :rows) g"
        ),
        params,
    )
    statements = [
        "insert into finance.transactions (id, occurred_at, booking_date, description,"
        "  status, origin, owner_entity_id, bank_transfer)"
        " select id, occurred_at, occurred_at::date, 'Seeded import', 'booked', 'manual',"
        "  :owner, bank_transfer from imported",
        "insert into finance.postings (transaction_id, position, account_id, instrument_id,"
        "  quantity)"
        " select id, 0, :account, :gbp, quantity from imported"
        " union all select id, 1, :unassigned, :gbp, -quantity from imported",
        "insert into finance.import_rows (id, batch_id, account_id, position, kind, status,"
        "  booking_date, occurred_at, time_unit, description, quantity, instrument_id,"
        "  counterparty_sort_code, counterparty_account_number,"
        "  counterparty_account_ending, dedupe_key, raw, transaction_id)"
        " select row_id, :first, :account, g, 'statement', 'committed', occurred_at::date,"
        "  occurred_at, 'minute', 'Seeded import', quantity, :gbp, sort_code,"
        "  account_number, account_ending, 'seed:' || g, '{}', id from imported",
        # Every fifth again, in a later file: every tenth printing an ending it didn't.
        "insert into finance.import_rows (batch_id, account_id, position, kind, status,"
        "  booking_date, occurred_at, time_unit, description, quantity, instrument_id,"
        "  counterparty_sort_code, counterparty_account_number,"
        "  counterparty_account_ending, dedupe_key, duplicate_of, raw)"
        " select :second, :account, g, 'statement', 'duplicate', occurred_at::date,"
        "  occurred_at, 'minute', 'Seeded import', quantity, :gbp, sort_code,"
        "  account_number, case when g % 10 = 0 then '8765' else account_ending end,"
        "  'seed:' || g, row_id, '{}' from imported where g % 5 = 0",
        "insert into finance.import_statements (batch_id, period_from, period_to, currency)"
        " values (:first, :from, :to, 'GBP'), (:second, :from, :to, 'GBP')",
    ]
    for statement in statements:
        await session.execute(text(statement), params)


async def _analyze_elsewhere(owner_url: str) -> None:
    """ANALYZE the tables from another connection, as autovacuum would."""
    engine = create_engine(owner_url)
    try:
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            for table in TABLES:
                await conn.execute(text(f"ANALYZE finance.{table}"))
    finally:
        await engine.dispose()


@pytest.mark.parametrize("statistics", ["none", "stale"])
async def test_heal_is_fast_on_a_large_ledger(
    statistics: Statistics,
    owner_url: str,
    session: AsyncSession,
    run: Run,
    gbp: InstrumentRecord,
    ids: SystemIds,
    connected_bank: MakeConnected,
    manual_bank: MakeManual,
    imported_bank: ImportedBank,
    heal: Heal,
) -> None:
    a = await connected_bank("A")
    i = await imported_bank("I")
    # Opened before the seeded history, so every transfer to B gets its placeholder.
    b = await manual_bank("B", identifier=None, snapshot_at=WHEN - timedelta(days=301))
    await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    await _seed(
        session,
        connection_id=a.connection_id,
        account_id=a.account.id,
        gbp=gbp.id,
        unassigned=ids.unassigned,
    )
    await _seed_imported(session, run, account_id=i.id, gbp=gbp.id, unassigned=ids.unassigned)
    if statistics == "stale":
        await _analyze_elsewhere(owner_url)

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
