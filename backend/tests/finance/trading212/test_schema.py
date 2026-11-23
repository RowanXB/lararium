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
    KEYS,
    MAP_ENTRIES,
    PERSONAL,
    split,
)
from lararium.modules.finance.typed_tables import column_name


async def _columns(session: AsyncSession) -> dict[str, dict[str, str]]:
    rows = await session.execute(
        text(
            "select table_name, column_name, data_type from information_schema.columns"
            " where table_schema = 'finance' and table_name like 'trading212\\_%'"
        )
    )
    found: dict[str, dict[str, str]] = {}
    for row in rows:
        found.setdefault(row.table_name, {})[row.column_name] = row.data_type
    return found


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
    assert set(found) == tables == set(KEYS)
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


async def test_column_types_follow_decision_3(session) -> None:
    found = await _columns(session)
    allowed = {"numeric", "text", "timestamp with time zone", "boolean"}
    for endpoint in COLUMNS.values():
        for table, paths in endpoint.items():
            for path, column in paths.items():
                assert found[table][column] in allowed, (table, path)
                is_time = path.endswith(("At", "On", "Date", "dateTime", ".date"))
                assert (found[table][column] == "timestamp with time zone") == is_time, path


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
