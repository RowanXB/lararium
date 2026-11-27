"""A payee bank account held by several own accounts, in place of its references.

A shared payee bank account's per-account payment references (0039) can't tell the owner's
accounts apart: a platform's reference is not fixed, so the one a payment uses differs from
the one its app shows (account type catalog design §8.1). The table goes. Instead a
uk_bank_account identifier may be held by several own accounts, as a card merchant is: its
holders are rows of finance.account_identifier_holders, whose kind (still under the foreign
key to the identifier's (id, kind)) now takes uk_bank_account too. The kind has no default
any more: each row states its identifier's. That an identifier has an account or holders,
never both, is kept by identifiers.py.

Downgrading deletes the bank accounts' holder rows (the next heal unpairs what they paired),
restores the kind's CHECK and default, and recreates the references table as 0039 made it,
empty.

Revision ID: 0045
Revises: 0044
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    "DROP TABLE finance.account_identifier_references",
    """
    ALTER TABLE finance.account_identifier_holders
      ALTER COLUMN kind DROP DEFAULT,
      DROP CONSTRAINT account_identifier_holders_kind_check,
      ADD CONSTRAINT account_identifier_holders_kind_check
        CHECK (kind IN ('card_merchant', 'uk_bank_account'))
    """,
]

DOWNGRADE = [
    "DELETE FROM finance.account_identifier_holders WHERE kind = 'uk_bank_account'",
    """
    ALTER TABLE finance.account_identifier_holders
      ALTER COLUMN kind SET DEFAULT 'card_merchant',
      DROP CONSTRAINT account_identifier_holders_kind_check,
      ADD CONSTRAINT account_identifier_holders_kind_check CHECK (kind = 'card_merchant')
    """,
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
