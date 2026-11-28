"""Migration 0050 (the Bitcoin wallet tables, the chain columns on transactions and the
`chain_native` instrument scheme) goes down to 0049 and up again, and the CHECKs hold."""

import re
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, create_engine, exc, text

BACKEND = Path(__file__).parents[3]
VERSIONS = BACKEND / "migrations" / "versions"
TABLES = [
    "bitcoin_addresses",
    "bitcoin_address_reports",
    "bitcoin_transactions",
    "bitcoin_inputs",
    "bitcoin_outputs",
]
TX_HASH = "ab" * 32


def _present(connection: Connection) -> dict[str, bool]:
    return {
        table: connection.execute(
            text("select to_regclass(:name) is not null"), {"name": f"finance.{table}"}
        ).scalar()
        is True
        for table in TABLES
    }


def _chain_columns(connection: Connection) -> set[str]:
    return set(
        connection.execute(
            text(
                "select column_name from information_schema.columns"
                " where table_schema = 'finance' and table_name = 'transactions'"
                " and column_name in ('chain', 'tx_hash', 'chain_address')"
            )
        ).scalars()
    )


def _schemes(connection: Connection) -> set[str]:
    definition = connection.execute(
        text(
            "select pg_get_constraintdef(oid) from pg_constraint"
            " where conname = 'instrument_identifiers_scheme_check'"
        )
    ).scalar_one()
    return set(re.findall(r"'(\w+)'", definition))


def test_migration_creates_its_own_tables() -> None:
    [path] = VERSIONS.glob("0050_*.py")
    assert re.findall(r"CREATE TABLE finance\.(\w+)", path.read_text()) == TABLES


# The links to finance.transactions, each indexed where set, as Binance's are (0041): a
# transaction's delete sets them NULL, and its lookup by link must not scan the table.
LINK_INDEXES = {
    "bitcoin_outputs_transaction_idx",
    "bitcoin_transactions_fee_transaction_idx",
    "bitcoin_transactions_net_transaction_idx",
}


def _link_indexes(connection: Connection) -> set[str]:
    return set(
        connection.execute(
            text(
                "select indexname from pg_indexes where schemaname = 'finance'"
                " and indexname = any(:names)"
            ),
            {"names": list(LINK_INDEXES)},
        ).scalars()
    )


def test_migration_round_trips(owner_url: str, migrated: None) -> None:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", owner_url)
    engine = create_engine(owner_url)
    try:
        with engine.begin() as connection:
            assert set(_present(connection).values()) == {True}
            assert _chain_columns(connection) == {"chain", "tx_hash", "chain_address"}
            assert "chain_native" in _schemes(connection)
            assert _link_indexes(connection) == LINK_INDEXES
        try:
            command.downgrade(config, "0049")
            with engine.begin() as connection:
                assert set(_present(connection).values()) == {False}
                assert _chain_columns(connection) == set()
                assert {"binance_asset", "coingecko"} <= _schemes(connection)
                assert "chain_native" not in _schemes(connection)
                assert _link_indexes(connection) == set()
        finally:
            command.upgrade(config, "head")
        with engine.begin() as connection:
            assert set(_present(connection).values()) == {True}
            assert _chain_columns(connection) == {"chain", "tx_hash", "chain_address"}
            assert "chain_native" in _schemes(connection)
            assert _link_indexes(connection) == LINK_INDEXES
    finally:
        engine.dispose()


# Invented bech32-shaped values (the CHECK wants 20 to 128 letters and digits).
_UPPER = "BC1QUPPERCASEADDRESSFORTESTSONLY0000000000"
_TWIN_UPPER = "BC1QTWINADDRESSFORTESTSONLY00000000000000"


