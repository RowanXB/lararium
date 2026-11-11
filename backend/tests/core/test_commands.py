import pytest
from sqlalchemy import text

from financial_gate.core.commands import command_context


async def test_command_context_writes_audit_and_events(session, actor) -> None:
    async with command_context(session, actor, "test.ping", {"n": 1}) as ctx:
        ctx.emit("test.pinged", subject_id=None, data={"n": 1})
    audit = (
        await session.execute(
            text("select actor_type, actor_id, command, args from core.audit_log")
        )
    ).one()
    assert tuple(audit) == ("user", "test", "test.ping", {"n": 1})
    event = (
        await session.execute(
            text("select type, source, data, version, published_at from core.events")
        )
    ).one()
    assert tuple(event) == ("test.pinged", "test", {"n": 1}, 1, None)


async def test_command_context_persists_nothing_on_error(session, actor) -> None:
    with pytest.raises(RuntimeError):
        async with command_context(session, actor, "test.fail", {}) as ctx:
            ctx.emit("test.failed", subject_id=None, data={})
            raise RuntimeError("boom")
    assert await session.scalar(text("select count(*) from core.audit_log")) == 0
    assert await session.scalar(text("select count(*) from core.events")) == 0


async def test_event_insert_notifies_listeners(backend_url: str) -> None:
    import psycopg

    dsn = backend_url.replace("postgresql+psycopg://", "postgresql://")
    async with (
        await psycopg.AsyncConnection.connect(dsn, autocommit=True) as listener,
        await psycopg.AsyncConnection.connect(dsn, autocommit=True) as writer,
    ):
        await listener.execute("LISTEN core_events")
        cursor = await writer.execute(
            "insert into core.events (type, source, data, version)"
            " values ('test.x', 'test', '{}', 1) returning id"
        )
        (event_id,) = await cursor.fetchone()
        notify = await anext(listener.notifies(timeout=5))
        await writer.execute("delete from core.events where id = %s", (event_id,))
    assert (notify.channel, notify.payload) == ("core_events", str(event_id))
