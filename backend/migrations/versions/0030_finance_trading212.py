"""Trading 212: the trading212_account registry kind, the trading212_ticker scheme, the
typed tables of what the API returns, and when a sync saw each report it stored.

Every known path of the Trading 212 field lists (trading212/fields.py) is a typed column of a
finance.trading212_* table, named by the snake_case of its dotted path below the row (M6
design, Decision 3); an array of objects or a map is a child table keyed by its parent and the
item's index (or the map's key). Numbers, ids among them, are numeric; strings text; instants
timestamptz; flags boolean. Each row carries its connection, its key and the raw record it was
read from; a row that is posted links its ledger row, which it outlives (SET NULL).

finance.report_sightings (any connector's): a raw record is stored once per content, so a
report whose content returns is one record. Each sync records each report record it stores or
finds stored, with the time its response arrived: what dates a report.

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-23
"""

import re
from collections.abc import Sequence

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NON_FINITE = "('NaN', 'Infinity', '-Infinity')"
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _column(path: str) -> str:
    """Decision 3's name for a path, as typed_tables.column_name gives it today."""
    return re.sub(r"[^a-z0-9]+", "_", _CAMEL.sub("_", path).lower()).strip("_")


def _columns(kind: str, paths: str, required: str = "") -> list[str]:
    """Column definitions for the paths of one type; `required` ones are NOT NULL."""
    needed = set(required.split())
    lines: list[str] = []
    for path in paths.split():
        line = f'"{_column(path)}" {kind}'
        if path in needed:
            line += " NOT NULL"
        if kind == "numeric":
            line += f' CHECK ("{_column(path)}" NOT IN {_NON_FINITE})'
        lines.append(line)
    if unknown := needed - set(paths.split()):
        raise ValueError(f"required but not listed: {sorted(unknown)}")
    return lines


def _table(name: str, head: list[str], *groups: list[str], tail: Sequence[str] = ()) -> str:
    body = ",\n      ".join([*head, *(line for group in groups for line in group), *tail])
    return f"CREATE TABLE finance.{name} (\n      {body}\n    )"


_CONNECTION = "connection_id uuid NOT NULL REFERENCES core.connections (id)"
_RAW = "raw_record_id uuid NOT NULL REFERENCES core.raw_records (id)"
_TRANSACTION = "transaction_id uuid UNIQUE REFERENCES finance.transactions (id) ON DELETE SET NULL"
_INDEX = "item_index integer NOT NULL CHECK (item_index >= 0)"

_INSTRUMENT_TEXT = "instrument.ticker instrument.name instrument.isin instrument.currency"
_ORDER_TEXT = f"""ticker {_INSTRUMENT_TEXT} type side status strategy timeInForce currency
initiatedFrom"""
_ORDER_NUMERIC = "id quantity filledQuantity value filledValue limitPrice stopPrice"
_RESULT = "priceAvgInvestedValue priceAvgResult priceAvgResultCoef priceAvgValue"


def _prefixed(prefix: str, paths: str) -> str:
    return " ".join(f"{prefix}{path}" for path in paths.split())


