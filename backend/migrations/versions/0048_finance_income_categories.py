"""Income kinds and the owner's transaction categories.

Income statistics group money received by what it is (interest, a dividend, share lending, a
reward in coins) and let the owner name a category of their own for any transaction
(income categories design §4.1).

finance.transactions.income_kind is what the source states about an income, never the
owner's word, so the source's own `category` stays untouched. finance.transaction_owner_categories
holds the owner's category by the transaction's source reference, as transfer_decisions does,
so it survives a rebuild of the transaction from the same source; the category is stored
normalised (lower case, at most 64 characters), and clearing it deletes the row. The
backend role gets its privileges on the table from the default privileges 0001 set for the
finance schema.

Revolut Savings (its PDF and CSV) and Tembo (its PDF) state interest by the row's type, which
their parsers now read as interest. Their import rows are marked the same way first, by the
config that parsed them (their batch's own, found by its format and layout, or a CSV's
columns and date format, as 0047 finds its entries), and the stored catalog profiles and
staged batches of the Revolut Savings CSV get its interest descriptions.

The backfill then gives `interest` to the transaction of every committed import row that
states interest, or that has a duplicate in a committed batch stating it that is surely the
row's own: most CSV profiles cannot state interest while the PDF layouts do, so with the CSV
committed first the PDF's rows land as duplicates. Connectors fill the column when their next
sync replays under a new normalizer version.

Downgrading drops the table and the column, and clears the replay markers of the four
connectors that set income_kind. The rows' interest marks and the interest descriptions stay:
the older code reads them the same way.

Revision ID: 0048
Revises: 0047
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# How a stored config ({c}) is recognised as the catalog entry, and the row types that pay
# interest under it.
_REVOLUT_SAVINGS_CSV = (
    "{c} ->> 'format' = 'csv'"
    " AND {c} -> 'columns' ->> 'type' = 'Description'"
    " AND {c} -> 'columns' ->> 'inflow' = 'Money in'"
    " AND {c} ->> 'date_format' = '%b %d, %Y'"
)
INTEREST_TYPES = [
    (
        "{c} ->> 'format' = 'pdf' AND {c} ->> 'layout' = 'revolut_savings_statement'",
        "('Gross Interest')",
    ),
    (_REVOLUT_SAVINGS_CSV, "('Gross Interest')"),
    (
        "{c} ->> 'format' = 'pdf' AND {c} ->> 'layout' = 'tembo_statement'",
        "('Interest', 'Interest Adjustment')",
    ),
]


def _mark_rows() -> str:
    """Sets `interest` on the statement rows, of any status, that pay interest by
    the config that parsed them: their batch's own."""
    matches = " OR ".join(
        f"({match.format(c='b.profile_config')} AND btrim(r.bank_type) IN {types})"
        for match, types in INTEREST_TYPES
    )
    return (
        "UPDATE finance.import_rows AS r SET interest = true"
        " FROM finance.import_batches AS b"
        f" WHERE b.id = r.batch_id AND r.kind = 'statement' AND ({matches})"
    )


MARK_ROWS = _mark_rows()

_DESCRIPTIONS = """'{"interest_descriptions": ["Gross Interest"]}'::jsonb"""
_NONE_YET = "coalesce({c} -> 'interest_descriptions', '[]'::jsonb) = '[]'::jsonb"
CONFIGS = [
    "UPDATE finance.import_profiles SET config = config || "
    + _DESCRIPTIONS
    + ", version = version + 1, updated_at = now()"
    f" WHERE {_NONE_YET.format(c='config')} AND {_REVOLUT_SAVINGS_CSV.format(c='config')}",
    "UPDATE finance.import_batches SET profile_config = profile_config || "
    + _DESCRIPTIONS
    + f" WHERE {_NONE_YET.format(c='profile_config')}"
    f" AND {_REVOLUT_SAVINGS_CSV.format(c='profile_config')} AND status = 'staged'",
]

# A duplicate speaks for its original only when it is surely that row, mirroring contra's
# surely_its_original: matched by its key, or the one committed row of its account on that
# day with that instrument and amount. Otherwise it was matched by day and amount alone
# (Decision 16), in file order, and among alike rows it may be another's.
BACKFILL = """
UPDATE finance.transactions t
SET income_kind = 'interest'
WHERE t.income_kind IS NULL
  AND EXISTS (
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
  )
"""

# What the migration changes in the data, after the column exists.
DATA = [MARK_ROWS, *CONFIGS, BACKFILL]

UPGRADE = [
    """
    ALTER TABLE finance.transactions
      ADD COLUMN income_kind text
        CHECK (income_kind IN ('interest', 'dividend', 'share_lending', 'reward'))
    """,
    """
    CREATE TABLE finance.transaction_owner_categories (
      source_kind text NOT NULL,
      source_ref text NOT NULL,
      category text NOT NULL
        CHECK (category <> '' AND length(category) <= 64 AND category = lower(category)),
      decided_at timestamptz NOT NULL DEFAULT now(),
      decided_by text NOT NULL,
      PRIMARY KEY (source_kind, source_ref)
    )
    """,
    *DATA,
]

# The older code reads `interest` and interest descriptions as this one does, so the rows'
# marks and the configs stay: they are facts about the files, and a config may have said so
# before the upgrade. The replay markers of the connectors whose normalizers set income_kind
# go, so the first sync after upgrading again replays their history (as 0047 does): without
# that, a downgrade and upgrade with no older sync between would leave their income unmarked.
DOWNGRADE_DATA = [
    "UPDATE core.connections SET config = config - 'normalizer_version'"
    " WHERE connector IN ('trading212', 'etoro', 'ibkr', 'binance')",
]
DOWNGRADE = [
    *DOWNGRADE_DATA,
    "DROP TABLE finance.transaction_owner_categories",
    "ALTER TABLE finance.transactions DROP COLUMN income_kind",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
