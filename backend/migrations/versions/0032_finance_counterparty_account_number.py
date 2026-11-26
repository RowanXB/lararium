"""Finance transactions: the other side's account number, when a connector's record gives it
without a sort code (eToro Money's bank transfers, ruling R28).

It is the same evidence an imported row's account number alone gives (transfer-evidence
design §5.1, item 2): the own account whose registered UK bank account has that account
number, when exactly one does, is the one the record names.

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


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE finance.transactions
          ADD COLUMN counterparty_account_number text
            CHECK (counterparty_account_number ~ '^[0-9]{8}$')
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE finance.transactions DROP COLUMN counterparty_account_number")
