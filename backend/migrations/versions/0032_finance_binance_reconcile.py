"""Binance: the reconciliation differences, intervals between two consecutive reported
snapshots of one account and instrument whose reported change the records posted in between
do not explain (design 5.11, plan Decision 11). A difference is never corrected; one the
records explain later gets `closed_at`.

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-23
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    CREATE TABLE finance.binance_reconciliation_differences (
      id uuid PRIMARY KEY DEFAULT uuidv7(),
      account_id uuid NOT NULL REFERENCES finance.accounts (id),
      instrument_id uuid NOT NULL REFERENCES finance.instruments (id),
      from_at timestamptz NOT NULL,
      to_at timestamptz NOT NULL,
      reported_change numeric NOT NULL
        CHECK (reported_change NOT IN ('NaN', 'Infinity', '-Infinity')),
      posted_sum numeric NOT NULL
        CHECK (posted_sum NOT IN ('NaN', 'Infinity', '-Infinity')),
      opened_at timestamptz NOT NULL,
      closed_at timestamptz,
      UNIQUE (account_id, instrument_id, from_at, to_at),
      CHECK (from_at < to_at)
    )
    """,
]

DOWNGRADE = ["DROP TABLE finance.binance_reconciliation_differences"]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
