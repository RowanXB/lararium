"""Finance ledger: transactions and their double-entry postings.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-11
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    CREATE TABLE finance.transactions (
      id uuid PRIMARY KEY DEFAULT uuidv7(),
      occurred_at timestamptz NOT NULL,
      occurred_tz text,
      booking_date date NOT NULL,
      value_date date,
      description text NOT NULL DEFAULT '',
      counterparty_entity_id uuid REFERENCES core.entities (id),
      status text NOT NULL CHECK (status IN ('pending', 'booked', 'inferred', 'confirmed', 'void')),
      origin text NOT NULL CHECK (origin IN ('connector', 'manual', 'inference', 'rule')),
      owner_entity_id uuid NOT NULL REFERENCES core.entities (id),
      recorded_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX transactions_booking_date ON finance.transactions (booking_date)",
    """
    CREATE TABLE finance.postings (
      id uuid PRIMARY KEY DEFAULT uuidv7(),
      transaction_id uuid NOT NULL REFERENCES finance.transactions (id) ON DELETE CASCADE,
      position int NOT NULL,
      account_id uuid NOT NULL REFERENCES finance.accounts (id),
      instrument_id uuid NOT NULL REFERENCES finance.instruments (id),
      quantity numeric NOT NULL
        CHECK (quantity <> 0 AND quantity NOT IN ('NaN', 'Infinity', '-Infinity')),
      cost_per_unit numeric CHECK (cost_per_unit NOT IN ('NaN', 'Infinity', '-Infinity')),
      cost_instrument_id uuid REFERENCES finance.instruments (id),
      price_per_unit numeric CHECK (price_per_unit NOT IN ('NaN', 'Infinity', '-Infinity')),
      price_instrument_id uuid REFERENCES finance.instruments (id),
      is_fee boolean NOT NULL DEFAULT false,
      memo text NOT NULL DEFAULT '',
      UNIQUE (transaction_id, position),
      CHECK ((cost_per_unit IS NULL) = (cost_instrument_id IS NULL)),
      CHECK ((price_per_unit IS NULL) = (price_instrument_id IS NULL))
    )
    """,
    "CREATE INDEX postings_account_instrument ON finance.postings (account_id, instrument_id)",
]

DOWNGRADE = [
    "DROP TABLE finance.postings",
    "DROP TABLE finance.transactions",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
