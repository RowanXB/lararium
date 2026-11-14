"""Connections to outside data sources: their lifecycle, sync state and encrypted credentials.

Credentials are encrypted before they reach the database and never go into events, the
audit log or a ConnectionRecord.
"""

import hashlib
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import ColumnElement, Text, bindparam, func, select, update
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext
from lararium.core.entities import get_self_entity_id
from lararium.core.errors import ConnectionNotFoundError
from lararium.core.json_types import JSONValue
from lararium.core.models import Connection, Credential
from lararium.core.secrets import KEY_VERSION, Secret, decrypt, encrypt

ConnectionStatus = Literal[
    "created", "authorising", "healthy", "failing", "needs_reauth", "disabled"
]


@dataclass(frozen=True)
class ConnectionRecord:
    id: UUID
    connector: str
    display_name: str
    status: ConnectionStatus
    config: dict[str, JSONValue]
    cursor: JSONValue
    last_success_at: datetime | None
    last_error: str | None
    consecutive_failures: int
    credential_expires_at: datetime | None
    warnings: list[str]
    created_at: datetime


async def _load(session: AsyncSession, *conditions: ColumnElement[bool]) -> list[ConnectionRecord]:
    """Connections matching all conditions, sorted by display name (then id)."""
    # Select columns, not ORM objects, so a stale identity map can't hide an update.
    c = Connection
    rows = await session.execute(
        select(
            c.id,
            c.connector,
            c.display_name,
            c.status,
            c.config,
            c.cursor,
            c.last_success_at,
            c.last_error,
            c.consecutive_failures,
            c.credential_expires_at,
            c.warnings,
            c.created_at,
        )
        .where(*conditions)
        .order_by(c.display_name, c.id)
    )
    return [
        ConnectionRecord(
            id=row.id,
            connector=row.connector,
            display_name=row.display_name,
            status=cast(ConnectionStatus, row.status),
            config=row.config,
            cursor=row.cursor,
            last_success_at=row.last_success_at,
            last_error=row.last_error,
            consecutive_failures=row.consecutive_failures,
            credential_expires_at=row.credential_expires_at,
            warnings=row.warnings,
            created_at=row.created_at,
        )
        for row in rows
    ]


def _optional_iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _change_data(record: ConnectionRecord) -> dict[str, JSONValue]:
    return {
        "id": str(record.id),
        "connector": record.connector,
        "display_name": record.display_name,
        "status": record.status,
        "config": record.config,
        "cursor": record.cursor,
        "last_success_at": _optional_iso(record.last_success_at),
        "last_error": record.last_error,
        "consecutive_failures": record.consecutive_failures,
        "credential_expires_at": _optional_iso(record.credential_expires_at),
        "warnings": list[JSONValue](record.warnings),
    }


async def get_connection(session: AsyncSession, connection_id: UUID) -> ConnectionRecord:
    found = await _load(session, Connection.id == connection_id)
    if not found:
        raise ConnectionNotFoundError(str(connection_id))
    return found[0]


async def list_connections(session: AsyncSession) -> list[ConnectionRecord]:
    return await _load(session)


async def _update(ctx: CommandContext, connection_id: UUID, **values: Any) -> ConnectionRecord:
    updated = await ctx.session.scalar(
        update(Connection)
        .where(Connection.id == connection_id)
        .values(**values, updated_at=func.now())
        .returning(Connection.id)
    )
    if updated is None:
        raise ConnectionNotFoundError(str(connection_id))
    record = await get_connection(ctx.session, connection_id)
    ctx.record_change(f"connection:{connection_id}", _change_data(record))
    return record


async def create_connection(
    ctx: CommandContext, *, connector: str, display_name: str, config: dict[str, JSONValue]
) -> ConnectionRecord:
    session = ctx.session
    connection_id = (
        await session.execute(
            insert(Connection)
            .values(
                connector=connector,
                display_name=display_name,
                status="created",
                config=config,
                owner_entity_id=await get_self_entity_id(session),
            )
            .returning(Connection.id)
        )
    ).scalar_one()
    record = await get_connection(session, connection_id)
    ctx.emit("core.connection.created", connection_id, {"connection_id": str(connection_id)})
    ctx.record_change(f"connection:{connection_id}", _change_data(record))
    return record


async def _locked_status(ctx: CommandContext, connection_id: UUID) -> ConnectionStatus:
    # Lock the row, so two concurrent changes can't both report the same "from".
    previous = await ctx.session.scalar(
        select(Connection.status).where(Connection.id == connection_id).with_for_update()
    )
    if previous is None:
        raise ConnectionNotFoundError(str(connection_id))
    return cast(ConnectionStatus, previous)


async def _change_status(
    ctx: CommandContext,
    connection_id: UUID,
    previous: ConnectionStatus,
    status: ConnectionStatus,
    error: str | None,
) -> ConnectionRecord:
    values: dict[str, Any] = {"status": status}
    if error is not None:
        values["last_error"] = error
    record = await _update(ctx, connection_id, **values)
    if previous != status:
        ctx.emit("core.connection.status_changed", connection_id, {"from": previous, "to": status})
    return record


async def set_status(
    ctx: CommandContext,
    connection_id: UUID,
    status: ConnectionStatus,
    *,
    error: str | None = None,
) -> ConnectionRecord:
    """Set the status, and last_error when given; emit an event only if the status changes."""
    previous = await _locked_status(ctx, connection_id)
    return await _change_status(ctx, connection_id, previous, status, error)


