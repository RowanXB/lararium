"""Finance transactions carry the connection and external id they came from.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    ALTER TABLE finance.transactions
      ADD COLUMN connection_id uuid REFERENCES core.connections (id)
    """,
    "ALTER TABLE finance.transactions ADD COLUMN external_id text",
    """
    ALTER TABLE finance.transactions ADD CONSTRAINT transactions_connector_refs_paired
      CHECK ((connection_id IS NULL) = (external_id IS NULL))
    """,
    """
    CREATE UNIQUE INDEX transactions_connection_external
      ON finance.transactions (connection_id, external_id)
    """,
]

DOWNGRADE = [
    "DROP INDEX finance.transactions_connection_external",
    "ALTER TABLE finance.transactions DROP CONSTRAINT transactions_connector_refs_paired",
    "ALTER TABLE finance.transactions DROP COLUMN external_id",
    "ALTER TABLE finance.transactions DROP COLUMN connection_id",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
