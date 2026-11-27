"""References on a shared payee bank account: finance.account_identifier_references.

A UK bank account registered to no own account may stand for several (a platform's client
money account that every one of the owner's accounts there is paid through): the owner tells
them apart by the payment reference each is paid with (account type catalog design §8.1).
Only a uk_bank_account row can carry references: a reference row carries its identifier's
kind, fixed to uk_bank_account, under a foreign key to (id, kind). A reference is stored
trimmed and upper-cased, unique within its identifier, and an account has one reference per
identifier. That an identifier carries an account or references, never both, is kept by
identifiers.py.

Revision ID: 0039
Revises: 0038
Create Date: 2026-11-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
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

DOWNGRADE = [
    "DROP TABLE finance.account_identifier_references",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
