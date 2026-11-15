"""The registry of the owner's account identifiers: what a counterparty's details on a
transaction are matched against to tell a transfer between own accounts.

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
    CREATE TABLE finance.account_identifiers (
      id uuid PRIMARY KEY DEFAULT uuidv7(),
      account_id uuid NOT NULL REFERENCES finance.accounts (id),
      kind text NOT NULL CHECK (kind IN ('uk_bank_account')),
      value text NOT NULL,
      created_by text NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE (kind, value),
      CHECK (kind <> 'uk_bank_account' OR value ~ '^[0-9]{6}:[0-9]{8}$')
    )
    """,
]

DOWNGRADE = [
    "DROP TABLE finance.account_identifiers",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
