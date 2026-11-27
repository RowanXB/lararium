"""Keep `transactions.income_kind` right for statement imports.

A statement event is interest when its committed row, or a committed duplicate surely of that
row, says so: most CSV profiles can't state interest while the PDF layouts do, so with the CSV
committed first the PDF's rows land as duplicates and carry the mark. A batch's commit or
rollback can therefore change the kind of a transaction of another batch.

Migration 0048's BACKFILL is the same rule over existing data; a migration can't import this
module, so the two are mirrored. Change them together.
"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# The transactions this batch's rows reach: its own rows' (rolled-back ones are void and
# skipped), and those of the committed rows its duplicate rows point at. A duplicate speaks
# for its original only when it is surely that row, mirroring contra's surely_its_original:
# matched by its key, or the one committed row of its account on that day with that
# instrument and amount. Otherwise it was matched by day and amount alone (Decision 16), in
# file order, and among alike rows it may be another's.
_REFRESH = text(
    """
    UPDATE finance.transactions t
    SET income_kind = CASE WHEN EXISTS (
        SELECT 1
        FROM finance.import_rows r
        WHERE r.transaction_id = t.id
          AND r.status = 'committed'
          AND (
            r.interest
            OR EXISTS (
              SELECT 1
              FROM finance.import_rows d
              JOIN finance.import_batches b ON b.id = d.batch_id
              WHERE d.duplicate_of = r.id AND d.interest AND b.status = 'committed'
                AND (
                  d.dedupe_key = r.dedupe_key
                  OR NOT EXISTS (
                    SELECT 1
                    FROM finance.import_rows s
                    WHERE s.account_id = d.account_id
                      AND s.status = 'committed'
                      AND s.booking_date = d.booking_date
                      AND s.kind = 'statement'
                      AND s.instrument_id = d.instrument_id
                      AND s.quantity = d.quantity
                      AND s.id <> d.duplicate_of
                  )
                )
            )
          )
      ) THEN 'interest' END
    WHERE t.status <> 'void'
      AND t.id IN (
        SELECT r.transaction_id
        FROM finance.import_rows r
        WHERE r.batch_id = :batch AND r.transaction_id IS NOT NULL
        UNION
        SELECT c.transaction_id
        FROM finance.import_rows d
        JOIN finance.import_rows c ON c.id = d.duplicate_of
        WHERE d.batch_id = :batch AND c.transaction_id IS NOT NULL
      )
    """
)


async def refresh_income_kinds(session: AsyncSession, batch_id: UUID) -> None:
    """Recompute `income_kind` for every live transaction this batch's rows reach."""
    await session.execute(_REFRESH, {"batch": batch_id})
