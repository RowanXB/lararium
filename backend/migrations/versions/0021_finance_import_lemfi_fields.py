"""Finance import fields LemFi statements print: the interest a statement totals apart from
money in, the bank that holds the account, a rate's validity and how often it pays, and a
counterparty's masked account ending ("*1234"), and whether a row's file gave its time of
day (else occurred_at is the day's midnight), which covered-day matching compares.

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-19
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    ALTER TABLE finance.import_statements
      ADD COLUMN interest numeric CHECK (interest NOT IN ('NaN', 'Infinity', '-Infinity')),
      ADD COLUMN bank text
    """,
    """
    ALTER TABLE finance.import_statement_rates
      ADD COLUMN valid_from date,
      ADD COLUMN valid_to date,
      ADD COLUMN frequency text,
      ADD CHECK (valid_from <= valid_to)
    """,
    """
    ALTER TABLE finance.import_rows
      ADD COLUMN counterparty_account_ending text
        CHECK (counterparty_account_ending ~ '^[0-9]+$'),
      ADD COLUMN time_stated boolean NOT NULL DEFAULT false
    """,
]

DOWNGRADE = [
    """
    ALTER TABLE finance.import_rows
      DROP COLUMN counterparty_account_ending, DROP COLUMN time_stated
    """,
    """
    ALTER TABLE finance.import_statement_rates
      DROP COLUMN valid_from, DROP COLUMN valid_to, DROP COLUMN frequency
    """,
    "ALTER TABLE finance.import_statements DROP COLUMN interest, DROP COLUMN bank",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