def test_upgrade_lower_cases_stored_bech32_addresses(owner_url: str, migrated: None) -> None:
    """A bech32 address stored upper-case before 0050 is lower-cased, so it still matches
    the normalised form; one whose lower-cased form is already stored is left alone."""
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", owner_url)
    engine = create_engine(owner_url)
    values = (_UPPER, _TWIN_UPPER, _TWIN_UPPER.lower())
    try:
        command.downgrade(config, "0049")
        try:
            with engine.begin() as connection:
                for value in values:
                    connection.execute(
                        text(
                            "insert into finance.account_identifiers (kind, value, created_by)"
                            " values ('chain_address', :v, 'test')"
                        ),
                        {"v": value},
                    )
        finally:
            command.upgrade(config, "head")
        with engine.begin() as connection:
            stored = set(
                connection.execute(
                    text(
                        "select value from finance.account_identifiers"
                        " where kind = 'chain_address' and lower(value) = any(:v)"
                    ),
                    {"v": [v.lower() for v in values]},
                ).scalars()
            )
            connection.execute(
                text(
                    "delete from finance.account_identifiers"
                    " where kind = 'chain_address' and lower(value) = any(:v)"
                ),
                {"v": [v.lower() for v in values]},
            )
        assert stored == {_UPPER.lower(), _TWIN_UPPER, _TWIN_UPPER.lower()}
    finally:
        engine.dispose()


def _insert_transaction(connection: Connection, **chain: object) -> None:
    owner = connection.execute(
        text("insert into core.entities (kind, name) values ('person', 'Schema test') returning id")
    ).scalar_one()
    columns = ", ".join(chain)
    values = ", ".join(f":{name}" for name in chain)
    connection.execute(
        text(
            "insert into finance.transactions (occurred_at, booking_date, status, origin,"
            f" owner_entity_id, {columns}) values (now(), current_date, 'booked', 'manual',"
            f" :owner, {values})"
        ),
        {"owner": owner, **chain},
    )


@pytest.mark.parametrize(
    "chain",
    [
        {"chain": "bitcoin", "tx_hash": "AB" * 32},
        {"chain": "bitcoin", "tx_hash": "ab" * 31},
        {"chain": "bitcoin"},
        {"tx_hash": TX_HASH},
        {"chain_address": "bc1qexample"},
    ],
)
def test_chain_checks_refuse(owner_url: str, migrated: None, chain: dict[str, str]) -> None:
    engine = create_engine(owner_url)
    try:
        with pytest.raises(exc.IntegrityError), engine.begin() as connection:
            _insert_transaction(connection, **chain)
    finally:
        engine.dispose()


def test_chain_checks_accept(owner_url: str, migrated: None) -> None:
    engine = create_engine(owner_url)
    try:
        with engine.connect() as connection:
            trans = connection.begin()
            try:
                _insert_transaction(connection, chain="bitcoin", tx_hash=TX_HASH)
                _insert_transaction(
                    connection, chain="bitcoin", tx_hash=TX_HASH, chain_address="bc1qexample"
                )
            finally:
                trans.rollback()
    finally:
        engine.dispose()


def test_chain_native_value_check(owner_url: str, migrated: None) -> None:
    engine = create_engine(owner_url)
    try:
        for value, ok in (("bitcoin", True), ("a-b-1", True), ("Bitcoin", False), ("a_b", False)):
            with engine.connect() as connection:
                trans = connection.begin()
                try:
                    owner = connection.execute(
                        text(
                            "insert into core.entities (kind, name)"
                            " values ('person', 'Schema test') returning id"
                        )
                    ).scalar_one()
                    instrument = connection.execute(
                        text(
                            "insert into finance.instruments (kind, symbol, name, decimals,"
                            " owner_entity_id) values ('crypto', 'ZZZ', 'Test', 8, :owner)"
                            " returning id"
                        ),
                        {"owner": owner},
                    ).scalar_one()
                    statement = text(
                        "insert into finance.instrument_identifiers (instrument_id, scheme, value)"
                        " values (:id, 'chain_native', :value)"
                    )
                    if ok:
                        connection.execute(statement, {"id": instrument, "value": value})
                    else:
                        with pytest.raises(exc.IntegrityError):
                            connection.execute(statement, {"id": instrument, "value": value})
                finally:
                    trans.rollback()
    finally:
        engine.dispose()
