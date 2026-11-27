"""Binance: the typed tables of crypto loans (design 5.6, 5.7): the flexible loans' borrow,
repay, LTV adjustment and liquidation histories and their ongoing orders, and the VIP loans'
repay, accrued interest and request histories, ongoing orders and collateral accounts; by the
same rule as 0041 (plan Decision 3): a record table per history label keyed by a hash of the row
and its occurrence (or Binance's id), a report table per position label keyed by the read and
the row's id, and a `_responses` table for what an answer says beside its records. The
statements were emitted from the catalogue (`tests/finance/binance/emit_ddl.py`) and are frozen
here.

Revision ID: 0048
Revises: 0047
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    CREATE TABLE finance.binance_loan_flexible_borrows (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "content_hash" text NOT NULL,
      "occurrence" integer NOT NULL,
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "loan_coin" text NOT NULL,
      "initial_loan_amount" numeric NOT NULL
        CHECK ("initial_loan_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "collateral_coin" text NOT NULL,
      "initial_collateral_amount" numeric
        CHECK ("initial_collateral_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "borrow_time" timestamptz NOT NULL,
      "status" text,
      PRIMARY KEY ("connection_id", "content_hash", "occurrence")
    )
    """,
    """
    CREATE INDEX binance_loan_flexible_borrows_transaction_idx
      ON finance.binance_loan_flexible_borrows (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_loan_flexible_borrows_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_loan_flexible_liquidations (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "content_hash" text NOT NULL,
      "occurrence" integer NOT NULL,
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "loan_coin" text NOT NULL,
      "liquidation_debt" numeric
        CHECK ("liquidation_debt" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "collateral_coin" text NOT NULL,
      "liquidation_collateral_amount" numeric
        CHECK ("liquidation_collateral_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "return_collateral_amount" numeric
        CHECK ("return_collateral_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "liquidation_fee" numeric
        CHECK ("liquidation_fee" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "liquidation_starting_price" numeric
        CHECK ("liquidation_starting_price" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "liquidation_starting_time" timestamptz NOT NULL,
      "status" text,
      PRIMARY KEY ("connection_id", "content_hash", "occurrence")
    )
    """,
    """
    CREATE INDEX binance_loan_flexible_liquidations_transaction_idx
      ON finance.binance_loan_flexible_liquidations (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_loan_flexible_liquidations_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_loan_flexible_ltv_adjustments (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "content_hash" text NOT NULL,
      "occurrence" integer NOT NULL,
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "loan_coin" text NOT NULL,
      "collateral_coin" text NOT NULL,
      "direction" text,
      "collateral_amount" numeric
        CHECK ("collateral_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "pre_ltv" numeric
        CHECK ("pre_ltv" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "after_ltv" numeric
        CHECK ("after_ltv" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "adjust_time" timestamptz NOT NULL,
      PRIMARY KEY ("connection_id", "content_hash", "occurrence")
    )
    """,
    """
    CREATE INDEX binance_loan_flexible_ltv_adjustments_transaction_idx
      ON finance.binance_loan_flexible_ltv_adjustments (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_loan_flexible_ltv_adjustments_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_loan_flexible_ongoing (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "loan_coin" text NOT NULL,
      "total_debt" numeric NOT NULL
        CHECK ("total_debt" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "collateral_coin" text NOT NULL,
      "collateral_amount" numeric
        CHECK ("collateral_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "current_ltv" numeric
        CHECK ("current_ltv" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "snapshot_at", "loan_coin", "collateral_coin")
    )
    """,
    """
    CREATE TABLE finance.binance_loan_flexible_ongoing_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_loan_flexible_repays (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "content_hash" text NOT NULL,
      "occurrence" integer NOT NULL,
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "loan_coin" text NOT NULL,
      "repay_amount" numeric NOT NULL
        CHECK ("repay_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "collateral_coin" text NOT NULL,
      "collateral_return" numeric
        CHECK ("collateral_return" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "repay_status" text,
      "repay_time" timestamptz NOT NULL,
      PRIMARY KEY ("connection_id", "content_hash", "occurrence")
    )
    """,
    """
    CREATE INDEX binance_loan_flexible_repays_transaction_idx
      ON finance.binance_loan_flexible_repays (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_loan_flexible_repays_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_vip_loan_collateral_account (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "collateral_account_id" text NOT NULL,
      "collateral_coin" text NOT NULL,
      PRIMARY KEY ("connection_id", "snapshot_at", "collateral_account_id")
    )
    """,
    """
    CREATE TABLE finance.binance_vip_loan_collateral_account_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_vip_loan_interest (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "content_hash" text NOT NULL,
      "occurrence" integer NOT NULL,
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "loan_coin" text NOT NULL,
      "principal_amount" numeric
        CHECK ("principal_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "interest_amount" numeric
        CHECK ("interest_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "annual_interest_rate" numeric
        CHECK ("annual_interest_rate" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "accrual_time" timestamptz NOT NULL,
      "order_id" text,
      PRIMARY KEY ("connection_id", "content_hash", "occurrence")
    )
    """,
    """
    CREATE INDEX binance_vip_loan_interest_transaction_idx
      ON finance.binance_vip_loan_interest (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_vip_loan_interest_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_vip_loan_ongoing (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "order_id" text NOT NULL,
      "loan_coin" text NOT NULL,
      "total_debt" numeric NOT NULL
        CHECK ("total_debt" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "loan_rate" numeric
        CHECK ("loan_rate" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "residual_interest" numeric
        CHECK ("residual_interest" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "collateral_account_id" text,
      "collateral_coin" text NOT NULL,
      "total_collateral_value_after_haircut" numeric
        CHECK ("total_collateral_value_after_haircut" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "locked_collateral_value" numeric
        CHECK ("locked_collateral_value" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "current_ltv" numeric
        CHECK ("current_ltv" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "expiration_time" timestamptz,
      "loan_date" text NOT NULL,
      "loan_date_at" timestamptz NOT NULL,
      "loan_term" text,
      PRIMARY KEY ("connection_id", "snapshot_at", "order_id")
    )
    """,
    """
    CREATE TABLE finance.binance_vip_loan_ongoing_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_vip_loan_repays (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "content_hash" text NOT NULL,
      "occurrence" integer NOT NULL,
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "loan_coin" text NOT NULL,
      "repay_amount" numeric NOT NULL
        CHECK ("repay_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "repay_principal" numeric
        CHECK ("repay_principal" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "repay_interest" numeric
        CHECK ("repay_interest" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "collateral_coin" text NOT NULL,
      "repay_status" text,
      "loan_date" text,
      "loan_date_at" timestamptz,
      "repay_time" text NOT NULL,
      "repay_time_at" timestamptz NOT NULL,
      "order_id" text,
      PRIMARY KEY ("connection_id", "content_hash", "occurrence")
    )
    """,
    """
    CREATE INDEX binance_vip_loan_repays_transaction_idx
      ON finance.binance_vip_loan_repays (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_vip_loan_repays_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_vip_loan_requests (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "loan_account_id" text,
      "order_id" text,
      "request_id" text NOT NULL,
      "loan_coin" text NOT NULL,
      "loan_amount" numeric
        CHECK ("loan_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "collateral_account_id" text,
      "collateral_coin" text NOT NULL,
      "loan_term" text,
      "status" text,
      "loan_date" text NOT NULL,
      "loan_date_at" timestamptz NOT NULL,
      PRIMARY KEY ("connection_id", "request_id")
    )
    """,
    """
    CREATE INDEX binance_vip_loan_requests_transaction_idx
      ON finance.binance_vip_loan_requests (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_vip_loan_requests_responses (
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
    "binance_vip_loan_requests_responses",
    "binance_vip_loan_requests",
    "binance_vip_loan_repays_responses",
    "binance_vip_loan_repays",
    "binance_vip_loan_ongoing_responses",
    "binance_vip_loan_ongoing",
    "binance_vip_loan_interest_responses",
    "binance_vip_loan_interest",
    "binance_vip_loan_collateral_account_responses",
    "binance_vip_loan_collateral_account",
    "binance_loan_flexible_repays_responses",
    "binance_loan_flexible_repays",
    "binance_loan_flexible_ongoing_responses",
    "binance_loan_flexible_ongoing",
    "binance_loan_flexible_ltv_adjustments_responses",
    "binance_loan_flexible_ltv_adjustments",
    "binance_loan_flexible_liquidations_responses",
    "binance_loan_flexible_liquidations",
    "binance_loan_flexible_borrows_responses",
    "binance_loan_flexible_borrows",
]
_REPORTS = [
    "loan-flexible-ongoing",
    "vip-loan-collateral-account",
    "vip-loan-ongoing",
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
