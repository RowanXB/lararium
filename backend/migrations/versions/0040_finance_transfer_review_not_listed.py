"""Finance transfers: the review reason not_listed_in_full.

When the import-coverage gate (M4 §5.8) is all that keeps an identified record from a
counterpart candidate on a manual account, its transfer awaits review with no placeholder,
which would count the money twice if the candidate is the real row (account type catalog
design §8.3 item 3).

Downgrading deletes those transfers: they are derived, and the next heal opens each again,
inferred with a placeholder, as before.

Revision ID: 0040
Revises: 0039
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    ALTER TABLE finance.transfers
      DROP CONSTRAINT transfers_review_reason_check,
      ADD CONSTRAINT transfers_review_reason_check
        CHECK (review_reason IN ('several_candidates', 'amount_differs', 'not_listed_in_full'))
    """,
]

DOWNGRADE = [
    "DELETE FROM finance.transfers WHERE review_reason = 'not_listed_in_full'",
    """
    ALTER TABLE finance.transfers
      DROP CONSTRAINT transfers_review_reason_check,
      ADD CONSTRAINT transfers_review_reason_check
        CHECK (review_reason IN ('several_candidates', 'amount_differs'))
    """,
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
