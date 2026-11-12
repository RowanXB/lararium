"""Core baseline: schemas, privileges, settings, entities, events, audit log.

Revision ID: 0001
Revises:
Create Date: 2026-11-11
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    "CREATE SCHEMA core AUTHORIZATION lararium_owner",
    "CREATE SCHEMA finance AUTHORIZATION lararium_owner",
    "GRANT USAGE ON SCHEMA core, finance TO lararium_backend",
    """
    ALTER DEFAULT PRIVILEGES FOR ROLE lararium_owner IN SCHEMA core, finance
      GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO lararium_backend
    """,
    """
    CREATE TABLE core.settings (
      key text PRIMARY KEY,
      value jsonb NOT NULL,
      updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """INSERT INTO core.settings (key, value) VALUES ('core.timezone', '"Europe/London"')""",
    """
    CREATE TABLE core.entities (
      id uuid PRIMARY KEY DEFAULT uuidv7(),
      kind text NOT NULL CHECK (kind IN ('person', 'organization', 'place')),
      name text NOT NULL,
      aliases text[] NOT NULL DEFAULT '{}',
      external_ids jsonb NOT NULL DEFAULT '{}',
      is_self boolean NOT NULL DEFAULT false,
      owner_entity_id uuid REFERENCES core.entities (id),
      created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX entities_single_self ON core.entities (is_self) WHERE is_self",
    "INSERT INTO core.entities (kind, name, is_self) VALUES ('person', 'Me', true)",
    "UPDATE core.entities SET owner_entity_id = id WHERE is_self",
    """
    CREATE TABLE core.events (
      id uuid PRIMARY KEY DEFAULT uuidv7(),
      type text NOT NULL,
      source text NOT NULL,
      subject_id uuid,
      occurred_at timestamptz NOT NULL DEFAULT now(),
      data jsonb NOT NULL,
      version int NOT NULL CHECK (version >= 1),
      published_at timestamptz
    )
    """,
    """
    CREATE FUNCTION core.notify_event() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
      PERFORM pg_notify('core_events', NEW.id::text);
      RETURN NULL;
    END;
    $$
    """,
    """
    CREATE TRIGGER events_notify AFTER INSERT ON core.events
      FOR EACH ROW EXECUTE FUNCTION core.notify_event()
    """,
    """
    CREATE TABLE core.audit_log (
      id uuid PRIMARY KEY DEFAULT uuidv7(),
      actor_type text NOT NULL
        CHECK (actor_type IN ('user', 'rule', 'connector', 'agent', 'system')),
      actor_id text NOT NULL,
      command text NOT NULL,
      args jsonb NOT NULL,
      changes jsonb NOT NULL DEFAULT '{}',
      correlation_id uuid NOT NULL,
      occurred_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "REVOKE UPDATE, DELETE ON core.audit_log FROM lararium_backend",
]

DOWNGRADE = [
    "DROP SCHEMA finance CASCADE",
    "DROP SCHEMA core CASCADE",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
