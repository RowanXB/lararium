"""Card merchants: the card_merchant registry kind and finance.account_identifier_holders.

A card merchant (a Monzo merchant id, `merch_…`) may pay into several own accounts (Trading
212's Invest and ISA share one), so it keeps account_id NULL and names its holders in
finance.account_identifier_holders (M6 design §5.12, Decision 16). Every other kind keeps its
one account in account_id. Only a card_merchant row can have holders: a holder row carries
its identifier's kind, fixed to card_merchant, under a foreign key to (id, kind).

Revision ID: 0035
Revises: 0034
Create Date: 2026-11-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    ALTER TABLE finance.account_identifiers
      DROP CONSTRAINT account_identifiers_kind_check,
      ADD CONSTRAINT account_identifiers_kind_check
        CHECK (kind IN ('uk_bank_account', 'ibkr_account', 'trading212_account',
          'etoro_account', 'card_merchant')),
      ADD CONSTRAINT account_identifiers_card_merchant_check
        CHECK (kind <> 'card_merchant' OR (value ~ '^merch_[0-9A-Za-z]+$' AND account_id IS NULL)),
      ADD CONSTRAINT account_identifiers_id_kind_key UNIQUE (id, kind)
    """,
    """
    CREATE TABLE finance.account_identifier_holders (
      identifier_id uuid NOT NULL,
      kind text NOT NULL DEFAULT 'card_merchant' CHECK (kind = 'card_merchant'),
      account_id uuid NOT NULL REFERENCES finance.accounts (id),
      PRIMARY KEY (identifier_id, account_id),
      FOREIGN KEY (identifier_id, kind) REFERENCES finance.account_identifiers (id, kind)
    )
    """,
]

DOWNGRADE = [
    "DROP TABLE finance.account_identifier_holders",
    "UPDATE finance.transactions SET counterparty_identifier_id = NULL"
    " WHERE counterparty_identifier_id IN"
    " (SELECT id FROM finance.account_identifiers WHERE kind = 'card_merchant')",
    "DELETE FROM finance.account_identifiers WHERE kind = 'card_merchant'",
    """
    ALTER TABLE finance.account_identifiers
      DROP CONSTRAINT account_identifiers_id_kind_key,
      DROP CONSTRAINT account_identifiers_card_merchant_check,
      DROP CONSTRAINT account_identifiers_kind_check,
      ADD CONSTRAINT account_identifiers_kind_check
        CHECK (kind IN ('uk_bank_account', 'ibkr_account', 'trading212_account',
          'etoro_account'))
    """,
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
