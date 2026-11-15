"""Finance import batches: a staged statement file, what it says, and its rows.

A batch holds the workflow (status, profile, covered days); its statement holds what the
file says at statement level; its rows are the file's transactions, staged for review.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _finite(column: str) -> str:
    return f"CHECK ({column} NOT IN ('NaN', 'Infinity', '-Infinity'))"


UPGRADE = [
    """
    CREATE TABLE finance.import_batches (
      id uuid PRIMARY KEY DEFAULT uuidv7(),
      account_id uuid NOT NULL REFERENCES finance.accounts (id),
      profile_id uuid NOT NULL REFERENCES finance.import_profiles (id),
      profile_version integer NOT NULL CHECK (profile_version >= 1),
      profile_config jsonb NOT NULL,
      parser_version text NOT NULL,
      document_id uuid NOT NULL REFERENCES core.documents (id),
      filename text NOT NULL,
      default_instrument_id uuid REFERENCES finance.instruments (id),
      status text NOT NULL DEFAULT 'staged'
        CHECK (status IN ('staged', 'committed', 'discarded', 'rolled_back')),
      warnings jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(warnings) = 'array'),
      covered_from date,
      covered_to date,
      -- The covered days the file lists in full (Decision 16).
      complete_from date,
      complete_to date,
      owner_entity_id uuid NOT NULL REFERENCES core.entities (id),
      created_at timestamptz NOT NULL DEFAULT now(),
      committed_at timestamptz,
      rolled_back_at timestamptz,
      CHECK ((covered_from IS NULL) = (covered_to IS NULL)),
      CHECK (covered_from <= covered_to),
      CHECK ((complete_from IS NULL) = (complete_to IS NULL)),
      CHECK (complete_from <= complete_to),
      CHECK (complete_from >= covered_from AND complete_to <= covered_to),
      CHECK ((committed_at IS NOT NULL) = (status IN ('committed', 'rolled_back'))),
      CHECK ((rolled_back_at IS NOT NULL) = (status = 'rolled_back')),
      -- The target of import_rows' (batch_id, account_id) key.
      UNIQUE (id, account_id)
    )
    """,
    "CREATE INDEX import_batches_account ON finance.import_batches (account_id, status)",
    f"""
    CREATE TABLE finance.import_statements (
      batch_id uuid PRIMARY KEY REFERENCES finance.import_batches (id) ON DELETE CASCADE,
      period_from date,
      period_to date,
      currency text CHECK (currency ~ '^[A-Z]{{3}}$'),
      opening_balance numeric {_finite("opening_balance")},
      closing_balance numeric {_finite("closing_balance")},
      closing_balance_date date,
      money_in numeric {_finite("money_in")},
      money_out numeric {_finite("money_out")},
      payments_in integer CHECK (payments_in >= 0),
      payments_out integer CHECK (payments_out >= 0),
      statement_reference text,
      document_date date,
      account_type text,
      account_name text,
      sort_code text CHECK (sort_code ~ '^[0-9]{{6}}$'),
      account_number text CHECK (account_number ~ '^[0-9]+$'),
      iban text CHECK (iban ~ '^[A-Z]{{2}}[0-9]{{2}}[A-Z0-9]{{1,30}}$'),
      bic text,
      customer_number text,
      clearing_account_number text,
      branch text,
      overdraft_limit numeric {_finite("overdraft_limit")},
      overdraft_limit_date date,
      available_balance numeric {_finite("available_balance")},
      available_balance_date date,
      CHECK ((period_from IS NULL) = (period_to IS NULL)),
      CHECK (period_from <= period_to)
    )
    """,
    f"""
    CREATE TABLE finance.import_statement_rates (
      id uuid PRIMARY KEY DEFAULT uuidv7(),
      batch_id uuid NOT NULL
        REFERENCES finance.import_statements (batch_id) ON DELETE CASCADE,
      position integer NOT NULL CHECK (position >= 0),
      kind text NOT NULL,
      band_from numeric {_finite("band_from")},
      band_to numeric {_finite("band_to")},
      rate numeric {_finite("rate")},
      ear numeric {_finite("ear")},
      UNIQUE (batch_id, position),
      CHECK (rate IS NOT NULL OR ear IS NOT NULL)
    )
    """,
    f"""
    CREATE TABLE finance.import_rows (
      id uuid PRIMARY KEY DEFAULT uuidv7(),
      batch_id uuid NOT NULL,
      -- Denormalised for import_rows_committed_once; the key below keeps it the batch's.
      account_id uuid NOT NULL REFERENCES finance.accounts (id),
      position integer NOT NULL,
      kind text NOT NULL DEFAULT 'statement' CHECK (kind IN ('statement', 'opening_adjustment')),
      status text NOT NULL
        CHECK (status IN ('new', 'duplicate', 'skipped', 'error', 'committed', 'rolled_back')),
      booking_date date,
      occurred_at timestamptz,
      occurred_tz text,
      description text NOT NULL DEFAULT '',
      quantity numeric {_finite("quantity")},
      fee numeric NOT NULL DEFAULT 0 {_finite("fee")},
      instrument_id uuid REFERENCES finance.instruments (id),
      balance numeric {_finite("balance")},
      external_id text,
      bank_type text,
      value_date date,
      payee text,
      memo text,
      check_number text,
      counterparty_name text,
      counterparty_sort_code text CHECK (counterparty_sort_code ~ '^[0-9]{{6}}$'),
      counterparty_account_number text CHECK (counterparty_account_number ~ '^[0-9]+$'),
      counterparty_bank text,
      payment_reference text,
      payment_kind text,
      bank_reference text,
      purpose_code text,
      remittance_info text,
      dedupe_key text,
      -- The committed row this one duplicates (Decision 16), by key or by covered day.
      duplicate_of uuid REFERENCES finance.import_rows (id),
      error text,
      note text,
      raw jsonb NOT NULL CHECK (jsonb_typeof(raw) = 'object'),
      transaction_id uuid REFERENCES finance.transactions (id),
      FOREIGN KEY (batch_id, account_id)
        REFERENCES finance.import_batches (id, account_id) ON DELETE CASCADE,
      UNIQUE (batch_id, position),
      CHECK ((status = 'error') = (error IS NOT NULL)),
      CHECK (duplicate_of IS NULL OR status = 'duplicate'),
      CHECK (
        status NOT IN ('new', 'duplicate', 'committed', 'rolled_back')
        OR (
          booking_date IS NOT NULL
          AND occurred_at IS NOT NULL
          AND quantity IS NOT NULL
          AND instrument_id IS NOT NULL
          AND dedupe_key IS NOT NULL
        )
      ),
      CHECK ((kind = 'opening_adjustment') = (position < 0)),
      CHECK ((status IN ('committed', 'rolled_back')) = (transaction_id IS NOT NULL))
    )
    """,
    # The database backstop for Decision 5: a key is committed at most once per account.
    """
    CREATE UNIQUE INDEX import_rows_committed_once
      ON finance.import_rows (account_id, dedupe_key) WHERE status = 'committed'
    """,
    # Rollback looks up the rows that duplicate a batch's rows (Decision 16).
    """
    CREATE INDEX import_rows_duplicate_of
      ON finance.import_rows (duplicate_of) WHERE duplicate_of IS NOT NULL
    """,
    # Covered days (Decision 16) look up an account's committed rows by day.
    """
    CREATE INDEX import_rows_committed_days
      ON finance.import_rows (account_id, booking_date) WHERE status = 'committed'
    """,
]

DOWNGRADE = [
    "DROP TABLE finance.import_rows",
    "DROP TABLE finance.import_statement_rates",
    "DROP TABLE finance.import_statements",
    "DROP TABLE finance.import_batches",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
