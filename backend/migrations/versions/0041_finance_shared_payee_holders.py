"""Drop finance.account_identifier_references.

A shared payee bank account's per-account payment references (0039) can't tell the owner's
accounts apart: a platform's reference is not fixed, so the one a payment uses differs from
the one its app shows (account type catalog design §8.1). The table goes.

Downgrading recreates it as 0039 made it, empty.

Revision ID: 0041
Revises: 0040
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    "DROP TABLE finance.account_identifier_references",
]

DOWNGRADE = [
    """
    CREATE TABLE finance.account_identifier_references (
      identifier_id uuid NOT NULL,
      kind text NOT NULL DEFAULT 'uk_bank_account' CHECK (kind = 'uk_bank_account'),
      account_id uuid NOT NULL REFERENCES finance.accounts (id),
      reference text NOT NULL CHECK (reference = upper(btrim(reference)) AND reference <> ''),
      PRIMARY KEY (identifier_id, reference),
      UNIQUE (identifier_id, account_id),
      FOREIGN KEY (identifier_id, kind) REFERENCES finance.account_identifiers (id, kind)
    )
    """,
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
