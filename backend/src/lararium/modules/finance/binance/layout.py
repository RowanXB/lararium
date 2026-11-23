"""The tables of the `finance.binance_*` schema, derived from the catalogue by one rule
(plan Decision 3), so the models, `columns_for` and `test_every_catalogue_field_has_a_column`
cannot drift from each other.

The migrations are frozen SQL: they never import this module. A migration's `CREATE TABLE`
statements are emitted once from here (`tests/finance/binance/emit_ddl.py`) and pasted in; a
test holds the models, which are built from this layout, to what the migrations created.

The rule, in short:

- A label's records are in its *root scope*: the elements of its `rows` array for a history or
  position label (what is outside them, `total` or `moreData`, is stored with each row), the
  elements of a body that is an array, the entries of a body that is a map of objects, else the
  body itself (a balance, public or account label whose array is a child table).
- Below the root, every array of objects and every map is a child table `<parent>_<path>`,
  keyed by the parent's key and the element's position (or the map's key). An array of scalars
  is a `text[]`/`numeric[]` column; an array of arrays is a child table with a `value` array.
- A column is the field's path in snake case; its type follows the field's type.
- A history label is a table of records (nullable `transaction_id`, keyed by the record's id, or
  by a hash of its content and its occurrence); any other label is a report, keyed by
  `snapshot_at` and the row's id, with the `read_to` of the read.
"""

import re
from dataclasses import dataclass, replace
from functools import cache

from lararium.modules.finance.binance.catalogue import ENDPOINTS, FIELDS, Field

# The labels whose tables exist; each migration that adds tables extends it.
TABLE_LABELS: tuple[str, ...] = (
    "exchange-info",
    "api-restrictions",
    "account-info",
    "account-status",
    "api-trading-status",
    "spot-account",
    "user-asset",
    "funding-asset",
    "wallet-balance",
    "earn-account",
    "earn-flexible-position",
    "snapshot",
    "coin-config",
    "asset-detail",
    "deposits",
    "withdrawals",
    "convert",
    "dividends",
    "earn-flexible-subscriptions",
    "earn-flexible-redemptions",
    "earn-flexible-rewards",
    "my-trades",
    "klines",
)

# Columns a record's table has beyond the catalogue's fields (design 5.12).
EXTRA_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "deposits": (("tx_hash", "text"), ("is_onchain", "boolean")),
    "withdrawals": (("tx_hash", "text"), ("is_onchain", "boolean")),
    # The key of the dividend a reward is linked to (rule 3).
    "earn-flexible-rewards": (("dividend_id", "text"),),
}
INDEXES: dict[str, tuple[tuple[str, ...], ...]] = {
    "deposits": (("network", "tx_hash"),),
    "withdrawals": (("network", "tx_hash"),),
}

# A bar of `klines` is positional, so its columns are named by what each position holds
# (the catalogue's meaning column); the table is market data, with no connection.
BAR_COLUMNS = {
    "0": "open_time",
    "1": "open",
    "2": "high",
    "3": "low",
    "4": "close",
    "5": "volume",
    "6": "close_time",
    "7": "quote_volume",
    "8": "trades",
    "9": "taker_buy_base_volume",
    "10": "taker_buy_quote_volume",
    "11": "unused",
}
BARS_LABEL = "klines"

CONTENT_HASH = "content_hash"
OCCURRENCE = "occurrence"
_MAX_NAME = 63  # the longest a Postgres identifier can be
_RESERVED = frozenset({"metadata", "registry"})
_TOKEN = re.compile(r"<[^<>]*>|\[\]|[^.\[\]<>]+")
_SQL = {
    "decimal": "numeric",
    "integer": "bigint",
    "id": "text",
    "boolean": "boolean",
    "time_ms": "timestamptz",
    "time_text": "text",
    "text": "text",
}


@dataclass(frozen=True)
class Column:
    name: str
    sql: str
    nullable: bool
    # The catalogue field this column stores and its catalogue type (which converts it).
    path: str | None = None
    type: str | None = None
    # The object keys to follow from the scope's element to the value (none: the element is it).
    reads: tuple[str, ...] = ()
    # The timestamptz twin of a `time_text` column.
    twin: bool = False


@dataclass(frozen=True)
class Table:
    name: str
    label: str
    scope: tuple[str, ...]
    parent: "Table | None"
    key: tuple[str, ...]
    columns: tuple[Column, ...]
    # A table of records (it has `transaction_id`), not of reports.
    record: bool
    # The columns this table adds to its parent's key.
    own_key: tuple[str, ...] = ()
    # The table of what a response says beside its records (`total`, `moreData`).
    response: bool = False

    def column(self, name: str) -> Column:
        return next(c for c in self.columns if c.name == name)


def tokens(path: str) -> tuple[str, ...]:
    return tuple(_TOKEN.findall(path))


