"""Finance valuation v1: FX rate lookup, balances, valuation and the daily net-worth series.

These functions are the shared read contract for the UI, BI and MCP. Never change a v1
function's behaviour in place; add a v2 alongside it instead.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-11
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    # How many units of p_quote one unit of p_base buys on p_day: same currency, then the
    # direct rate, then the reciprocal of the inverse rate, then one hop via EUR; else NULL.
    """
    CREATE FUNCTION finance.v1_fx_rate(p_base uuid, p_quote uuid, p_day date) RETURNS numeric
    LANGUAGE sql STABLE AS $$
      WITH eur AS (
        SELECT ii.instrument_id AS id
        FROM finance.instrument_identifiers ii
        WHERE ii.scheme = 'iso4217' AND ii.value = 'EUR'
      ),
      pairs (step, base, quote) AS (
        SELECT 'direct', p_base, p_quote
        UNION ALL SELECT 'to_eur', p_base, eur.id FROM eur
        UNION ALL SELECT 'from_eur', eur.id, p_quote FROM eur
      ),
      legs AS (
        SELECT pairs.step, coalesce(
          (SELECT r.rate FROM finance.fx_rates r
           WHERE r.base_instrument_id = pairs.base AND r.quote_instrument_id = pairs.quote
             AND r.as_of_date <= p_day
           ORDER BY r.as_of_date DESC, (r.source = 'manual') DESC, r.recorded_at DESC
           LIMIT 1),
          (SELECT 1 / r.rate FROM finance.fx_rates r
           WHERE r.base_instrument_id = pairs.quote AND r.quote_instrument_id = pairs.base
             AND r.as_of_date <= p_day
           ORDER BY r.as_of_date DESC, (r.source = 'manual') DESC, r.recorded_at DESC
           LIMIT 1)
        ) AS rate
        FROM pairs
      )
      SELECT CASE
        WHEN p_base = p_quote THEN 1::numeric
        ELSE coalesce(
          (SELECT legs.rate FROM legs WHERE legs.step = 'direct'),
          (SELECT a.rate * b.rate FROM legs a, legs b
           WHERE a.step = 'to_eur' AND b.step = 'from_eur')
        )
      END
    $$
    """,
    # Each (account, instrument) balance on p_day, per the account's balance mode.
    """
    CREATE FUNCTION finance.v1_balances_asof(p_day date)
    RETURNS TABLE (
      account_id uuid,
      instrument_id uuid,
      quantity numeric,
      snapshot_quantity numeric,
      snapshot_date date
    )
    LANGUAGE sql STABLE AS $$
      WITH live_accounts AS (
        SELECT a.id, a.balance_mode
        FROM finance.accounts a
        WHERE NOT (a.status = 'closed' AND a.closed_on <= p_day)
      ),
      snaps AS (
        SELECT DISTINCT ON (s.account_id, s.instrument_id)
          s.account_id, s.instrument_id, s.quantity, s.as_of, s.as_of_date
        FROM finance.balance_snapshots s
        JOIN live_accounts a ON a.id = s.account_id
        WHERE a.balance_mode IN ('snapshots', 'hybrid') AND s.as_of_date <= p_day
        ORDER BY s.account_id, s.instrument_id,
          s.as_of_date DESC, s.as_of DESC, s.recorded_at DESC
      ),
      posted AS (
        SELECT p.account_id, p.instrument_id, p.quantity, t.occurred_at
        FROM finance.postings p
        JOIN finance.transactions t ON t.id = p.transaction_id
        JOIN live_accounts a ON a.id = p.account_id
        WHERE a.balance_mode IN ('transactions', 'hybrid')
          AND t.status <> 'void' AND t.booking_date <= p_day
      ),
      keys AS (
        SELECT snaps.account_id, snaps.instrument_id FROM snaps
        UNION
        SELECT posted.account_id, posted.instrument_id FROM posted
      )
      SELECT
        k.account_id,
        k.instrument_id,
        CASE
          WHEN s.account_id IS NULL THEN sum(p.quantity)
          ELSE s.quantity
            + coalesce(sum(p.quantity) FILTER (WHERE p.occurred_at > s.as_of), 0)
        END,
        s.quantity,
        s.as_of_date
      FROM keys k
      LEFT JOIN snaps s
        ON s.account_id = k.account_id AND s.instrument_id = k.instrument_id
      LEFT JOIN posted p
        ON p.account_id = k.account_id AND p.instrument_id = k.instrument_id
      GROUP BY k.account_id, k.instrument_id, s.account_id, s.quantity, s.as_of, s.as_of_date
    $$
    """,
    # Each asset and liability holding on p_day, valued in the reporting currency.
    """
    CREATE FUNCTION finance.v1_valuation_asof(p_day date)
    RETURNS TABLE (
      account_id uuid,
      account_name text,
      instrument_id uuid,
      symbol text,
      quantity numeric,
      unit_price numeric,
      price_currency_id uuid,
      fx_rate numeric,
      value numeric,
      status text
    )
    LANGUAGE sql STABLE AS $$
      WITH reporting AS (
        SELECT i.id
        FROM core.settings st
        JOIN finance.instrument_identifiers ii
          ON ii.scheme = 'iso4217' AND ii.value = st.value #>> '{}'
        JOIN finance.instruments i ON i.id = ii.instrument_id AND i.kind = 'fiat'
        WHERE st.key = 'finance.reporting_currency'
      ),
      priced AS (
        SELECT b.account_id, a.name AS account_name, b.instrument_id, i.symbol, b.quantity,
          pr.unit_price, pr.price_currency_id,
          finance.v1_fx_rate(pr.price_currency_id, (SELECT reporting.id FROM reporting), p_day)
            AS fx_rate
        FROM finance.v1_balances_asof(p_day) b
        JOIN finance.accounts a ON a.id = b.account_id
        JOIN finance.instruments i ON i.id = b.instrument_id
        LEFT JOIN LATERAL (
          SELECT 1::numeric AS unit_price, i.id AS price_currency_id
          WHERE i.kind = 'fiat'
          UNION ALL
          (SELECT p.price, p.quote_instrument_id
           FROM finance.prices p
           WHERE i.kind <> 'fiat' AND p.instrument_id = i.id AND p.as_of_date <= p_day
           ORDER BY p.as_of_date DESC, (p.source = 'manual') DESC, p.recorded_at DESC
           LIMIT 1)
        ) pr ON true
        WHERE a.ledger_type IN ('asset', 'liability') AND NOT i.hidden AND b.quantity <> 0
      )
      SELECT priced.account_id, priced.account_name, priced.instrument_id, priced.symbol,
        priced.quantity, priced.unit_price, priced.price_currency_id, priced.fx_rate,
        CASE WHEN priced.unit_price IS NOT NULL AND priced.fx_rate IS NOT NULL
          THEN priced.quantity * priced.unit_price * priced.fx_rate
        END,
        CASE
          WHEN priced.unit_price IS NULL THEN 'missing_price'
          WHEN priced.fx_rate IS NULL THEN 'missing_fx'
          ELSE 'ok'
        END
      FROM priced
    $$
    """,
    # One row per day: the sum of that day's valued holdings and the count of unvalued ones.
    """
    CREATE FUNCTION finance.v1_net_worth_series(p_start date, p_end date)
    RETURNS TABLE (day date, value numeric, missing_count int)
    LANGUAGE sql STABLE AS $$
      SELECT d.day,
        coalesce(sum(v.value) FILTER (WHERE v.status = 'ok'), 0),
        (count(*) FILTER (WHERE v.status <> 'ok'))::int
      FROM (SELECT p_start + n AS day FROM generate_series(0, p_end - p_start) AS n) d
      LEFT JOIN LATERAL finance.v1_valuation_asof(d.day) v ON true
      GROUP BY d.day
      ORDER BY d.day
    $$
    """,
]

DOWNGRADE = [
    "DROP FUNCTION finance.v1_net_worth_series(date, date)",
    "DROP FUNCTION finance.v1_valuation_asof(date)",
    "DROP FUNCTION finance.v1_balances_asof(date)",
    "DROP FUNCTION finance.v1_fx_rate(uuid, uuid, date)",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
