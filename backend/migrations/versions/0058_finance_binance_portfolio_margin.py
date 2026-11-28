"""Binance: the typed tables of the Portfolio Margin account (design 5.6, 5.11, 5.13): its
account (`pm-account`) and its balance per asset (`pm-balance`, the wallet's report; plan
Decision 11 reconciles on each asset's `totalWalletBalance`, read from the raw read); by the
same rule as 0041 (plan Decision 3): a report table per label keyed by the read. Portfolio
Margin's records are not posted (design 2), so it has no record table. The statements were
emitted from the catalogue (`tests/finance/binance/emit_ddl.py`) and are frozen here.

Revision ID: 0058
Revises: 0057
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0058"
down_revision: str | None = "0057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    CREATE TABLE finance.binance_pm_account (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "account_equity" numeric
        CHECK ("account_equity" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "account_initial_margin" numeric
        CHECK ("account_initial_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "account_maint_margin" numeric
        CHECK ("account_maint_margin" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "account_status" text,
      "actual_equity" numeric
        CHECK ("actual_equity" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_available_balance" numeric
        CHECK ("total_available_balance" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_margin_open_loss" numeric
        CHECK ("total_margin_open_loss" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "uni_mmr" numeric
        CHECK ("uni_mmr" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "update_time" timestamptz,
      "virtual_max_withdraw_amount" numeric
        CHECK ("virtual_max_withdraw_amount" NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY ("connection_id", "snapshot_at")
    )
    """,
    """
    CREATE TABLE finance.binance_pm_balance (
      "connection_id" uuid NOT NULL REFERENCES core.connections (id),
      "snapshot_at" timestamptz NOT NULL,
      "read_to" timestamptz NOT NULL,
      "asset" text NOT NULL,
      "cm_unrealized_pnl" numeric
        CHECK ("cm_unrealized_pnl" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "cm_wallet_balance" numeric
        CHECK ("cm_wallet_balance" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "cross_margin_asset" numeric
        CHECK ("cross_margin_asset" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "cross_margin_borrowed" numeric
        CHECK ("cross_margin_borrowed" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "cross_margin_free" numeric
        CHECK ("cross_margin_free" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "cross_margin_interest" numeric
        CHECK ("cross_margin_interest" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "cross_margin_locked" numeric
        CHECK ("cross_margin_locked" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "negative_balance" numeric
        CHECK ("negative_balance" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "total_wallet_balance" numeric NOT NULL
        CHECK ("total_wallet_balance" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "um_unrealized_pnl" numeric
        CHECK ("um_unrealized_pnl" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "um_wallet_balance" numeric
        CHECK ("um_wallet_balance" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "update_time" timestamptz,
      PRIMARY KEY ("connection_id", "snapshot_at", "asset")
    )
    """,
]

_DROPPED = ["binance_pm_balance", "binance_pm_account"]
_REPORTS = ["pm-account", "pm-balance"]

# The raw records and the cursors are kept, as 0041's downgrade keeps them. These labels are
# reports, not records: no raw record is applied into them, so `normalized_through` stays; the
# digests of what they stored go, so an upgrade stores the next answer again.
DOWNGRADE = [
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
