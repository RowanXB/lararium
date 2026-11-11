"""Finance balance snapshots, instrument prices and FX rates.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-11
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    CREATE TABLE finance.balance_snapshots (
      id uuid PRIMARY KEY DEFAULT uuidv7(),
      account_id uuid NOT NULL REFERENCES finance.accounts (id),
      instrument_id uuid NOT NULL REFERENCES finance.instruments (id),
      quantity numeric NOT NULL,
      as_of timestamptz NOT NULL,
      as_of_date date NOT NULL,
      kind text NOT NULL CHECK (kind IN ('reported', 'manual')),
      owner_entity_id uuid NOT NULL REFERENCES core.entities (id),
      recorded_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX balance_snapshots_lookup
      ON finance.balance_snapshots (account_id, instrument_id, as_of_date DESC, as_of DESC)
    """,
    """
    CREATE TABLE finance.prices (
      id uuid PRIMARY KEY DEFAULT uuidv7(),
      instrument_id uuid NOT NULL REFERENCES finance.instruments (id),
      quote_instrument_id uuid NOT NULL REFERENCES finance.instruments (id),
      price numeric NOT NULL CHECK (price >= 0),
      as_of_date date NOT NULL,
      source text NOT NULL,
      recorded_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE (instrument_id, quote_instrument_id, as_of_date, source)
    )
    """,
    """
    CREATE TABLE finance.fx_rates (
      id uuid PRIMARY KEY DEFAULT uuidv7(),
      base_instrument_id uuid NOT NULL REFERENCES finance.instruments (id),
      quote_instrument_id uuid NOT NULL REFERENCES finance.instruments (id),
      rate numeric NOT NULL CHECK (rate > 0),
      as_of_date date NOT NULL,
      source text NOT NULL,
      recorded_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE (base_instrument_id, quote_instrument_id, as_of_date, source),
      CHECK (base_instrument_id <> quote_instrument_id)
    )
    """,
]

DOWNGRADE = [
    "DROP TABLE finance.fx_rates",
    "DROP TABLE finance.prices",
    "DROP TABLE finance.balance_snapshots",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