def snake(name: str) -> str:
    name = name.strip("<>")
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name).lower()


def table_name(label: str) -> str:
    return "binance_" + label.replace("-", "_")


def _is_opener(token: str) -> bool:
    return token == "[]" or token.startswith("<")


def _join(parts: tuple[str, ...]) -> str:
    out = ""
    for part in parts:
        if part == "[]":
            out += part
        else:
            out += part if not out else f".{part}"
    return out


def root_scope(label: str) -> tuple[str, ...]:
    """The tokens up to the element that is one record of the label."""
    endpoint = ENDPOINTS[label]
    if endpoint.rows == ".":
        return ("[]",)
    if endpoint.rows == "":
        firsts = {tokens(f.path)[0] for f in FIELDS[label]}
        if len(firsts) == 1 and (only := next(iter(firsts))).startswith("<"):
            return (only,)
        return ()
    if endpoint.kind in ("history", "position"):
        return (*endpoint.rows.split("."), "[]")
    return ()


@dataclass(frozen=True)
class _Place:
    field: Field
    scope: tuple[str, ...]
    reads: tuple[str, ...]
    array: bool
    envelope: bool


def _place(field: Field, root: tuple[str, ...]) -> _Place:
    t = tokens(field.path)
    trailing = len(t) - len(_strip_trailing(t))
    body = _strip_trailing(t)
    if trailing == 2:
        # An array of arrays: the outer array is the scope, its elements are the values.
        return _Place(field, (*body, "[]"), (), True, False)
    if trailing == 0 and t[-1].startswith("<"):
        return _Place(field, t, (), False, False)
    openers = [i for i, token in enumerate(body) if _is_opener(token)]
    scope = body[: openers[-1] + 1] if openers else ()
    if scope[: len(root)] != root:
        # Beside the records (`total`): stored on each of them.
        if openers:
            raise ValueError(f"{field.path} is beside the records but has a child table")
        return _Place(field, root, tuple(body), trailing == 1, True)
    return _Place(field, scope, tuple(body[len(scope) :]), trailing == 1, False)


def _strip_trailing(t: tuple[str, ...]) -> tuple[str, ...]:
    end = len(t)
    while end > 0 and t[end - 1] == "[]":
        end -= 1
    return t[:end]


def _field_columns(
    places: list[_Place], objects: dict[str, bool], taken: frozenset[str], prefix: str
) -> list[Column]:
    """The columns of the fields in one scope. A field named as a key column inherited from the
    parent (a network's `coin` under the coin) takes the scope's name as a prefix."""
    columns: list[Column] = []
    for place in places:
        field = place.field
        name = "_".join(snake(n) for n in place.reads) or "value"
        if name in taken:
            name = f"{prefix}_{name}"
        # Required only when every object above it is: `data.isLocked` of an optional `data`.
        parents = [field.path[:i] for i, c in enumerate(field.path) if c == "."]
        required = (
            field.required and not place.envelope and all(objects.get(p, True) for p in parents)
        )
        sql = _SQL[field.type] + ("[]" if place.array else "")
        nullable = not required or place.array
        columns.append(
            Column(
                name,
                sql,
                nullable,
                path=field.path,
                type=field.type,
                reads=place.reads,
            )
        )
        if field.type == "time_text":
            twin = f"{name}_at"
            columns.append(
                Column(
                    twin,
                    "timestamptz",
                    nullable,
                    path=field.path,
                    type=field.type,
                    reads=place.reads,
                    twin=True,
                )
            )
    return columns


@cache
def layout(label: str) -> tuple[Table, ...]:
    """The tables of a label, the root first and each parent before its children."""
    if label == BARS_LABEL:
        return (_bars_table(),)
    endpoint = ENDPOINTS[label]
    record = endpoint.kind == "history"
    root = root_scope(label)
    objects = {f.path: f.required for f in FIELDS[label] if f.type == "object"}
    places = [_place(f, root) for f in FIELDS[label] if f.type not in ("object", "array")]

    scopes = {root}
    for place in places:
        scopes.update(
            place.scope[: i + 1]
            for i, token in enumerate(place.scope)
            if _is_opener(token) and i >= len(root)
        )
    ordered = sorted(scopes, key=lambda s: (len(s), s))

    built: dict[tuple[str, ...], Table] = {}
    for scope in ordered:
        mine = [p for p in places if p.scope == scope and not p.envelope]
        if scope == root:
            table = _root_table(label, record, root, mine, objects, endpoint.id_fields)
        else:
            parent = built[max((s for s in built if scope[: len(s)] == s), key=len)]
            table = _child_table(label, scope, parent, mine, objects)
        names = [c.name for c in table.columns]
        if dupes := sorted({n for n in names if names.count(n) > 1}):
            raise ValueError(f"{table.name} has columns named twice: {dupes}")
        if bad := _RESERVED & set(names):
            raise ValueError(f"{table.name}: {sorted(bad)} cannot be column names")
        if len(table.name) > _MAX_NAME:
            raise ValueError(f"{table.name} is too long for a table name")
        built[scope] = table
    tables = [built[scope] for scope in ordered]
    if envelope := [p for p in places if p.envelope]:
        tables.append(_response_table(label, record, envelope, objects))
    return tuple(tables)


