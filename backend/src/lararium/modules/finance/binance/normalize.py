"""Binance raw records into the typed tables and the ledger (design 5.3, 5.7, 5.13).

A run applies every history raw record after `config["normalized_through"]` (raw record ids
are UUIDv7, so in the order they were stored): each is checked against the catalogue (a field
or value it does not list becomes a flag), its typed row upserted, and its label's handler
run. A row that changed (a withdrawal that failed) is a new raw record, so it is applied again
and updates its typed row and transactions in place. Then rewards are linked to the dividends
that paid them (rule 3), and the new flags published.

The run writes in a transaction of its own: making a wallet's account and setting the
connection's config lock its row, which a later step of the sync that walks (in short
transactions of its own) must be able to write. Its arithmetic is exact, as the ledger's
balance check is.
"""

from collections.abc import Iterable
from decimal import MAX_PREC, localcontext
from uuid import UUID

from sqlalchemy import select, text

from lararium.core import models as core_models
from lararium.core.commands import command_context
from lararium.core.connections import get_connection, set_warnings, update_config
from lararium.core.connectors import SyncContext
from lararium.core.json_types import JSONValue
from lararium.core.raw_records import RawRecord, get_raw_records
from lararium.modules.finance.binance import layout, tables
from lararium.modules.finance.binance.catalogue import ENDPOINTS
from lararium.modules.finance.binance.fetch import record_type
from lararium.modules.finance.binance.parse import ApiValue, Row, as_api, parse
from lararium.modules.finance.binance.records import (
    BinanceRecordError,
    Current,
    Handler,
    NormalizeReport,
    Run,
    capital,
    earn,
    post,
    remove,
    spot,
)
from lararium.modules.finance.binance.records import text as field_text

__all__ = [
    "HANDLERS",
    "NORMALIZER_VERSION",
    "BinanceRecordError",
    "Handler",
    "NormalizeReport",
    "Run",
    "link_payouts",
    "normalize",
    "post",
    "publish_flags",
    "regenerate",
    "remove",
]

NORMALIZER_VERSION = "binance-norm-1"
VERSION_KEY = "normalizer_version"
NORMALIZED_THROUGH_KEY = "normalized_through"
FLAGS_KEY = "flags"
FLAG_PREFIX = "Binance flagged: "
# Rule 3: a reward and the dividend that paid it are at most this far apart.
PAYOUT_WINDOW_HOURS = 26

# A label with no handler is typed only.
HANDLERS: dict[str, Handler] = {
    capital.DEPOSITS: capital.deposit,
    capital.WITHDRAWALS: capital.withdrawal,
    spot.TRADES: spot.trade,
    spot.CONVERT: spot.convert,
    spot.DIVIDENDS: spot.dividend,
    earn.SUBSCRIPTIONS: earn.flexible_subscription,
    earn.REDEMPTIONS: earn.flexible_redemption,
    earn.REWARDS: earn.flexible_reward,
}
# The history labels whose rows have typed tables, by raw record type.
_LABEL_OF = {
    record_type(label): label for label in layout.TABLE_LABELS if ENDPOINTS[label].kind == "history"
}


def _body(label: str, row: Row) -> ApiValue:
    """One row as the label's answer would carry it, for the parser."""
    where = ENDPOINTS[label].rows
    if where == "":
        return dict(row)
    body: ApiValue = [dict(row)]
    if where != ".":
        for step in reversed(where.split(".")):
            body = {step: body}
    return body


def _row_path(label: str, path: str) -> str:
    """A path of the parser's relative to the row (`rows[].status` is `status`)."""
    where = ENDPOINTS[label].rows
    prefix = "[]." if where == "." else f"{where}[]."
    return path.removeprefix(prefix)


async def _apply(run: Run, record: RawRecord) -> None:
    label = _LABEL_OF[record.record_type]
    content = as_api(record.content)
    occurrence: int | None = None
    if not ENDPOINTS[label].id_fields:
        assert isinstance(content, dict)
        held, number = content["row"], content["occurrence"]
        assert isinstance(held, dict) and isinstance(number, int)
        content, occurrence = held, number
    assert isinstance(content, dict)
    parsed = parse(label, _body(label, content))
    for path in sorted(parsed.unknown_fields):
        run.flag(f"{label}.{_row_path(label, path)} (a field the catalogue does not list)")
    for path, value in sorted(parsed.unknown_enums):
        run.flag(f"{label}.{_row_path(label, path)}={value}")
    [row] = parsed.rows

    name = record_type(label).removeprefix("binance.")
    root = layout.layout(label)[0]
    key: dict[str, object] = {"connection_id": run.connection.id}
    if occurrence is None:
        ids = [field_text(row, field) or "" for field in ENDPOINTS[label].id_fields]
    else:
        digest = tables.content_hash(row)
        key |= {layout.CONTENT_HASH: digest, layout.OCCURRENCE: occurrence}
        ids = [digest, str(occurrence)]
    await tables.store(run.command, label, key, row)
    values = tables.columns_for(label, row)
    typed_key = {name_: key[name_] if name_ in key else values[name_] for name_ in root.key}
    run.current = Current(label, record, ":".join([name, *ids]), typed_key)
    run.report.records += 1
    handler = HANDLERS.get(label)
    if handler is not None:
        await handler(run, row)
    run.current = None


