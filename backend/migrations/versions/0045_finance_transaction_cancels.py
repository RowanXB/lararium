"""Finance transactions: the transaction one cancels.

A cancelled eToro withdrawal and its cancellation post against In transit, the cancellation
naming the request it cancels, so the transfer engine can tell the pair from a transfer
without reading a connector's tables (eToro cancelled withdrawal design §4.2).

Revision ID: 0045
Revises: 0044
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    ALTER TABLE finance.transactions
      ADD COLUMN cancels_transaction_id uuid REFERENCES finance.transactions (id),
      ADD CONSTRAINT transactions_cancels_another CHECK (cancels_transaction_id <> id)
    """,
    # Cancelled by at most one live transaction: a void one keeps its link as it was voided
    # (the owner's void is never rewritten) without holding the transaction it named.
    """
    CREATE UNIQUE INDEX transactions_cancels_once ON finance.transactions
      (cancels_transaction_id) WHERE status <> 'void'
    """,
]

DOWNGRADE = [
    "DROP INDEX finance.transactions_cancels_once",
    """
    ALTER TABLE finance.transactions
      DROP CONSTRAINT transactions_cancels_another,
      DROP COLUMN cancels_transaction_id
    """,
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
