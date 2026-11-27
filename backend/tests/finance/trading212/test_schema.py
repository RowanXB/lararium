"""Migration 0030 against the field lists: every known Trading 212 path has its typed column,
and the registry and instrument schemes take Trading 212's ids."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.modules.finance.errors import FinanceValidationError
from lararium.modules.finance.identifiers import normalize_identifier
from lararium.modules.finance.instruments import (
    InstrumentIdentifier,
    create_instrument,
    find_by_identifier,
)
from lararium.modules.finance.trading212.fields import (
    COLUMNS,
    ENDPOINTS,
    EXPORT_COLUMNS,
    EXPORT_KINDS,
    EXPORT_KNOWN,
    EXPORT_REQUIRED,
    EXPORT_TABLE,
    KEYS,
    KINDS,
    MAP_ENTRIES,
    NOTES_TRANSACTION_ID,
    OWN_COLUMNS,
    PERSONAL,
    split,
)
from lararium.modules.finance.typed_tables import column_name

_DATA_TYPES = {
    "numeric": "numeric",
    "text": "text",
    "timestamptz": "timestamp with time zone",
    "boolean": "boolean",
}
# The ledger row each posted row links to: (table, column) -> the table it references.
_LEDGER_LINKS = {
    ("trading212_accounts", "snapshot_id"): "finance.balance_snapshots",
    ("trading212_positions", "snapshot_id"): "finance.balance_snapshots",
    ("trading212_orders", "transaction_id"): "finance.transactions",
    ("trading212_dividends", "transaction_id"): "finance.transactions",
    ("trading212_transactions", "transaction_id"): "finance.transactions",
    (EXPORT_TABLE, "transaction_id"): "finance.transactions",
}


async def _catalog(session: AsyncSession) -> dict[str, dict[str, tuple[str, bool]]]:
    """Table -> column -> (data type, nullable) of the trading212_* tables."""
    rows = await session.execute(
        text(
            "select table_name, column_name, data_type, is_nullable"
            " from information_schema.columns"
            " where table_schema = 'finance' and table_name like 'trading212\\_%'"
        )
    )
    found: dict[str, dict[str, tuple[str, bool]]] = {}
    for row in rows:
        found.setdefault(row.table_name, {})[row.column_name] = (
            row.data_type,
            row.is_nullable == "YES",
        )
    return found


async def _columns(session: AsyncSession) -> dict[str, dict[str, str]]:
    return {
        table: {column: kind for column, (kind, _) in columns.items()}
        for table, columns in (await _catalog(session)).items()
    }


def _writers(table: str) -> int:
    return sum(table in tables for tables in COLUMNS.values())


async def test_schema_has_every_known_field(session) -> None:
    found = await _columns(session)
    missing = [
        f"{name}: {path} -> {table}.{column_name(split(spec, path)[1])}"
        for name, spec in ENDPOINTS.items()
        for path in sorted(spec.known)
        for table in [split(spec, path)[0]]
        if column_name(split(spec, path)[1]) not in found.get(table, {})
    ]
    assert missing == []
    # COLUMNS is the same rule, built through column_map.
    for name, spec in ENDPOINTS.items():
        for path in spec.known:
            table, below = split(spec, path)
            assert COLUMNS[name][table][path] == column_name(below)


async def test_every_table_has_its_key_and_references(session) -> None:
    found = await _columns(session)
    tables = {
        table for spec in ENDPOINTS.values() for table in (spec.table, *spec.children.values())
    }
    assert set(found) == tables | {EXPORT_TABLE} == set(KEYS)
    for table, columns in found.items():
        assert {"connection_id", "raw_record_id"} <= set(columns), table
    keys = await session.execute(
        text(
            "select c.conrelid::regclass::text as tbl,"
            " array(select a.attname from unnest(c.conkey) with ordinality k(n, o)"
            " join pg_attribute a on a.attrelid = c.conrelid and a.attnum = k.n"
            " order by k.o)::text[] as cols"
            " from pg_constraint c where c.contype = 'p'"
            " and c.conrelid::regclass::text like 'finance.trading212\\_%'"
        )
    )
    assert {r.tbl.removeprefix("finance."): tuple(r.cols) for r in keys} == KEYS


async def test_column_types_match_declared_kinds(session) -> None:
    found = await _columns(session)
    wrong = [
        f"{name}: {path} is {found[table][column]}, declared {KINDS[name][path]}"
        for name, tables in COLUMNS.items()
        for table, paths in tables.items()
        for path, column in paths.items()
        if found[table][column] != _DATA_TYPES[KINDS[name][path]]
    ]
    assert wrong == []


def test_kinds_follow_decision_3() -> None:
    for name, spec in ENDPOINTS.items():
        assert set(KINDS[name]) == spec.known
        for path, kind in KINDS[name].items():
            is_time = path.endswith(("At", "On", "Date", "dateTime", ".date"))
            assert (kind == "timestamptz") == is_time, path
            assert kind != "boolean" or path.endswith("extendedHours"), path


async def test_not_null_matches_required_for_single_writer_tables(session) -> None:
    found = await _catalog(session)
    wrong = [
        f"{name}: {path} nullable={found[table][column][1]}"
        for name, spec in ENDPOINTS.items()
        for table, paths in COLUMNS[name].items()
        if _writers(table) == 1
        for path, column in paths.items()
        if found[table][column][1] == (path in spec.required)
    ]
    assert wrong == []
    # The pies row is made by the list and completed by a pie's detail: only the list's
    # required id can be NOT NULL.
    assert _writers("trading212_pies") == 2
    assert not found["trading212_pies"]["id"][1]
    assert found["trading212_pies"]["settings_id"][1]


async def test_every_column_is_a_field_or_its_own(session) -> None:
    found = await _columns(session)
    fields: dict[str, set[str]] = {}
    for tables in COLUMNS.values():
        for table, paths in tables.items():
            fields.setdefault(table, set()).update(paths.values())
    fields[EXPORT_TABLE] = {*EXPORT_COLUMNS.values(), NOTES_TRANSACTION_ID}
    stray = {
        table: sorted(set(columns) - fields.get(table, set()) - OWN_COLUMNS)
        for table, columns in found.items()
    }
    assert {table: extra for table, extra in stray.items() if extra} == {}


async def test_posted_rows_link_their_ledger_row(session) -> None:
    rows = await session.execute(
        text(
            "select c.conrelid::regclass::text as tbl, a.attname as col,"
            " c.confrelid::regclass::text as target, c.confdeltype as on_delete"
            " from pg_constraint c"
            " join pg_attribute a on a.attrelid = c.conrelid and a.attnum = c.conkey[1]"
            " where c.contype = 'f' and cardinality(c.conkey) = 1"
            " and c.conrelid::regclass::text like 'finance.trading212\\_%'"
        )
    )
    links = {(r.tbl.removeprefix("finance."), r.col): (r.target, r.on_delete) for r in rows}
    for link, target in _LEDGER_LINKS.items():
        # The typed row outlives its ledger row: deleting that only unlinks it.
        assert links.get(link) == (target, "n"), link
    unique = await session.execute(
        text(
            "select c.conrelid::regclass::text as tbl, a.attname as col from pg_constraint c"
            " join pg_attribute a on a.attrelid = c.conrelid and a.attnum = c.conkey[1]"
            " where c.contype = 'u' and cardinality(c.conkey) = 1"
            " and c.conrelid::regclass::text like 'finance.trading212\\_%'"
        )
    )
    assert set(_LEDGER_LINKS) <= {(r.tbl.removeprefix("finance."), r.col) for r in unique}


def test_field_lists_are_consistent() -> None:
    assert PERSONAL == frozenset()
    for spec in ENDPOINTS.values():
        assert spec.required <= spec.known
        assert spec.record_type.startswith("trading212.")
        for child in spec.children:
            assert child.endswith(("[]", "{}"))
    for entry, fields in MAP_ENTRIES.items():
        spec = ENDPOINTS["pie"]
        assert {f"{entry}.{f}" for f in fields} <= spec.known


@pytest.mark.parametrize("spelling", ["10000001", " 10000001 "])
def test_normalize_trading212_account(spelling) -> None:
    assert normalize_identifier("trading212_account", spelling) == "10000001"


@pytest.mark.parametrize("bad", ["", "0", "01234", "12a4", "1" * 20, "-5"])
def test_normalize_trading212_account_rejects_anything_else(bad) -> None:
    with pytest.raises(FinanceValidationError, match="Trading 212 account id"):
        normalize_identifier("trading212_account", bad)


async def test_trading212_account_identifier_checked_by_database(session) -> None:
    for value in ("0", "012", "abc", "1" * 20):
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "insert into finance.account_identifiers (kind, value, created_by)"
                        " values ('trading212_account', :v, 'user')"
                    ),
                    {"v": value},
                )
    await session.execute(
        text(
            "insert into finance.account_identifiers (kind, value, created_by)"
            " values ('trading212_account', '10000001', 'user')"
        )
    )


async def test_trading212_ticker_scheme(run, session, usd) -> None:
    created = await run(
        create_instrument,
        kind="equity",
        symbol="ZZQA",
        name="Zzqa Example Corp",
        decimals=8,
        quote_instrument_id=usd.id,
        identifiers=[InstrumentIdentifier("trading212_ticker", "VZZQl_EQ")],
    )
    # Trading 212's tickers keep their case (LSE listings end in a lower-case l).
    found = await find_by_identifier(session, "trading212_ticker", "VZZQl_EQ")
    assert found is not None and found.id == created.id


async def test_export_table_has_every_known_column(session) -> None:
    """Migration 0034 against the export's columns (Decision 3): one typed column each, NOT
    NULL when required, plus the Notes' card transaction id."""
    found = (await _catalog(session))[EXPORT_TABLE]
    assert EXPORT_KNOWN == set(EXPORT_COLUMNS) and set(EXPORT_REQUIRED) <= EXPORT_KNOWN
    for header, column in EXPORT_COLUMNS.items():
        assert column == column_name(header)
        kind, nullable = found[column]
        assert kind == _DATA_TYPES[EXPORT_KINDS[header]], header
        assert nullable == (header not in EXPORT_REQUIRED), header
    assert found[NOTES_TRANSACTION_ID] == ("text", True)
    assert found["notes_sort_code"] == ("text", True)
    assert found["notes_account_number"] == ("text", True)
    assert found["transaction_id"] == ("uuid", True)
    assert EXPORT_COLUMNS["Time (UTC)"] == "time_utc"
    assert EXPORT_COLUMNS["Currency (Total)"] == "currency_total"
