"""Typed tables shared by the connectors: a source's fields stored one typed column each.

A column's name is the snake_case of the field's path (`column_name`). `typed_values` reads each
column's type from the catalog and converts a row's values to it, so a new column needs no change
here; `upsert_typed` writes the row only when it differs. Errors name the field and never its
value, which may be the owner's data.
"""

import re
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext
from lararium.core.errors import DisplayableError

type Fields = Mapping[str, object]
type Value = str | Decimal | bool | None

_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NOT_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")
_TEXT_TYPES = frozenset({"text", "character varying", "character"})
_TIMESTAMP_TYPES = frozenset({"timestamp with time zone", "timestamp without time zone"})


class TypedValueError(DisplayableError, ValueError):
    """A value does not fit its column's type; the message names the field only."""


def column_name(path: str) -> str:
    """The column for a field: a dotted JSON path (`walletImpact.fxRate`) or a statement header
    (`Spread Fees (USD)`) in snake_case, every run of other characters one underscore."""
    return _NOT_ALPHANUMERIC.sub("_", _CAMEL.sub("_", path).lower()).strip("_")


def column_map(paths: Iterable[str], name: Callable[[str], str] = column_name) -> dict[str, str]:
    """Path to column for a source's known fields. `column_name` is lossy, so two distinct paths
    that give one column raise, naming both: the second would overwrite the first. `name` is the
    caller's own rule when it has one."""
    owner: dict[str, str] = {}
    columns: dict[str, str] = {}
    for path in paths:
        column = name(path)
        if not column:
            raise ValueError(f"{path!r} has no column name")
        if owner.setdefault(column, path) != path:
            raise ValueError(f"{owner[column]!r} and {path!r} are both column {column}")
        columns[path] = column
    return columns


def parse_date(value: str, label: str, separators: str = ";T ") -> date:
    """An ISO date (`yyyy-mm-dd` or `yyyymmdd`); a date-time keeps its date, the time starting at
    any of `separators` (a caller that knows its source's one separator passes just that)."""
    try:
        return date.fromisoformat(re.split(f"[{re.escape(separators)}]", value.strip())[0])
    except ValueError:
        raise TypedValueError(f"{label} is not a date") from None


def parse_moment(value: str, label: str) -> datetime:
    """An ISO 8601 date-time; aware when it carries an offset or `Z`, else naive."""
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        raise TypedValueError(f"{label} is not a date-time") from None


def parse_decimal(value: str, label: str) -> Decimal:
    try:
        number = Decimal(value.strip())
    except InvalidOperation:
        raise TypedValueError(f"{label} is not a number") from None
    if not number.is_finite():
        raise TypedValueError(f"{label} is not a number")
    return number


_column_types: dict[str, dict[str, str]] = {}


async def column_types(session: AsyncSession, table: str) -> dict[str, str]:
    """Column name to data type of a table, read once per process (the schema is fixed)."""
    if table not in _column_types:
        rows = await session.execute(
            text(
                "select column_name, data_type from information_schema.columns"
                " where table_schema = 'finance' and table_name = :t"
            ),
            {"t": table},
        )
        found = {row.column_name: row.data_type for row in rows}
        if not found:
            raise ValueError(f"no table finance.{table}")
        _column_types[table] = found
    return _column_types[table]


def _convert(kind: str, value: str | Decimal | bool, label: str) -> object:
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        raise TypedValueError(f"{label} is not true or false")
    if isinstance(value, bool):
        raise TypedValueError(f"{label} is a boolean, not what its column holds")
    if kind == "numeric":
        return value if isinstance(value, Decimal) else parse_decimal(value, label)
    text_value = value if isinstance(value, str) else str(value)
    if kind in _TEXT_TYPES:
        return text_value
    if kind == "date":
        return parse_date(text_value, label)
    if kind in _TIMESTAMP_TYPES:
        moment = parse_moment(text_value, label)
        if (kind == "timestamp with time zone") != (moment.tzinfo is not None):
            raise TypedValueError(
                f"{label} has no time zone"
                if kind == "timestamp with time zone"
                else f"{label} has a time zone, its column has none"
            )
        return moment
    raise TypedValueError(f"{label} has a column type this does not convert to")


async def typed_values(
    session: AsyncSession,
    table: str,
    values: Mapping[str, Value],
    labels: Mapping[str, str] | None = None,
) -> Fields:
    """The values that have a column in the table (keyed by column name), as the column's type.

    An empty string is NULL. Names without a column and columns of type uuid (the caller's
    references) are left out. `labels` names a column's field in errors (default: the column).
    """
    types = await column_types(session, table)
    converted: dict[str, object] = {}
    for column, value in values.items():
        kind = types.get(column)
        if kind is None or kind == "uuid":
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            converted[column] = None
        else:
            converted[column] = _convert(kind, value, (labels or {}).get(column, column))
    return converted


def _quote(name: str) -> str:
    if _IDENTIFIER.fullmatch(name) is None:
        raise ValueError(f"not a column name: {name!r}")
    return f'"{name}"'


async def upsert_typed(ctx: CommandContext, table: str, key: Fields, values: Fields) -> bool:
    """Insert the row, or update it when it differs; True when it wrote. `key` names the
    table's unique columns."""
    if not key:
        raise ValueError(f"{table} needs a key")
    if overlap := key.keys() & values.keys():
        raise ValueError(f"{', '.join(sorted(overlap))} is a key of {table}, not a value")
    row = {**key, **values}
    quoted = _quote(table)
    sql = (
        f"insert into finance.{quoted} ({', '.join(_quote(name) for name in row)})"
        f" values ({', '.join(f':p{n}' for n in range(len(row)))})"
        f" on conflict ({', '.join(_quote(c) for c in key)})"
    )
    if values:
        assignments = [_quote(name) for name in values]
        sql += (
            f" do update set {', '.join(f'{a} = excluded.{a}' for a in assignments)}"
            f" where ({', '.join(f'{quoted}.{a}' for a in assignments)})"
            f" is distinct from ({', '.join(f'excluded.{a}' for a in assignments)})"
        )
    else:
        sql += " do nothing"
    written = await ctx.session.scalar(
        text(sql + " returning 1"), {f"p{n}": value for n, value in enumerate(row.values())}
    )
    return written is not None
