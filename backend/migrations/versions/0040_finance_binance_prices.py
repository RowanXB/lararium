"""Binance prices: the daily bars of the pairs that price an asset (`binance_klines`, one column
per position of a bar, market data shared by every connection) and the CoinGecko id each
asset was looked up under (design 5.10, plan Decision 13).

Revision ID: 0040
Revises: 0039
Create Date: 2026-11-23
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    CREATE TABLE finance.binance_klines (
      "symbol" text NOT NULL,
      "open_time" timestamptz NOT NULL,
      "open" numeric NOT NULL
        CHECK ("open" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "high" numeric NOT NULL
        CHECK ("high" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "low" numeric NOT NULL
        CHECK ("low" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "close" numeric NOT NULL
        CHECK ("close" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "volume" numeric NOT NULL
        CHECK ("volume" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "close_time" timestamptz NOT NULL,
      "quote_volume" numeric NOT NULL
        CHECK ("quote_volume" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "trades" bigint NOT NULL,
      "taker_buy_base_volume" numeric NOT NULL
        CHECK ("taker_buy_base_volume" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "taker_buy_quote_volume" numeric NOT NULL
        CHECK ("taker_buy_quote_volume" NOT IN ('NaN', 'Infinity', '-Infinity')),
      "unused" text,
      PRIMARY KEY ("symbol", "open_time")
    )
    """,
    # A null `coingecko_id` is a lookup that found none, kept so it is not asked again at once.
    """
    CREATE TABLE finance.binance_coingecko_ids (
      asset text PRIMARY KEY,
      coingecko_id text,
      fetched_at timestamptz NOT NULL DEFAULT now()
    )
    """,
]

DOWNGRADE = [
    "DROP TABLE finance.binance_coingecko_ids",
    "DROP TABLE finance.binance_klines",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
