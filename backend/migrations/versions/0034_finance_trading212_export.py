"""Trading 212's CSV export: finance.trading212_export_rows.

The export is optional (M6 design §5.3a): uploaded to a Trading 212 connection, each row is a
raw record (`trading212.export_row`), typed here, keyed by its connection and `ID`. A row whose
`ID` is an API transaction's reference, or one the export alone posts, links that ledger row,
which it outlives (SET NULL). One column per known column, named by Decision 3's rule from its
header (trading212/fields.py, EXPORT_KINDS); `notes_transaction_id` is the card's
"Transaction ID: <id>" read from Notes.

Revision ID: 0034
Revises: 0033
Create Date: 2026-11-23
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE finance.trading212_export_rows (
          connection_id uuid NOT NULL REFERENCES core.connections (id),
          transaction_id uuid UNIQUE REFERENCES finance.transactions (id) ON DELETE SET NULL,
          raw_record_id uuid NOT NULL REFERENCES core.raw_records (id),
          "action" text NOT NULL,
          "time_utc" timestamptz NOT NULL,
          "notes" text,
          "id" text NOT NULL,
          "total" numeric NOT NULL CHECK ("total" NOT IN ('NaN', 'Infinity', '-Infinity')),
          "currency_total" text NOT NULL,
          "notes_transaction_id" text,
          PRIMARY KEY (connection_id, id)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE finance.trading212_export_rows")
