"""Writes to the typed `finance.binance_*` tables: a row's fields as the columns' types, and an
upsert that writes only what changed.

`columns_for` converts a record by the catalogue's field types (plan Decision 3); `store`
writes a record and the child tables below it. Nothing here logs or reports a value: an error
names the label and the field.
"""

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Table, delete, or_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult

from lararium.core.commands import CommandContext
from lararium.core.models import Base
from lararium.modules.finance.binance import layout
from lararium.modules.finance.binance.models import model_of
from lararium.modules.finance.binance.parse import ApiValue, BinanceDataError, Parsed, Row

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# Binance's text times: `2019-10-12 11:12:02` (UTC), or milliseconds since the epoch as text.
_TEXT_TIME = "%Y-%m-%d %H:%M:%S"


def _fail(label: str, path: str | None) -> BinanceDataError:
    return BinanceDataError(label, path or "")


def _decimal(label: str, path: str | None, value: ApiValue) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal | int | str):
        raise _fail(label, path)
    try:
        number = Decimal(value)
    except ArithmeticError:
        raise _fail(label, path) from None
    if not number.is_finite():
        raise _fail(label, path)
    return number


def _integer(label: str, path: str | None, value: ApiValue) -> int:
    number = _decimal(label, path, value)
    if number != number.to_integral():
        raise _fail(label, path)
    return int(number)


def _moment_ms(label: str, path: str | None, value: ApiValue) -> datetime:
    return _EPOCH + timedelta(milliseconds=_integer(label, path, value))


def _moment_text(label: str, path: str | None, value: ApiValue) -> datetime:
    if isinstance(value, str) and not value.strip().lstrip("-").isdigit():
        try:
            return datetime.strptime(value.strip(), _TEXT_TIME).replace(tzinfo=UTC)
        except ValueError:
            raise _fail(label, path) from None
    return _moment_ms(label, path, value)


def _text(label: str, path: str | None, value: ApiValue) -> str:
    if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
        raise _fail(label, path)
    if isinstance(value, Decimal):
        return str(_integer(label, path, value))
    return str(value)


def _convert(label: str, column: layout.Column, value: ApiValue) -> object:
    """A value of the column's catalogue type as the column holds it."""
    kind, path = column.type, column.path
    if column.twin:
        return _moment_text(label, path, value)
    match kind:
        case "decimal":
            return _decimal(label, path, value)
        case "integer":
            return _integer(label, path, value)
        case "boolean":
            if not isinstance(value, bool):
                raise _fail(label, path)
            return value
        case "time_ms":
            return _moment_ms(label, path, value)
        case "id" | "text" | "time_text":
            return _text(label, path, value)
        case _:
            raise AssertionError(kind)


def _reach(value: ApiValue, reads: tuple[str, ...]) -> ApiValue:
    for key in reads:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _values(label: str, table: layout.Table, element: ApiValue) -> dict[str, object]:
    """The table's field columns from one element (for a response table, the whole body); a
    field the element lacks is None."""
    values: dict[str, object] = {}
    for column in table.columns:
        if column.path is None:
            continue
        raw = _reach(element, column.reads)
        if raw is None:
            values[column.name] = None
        elif column.sql.endswith("[]"):
            if not isinstance(raw, list):
                raise _fail(label, column.path)
            values[column.name] = [_convert(label, column, item) for item in raw]
        else:
            values[column.name] = _convert(label, column, raw)
    return values


def columns_for(label: str, row: Row) -> dict[str, object]:
    """The root table's field columns for a record, by Decision 3.

    `row` is the record (for a label whose answer is one record, the answer itself). Keys and
    system columns (`connection_id`, `snapshot_at`, `read_to`, `transaction_id`,
    `content_hash`) are the caller's, a child table's columns are written by `store`, and what
    the response says beside its records is `store_response`'s.
    """
    return _values(label, layout.layout(label)[0], dict(row))


def content_hash(row: Row) -> str:
    """The first 24 hex digits of the SHA-256 of a record's canonical JSON (Decision 5): a
    record with no id of its own is keyed by it and its occurrence."""

    def plain(value: ApiValue) -> object:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, list):
            return [plain(v) for v in value]
        if isinstance(value, dict):
            return {k: plain(v) for k, v in value.items()}
        return value

    canonical = json.dumps(plain(dict(row)), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:24]


def map_row(label: str, key: str, row: Row) -> dict[str, ApiValue]:
    """A record of a body that is a map (`asset-detail`): the entry with its key, which
    `columns_for` reads as the column named after the map's key."""
    root = layout.root_scope(label)
    return {layout.snake(root[-1]): key, **row}


def records(label: str, parsed: Parsed) -> list[Row]:
    """The records of an answer, as `store` takes them: its rows, the body itself where the
    answer is one record that has the rows as a child (`spot-account`), or one entry per key of
    a map (`asset-detail`)."""
    root = layout.root_scope(label)
    if not root:
        return [parsed.body] if isinstance(parsed.body, dict) else []
    if root[-1].startswith("<"):
        body = parsed.body if isinstance(parsed.body, dict) else {}
        return [map_row(label, key, row) for key, row in body.items() if isinstance(row, dict)]
    return parsed.rows


def _pairs(element: ApiValue, scope: tuple[str, ...]) -> list[tuple[object, ApiValue]]:
    """The elements below one element at a child scope: (position or map key, element)."""
    found: ApiValue = element
    for token in scope[:-1]:
        if not isinstance(found, dict):
            return []
        found = found.get(token)
    if scope[-1] == "[]":
        return list(enumerate(found)) if isinstance(found, list) else []
    return list(found.items()) if isinstance(found, dict) else []


