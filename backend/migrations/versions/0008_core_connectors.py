"""Core connectors: connections, encrypted credentials, raw records, the webhook inbox.

Raw records are immutable: the backend role may insert and read them, never change them.
The last statement adds the foreign key from finance.accounts.source_connection_id that
migration 0003 left for this plan.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-12
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    CREATE TABLE core.connections (
      id uuid PRIMARY KEY DEFAULT uuidv7(),
      connector text NOT NULL,
      display_name text NOT NULL,
      status text NOT NULL CHECK (
        status IN ('created', 'authorising', 'healthy', 'failing', 'needs_reauth', 'disabled')
      ),
      config jsonb NOT NULL DEFAULT '{}',
      cursor jsonb,
      last_success_at timestamptz,
      last_error text,
      consecutive_failures int NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
      credential_expires_at timestamptz,
      warnings jsonb NOT NULL DEFAULT '[]',
      owner_entity_id uuid NOT NULL REFERENCES core.entities (id),
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE core.credentials (
      connection_id uuid NOT NULL REFERENCES core.connections (id) ON DELETE CASCADE,
      purpose text NOT NULL,
      nonce bytea NOT NULL,
      ciphertext bytea NOT NULL,
      key_version int NOT NULL DEFAULT 1,
      expires_at timestamptz,
      updated_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (connection_id, purpose)
    )
    """,
    """
    CREATE TABLE core.raw_records (
      id uuid PRIMARY KEY DEFAULT uuidv7(),
      connection_id uuid NOT NULL REFERENCES core.connections (id),
      sync_id uuid NOT NULL,
      record_type text NOT NULL,
      request jsonb NOT NULL,
      fetched_at timestamptz NOT NULL DEFAULT now(),
      content jsonb NOT NULL,
      content_hash text NOT NULL,
      connector_version text NOT NULL,
      UNIQUE (connection_id, record_type, content_hash)
    )
    """,
    "CREATE INDEX raw_records_sync ON core.raw_records (sync_id)",
    "REVOKE UPDATE, DELETE ON core.raw_records FROM lararium_backend",
    """
    CREATE TABLE core.webhook_inbox (
      id uuid PRIMARY KEY DEFAULT uuidv7(),
      source text NOT NULL,
      body jsonb NOT NULL,
      received_at timestamptz NOT NULL DEFAULT now(),
      processed_at timestamptz,
      outcome text
    )
    """,
    """
    ALTER TABLE finance.accounts
      ADD CONSTRAINT accounts_source_connection_fkey
        FOREIGN KEY (source_connection_id) REFERENCES core.connections (id)
    """,
]

DOWNGRADE = [
    "ALTER TABLE finance.accounts DROP CONSTRAINT accounts_source_connection_fkey",
    "DROP TABLE core.webhook_inbox",
    "DROP TABLE core.raw_records",
    "DROP TABLE core.credentials",
    "DROP TABLE core.connections",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
