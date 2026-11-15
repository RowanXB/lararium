from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select, update

from lararium.core.connections import create_connection
from lararium.core.models import AuditLogEntry
from lararium.modules.finance.declined import DeclinedInput, upsert_declined
from lararium.modules.finance.models import DeclinedTransaction

WHEN = datetime(2026, 11, 2, 12, tzinfo=UTC)


async def _changes(session) -> list[str]:
    rows = (await session.execute(select(AuditLogEntry.changes))).scalars().all()
    return [key for changes in rows for key in changes if key.startswith("declined_transaction:")]


async def test_upsert_declined_created_unchanged_updated(session, run, make_account, gbp) -> None:
    connection = await run(create_connection, connector="monzo", display_name="m", config={})
    account = await make_account()
    first = DeclinedInput(
        connection_id=connection.id,
        external_id="tx_1",
        account_id=account.id,
        occurred_at=WHEN,
        quantity=Decimal("-3.00"),
        instrument_id=gbp.id,
        description="Coffee",
        counterparty_entity_id=None,
        place_entity_id=None,
        decline_reason="INSUFFICIENT_FUNDS",
    )
    assert await run(upsert_declined, first) == "created"
    row = (await session.execute(select(DeclinedTransaction))).scalar_one()
    # now() is the test's transaction start, so age the row to see updated_at move.
    old = datetime(2020, 1, 1, tzinfo=UTC)
    await session.execute(update(DeclinedTransaction).values(updated_at=old))
    recorded = len(await _changes(session))

    assert await run(upsert_declined, first) == "unchanged"
    assert len(await _changes(session)) == recorded

    changed = replace(first, decline_reason="CARD_INACTIVE", description="Coffee shop")
    assert await run(upsert_declined, changed) == "updated"
    assert len(await _changes(session)) == recorded + 1
    await session.refresh(row)
    assert (row.decline_reason, row.description) == ("CARD_INACTIVE", "Coffee shop")
    assert row.updated_at > old
    count = await session.scalar(select(func.count()).select_from(DeclinedTransaction))
    assert count == 1
