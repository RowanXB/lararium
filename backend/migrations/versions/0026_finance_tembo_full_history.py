"""Finance import batches: a Tembo statement lists everything from the account's opening.

The owner confirmed (2026-11-21) that every Tembo statement lists everything from the
account's opening to the day it is made. Batches imported before stored the printed period
(first row to last) and listed in full only the days strictly between those rows, so M4's
gate never took a one-sided transfer into a Tembo account and a registered Tembo account
got a placeholder beside its imported deposit. Their statements now run from the account's
opening to the valuation date, and their batches list in full through the day before it.

Only a batch whose printed dates hold its rows changes, as staging would now give it: one
opened on or before its valuation date, with no row before the one or after the other.
Staging now refuses any other, so it is left as it was imported rather than given a period
its rows fall outside, or one that runs backwards.

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-21
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The Tembo statement s, of batch b, whose printed dates hold its rows.
_FULL_HISTORY = """
b.profile_config ->> 'layout' = 'tembo_statement'
  AND s.account_opened_on IS NOT NULL
  AND s.document_date IS NOT NULL
  AND s.account_opened_on <= s.document_date
  AND NOT EXISTS (
    SELECT FROM finance.import_rows AS r
    WHERE r.batch_id = b.id
      AND r.kind = 'statement'
      AND (r.booking_date < s.account_opened_on OR r.booking_date > s.document_date)
  )
"""

PERIOD = f"""
UPDATE finance.import_statements AS s
SET period_from = s.account_opened_on, period_to = s.document_date
FROM finance.import_batches AS b
WHERE s.batch_id = b.id
  AND {_FULL_HISTORY}
"""

# Covered: the new period and the days the batch covered (its rows'), together, as staging
# takes them; with the rows inside the period, that is the period.
COVERAGE = f"""
UPDATE finance.import_batches AS b
SET covered_from = least(b.covered_from, s.account_opened_on),
    covered_to = greatest(b.covered_to, s.document_date),
    complete_from = CASE WHEN s.document_date - 1 >= s.account_opened_on
                         THEN s.account_opened_on END,
    complete_to = CASE WHEN s.document_date - 1 >= s.account_opened_on
                       THEN s.document_date - 1 END
FROM finance.import_statements AS s
WHERE s.batch_id = b.id
  AND {_FULL_HISTORY}
"""

UPGRADE = [PERIOD, COVERAGE]

# Nothing to undo: the old period and coverage were wrong, not a state to restore, and the
# rows' own days are still in each batch.
DOWNGRADE: list[str] = []


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
