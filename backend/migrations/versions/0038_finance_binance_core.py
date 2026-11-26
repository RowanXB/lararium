"""Binance: the typed tables of what the API says, the home of its assets, and the identifier
kinds M7 adds.

Every response field of the Phase A endpoints is a typed column of a `finance.binance_*` table
(plan Decision 3, design 5.3): a record table per history label (keyed by Binance's own id and
linked to its ledger transaction), a report table per balance, position, account or reference
label (keyed by `snapshot_at`, with the `read_to` of the read), a child table per array of
objects or map, and a `_responses` table for what a response says beside its records (`total`,
`moreData`: they describe the request and the page, not a row). The CREATE TABLE statements were
emitted once from the catalogue and are frozen here: this migration never imports it.

`binance_assets_seen` is the set of assets a connection has shown, which later syncs use to
choose the trade symbols to ask for. The `binance_asset` instrument scheme and the
`chain_address` account identifier kind are new (design 5.5, 5.12).

Revision ID: 0038
Revises: 0037
Create Date: 2026-11-23
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = [
    """
    CREATE TABLE finance.binance_exchange_info (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "timezone" text,
      "server_time" timestamptz,
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_exchange_info_exchange_filters (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "exchange_filters_ordinal" integer NOT NULL,
      "filter_type" text,
      "max_num_orders" bigint,
      "max_num_algo_orders" bigint,
      "max_num_iceberg_orders" bigint,
      "max_num_order_lists" bigint,
      PRIMARY KEY ("connection_id", "snapshot_at", "exchange_filters_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at")
        REFERENCES finance.binance_exchange_info
          ("connection_id", "snapshot_at")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_exchange_info_rate_limits (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "rate_limits_ordinal" integer NOT NULL,
      "rate_limit_type" text,
      "interval" text,
      "interval_num" bigint,
      "limit" bigint,
      "count" bigint,
      PRIMARY KEY ("connection_id", "snapshot_at", "rate_limits_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at")
        REFERENCES finance.binance_exchange_info
          ("connection_id", "snapshot_at")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_exchange_info_sors (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "sors_ordinal" integer NOT NULL,
      "base_asset" text,
      "symbols" text[],
      PRIMARY KEY ("connection_id", "snapshot_at", "sors_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at")
        REFERENCES finance.binance_exchange_info
          ("connection_id", "snapshot_at")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_exchange_info_symbols (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "symbols_ordinal" integer NOT NULL,
      "symbol" text NOT NULL,
      "status" text,
      "base_asset" text NOT NULL,
      "base_asset_precision" bigint,
      "quote_asset" text NOT NULL,
      "quote_precision" bigint,
      "quote_asset_precision" bigint,
      "base_commission_precision" bigint,
      "quote_commission_precision" bigint,
      "order_types" text[],
      "iceberg_allowed" boolean,
      "oco_allowed" boolean,
      "oto_allowed" boolean,
      "opo_allowed" boolean,
      "quote_order_qty_market_allowed" boolean,
      "allow_trailing_stop" boolean,
      "cancel_replace_allowed" boolean,
      "amend_allowed" boolean,
      "peg_instructions_allowed" boolean,
      "market_quote_notional_limit_allowed" boolean,
      "limit_quote_notional_limit_allowed" boolean,
      "is_spot_trading_allowed" boolean,
      "is_margin_trading_allowed" boolean,
      "permissions" text[],
      "default_self_trade_prevention_mode" text,
      "allowed_self_trade_prevention_modes" text[],
      PRIMARY KEY ("connection_id", "snapshot_at", "symbols_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at")
        REFERENCES finance.binance_exchange_info
          ("connection_id", "snapshot_at")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_exchange_info_symbols_filters (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "symbols_ordinal" integer NOT NULL,
      "filters_ordinal" integer NOT NULL,
      "filter_type" text,
      PRIMARY KEY ("connection_id", "snapshot_at", "symbols_ordinal", "filters_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at", "symbols_ordinal")
        REFERENCES finance.binance_exchange_info_symbols
          ("connection_id", "snapshot_at", "symbols_ordinal")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_exchange_info_symbols_permission_sets (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "symbols_ordinal" integer NOT NULL,
      "permission_sets_ordinal" integer NOT NULL,
      "value" text[],
      PRIMARY KEY ("connection_id", "snapshot_at", "symbols_ordinal", "permission_sets_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at", "symbols_ordinal")
        REFERENCES finance.binance_exchange_info_symbols
          ("connection_id", "snapshot_at", "symbols_ordinal")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_api_restrictions (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "ip_restrict" boolean,
      "create_time" timestamptz,
      "enable_reading" boolean,
      "enable_withdrawals" boolean,
      "enable_internal_transfer" boolean,
      "enable_margin" boolean,
      "enable_futures" boolean,
      "permits_universal_transfer" boolean,
      "enable_vanilla_options" boolean,
      "enable_fix_api_trade" boolean,
      "enable_fix_read_only" boolean,
      "enable_spot_and_margin_trading" boolean,
      "enable_portfolio_margin_trading" boolean,
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_account_info (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "vip_level" bigint NOT NULL,
      "is_margin_enabled" boolean NOT NULL,
      "is_future_enabled" boolean NOT NULL,
      "is_options_enabled" boolean NOT NULL,
      "is_portfolio_margin_retail_enabled" boolean NOT NULL,
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_account_status (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "data" text NOT NULL,
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_api_trading_status (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "data_is_locked" boolean,
      "data_planned_recover_time" timestamptz,
      "data_trigger_condition_gcr" bigint,
      "data_trigger_condition_ifer" bigint,
      "data_trigger_condition_ufr" bigint,
      "data_update_time" timestamptz,
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_spot_account (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "maker_commission" bigint,
      "taker_commission" bigint,
      "buyer_commission" bigint,
      "seller_commission" bigint,
      "commission_rates_maker" numeric
        CHECK ("commission_rates_maker" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "commission_rates_taker" numeric
        CHECK ("commission_rates_taker" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "commission_rates_buyer" numeric
        CHECK ("commission_rates_buyer" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "commission_rates_seller" numeric
        CHECK ("commission_rates_seller" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "can_trade" boolean,
      "can_withdraw" boolean,
      "can_deposit" boolean,
      "brokered" boolean,
      "require_self_trade_prevention" boolean,
      "prevent_sor" boolean,
      "update_time" timestamptz,
      "account_type" text,
      "permissions" text[],
      "uid" text,
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_spot_account_balances (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "balances_ordinal" integer NOT NULL,
      "asset" text NOT NULL,
      "free" numeric NOT NULL
        CHECK ("free" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "locked" numeric NOT NULL
        CHECK ("locked" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "snapshot_at", "balances_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at")
        REFERENCES finance.binance_spot_account
          ("connection_id", "snapshot_at")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_user_asset (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "asset" text NOT NULL,
      "free" numeric NOT NULL
        CHECK ("free" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "locked" numeric NOT NULL
        CHECK ("locked" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "freeze" numeric
        CHECK ("freeze" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "withdrawing" numeric
        CHECK ("withdrawing" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "ipoable" numeric
        CHECK ("ipoable" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "btc_valuation" numeric
        CHECK ("btc_valuation" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "snapshot_at", "asset")
    )
    """,
    """
    CREATE TABLE finance.binance_funding_asset (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "asset" text NOT NULL,
      "free" numeric NOT NULL
        CHECK ("free" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "locked" numeric NOT NULL
        CHECK ("locked" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "freeze" numeric
        CHECK ("freeze" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "withdrawing" numeric
        CHECK ("withdrawing" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "btc_valuation" numeric
        CHECK ("btc_valuation" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "snapshot_at", "asset")
    )
    """,
    """
    CREATE TABLE finance.binance_wallet_balance (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "activate" boolean,
      "balance" numeric NOT NULL
        CHECK ("balance" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "wallet_name" text NOT NULL,
      PRIMARY KEY ("connection_id", "snapshot_at", "wallet_name")
    )
    """,
    """
    CREATE TABLE finance.binance_wallet_balance_asset_balances (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "wallet_name" text NOT NULL,
      "asset_balances_ordinal" integer NOT NULL,
      "asset" text,
      "asset_name" text,
      "free" numeric
        CHECK ("free" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "locked" numeric
        CHECK ("locked" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "freeze" numeric
        CHECK ("freeze" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "withdrawing" numeric
        CHECK ("withdrawing" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "btc_valuation" numeric
        CHECK ("btc_valuation" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "snapshot_at", "wallet_name", "asset_balances_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at", "wallet_name")
        REFERENCES finance.binance_wallet_balance
          ("connection_id", "snapshot_at", "wallet_name")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_earn_account (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "total_amount_in_btc" numeric NOT NULL
        CHECK ("total_amount_in_btc" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_amount_in_usdt" numeric NOT NULL
        CHECK ("total_amount_in_usdt" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_flexible_amount_in_btc" numeric NOT NULL
        CHECK ("total_flexible_amount_in_btc" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_flexible_amount_in_usdt" numeric NOT NULL
        CHECK ("total_flexible_amount_in_usdt" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_locked_in_btc" numeric NOT NULL
        CHECK ("total_locked_in_btc" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_locked_in_usdt" numeric NOT NULL
        CHECK ("total_locked_in_usdt" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_earn_flexible_position (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "total_amount" numeric
        CHECK ("total_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "latest_annual_percentage_rate" numeric
        CHECK ("latest_annual_percentage_rate" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "yesterday_airdrop_percentage_rate" numeric
        CHECK ("yesterday_airdrop_percentage_rate" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "asset" text NOT NULL,
      "air_drop_asset" text,
      "can_redeem" boolean,
      "collateral_amount" numeric
        CHECK ("collateral_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "product_id" text NOT NULL,
      "yesterday_real_time_rewards" numeric
        CHECK ("yesterday_real_time_rewards" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "cumulative_bonus_rewards" numeric
        CHECK ("cumulative_bonus_rewards" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "cumulative_real_time_rewards" numeric
        CHECK ("cumulative_real_time_rewards" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "cumulative_total_rewards" numeric
        CHECK ("cumulative_total_rewards" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "auto_subscribe" boolean,
      PRIMARY KEY ("connection_id", "snapshot_at", "product_id")
    )
    """,
    """
    CREATE TABLE finance.binance_earn_flexible_position_tier_annual_percentage_rate (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "product_id" text NOT NULL,
      "tier" text NOT NULL,
      "value" numeric
        CHECK ("value" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "snapshot_at", "product_id", "tier"),
      FOREIGN KEY ("connection_id", "snapshot_at", "product_id")
        REFERENCES finance.binance_earn_flexible_position
          ("connection_id", "snapshot_at", "product_id")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_earn_flexible_position_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_snapshot (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "type" text NOT NULL,
      "update_time" timestamptz NOT NULL,
      "data_total_asset_of_btc" numeric
        CHECK ("data_total_asset_of_btc" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "data_margin_level" numeric
        CHECK ("data_margin_level" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "data_total_liability_of_btc" numeric
        CHECK ("data_total_liability_of_btc" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "data_total_net_asset_of_btc" numeric
        CHECK ("data_total_net_asset_of_btc" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "type", "update_time")
    )
    """,
    """
    CREATE INDEX binance_snapshot_transaction_idx
      ON finance.binance_snapshot (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_snapshot_data_assets (
      "connection_id" uuid NOT NULL,
      "type" text NOT NULL,
      "update_time" timestamptz NOT NULL,
      "data_assets_ordinal" integer NOT NULL,
      "asset" text,
      "margin_balance" numeric
        CHECK ("margin_balance" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "wallet_balance" numeric
        CHECK ("wallet_balance" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "type", "update_time", "data_assets_ordinal"),
      FOREIGN KEY ("connection_id", "type", "update_time")
        REFERENCES finance.binance_snapshot
          ("connection_id", "type", "update_time")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_snapshot_data_balances (
      "connection_id" uuid NOT NULL,
      "type" text NOT NULL,
      "update_time" timestamptz NOT NULL,
      "data_balances_ordinal" integer NOT NULL,
      "asset" text,
      "free" numeric
        CHECK ("free" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "locked" numeric
        CHECK ("locked" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "type", "update_time", "data_balances_ordinal"),
      FOREIGN KEY ("connection_id", "type", "update_time")
        REFERENCES finance.binance_snapshot
          ("connection_id", "type", "update_time")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_snapshot_data_position (
      "connection_id" uuid NOT NULL,
      "type" text NOT NULL,
      "update_time" timestamptz NOT NULL,
      "data_position_ordinal" integer NOT NULL,
      "entry_price" numeric
        CHECK ("entry_price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "mark_price" numeric
        CHECK ("mark_price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "position_amt" numeric
        CHECK ("position_amt" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "symbol" text,
      "un_realized_profit" numeric
        CHECK ("un_realized_profit" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "type", "update_time", "data_position_ordinal"),
      FOREIGN KEY ("connection_id", "type", "update_time")
        REFERENCES finance.binance_snapshot
          ("connection_id", "type", "update_time")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_snapshot_data_user_assets (
      "connection_id" uuid NOT NULL,
      "type" text NOT NULL,
      "update_time" timestamptz NOT NULL,
      "data_user_assets_ordinal" integer NOT NULL,
      "asset" text,
      "borrowed" numeric
        CHECK ("borrowed" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "free" numeric
        CHECK ("free" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "interest" numeric
        CHECK ("interest" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "locked" numeric
        CHECK ("locked" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "net_asset" numeric
        CHECK ("net_asset" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "type", "update_time", "data_user_assets_ordinal"),
      FOREIGN KEY ("connection_id", "type", "update_time")
        REFERENCES finance.binance_snapshot
          ("connection_id", "type", "update_time")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_snapshot_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "code" bigint,
      "msg" text,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_coin_config (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "coin" text NOT NULL,
      "deposit_all_enable" boolean,
      "withdraw_all_enable" boolean,
      "name" text,
      "free" numeric
        CHECK ("free" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "locked" numeric
        CHECK ("locked" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "freeze" numeric
        CHECK ("freeze" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "withdrawing" numeric
        CHECK ("withdrawing" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "ipoing" numeric
        CHECK ("ipoing" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "ipoable" numeric
        CHECK ("ipoable" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "storage" numeric
        CHECK ("storage" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "is_legal_money" boolean,
      "trading" boolean,
      PRIMARY KEY ("connection_id", "snapshot_at", "coin")
    )
    """,
    """
    CREATE TABLE finance.binance_coin_config_network_list (
      "connection_id" uuid NOT NULL,
      "snapshot_at" timestamptz NOT NULL,
      "coin" text NOT NULL,
      "network_list_ordinal" integer NOT NULL,
      "network" text,
      "network_list_coin" text,
      "withdraw_integer_multiple" numeric
        CHECK ("withdraw_integer_multiple" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "is_default" boolean,
      "deposit_enable" boolean,
      "withdraw_enable" boolean,
      "deposit_desc" text,
      "withdraw_desc" text,
      "special_tips" text,
      "special_withdraw_tips" text,
      "name" text,
      "reset_address_status" boolean,
      "address_regex" text,
      "memo_regex" text,
      "withdraw_fee" numeric
        CHECK ("withdraw_fee" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "withdraw_min" numeric
        CHECK ("withdraw_min" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "withdraw_max" numeric
        CHECK ("withdraw_max" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "withdraw_internal_min" numeric
        CHECK ("withdraw_internal_min" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "deposit_dust" numeric
        CHECK ("deposit_dust" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "min_confirm" bigint,
      "un_lock_confirm" bigint,
      "same_address" boolean,
      "withdraw_tag" boolean,
      "estimated_arrival_time" bigint,
      "busy" boolean,
      "contract_address_url" text,
      "contract_address" text,
      "denomination" bigint,
      PRIMARY KEY ("connection_id", "snapshot_at", "coin", "network_list_ordinal"),
      FOREIGN KEY ("connection_id", "snapshot_at", "coin")
        REFERENCES finance.binance_coin_config
          ("connection_id", "snapshot_at", "coin")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_asset_detail (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "asset" text NOT NULL,
      "read_to" timestamptz NOT NULL,
      "min_withdraw_amount" numeric
        CHECK ("min_withdraw_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "deposit_status" boolean,
      "withdraw_fee" numeric
        CHECK ("withdraw_fee" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "withdraw_status" boolean,
      "deposit_tip" text,
      PRIMARY KEY ("connection_id", "snapshot_at", "asset")
    )
    """,
    """
    CREATE TABLE finance.binance_deposits (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "id" text NOT NULL,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "coin" text NOT NULL,
      "network" text,
      "status" bigint,
      "address" text,
      "address_tag" text,
      "tx_id" text,
      "insert_time" timestamptz NOT NULL,
      "complete_time" timestamptz,
      "transfer_type" bigint,
      "confirm_times" text,
      "unlock_confirm" bigint,
      "wallet_type" bigint,
      "travel_rule_status" bigint,
      "source_address" text,
      "tx_hash" text,
      "is_onchain" boolean,
      PRIMARY KEY ("connection_id", "id")
    )
    """,
    """
    CREATE INDEX binance_deposits_transaction_idx
      ON finance.binance_deposits (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE INDEX binance_deposits_network_tx_hash_idx
      ON finance.binance_deposits ("network", "tx_hash")
    """,
    """
    CREATE TABLE finance.binance_withdrawals (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "id" text NOT NULL,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "transaction_fee" numeric
        CHECK ("transaction_fee" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "coin" text NOT NULL,
      "status" bigint,
      "address" text,
      "tx_id" text,
      "apply_time" text NOT NULL,
      "apply_time_at" timestamptz NOT NULL,
      "network" text,
      "transfer_type" bigint,
      "withdraw_order_id" text,
      "info" text,
      "confirm_no" bigint,
      "wallet_type" bigint,
      "tx_key" text,
      "complete_time" text,
      "complete_time_at" timestamptz,
      "tx_hash" text,
      "is_onchain" boolean,
      PRIMARY KEY ("connection_id", "id")
    )
    """,
    """
    CREATE INDEX binance_withdrawals_transaction_idx
      ON finance.binance_withdrawals (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE INDEX binance_withdrawals_network_tx_hash_idx
      ON finance.binance_withdrawals ("network", "tx_hash")
    """,
    """
    CREATE TABLE finance.binance_convert (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "quote_id" text,
      "order_id" text NOT NULL,
      "order_status" text,
      "from_asset" text NOT NULL,
      "from_amount" numeric NOT NULL
        CHECK ("from_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "to_asset" text NOT NULL,
      "to_amount" numeric NOT NULL
        CHECK ("to_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "ratio" numeric
        CHECK ("ratio" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "inverse_ratio" numeric
        CHECK ("inverse_ratio" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "create_time" timestamptz NOT NULL,
      "order_type" text,
      "side" text,
      "wallet_type" text,
      PRIMARY KEY ("connection_id", "order_id")
    )
    """,
    """
    CREATE INDEX binance_convert_transaction_idx
      ON finance.binance_convert (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_convert_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "start_time" timestamptz,
      "end_time" timestamptz,
      "limit" bigint,
      "more_data" boolean,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_dividends (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "id" text NOT NULL,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "asset" text NOT NULL,
      "div_time" timestamptz NOT NULL,
      "en_info" text,
      "tran_id" text,
      "direction" bigint,
      PRIMARY KEY ("connection_id", "id")
    )
    """,
    """
    CREATE INDEX binance_dividends_transaction_idx
      ON finance.binance_dividends (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_dividends_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_earn_flexible_subscriptions (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "asset" text NOT NULL,
      "time" timestamptz NOT NULL,
      "purchase_id" text NOT NULL,
      "product_id" text,
      "type" text,
      "source_account" text,
      "amt_from_spot" numeric
        CHECK ("amt_from_spot" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "amt_from_funding" numeric
        CHECK ("amt_from_funding" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "status" text,
      PRIMARY KEY ("connection_id", "purchase_id")
    )
    """,
    """
    CREATE INDEX binance_earn_flexible_subscriptions_transaction_idx
      ON finance.binance_earn_flexible_subscriptions (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_earn_flexible_subscriptions_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_earn_flexible_redemptions (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "asset" text NOT NULL,
      "time" timestamptz NOT NULL,
      "product_id" text,
      "redeem_id" text NOT NULL,
      "dest_account" text,
      "status" text,
      PRIMARY KEY ("connection_id", "redeem_id")
    )
    """,
    """
    CREATE INDEX binance_earn_flexible_redemptions_transaction_idx
      ON finance.binance_earn_flexible_redemptions (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_earn_flexible_redemptions_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_earn_flexible_rewards (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "content_hash" text NOT NULL,
      "occurrence" integer NOT NULL,
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "asset" text NOT NULL,
      "rewards" numeric NOT NULL
        CHECK ("rewards" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "product_id" text,
      "type" text,
      "time" timestamptz NOT NULL,
      "dividend_id" text,
      PRIMARY KEY ("connection_id", "content_hash", "occurrence")
    )
    """,
    """
    CREATE INDEX binance_earn_flexible_rewards_transaction_idx
      ON finance.binance_earn_flexible_rewards (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_earn_flexible_rewards_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_my_trades (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "symbol" text NOT NULL,
      "id" text NOT NULL,
      "order_id" text,
      "order_list_id" text,
      "price" numeric NOT NULL
        CHECK ("price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "qty" numeric NOT NULL
        CHECK ("qty" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "quote_qty" numeric NOT NULL
        CHECK ("quote_qty" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "commission" numeric NOT NULL
        CHECK ("commission" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "commission_asset" text NOT NULL,
      "time" timestamptz NOT NULL,
      "is_buyer" boolean,
      "is_maker" boolean,
      "is_best_match" boolean,
      PRIMARY KEY ("connection_id", "symbol", "id")
    )
    """,
    """
    CREATE INDEX binance_my_trades_transaction_idx
      ON finance.binance_my_trades (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_assets_seen (
      connection_id uuid NOT NULL REFERENCES core.connections (id),
      asset text NOT NULL,
      first_seen_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (connection_id, asset)
    )
    """,
]

# The lists as 0037 leaves them (0030 added `trading212_ticker` and `trading212_account`, 0031
# `etoro_account`, 0035 `card_merchant`); the downgrade restores exactly these.
_SCHEMES = """'iso4217', 'isin', 'figi', 'ticker_mic', 'ibkr_conid', 'etoro_id',
          'chain_contract', 'coingecko', 'trading212_ticker'"""
_KINDS = """'uk_bank_account', 'ibkr_account', 'trading212_account', 'etoro_account',
          'card_merchant'"""


def _scheme_check(extra: str) -> str:
    return f"""
    ALTER TABLE finance.instrument_identifiers
      DROP CONSTRAINT instrument_identifiers_scheme_check,
      ADD CONSTRAINT instrument_identifiers_scheme_check
        CHECK (scheme IN ({_SCHEMES}{extra}))
    """


def _kind_check(extra: str) -> str:
    return f"""
    ALTER TABLE finance.account_identifiers
      DROP CONSTRAINT account_identifiers_kind_check,
      ADD CONSTRAINT account_identifiers_kind_check
        CHECK (kind IN ({_KINDS}{extra}))
    """


UPGRADE = [
    _scheme_check(", 'binance_asset'"),
    _kind_check(", 'chain_address'"),
    """
    ALTER TABLE finance.account_identifiers
      ADD CONSTRAINT account_identifiers_chain_address_check
        CHECK (kind <> 'chain_address' OR value ~ '^[A-Za-z0-9:_-]{20,128}$')
    """,
    *_TABLES,
]

_DROPPED = [
    "binance_assets_seen",
    "binance_my_trades",
    "binance_earn_flexible_rewards_responses",
    "binance_earn_flexible_rewards",
    "binance_earn_flexible_redemptions_responses",
    "binance_earn_flexible_redemptions",
    "binance_earn_flexible_subscriptions_responses",
    "binance_earn_flexible_subscriptions",
    "binance_dividends_responses",
    "binance_dividends",
    "binance_convert_responses",
    "binance_convert",
    "binance_withdrawals",
    "binance_deposits",
    "binance_asset_detail",
    "binance_coin_config_network_list",
    "binance_coin_config",
    "binance_snapshot_responses",
    "binance_snapshot_data_user_assets",
    "binance_snapshot_data_position",
    "binance_snapshot_data_balances",
    "binance_snapshot_data_assets",
    "binance_snapshot",
    "binance_earn_flexible_position_responses",
    "binance_earn_flexible_position_tier_annual_percentage_rate",
    "binance_earn_flexible_position",
    "binance_earn_account",
    "binance_wallet_balance_asset_balances",
    "binance_wallet_balance",
    "binance_funding_asset",
    "binance_user_asset",
    "binance_spot_account_balances",
    "binance_spot_account",
    "binance_api_trading_status",
    "binance_account_status",
    "binance_account_info",
    "binance_api_restrictions",
    "binance_exchange_info_symbols_permission_sets",
    "binance_exchange_info_symbols_filters",
    "binance_exchange_info_symbols",
    "binance_exchange_info_sors",
    "binance_exchange_info_rate_limits",
    "binance_exchange_info_exchange_filters",
    "binance_exchange_info",
]

# A downgrade and a new upgrade do not rebuild everything: the cursors are kept (the raw records
# are), so the history answers' envelopes (`*_responses`) of windows walked before are not asked
# again, and the report tables hold only the next reads. The ledger needs neither. Downgrading
# the Binance migrations on a database with Binance data loses the instrument bindings
# (`binance_asset`) and the empty-report times, which nothing rebuilds: to go back, restore a
# backup.
DOWNGRADE = [
    # What a Binance connection's config says about the tables dropped here: the digest of
    # each report's last stored answer (its table empty, the answer would not be stored
    # again) and the last raw record applied (the typed record rows empty, the raw records
    # would never be replayed into them).
    "UPDATE core.connections SET config = config - ARRAY['report_digests', 'normalized_through']"
    " WHERE connector = 'binance'",
    *(f"DROP TABLE finance.{name}" for name in _DROPPED),
    "DELETE FROM finance.instrument_identifiers WHERE scheme = 'binance_asset'",
    # A transaction to or from a chain address names it: the name goes, as 0035 does for a
    # card merchant's, before the identifier does.
    "UPDATE finance.transactions SET counterparty_identifier_id = NULL"
    " WHERE counterparty_identifier_id IN"
    " (SELECT id FROM finance.account_identifiers WHERE kind = 'chain_address')",
    "DELETE FROM finance.account_identifiers WHERE kind = 'chain_address'",
    """
    ALTER TABLE finance.account_identifiers DROP CONSTRAINT account_identifiers_chain_address_check
    """,
    _kind_check(""),
    _scheme_check(""),
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
