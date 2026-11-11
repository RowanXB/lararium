"""Finance instruments: instruments, identifiers, seeded fiat currencies, reporting currency.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-11
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """INSERT INTO core.settings (key, value) VALUES ('finance.reporting_currency', '"GBP"')""",
    """
    CREATE TABLE finance.instruments (
      id uuid PRIMARY KEY DEFAULT uuidv7(),
      kind text NOT NULL CHECK (
        kind IN ('fiat', 'equity', 'etf', 'fund', 'bond', 'crypto', 'option', 'cfd', 'other')
      ),
      symbol text NOT NULL,
      name text NOT NULL,
      decimals int NOT NULL CHECK (decimals BETWEEN 0 AND 30),
      quote_instrument_id uuid REFERENCES finance.instruments (id),
      hidden boolean NOT NULL DEFAULT false,
      owner_entity_id uuid NOT NULL REFERENCES core.entities (id),
      created_at timestamptz NOT NULL DEFAULT now(),
      CHECK (kind <> 'fiat' OR quote_instrument_id IS NULL)
    )
    """,
    """
    CREATE TABLE finance.instrument_identifiers (
      instrument_id uuid NOT NULL REFERENCES finance.instruments (id) ON DELETE CASCADE,
      scheme text NOT NULL CHECK (
        scheme IN (
          'iso4217', 'isin', 'figi', 'ticker_mic', 'ibkr_conid', 'etoro_id', 'chain_contract',
          'coingecko'
        )
      ),
      value text NOT NULL,
      PRIMARY KEY (scheme, value)
    )
    """,
    """
    INSERT INTO finance.instruments (kind, symbol, name, decimals, owner_entity_id)
    SELECT 'fiat', c.symbol, c.name, c.decimals, self.id
    FROM (VALUES
      ('GBP', 'Pound Sterling', 2),
      ('USD', 'US Dollar', 2),
      ('EUR', 'Euro', 2),
      ('CNY', 'Chinese Yuan Renminbi', 2),
      ('HKD', 'Hong Kong Dollar', 2),
      ('CHF', 'Swiss Franc', 2),
      ('CAD', 'Canadian Dollar', 2),
      ('AUD', 'Australian Dollar', 2),
      ('SGD', 'Singapore Dollar', 2),
      ('JPY', 'Japanese Yen', 0)
    ) AS c (symbol, name, decimals)
    CROSS JOIN (SELECT id FROM core.entities WHERE is_self) AS self
    """,
    """
    INSERT INTO finance.instrument_identifiers (instrument_id, scheme, value)
    SELECT id, 'iso4217', symbol FROM finance.instruments WHERE kind = 'fiat'
    """,
]

DOWNGRADE = [
    "DROP TABLE finance.instrument_identifiers",
    "DROP TABLE finance.instruments",
    "DELETE FROM core.settings WHERE key = 'finance.reporting_currency'",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
