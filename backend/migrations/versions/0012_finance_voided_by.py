"""A void records who made it (the actor's type), so a connector's own void of a reversed
transaction can give way to later upstream data while the owner's never does.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-14
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Voids made before this: who made each is in the audit log, as the actor of the latest
# command that recorded the transaction as void (a void records it whole; a connector's
# upsert records it as before and after). A void with no such entry stays NULL, which counts
# as the owner's and so never gives way.
BACKFILL_VOIDED_BY = """
    UPDATE finance.transactions t SET voided_by = (
      SELECT a.actor_type FROM core.audit_log a
      WHERE a.changes -> ('transaction:' || t.id) ->> 'status' = 'void'
         OR a.changes -> ('transaction:' || t.id) -> 'after' ->> 'status' = 'void'
      ORDER BY a.occurred_at DESC, a.id DESC
      LIMIT 1
    )
    WHERE t.status = 'void'
"""

UPGRADE = [
    "ALTER TABLE finance.transactions ADD COLUMN voided_by text",
    """
    ALTER TABLE finance.transactions ADD CONSTRAINT transactions_voided_by_only_when_void
      CHECK (voided_by IS NULL OR status = 'void')
    """,
    BACKFILL_VOIDED_BY,
]

DOWNGRADE = [
    "ALTER TABLE finance.transactions DROP CONSTRAINT transactions_voided_by_only_when_void",
    "ALTER TABLE finance.transactions DROP COLUMN voided_by",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
