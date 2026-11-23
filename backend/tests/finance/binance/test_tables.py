"""The typed `finance.binance_*` tables: that every catalogue field has a column, that the
models are the tables the migration made, and what `columns_for`, `upsert` and `store` write."""

import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Table, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from lararium.core.connections import create_connection
from lararium.modules.finance.binance import layout
from lararium.modules.finance.binance.catalogue import ENDPOINTS, FIELDS
from lararium.modules.finance.binance.models import AssetsSeen, model_of, models_of
from lararium.modules.finance.binance.parse import BinanceDataError, parse
from lararium.modules.finance.binance.tables import (
    columns_for,
    content_hash,
    records,
    store,
    store_response,
    upsert,
)

from .fixtures import BASE_TIME_MS, build_response, build_row

MOMENT = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 11, 20, 12, 0, 5, tzinfo=UTC)


async def _connection(run) -> uuid.UUID:
    created = await run(
        lambda ctx: create_connection(ctx, connector="binance", display_name="Binance", config={})
    )
    return created.id


def _table(model: type) -> Table:
    return model.__table__  # pyright: ignore[reportAttributeAccessIssue,reportReturnType]


# The catalogue is what the tables are made from.


@pytest.mark.parametrize("label", layout.TABLE_LABELS)
def test_every_catalogue_field_has_a_column(label: str) -> None:
    """The owner's typed-column rule: no response field without a column of its own. This
    list of labels grows with each migration (`layout.TABLE_LABELS`)."""
    columns = {
        table.name: {c.name for c in _table(model).columns}
        for table, model in zip(layout.layout(label), models_of(label), strict=True)
    }
    for field in FIELDS[label]:
        if field.type == "object":
            continue  # its members are fields of their own
        if field.type == "array":
            # An array of objects: a table whose scope is the array.
            assert any(t.scope == layout.tokens(field.path) for t in layout.layout(label)), (
                label,
                field.path,
            )
            continue
        table, column = layout.locate(label, field.path)
        assert column.name in columns[table.name], (label, field.path)
        # A field beside the records is in the label's response table, never in a row's.
        beside = not layout.tokens(field.path)[
            : len(layout.root_scope(label))
        ] == layout.root_scope(label)
        assert table.response is beside, (label, field.path)
        if field.type == "time_text":
            assert f"{column.name}_at" in columns[table.name]


def test_label_with_a_table_is_a_label_of_the_catalogue() -> None:
    assert set(layout.TABLE_LABELS) <= set(ENDPOINTS)
    assert len(set(layout.TABLE_LABELS)) == len(layout.TABLE_LABELS)


def test_column_names_follow_the_field_paths() -> None:
    assert (
        layout.locate("spot-account", "commissionRates.maker")[1].name == "commission_rates_maker"
    )
    assert layout.locate("deposits", "[].completeTime")[1].name == "complete_time"
    assert layout.locate("api-trading-status", "data.triggerCondition.GCR")[1].name == (
        "data_trigger_condition_gcr"
    )
    table, column = layout.locate("exchange-info", "symbols[].orderTypes[]")
    assert (table.name, column.name, column.sql) == (
        "binance_exchange_info_symbols",
        "order_types",
        "text[]",
    )
    table, column = layout.locate("exchange-info", "symbols[].permissionSets[][]")
    assert (table.name, column.name, column.sql) == (
        "binance_exchange_info_symbols_permission_sets",
        "value",
        "text[]",
    )
    # A child field named as its parent's key takes the child's name as a prefix.
    assert layout.locate("coin-config", "[].networkList[].coin")[1].name == "network_list_coin"


def test_types_follow_the_catalogue() -> None:
    def sql(label: str, path: str) -> str:
        return layout.locate(label, path)[1].sql

    assert sql("deposits", "[].amount") == "numeric"
    assert sql("deposits", "[].id") == "text"  # an id
    assert sql("deposits", "[].status") == "bigint"  # an integer that is no id
    assert sql("deposits", "[].insertTime") == "timestamptz"
    assert sql("deposits", "[].confirmTimes") == "text"
    assert sql("withdrawals", "[].applyTime") == "text"  # text time kept as given ...
    table = layout.layout("withdrawals")[0]
    assert table.column("apply_time_at").sql == "timestamptz"  # ... with a timestamptz beside it
    assert sql("spot-account", "canTrade") == "boolean"