async def _records(run: Run, after: UUID | None) -> list[RawRecord]:
    """The connection's history raw records stored after `after` (all with None), in order."""
    r = core_models.RawRecord
    query = select(r.id).where(
        r.connection_id == run.connection.id, r.record_type.in_(list(_LABEL_OF))
    )
    if after is not None:
        query = query.where(r.id > after)
    ids = list(await run.session.scalars(query.order_by(r.id)))
    return await get_raw_records(run.session, ids)


async def link_payouts(run: Run) -> None:
    """Rule 3: each reward with no dividend gets the unlinked dividend of the same asset and
    amount within 26 hours of it (the nearest), and its own posting is removed: the payout is
    posted where it landed, on Spot. Rewards are taken oldest first."""
    rows = await run.session.execute(
        text(
            "select r.content_hash, r.occurrence, d.id as dividend_id"
            " from finance.binance_earn_flexible_rewards r"
            " join finance.binance_dividends d on d.connection_id = r.connection_id"
            "  and d.asset = r.asset and d.amount = r.rewards"
            "  and coalesce(d.direction, 1) = 1"
            "  and d.div_time between r.time - make_interval(hours => :h)"
            "   and r.time + make_interval(hours => :h)"
            " where r.connection_id = :c and r.dividend_id is null"
            "  and not exists (select 1 from finance.binance_earn_flexible_rewards x"
            "   where x.connection_id = :c and x.dividend_id = d.id)"
            " order by r.time, r.content_hash, r.occurrence,"
            "  abs(extract(epoch from d.div_time - r.time)), d.div_time, d.id"
        ),
        {"c": run.connection.id, "h": PAYOUT_WINDOW_HOURS},
    )
    linked: set[tuple[str, int]] = set()
    paid: set[str] = set()
    name = record_type(earn.REWARDS).removeprefix("binance.")
    for row in rows.all():
        reward = (row.content_hash, row.occurrence)
        if reward in linked or row.dividend_id in paid:
            continue
        linked.add(reward)
        paid.add(row.dividend_id)
        await run.session.execute(
            text(
                "update finance.binance_earn_flexible_rewards"
                " set dividend_id = :d, transaction_id = null"
                " where connection_id = :c and content_hash = :h and occurrence = :o"
            ),
            {
                "d": row.dividend_id,
                "c": run.connection.id,
                "h": row.content_hash,
                "o": row.occurrence,
            },
        )
        await remove(run, f"{name}:{row.content_hash}:{row.occurrence}")


async def publish_flags(run: Run) -> list[str]:
    """Add this run's flags that `config["flags"]` does not hold yet to it, keep every flag
    in the connection's warnings, and return the new ones (for one notification line)."""
    connection = await get_connection(run.session, run.connection.id)
    stored = connection.config.get(FLAGS_KEY)
    known = [f for f in stored if isinstance(f, str)] if isinstance(stored, list) else []
    new = sorted(set(run.report.flagged) - set(known))
    if new:
        flags: list[JSONValue] = [*known, *new]
        await update_config(run.command, connection.id, {FLAGS_KEY: flags})
    warnings = [w for w in connection.warnings if not w.startswith(FLAG_PREFIX)]
    warnings += [FLAG_PREFIX + flag for flag in sorted({*known, *new})]
    if warnings != connection.warnings:
        await set_warnings(run.command, connection.id, warnings)
    return new


async def normalize(ctx: SyncContext, *, flags: Iterable[str] = ()) -> NormalizeReport:
    """Apply the raw records stored since the last run; `flags` are the sync's own (the
    wallet checks), published with the run's. A new NORMALIZER_VERSION applies them all."""
    return await _normalize(ctx, everything=False, flags=flags)


async def regenerate(ctx: SyncContext) -> NormalizeReport:
    """Apply every raw record of the connection again."""
    return await _normalize(ctx, everything=True, flags=())


async def _normalize(
    ctx: SyncContext, *, everything: bool, flags: Iterable[str]
) -> NormalizeReport:
    args: dict[str, JSONValue] = {"connection_id": str(ctx.connection.id)}
    with localcontext(prec=MAX_PREC):
        async with (
            ctx.session_factory() as session,
            command_context(session, ctx.command.actor, "binance.normalize", args) as cmd,
        ):
            connection = await get_connection(session, ctx.connection.id)
            config = connection.config
            through = config.get(NORMALIZED_THROUGH_KEY)
            replay = everything or config.get(VERSION_KEY) != NORMALIZER_VERSION
            after = UUID(through) if isinstance(through, str) and not replay else None
            run = await Run.start(ctx, cmd, connection)
            for flag in flags:
                run.flag(flag)
            last = after
            for record in await _records(run, after):
                await _apply(run, record)
                last = record.id
            await link_payouts(run)
            run.report.new_flags = await publish_flags(run)
            run.report.touched = run.touched
            values: dict[str, JSONValue] = {}
            if last is not None and through != str(last):
                values[NORMALIZED_THROUGH_KEY] = str(last)
            if config.get(VERSION_KEY) != NORMALIZER_VERSION:
                values[VERSION_KEY] = NORMALIZER_VERSION
            if values:
                await update_config(cmd, connection.id, values)
            return run.report
