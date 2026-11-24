"""What the owner is to be told about a Binance connection, kept until it is told (design 5.14,
plan Decision 15).

A step that finds something new (the normaliser's new flags; a reconciliation difference)
queues it in the connection's config, in the same transaction that records it as known. The
sync sends everything queued as one message when it ends and only then clears the queue: a
sync that fails after a step committed what it found still tells the owner, on the next sync
that succeeds, exactly once.
"""

import logging
from collections.abc import Iterable
from uuid import UUID

from lararium.core.commands import CommandContext
from lararium.core.connections import get_connection, set_warnings, update_config
from lararium.core.connectors import SyncContext
from lararium.core.json_types import JSONValue

logger = logging.getLogger(__name__)

FLAG_PREFIX = "Binance flagged: "
# Every flag already published.
FLAGS_KEY = "flags"
# The new flags not told yet (they share one line), and the other lines not told yet.
PENDING_FLAGS_KEY = "pending_flags"
PENDING_LINES_KEY = "pending_lines"


def _texts(value: JSONValue) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


async def queue(
    ctx: CommandContext,
    connection_id: UUID,
    *,
    flags: Iterable[str] = (),
    lines: Iterable[str] = (),
) -> None:
    """Add flags and lines to what the next message tells, in the caller's transaction."""
    config = (await get_connection(ctx.session, connection_id)).config
    values: dict[str, JSONValue] = {}
    for key, added in ((PENDING_FLAGS_KEY, list(flags)), (PENDING_LINES_KEY, list(lines))):
        held = _texts(config.get(key))
        merged = list(dict.fromkeys([*held, *added]))
        if merged != held:
            values[key] = list[JSONValue](merged)
    if values:
        await update_config(ctx, connection_id, values)


async def publish(ctx: CommandContext, connection_id: UUID, flags: Iterable[str]) -> list[str]:
    """Add the flags `config["flags"]` does not hold yet to it and queue them for the sync's
    message, in the caller's transaction, so a flag known is a flag told; keep every flag in
    the connection's warnings; return the new ones."""
    connection = await get_connection(ctx.session, connection_id)
    stored = connection.config.get(FLAGS_KEY)
    known = [f for f in stored if isinstance(f, str)] if isinstance(stored, list) else []
    new = sorted(set(flags) - set(known))
    if new:
        held: list[JSONValue] = [*known, *new]
        await update_config(ctx, connection_id, {FLAGS_KEY: held})
        await queue(ctx, connection_id, flags=new)
    warnings = [w for w in connection.warnings if not w.startswith(FLAG_PREFIX)]
    warnings += [FLAG_PREFIX + flag for flag in sorted({*known, *new})]
    if warnings != connection.warnings:
        await set_warnings(ctx, connection_id, warnings)
    return new


def message(display_name: str, config: dict[str, JSONValue]) -> str | None:
    """The one message for what is queued; None when nothing is."""
    flags = _texts(config.get(PENDING_FLAGS_KEY))
    lines = ([FLAG_PREFIX + ", ".join(flags)] if flags else []) + _texts(
        config.get(PENDING_LINES_KEY)
    )
    return f"{display_name}: " + "\n".join(lines) if lines else None


async def tell(ctx: SyncContext) -> None:
    """Send what is queued, then clear it in the sync's transaction. A send that raises keeps
    it queued; only the exception's class is logged, as its message could carry the
    notifier's webhook URL."""
    connection = await get_connection(ctx.command.session, ctx.connection.id)
    text = message(connection.display_name, connection.config)
    if text is None:
        return
    try:
        await ctx.notifier.send(text)
    except Exception as error:
        logger.warning("sending a Binance notification failed (%s)", type(error).__name__)
        return
    await update_config(ctx.command, connection.id, remove=[PENDING_FLAGS_KEY, PENDING_LINES_KEY])