def test_nullable_unless_required() -> None:
    deposits = layout.layout("deposits")[0]
    nullable = {c.name: c.nullable for c in deposits.columns}
    for field in FIELDS["deposits"]:
        column = layout.locate("deposits", field.path)[1]
        assert nullable[column.name] is not field.required, field.path
    # A required field under an optional object is not required.
    status = layout.layout("api-trading-status")[0]
    assert status.column("data_update_time").nullable
    assert not layout.layout("account-status")[0].column("data").nullable


def test_keys() -> None:
    def key(label: str) -> tuple[str, ...]:
        return layout.layout(label)[0].key

    assert key("deposits") == ("connection_id", "id")
    assert key("my-trades") == ("connection_id", "symbol", "id")
    assert key("earn-flexible-subscriptions") == ("connection_id", "purchase_id")
    assert key("earn-flexible-rewards") == ("connection_id", "content_hash", "occurrence")
    assert key("snapshot") == ("connection_id", "type", "update_time")
    assert key("user-asset") == ("connection_id", "snapshot_at", "asset")
    assert key("spot-account") == ("connection_id", "snapshot_at")
    assert key("asset-detail") == ("connection_id", "snapshot_at", "asset")
    responses = {
        t.label: t for label in layout.TABLE_LABELS for t in layout.layout(label) if t.response
    }
    assert responses["dividends"].key == ("connection_id", "window_from", "window_to", "page")
    assert responses["earn-flexible-position"].key == ("connection_id", "snapshot_at")
    assert not any(c.name == "total" for c in layout.layout("dividends")[0].columns)
    balances = layout.layout("spot-account")[1]
    assert balances.key == ("connection_id", "snapshot_at", "balances_ordinal")


def test_reports_have_read_to_and_records_a_transaction() -> None:
    for label in layout.TABLE_LABELS:
        root = layout.layout(label)[0]
        names = {c.name for c in root.columns}
        if ENDPOINTS[label].kind == "history":
            assert "transaction_id" in names and "read_to" not in names, label
            assert root.column("transaction_id").nullable
        else:
            assert "read_to" in names and "snapshot_at" in names, label
            assert not root.column("read_to").nullable


def test_deposit_and_withdrawal_extras() -> None:
    for label in ("deposits", "withdrawals"):
        root = layout.layout(label)[0]
        assert (root.column("tx_hash").sql, root.column("is_onchain").sql) == ("text", "boolean")
    assert layout.layout("earn-flexible-rewards")[0].column("dividend_id").sql == "text"


# The models are what the migration made.


def _compile(column) -> str:
    return column.type.compile(dialect=postgresql.dialect()).lower()


async def test_models_are_the_migrated_tables(session) -> None:
    models = [model_of(t.name) for label in layout.TABLE_LABELS for t in layout.layout(label)]
    models.append(AssetsSeen)
    found = {
        row.relname
        for row in await session.execute(
            text(
                "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace"
                " where n.nspname = 'finance' and c.relkind = 'r' and c.relname like 'binance_%'"
            )
        )
    }
    assert found == {_table(m).name for m in models}
    for model in models:
        table = _table(model)
        rows = await session.execute(
            text(
                "select a.attname as name, format_type(a.atttypid, a.atttypmod) as type,"
                " a.attnotnull as notnull from pg_attribute a"
                " where a.attrelid = cast(:t as regclass) and a.attnum > 0 and not a.attisdropped"
            ),
            {"t": f"finance.{table.name}"},
        )
        actual = {r.name: (r.type, r.notnull) for r in rows}
        wanted = {c.name: (_compile(c), not c.nullable) for c in table.columns}
        assert actual == wanted, table.name
        primary = await session.execute(
            text(
                "select a.attname from pg_index i join pg_attribute a on a.attrelid = i.indrelid"
                " and a.attnum = any(i.indkey) where i.indrelid = cast(:t as regclass)"
                " and i.indisprimary"
            ),
            {"t": f"finance.{table.name}"},
        )
        assert {r.attname for r in primary} == {c.name for c in table.primary_key.columns}


