"""eToro statements: the two currencies of Account Summary's "Currency" label.

Real statements give the label as two ISO codes joined by a slash, `USD/GBP` (ruling R41): the
Trading account's currency, and the second one eToro also reports figures in (the `(GBP)`
columns). The label stays as given in `currency`; its two codes are typed beside it, in
`account_currency` and `second_currency` (NULL when the label names one).

Revision ID: 0036
Revises: 0035
Create Date: 2026-11-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE finance.etoro_statements
          ADD COLUMN account_currency text,
          ADD COLUMN second_currency text
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE finance.etoro_statements"
        " DROP COLUMN account_currency, DROP COLUMN second_currency"
    )
