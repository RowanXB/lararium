"""Finance transactions: never_transfer, the source's word that a movement was not a transfer
(a Binance withdrawal that failed after it left the balance), so it is never a transfer's leg.

Revision ID: 0039
Revises: 0038
Create Date: 2026-11-23
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE finance.transactions ADD COLUMN never_transfer boolean NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    # The last raw record a Binance connection applied: its marks go with the column, so the
    # records are applied again once it is back.
    op.execute(
        "UPDATE core.connections SET config = config - 'normalized_through'"
        " WHERE connector = 'binance'"
    )
    op.execute("ALTER TABLE finance.transactions DROP COLUMN never_transfer")
