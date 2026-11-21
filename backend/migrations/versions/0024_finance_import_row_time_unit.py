"""Finance import rows: what the file gave a row's time to (time_unit): "day" when it gave
only a date, so occurred_at is that day's midnight, else "minute" or "second". Set exactly
when occurred_at is.

Rows imported before take it from their batch's profile and their raw row, as the parsers
read them by then: a CSV whose date format has a time directive ("%S": seconds, else
minutes; "%%" is a literal "%"), an OFX DTPOSTED with a time (always to the second), and a
LemFi PDF row whose date the statement prints with a time (to the minute). Every other row,
and every opening adjustment, has only its day.

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-21
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADD_COLUMN = """
ALTER TABLE finance.import_rows
  ADD COLUMN time_unit text CHECK (time_unit IN ('day', 'minute', 'second'))
"""

BACKFILL = """
UPDATE finance.import_rows AS r
SET time_unit = CASE
    WHEN r.kind <> 'statement' THEN 'day'
    WHEN b.profile_config ->> 'format' = 'ofx' AND r.raw ->> 'DTPOSTED' ~ '^[0-9]{14}'
      THEN 'second'
    WHEN b.profile_config ->> 'format' = 'csv'
      AND replace(b.profile_config ->> 'date_format', '%%', '') ~ '%S' THEN 'second'
    WHEN b.profile_config ->> 'format' = 'csv'
      AND replace(b.profile_config ->> 'date_format', '%%', '') ~ '%[HIMp]' THEN 'minute'
    WHEN b.profile_config ->> 'format' = 'pdf'
      AND b.profile_config ->> 'layout' = 'lemfi_statement'
      AND r.raw ->> 'Date' ~ ' ([01][0-9]|2[0-3]):[0-5][0-9]$' THEN 'minute'
    ELSE 'day'
  END
FROM finance.import_batches AS b
WHERE b.id = r.batch_id AND r.occurred_at IS NOT NULL
"""

ADD_CHECK = """
ALTER TABLE finance.import_rows
  ADD CONSTRAINT import_rows_time_unit_with_time
    CHECK ((time_unit IS NULL) = (occurred_at IS NULL))
"""

UPGRADE = [ADD_COLUMN, BACKFILL, ADD_CHECK]

DOWNGRADE = ["ALTER TABLE finance.import_rows DROP COLUMN time_unit"]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
