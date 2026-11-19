"""Finance import profiles: how to read one institution's statement files, stored as data.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    CREATE TABLE finance.import_profiles (
      id uuid PRIMARY KEY DEFAULT uuidv7(),
      name text NOT NULL,
      format text NOT NULL CHECK (format IN ('csv', 'ofx', 'qif', 'pdf')),
      config jsonb NOT NULL,
      version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
      owner_entity_id uuid NOT NULL REFERENCES core.entities (id),
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CHECK (config ->> 'format' = format)
    )
    """,
    """
    CREATE UNIQUE INDEX import_profiles_name_unique
      ON finance.import_profiles (lower(btrim(name)))
    """,
]

DOWNGRADE = [
    "DROP TABLE finance.import_profiles",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
