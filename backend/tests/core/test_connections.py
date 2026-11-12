import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import exc, text

from lararium.core.connections import (
    ConnectionRecord,
    create_connection,
    get_connection,
    list_connections,
    load_credential,
    record_sync_failure,
    record_sync_success,
    set_status,
    set_warnings,
    store_credential,
    update_config,
)
from lararium.core.errors import ConnectionNotFoundError
from lararium.core.raw_records import list_raw_records, store_raw_record
from lararium.modules import registry

KEY = bytes(range(32))


async def _create(run) -> ConnectionRecord:
    return await run(
        create_connection, connector="test", display_name="Test bank", config={"client_id": "c1"}
    )


async def test_create_get_and_list_connections(session, run) -> None:
    created = await _create(run)
    assert (created.connector, created.display_name, created.status, created.config) == (
        "test",
        "Test bank",
        "created",
        {"client_id": "c1"},
    )
    assert (created.cursor, created.consecutive_failures, created.warnings) == (None, 0, [])
    assert (created.last_success_at, created.last_error, created.credential_expires_at) == (
        None,
        None,
        None,
    )
    assert await get_connection(session, created.id) == created
    assert await list_connections(session) == [created]
    event = (await session.execute(text("select type, subject_id from core.events"))).one()
    assert tuple(event) == ("core.connection.created", created.id)


async def test_unknown_connection_raises(session) -> None:
    with pytest.raises(ConnectionNotFoundError):
        await get_connection(session, uuid.uuid4())


async def test_credential_store_round_trip(session, run) -> None:
    connection = await _create(run)
    await run(store_credential, KEY, connection.id, "access_token", "tok-first", None)
    row = (
        await session.execute(
            text("select ciphertext, key_version from core.credentials where connection_id = :id"),
            {"id": connection.id},
        )
    ).one()
    assert b"tok-first" not in row.ciphertext
    assert row.key_version == 1
    loaded = await load_credential(session, KEY, connection.id, "access_token")
    assert loaded is not None and loaded.reveal() == "tok-first"

    expires = datetime(2026, 11, 12, 18, 0, tzinfo=UTC)
    await run(store_credential, KEY, connection.id, "access_token", "tok-second", expires)
    replaced = await load_credential(session, KEY, connection.id, "access_token")
    assert replaced is not None and replaced.reveal() == "tok-second"
    assert await session.scalar(text("select count(*) from core.credentials")) == 1
    assert await session.scalar(text("select expires_at from core.credentials")) == expires
    assert await load_credential(session, KEY, connection.id, "refresh_token") is None

    # The plaintext appears in no audit or event row.
    logged = await session.scalar(
        text(
            "select (select string_agg(args::text || changes::text, ' ') from core.audit_log)"
            " || (select string_agg(data::text, ' ') from core.events)"
        )
    )
    assert logged is not None and "tok-" not in logged


async def test_status_change_emits_event_once(session, run) -> None:
    connection = await _create(run)
    await run(set_status, connection.id, "authorising")
    again = await run(set_status, connection.id, "authorising", error="approve in the app")
    assert (again.status, again.last_error) == ("authorising", "approve in the app")
    events = (
        await session.execute(
            text(
                "select subject_id, data from core.events"
                " where type = 'core.connection.status_changed'"
            )
        )
    ).all()
    assert [tuple(e) for e in events] == [(connection.id, {"from": "created", "to": "authorising"})]


async def test_set_status_of_unknown_connection_raises(run) -> None:
    with pytest.raises(ConnectionNotFoundError):
        await run(set_status, uuid.uuid4(), "healthy")


async def test_failure_counter_resets_on_success(session, run) -> None:
    connection = await _create(run)
    assert await run(record_sync_failure, connection.id, "timeout") == 1
    assert await run(record_sync_failure, connection.id, "timeout again") == 2
    failing = await get_connection(session, connection.id)
    assert (failing.consecutive_failures, failing.last_error) == (2, "timeout again")

    synced = await run(record_sync_success, connection.id, {"since": "tx_1"})
    assert (synced.consecutive_failures, synced.last_error, synced.cursor) == (
        0,
        None,
        {"since": "tx_1"},
    )
    assert synced.last_success_at is not None


async def test_warnings_and_config_are_replaced(run) -> None:
    connection = await _create(run)
    warned = await run(set_warnings, connection.id, ["history may be truncated"])
    assert warned.warnings == ["history may be truncated"]
    configured = await run(update_config, connection.id, {"client_id": "c2", "n": 1})
    assert configured.config == {"client_id": "c2", "n": 1}
    assert configured.warnings == ["history may be truncated"]


async def test_raw_record_dedup_by_content(session, run) -> None:
    connection = await _create(run)
    sync_id = uuid.uuid4()

    async def store(content, sync) -> uuid.UUID:
        return await run(
            store_raw_record,
            connection_id=connection.id,
            record_type="transaction",
            request={"path": "/transactions"},
            content=content,
            connector_version="1",
            sync_id=sync,
        )

    first = await store({"id": "tx_1", "amount": -350}, sync_id)
    # Same content with the keys in another order, in a later sync: the same record.
    assert await store({"amount": -350, "id": "tx_1"}, uuid.uuid4()) == first
    changed = await store({"id": "tx_1", "amount": -360}, sync_id)
    assert changed != first

    records = await list_raw_records(session, sync_id)
    assert [r.id for r in records] == [first, changed]
    assert (records[0].record_type, records[0].content, records[0].request) == (
        "transaction",
        {"id": "tx_1", "amount": -350},
        {"path": "/transactions"},
    )
    assert records[0].content_hash != records[1].content_hash
    assert await session.scalar(text("select count(*) from core.raw_records")) == 2


@pytest.mark.parametrize(
    "statement", ["update core.raw_records set record_type = 'x'", "delete from core.raw_records"]
)
async def test_backend_role_cannot_modify_raw_records(session, statement: str) -> None:
    with pytest.raises(exc.ProgrammingError, match="permission denied"):
        await session.execute(text(statement))


async def test_account_source_must_be_a_connection(session) -> None:
    with pytest.raises(exc.IntegrityError, match="foreign key"):
        await session.execute(
            text(
                "insert into finance.accounts"
                " (name, ledger_type, subtype, owner_entity_id, balance_mode, source_kind,"
                "  source_connection_id)"
                " select 'x', 'asset', 'bank', id, 'transactions', 'connection', uuidv7()"
                " from core.entities where is_self"
            )
        )


def test_unknown_connector_raises() -> None:
    with pytest.raises(KeyError):
        registry.connector("no-such-connector")
