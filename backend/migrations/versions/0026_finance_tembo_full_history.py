"""Finance import batches: a Tembo statement lists everything from the account's opening.

The owner confirmed (2026-11-21) that every Tembo statement lists everything from the
account's opening to the day it is made. Batches imported before stored the printed period
(first row to last) and listed in full only the days strictly between those rows, so M4's
gate never took a one-sided transfer into a Tembo account and a registered Tembo account
got a placeholder beside its imported deposit. Their statements now run from the account's
opening to the valuation date, and their batches list in full through the day before it.

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

PERIOD = """
UPDATE finance.import_statements AS s
SET period_from = s.account_opened_on, period_to = s.document_date
FROM finance.import_batches AS b
WHERE s.batch_id = b.id
  AND b.profile_config ->> 'layout' = 'tembo_statement'
  AND s.account_opened_on IS NOT NULL
  AND s.document_date IS NOT NULL
"""

COVERAGE = """
UPDATE finance.import_batches AS b
SET covered_from = s.account_opened_on,
    covered_to = greatest(b.covered_to, s.document_date),
    complete_from = CASE WHEN s.document_date - 1 >= s.account_opened_on
                         THEN s.account_opened_on END,
    complete_to = CASE WHEN s.document_date - 1 >= s.account_opened_on
                       THEN s.document_date - 1 END
FROM finance.import_statements AS s
WHERE s.batch_id = b.id
  AND b.profile_config ->> 'layout' = 'tembo_statement'
  AND s.account_opened_on IS NOT NULL
  AND s.document_date IS NOT NULL
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