_UPGRADE_TABLES = [
    # account/summary, one row per sync day; linked to its cash reported snapshot.
    _table(
        "trading212_accounts",
        [
            _CONNECTION,
            "as_of_date date NOT NULL",
            "account_id uuid NOT NULL REFERENCES finance.accounts (id)",
            "account_kind text NOT NULL CHECK (account_kind IN ('invest', 'isa'))",
            # The reported snapshot of the account's cash this summary is the detail of.
            "snapshot_id uuid UNIQUE REFERENCES finance.balance_snapshots (id) ON DELETE SET NULL",
            _RAW,
        ],
        _columns(
            "numeric",
            """id totalValue cash.availableToTrade cash.reservedForOrders cash.inPies
            investments.currentValue investments.totalCost investments.realizedProfitLoss
            investments.unrealizedProfitLoss""",
            "id cash.availableToTrade cash.reservedForOrders cash.inPies",
        ),
        _columns("text", "currency", "currency"),
        tail=["PRIMARY KEY (connection_id, as_of_date)"],
    ),
    # positions, one row per instrument and sync day; linked to its reported snapshot.
    _table(
        "trading212_positions",
        [
            _CONNECTION,
            "as_of_date date NOT NULL",
            "snapshot_id uuid UNIQUE REFERENCES finance.balance_snapshots (id) ON DELETE SET NULL",
            _RAW,
        ],
        _columns(
            "text",
            f"{_INSTRUMENT_TEXT} walletImpact.currency",
            "instrument.ticker instrument.isin instrument.currency",
        ),
        _columns(
            "numeric",
            """quantity quantityAvailableForTrading quantityInPies averagePricePaid currentPrice
            walletImpact.totalCost walletImpact.currentValue walletImpact.unrealizedProfitLoss
            walletImpact.fxImpact""",
            "quantity",
        ),
        _columns("timestamptz", "createdAt"),
        tail=["PRIMARY KEY (connection_id, as_of_date, instrument_ticker)"],
    ),
    # orders (pending), one row per order and sync day.
    _table(
        "trading212_pending_orders",
        [_CONNECTION, "as_of_date date NOT NULL", _RAW],
        _columns("numeric", _ORDER_NUMERIC, "id"),
        _columns("text", _ORDER_TEXT, "ticker side status"),
        _columns("boolean", "extendedHours"),
        _columns("timestamptz", "createdAt"),
        tail=["PRIMARY KEY (connection_id, as_of_date, id)"],
    ),
    # history/orders: external_id is `order:<fill id>`, or `order-none:<order id>` unfilled.
    _table(
        "trading212_orders",
        [_CONNECTION, "external_id text NOT NULL", _TRANSACTION, _RAW],
        _columns("numeric", _prefixed("order.", _ORDER_NUMERIC), "order.id"),
        _columns(
            "text",
            _prefixed("order.", _ORDER_TEXT),
            """order.side order.status order.instrument.ticker order.instrument.isin
            order.instrument.currency""",
        ),
        _columns("boolean", "order.extendedHours"),
        _columns("timestamptz", "order.createdAt"),
        _columns(
            "numeric",
            """fill.id fill.price fill.quantity fill.walletImpact.fxRate
            fill.walletImpact.netValue fill.walletImpact.realisedProfitLoss""",
        ),
        _columns("text", "fill.tradingMethod fill.type fill.walletImpact.currency"),
        _columns("timestamptz", "fill.filledAt"),
        tail=["PRIMARY KEY (connection_id, external_id)"],
    ),
    _table(
        "trading212_order_taxes",
        [_CONNECTION, "external_id text NOT NULL", _INDEX, _RAW],
        _columns("text", "name currency", "name currency"),
        _columns("numeric", "quantity", "quantity"),
        _columns("timestamptz", "chargedAt"),
        tail=[
            "PRIMARY KEY (connection_id, external_id, item_index)",
            "FOREIGN KEY (connection_id, external_id)"
            " REFERENCES finance.trading212_orders (connection_id, external_id)"
            " ON DELETE CASCADE",
        ],
    ),
    _table(
        "trading212_dividends",
        [_CONNECTION, _TRANSACTION, _RAW],
        _columns(
            "text",
            f"reference ticker {_INSTRUMENT_TEXT} type currency tickerCurrency",
            "reference currency instrument.ticker instrument.isin instrument.currency",
        ),
        _columns("numeric", "amount amountInEuro grossAmountPerShare quantity", "amount"),
        _columns("timestamptz", "paidOn", "paidOn"),
        tail=["PRIMARY KEY (connection_id, reference)"],
    ),
    _table(
        "trading212_transactions",
        [_CONNECTION, _TRANSACTION, _RAW],
        _columns("text", "reference type currency", "reference type currency"),
        _columns("numeric", "amount", "amount"),
        _columns("timestamptz", "dateTime", "dateTime"),
        tail=["PRIMARY KEY (connection_id, reference)"],
    ),
    # metadata/instruments; exchange_id is the exchange whose schedule workingScheduleId is.
    _table(
        "trading212_instruments",
        [
            _CONNECTION,
            "instrument_id uuid REFERENCES finance.instruments (id)",
            "exchange_id numeric CHECK (exchange_id NOT IN ('NaN', 'Infinity', '-Infinity'))",
            _RAW,
        ],
        _columns("text", "ticker name shortName isin currencyCode type", "ticker type"),
        _columns("numeric", "maxOpenQuantity workingScheduleId"),
        _columns("boolean", "extendedHours"),
        _columns("timestamptz", "addedOn"),
        tail=["PRIMARY KEY (connection_id, ticker)"],
    ),
    _table(
        "trading212_exchanges",
        [_CONNECTION, _RAW],
        _columns("numeric", "id", "id"),
        _columns("text", "name"),
        tail=["PRIMARY KEY (connection_id, id)"],
    ),
    _table(
        "trading212_exchange_schedules",
        [_CONNECTION, "exchange_id numeric NOT NULL", _INDEX, _RAW],
        _columns("numeric", "id", "id"),
        tail=[
            "PRIMARY KEY (connection_id, exchange_id, item_index)",
            "FOREIGN KEY (connection_id, exchange_id)"
            " REFERENCES finance.trading212_exchanges (connection_id, id) ON DELETE CASCADE",
        ],
    ),
    _table(
        "trading212_exchange_time_events",
        [
            _CONNECTION,
            "exchange_id numeric NOT NULL",
            "schedule_index integer NOT NULL",
            _INDEX,
            _RAW,
        ],
        _columns("timestamptz", "date"),
        _columns("text", "type"),
        tail=[
            "PRIMARY KEY (connection_id, exchange_id, schedule_index, item_index)",
            "FOREIGN KEY (connection_id, exchange_id, schedule_index)"
            " REFERENCES finance.trading212_exchange_schedules"
            " (connection_id, exchange_id, item_index) ON DELETE CASCADE",
        ],
    ),
    # pies (the list) and pies/{id} (settings, from detail_raw_record_id), per sync day.
    _table(
        "trading212_pies",
        [
            _CONNECTION,
            "as_of_date date NOT NULL",
            _RAW,
            "detail_raw_record_id uuid REFERENCES core.raw_records (id)",
        ],
        _columns(
            "numeric",
            f"""id cash progress {_prefixed("result.", _RESULT)} dividendDetails.gained
            dividendDetails.inCash dividendDetails.reinvested settings.id settings.goal
            settings.initialInvestment""",
            "id",
        ),
        _columns(
            "text",
            """status settings.name settings.icon settings.dividendCashAction
            settings.publicUrl""",
        ),
        _columns("timestamptz", "settings.creationDate settings.endDate"),
        tail=["PRIMARY KEY (connection_id, id, as_of_date)"],
    ),
    _table(
        "trading212_pie_instruments",
        [_CONNECTION, "pie_id numeric NOT NULL", "as_of_date date NOT NULL", _INDEX, _RAW],
        _columns("text", "ticker", "ticker"),
        _columns(
            "numeric",
            f"ownedQuantity currentShare expectedShare {_prefixed('result.', _RESULT)}",
        ),
        tail=[
            "PRIMARY KEY (connection_id, pie_id, as_of_date, item_index)",
            "FOREIGN KEY (connection_id, pie_id, as_of_date)"
            " REFERENCES finance.trading212_pies (connection_id, id, as_of_date)"
            " ON DELETE CASCADE",
        ],
    ),
    _table(
        "trading212_pie_instrument_issues",
        [
            _CONNECTION,
            "pie_id numeric NOT NULL",
            "as_of_date date NOT NULL",
            "instrument_index integer NOT NULL",
            _INDEX,
            _RAW,
        ],
        _columns("text", "name severity"),
        tail=[
            "PRIMARY KEY (connection_id, pie_id, as_of_date, instrument_index, item_index)",
            "FOREIGN KEY (connection_id, pie_id, as_of_date, instrument_index)"
            " REFERENCES finance.trading212_pie_instruments"
            " (connection_id, pie_id, as_of_date, item_index) ON DELETE CASCADE",
        ],
    ),
    # settings.instrumentShares: ticker → share.
    _table(
        "trading212_pie_instrument_shares",
        [_CONNECTION, "pie_id numeric NOT NULL", "as_of_date date NOT NULL", _RAW],
        _columns("text", "ticker", "ticker"),
        _columns("numeric", "share", "share"),
        tail=[
            "PRIMARY KEY (connection_id, pie_id, as_of_date, ticker)",
            "FOREIGN KEY (connection_id, pie_id, as_of_date)"
            " REFERENCES finance.trading212_pies (connection_id, id, as_of_date)"
            " ON DELETE CASCADE",
        ],
    ),
]

