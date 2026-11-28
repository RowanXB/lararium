"""Migration 0041 goes down to 0040 and up again, taking what it added and leaving the rest;
migration 0042 (`never_transfer`) likewise down to 0041, 0043 (the reconciliation
differences and the account report times) down to 0042, and 0044 (the daily bars and the
CoinGecko ids) down to 0043. Each table a migration creates is checked on its own, and each
downgrade takes from a Binance connection's config what says what is in what it drops."""

import json
import re
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text

BACKEND = Path(__file__).parents[3]
VERSIONS = BACKEND / "migrations" / "versions"


def _created(revision: str) -> list[str]:
    """The tables (in `finance`) the migration creates, in its order."""
    [path] = VERSIONS.glob(f"{revision}_*.py")
    return re.findall(r"CREATE TABLE finance\.(\w+)", path.read_text())


def _present(connection: Connection, tables: list[str]) -> dict[str, bool]:
    return {
        table: connection.execute(
            text("select to_regclass(:name) is not null"), {"name": f"finance.{table}"}
        ).scalar()
        is True
        for table in tables
    }


def _all(tables: list[str], present: bool) -> dict[str, bool]:
    return dict.fromkeys(tables, present)


def _config(owner_url: str) -> Config:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", owner_url)
    return config


# Every key a Binance connection's config holds (sync.py lists them). A downgrade removes those
# that say what is in a table (or column) it drops, and only from a Binance connection.
_CONFIG: dict[str, object] = {
    "key_type": "hmac",
    "uid": 1,
    "account_map": {"spot": "00000000-0000-0000-0000-000000000001"},
    "cursors": {"deposits": {"fetched_to": 1, "backfilled_to": 1, "complete": True}},
    "not_present": {"dividends": 1},
    "first_sync_done": True,
    "normalized_through": "00000000-0000-0000-0000-000000000002",
    "normalizer_version": "binance-norm-2",
    "flags": ["AAA flag"],
    "report_digests": {"coin-config": "digest"},
    "swept": [["AAA", 1]],
    "reconciled_through": 1,
    "touched_pending": {"a:i": [1, 2]},
    "pending_flags": ["AAA flag"],
    "pending_lines": ["line"],
    "bars_asked": {"AAABBB": ["2026-01-01", "2026-01-02"]},
    "coingecko_ids_asked": {"at_ms": 1, "ok": True},
    "sync_requested": True,
}
# What each downgrade removes, by the revision it goes down to. Every one passes 0048's, which
# takes the replay marker from Binance and Trading 212 alike (their next sync replays).
_REPLAY = {"normalizer_version"}
_REMOVED = {
    "0043": {"bars_asked", "coingecko_ids_asked"},
    "0042": {"bars_asked", "coingecko_ids_asked", "reconciled_through"},
    "0041": {"bars_asked", "coingecko_ids_asked", "reconciled_through", "normalized_through"},
    "0040": {
        "bars_asked",
        "coingecko_ids_asked",
        "reconciled_through",
        "normalized_through",
        "report_digests",
    },
}


def _seed(connection: Connection) -> tuple[object, object, object]:
    """A Binance connection and a Trading 212 one, which no downgrade from 0040 on touches but
    for 0048's replay marker, each with every key; (owner, binance, other)."""
    owner = connection.execute(
        text("insert into core.entities (kind, name) values ('person', 'Config test') returning id")
    ).scalar_one()
    made = [
        connection.execute(
            text(
                "insert into core.connections (connector, display_name, status, config,"
                " owner_entity_id) values (:c, 'Config test', 'healthy', cast(:config as jsonb),"
                " :owner) returning id"
            ),
            {"c": connector, "config": json.dumps(_CONFIG), "owner": owner},
        ).scalar_one()
        for connector in ("binance", "trading212")
    ]
    return owner, made[0], made[1]


