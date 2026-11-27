"""Binance: the typed tables of the margin wallets (design 5.6, 5.7): the cross and isolated
margin accounts (each a wallet's report), the liquidation loan, the margin trades and the
transfer, borrow and repay, interest, capital flow, forced liquidation, small liability exchange
and liquidation loan repay histories; by the same rule as 0041 (plan Decision 3): a record table
per history label keyed by Binance's id (or a hash of the row and its occurrence), a report table
per account or position label keyed by the read, its arrays child tables, and a `_responses`
table for what an answer says beside its records. A margin transfer keeps the movement it is
linked to (rule 2) and a borrow or repay the `type` it was asked for. The statements were
emitted from the catalogue (`tests/finance/binance/emit_ddl.py`) and are frozen here.

Revision ID: 0050
Revises: 0049
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    CREATE TABLE finance.binance_margin_account (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "created" boolean,
      "borrow_enabled" boolean,
      "margin_level" numeric
        CHECK ("margin_level" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "collateral_margin_level" numeric
        CHECK ("collateral_margin_level" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_asset_of_btc" numeric
        CHECK ("total_asset_of_btc" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_liability_of_btc" numeric
        CHECK ("total_liability_of_btc" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_net_asset_of_btc" numeric
        CHECK ("total_net_asset_of_btc" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_collateral_value_in_usdt" numeric
        CHECK ("total_collateral_value_in_usdt" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "trade_enabled" boolean,
      "transfer_in_enabled" boolean,
      "transfer_out_enabled" boolean,
      "transfer_enabled" boolean,
      "account_type" text,
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_margin_account_user_assets (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "user_assets_ordinal" integer NOT NULL,
      "asset" text NOT NULL,
      "borrowed" numeric
        CHECK ("borrowed" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "free" numeric NOT NULL
        CHECK ("free" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "interest" numeric
        CHECK ("interest" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "locked" numeric NOT NULL
        CHECK ("locked" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "net_asset" numeric
        CHECK ("net_asset" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "snapshot_at", "user_assets_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at")
        REFERENCES finance.binance_margin_account
          ("connection_id", "snapshot_at")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_margin_isolated_account (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "total_asset_of_btc" numeric
        CHECK ("total_asset_of_btc" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_liability_of_btc" numeric
        CHECK ("total_liability_of_btc" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_net_asset_of_btc" numeric
        CHECK ("total_net_asset_of_btc" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_margin_isolated_account_assets (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "assets_ordinal" integer NOT NULL,
      "base_asset_asset" text,
      "base_asset_borrow_enabled" boolean,
      "base_asset_borrowed" numeric
        CHECK ("base_asset_borrowed" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "base_asset_free" numeric
        CHECK ("base_asset_free" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "base_asset_interest" numeric
        CHECK ("base_asset_interest" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "base_asset_locked" numeric
        CHECK ("base_asset_locked" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "base_asset_net_asset" numeric
        CHECK ("base_asset_net_asset" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "base_asset_net_asset_of_btc" numeric
        CHECK ("base_asset_net_asset_of_btc" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "base_asset_repay_enabled" boolean,
      "base_asset_total_asset" numeric
        CHECK ("base_asset_total_asset" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "quote_asset_asset" text,
      "quote_asset_borrow_enabled" boolean,
      "quote_asset_borrowed" numeric
        CHECK ("quote_asset_borrowed" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "quote_asset_free" numeric
        CHECK ("quote_asset_free" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "quote_asset_interest" numeric
        CHECK ("quote_asset_interest" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "quote_asset_locked" numeric
        CHECK ("quote_asset_locked" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "quote_asset_net_asset" numeric
        CHECK ("quote_asset_net_asset" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "quote_asset_net_asset_of_btc" numeric
        CHECK ("quote_asset_net_asset_of_btc" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "quote_asset_repay_enabled" boolean,
      "quote_asset_total_asset" numeric
        CHECK ("quote_asset_total_asset" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "symbol" text NOT NULL,
      "isolated_created" boolean,
      "enabled" boolean,
      "margin_level" numeric
        CHECK ("margin_level" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "margin_level_status" text,
      "margin_ratio" numeric
        CHECK ("margin_ratio" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "index_price" numeric
        CHECK ("index_price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "liquidate_price" numeric
        CHECK ("liquidate_price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "liquidate_rate" numeric
        CHECK ("liquidate_rate" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "trade_enabled" boolean,
      PRIMARY KEY ("connection_id", "snapshot_at", "assets_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at")
        REFERENCES finance.binance_margin_isolated_account
          ("connection_id", "snapshot_at")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_margin_trades (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "commission" numeric NOT NULL
        CHECK ("commission" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "commission_asset" text NOT NULL,
      "id" text NOT NULL,
      "is_best_match" boolean,
      "is_buyer" boolean,
      "is_maker" boolean,
      "order_id" text,
      "price" numeric NOT NULL
        CHECK ("price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "qty" numeric NOT NULL
        CHECK ("qty" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "symbol" text NOT NULL,
      "is_isolated" boolean,
      "time" timestamptz NOT NULL,
      PRIMARY KEY ("connection_id", "symbol", "id")
    )
    """,
    """
    CREATE INDEX binance_margin_trades_transaction_idx
      ON finance.binance_margin_trades (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_margin_transfers (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "asset" text NOT NULL,
      "status" text,
      "timestamp" timestamptz NOT NULL,
      "tx_id" text NOT NULL,
      "type" text,
      "trans_from" text,
      "trans_to" text,
      "from_symbol" text,
      "to_symbol" text,
      "movement_key" text,
      PRIMARY KEY ("connection_id", "tx_id")
    )
    """,
    """
    CREATE INDEX binance_margin_transfers_transaction_idx
      ON finance.binance_margin_transfers (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_margin_transfers_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_margin_borrow_repay (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "type" text,
      "isolated_symbol" text,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "asset" text NOT NULL,
      "interest" numeric
        CHECK ("interest" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "principal" numeric
        CHECK ("principal" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "status" text,
      "timestamp" timestamptz NOT NULL,
      "tx_id" text NOT NULL,
      "request_type" text,
      PRIMARY KEY ("connection_id", "tx_id")
    )
    """,
    """
    CREATE INDEX binance_margin_borrow_repay_transaction_idx
      ON finance.binance_margin_borrow_repay (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_margin_borrow_repay_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_margin_interest (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "tx_id" text NOT NULL,
      "interest_accured_time" timestamptz NOT NULL,
      "asset" text NOT NULL,
      "raw_asset" text,
      "principal" numeric
        CHECK ("principal" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "interest" numeric
        CHECK ("interest" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "interest_rate" numeric
        CHECK ("interest_rate" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "type" text,
      "isolated_symbol" text,
      PRIMARY KEY ("connection_id", "tx_id", "interest_accured_time", "asset")
    )
    """,
    """
    CREATE INDEX binance_margin_interest_transaction_idx
      ON finance.binance_margin_interest (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_margin_interest_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_margin_capital_flow (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "id" text NOT NULL,
      "tran_id" text,
      "timestamp" timestamptz NOT NULL,
      "asset" text NOT NULL,
      "symbol" text,
      "type" text,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "note" text,
      PRIMARY KEY ("connection_id", "id")
    )
    """,
    """
    CREATE INDEX binance_margin_capital_flow_transaction_idx
      ON finance.binance_margin_capital_flow (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_margin_liquidations (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "avg_price" numeric
        CHECK ("avg_price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "executed_qty" numeric
        CHECK ("executed_qty" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "order_id" text NOT NULL,
      "price" numeric
        CHECK ("price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "qty" numeric
        CHECK ("qty" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "side" text,
      "symbol" text,
      "time_in_force" text,
      "is_isolated" boolean,
      "updated_time" timestamptz NOT NULL,
      PRIMARY KEY ("connection_id", "order_id")
    )
    """,
    """
    CREATE INDEX binance_margin_liquidations_transaction_idx
      ON finance.binance_margin_liquidations (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_margin_liquidations_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_margin_small_liability (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "content_hash" text NOT NULL,
      "occurrence" integer NOT NULL,
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "asset" text NOT NULL,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "target_asset" text,
      "target_amount" numeric
        CHECK ("target_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "biz_type" text,
      "timestamp" timestamptz NOT NULL,
      PRIMARY KEY ("connection_id", "content_hash", "occurrence")
    )
    """,
    """
    CREATE INDEX binance_margin_small_liability_transaction_idx
      ON finance.binance_margin_small_liability (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_margin_small_liability_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_margin_liquidation_loan (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "asset" text,
      "amount" numeric
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "repaid_amount" numeric
        CHECK ("repaid_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "remaining_amount" numeric
        CHECK ("remaining_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_margin_liquidation_loan_repays (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "repay_id" text NOT NULL,
      "asset" text NOT NULL,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "status" text,
      "create_time" timestamptz NOT NULL,
      PRIMARY KEY ("connection_id", "repay_id")
    )
    """,
    """
    CREATE INDEX binance_margin_liquidation_loan_repays_transaction_idx
      ON finance.binance_margin_liquidation_loan_repays (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_margin_liquidation_loan_repays_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
]

_DROPPED = [
    "binance_margin_liquidation_loan_repays_responses",
    "binance_margin_liquidation_loan_repays",
    "binance_margin_liquidation_loan",
    "binance_margin_small_liability_responses",
    "binance_margin_small_liability",
    "binance_margin_liquidations_responses",
    "binance_margin_liquidations",
    "binance_margin_capital_flow",
    "binance_margin_interest_responses",
    "binance_margin_interest",
    "binance_margin_borrow_repay_responses",
    "binance_margin_borrow_repay",
    "binance_margin_transfers_responses",
    "binance_margin_transfers",
    "binance_margin_trades",
    "binance_margin_isolated_account_assets",
    "binance_margin_isolated_account",
    "binance_margin_account_user_assets",
    "binance_margin_account",
]
_REPORTS = [
    "margin-account",
    "margin-isolated-account",
    "margin-liquidation-loan",
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