_TABLES = [
    "trading212_accounts",
    "trading212_positions",
    "trading212_pending_orders",
    "trading212_orders",
    "trading212_order_taxes",
    "trading212_dividends",
    "trading212_transactions",
    "trading212_instruments",
    "trading212_exchanges",
    "trading212_exchange_schedules",
    "trading212_exchange_time_events",
    "trading212_pies",
    "trading212_pie_instruments",
    "trading212_pie_instrument_issues",
    "trading212_pie_instrument_shares",
]

_SCHEMES = """'iso4217', 'isin', 'figi', 'ticker_mic', 'ibkr_conid', 'etoro_id', 'chain_contract',
          'coingecko'"""

_SIGHTINGS = [
    """
    CREATE TABLE finance.report_sightings (
      connection_id uuid NOT NULL REFERENCES core.connections (id),
      raw_record_id uuid NOT NULL REFERENCES core.raw_records (id),
      sync_id uuid NOT NULL,
      seen_at timestamptz NOT NULL,
      PRIMARY KEY (connection_id, raw_record_id, sync_id)
    )
    """,
    "CREATE INDEX report_sightings_sync ON finance.report_sightings (connection_id, sync_id)",
    "CREATE INDEX report_sightings_seen ON finance.report_sightings (connection_id, seen_at)",
]