async def test_numbers_must_be_finite_in_the_database(session, run) -> None:
    connection = await _connection(run)
    insert = (
        "insert into finance.binance_user_asset"
        " (connection_id, snapshot_at, asset, free, locked, read_to)"
        " values (:c, now(), 'AAA', cast(:v as numeric), 1, now())"
    )
    await session.execute(text(insert), {"c": connection, "v": "1.5"})
    for bad in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await session.execute(text(insert), {"c": connection, "v": bad})


async def test_record_links_to_a_transaction_that_may_go(session) -> None:
    action = await session.scalar(
        text(
            "select confdeltype from pg_constraint where contype = 'f'"
            " and conrelid = 'finance.binance_deposits'::regclass"
            " and confrelid = 'finance.transactions'::regclass"
        )
    )
    assert action == "n"  # ON DELETE SET NULL


async def test_deposit_and_withdrawal_hash_index(session) -> None:
    for table in ("binance_deposits", "binance_withdrawals"):
        definitions = (
            await session.scalars(
                text(
                    "select indexdef from pg_indexes"
                    " where schemaname = 'finance' and tablename = :t"
                ),
                {"t": table},
            )
        ).all()
        assert any("(network, tx_hash)" in d for d in definitions), table


# columns_for


def test_columns_for_converts_by_the_catalogue_type() -> None:
    row = build_row("deposits", insertTime=BASE_TIME_MS, amount="0.5", id=77)
    values = columns_for("deposits", row)
    assert values["id"] == "77"
    assert values["amount"] == Decimal("0.5")
    assert values["insert_time"] == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)
    assert values["status"] == row["status"]
    assert "connection_id" not in values and "transaction_id" not in values


def test_columns_for_keeps_a_text_time_and_adds_its_instant() -> None:
    row = build_row("withdrawals", applyTime="2023-11-14 22:13:20", completeTime=None)
    values = columns_for("withdrawals", row)
    assert values["apply_time"] == "2023-11-14 22:13:20"
    assert values["apply_time_at"] == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)
    assert values["complete_time"] is None and values["complete_time_at"] is None
    digits = columns_for("withdrawals", build_row("withdrawals", applyTime="1700000000000"))
    assert digits["apply_time_at"] == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)


def test_columns_for_reads_nested_fields_and_arrays() -> None:
    body = build_response("spot-account", [build_row("spot-account")])
    assert isinstance(body, dict)
    values = columns_for("spot-account", body)
    assert values["commission_rates_maker"] == Decimal(str(body["commissionRates"]["maker"]))  # pyright: ignore[reportIndexIssue,reportCallIssue,reportArgumentType]
    assert values["permissions"] == body["permissions"]
    status = columns_for(
        "api-trading-status",
        {
            "data": {
                "isLocked": False,
                "triggerCondition": {"GCR": 150},
                "updateTime": 1700000000000,
            }
        },
    )
    assert status["data_trigger_condition_gcr"] == 150
    assert status["data_update_time"] == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)
    assert status["data_planned_recover_time"] is None


def test_columns_for_leaves_the_envelope_out() -> None:
    body = build_response("dividends", [build_row("dividends")])
    parsed = parse("dividends", body)
    (row,) = records("dividends", parsed)
    assert "total" not in columns_for("dividends", row)


def test_columns_for_ignores_a_field_the_catalogue_does_not_list() -> None:
    row = {**build_row("deposits"), "invented": "x"}
    assert "invented" not in columns_for("deposits", row)


def test_columns_for_refuses_a_value_of_the_wrong_kind_naming_the_field() -> None:
    with pytest.raises(BinanceDataError) as error:
        columns_for("deposits", {**build_row("deposits"), "amount": "twelve"})
    assert "amount" in str(error.value) and "twelve" not in str(error.value)
    with pytest.raises(BinanceDataError):
        columns_for("deposits", {**build_row("deposits"), "amount": "NaN"})
    with pytest.raises(BinanceDataError):
        columns_for("withdrawals", build_row("withdrawals", applyTime="yesterday"))


