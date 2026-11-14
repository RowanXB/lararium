"""Procrastinate job queue, in its own schema `procrastinate`.

Procrastinate's bundled schema SQL creates its objects unqualified, so it runs here with
`SET LOCAL search_path TO procrastinate`. Its functions and triggers refer to the tables
unqualified too, so each function gets `search_path = procrastinate` of its own (a
trigger fired by an insert from any session finds its tables), and the worker's
connections set the same search_path for the queries it sends (see lararium.worker.app).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-13
"""

from collections.abc import Sequence

from alembic import op
from procrastinate.schema import SchemaManager

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA procrastinate AUTHORIZATION lararium_owner")
    op.execute("SET LOCAL search_path TO procrastinate")
    # Straight to the driver: the script is several statements, not a SQLAlchemy text().
    # Its `%` (RAISE placeholders) are doubled, as SchemaManager.apply_schema does.
    op.get_bind().exec_driver_sql(SchemaManager.get_schema().replace("%", "%%"))
    op.execute("SET LOCAL search_path TO DEFAULT")
    op.execute(
        """
        DO $$
        DECLARE f regprocedure;
        BEGIN
          FOR f IN
            SELECT p.oid::regprocedure FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'procrastinate'
          LOOP
            EXECUTE format('ALTER FUNCTION %s SET search_path = procrastinate', f);
          END LOOP;
        END $$
        """
    )
    for statement in [
        "GRANT USAGE ON SCHEMA procrastinate TO lararium_backend",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA procrastinate"
        " TO lararium_backend",
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA procrastinate TO lararium_backend",
        "GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA procrastinate TO lararium_backend",
    ]:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP SCHEMA procrastinate CASCADE")
