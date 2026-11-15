"""Typed columns for fields a source provides: a transaction's original (local-currency)
amount, category, labels, spending flag, scheme and place; an account's product, owner type,
goal and round-up settings; a place's address, coordinates and merchant details; category
splits; and declined transactions, which live outside the ledger.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    ALTER TABLE finance.transactions
      ADD COLUMN original_quantity numeric,
      ADD COLUMN original_instrument_id uuid REFERENCES finance.instruments (id),
      ADD COLUMN category text,
      ADD COLUMN labels text[] NOT NULL DEFAULT '{}',
      ADD COLUMN include_in_spending boolean,
      ADD COLUMN payment_scheme text,
      ADD COLUMN place_entity_id uuid REFERENCES core.entities (id),
      ADD CONSTRAINT transactions_original_quantity_finite
        CHECK (original_quantity NOT IN ('NaN', 'Infinity', '-Infinity')),
      ADD CONSTRAINT transactions_original_amount_both_or_neither
        CHECK ((original_quantity IS NULL) = (original_instrument_id IS NULL))
    """,
    """
    ALTER TABLE finance.accounts
      ADD COLUMN product_type text,
      ADD COLUMN owner_type text,
      ADD COLUMN goal_amount numeric,
      ADD COLUMN locked boolean,
      ADD COLUMN round_up boolean,
      ADD COLUMN round_up_multiplier numeric,
      ADD CONSTRAINT accounts_goal_amount_finite
        CHECK (goal_amount NOT IN ('NaN', 'Infinity', '-Infinity')),
      ADD CONSTRAINT accounts_round_up_multiplier_finite
        CHECK (round_up_multiplier NOT IN ('NaN', 'Infinity', '-Infinity'))
    """,
    """
    ALTER TABLE core.entities
      ADD COLUMN address_street text,
      ADD COLUMN address_city text,
      ADD COLUMN address_region text,
      ADD COLUMN address_postcode text,
      ADD COLUMN address_country text,
      ADD COLUMN address_formatted text,
      ADD COLUMN latitude numeric,
      ADD COLUMN longitude numeric,
      ADD COLUMN location_approximate boolean,
      ADD COLUMN online boolean,
      ADD COLUMN mcc text,
      ADD COLUMN website text,
      ADD COLUMN card_acceptor_website text,
      ADD COLUMN logo_url text,
      ADD CONSTRAINT entities_latitude_finite
        CHECK (latitude NOT IN ('NaN', 'Infinity', '-Infinity')),
      ADD CONSTRAINT entities_longitude_finite
        CHECK (longitude NOT IN ('NaN', 'Infinity', '-Infinity')),
      ADD CONSTRAINT entities_mcc_four_digits CHECK (mcc ~ '^[0-9]{4}$')
    """,
    """
    CREATE TABLE finance.transaction_category_splits (
      transaction_id uuid NOT NULL REFERENCES finance.transactions (id) ON DELETE CASCADE,
      position int NOT NULL,
      category text NOT NULL,
      quantity numeric NOT NULL CHECK (quantity NOT IN ('NaN', 'Infinity', '-Infinity')),
      PRIMARY KEY (transaction_id, position)
    )
    """,
    """
    CREATE TABLE finance.declined_transactions (
      id uuid PRIMARY KEY DEFAULT uuidv7(),
      connection_id uuid NOT NULL REFERENCES core.connections (id),
      external_id text NOT NULL,
      account_id uuid NOT NULL REFERENCES finance.accounts (id),
      occurred_at timestamptz NOT NULL,
      quantity numeric NOT NULL CHECK (quantity NOT IN ('NaN', 'Infinity', '-Infinity')),
      instrument_id uuid NOT NULL REFERENCES finance.instruments (id),
      description text NOT NULL DEFAULT '',
      counterparty_entity_id uuid REFERENCES core.entities (id),
      place_entity_id uuid REFERENCES core.entities (id),
      decline_reason text NOT NULL,
      owner_entity_id uuid NOT NULL REFERENCES core.entities (id),
      recorded_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE (connection_id, external_id)
    )
    """,
]

DOWNGRADE = [
    "DROP TABLE finance.declined_transactions",
    "DROP TABLE finance.transaction_category_splits",
    """
    ALTER TABLE core.entities
      DROP CONSTRAINT entities_mcc_four_digits,
      DROP CONSTRAINT entities_longitude_finite,
      DROP CONSTRAINT entities_latitude_finite,
      DROP COLUMN logo_url,
      DROP COLUMN card_acceptor_website,
      DROP COLUMN website,
      DROP COLUMN mcc,
      DROP COLUMN online,
      DROP COLUMN location_approximate,
      DROP COLUMN longitude,
      DROP COLUMN latitude,
      DROP COLUMN address_formatted,
      DROP COLUMN address_country,
      DROP COLUMN address_postcode,
      DROP COLUMN address_region,
      DROP COLUMN address_city,
      DROP COLUMN address_street
    """,
    """
    ALTER TABLE finance.accounts
      DROP CONSTRAINT accounts_round_up_multiplier_finite,
      DROP CONSTRAINT accounts_goal_amount_finite,
      DROP COLUMN round_up_multiplier,
      DROP COLUMN round_up,
      DROP COLUMN locked,
      DROP COLUMN goal_amount,
      DROP COLUMN owner_type,
      DROP COLUMN product_type
    """,
    """
    ALTER TABLE finance.transactions
      DROP CONSTRAINT transactions_original_amount_both_or_neither,
      DROP CONSTRAINT transactions_original_quantity_finite,
      DROP COLUMN place_entity_id,
      DROP COLUMN payment_scheme,
      DROP COLUMN include_in_spending,
      DROP COLUMN labels,
      DROP COLUMN category,
      DROP COLUMN original_instrument_id,
      DROP COLUMN original_quantity
    """,
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