def _bars_table() -> Table:
    """The daily bars of one symbol: its name and one column per position of a bar, keyed by
    the symbol and the open time. Bars are market data shared by every connection."""
    root = root_scope(BARS_LABEL)
    columns = [Column("symbol", "text", False)]
    for field in FIELDS[BARS_LABEL]:
        place = _place(field, root)
        (column,) = _field_columns([place], {}, frozenset(), "")
        columns.append(replace(column, name=BAR_COLUMNS[place.reads[0]]))
    return Table(
        name=table_name(BARS_LABEL),
        label=BARS_LABEL,
        scope=root,
        parent=None,
        key=("symbol", "open_time"),
        columns=tuple(columns),
        record=False,
    )


def _response_table(
    label: str, record: bool, places: list[_Place], objects: dict[str, bool]
) -> Table:
    """What a response says beside its records, which describes the request and the page, not
    a row: one row per request (a history label's window and page; a report's read)."""
    head = [Column("connection_id", "uuid", False)]
    if record:
        head += [
            Column("window_from", "timestamptz", False),
            Column("window_to", "timestamptz", False),
            Column("page", "integer", False),
        ]
    else:
        head += [
            Column("snapshot_at", "timestamptz", False),
            Column("read_to", "timestamptz", False),
        ]
    key = tuple(c.name for c in head[: 4 if record else 2])
    return Table(
        name=f"{table_name(label)}_responses",
        label=label,
        scope=(),
        parent=None,
        key=key,
        columns=tuple([*head, *_field_columns(places, objects, frozenset(), "")]),
        record=False,
        response=True,
    )


def _root_table(
    label: str,
    record: bool,
    root: tuple[str, ...],
    mine: list[_Place],
    objects: dict[str, bool],
    id_fields: tuple[str, ...],
) -> Table:
    head = [Column("connection_id", "uuid", False)]
    if not record:
        head.append(Column("snapshot_at", "timestamptz", False))
    if root and root[-1].startswith("<"):
        head.append(Column(snake(root[-1]), "text", False))
    key = [c.name for c in head]
    ids = [_join((*root, *tokens(name))) for name in id_fields] if root else []
    if root and not root[-1].startswith("<") and record and not id_fields:
        head += [Column(CONTENT_HASH, "text", False), Column(OCCURRENCE, "integer", False)]
        key += [CONTENT_HASH, OCCURRENCE]
    fields = _field_columns(mine, objects, frozenset(), "")
    for path in ids:
        found = next((c for c in fields if c.path == path and not c.twin), None)
        if found is None:
            raise ValueError(f"{label}: no field {path} to key the rows by")
        key.append(found.name)
    fields = [replace(c, nullable=False) if c.name in key else c for c in fields]
    system = (
        [Column("transaction_id", "uuid", True)]
        if record
        else [Column("read_to", "timestamptz", False)]
    )
    extras = [Column(name, sql, True) for name, sql in EXTRA_COLUMNS.get(label, ())]
    return Table(
        name=table_name(label),
        label=label,
        scope=root,
        parent=None,
        key=tuple(key),
        columns=tuple([*head, *system, *fields, *extras]),
        record=record,
    )


def _child_table(
    label: str,
    scope: tuple[str, ...],
    parent: Table,
    mine: list[_Place],
    objects: dict[str, bool],
) -> Table:
    between = [snake(t) for t in scope[len(parent.scope) :] if not _is_opener(t)]
    suffix = "_".join(between)
    inherited = [Column(k, parent.column(k).sql, False) for k in parent.key]
    if scope[-1] == "[]":
        own = f"{suffix}_ordinal" if suffix else "ordinal"
        added = Column(own, "integer", False)
    else:
        own = snake(scope[-1])
        added = Column(own, "text", False)
    fields = _field_columns(mine, objects, frozenset(c.name for c in inherited), suffix)
    return Table(
        name=f"{parent.name}_{suffix}",
        label=label,
        scope=scope,
        parent=parent,
        key=(*parent.key, own),
        columns=tuple([*inherited, added, *fields]),
        record=False,
        own_key=(own,),
    )


def locate(label: str, path: str) -> tuple[Table, Column]:
    """The table and column that store the catalogue field at `path` (the text column of a
    `time_text`)."""
    for table in layout(label):
        for column in table.columns:
            if column.path == path and not column.twin:
                return table, column
    raise KeyError(path)
