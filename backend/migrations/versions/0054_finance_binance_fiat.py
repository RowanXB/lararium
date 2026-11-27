"""Binance: the typed tables of fiat by bank (`fiat-orders`), card purchases and sales
(`fiat-payments`), P2P orders (`c2c`), Pay and rebates (design 5.7, 5.12); by the same rule as
0041 (plan Decision 3): a record table per history label keyed by Binance's id (a rebate, which
has none, by its content and occurrence), its arrays child tables, and what an answer says beside
its records a response table. A fiat order and a card payment keep the `transactionType` they
were asked for (the row does not say it); a rebate keeps the dividend it is linked to (rule 3).
The statements were emitted from the catalogue (`tests/finance/binance/emit_ddl.py`) and are
frozen here.

Revision ID: 0054
Revises: 0053
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    CREATE TABLE finance.binance_fiat_orders (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "order_no" text NOT NULL,
      "fiat_currency" text,
      "indicated_amount" numeric
        CHECK ("indicated_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_fee" numeric
        CHECK ("total_fee" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "method" text,
      "status" text,
      "create_time" timestamptz NOT NULL,
      "update_time" timestamptz,
      "transaction_type" text,
      PRIMARY KEY ("connection_id", "order_no")
    )
    """,
    """
    CREATE INDEX binance_fiat_orders_transaction_idx
      ON finance.binance_fiat_orders (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_fiat_orders_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "code" text,
      "message" text,
      "total" bigint,
      "success" boolean,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_fiat_payments (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "order_no" text NOT NULL,
      "source_amount" numeric
        CHECK ("source_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "fiat_currency" text,
      "obtain_amount" numeric
        CHECK ("obtain_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "crypto_currency" text,
      "total_fee" numeric
        CHECK ("total_fee" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "price" numeric
        CHECK ("price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "status" text,
      "payment_method" text,
      "create_time" timestamptz NOT NULL,
      "update_time" timestamptz,
      "transaction_type" text,
      PRIMARY KEY ("connection_id", "order_no")
    )
    """,
    """
    CREATE INDEX binance_fiat_payments_transaction_idx
      ON finance.binance_fiat_payments (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_fiat_payments_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "code" text,
      "message" text,
      "total" bigint,
      "success" boolean,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_c2c (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "order_number" text NOT NULL,
      "adv_no" text,
      "trade_type" text,
      "asset" text NOT NULL,
      "fiat" text,
      "fiat_symbol" text,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_price" numeric
        CHECK ("total_price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "unit_price" numeric
        CHECK ("unit_price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "order_status" text,
      "create_time" timestamptz NOT NULL,
      "commission" numeric
        CHECK ("commission" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "counter_part_nick_name" text,
      "pay_method_name" text,
      "additional_kyc_verify" bigint,
      "taker_commission_rate" numeric
        CHECK ("taker_commission_rate" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "taker_commission" numeric
        CHECK ("taker_commission" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "taker_amount" numeric
        CHECK ("taker_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "advertisement_role" text,
      PRIMARY KEY ("connection_id", "order_number")
    )
    """,
    """
    CREATE INDEX binance_c2c_transaction_idx
      ON finance.binance_c2c (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_c2c_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "code" text,
      "message" text,
      "total" bigint,
      "success" boolean,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_pay (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "order_type" text,
      "pay_transaction_id" text NOT NULL,
      "transaction_time" timestamptz NOT NULL,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "currency" text,
      "wallet_type" bigint,
      "wallet_types" bigint[],
      "payer_info_name" text,
      "payer_info_type" text,
      "payer_info_binance_id" text,
      "receiver_info_name" text,
      "receiver_info_type" text,
      "receiver_info_email" text,
      "receiver_info_binance_id" text,
      "receiver_info_account_id" text,
      "receiver_info_country_code" text,
      "receiver_info_phone_number" text,
      "receiver_info_mobile_code" text,
      "receiver_info_extend_institution_name" text,
      "receiver_info_extend_card_number" text,
      "receiver_info_extend_digital_wallet_id" text,
      PRIMARY KEY ("connection_id", "pay_transaction_id")
    )
    """,
    """
    CREATE INDEX binance_pay_transaction_idx
      ON finance.binance_pay (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_pay_funds_detail (
      "connection_id" uuid NOT NULL,
      "pay_transaction_id" text NOT NULL,
      "funds_detail_ordinal" integer NOT NULL,
      "currency" text,
      "amount" numeric
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "pay_transaction_id", "funds_detail_ordinal"),
      FOREIGN KEY ("connection_id", "pay_transaction_id")
        REFERENCES finance.binance_pay
          ("connection_id", "pay_transaction_id")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_pay_funds_detail_wallet_asset_cost (
      "connection_id" uuid NOT NULL,
      "pay_transaction_id" text NOT NULL,
      "funds_detail_ordinal" integer NOT NULL,
      "wallet_type" text NOT NULL,
      "value" numeric
        CHECK ("value" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "pay_transaction_id", "funds_detail_ordinal", "wallet_type"),
      FOREIGN KEY ("connection_id", "pay_transaction_id", "funds_detail_ordinal")
        REFERENCES finance.binance_pay_funds_detail
          ("connection_id", "pay_transaction_id", "funds_detail_ordinal")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_pay_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "code" text,
      "message" text,
      "success" boolean,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_rebate (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "content_hash" text NOT NULL,
      "occurrence" integer NOT NULL,
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "asset" text NOT NULL,
      "type" bigint,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "update_time" timestamptz NOT NULL,
      "dividend_id" text,
      PRIMARY KEY ("connection_id", "content_hash", "occurrence")
    )
    """,
    """
    CREATE INDEX binance_rebate_transaction_idx
      ON finance.binance_rebate (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_rebate_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "status" text,
      "type" text,
      "code" text,
      "data_page" bigint,
      "data_total_records" bigint,
      "data_total_page_num" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
]

_DROPPED = [
    "binance_rebate_responses",
    "binance_rebate",
    "binance_pay_responses",
    "binance_pay_funds_detail_wallet_asset_cost",
    "binance_pay_funds_detail",
    "binance_pay",
    "binance_c2c_responses",
    "binance_c2c",
    "binance_fiat_payments_responses",
    "binance_fiat_payments",
    "binance_fiat_orders_responses",
    "binance_fiat_orders",
]

# The raw records and the cursors are kept, as 0041's downgrade keeps them; the transactions
# these records posted stay in the ledger. None of these labels is a report, so no digest goes.
DOWNGRADE = [
    # The last raw record applied: with these typed tables gone, an upgrade must apply every raw
    # record again to fill them.
    "UPDATE core.connections SET config = config - 'normalized_through'"
    " WHERE connector = 'binance'",
    *(f"DROP TABLE finance.{name}" for name in _DROPPED),
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
