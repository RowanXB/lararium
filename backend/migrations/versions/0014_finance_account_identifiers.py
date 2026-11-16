"""The registry of bank accounts any source names: with an account, one of the owner's
(what a counterparty's details are matched against to tell a transfer between own
accounts); without, a counterparty's, or one the owner dismissed as not theirs.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    CREATE TABLE finance.account_identifiers (
      id uuid PRIMARY KEY DEFAULT uuidv7(),
      kind text NOT NULL CHECK (kind IN ('uk_bank_account')),
      value text NOT NULL,
      account_id uuid REFERENCES finance.accounts (id),
      dismissed_at timestamptz,
      created_by text NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE (kind, value),
      CHECK (kind <> 'uk_bank_account' OR value ~ '^[0-9]{6}:[0-9]{8}$'),
      CHECK (account_id IS NULL OR dismissed_at IS NULL)
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