def test_content_hash_is_stable_and_24_hex() -> None:
    row = build_row("earn-flexible-rewards")
    assert content_hash(row) == content_hash(dict(reversed(list(row.items()))))
    assert len(content_hash(row)) == 24 and int(content_hash(row), 16) >= 0
    assert content_hash(row) != content_hash({**row, "rewards": "99"})


# upsert


async def test_upsert_inserts_then_writes_only_a_change(session, run) -> None:
    connection = await _connection(run)
    model = model_of("binance_user_asset")
    key = {"connection_id": connection, "snapshot_at": MOMENT, "asset": "AAA"}
    values = {"free": Decimal("1.5"), "locked": Decimal("0"), "read_to": LATER}
    assert await run(upsert, model, key, values) is True
    assert await run(upsert, model, key, values) is False
    assert await run(upsert, model, key, {**values, "free": Decimal("2")}) is True
    free = await session.scalar(text("select free from finance.binance_user_asset"))
    assert free == Decimal("2")


async def test_upsert_checks_what_the_database_enforces(run) -> None:
    connection = await _connection(run)
    model = model_of("binance_user_asset")
    key = {"connection_id": connection, "snapshot_at": MOMENT, "asset": "AAA"}
    full = {"free": Decimal("1"), "locked": Decimal("0"), "read_to": LATER}
    with pytest.raises(ValueError, match="keyed by"):
        await run(upsert, model, {"connection_id": connection}, full)
    with pytest.raises(ValueError, match="no column"):
        await run(upsert, model, key, {**full, "invented": 1})
    with pytest.raises(ValueError, match="may not be empty"):
        await run(upsert, model, key, {"free": Decimal("1"), "read_to": LATER})
    with pytest.raises(ValueError, match="may not be empty"):
        await run(upsert, model, key, {**full, "free": None})
    with pytest.raises(ValueError, match="finite"):
        await run(upsert, model, key, {**full, "free": Decimal("NaN")})
    with pytest.raises(ValueError, match="key of"):
        await run(upsert, model, key, {**full, "asset": "BBB"})


# store


async def test_store_a_report_with_its_children_and_again(session, run) -> None:
    connection = await _connection(run)
    rows = [build_row("spot-account")]
    body = build_response("spot-account", rows)
    parsed = parse("spot-account", body)
    (record,) = records("spot-account", parsed)
    key = {"connection_id": connection, "snapshot_at": MOMENT}
    system = {"read_to": LATER}
    assert await run(store, "spot-account", key, record, system) is True
    assert await run(store, "spot-account", key, record, system) is False
    balances = await session.scalar(
        text("select count(*) from finance.binance_spot_account_balances")
    )
    assert balances == len(parsed.rows) == 1
    uid = await session.scalar(text("select uid from finance.binance_spot_account"))
    assert uid == str(parsed.body["uid"])  # pyright: ignore[reportIndexIssue,reportCallIssue,reportArgumentType]


async def test_store_a_listing_with_arrays_and_grandchildren(session, run) -> None:
    connection = await _connection(run)
    symbol = build_row(
        "exchange-info",
        permissionSets=[["SPOT", "MARGIN"], ["SPOT"]],
        filters=[{"filterType": "PRICE_FILTER"}, {"filterType": "LOT_SIZE"}],
    )
    parsed = parse("exchange-info", build_response("exchange-info", [symbol]))
    (record,) = records("exchange-info", parsed)
    key = {"connection_id": connection, "snapshot_at": MOMENT}
    await run(store, "exchange-info", key, record, {"read_to": LATER})
    sets = (
        await session.execute(
            text(
                "select permission_sets_ordinal, value from"
                " finance.binance_exchange_info_symbols_permission_sets order by 1"
            )
        )
    ).all()
    assert [(r[0], r[1]) for r in sets] == [(0, ["SPOT", "MARGIN"]), (1, ["SPOT"])]
    filters = (
        await session.scalars(
            text("select filter_type from finance.binance_exchange_info_symbols_filters order by 1")
        )
    ).all()
    assert filters == ["LOT_SIZE", "PRICE_FILTER"]
    assert await session.scalar(
        text("select count(*) from finance.binance_exchange_info_rate_limits")
    )
    assert await session.scalar(text("select count(*) from finance.binance_exchange_info_sors"))
    symbols = await session.scalar(
        text("select order_types from finance.binance_exchange_info_symbols")
    )
    assert symbols == symbol["orderTypes"]


