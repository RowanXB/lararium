"""Finance postings: a cost or price must be in another instrument than the posting's own.

The balance check weighs a posting with a cost or price in that cost or price instrument.
In the posting's own instrument the weight would be the quantity rescaled by any factor,
so a transaction could balance while its quantities don't.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-12
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    ALTER TABLE finance.postings
      ADD CONSTRAINT postings_cost_in_other_instrument
        CHECK (cost_instrument_id <> instrument_id),
      ADD CONSTRAINT postings_price_in_other_instrument
        CHECK (price_instrument_id <> instrument_id)
    """,
]

DOWNGRADE = [
    """
    ALTER TABLE finance.postings
      DROP CONSTRAINT postings_price_in_other_instrument,
      DROP CONSTRAINT postings_cost_in_other_instrument
    """,
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