async def upsert(
    ctx: CommandContext, model: type[Base], key: Mapping[str, object], values: Mapping[str, object]
) -> bool:
    """Insert the row, or update it when a value differs; True when it wrote.

    `key` must name exactly the table's primary key. Every rule the table enforces is checked
    here first: no unknown column, no NULL or missing value in a NOT NULL column, no non-finite
    number.
    """
    table = cast(Table, model.__table__)
    primary = [c.name for c in table.primary_key.columns]
    if set(key) != set(primary):
        raise ValueError(f"{table.name} is keyed by {', '.join(primary)}")
    if overlap := key.keys() & values.keys():
        raise ValueError(f"{', '.join(sorted(overlap))} is a key of {table.name}, not a value")
    row = {**key, **values}
    if unknown := row.keys() - set(table.c.keys()):
        raise ValueError(f"{table.name} has no column {', '.join(sorted(unknown))}")
    for column in table.columns:
        if row.get(column.name) is None and not column.nullable and column.server_default is None:
            raise ValueError(f"{table.name}.{column.name} may not be empty")
    for name, value in row.items():
        items = cast(list[object], value) if isinstance(value, list) else [value]
        if any(isinstance(v, Decimal) and not v.is_finite() for v in items):
            raise ValueError(f"{table.name}.{name} is not a finite number")
    statement = insert(table).values(row)
    if values:
        changed = [table.c[name].is_distinct_from(statement.excluded[name]) for name in values]
        statement = statement.on_conflict_do_update(
            index_elements=primary,
            set_={name: statement.excluded[name] for name in values},
            where=or_(*changed),
        )
    else:
        statement = statement.on_conflict_do_nothing(index_elements=primary)
    written = await ctx.session.scalar(statement.returning(table.c[primary[0]]))
    return written is not None


async def store(
    ctx: CommandContext,
    label: str,
    key: Mapping[str, object],
    row: Row,
    system: Mapping[str, object] | None = None,
) -> bool:
    """Write a record and the child rows below it; True when anything changed.

    `key` holds the root key columns the record does not carry itself: `connection_id` and
    `snapshot_at` for a report, `connection_id` for a record (with `content_hash` and
    `occurrence` when it has no id). `system` holds the other root columns the caller sets
    (`read_to`, `transaction_id`). The child rows of a record that changed are replaced: one the
    record no longer has is deleted (a report is not rewritten, so it is not pruned).
    """
    tables = layout.layout(label)
    root = tables[0]
    values = columns_for(label, row)
    full = dict(key)
    for name in root.key:
        if name not in full:
            # A field of the record, or the key of a map's entry (`map_row`).
            full[name] = values.pop(name) if name in values else row[name]
    values.update(system or {})
    changed = await upsert(ctx, model_of(root.name), full, values)
    return await _children(ctx, tables, root, full, dict(row), root.record) or changed


async def store_response(
    ctx: CommandContext,
    connection_id: UUID,
    label: str,
    body: ApiValue,
    *,
    window: tuple[datetime, datetime] | None = None,
    page: int = 0,
    read: tuple[datetime, datetime] | None = None,
) -> bool:
    """Write what a response says beside its records (`total`, `moreData`, the echoed window):
    it describes the request and the page, not a row, so it is not in the row tables.

    A history label's response is keyed by the window the walk asked for (`window`: its start
    and end) and the page's 0-based index within it; asking again updates it in place. A
    request with no time bounds passes `window=None`, stored as the epoch twice. A report's is
    keyed by the read (`read`: `read_from`, `read_to`, as for the report's rows). True when it
    wrote; False when nothing changed or the label has no such fields.
    """
    tables = [t for t in layout.layout(label) if t.response]
    if not tables:
        return False
    table = tables[0]
    if not isinstance(body, dict):
        raise _fail(label, "")
    key: dict[str, object] = {"connection_id": connection_id}
    system: dict[str, object] = {}
    if "window_from" in table.key:
        start, end = window or (_EPOCH, _EPOCH)
        key |= {"window_from": start, "window_to": end, "page": page}
    else:
        if read is None:
            raise ValueError(f"{label}'s response is keyed by the read: pass read=")
        key["snapshot_at"] = read[0]
        system["read_to"] = read[1]
    return await upsert(ctx, model_of(table.name), key, {**_values(label, table, body), **system})


async def _children(
    ctx: CommandContext,
    tables: tuple[layout.Table, ...],
    parent: layout.Table,
    parent_key: Mapping[str, object],
    element: ApiValue,
    prune: bool,
) -> bool:
    changed = False
    for table in tables:
        if table.parent is not parent:
            continue
        model = model_of(table.name)
        own = table.own_key[0]
        written: list[object] = []
        for position, item in _pairs(element, table.scope[len(parent.scope) :]):
            key = {**parent_key, own: position}
            changed |= await upsert(ctx, model, key, _values(table.label, table, item))
            written.append(position)
            changed |= await _children(ctx, tables, table, key, item, prune)
        if prune:
            changed |= await _prune(ctx, model, parent, parent_key, own, written)
    return changed


async def _prune(
    ctx: CommandContext,
    model: type[Base],
    parent: layout.Table,
    parent_key: Mapping[str, object],
    own: str,
    written: list[object],
) -> bool:
    """Delete the child rows of this parent that the record no longer has."""
    table = cast(Table, model.__table__)
    condition = [table.c[name] == parent_key[name] for name in parent.key]
    if written:
        condition.append(table.c[own].not_in(written))
    result = await ctx.session.execute(delete(table).where(*condition))
    return cast(CursorResult[Any], result).rowcount > 0
