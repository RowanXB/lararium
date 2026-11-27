"""Finance accounts: the account type an account is (account type catalog, design §6.2).

`type_key` is a key of the catalog (finance/account_types.py), or NULL when the account has
none. It is stored only: nothing reads it for posting yet. A Trading 212 connection's account
gets its key from the connection's account kind; the connector keeps it set from then on.

Revision ID: 0038
Revises: 0037
Create Date: 2026-11-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BACKFILL = """
UPDATE finance.accounts a
   SET type_key = CASE c.config ->> 'account_kind'
                    WHEN 'invest' THEN 'trading212.invest'
                    WHEN 'isa' THEN 'trading212.stocks_isa' END
  FROM core.connections c
 WHERE a.source_connection_id = c.id AND c.connector = 'trading212'
"""


def upgrade() -> None:
    op.execute("ALTER TABLE finance.accounts ADD COLUMN type_key text")
    op.execute(BACKFILL)


def downgrade() -> None:
    op.execute("ALTER TABLE finance.accounts DROP COLUMN type_key")
