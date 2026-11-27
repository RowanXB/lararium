"""The account_family identifier kind: a family of the account type catalog.

An account family (`trading212`) is an identifier for "an account of this family", whichever
it is. It never has an account or holder rows: whom a record naming it names is read from
the accounts' `type_key` when it is resolved (identifiers.named_accounts). So an
`account_family` row keeps `account_id` NULL, and the holders' kind CHECK (0045) is left
as it is: it can't hold one. That the value is a family of the catalog is checked in code,
as the catalog is code.

What a History CSV's Notes say of a row (account family identifier design §5.9, owner's
decision 2026-11-27), typed: `import_rows.family_move` (true: a move within the account's
family; false: a movement outside it; NULL: the profile says nothing), and
`transactions.outside_family` (the source says the money went outside the family, so the
record is never a family move's counterpart). A Trading 212 export row also types the bank
account its Notes name as sent to: `trading212_export_rows.notes_sort_code` and
`notes_account_number` (digits only).

Downgrading clears `counterparty_identifier_id` on the transactions naming such a row,
deletes the rows, restores the kind CHECK as 0041 left it and drops the four columns.

Revision ID: 0046
Revises: 0045
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = """'uk_bank_account', 'ibkr_account', 'trading212_account', 'etoro_account',
          'card_merchant', 'chain_address'"""


def _kind_check(extra: str) -> str:
    return f"""
    ALTER TABLE finance.account_identifiers
      DROP CONSTRAINT account_identifiers_kind_check,
      ADD CONSTRAINT account_identifiers_kind_check
        CHECK (kind IN ({_KINDS}{extra}))
    """


UPGRADE = [
    _kind_check(", 'account_family'"),
    """
    ALTER TABLE finance.account_identifiers
      ADD CONSTRAINT account_identifiers_account_family_check
        CHECK (kind <> 'account_family' OR account_id IS NULL)
    """,
    "ALTER TABLE finance.transactions ADD COLUMN outside_family boolean NOT NULL DEFAULT false",
    "ALTER TABLE finance.import_rows ADD COLUMN family_move boolean",
    "ALTER TABLE finance.trading212_export_rows ADD COLUMN notes_sort_code text,"
    " ADD COLUMN notes_account_number text",
]

DOWNGRADE = [
    "UPDATE finance.transactions SET counterparty_identifier_id = NULL"
    " WHERE counterparty_identifier_id IN"
    " (SELECT id FROM finance.account_identifiers WHERE kind = 'account_family')",
    "DELETE FROM finance.account_identifiers WHERE kind = 'account_family'",
    """
    ALTER TABLE finance.account_identifiers
      DROP CONSTRAINT account_identifiers_account_family_check
    """,
    _kind_check(""),
    "ALTER TABLE finance.trading212_export_rows DROP COLUMN notes_sort_code,"
    " DROP COLUMN notes_account_number",
    "ALTER TABLE finance.import_rows DROP COLUMN family_move",
    "ALTER TABLE finance.transactions DROP COLUMN outside_family",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
