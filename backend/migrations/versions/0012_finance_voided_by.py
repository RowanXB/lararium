"""A void records who made it (the actor's type), so a connector's own void of a reversed
transaction can give way to later upstream data while the owner's never does.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-14
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    # NULL on voids made before this: treated as the owner's, which never give way.
    "ALTER TABLE finance.transactions ADD COLUMN voided_by text",
    """
    ALTER TABLE finance.transactions ADD CONSTRAINT transactions_voided_by_only_when_void
      CHECK (voided_by IS NULL OR status = 'void')
    """,
]

DOWNGRADE = [
    "ALTER TABLE finance.transactions DROP CONSTRAINT transactions_voided_by_only_when_void",
    "ALTER TABLE finance.transactions DROP COLUMN voided_by",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
