import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import insert

from lararium.core import models
from lararium.core.connections import ConnectionRecord, create_connection
from lararium.core.raw_records import list_raw_records

T0 = datetime(2026, 3, 1, 12, tzinfo=UTC)


async def _create(run, name: str) -> ConnectionRecord:
    return await run(create_connection, connector="test", display_name=name, config={})


async def _insert(
    session, connection_id: uuid.UUID, record_type: str, content: Any, fetched_at: datetime
) -> uuid.UUID:
    """A record fetched at the given time (store_raw_record leaves it to the database)."""
    digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
    r = models.RawRecord
    return await session.scalar(
        insert(r)
        .values(
            connection_id=connection_id,
            sync_id=uuid.uuid4(),
            record_type=record_type,
            request={"account_id": "acc_TEST1"},
            fetched_at=fetched_at,
            content=content,
            content_hash=digest,
            connector_version="1",
        )
        .returning(r.id)
    )


async def test_list_raw_records_of_a_connection_and_type_by_fetch_time(session, run) -> None:
    mine, other = await _create(run, "Test bank"), await _create(run, "Other bank")
    later = await _insert(session, mine.id, "page", [{"id": "tx_1"}], T0 + timedelta(hours=1))
    first = await _insert(session, mine.id, "page", [{"id": "tx_2"}], T0)
    # Fetched at the same time as `first`: after it, by id.
    same_time = await _insert(session, mine.id, "page", [{"id": "tx_3"}], T0)
    await _insert(session, mine.id, "balance", {"balance": 1}, T0)
    await _insert(session, other.id, "page", [{"id": "tx_4"}], T0)

    records = await list_raw_records(session, mine.id, "page")

    assert [r.id for r in records] == [first, same_time, later]
    assert (
        records[0].connection_id,
        records[0].record_type,
        records[0].request,
        records[0].content,
        records[0].fetched_at,
    ) == (mine.id, "page", {"account_id": "acc_TEST1"}, [{"id": "tx_2"}], T0)
    assert await list_raw_records(session, mine.id, "pots") == []