def _assert_config(engine: Engine, seeded: tuple[object, object, object], down_to: str) -> None:
    _, binance, other = seeded
    with engine.begin() as connection:
        config = {
            id_: connection.execute(
                text("select config from core.connections where id = :id"), {"id": id_}
            ).scalar_one()
            for id_ in (binance, other)
        }
    removed = _REMOVED[down_to] | _REPLAY
    assert config[binance] == {k: v for k, v in _CONFIG.items() if k not in removed}
    assert config[other] == {k: v for k, v in _CONFIG.items() if k not in _REPLAY}


def _unseed(engine: Engine, seeded: tuple[object, object, object]) -> None:
    owner, binance, other = seeded
    with engine.begin() as connection:
        for id_ in (binance, other):
            connection.execute(text("delete from core.connections where id = :id"), {"id": id_})
        connection.execute(text("delete from core.entities where id = :id"), {"id": owner})


def test_each_migration_creates_its_own_tables() -> None:
    assert len(_created("0041")) == 44
    assert "binance_assets_seen" in _created("0041")
    assert _created("0042") == []
    assert _created("0043") == ["binance_account_reports", "binance_reconciliation_differences"]
    assert _created("0044") == ["binance_klines", "binance_coingecko_ids"]


_CHECK = (
    "select pg_get_constraintdef(c.oid) from pg_constraint c"
    " join pg_class t on t.oid = c.conrelid join pg_namespace n on n.oid = t.relnamespace"
    " where n.nspname = 'finance' and t.relname = :table and c.conname = :name"
)


def _check(connection: Connection, table: str, name: str) -> str | None:
    return connection.execute(text(_CHECK), {"table": table, "name": name}).scalar()


def _allowed(definition: str | None) -> set[str]:
    """The quoted values a CHECK's IN list (or ANY array) names."""
    assert definition is not None
    return set(re.findall(r"'(\w+)'", definition))


# What 0040 (main before Binance) leaves: 0041 adds to these lists and its downgrade restores them.
_M6_KINDS = {
    "uk_bank_account",
    "ibkr_account",
    "trading212_account",
    "etoro_account",
    "card_merchant",
}
_M6_SCHEMES = {
    "iso4217",
    "isin",
    "figi",
    "ticker_mic",
    "ibkr_conid",
    "etoro_id",
    "chain_contract",
    "coingecko",
    "trading212_ticker",
}


def _assert_lists(connection: Connection, *, binance: bool) -> None:
    kinds = _allowed(_check(connection, "account_identifiers", "account_identifiers_kind_check"))
    schemes = _allowed(
        _check(connection, "instrument_identifiers", "instrument_identifiers_scheme_check")
    )
    assert kinds == (_M6_KINDS | {"chain_address"} if binance else _M6_KINDS)
    assert schemes == (_M6_SCHEMES | {"binance_asset"} if binance else _M6_SCHEMES)
    # M6's own value checks are left as they are.
    for name in (
        "account_identifiers_trading212_account_check",
        "account_identifiers_etoro_account_check",
        "account_identifiers_card_merchant_check",
    ):
        assert _check(connection, "account_identifiers", name) is not None
    chain = _check(connection, "account_identifiers", "account_identifiers_chain_address_check")
    assert (chain is not None) == binance