UPGRADE = [
    """
    ALTER TABLE finance.account_identifiers
      DROP CONSTRAINT account_identifiers_kind_check,
      ADD CONSTRAINT account_identifiers_kind_check
        CHECK (kind IN ('uk_bank_account', 'ibkr_account', 'trading212_account')),
      ADD CONSTRAINT account_identifiers_trading212_account_check
        CHECK (kind <> 'trading212_account' OR value ~ '^[1-9][0-9]{0,18}$')
    """,
    f"""
    ALTER TABLE finance.instrument_identifiers
      DROP CONSTRAINT instrument_identifiers_scheme_check,
      ADD CONSTRAINT instrument_identifiers_scheme_check
        CHECK (scheme IN ({_SCHEMES}, 'trading212_ticker'))
    """,
    *_UPGRADE_TABLES,
    *_SIGHTINGS,
]

DOWNGRADE = [
    "DROP TABLE finance.report_sightings",
    *(f"DROP TABLE finance.{name}" for name in reversed(_TABLES)),
    "DELETE FROM finance.instrument_identifiers WHERE scheme = 'trading212_ticker'",
    f"""
    ALTER TABLE finance.instrument_identifiers
      DROP CONSTRAINT instrument_identifiers_scheme_check,
      ADD CONSTRAINT instrument_identifiers_scheme_check CHECK (scheme IN ({_SCHEMES}))
    """,
    "DELETE FROM finance.account_identifiers WHERE kind = 'trading212_account'",
    """
    ALTER TABLE finance.account_identifiers
      DROP CONSTRAINT account_identifiers_trading212_account_check,
      DROP CONSTRAINT account_identifiers_kind_check,
      ADD CONSTRAINT account_identifiers_kind_check
        CHECK (kind IN ('uk_bank_account', 'ibkr_account'))
    """,
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
