from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.actors import Actor
from lararium.core.json_types import JSONValue
from lararium.core.models import AuditLogEntry, Event


class CommandContext:
    """State for one command: who runs it, and the events and changes it produces."""

    def __init__(self, session: AsyncSession, actor: Actor, correlation_id: UUID) -> None:
        self.session = session
        self.actor = actor
        self.correlation_id = correlation_id
        self._events: list[Event] = []
        self._changes: dict[str, JSONValue] = {}

    def emit(
        self,
        event_type: str,
        subject_id: UUID | None,
        data: Mapping[str, JSONValue],
        version: int = 1,
    ) -> None:
        self._events.append(
            Event(
                type=event_type,
                source=event_type.split(".", 1)[0],
                subject_id=subject_id,
                data=dict(data),
                version=version,
            )
        )

    def record_change(self, key: str, value: JSONValue) -> None:
        self._changes[key] = value


@asynccontextmanager
async def command_context(
    session: AsyncSession,
    actor: Actor,
    command: str,
    args: Mapping[str, JSONValue],
    correlation_id: UUID | None = None,
) -> AsyncGenerator[CommandContext]:
    """Run a command as one unit: audit row, outbox events and changes commit together.

    On any exception the session is rolled back and the exception re-raised.
    """
    try:
        if correlation_id is None:
            new_id: UUID = (await session.execute(select(func.uuidv7()))).scalar_one()
            correlation_id = new_id
        ctx = CommandContext(session, actor, correlation_id)
        yield ctx
        session.add(
            AuditLogEntry(
                actor_type=actor.type,
                actor_id=actor.id,
                command=command,
                args=dict(args),
                changes=ctx._changes,  # pyright: ignore[reportPrivateUsage]
                correlation_id=correlation_id,
            )
        )
        session.add_all(ctx._events)  # pyright: ignore[reportPrivateUsage]
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