async def test_store_a_map_keeps_the_asset_as_the_key(session, run) -> None:
    connection = await _connection(run)
    body = build_response("asset-detail", [build_row("asset-detail")])
    parsed = parse("asset-detail", body)
    found = records("asset-detail", parsed)
    assert len(found) == 1
    key = {"connection_id": connection, "snapshot_at": MOMENT}
    await run(store, "asset-detail", key, found[0], {"read_to": LATER})
    assert await session.scalar(text("select asset from finance.binance_asset_detail")) == "AAA"


async def test_store_a_record_updates_it_in_place_and_prunes_children(session, run) -> None:
    connection = await _connection(run)
    key = {"connection_id": connection}

    async def snapshot(*balances: str) -> bool:
        row = build_row("snapshot")
        row["data"] = {
            "balances": [{"asset": a, "free": "1", "locked": "0"} for a in balances],
            "totalAssetOfBtc": "1.5",
        }
        return await run(store, "snapshot", key, row)

    assert await snapshot("AAA", "BBB") is True
    assert await snapshot("AAA", "BBB") is False
    assert await snapshot("AAA") is True
    count = await session.scalar(
        text("select count(*) from finance.binance_snapshot_data_balances")
    )
    assert count == 1
    assert await session.scalar(text("select count(*) from finance.binance_snapshot")) == 1


async def test_a_record_with_no_id_is_keyed_by_hash_and_occurrence(session, run) -> None:
    connection = await _connection(run)
    row = build_row("earn-flexible-rewards")
    digest = content_hash(row)
    for occurrence in (0, 1):
        key = {"connection_id": connection, "content_hash": digest, "occurrence": occurrence}
        assert await run(store, "earn-flexible-rewards", key, row) is True
    assert (
        await session.scalar(text("select count(*) from finance.binance_earn_flexible_rewards"))
        == 2
    )


async def test_a_record_is_keyed_by_the_connection_and_its_id(session, run) -> None:
    first = await _connection(run)
    second = await _connection(run)
    row = build_row("deposits")
    for connection in (first, second):
        assert await run(store, "deposits", {"connection_id": connection}, row) is True
    assert await session.scalar(text("select count(*) from finance.binance_deposits")) == 2
    changed = {**row, "status": 1}
    assert await run(store, "deposits", {"connection_id": first}, changed) is True
    assert await session.scalar(text("select count(*) from finance.binance_deposits")) == 2
    status = await session.scalar(
        text("select status from finance.binance_deposits where connection_id = :c"), {"c": first}
    )
    assert status == 1


async def test_every_label_with_a_table_can_be_stored(session, run) -> None:
    connection = await _connection(run)
    for label in layout.TABLE_LABELS:
        record = layout.layout(label)[0].record
        parsed = parse(label, build_response(label, [build_row(label)]))
        key: dict[str, object] = {"connection_id": connection}
        system: dict[str, object] = {}
        if not record:
            key["snapshot_at"] = MOMENT
            system["read_to"] = LATER
        if "content_hash" in layout.layout(label)[0].key:
            key |= {"content_hash": content_hash(parsed.rows[0]), "occurrence": 0}
        for row in records(label, parsed):
            await run(store, label, key, row, system)
        count = await session.scalar(
            text(f"select count(*) from finance.{layout.table_name(label)}")
        )
        assert count == 1, label


# What a response says beside its records is the response's, not the row's.


def _dividends(total: int) -> tuple[dict, object]:
    row = build_row("dividends")
    body = build_response("dividends", [row])
    assert isinstance(body, dict)
    body["total"] = total
    return row, body


async def test_a_row_seen_in_another_response_is_unchanged(session, run) -> None:
    connection = await _connection(run)
    key = {"connection_id": connection}
    row, first = _dividends(1)
    assert await run(store, "dividends", key, row) is True
    window = (MOMENT, LATER)
    assert await run(store_response, connection, "dividends", first, window=window) is True
    # The same row comes back under another window and a different total.
    _, second = _dividends(40)
    later = (LATER, LATER.replace(day=21))
    assert await run(store, "dividends", key, row) is False
    assert await run(store_response, connection, "dividends", second, window=later) is True
    rows = (await session.execute(text("select * from finance.binance_dividends"))).all()
    assert len(rows) == 1 and "total" not in rows[0]._mapping
    totals = (
        await session.scalars(
            text("select total from finance.binance_dividends_responses order by window_from")
        )
    ).all()
    assert totals == [1, 40]


