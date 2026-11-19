"""Finance import statements: an account identifier that isn't a number, and the open date.

Tembo prints its account as "Account no: ACC-…", which account_number (digits only, as
sort code and account number are matched) can't hold, and the date the account opened.

Numbered after M5's 0019, which isn't merged yet: whichever of the two merges later points
down_revision at the other.

Revision ID: 0020
Revises: 0018
Create Date: 2026-11-19
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    ALTER TABLE finance.import_statements
      ADD COLUMN account_reference text,
      ADD COLUMN account_opened_on date
    """,
]

DOWNGRADE = [
    """
    ALTER TABLE finance.import_statements
      DROP COLUMN account_opened_on,
      DROP COLUMN account_reference
    """,
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