async def transition_status(
    ctx: CommandContext,
    connection_id: UUID,
    status: ConnectionStatus,
    *,
    only_from: Collection[ConnectionStatus],
    error: str | None = None,
) -> ConnectionStatus | None:
    """Set the status only if the current one, read under the row lock, is in `only_from`.

    Return the status it had, or None when it was left alone: a change made meanwhile (the
    owner disabled the connection, say) wins over one decided on an older reading.
    """
    previous = await _locked_status(ctx, connection_id)
    if previous not in only_from:
        return None
    await _change_status(ctx, connection_id, previous, status, error)
    return previous


async def record_sync_success(
    ctx: CommandContext, connection_id: UUID, cursor: JSONValue
) -> ConnectionRecord:
    return await _update(
        ctx,
        connection_id,
        cursor=cursor,
        last_success_at=func.now(),
        last_error=None,
        consecutive_failures=0,
    )


async def record_sync_failure(ctx: CommandContext, connection_id: UUID, error: str) -> int:
    """Record a failed sync; return the number of failures in a row, this one included."""
    record = await _update(
        ctx,
        connection_id,
        last_error=error,
        consecutive_failures=Connection.consecutive_failures + 1,
    )
    return record.consecutive_failures


async def set_warnings(
    ctx: CommandContext, connection_id: UUID, warnings: list[str]
) -> ConnectionRecord:
    return await _update(ctx, connection_id, warnings=warnings)


async def update_config(
    ctx: CommandContext,
    connection_id: UUID,
    values: Mapping[str, JSONValue] | None = None,
    *,
    remove: Collection[str] = (),
) -> ConnectionRecord:
    """Set the given keys and remove others, leaving every other key as it is.

    The merge happens in the UPDATE itself, on the row as it is then, so writers that each own
    their keys (a sync's account map, the API's OAuth state) never undo each other's changes.
    """
    config: Any = Connection.config
    if values:
        config = config.op("||", return_type=JSONB)(bindparam(None, dict(values), type_=JSONB))
    if remove:
        config = config.op("-", return_type=JSONB)(bindparam(None, list(remove), type_=ARRAY(Text)))
    return await _update(ctx, connection_id, config=config)


OAUTH_STATE_KEY = "oauth_state"


def _state_hash(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


async def store_oauth_state(
    ctx: CommandContext, connection_id: UUID, state: str, valid_for: timedelta
) -> None:
    """Remember an OAuth state for this connection: only its hash, and when it expires.

    A new state replaces the previous one, which then no longer works.
    """
    expires_at = datetime.now(UTC) + valid_for
    stored: dict[str, JSONValue] = {
        "hash": _state_hash(state),
        "expires_at": expires_at.isoformat(),
    }
    await update_config(ctx, connection_id, {OAUTH_STATE_KEY: stored})


async def consume_oauth_state(ctx: CommandContext, state: str) -> UUID | None:
    """The connection whose OAuth state this is, if it hasn't expired; None otherwise.

    The state is removed either way, so it works once: the row lock makes a second, concurrent
    use wait for this one and then find nothing.
    """
    stored_hash = Connection.config[OAUTH_STATE_KEY]["hash"].astext
    row = (
        await ctx.session.execute(
            select(Connection.id, Connection.config[OAUTH_STATE_KEY]["expires_at"].astext)
            .where(stored_hash == _state_hash(state))
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        return None
    connection_id, expires_at = row
    await update_config(ctx, connection_id, remove=[OAUTH_STATE_KEY])
    if expires_at is None or datetime.fromisoformat(expires_at) <= datetime.now(UTC):
        return None
    return connection_id


def _aad(connection_id: UUID, purpose: str) -> str:
    # Binds the ciphertext to its row: it can't be moved to another connection or purpose.
    return f"{connection_id}:{purpose}"


async def store_credential(
    ctx: CommandContext,
    master_key: bytes,
    connection_id: UUID,
    purpose: str,
    value: str,
    expires_at: datetime | None,
) -> None:
    """Encrypt and store a credential, replacing any with the same purpose."""
    nonce, ciphertext = encrypt(master_key, value, _aad(connection_id, purpose))
    statement = insert(Credential).values(
        connection_id=connection_id,
        purpose=purpose,
        nonce=nonce,
        ciphertext=ciphertext,
        key_version=KEY_VERSION,
        expires_at=expires_at,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[Credential.connection_id, Credential.purpose],
        set_={
            "nonce": statement.excluded.nonce,
            "ciphertext": statement.excluded.ciphertext,
            "key_version": statement.excluded.key_version,
            "expires_at": statement.excluded.expires_at,
            "updated_at": func.now(),
        },
    )
    await ctx.session.execute(statement)
    # The audit log learns that the credential changed, never its value.
    ctx.record_change(
        f"credential:{connection_id}:{purpose}", {"expires_at": _optional_iso(expires_at)}
    )


async def load_credential(
    session: AsyncSession, master_key: bytes, connection_id: UUID, purpose: str
) -> Secret | None:
    row = (
        await session.execute(
            select(Credential.nonce, Credential.ciphertext).where(
                Credential.connection_id == connection_id, Credential.purpose == purpose
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return decrypt(master_key, row.nonce, row.ciphertext, _aad(connection_id, purpose))


async def load_credential_saved_at(
    session: AsyncSession, connection_id: UUID, purpose: str
) -> datetime | None:
    """When the stored credential was last saved; None if it doesn't exist."""
    return await session.scalar(
        select(Credential.updated_at).where(
            Credential.connection_id == connection_id, Credential.purpose == purpose
        )
    )


async def load_credential_expiry(
    session: AsyncSession, connection_id: UUID, purpose: str
) -> datetime | None:
    """When the stored credential expires; None if it has no expiry or doesn't exist."""
    return await session.scalar(
        select(Credential.expires_at).where(
            Credential.connection_id == connection_id, Credential.purpose == purpose
        )
    )