async def test_the_same_request_updates_its_response_in_place(session, run) -> None:
    connection = await _connection(run)
    window = (MOMENT, LATER)
    _, body = _dividends(1)
    assert await run(store_response, connection, "dividends", body, window=window) is True
    assert await run(store_response, connection, "dividends", body, window=window) is False
    _, changed = _dividends(2)
    assert await run(store_response, connection, "dividends", changed, window=window) is True
    assert await run(store_response, connection, "dividends", changed, window=window, page=1)
    count = await session.scalar(text("select count(*) from finance.binance_dividends_responses"))
    assert count == 2
    first = await session.scalar(
        text("select total from finance.binance_dividends_responses where page = 0")
    )
    assert first == 2


async def test_a_history_response_types_every_envelope_field(session, run) -> None:
    connection = await _connection(run)
    body = build_response("convert", [build_row("convert")])
    assert isinstance(body, dict)
    await run(store_response, connection, "convert", body, window=(MOMENT, LATER))
    row = (await session.execute(text("select * from finance.binance_convert_responses"))).one()
    assert row.start_time == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)
    assert row.limit == body["limit"] and row.more_data is body["moreData"]
    assert row.end_time is not None


async def test_a_report_response_is_keyed_by_the_read(session, run) -> None:
    connection = await _connection(run)
    body = build_response("earn-flexible-position", [build_row("earn-flexible-position")])
    with pytest.raises(ValueError, match="read"):
        await run(store_response, connection, "earn-flexible-position", body)
    read = (MOMENT, LATER)
    assert await run(store_response, connection, "earn-flexible-position", body, read=read)
    assert not await run(store_response, connection, "earn-flexible-position", body, read=read)
    row = (
        await session.execute(
            text("select * from finance.binance_earn_flexible_position_responses")
        )
    ).one()
    assert (row.snapshot_at, row.read_to, row.total) == (MOMENT, LATER, 1)


async def test_a_label_without_envelope_fields_has_no_response(run) -> None:
    connection = await _connection(run)
    body = build_response("deposits", [build_row("deposits")])
    assert await run(store_response, connection, "deposits", body) is False


async def _non_finite_checks(session) -> dict[str, list[str]]:
    """Each binance table's CHECK definitions, by table."""
    rows = await session.execute(
        text(
            "select conrelid::regclass::text as tbl, pg_get_constraintdef(oid) as def_"
            " from pg_constraint where contype = 'c'"
            " and conrelid::regclass::text like 'finance.binance\\_%' and "
            "pg_get_constraintdef(oid) like '%NaN%'"
        )
    )
    found: dict[str, list[str]] = {}
    for row in rows:
        found.setdefault(row.tbl.removeprefix("finance."), []).append(row.def_)
    return found


def _missing_checks(checks: dict[str, list[str]]) -> list[tuple[str, str]]:
    missing: list[tuple[str, str]] = []
    for label in layout.TABLE_LABELS:
        for table in layout.layout(label):
            for column in _table(model_of(table.name)).columns:
                if _compile(column) not in ("numeric", "numeric[]"):
                    continue
                pattern = rf"\b{column.name}\b"
                if not any(re.search(pattern, d) for d in checks.get(table.name, [])):
                    missing.append((table.name, column.name))
    return missing


async def test_every_decimal_column_refuses_non_finite_numbers(session) -> None:
    checks = await _non_finite_checks(session)
    assert checks
    assert _missing_checks(checks) == []


async def test_a_table_without_its_own_non_finite_check_is_found(session) -> None:
    # `amount` is a column of several tables: another table's check must not stand in.
    checks = await _non_finite_checks(session)
    assert any("amount" in d for d in checks["binance_dividends"])
    mutated = {
        **checks,
        "binance_deposits": [d for d in checks["binance_deposits"] if "amount" not in d],
    }
    assert _missing_checks(mutated) == [("binance_deposits", "amount")]
