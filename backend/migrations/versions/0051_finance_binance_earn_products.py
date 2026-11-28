"""Binance: the typed tables of the other Earn products (design 5.6, 5.7): locked Simple Earn
and its positions, the flexible collateral record, BFUSD and RWUSD, ETH and SOL staking (WBETH
wraps, rate and boost rewards, unclaimed rewards), on-chain yields and their positions, soft
staking, and the dual investment and discount buy accounts and positions; by the same rule as
0041 (plan Decision 3): a record table per history label keyed by Binance's id (or a hash of the
row and its occurrence), a report table per position or account label keyed by the read, and a
`_responses` table for what an answer says beside its records.

Beyond the catalogue's fields: the dividend a reward is linked to (rule 3) on every reward
table, and the `type` a boost reward was asked for (its row does not say it). The statements
were emitted from the catalogue (`tests/finance/binance/emit_ddl.py`) and are frozen here.

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
    CREATE TABLE finance.binance_bfusd_account (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "bfusd_amount" numeric NOT NULL
        CHECK ("bfusd_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "usdt_profit" numeric NOT NULL
        CHECK ("usdt_profit" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "bfusd_profit" numeric NOT NULL
        CHECK ("bfusd_profit" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_bfusd_redemptions (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "content_hash" text NOT NULL,
      "occurrence" integer NOT NULL,
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "time" timestamptz NOT NULL,
      "asset" text NOT NULL,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "receive_asset" text NOT NULL,
      "receive_amount" numeric NOT NULL
        CHECK ("receive_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "fee" numeric
        CHECK ("fee" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "arrival_time" timestamptz,
      "status" text,
      PRIMARY KEY ("connection_id", "content_hash", "occurrence")
    )
    """,
    """
    CREATE INDEX binance_bfusd_redemptions_transaction_idx
      ON finance.binance_bfusd_redemptions (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_bfusd_redemptions_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_bfusd_rewards (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "content_hash" text NOT NULL,
      "occurrence" integer NOT NULL,
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "time" timestamptz NOT NULL,
      "reward_asset" text,
      "rewards_amount" numeric NOT NULL
        CHECK ("rewards_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "bfusd_position" numeric
        CHECK ("bfusd_position" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "annual_percentage_rate" numeric
        CHECK ("annual_percentage_rate" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "dividend_id" text,
      PRIMARY KEY ("connection_id", "content_hash", "occurrence")
    )
    """,
    """
    CREATE INDEX binance_bfusd_rewards_transaction_idx
      ON finance.binance_bfusd_rewards (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_bfusd_rewards_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_bfusd_subscriptions (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "content_hash" text NOT NULL,
      "occurrence" integer NOT NULL,
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "time" timestamptz NOT NULL,
      "asset" text NOT NULL,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "receive_asset" text NOT NULL,
      "receive_amount" numeric NOT NULL
        CHECK ("receive_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "status" text,
      PRIMARY KEY ("connection_id", "content_hash", "occurrence")
    )
    """,
    """
    CREATE INDEX binance_bfusd_subscriptions_transaction_idx
      ON finance.binance_bfusd_subscriptions (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_bfusd_subscriptions_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_bnsol_rewards (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "content_hash" text NOT NULL,
      "occurrence" integer NOT NULL,
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "time" timestamptz NOT NULL,
      "amount_in_sol" numeric
        CHECK ("amount_in_sol" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "holding" numeric
        CHECK ("holding" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "holding_in_sol" numeric
        CHECK ("holding_in_sol" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "annual_percentage_rate" numeric
        CHECK ("annual_percentage_rate" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "content_hash", "occurrence")
    )
    """,
    """
    CREATE INDEX binance_bnsol_rewards_transaction_idx
      ON finance.binance_bnsol_rewards (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_bnsol_rewards_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "est_rewards_in_sol" numeric
        CHECK ("est_rewards_in_sol" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_dci_accounts (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "total_amount_in_btc" numeric NOT NULL
        CHECK ("total_amount_in_btc" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_amount_in_usdt" numeric NOT NULL
        CHECK ("total_amount_in_usdt" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_dci_positions (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "id" text NOT NULL,
      "invest_coin" text,
      "exercised_coin" text,
      "subscription_amount" numeric
        CHECK ("subscription_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "strike_price" numeric
        CHECK ("strike_price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "duration" bigint,
      "settle_date" timestamptz,
      "purchase_status" text,
      "apr" numeric
        CHECK ("apr" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "order_id" text,
      "purchase_end_time" timestamptz,
      "option_type" text,
      "auto_compound_plan" text,
      "subscription_time" timestamptz NOT NULL,
      PRIMARY KEY ("connection_id", "snapshot_at", "id")
    )
    """,
    """
    CREATE TABLE finance.binance_dci_positions_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_discount_accounts (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "total_amount_in_btc" numeric NOT NULL
        CHECK ("total_amount_in_btc" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_amount_in_usdt" numeric NOT NULL
        CHECK ("total_amount_in_usdt" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_discount_positions (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "id" text NOT NULL,
      "invest_asset" text,
      "target_asset" text,
      "deposit_amount" numeric
        CHECK ("deposit_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "strike_price" numeric
        CHECK ("strike_price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "knockout_price" numeric
        CHECK ("knockout_price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "duration" bigint,
      "settle_date" timestamptz,
      "purchase_status" text,
      "knockout_apr" numeric
        CHECK ("knockout_apr" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "order_id" text,
      PRIMARY KEY ("connection_id", "snapshot_at", "id")
    )
    """,
    """
    CREATE TABLE finance.binance_discount_positions_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_earn_flexible_collateral (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "product_id" text,
      "asset" text NOT NULL,
      "create_time" timestamptz NOT NULL,
      "type" text,
      "product_name" text,
      "order_id" text NOT NULL,
      PRIMARY KEY ("connection_id", "order_id")
    )
    """,
    """
    CREATE INDEX binance_earn_flexible_collateral_transaction_idx
      ON finance.binance_earn_flexible_collateral (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_earn_flexible_collateral_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" text,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_earn_locked_position (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "position_id" text NOT NULL,
      "parent_position_id" text,
      "project_id" text,
      "asset" text NOT NULL,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "purchase_time" timestamptz NOT NULL,
      "duration" text,
      "accrual_days" text,
      "reward_asset" text,
      "apy" numeric
        CHECK ("apy" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "reward_amt" numeric
        CHECK ("reward_amt" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "extra_reward_asset" text,
      "extra_reward_apr" numeric
        CHECK ("extra_reward_apr" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "est_extra_reward_amt" numeric
        CHECK ("est_extra_reward_amt" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "boost_reward_asset" text,
      "boost_apr" numeric
        CHECK ("boost_apr" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_boost_reward_amt" numeric
        CHECK ("total_boost_reward_amt" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "next_pay" numeric
        CHECK ("next_pay" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "next_pay_date" timestamptz,
      "pay_period" text,
      "redeem_amount_early" numeric
        CHECK ("redeem_amount_early" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "rewards_end_date" timestamptz,
      "deliver_date" timestamptz,
      "redeem_period" text,
      "redeeming_amt" numeric
        CHECK ("redeeming_amt" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "redeem_to" text,
      "partial_amt_deliver_date" timestamptz,
      "can_redeem_early" boolean,
      "can_fast_redemption" boolean,
      "auto_subscribe" boolean,
      "type" text,
      "status" text,
      "can_re_stake" boolean,
      PRIMARY KEY ("connection_id", "snapshot_at", "position_id")
    )
    """,
    """
    CREATE TABLE finance.binance_earn_locked_position_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_earn_locked_redemptions (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "position_id" text,
      "redeem_id" text NOT NULL,
      "time" timestamptz NOT NULL,
      "asset" text NOT NULL,
      "lock_period" text,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "original_amount" numeric
        CHECK ("original_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "type" text,
      "deliver_date" timestamptz,
      "loss_amount" numeric
        CHECK ("loss_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "is_complete" boolean,
      "reward_asset" text,
      "reward_amt" numeric
        CHECK ("reward_amt" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "extra_reward_asset" text,
      "est_extra_reward_amt" numeric
        CHECK ("est_extra_reward_amt" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "status" text,
      PRIMARY KEY ("connection_id", "redeem_id")
    )
    """,
    """
    CREATE INDEX binance_earn_locked_redemptions_transaction_idx
      ON finance.binance_earn_locked_redemptions (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_earn_locked_redemptions_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_earn_locked_rewards (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "content_hash" text NOT NULL,
      "occurrence" integer NOT NULL,
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "position_id" text,
      "time" timestamptz NOT NULL,
      "asset" text NOT NULL,
      "lock_period" text,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "type" text,
      "dividend_id" text,
      PRIMARY KEY ("connection_id", "content_hash", "occurrence")
    )
    """,
    """
    CREATE INDEX binance_earn_locked_rewards_transaction_idx
      ON finance.binance_earn_locked_rewards (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_earn_locked_rewards_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_earn_locked_subscriptions (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "position_id" text,
      "purchase_id" text NOT NULL,
      "project_id" text,
      "time" timestamptz NOT NULL,
      "asset" text NOT NULL,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "lock_period" text,
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
    CREATE INDEX binance_earn_locked_subscriptions_transaction_idx
      ON finance.binance_earn_locked_subscriptions (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_earn_locked_subscriptions_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_eth_redemptions (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "content_hash" text NOT NULL,
      "occurrence" integer NOT NULL,
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "time" timestamptz NOT NULL,
      "arrival_time" timestamptz,
      "asset" text NOT NULL,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "distribute_asset" text NOT NULL,
      "distribute_amount" numeric NOT NULL
        CHECK ("distribute_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "conversion_ratio" numeric
        CHECK ("conversion_ratio" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "status" text,
      PRIMARY KEY ("connection_id", "content_hash", "occurrence")
    )
    """,
    """
    CREATE INDEX binance_eth_redemptions_transaction_idx
      ON finance.binance_eth_redemptions (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_eth_redemptions_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_eth_stakes (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "content_hash" text NOT NULL,
      "occurrence" integer NOT NULL,
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "time" timestamptz NOT NULL,
      "asset" text NOT NULL,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "distribute_asset" text NOT NULL,
      "distribute_amount" numeric NOT NULL
        CHECK ("distribute_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "conversion_ratio" numeric
        CHECK ("conversion_ratio" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "status" text,
      PRIMARY KEY ("connection_id", "content_hash", "occurrence")
    )
    """,
    """
    CREATE INDEX binance_eth_stakes_transaction_idx
      ON finance.binance_eth_stakes (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_eth_stakes_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_eth_staking_account (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "holding_in_eth" numeric NOT NULL
        CHECK ("holding_in_eth" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "holdings_wbeth_amount" numeric
        CHECK ("holdings_wbeth_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "holdings_beth_amount" numeric
        CHECK ("holdings_beth_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "thirty_days_profit_in_eth" numeric NOT NULL
        CHECK ("thirty_days_profit_in_eth" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "profit_amount_from_wbeth" numeric
        CHECK ("profit_amount_from_wbeth" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "profit_amount_from_beth" numeric
        CHECK ("profit_amount_from_beth" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_onchain_account (
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
    CREATE TABLE finance.binance_onchain_locked_position (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "position_id" text NOT NULL,
      "project_id" text,
      "asset" text NOT NULL,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "purchase_time" text NOT NULL,
      "purchase_time_at" timestamptz NOT NULL,
      "duration" text,
      "accrual_days" text,
      "reward_asset" text,
      "apy" numeric
        CHECK ("apy" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "reward_amt" numeric
        CHECK ("reward_amt" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "next_pay" numeric
        CHECK ("next_pay" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "next_pay_date" text,
      "next_pay_date_at" timestamptz,
      "pay_period" text,
      "rewards_pay_date" text,
      "rewards_pay_date_at" timestamptz,
      "rewards_end_date" text,
      "rewards_end_date_at" timestamptz,
      "deliver_date" text,
      "deliver_date_at" timestamptz,
      "next_subscription_date" text,
      "next_subscription_date_at" timestamptz,
      "redeeming_amt" numeric
        CHECK ("redeeming_amt" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "redeem_to" text,
      "can_redeem_early" boolean,
      "auto_subscribe" boolean,
      "type" text,
      "status" text,
      PRIMARY KEY ("connection_id", "snapshot_at", "position_id")
    )
    """,
    """
    CREATE TABLE finance.binance_onchain_locked_position_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_onchain_redemptions (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "position_id" text,
      "redeem_id" text NOT NULL,
      "time" timestamptz NOT NULL,
      "asset" text NOT NULL,
      "lock_period" text,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "original_amount" numeric
        CHECK ("original_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "type" text,
      "deliver_date" text,
      "deliver_date_at" timestamptz,
      "loss_amount" numeric
        CHECK ("loss_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "is_complete" boolean,
      "reward_asset" text,
      "reward_amt" numeric
        CHECK ("reward_amt" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "status" text,
      PRIMARY KEY ("connection_id", "redeem_id")
    )
    """,
    """
    CREATE INDEX binance_onchain_redemptions_transaction_idx
      ON finance.binance_onchain_redemptions (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_onchain_redemptions_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_onchain_rewards (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "content_hash" text NOT NULL,
      "occurrence" integer NOT NULL,
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "position_id" text,
      "time" timestamptz NOT NULL,
      "asset" text NOT NULL,
      "lock_period" text,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "dividend_id" text,
      PRIMARY KEY ("connection_id", "content_hash", "occurrence")
    )
    """,
    """
    CREATE INDEX binance_onchain_rewards_transaction_idx
      ON finance.binance_onchain_rewards (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_onchain_rewards_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_onchain_subscriptions (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "position_id" text,
      "purchase_id" text NOT NULL,
      "project_id" text,
      "client_id" text,
      "time" timestamptz NOT NULL,
      "asset" text NOT NULL,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "lock_period" text,
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
    CREATE INDEX binance_onchain_subscriptions_transaction_idx
      ON finance.binance_onchain_subscriptions (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_onchain_subscriptions_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_rwusd_account (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "rwusd_amount" numeric NOT NULL
        CHECK ("rwusd_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_profit" numeric NOT NULL
        CHECK ("total_profit" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_rwusd_redemptions (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "content_hash" text NOT NULL,
      "occurrence" integer NOT NULL,
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "time" timestamptz NOT NULL,
      "asset" text NOT NULL,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "receive_asset" text NOT NULL,
      "receive_amount" numeric NOT NULL
        CHECK ("receive_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "fee" numeric
        CHECK ("fee" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "arrival_time" timestamptz,
      "status" text,
      PRIMARY KEY ("connection_id", "content_hash", "occurrence")
    )
    """,
    """
    CREATE INDEX binance_rwusd_redemptions_transaction_idx
      ON finance.binance_rwusd_redemptions (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_rwusd_redemptions_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_rwusd_rewards (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "content_hash" text NOT NULL,
      "occurrence" integer NOT NULL,
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "time" timestamptz NOT NULL,
      "rewards_amount" numeric NOT NULL
        CHECK ("rewards_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "rwusd_position" numeric
        CHECK ("rwusd_position" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "annual_percentage_rate" numeric
        CHECK ("annual_percentage_rate" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "dividend_id" text,
      PRIMARY KEY ("connection_id", "content_hash", "occurrence")
    )
    """,
    """
    CREATE INDEX binance_rwusd_rewards_transaction_idx
      ON finance.binance_rwusd_rewards (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_rwusd_rewards_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_rwusd_subscriptions (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "content_hash" text NOT NULL,
      "occurrence" integer NOT NULL,
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "time" timestamptz NOT NULL,
      "asset" text NOT NULL,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "receive_asset" text NOT NULL,
      "receive_amount" numeric NOT NULL
        CHECK ("receive_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "status" text,
      PRIMARY KEY ("connection_id", "content_hash", "occurrence")
    )
    """,
    """
    CREATE INDEX binance_rwusd_subscriptions_transaction_idx
      ON finance.binance_rwusd_subscriptions (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_rwusd_subscriptions_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_soft_staking_list (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "asset" text NOT NULL,
      "min_amount" numeric
        CHECK ("min_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "max_cap" numeric
        CHECK ("max_cap" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "apr" numeric
        CHECK ("apr" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "staked_amount" numeric
        CHECK ("staked_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_profit" numeric
        CHECK ("total_profit" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "snapshot_at", "asset")
    )
    """,
    """
    CREATE TABLE finance.binance_soft_staking_list_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "status" boolean,
      "total_rewards_usdt" numeric
        CHECK ("total_rewards_usdt" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total" bigint,
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_soft_staking_rewards (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "content_hash" text NOT NULL,
      "occurrence" integer NOT NULL,
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "asset" text NOT NULL,
      "rewards" numeric NOT NULL
        CHECK ("rewards" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "reward_asset" text,
      "avg_amount" numeric
        CHECK ("avg_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "time" timestamptz NOT NULL,
      "dividend_id" text,
      PRIMARY KEY ("connection_id", "content_hash", "occurrence")
    )
    """,
    """
    CREATE INDEX binance_soft_staking_rewards_transaction_idx
      ON finance.binance_soft_staking_rewards (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_soft_staking_rewards_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_sol_boost_rewards (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "content_hash" text NOT NULL,
      "occurrence" integer NOT NULL,
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "time" timestamptz NOT NULL,
      "token" text NOT NULL,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "bnsol_holding" numeric
        CHECK ("bnsol_holding" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "status" text,
      "type" text,
      "dividend_id" text,
      PRIMARY KEY ("connection_id", "content_hash", "occurrence")
    )
    """,
    """
    CREATE INDEX binance_sol_boost_rewards_transaction_idx
      ON finance.binance_sol_boost_rewards (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_sol_boost_rewards_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_sol_redemptions (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "content_hash" text NOT NULL,
      "occurrence" integer NOT NULL,
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "time" timestamptz NOT NULL,
      "arrival_time" timestamptz,
      "asset" text NOT NULL,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "distribute_asset" text NOT NULL,
      "distribute_amount" numeric NOT NULL
        CHECK ("distribute_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "exchange_rate" numeric
        CHECK ("exchange_rate" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "status" text,
      PRIMARY KEY ("connection_id", "content_hash", "occurrence")
    )
    """,
    """
    CREATE INDEX binance_sol_redemptions_transaction_idx
      ON finance.binance_sol_redemptions (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_sol_redemptions_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_sol_stakes (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "content_hash" text NOT NULL,
      "occurrence" integer NOT NULL,
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "time" timestamptz NOT NULL,
      "asset" text NOT NULL,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "distribute_asset" text NOT NULL,
      "distribute_amount" numeric NOT NULL
        CHECK ("distribute_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "exchange_rate" numeric
        CHECK ("exchange_rate" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "status" text,
      PRIMARY KEY ("connection_id", "content_hash", "occurrence")
    )
    """,
    """
    CREATE INDEX binance_sol_stakes_transaction_idx
      ON finance.binance_sol_stakes (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_sol_stakes_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_sol_staking_account (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "bnsol_amount" numeric NOT NULL
        CHECK ("bnsol_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "holding_in_sol" numeric NOT NULL
        CHECK ("holding_in_sol" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "thirty_days_profit_in_sol" numeric NOT NULL
        CHECK ("thirty_days_profit_in_sol" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_sol_unclaimed_rewards (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "rewards_asset" text NOT NULL,
      PRIMARY KEY ("connection_id", "snapshot_at", "rewards_asset")
    )
    """,
    """
    CREATE TABLE finance.binance_wbeth_rewards (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "content_hash" text NOT NULL,
      "occurrence" integer NOT NULL,
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "time" timestamptz NOT NULL,
      "amount_in_eth" numeric
        CHECK ("amount_in_eth" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "holding" numeric
        CHECK ("holding" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "holding_in_eth" numeric
        CHECK ("holding_in_eth" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "annual_percentage_rate" numeric
        CHECK ("annual_percentage_rate" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "content_hash", "occurrence")
    )
    """,
    """
    CREATE INDEX binance_wbeth_rewards_transaction_idx
      ON finance.binance_wbeth_rewards (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_wbeth_rewards_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "est_rewards_in_eth" numeric
        CHECK ("est_rewards_in_eth" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_wbeth_unwraps (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "content_hash" text NOT NULL,
      "occurrence" integer NOT NULL,
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "time" timestamptz NOT NULL,
      "from_asset" text NOT NULL,
      "from_amount" numeric NOT NULL
        CHECK ("from_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "to_asset" text NOT NULL,
      "to_amount" numeric NOT NULL
        CHECK ("to_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "exchange_rate" numeric
        CHECK ("exchange_rate" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "status" text,
      PRIMARY KEY ("connection_id", "content_hash", "occurrence")
    )
    """,
    """
    CREATE INDEX binance_wbeth_unwraps_transaction_idx
      ON finance.binance_wbeth_unwraps (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_wbeth_unwraps_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_wbeth_wraps (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "content_hash" text NOT NULL,
      "occurrence" integer NOT NULL,
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "time" timestamptz NOT NULL,
      "from_asset" text NOT NULL,
      "from_amount" numeric NOT NULL
        CHECK ("from_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "to_asset" text NOT NULL,
      "to_amount" numeric NOT NULL
        CHECK ("to_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "exchange_rate" numeric
        CHECK ("exchange_rate" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "status" text,
      PRIMARY KEY ("connection_id", "content_hash", "occurrence")
    )
    """,
    """
    CREATE INDEX binance_wbeth_wraps_transaction_idx
      ON finance.binance_wbeth_wraps (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_wbeth_wraps_responses (
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
    "binance_wbeth_wraps_responses",
    "binance_wbeth_wraps",
    "binance_wbeth_unwraps_responses",
    "binance_wbeth_unwraps",
    "binance_wbeth_rewards_responses",
    "binance_wbeth_rewards",
    "binance_sol_unclaimed_rewards",
    "binance_sol_staking_account",
    "binance_sol_stakes_responses",
    "binance_sol_stakes",
    "binance_sol_redemptions_responses",
    "binance_sol_redemptions",
    "binance_sol_boost_rewards_responses",
    "binance_sol_boost_rewards",
    "binance_soft_staking_rewards_responses",
    "binance_soft_staking_rewards",
    "binance_soft_staking_list_responses",
    "binance_soft_staking_list",
    "binance_rwusd_subscriptions_responses",
    "binance_rwusd_subscriptions",
    "binance_rwusd_rewards_responses",
    "binance_rwusd_rewards",
    "binance_rwusd_redemptions_responses",
    "binance_rwusd_redemptions",
    "binance_rwusd_account",
    "binance_onchain_subscriptions_responses",
    "binance_onchain_subscriptions",
    "binance_onchain_rewards_responses",
    "binance_onchain_rewards",
    "binance_onchain_redemptions_responses",
    "binance_onchain_redemptions",
    "binance_onchain_locked_position_responses",
    "binance_onchain_locked_position",
    "binance_onchain_account",
    "binance_eth_staking_account",
    "binance_eth_stakes_responses",
    "binance_eth_stakes",
    "binance_eth_redemptions_responses",
    "binance_eth_redemptions",
    "binance_earn_locked_subscriptions_responses",
    "binance_earn_locked_subscriptions",
    "binance_earn_locked_rewards_responses",
    "binance_earn_locked_rewards",
    "binance_earn_locked_redemptions_responses",
    "binance_earn_locked_redemptions",
    "binance_earn_locked_position_responses",
    "binance_earn_locked_position",
    "binance_earn_flexible_collateral_responses",
    "binance_earn_flexible_collateral",
    "binance_discount_positions_responses",
    "binance_discount_positions",
    "binance_discount_accounts",
    "binance_dci_positions_responses",
    "binance_dci_positions",
    "binance_dci_accounts",
    "binance_bnsol_rewards_responses",
    "binance_bnsol_rewards",
    "binance_bfusd_subscriptions_responses",
    "binance_bfusd_subscriptions",
    "binance_bfusd_rewards_responses",
    "binance_bfusd_rewards",
    "binance_bfusd_redemptions_responses",
    "binance_bfusd_redemptions",
    "binance_bfusd_account",
]
# The report labels whose tables this drops: the digest of each one's last stored answer goes
# with them, or an upgrade would not store an unchanged answer again.
_REPORTS = [
    "bfusd-account",
    "dci-accounts",
    "dci-positions",
    "discount-accounts",
    "discount-positions",
    "earn-locked-position",
    "eth-staking-account",
    "onchain-account",
    "onchain-locked-position",
    "rwusd-account",
    "soft-staking-list",
    "sol-staking-account",
    "sol-unclaimed-rewards",
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