def test_migration_round_trip(owner_url: str, migrated: None) -> None:
    config = _config(owner_url)
    core, later = _created("0041"), [*_created("0043"), *_created("0044")]
    engine = create_engine(owner_url)
    try:
        with engine.begin() as connection:
            assert _present(connection, core + later) == _all(core + later, True)
            _assert_lists(connection, binance=True)
            identifier = connection.execute(
                text(
                    "insert into finance.account_identifiers (kind, value, created_by)"
                    " values ('chain_address', '0x11111111111111111111', 'test') returning id"
                )
            ).scalar_one()
            # A withdrawal to that address names it; the downgrade must let go of the name.
            owner = connection.execute(
                text(
                    "insert into core.entities (kind, name) values ('person', 'Migration test')"
                    " returning id"
                )
            ).scalar_one()
            named = connection.execute(
                text(
                    "insert into finance.transactions (occurred_at, booking_date, status,"
                    "  origin, owner_entity_id, counterparty_identifier_id)"
                    " values (now(), current_date, 'booked', 'manual', :owner, :identifier)"
                    " returning id"
                ),
                {"owner": owner, "identifier": identifier},
            ).scalar_one()
            seeded = _seed(connection)
        try:
            command.downgrade(config, "0040")
            _assert_config(engine, seeded, "0040")
            with engine.begin() as connection:
                assert _present(connection, core + later) == _all(core + later, False)
                kept = connection.execute(
                    text(
                        "select counterparty_identifier_id from finance.transactions where id = :id"
                    ),
                    {"id": named},
                )
                assert kept.one() == (None,)
                gone = connection.execute(
                    text(
                        "select count(*) from finance.account_identifiers"
                        " where kind = 'chain_address'"
                    )
                )
                assert gone.scalar() == 0
                _assert_lists(connection, binance=False)
        finally:
            command.upgrade(config, "head")
            with engine.begin() as connection:
                connection.execute(
                    text("delete from finance.transactions where id = :id"), {"id": named}
                )
                connection.execute(text("delete from core.entities where id = :id"), {"id": owner})
            _unseed(engine, seeded)
        with engine.begin() as connection:
            assert _present(connection, core + later) == _all(core + later, True)
            _assert_lists(connection, binance=True)
    finally:
        engine.dispose()


_NEVER_TRANSFER = (
    "select count(*) from information_schema.columns where table_schema = 'finance'"
    " and table_name = 'transactions' and column_name = 'never_transfer'"
)


def test_never_transfer_migration_round_trip(owner_url: str, migrated: None) -> None:
    config = _config(owner_url)
    core = _created("0041")
    engine = create_engine(owner_url)
    try:
        with engine.begin() as connection:
            seeded = _seed(connection)
        try:
            command.downgrade(config, "0041")
            _assert_config(engine, seeded, "0041")
            with engine.begin() as connection:
                assert connection.execute(text(_NEVER_TRANSFER)).scalar() == 0
                assert _present(connection, core) == _all(core, True)
        finally:
            command.upgrade(config, "head")
            _unseed(engine, seeded)
        with engine.begin() as connection:
            assert connection.execute(text(_NEVER_TRANSFER)).scalar() == 1
    finally:
        engine.dispose()


def test_reconcile_migration_round_trip(owner_url: str, migrated: None) -> None:
    config = _config(owner_url)
    tables = _created("0043")
    engine = create_engine(owner_url)
    try:
        with engine.begin() as connection:
            seeded = _seed(connection)
        try:
            command.downgrade(config, "0042")
            _assert_config(engine, seeded, "0042")
            with engine.begin() as connection:
                assert _present(connection, tables) == _all(tables, False)
                assert connection.execute(text(_NEVER_TRANSFER)).scalar() == 1
        finally:
            command.upgrade(config, "head")
            _unseed(engine, seeded)
        with engine.begin() as connection:
            assert _present(connection, tables) == _all(tables, True)
    finally:
        engine.dispose()


def test_prices_migration_round_trip(owner_url: str, migrated: None) -> None:
    config = _config(owner_url)
    tables, before = _created("0044"), _created("0043")
    engine = create_engine(owner_url)
    try:
        with engine.begin() as connection:
            seeded = _seed(connection)
        try:
            command.downgrade(config, "0043")
            _assert_config(engine, seeded, "0043")
            with engine.begin() as connection:
                assert _present(connection, tables) == _all(tables, False)
                assert _present(connection, before) == _all(before, True)
        finally:
            command.upgrade(config, "head")
            _unseed(engine, seeded)
        with engine.begin() as connection:
            assert _present(connection, tables) == _all(tables, True)
    finally:
        engine.dispose()
