"""Finance import rows: interest marks a row that pays interest, which is never a bank
transfer. LemFi prints no type codes, so its savings interest payouts were marked bank
transfers and could be paired with a transfer of the same amount.

Rows imported before are marked by LemFi's fixed text for a payout, which no other catalog
bank prints, and their transactions are no bank transfers. The downgrade keeps those
corrected values: they were wrong. Stored LemFi CSV profiles get the interest
descriptions their catalog entry now has, and a new version.

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-21
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADD_COLUMN = """
ALTER TABLE finance.import_rows ADD COLUMN interest boolean NOT NULL DEFAULT false
"""

BACKFILL_INTEREST = """
UPDATE finance.import_rows SET interest = true
WHERE kind = 'statement' AND description = 'Savings Interest Payout'
"""

NO_TRANSFER = """
UPDATE finance.transactions AS t SET bank_transfer = false
FROM finance.import_rows AS r
WHERE r.transaction_id = t.id AND r.interest AND t.bank_transfer
"""

# Profiles created from the catalog's LemFi CSV entry store its config as it was then, without
# the interest descriptions; they are found by LemFi's own headers and date format.
PROFILES = """
UPDATE finance.import_profiles
SET config = config || '{"interest_descriptions": ["Savings Interest Payout"]}'::jsonb,
    version = version + 1,
    updated_at = now()
WHERE format = 'csv'
  AND config -> 'columns' ->> 'inflow' = 'Money in (GBP)'
  AND config -> 'columns' ->> 'outflow' = 'Money out (GBP)'
  AND config -> 'columns' ->> 'balance' = 'Balance (GBP)'
  AND config ->> 'date_format' = '%d %b, %Y %H:%M'
  AND coalesce(jsonb_array_length(config -> 'interest_descriptions'), 0) = 0
"""

UPGRADE = [ADD_COLUMN, BACKFILL_INTEREST, NO_TRANSFER, PROFILES]

DOWNGRADE = ["ALTER TABLE finance.import_rows DROP COLUMN interest"]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
