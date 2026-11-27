"""Income kinds and the owner's transaction categories.

Income statistics group money received by what it is (interest, a dividend, share lending, a
reward in coins) and let the owner name a category of their own for any transaction
(income categories design §4.1).

finance.transactions.income_kind is what the source states about an income, never the
owner's word, so the source's own `category` stays untouched. finance.transaction_owner_categories
holds the owner's category by the transaction's source reference, as transfer_decisions does,
so it survives a rebuild of the transaction from the same source; the category is stored
normalised (lower case, at most 64 characters), and clearing it deletes the row. The
backend role gets its privileges on the table from the default privileges 0001 set for the
finance schema.

The backfill gives `interest` to the transaction of every committed import row that states
interest, or that has a duplicate in a committed batch stating it: most CSV profiles cannot
state interest while the PDF layouts do, so with the CSV committed first the PDF's rows land
as duplicates. Connectors fill the column when their next sync replays under a new
normalizer version.

Downgrading drops the table and the column.

Revision ID: 0047
Revises: 0046
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BACKFILL = """
UPDATE finance.transactions t
SET income_kind = 'interest'
WHERE t.income_kind IS NULL
  AND EXISTS (
    SELECT 1
    FROM finance.import_rows r
    WHERE r.transaction_id = t.id
      AND r.status = 'committed'
      AND (
        r.interest
        OR EXISTS (
          SELECT 1
          FROM finance.import_rows d
          JOIN finance.import_batches b ON b.id = d.batch_id
          WHERE d.duplicate_of = r.id AND d.interest AND b.status = 'committed'
        )
      )
  )
"""

UPGRADE = [
    """
    ALTER TABLE finance.transactions
      ADD COLUMN income_kind text
        CHECK (income_kind IN ('interest', 'dividend', 'share_lending', 'reward'))
    """,
    """
    CREATE TABLE finance.transaction_owner_categories (
      source_kind text NOT NULL,
      source_ref text NOT NULL,
      category text NOT NULL
        CHECK (category <> '' AND length(category) <= 64 AND category = lower(category)),
      decided_at timestamptz NOT NULL DEFAULT now(),
      decided_by text NOT NULL,
      PRIMARY KEY (source_kind, source_ref)
    )
    """,
    BACKFILL,
]

DOWNGRADE = [
    "DROP TABLE finance.transaction_owner_categories",
    "ALTER TABLE finance.transactions DROP COLUMN income_kind",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
