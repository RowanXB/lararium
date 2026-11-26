"""eToro order lookups: the order's `externalOperation`.

eToro sends the object with some orders: its `type`, its `referenceId` (text, as eToro's other
ids of that form are kept) and `data`, a string holding the JSON text of an object. The string
stays as sent in `external_operation_data`; the object's members, all numbers, are typed beside
it. Every column is NULL for an order without the object.

Revision ID: 0037
Revises: 0036
Create Date: 2026-11-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE finance.etoro_orders
          ADD COLUMN external_operation_type text,
          ADD COLUMN external_operation_reference_id text,
          ADD COLUMN external_operation_data text,
          ADD COLUMN external_operation_data_creditor_type_id numeric,
          ADD COLUMN external_operation_data_debitor_type_id numeric,
          ADD COLUMN external_operation_data_id numeric,
          ADD COLUMN external_operation_data_status_id numeric
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE finance.etoro_orders
          DROP COLUMN external_operation_type,
          DROP COLUMN external_operation_reference_id,
          DROP COLUMN external_operation_data,
          DROP COLUMN external_operation_data_creditor_type_id,
          DROP COLUMN external_operation_data_debitor_type_id,
          DROP COLUMN external_operation_data_id,
          DROP COLUMN external_operation_data_status_id
        """
    )
