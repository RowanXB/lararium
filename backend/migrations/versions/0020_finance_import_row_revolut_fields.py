"""Finance import rows: the payment's start time, card and foreign amount (Revolut).

A Revolut CSV gives when a payment was started as well as when it completed; its PDF
statement prints the card a payment was made with and, for a payment in another currency,
the amount in that currency with Revolut's rate and the ECB's.

Numbered after M5's 0019 (PR #19, not merged when this was written), but revising 0018,
main's head: whichever of the two PRs merges later renumbers its migration and rebases the
chain onto the other.

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
    ALTER TABLE finance.import_rows
      ADD COLUMN started_at timestamptz,
      ADD COLUMN card_number text,
      ADD COLUMN original_amount numeric
        CHECK (original_amount NOT IN ('NaN', 'Infinity', '-Infinity')),
      ADD COLUMN fx_rate numeric CHECK (fx_rate > 0),
      ADD COLUMN fx_reference_rate numeric CHECK (fx_reference_rate > 0)
    """,
]

DOWNGRADE = [
    """
    ALTER TABLE finance.import_rows
      DROP COLUMN fx_reference_rate,
      DROP COLUMN fx_rate,
      DROP COLUMN original_amount,
      DROP COLUMN card_number,
      DROP COLUMN started_at
    """,
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
