"""The webhook receiver's role may insert into core.webhook_inbox, and nothing else.

lararium_webhook_in gets USAGE on schema core and INSERT on the inbox: no SELECT (so no
RETURNING either), and no access to any other table. roles.sql creates the role; on a
database whose roles predate it, the grants are skipped with a notice.

Also indexes the unprocessed deliveries, which the worker claims every minute.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    DO $$
    BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'lararium_webhook_in') THEN
        GRANT USAGE ON SCHEMA core TO lararium_webhook_in;
        GRANT INSERT ON core.webhook_inbox TO lararium_webhook_in;
      ELSE
        RAISE NOTICE 'role lararium_webhook_in does not exist: skipping its grants';
      END IF;
    END $$
    """,
    """
    CREATE INDEX webhook_inbox_unprocessed ON core.webhook_inbox (received_at)
      WHERE processed_at IS NULL
    """,
]

DOWNGRADE = [
    "DROP INDEX core.webhook_inbox_unprocessed",
    """
    DO $$
    BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'lararium_webhook_in') THEN
        REVOKE INSERT ON core.webhook_inbox FROM lararium_webhook_in;
        REVOKE USAGE ON SCHEMA core FROM lararium_webhook_in;
      END IF;
    END $$
    """,
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
