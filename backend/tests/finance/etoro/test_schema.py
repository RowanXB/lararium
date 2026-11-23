"""Migration 0031 against the field lists: every known eToro path and statement column has its
typed column, the registry takes eToro's account ids, and the review, decision and leg-fee
tables hold their rules."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.connections import create_connection
from lararium.modules.finance.errors import FinanceValidationError
from lararium.modules.finance.etoro.fields import (
    COLUMNS,
    ENDPOINTS,
    KEYS,
    KINDS,
    OWN_COLUMNS,
    SCALAR_ITEMS,
    SECOND_VALUE,
    STATEMENT_COLUMNS,
    STATEMENT_KINDS,
    STATEMENT_SHEETS,
    STATEMENT_TABLES,
    SUMMARY_KINDS,
    split,
)
from lararium.modules.finance.identifiers import normalize_identifier
from lararium.modules.finance.typed_tables import column_name

_DATA_TYPES = {
    "numeric": "numeric",
    "text": "text",
    "timestamptz": "timestamp with time zone",
    "timestamp": "timestamp without time zone",
    "date": "date",
    "boolean": "boolean",
}
# The finance row each typed row links to: (table, column) -> (target, unique).
_LINKS = {
    ("etoro_cash_transactions", "transaction_id"): ("finance.transactions", True),
    ("etoro_trades", "transaction_id"): ("finance.transactions", True),
    ("etoro_position_executions", "transaction_id"): ("finance.transactions", True),
    ("etoro_activity", "transaction_id"): ("finance.transactions", False),
    ("etoro_dividends", "transaction_id"): ("finance.transactions", False),
    ("etoro_aggregate_instruments", "snapshot_id"): ("finance.balance_snapshots", True),
    ("etoro_balance_accounts", "snapshot_id"): ("finance.balance_snapshots", True),
    ("etoro_balance_account_snapshots", "snapshot_id"): ("finance.balance_snapshots", True),
}


async def _catalog(session: AsyncSession) -> dict[str, dict[str, tuple[str, bool]]]:
    """Table -> column -> (data type, nullable) of the etoro_* tables."""
    rows = await session.execute(
        text(
            "select table_name, column_name, data_type, is_nullable"
            " from information_schema.columns"
            " where table_schema = 'finance' and table_name like 'etoro\\_%'"
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
    return sum(table in tables for tables in COLUMNS.values()) + (table in STATEMENT_COLUMNS)


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
    for name, spec in ENDPOINTS.items():
        for path in spec.known:
            table, below = split(spec, path)
            assert COLUMNS[name][table][path] == column_name(below)

    # Every column of the statement's table sheets, and every label of Account Summary.
    for sheet, (table, prefix) in STATEMENT_TABLES.items():
        assert set(STATEMENT_KINDS[sheet]) == STATEMENT_SHEETS[sheet].known
        for header, kind in STATEMENT_KINDS[sheet].items():
            column = prefix + column_name(header)
            assert STATEMENT_COLUMNS[table][header] == column
            assert found[table].get(column) == _DATA_TYPES[kind], f"{sheet}.{header}"
    for label, kind in SUMMARY_KINDS.items():
        column = column_name(label)
        assert found["etoro_statements"].get(column) == _DATA_TYPES[kind], label
        assert found["etoro_statements"].get(column + SECOND_VALUE) == "text", label


async def test_every_table_has_its_key(session) -> None:
    found = await _columns(session)
    tables = {
        table for spec in ENDPOINTS.values() for table in (spec.table, *spec.children.values())
    }
    tables |= set(STATEMENT_COLUMNS) | {"etoro_financial_summary", "etoro_financial_summary_values"}
    assert set(found) == tables == set(KEYS)
    for table, columns in found.items():
        assert "connection_id" in columns, table
        assert {"raw_record_id", "document_id"} & set(columns), table
    keys = await session.execute(
        text(
            "select c.conrelid::regclass::text as tbl,"
            " array(select a.attname from unnest(c.conkey) with ordinality k(n, o)"
            " join pg_attribute a on a.attrelid = c.conrelid and a.attnum = k.n"
            " order by k.o)::text[] as cols"
            " from pg_constraint c where c.contype = 'p'"
            " and c.conrelid::regclass::text like 'finance.etoro\\_%'"
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


async def test_not_null_matches_required_for_single_writer_tables(session) -> None:
    found = await _catalog(session)
    wrong = [
        f"{name}: {path} nullable={found[table][column][1]}"
        for name, spec in ENDPOINTS.items()
        for table, paths in COLUMNS[name].items()
        if _writers(table) == 1
        for path, column in paths.items()
        # An item of a list of plain values is never null.
        if found[table][column][1] == (path in spec.required or path in SCALAR_ITEMS)
    ]
    assert wrong == []
    # A close comes from trade history, the statement or both: neither's fields are required.
    assert _writers("etoro_trades") == 2
    assert found["etoro_trades"]["position_id"][1]
    assert found["etoro_trades"]["raw_record_id"][1]


async def test_every_column_is_a_field_or_its_own(session) -> None:
    found = await _columns(session)
    fields: dict[str, set[str]] = {}
    for tables in COLUMNS.values():
        for table, paths in tables.items():
            fields.setdefault(table, set()).update(paths.values())
    for table, mapping in STATEMENT_COLUMNS.items():
        fields.setdefault(table, set()).update(mapping.values())
    fields.setdefault("etoro_financial_summary", set()).add("name")
    fields.setdefault("etoro_order_positions_to_close", set()).add("position_id")
    stray = {
        table: sorted(set(columns) - fields.get(table, set()) - OWN_COLUMNS)
        for table, columns in found.items()
    }
    assert {table: extra for table, extra in stray.items() if extra} == {}


async def test_links_to_finance_rows(session) -> None:
    rows = await session.execute(
        text(
            "select c.conrelid::regclass::text as tbl, a.attname as col,"
            " c.confrelid::regclass::text as target, c.confdeltype as on_delete"
            " from pg_constraint c"
            " join pg_attribute a on a.attrelid = c.conrelid and a.attnum = c.conkey[1]"
            " where c.contype = 'f' and cardinality(c.conkey) = 1"
            " and c.conrelid::regclass::text like 'finance.etoro\\_%'"
        )
    )
    links = {(r.tbl.removeprefix("finance."), r.col): (r.target, r.on_delete) for r in rows}
    for link, (target, _) in _LINKS.items():
        # The typed row outlives its ledger row: deleting that only unlinks it.
        assert links.get(link) == (target, "n"), link
    # eToro's accountId and instrumentId are fields; finance's rows are linked by other names.
    for (table, column), (target, _) in links.items():
        if column == "finance_account_id":
            assert target == "finance.accounts", table
        if column == "finance_instrument_id":
            assert target == "finance.instruments", table
        assert column not in ("account_id", "instrument_id"), table
    unique = await session.execute(
        text(
            "select c.conrelid::regclass::text as tbl, a.attname as col from pg_constraint c"
            " join pg_attribute a on a.attrelid = c.conrelid and a.attnum = c.conkey[1]"
            " where c.contype = 'u' and cardinality(c.conkey) = 1"
            " and c.conrelid::regclass::text like 'finance.etoro\\_%'"
        )
    )
    unique_links = {(r.tbl.removeprefix("finance."), r.col) for r in unique}
    assert {link for link, (_, one) in _LINKS.items() if one} <= unique_links
    assert not {link for link, (_, one) in _LINKS.items() if not one} & unique_links


@pytest.mark.parametrize(
    ("spelling", "stored"),
    [
        ("12345678", "12345678"),
        (" 1234567 ", "1234567"),
        ("0F9E8D7C-1111-4222-8333-944455556666", "0f9e8d7c-1111-4222-8333-944455556666"),
        (" 0f9e8d7c-1111-4222-8333-944455556666", "0f9e8d7c-1111-4222-8333-944455556666"),
    ],
)
def test_normalize_etoro_account(spelling: str, stored: str) -> None:
    assert normalize_identifier("etoro_account", spelling) == stored


@pytest.mark.parametrize(
    "bad",
    ["", "12a4", "1" * 20, "-5", "0f9e8d7c-1111-4222-8333", "0f9e8d7c11114222833394445555666"],
)
def test_normalize_etoro_account_rejects_anything_else(bad: str) -> None:
    with pytest.raises(FinanceValidationError, match="eToro account id"):
        normalize_identifier("etoro_account", bad)


async def test_etoro_account_identifier_checked_by_database(session) -> None:
    for value in ("abc", "1" * 20, "0F9E8D7C-1111-4222-8333-944455556666"):
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "insert into finance.account_identifiers (kind, value, created_by)"
                        " values ('etoro_account', :v, 'user')"
                    ),
                    {"v": value},
                )
    for value in ("12345678", "0f9e8d7c-1111-4222-8333-944455556666"):
        await session.execute(
            text(
                "insert into finance.account_identifiers (kind, value, created_by)"
                " values ('etoro_account', :v, 'user')"
            ),
            {"v": value},
        )


async def _connection_id(run) -> uuid.UUID:
    connection = await run(create_connection, connector="etoro", display_name="eToro", config={})
    return connection.id


async def test_one_open_review_per_event_and_field(session, run) -> None:
    connection_id = await _connection_id(run)
    insert = text(
        "insert into finance.source_reviews"
        " (connection_id, event_key, field, api_value, statement_value)"
        " values (:c, 'close:3000000001', 'units', '4', '5')"
    )
    await session.execute(insert, {"c": connection_id})
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(insert, {"c": connection_id})
    await session.execute(
        text(
            "update finance.source_reviews set resolved_at = now(), resolution = 'api',"
            " resolved_by = 'user'"
        )
    )
    # Resolved, it leaves room for a new open item.
    await session.execute(insert, {"c": connection_id})
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                text(
                    "update finance.source_reviews set resolution = 'both'"
                    " where resolved_at is not null"
                )
            )


async def test_leg_fee_is_not_negative(session, usd) -> None:
    # The CHECK fires before the foreign key would.
    with pytest.raises(IntegrityError, match="transfer_leg_fees_amount_check"):
        async with session.begin_nested():
            await session.execute(
                text(
                    "insert into finance.transfer_leg_fees (transaction_id, instrument_id, amount)"
                    " values (uuidv7(), :i, -1)"
                ),
                {"i": usd.id},
            )
