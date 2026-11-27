"""Binance: the typed tables of the futures wallets (design 5.6, 5.7, 5.11): USDⓈ-M's and
COIN-M's balances, accounts (each a wallet's report; Decision 11 reconciles on the accounts'
`wallet_balance`) and positions, their income and trade histories, and each futures host's
exchange information (its rate limits, plan Decision 4, and its symbols); by the same rule as
0041 (plan Decision 3): a record table per history label keyed by Binance's id, a report table
per account, balance, position or public label keyed by the read, its arrays child tables. An
income row keeps the movement it is linked to (rule 2). The statements were emitted from the
catalogue (`tests/finance/binance/emit_ddl.py`) and are frozen here.

Revision ID: 0051
Revises: 0050
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    CREATE TABLE finance.binance_um_exchange_info (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "timezone" text,
      "server_time" timestamptz,
      "exchange_filters" text[],
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_um_exchange_info_assets (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "assets_ordinal" integer NOT NULL,
      "asset" text NOT NULL,
      "margin_available" boolean,
      "auto_asset_exchange" numeric
        CHECK ("auto_asset_exchange" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "snapshot_at", "assets_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at")
        REFERENCES finance.binance_um_exchange_info
          ("connection_id", "snapshot_at")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_um_exchange_info_rate_limits (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "rate_limits_ordinal" integer NOT NULL,
      "rate_limit_type" text,
      "interval" text,
      "interval_num" bigint,
      "limit" bigint,
      PRIMARY KEY ("connection_id", "snapshot_at", "rate_limits_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at")
        REFERENCES finance.binance_um_exchange_info
          ("connection_id", "snapshot_at")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_um_exchange_info_symbols (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "symbols_ordinal" integer NOT NULL,
      "symbol" text NOT NULL,
      "pair" text,
      "contract_type" text,
      "delivery_date" timestamptz,
      "onboard_date" timestamptz,
      "status" text,
      "maint_margin_percent" numeric
        CHECK ("maint_margin_percent" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "required_margin_percent" numeric
        CHECK ("required_margin_percent" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "base_asset" text,
      "quote_asset" text,
      "margin_asset" text,
      "price_precision" bigint,
      "quantity_precision" bigint,
      "base_asset_precision" bigint,
      "quote_precision" bigint,
      "underlying_type" text,
      "underlying_sub_type" text[],
      "settle_plan" bigint,
      "trigger_protect" numeric
        CHECK ("trigger_protect" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "order_types" text[],
      "time_in_force" text[],
      "liquidation_fee" numeric
        CHECK ("liquidation_fee" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "market_take_bound" numeric
        CHECK ("market_take_bound" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "snapshot_at", "symbols_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at")
        REFERENCES finance.binance_um_exchange_info
          ("connection_id", "snapshot_at")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_um_exchange_info_symbols_filters (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "symbols_ordinal" integer NOT NULL,
      "filters_ordinal" integer NOT NULL,
      "filter_type" text,
      PRIMARY KEY ("connection_id", "snapshot_at", "symbols_ordinal", "filters_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at", "symbols_ordinal")
        REFERENCES finance.binance_um_exchange_info_symbols
          ("connection_id", "snapshot_at", "symbols_ordinal")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_um_balance (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "account_alias" text,
      "asset" text NOT NULL,
      "balance" numeric NOT NULL
        CHECK ("balance" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "cross_wallet_balance" numeric
        CHECK ("cross_wallet_balance" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "cross_un_pnl" numeric
        CHECK ("cross_un_pnl" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "available_balance" numeric
        CHECK ("available_balance" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "max_withdraw_amount" numeric
        CHECK ("max_withdraw_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "margin_available" boolean,
      "update_time" timestamptz NOT NULL,
      PRIMARY KEY ("connection_id", "snapshot_at", "asset")
    )
    """,
    """
    CREATE TABLE finance.binance_um_account (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "total_initial_margin" numeric
        CHECK ("total_initial_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_maint_margin" numeric
        CHECK ("total_maint_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_wallet_balance" numeric
        CHECK ("total_wallet_balance" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_unrealized_profit" numeric
        CHECK ("total_unrealized_profit" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_margin_balance" numeric
        CHECK ("total_margin_balance" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_position_initial_margin" numeric
        CHECK ("total_position_initial_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_open_order_initial_margin" numeric
        CHECK ("total_open_order_initial_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_cross_wallet_balance" numeric
        CHECK ("total_cross_wallet_balance" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_cross_un_pnl" numeric
        CHECK ("total_cross_un_pnl" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "available_balance" numeric
        CHECK ("available_balance" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "max_withdraw_amount" numeric
        CHECK ("max_withdraw_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_um_account_assets (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "assets_ordinal" integer NOT NULL,
      "asset" text NOT NULL,
      "wallet_balance" numeric NOT NULL
        CHECK ("wallet_balance" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "unrealized_profit" numeric
        CHECK ("unrealized_profit" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "margin_balance" numeric
        CHECK ("margin_balance" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "maint_margin" numeric
        CHECK ("maint_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "initial_margin" numeric
        CHECK ("initial_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "position_initial_margin" numeric
        CHECK ("position_initial_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "open_order_initial_margin" numeric
        CHECK ("open_order_initial_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "cross_wallet_balance" numeric
        CHECK ("cross_wallet_balance" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "cross_un_pnl" numeric
        CHECK ("cross_un_pnl" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "available_balance" numeric
        CHECK ("available_balance" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "max_withdraw_amount" numeric
        CHECK ("max_withdraw_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "update_time" timestamptz NOT NULL,
      PRIMARY KEY ("connection_id", "snapshot_at", "assets_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at")
        REFERENCES finance.binance_um_account
          ("connection_id", "snapshot_at")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_um_account_positions (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "positions_ordinal" integer NOT NULL,
      "symbol" text,
      "position_side" text,
      "position_amt" numeric
        CHECK ("position_amt" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "unrealized_profit" numeric
        CHECK ("unrealized_profit" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "isolated_margin" numeric
        CHECK ("isolated_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "notional" numeric
        CHECK ("notional" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "isolated_wallet" numeric
        CHECK ("isolated_wallet" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "initial_margin" numeric
        CHECK ("initial_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "maint_margin" numeric
        CHECK ("maint_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "update_time" timestamptz,
      PRIMARY KEY ("connection_id", "snapshot_at", "positions_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at")
        REFERENCES finance.binance_um_account
          ("connection_id", "snapshot_at")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_um_positions (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "symbol" text NOT NULL,
      "position_side" text NOT NULL,
      "position_amt" numeric NOT NULL
        CHECK ("position_amt" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "entry_price" numeric
        CHECK ("entry_price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "break_even_price" numeric
        CHECK ("break_even_price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "mark_price" numeric
        CHECK ("mark_price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "un_realized_profit" numeric
        CHECK ("un_realized_profit" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "liquidation_price" numeric
        CHECK ("liquidation_price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "isolated_margin" numeric
        CHECK ("isolated_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "notional" numeric
        CHECK ("notional" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "margin_asset" text,
      "isolated_wallet" numeric
        CHECK ("isolated_wallet" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "initial_margin" numeric
        CHECK ("initial_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "maint_margin" numeric
        CHECK ("maint_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "position_initial_margin" numeric
        CHECK ("position_initial_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "open_order_initial_margin" numeric
        CHECK ("open_order_initial_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "adl" bigint,
      "bid_notional" numeric
        CHECK ("bid_notional" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "ask_notional" numeric
        CHECK ("ask_notional" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "update_time" timestamptz NOT NULL,
      PRIMARY KEY ("connection_id", "snapshot_at", "symbol", "position_side")
    )
    """,
    """
    CREATE TABLE finance.binance_um_income (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "symbol" text,
      "income_type" text NOT NULL,
      "income" numeric NOT NULL
        CHECK ("income" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "asset" text NOT NULL,
      "info" text,
      "time" timestamptz NOT NULL,
      "tran_id" text NOT NULL,
      "trade_id" text,
      "movement_key" text,
      PRIMARY KEY ("connection_id", "income_type", "tran_id")
    )
    """,
    """
    CREATE INDEX binance_um_income_transaction_idx
      ON finance.binance_um_income (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_um_trades (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "buyer" boolean,
      "commission" numeric NOT NULL
        CHECK ("commission" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "commission_asset" text NOT NULL,
      "id" text NOT NULL,
      "maker" boolean,
      "order_id" text,
      "price" numeric NOT NULL
        CHECK ("price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "qty" numeric NOT NULL
        CHECK ("qty" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "quote_qty" numeric NOT NULL
        CHECK ("quote_qty" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "base_qty" numeric
        CHECK ("base_qty" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "margin_asset" text,
      "realized_pnl" numeric
        CHECK ("realized_pnl" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "side" text,
      "position_side" text,
      "symbol" text NOT NULL,
      "pair" text,
      "time" timestamptz NOT NULL,
      PRIMARY KEY ("connection_id", "symbol", "id")
    )
    """,
    """
    CREATE INDEX binance_um_trades_transaction_idx
      ON finance.binance_um_trades (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_cm_exchange_info (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "timezone" text,
      "server_time" timestamptz,
      "exchange_filters" text[],
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_cm_exchange_info_rate_limits (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "rate_limits_ordinal" integer NOT NULL,
      "rate_limit_type" text,
      "interval" text,
      "interval_num" bigint,
      "limit" bigint,
      PRIMARY KEY ("connection_id", "snapshot_at", "rate_limits_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at")
        REFERENCES finance.binance_cm_exchange_info
          ("connection_id", "snapshot_at")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_cm_exchange_info_symbols (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "symbols_ordinal" integer NOT NULL,
      "symbol" text NOT NULL,
      "pair" text,
      "contract_type" text,
      "delivery_date" timestamptz,
      "onboard_date" timestamptz,
      "contract_status" text,
      "contract_size" bigint,
      "base_asset" text,
      "quote_asset" text,
      "margin_asset" text,
      "price_precision" bigint,
      "quantity_precision" bigint,
      "base_asset_precision" bigint,
      "quote_precision" bigint,
      "equal_qty_precision" bigint,
      "trigger_protect" numeric
        CHECK ("trigger_protect" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "maint_margin_percent" numeric
        CHECK ("maint_margin_percent" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "required_margin_percent" numeric
        CHECK ("required_margin_percent" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "underlying_type" text,
      "underlying_sub_type" text[],
      "order_types" text[],
      "time_in_force" text[],
      "liquidation_fee" numeric
        CHECK ("liquidation_fee" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "market_take_bound" numeric
        CHECK ("market_take_bound" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "snapshot_at", "symbols_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at")
        REFERENCES finance.binance_cm_exchange_info
          ("connection_id", "snapshot_at")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_cm_exchange_info_symbols_filters (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "symbols_ordinal" integer NOT NULL,
      "filters_ordinal" integer NOT NULL,
      "filter_type" text,
      PRIMARY KEY ("connection_id", "snapshot_at", "symbols_ordinal", "filters_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at", "symbols_ordinal")
        REFERENCES finance.binance_cm_exchange_info_symbols
          ("connection_id", "snapshot_at", "symbols_ordinal")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_cm_balance (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "account_alias" text,
      "asset" text NOT NULL,
      "balance" numeric NOT NULL
        CHECK ("balance" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "withdraw_available" numeric
        CHECK ("withdraw_available" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "cross_wallet_balance" numeric
        CHECK ("cross_wallet_balance" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "cross_un_pnl" numeric
        CHECK ("cross_un_pnl" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "available_balance" numeric
        CHECK ("available_balance" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "update_time" timestamptz NOT NULL,
      PRIMARY KEY ("connection_id", "snapshot_at", "asset")
    )
    """,
    """
    CREATE TABLE finance.binance_cm_account (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "can_deposit" boolean,
      "can_trade" boolean,
      "can_withdraw" boolean,
      "fee_tier" bigint,
      "update_time" timestamptz,
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_cm_account_assets (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "assets_ordinal" integer NOT NULL,
      "asset" text NOT NULL,
      "wallet_balance" numeric NOT NULL
        CHECK ("wallet_balance" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "unrealized_profit" numeric
        CHECK ("unrealized_profit" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "margin_balance" numeric
        CHECK ("margin_balance" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "maint_margin" numeric
        CHECK ("maint_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "initial_margin" numeric
        CHECK ("initial_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "position_initial_margin" numeric
        CHECK ("position_initial_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "open_order_initial_margin" numeric
        CHECK ("open_order_initial_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "max_withdraw_amount" numeric
        CHECK ("max_withdraw_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "cross_wallet_balance" numeric
        CHECK ("cross_wallet_balance" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "cross_un_pnl" numeric
        CHECK ("cross_un_pnl" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "available_balance" numeric
        CHECK ("available_balance" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "update_time" timestamptz NOT NULL,
      PRIMARY KEY ("connection_id", "snapshot_at", "assets_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at")
        REFERENCES finance.binance_cm_account
          ("connection_id", "snapshot_at")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_cm_account_positions (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "positions_ordinal" integer NOT NULL,
      "symbol" text,
      "position_amt" numeric
        CHECK ("position_amt" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "initial_margin" numeric
        CHECK ("initial_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "maint_margin" numeric
        CHECK ("maint_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "unrealized_profit" numeric
        CHECK ("unrealized_profit" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "position_initial_margin" numeric
        CHECK ("position_initial_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "open_order_initial_margin" numeric
        CHECK ("open_order_initial_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "leverage" text,
      "isolated" boolean,
      "position_side" text,
      "entry_price" numeric
        CHECK ("entry_price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "break_even_price" numeric
        CHECK ("break_even_price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "max_qty" numeric
        CHECK ("max_qty" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "update_time" timestamptz,
      "notional_value" numeric
        CHECK ("notional_value" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "isolated_wallet" numeric
        CHECK ("isolated_wallet" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "snapshot_at", "positions_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at")
        REFERENCES finance.binance_cm_account
          ("connection_id", "snapshot_at")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_cm_positions (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "symbol" text NOT NULL,
      "position_amt" numeric NOT NULL
        CHECK ("position_amt" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "entry_price" numeric
        CHECK ("entry_price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "break_even_price" numeric
        CHECK ("break_even_price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "mark_price" numeric
        CHECK ("mark_price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "un_realized_profit" numeric
        CHECK ("un_realized_profit" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "liquidation_price" numeric
        CHECK ("liquidation_price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "leverage" text,
      "max_qty" numeric
        CHECK ("max_qty" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "margin_type" text,
      "isolated_margin" numeric
        CHECK ("isolated_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "is_auto_add_margin" text,
      "position_side" text NOT NULL,
      "update_time" timestamptz NOT NULL,
      "isolated_wallet" numeric
        CHECK ("isolated_wallet" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "notional_value" numeric
        CHECK ("notional_value" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "snapshot_at", "symbol", "position_side")
    )
    """,
    """
    CREATE TABLE finance.binance_cm_income (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "symbol" text,
      "income_type" text NOT NULL,
      "income" numeric NOT NULL
        CHECK ("income" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "asset" text NOT NULL,
      "info" text,
      "time" timestamptz NOT NULL,
      "tran_id" text NOT NULL,
      "trade_id" text,
      "movement_key" text,
      PRIMARY KEY ("connection_id", "income_type", "tran_id")
    )
    """,
    """
    CREATE INDEX binance_cm_income_transaction_idx
      ON finance.binance_cm_income (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_cm_trades (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "symbol" text NOT NULL,
      "id" text NOT NULL,
      "order_id" text,
      "pair" text,
      "side" text,
      "price" numeric NOT NULL
        CHECK ("price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "qty" numeric NOT NULL
        CHECK ("qty" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "realized_pnl" numeric
        CHECK ("realized_pnl" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "margin_asset" text,
      "base_qty" numeric
        CHECK ("base_qty" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "quote_qty" numeric NOT NULL
        CHECK ("quote_qty" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "commission" numeric NOT NULL
        CHECK ("commission" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "commission_asset" text NOT NULL,
      "time" timestamptz NOT NULL,
      "position_side" text,
      "buyer" boolean,
      "maker" boolean,
      PRIMARY KEY ("connection_id", "symbol", "id")
    )
    """,
    """
    CREATE INDEX binance_cm_trades_transaction_idx
      ON finance.binance_cm_trades (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
]

_DROPPED = [
    "binance_cm_trades",
    "binance_cm_income",
    "binance_cm_positions",
    "binance_cm_account_positions",
    "binance_cm_account_assets",
    "binance_cm_account",
    "binance_cm_balance",
    "binance_cm_exchange_info_symbols_filters",
    "binance_cm_exchange_info_symbols",
    "binance_cm_exchange_info_rate_limits",
    "binance_cm_exchange_info",
    "binance_um_trades",
    "binance_um_income",
    "binance_um_positions",
    "binance_um_account_positions",
    "binance_um_account_assets",
    "binance_um_account",
    "binance_um_balance",
    "binance_um_exchange_info_symbols_filters",
    "binance_um_exchange_info_symbols",
    "binance_um_exchange_info_rate_limits",
    "binance_um_exchange_info_assets",
    "binance_um_exchange_info",
]
_REPORTS = [
    "um-exchange-info",
    "um-balance",
    "um-account",
    "um-positions",
    "cm-exchange-info",
    "cm-balance",
    "cm-account",
    "cm-positions",
]

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
