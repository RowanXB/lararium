"""Core documents: uploaded files, stored by content hash outside the database.

Documents are immutable raw data, like the audit log: the backend role may insert and read
them, never change them.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    CREATE TABLE core.documents (
      id uuid PRIMARY KEY DEFAULT uuidv7(),
      sha256 text NOT NULL UNIQUE CHECK (sha256 ~ '^[0-9a-f]{64}$'),
      size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
      media_type text NOT NULL,
      original_filename text NOT NULL,
      source text NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "REVOKE UPDATE, DELETE ON core.documents FROM lararium_backend",
]

DOWNGRADE = [
    "DROP TABLE core.documents",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
