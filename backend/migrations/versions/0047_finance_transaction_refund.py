"""Finance transactions: refund, the source's word that money came back for an earlier card
payment, so the cash-flow report counts it as negative spending, not income.

Revision ID: 0047
Revises: 0046
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE finance.transactions ADD COLUMN refund boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE finance.transactions DROP COLUMN refund")
