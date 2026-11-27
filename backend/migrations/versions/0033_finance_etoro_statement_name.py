"""eToro statements: the second "Name" row of Account Summary.

The owner's statements list the label "Name" twice (the statement probe), and
finance.etoro_statements had one column for it. The second row is typed in columns of its own,
`name_occurrence_1` and `name_occurrence_1_2` (its column C), as etoro/fields.py names them
(REPEATED_LABELS).

Revision ID: 0033
Revises: 0032
Create Date: 2026-11-23
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE finance.etoro_statements
          ADD COLUMN name_occurrence_1 text,
          ADD COLUMN name_occurrence_1_2 text
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE finance.etoro_statements"
        " DROP COLUMN name_occurrence_1, DROP COLUMN name_occurrence_1_2"
    )
