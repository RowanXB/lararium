"""Finance import fields LemFi statements print: the interest a statement totals apart from
money in, the bank that holds the account, a rate's validity and how often it pays, and a
counterparty's masked account ending ("*1234"), and to what a row's file gave its time of
day (None: it gave none, and occurred_at is the day's midnight), which covered-day matching
compares. Rows imported before take it from their batch's profile and their raw row.

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-19
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
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
      ADD COLUMN time_precision text CHECK (time_precision IN ('minute', 'second'))
    """,
]

# Before 0022 only CSVs whose date format has a time directive (as the profile reads it,
# "%%" being a literal "%") and OFX rows whose DTPOSTED has one gave a time; PDF layouts
# printed none.
BACKFILL_TIME = """
UPDATE finance.import_rows AS r
SET time_precision = CASE
    WHEN b.profile_config ->> 'format' = 'ofx'
      OR replace(b.profile_config ->> 'date_format', '%%', '') ~ '%S' THEN 'second'
    ELSE 'minute'
  END
FROM finance.import_batches AS b
WHERE b.id = r.batch_id
  AND r.kind = 'statement'
  AND r.booking_date IS NOT NULL
  AND (
    (b.profile_config ->> 'format' = 'ofx' AND r.raw ->> 'DTPOSTED' ~ '^[0-9]{14}')
    OR (
      b.profile_config ->> 'format' = 'csv'
      AND replace(b.profile_config ->> 'date_format', '%%', '') ~ '%[HIMSp]'
    )
  )
"""

UPGRADE.append(BACKFILL_TIME)

DOWNGRADE = [
    """
    ALTER TABLE finance.import_rows
      DROP COLUMN counterparty_account_ending, DROP COLUMN time_precision
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
