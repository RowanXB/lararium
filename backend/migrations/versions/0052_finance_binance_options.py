"""Binance: the typed tables of the Options wallet (design 5.6, 5.7, 5.11): its margin account
(the wallet's report; Decision 11 reconciles on its assets' `margin_balance`), positions, bill,
trade and exercise histories, and the options host's exchange information (its rate limits, plan
Decision 4, and its symbols); by the same rule as 0041 (plan Decision 3): a record table per
history label keyed by Binance's id, a report table per account, position or public label keyed
by the read, its arrays child tables. A bill row keeps the movement it is linked to (rule 2).
The statements were emitted from the catalogue (`tests/finance/binance/emit_ddl.py`) and are
frozen here.

Revision ID: 0052
Revises: 0051
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    CREATE TABLE finance.binance_opt_exchange_info (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "timezone" text,
      "server_time" timestamptz,
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_opt_exchange_info_option_assets (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "option_assets_ordinal" integer NOT NULL,
      "name" text,
      PRIMARY KEY ("connection_id", "snapshot_at", "option_assets_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at")
        REFERENCES finance.binance_opt_exchange_info
          ("connection_id", "snapshot_at")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_opt_exchange_info_option_contracts (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "option_contracts_ordinal" integer NOT NULL,
      "base_asset" text,
      "quote_asset" text,
      "underlying" text,
      "settle_asset" text,
      PRIMARY KEY ("connection_id", "snapshot_at", "option_contracts_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at")
        REFERENCES finance.binance_opt_exchange_info
          ("connection_id", "snapshot_at")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_opt_exchange_info_option_symbols (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "option_symbols_ordinal" integer NOT NULL,
      "expiry_date" timestamptz,
      "symbol" text NOT NULL,
      "side" text,
      "strike_price" numeric
        CHECK ("strike_price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "underlying" text,
      "unit" bigint,
      "liquidation_fee_rate" numeric
        CHECK ("liquidation_fee_rate" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "min_qty" numeric
        CHECK ("min_qty" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "max_qty" numeric
        CHECK ("max_qty" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "initial_margin" numeric
        CHECK ("initial_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "maintenance_margin" numeric
        CHECK ("maintenance_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "min_initial_margin" numeric
        CHECK ("min_initial_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "min_maintenance_margin" numeric
        CHECK ("min_maintenance_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "price_scale" bigint,
      "quantity_scale" bigint,
      "quote_asset" text,
      "contract_type" text,
      "underlying_type" text,
      "naked_sell" boolean,
      "status" text,
      PRIMARY KEY ("connection_id", "snapshot_at", "option_symbols_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at")
        REFERENCES finance.binance_opt_exchange_info
          ("connection_id", "snapshot_at")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_opt_exchange_info_rate_limits (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "rate_limits_ordinal" integer NOT NULL,
      "rate_limit_type" text,
      "interval" text,
      "interval_num" bigint,
      "limit" bigint,
      PRIMARY KEY ("connection_id", "snapshot_at", "rate_limits_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at")
        REFERENCES finance.binance_opt_exchange_info
          ("connection_id", "snapshot_at")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_opt_exchange_info_option_symbols_filters (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "option_symbols_ordinal" integer NOT NULL,
      "filters_ordinal" integer NOT NULL,
      "filter_type" text,
      "min_price" numeric
        CHECK ("min_price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "max_price" numeric
        CHECK ("max_price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "tick_size" numeric
        CHECK ("tick_size" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "min_qty" numeric
        CHECK ("min_qty" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "max_qty" numeric
        CHECK ("max_qty" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "step_size" numeric
        CHECK ("step_size" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "snapshot_at", "option_symbols_ordinal", "filters_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at", "option_symbols_ordinal")
        REFERENCES finance.binance_opt_exchange_info_option_symbols
          ("connection_id", "snapshot_at", "option_symbols_ordinal")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_opt_account (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "time" timestamptz,
      "can_trade" boolean,
      "can_deposit" boolean,
      "can_withdraw" boolean,
      "reduce_only" boolean,
      "trade_group_id" text,
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_opt_account_asset (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "asset_ordinal" integer NOT NULL,
      "asset" text NOT NULL,
      "margin_balance" numeric
        CHECK ("margin_balance" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "equity" numeric
        CHECK ("equity" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "available" numeric
        CHECK ("available" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "initial_margin" numeric
        CHECK ("initial_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "maint_margin" numeric
        CHECK ("maint_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "unrealized_pnl" numeric
        CHECK ("unrealized_pnl" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "adjusted_equity" numeric
        CHECK ("adjusted_equity" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "snapshot_at", "asset_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at")
        REFERENCES finance.binance_opt_account
          ("connection_id", "snapshot_at")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_opt_account_greek (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "greek_ordinal" integer NOT NULL,
      "underlying" text,
      "delta" numeric
        CHECK ("delta" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "gamma" numeric
        CHECK ("gamma" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "theta" numeric
        CHECK ("theta" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "vega" numeric
        CHECK ("vega" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "snapshot_at", "greek_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at")
        REFERENCES finance.binance_opt_account
          ("connection_id", "snapshot_at")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_opt_positions (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "entry_price" numeric
        CHECK ("entry_price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "symbol" text NOT NULL,
      "side" text NOT NULL,
      "quantity" numeric
        CHECK ("quantity" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "mark_value" numeric
        CHECK ("mark_value" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "unrealized_pnl" numeric
        CHECK ("unrealized_pnl" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "mark_price" numeric
        CHECK ("mark_price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "strike_price" numeric
        CHECK ("strike_price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "expiry_date" timestamptz,
      "price_scale" bigint,
      "quantity_scale" bigint,
      "option_side" text,
      "quote_asset" text,
      "time" timestamptz NOT NULL,
      "bid_quantity" numeric
        CHECK ("bid_quantity" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "ask_quantity" numeric
        CHECK ("ask_quantity" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "snapshot_at", "symbol", "side")
    )
    """,
    """
    CREATE TABLE finance.binance_opt_bill (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "id" text NOT NULL,
      "asset" text NOT NULL,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "symbol" text,
      "type" text,
      "create_date" timestamptz NOT NULL,
      "movement_key" text,
      PRIMARY KEY ("connection_id", "id")
    )
    """,
    """
    CREATE INDEX binance_opt_bill_transaction_idx
      ON finance.binance_opt_bill (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_opt_trades (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "id" text NOT NULL,
      "trade_id" text,
      "order_id" text,
      "symbol" text,
      "price" numeric NOT NULL
        CHECK ("price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "quantity" numeric NOT NULL
        CHECK ("quantity" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "fee" numeric
        CHECK ("fee" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "realized_profit" numeric
        CHECK ("realized_profit" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "side" text,
      "type" text,
      "liquidity" text,
      "time" timestamptz NOT NULL,
      "price_scale" bigint,
      "quantity_scale" bigint,
      "option_side" text,
      "quote_asset" text,
      PRIMARY KEY ("connection_id", "id")
    )
    """,
    """
    CREATE INDEX binance_opt_trades_transaction_idx
      ON finance.binance_opt_trades (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_opt_exercises (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "id" text NOT NULL,
      "currency" text,
      "symbol" text,
      "exercise_price" numeric
        CHECK ("exercise_price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "quantity" numeric
        CHECK ("quantity" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "fee" numeric
        CHECK ("fee" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "create_date" timestamptz NOT NULL,
      "price_scale" bigint,
      "quantity_scale" bigint,
      "option_side" text,
      "position_side" text,
      "quote_asset" text,
      PRIMARY KEY ("connection_id", "id")
    )
    """,
    """
    CREATE INDEX binance_opt_exercises_transaction_idx
      ON finance.binance_opt_exercises (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
]

_DROPPED = [
    "binance_opt_exercises",
    "binance_opt_trades",
    "binance_opt_bill",
    "binance_opt_positions",
    "binance_opt_account_greek",
    "binance_opt_account_asset",
    "binance_opt_account",
    "binance_opt_exchange_info_option_symbols_filters",
    "binance_opt_exchange_info_rate_limits",
    "binance_opt_exchange_info_option_symbols",
    "binance_opt_exchange_info_option_contracts",
    "binance_opt_exchange_info_option_assets",
    "binance_opt_exchange_info",
]
_REPORTS = ["opt-exchange-info", "opt-account", "opt-positions"]

# The raw records and the cursors are kept, as 0041's downgrade keeps them; the transactions
# these records posted stay in the ledger.
DOWNGRADE = [
    # The last raw record applied: with these typed tables gone, an upgrade must apply every raw
    # record again to fill them.
    "UPDATE core.connections SET config = config - 'normalized_through'"
    " WHERE connector = 'binance'",
    "UPDATE core.connections SET config = jsonb_set(config, '{report_digests}',"
    f" (config -> 'report_digests') - ARRAY[{', '.join(repr(label) for label in _REPORTS)}])"
    " WHERE connector = 'binance' AND jsonb_typeof(config -> 'report_digests') = 'object'",
    *(f"DROP TABLE finance.{name}" for name in _DROPPED),
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
