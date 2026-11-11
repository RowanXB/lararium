"""Finance accounts: ledger type, subtype, tax wrapper, parent account, source, closing.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-11
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    CREATE TABLE finance.accounts (
      id uuid PRIMARY KEY DEFAULT uuidv7(),
      name text NOT NULL,
      ledger_type text NOT NULL CHECK (
        ledger_type IN ('asset', 'liability', 'income', 'expense', 'equity')
      ),
      subtype text NOT NULL CHECK (
        subtype IN (
          'bank', 'savings_pot', 'broker', 'exchange', 'wallet', 'in_transit', 'manual_asset',
          'other'
        )
      ),
      institution_entity_id uuid REFERENCES core.entities (id),
      owner_entity_id uuid NOT NULL REFERENCES core.entities (id),
      tax_wrapper text NOT NULL DEFAULT 'none' CHECK (
        tax_wrapper IN ('none', 'isa', 'sipp', 'lisa', 'gia')
      ),
      parent_account_id uuid REFERENCES finance.accounts (id),
      balance_mode text NOT NULL CHECK (balance_mode IN ('transactions', 'snapshots', 'hybrid')),
      source_kind text NOT NULL DEFAULT 'manual' CHECK (source_kind IN ('manual', 'connection')),
      source_connection_id uuid,  -- the foreign key to core.connections is added in plan 3
      status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
      opened_on date,
      closed_on date,
      created_at timestamptz NOT NULL DEFAULT now(),
      CHECK ((source_kind = 'manual') = (source_connection_id IS NULL)),
      CHECK ((status = 'closed') = (closed_on IS NOT NULL))
    )
    """,
]

DOWNGRADE = [
    "DROP TABLE finance.accounts",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
