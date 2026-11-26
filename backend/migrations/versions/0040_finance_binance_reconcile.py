"""Binance: the times each wallet account was reported at, and the reconciliation
differences: intervals between two consecutive reports of one account whose reported change
of an instrument the records posted in between do not explain (design 5.11, plan Decision
11). A report time is kept whether or not the report listed anything: a wallet's report is
complete, so an asset it does not list is 0 there. A difference is never corrected; one the
records explain later gets `closed_at`.

Revision ID: 0040
Revises: 0039
Create Date: 2026-11-23
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    CREATE TABLE finance.binance_account_reports (
      connection_id uuid NOT NULL REFERENCES core.connections (id),
      account_id uuid NOT NULL REFERENCES finance.accounts (id),
      as_of timestamptz NOT NULL,
      read_to timestamptz NOT NULL,
      PRIMARY KEY (account_id, as_of),
      CHECK (read_to >= as_of)
    )
    """,
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

DOWNGRADE = [
    # The newest report time a Binance connection's last reconciliation covered: the report
    # times and differences go.
    "UPDATE core.connections SET config = config - 'reconciled_through'"
    " WHERE connector = 'binance'",
    "DROP TABLE finance.binance_reconciliation_differences",
    "DROP TABLE finance.binance_account_reports",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
