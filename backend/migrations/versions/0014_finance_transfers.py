"""Transfers between the owner's accounts: the counterparty fields a transaction's source
gives, the transfers linking a sending and a receiving record, and the owner's decisions
about them.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The counterparty fields are new whenever this runs, so a Monzo connection's note that it
# filled them (normalize.COUNTERPARTY_FIELDS_KEY) is stale, whatever an earlier downgrade
# left; the next sync fills them again from the raw records.
FORGET_COUNTERPARTY_BACKFILL = """
    UPDATE core.connections
      SET config = config - 'counterparty_fields_v1'
      WHERE config ? 'counterparty_fields_v1'
"""

UPGRADE = [
    """
    ALTER TABLE finance.transactions
      ADD COLUMN counterparty_identifier_id uuid REFERENCES finance.account_identifiers (id),
      ADD COLUMN counterparty_name text,
      ADD COLUMN payment_reference text,
      ADD COLUMN bank_transfer boolean NOT NULL DEFAULT false
    """,
    """
    CREATE INDEX transactions_counterparty_identifier
      ON finance.transactions (counterparty_identifier_id)
    """,
    # The legs reference transactions with no cascade: a linked transaction can't be deleted
    # until it is unlinked, so nothing leaves a transfer pointing at nothing.
    """
    CREATE TABLE finance.transfers (
      id uuid PRIMARY KEY DEFAULT uuidv7(),
      from_account_id uuid NOT NULL REFERENCES finance.accounts (id),
      to_account_id uuid NOT NULL REFERENCES finance.accounts (id),
      instrument_id uuid NOT NULL REFERENCES finance.instruments (id),
      amount numeric NOT NULL CHECK (amount > 0 AND amount NOT IN ('NaN', 'Infinity')),
      status text NOT NULL CHECK (
        status IN ('awaiting', 'inferred', 'matched', 'before_history', 'rejected')
      ),
      review_reason text CHECK (review_reason IN ('several_candidates', 'amount_differs')),
      out_transaction_id uuid UNIQUE REFERENCES finance.transactions (id),
      in_transaction_id uuid UNIQUE REFERENCES finance.transactions (id),
      placeholder_transaction_id uuid UNIQUE REFERENCES finance.transactions (id),
      settlement_transaction_id uuid UNIQUE REFERENCES finance.transactions (id),
      opened_at timestamptz NOT NULL,
      matched_at timestamptz,
      notified_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      CHECK (from_account_id <> to_account_id),
      CHECK (out_transaction_id IS NOT NULL OR in_transaction_id IS NOT NULL)
    )
    """,
    """
    CREATE TABLE finance.transfer_decisions (
      id uuid PRIMARY KEY DEFAULT uuidv7(),
      kind text NOT NULL CHECK (kind IN ('reject', 'never_pair', 'pair')),
      subject_kind text NOT NULL,
      subject_ref text NOT NULL,
      other_kind text,
      other_ref text,
      created_at timestamptz NOT NULL DEFAULT now(),
      CHECK ((kind IN ('never_pair', 'pair')) = (other_kind IS NOT NULL)),
      CHECK ((other_kind IS NULL) = (other_ref IS NULL)),
      UNIQUE NULLS NOT DISTINCT (kind, subject_kind, subject_ref, other_kind, other_ref)
    )
    """,
    FORGET_COUNTERPARTY_BACKFILL,
]

# The code before 0014 posts every contra against Unassigned and knows no derived rows, so
# the ledger goes back to that before the tables go: the placeholders and settlements are
# deleted (their postings cascade), and every recorded posting on In transit moves back to
# Unassigned. The system accounts are found by their settings; In transit itself stays,
# empty, as accounts are never deleted. The counterparty fields go with their columns, and
# so does the note that they were filled.
_SYSTEM_ACCOUNT = "(SELECT (value #>> '{{}}')::uuid FROM core.settings WHERE key = '{}')"
LEDGER_UNDO = [
    # Its rows reference the derived transactions.
    "DELETE FROM finance.transfers",
    "DELETE FROM finance.transactions WHERE origin IN ('inference', 'rule')",
    f"""
    UPDATE finance.postings
      SET account_id = {_SYSTEM_ACCOUNT.format("finance.system_account.unassigned")}
      WHERE account_id = {_SYSTEM_ACCOUNT.format("finance.system_account.in_transit")}
    """,
    FORGET_COUNTERPARTY_BACKFILL,
]

DOWNGRADE = [
    *LEDGER_UNDO,
    "DROP TABLE finance.transfer_decisions",
    "DROP TABLE finance.transfers",
    "DROP INDEX finance.transactions_counterparty_identifier",
    """
    ALTER TABLE finance.transactions
      DROP COLUMN bank_transfer,
      DROP COLUMN payment_reference,
      DROP COLUMN counterparty_name,
      DROP COLUMN counterparty_identifier_id
    """,
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
