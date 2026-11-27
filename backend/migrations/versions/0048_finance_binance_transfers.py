"""Binance: the typed tables of wallet-to-wallet transfers (`universal-transfer`), dust
conversions (`dust`, a child table per conversion's lines), Cloud Mining payments and refunds
and the travel-rule deposit and withdrawal histories (design 5.7, 5.12), by the same rule as
0041 (plan Decision 3): a record table per label keyed by Binance's id, a `_responses` table
for what an answer says beside its records.

Beyond the catalogue's fields: the isolated pair a transfer was asked for (its row does not
name it), the wallet a dust conversion was asked for (`accountType`), and the id of the deposit
or withdrawal a travel-rule row describes. The statements were emitted from the catalogue
(`tests/finance/binance/emit_ddl.py`) and are frozen here.

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
    CREATE TABLE finance.binance_cloud_mining (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "create_time" timestamptz NOT NULL,
      "tran_id" text NOT NULL,
      "type" bigint,
      "asset" text NOT NULL,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "status" text,
      PRIMARY KEY ("connection_id", "tran_id")
    )
    """,
    """
    CREATE INDEX binance_cloud_mining_transaction_idx
      ON finance.binance_cloud_mining (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_cloud_mining_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_dust (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "operate_time" timestamptz NOT NULL,
      "total_transfered_amount" numeric
        CHECK ("total_transfered_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_service_charge_amount" numeric
        CHECK ("total_service_charge_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "trans_id" text NOT NULL,
      "account_type" text,
      PRIMARY KEY ("connection_id", "trans_id")
    )
    """,
    """
    CREATE INDEX binance_dust_transaction_idx
      ON finance.binance_dust (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_dust_user_asset_dribblet_details (
      "connection_id" uuid NOT NULL,
      "trans_id" text NOT NULL,
      "user_asset_dribblet_details_ordinal" integer NOT NULL,
      "user_asset_dribblet_details_trans_id" text,
      "service_charge_amount" numeric
        CHECK ("service_charge_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "amount" numeric
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "operate_time" timestamptz,
      "transfered_amount" numeric
        CHECK ("transfered_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "from_asset" text,
      "target_asset" text,
      PRIMARY KEY ("connection_id", "trans_id", "user_asset_dribblet_details_ordinal"),
      FOREIGN KEY ("connection_id", "trans_id")
        REFERENCES finance.binance_dust
          ("connection_id", "trans_id")
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE finance.binance_dust_responses (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "window_from" timestamptz NOT NULL,
      "window_to" timestamptz NOT NULL,
      "page" integer NOT NULL,
      "total" bigint,
      PRIMARY KEY ("connection_id", "window_from", "window_to", "page")
    )
    """,
    """
    CREATE TABLE finance.binance_travel_deposits (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "tr_id" text NOT NULL,
      "tran_id" text,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "coin" text NOT NULL,
      "network" text,
      "deposit_status" bigint,
      "travel_rule_status" bigint,
      "travel_rule_status_v2" text,
      "address" text,
      "address_tag" text,
      "tx_id" text,
      "insert_time" timestamptz NOT NULL,
      "complete_time" timestamptz,
      "transfer_type" bigint,
      "confirm_times" text,
      "require_questionnaire" boolean,
      "questionnaire" text,
      "deposit_id" text,
      PRIMARY KEY ("connection_id", "tr_id")
    )
    """,
    """
    CREATE INDEX binance_travel_deposits_transaction_idx
      ON finance.binance_travel_deposits (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_travel_withdrawals (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "id" text NOT NULL,
      "tr_id" text,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "transaction_fee" numeric
        CHECK ("transaction_fee" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "coin" text NOT NULL,
      "withdrawal_status" bigint,
      "travel_rule_status" bigint,
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
      "questionnaire" text,
      "complete_time" text,
      "complete_time_at" timestamptz,
      "withdrawal_id" text,
      PRIMARY KEY ("connection_id", "id")
    )
    """,
    """
    CREATE INDEX binance_travel_withdrawals_transaction_idx
      ON finance.binance_travel_withdrawals (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_universal_transfer (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "transaction_id" uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      "asset" text NOT NULL,
      "amount" numeric NOT NULL
        CHECK ("amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "type" text,
      "status" text,
      "tran_id" text NOT NULL,
      "timestamp" timestamptz NOT NULL,
      "from_symbol" text,
      "to_symbol" text,
      PRIMARY KEY ("connection_id", "tran_id")
    )
    """,
    """
    CREATE INDEX binance_universal_transfer_transaction_idx
      ON finance.binance_universal_transfer (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE finance.binance_universal_transfer_responses (
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
    "binance_universal_transfer_responses",
    "binance_universal_transfer",
    "binance_travel_withdrawals",
    "binance_travel_deposits",
    "binance_dust_responses",
    "binance_dust_user_asset_dribblet_details",
    "binance_dust",
    "binance_cloud_mining_responses",
    "binance_cloud_mining",
]

# The raw records and the cursors are kept, as 0041's downgrade keeps them; the transactions
# these records posted stay in the ledger.
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
