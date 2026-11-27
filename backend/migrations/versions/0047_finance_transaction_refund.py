"""Finance transactions: refund, the source's word that money came back for an earlier card
payment, so the cash-flow report counts it as negative spending, not income.

File imports mark a refund by the profile's card payment types. Stored catalog profiles and
staged batches get their entry's types, found by their config as 0028 finds LemFi's (the owner
may have renamed a profile; a profile the owner defined matches no entry and is left alone).
The positive card rows already committed are then marked, by the types of the config that
parsed them: their batch's own, as it was at staging, which stays unchanged.

Revision ID: 0047
Revises: 0046
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADD_COLUMN = "ALTER TABLE finance.transactions ADD COLUMN refund boolean NOT NULL DEFAULT false"
# Monzo's own card fields, stored typed: the lifecycle id a card's authorisation and its
# settlement share, and whether the source calls the transaction a reversal.
ADD_CARD_COLUMNS = (
    "ALTER TABLE finance.transactions"
    " ADD COLUMN card_lifecycle_id text, ADD COLUMN card_reversal boolean"
)

# The catalog entries that have card payment types: how a stored config is recognised ({c} is
# the config; its format, plus a PDF's layout, or the columns and date format that identify a
# CSV), and the types the entry gives.
_HSBC = '[")))", "VIS"]'
_LLOYDS = '["DEB"]'
ENTRIES = [
    ("{c} ->> 'format' = 'pdf' AND {c} ->> 'layout' = 'hsbc_uk_statement'", _HSBC),
    (
        "{c} ->> 'format' = 'csv'"
        " AND {c} -> 'columns' ->> 'type' = 'Type'"
        " AND {c} -> 'columns' ->> 'amount' = 'Debit/Credit'"
        " AND {c} -> 'columns' ->> 'balance' = 'Balance'"
        " AND {c} ->> 'date_format' = '%Y-%m-%d'",
        _HSBC,
    ),
    ("{c} ->> 'format' = 'pdf' AND {c} ->> 'layout' = 'lloyds_statement'", _LLOYDS),
    (
        "{c} ->> 'format' = 'csv'"
        " AND {c} -> 'columns' ->> 'type' = 'Transaction Type'"
        " AND {c} -> 'columns' ->> 'inflow' = 'Credit Amount'"
        " AND {c} -> 'columns' ->> 'outflow' = 'Debit Amount'"
        " AND {c} ->> 'date_format' = '%d/%m/%Y'",
        _LLOYDS,
    ),
    (
        "{c} ->> 'format' = 'csv'"
        " AND {c} -> 'columns' ->> 'type' = 'Type'"
        " AND {c} -> 'columns' ->> 'started_at' = 'Started Date'"
        " AND {c} ->> 'date_format' = '%Y-%m-%d %H:%M:%S'",
        '["Card Payment", "Card Refund"]',
    ),
    (
        "{c} ->> 'format' = 'pdf' AND {c} ->> 'layout' = 'boc_uk_statement'",
        '["Pre-Authorization Completed"]',
    ),
    ("{c} ->> 'format' = 'ofx'", '["POS"]'),
]


def _add_types(table: str, column: str, also_set: str, also_where: str) -> list[str]:
    return [
        f"UPDATE finance.{table} SET {column} = {column}"
        f" || '{{\"card_payment_types\": {types}}}'::jsonb{also_set}"
        f" WHERE NOT ({column} ? 'card_payment_types') AND {match.format(c=column)}{also_where}"
        for match, types in ENTRIES
    ]


PROFILES = _add_types(
    "import_profiles", "config", ", version = version + 1, updated_at = now()", ""
)
STAGED_BATCHES = _add_types("import_batches", "profile_config", "", " AND status = 'staged'")

# A committed batch's card payment types, read from the config that parsed it (its own
# snapshot, not its profile, which the owner may have edited since): the types it states, or
# else those of the catalog entry the snapshot is, or none.
_BATCH_TYPES = (
    "coalesce(b.profile_config -> 'card_payment_types', CASE "
    + " ".join(
        f"WHEN {match.format(c='b.profile_config')} THEN '{types}'::jsonb"
        for match, types in ENTRIES
    )
    + " ELSE '[]'::jsonb END)"
)

# A committed statement row paid by card (its card number, or one of its batch's card payment
# types) that moved money in, and no interest.
REFUNDS = f"""
UPDATE finance.transactions AS t SET refund = true
FROM finance.import_rows AS r
JOIN finance.import_batches AS b ON b.id = r.batch_id
WHERE r.transaction_id = t.id
  AND r.kind = 'statement' AND r.status = 'committed'
  AND r.quantity > 0 AND NOT r.interest
  AND t.status <> 'void'
  AND (
    r.card_number IS NOT NULL
    OR btrim(r.bank_type) IN (SELECT jsonb_array_elements_text({_BATCH_TYPES}))
  )
"""

# What the migration changes in the data, after the columns exist.
DATA = [*PROFILES, *STAGED_BATCHES, REFUNDS]
UPGRADE = [ADD_COLUMN, ADD_CARD_COLUMNS, *DATA]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    op.execute(
        "ALTER TABLE finance.transactions"
        " DROP COLUMN card_reversal, DROP COLUMN card_lifecycle_id, DROP COLUMN refund"
    )
