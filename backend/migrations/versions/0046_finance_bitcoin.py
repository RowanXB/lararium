"""Finance Bitcoin wallet: the connector's tables (addresses, per-sync address reports, the
transactions with their inputs and outputs as the indexer gave them), the chain columns that
name an on-chain transaction on `finance.transactions`, and the `chain_native` instrument
scheme (Bitcoin wallet design).

`chain` / `tx_hash` / `chain_address` let the transfer engine pair an on-chain movement with an
exchange's withdrawal or deposit without reading a connector's tables; Binance fills them too.

Revision ID: 0046
Revises: 0045
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The list as 0045 leaves it (0041 added `binance_asset`); the downgrade restores exactly this.
_SCHEMES = """'iso4217', 'isin', 'figi', 'ticker_mic', 'ibkr_conid', 'etoro_id',
          'chain_contract', 'coingecko', 'trading212_ticker', 'binance_asset'"""


def _scheme_check(extra: str) -> str:
    return f"""
    ALTER TABLE finance.instrument_identifiers
      DROP CONSTRAINT instrument_identifiers_scheme_check,
      ADD CONSTRAINT instrument_identifiers_scheme_check
        CHECK (scheme IN ({_SCHEMES}{extra}))
    """


UPGRADE = [
    _scheme_check(", 'chain_native'"),
    # A chain's native coin is named by the chain: 'bitcoin'.
    """
    ALTER TABLE finance.instrument_identifiers
      ADD CONSTRAINT instrument_identifiers_chain_native_check
        CHECK (scheme <> 'chain_native' OR value ~ '^[a-z0-9-]{1,64}$')
    """,
    """
    ALTER TABLE finance.transactions
      ADD COLUMN chain text,
      ADD COLUMN tx_hash text,
      ADD COLUMN chain_address text,
      ADD CONSTRAINT transactions_chain_tx_hash_check CHECK ((chain IS NULL) = (tx_hash IS NULL)),
      ADD CONSTRAINT transactions_tx_hash_hex_check
        CHECK (tx_hash IS NULL OR tx_hash ~ '^[0-9a-f]{64}$'),
      ADD CONSTRAINT transactions_chain_address_check
        CHECK (chain_address IS NULL OR chain IS NOT NULL)
    """,
    """
    CREATE INDEX transactions_chain_hash_idx ON finance.transactions (chain, tx_hash)
      WHERE tx_hash IS NOT NULL
    """,
    """
    CREATE TABLE finance.bitcoin_addresses (
      connection_id uuid NOT NULL REFERENCES core.connections (id) ON DELETE CASCADE,
      source text NOT NULL,
      branch smallint NOT NULL,
      address_index integer NOT NULL,
      address text NOT NULL,
      script_type text NOT NULL,
      first_used_at timestamptz,
      last_used_at timestamptz,
      PRIMARY KEY (connection_id, source, branch, address_index),
      UNIQUE (connection_id, address)
    )
    """,
    """
    CREATE TABLE finance.bitcoin_address_reports (
      connection_id uuid NOT NULL REFERENCES core.connections (id) ON DELETE CASCADE,
      sync_id uuid NOT NULL,
      address text NOT NULL,
      chain_tx_count bigint NOT NULL,
      chain_funded_txo_count bigint NOT NULL,
      chain_funded_txo_sum bigint NOT NULL,
      chain_spent_txo_count bigint NOT NULL,
      chain_spent_txo_sum bigint NOT NULL,
      mempool_tx_count bigint NOT NULL,
      mempool_funded_txo_count bigint NOT NULL,
      mempool_funded_txo_sum bigint NOT NULL,
      mempool_spent_txo_count bigint NOT NULL,
      mempool_spent_txo_sum bigint NOT NULL,
      PRIMARY KEY (connection_id, sync_id, address)
    )
    """,
    """
    CREATE TABLE finance.bitcoin_transactions (
      connection_id uuid NOT NULL REFERENCES core.connections (id) ON DELETE CASCADE,
      txid text NOT NULL,
      version bigint,
      locktime bigint,
      size bigint,
      weight bigint,
      sigops bigint,
      fee bigint,
      confirmed boolean,
      block_height bigint,
      block_hash text,
      block_time timestamptz,
      first_seen_at timestamptz,
      last_seen_at timestamptz,
      left_chain_at timestamptz,
      fee_transaction_id uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      net_transaction_id uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      PRIMARY KEY (connection_id, txid)
    )
    """,
    """
    CREATE TABLE finance.bitcoin_inputs (
      connection_id uuid NOT NULL REFERENCES core.connections (id) ON DELETE CASCADE,
      txid text NOT NULL,
      input_index integer NOT NULL,
      prev_txid text,
      prev_vout bigint,
      is_coinbase boolean,
      scriptsig text,
      scriptsig_asm text,
      inner_redeemscript_asm text,
      inner_witnessscript_asm text,
      sequence bigint,
      witness text[],
      prevout_scriptpubkey text,
      prevout_scriptpubkey_asm text,
      prevout_scriptpubkey_type text,
      prevout_scriptpubkey_address text,
      prevout_value bigint,
      own boolean NOT NULL,
      PRIMARY KEY (connection_id, txid, input_index)
    )
    """,
    """
    CREATE TABLE finance.bitcoin_outputs (
      connection_id uuid NOT NULL REFERENCES core.connections (id) ON DELETE CASCADE,
      txid text NOT NULL,
      vout integer NOT NULL,
      scriptpubkey text,
      scriptpubkey_asm text,
      scriptpubkey_type text,
      scriptpubkey_address text,
      value bigint NOT NULL,
      own boolean NOT NULL,
      transaction_id uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      PRIMARY KEY (connection_id, txid, vout)
    )
    """,
]

DOWNGRADE = [
    "DROP TABLE finance.bitcoin_outputs",
    "DROP TABLE finance.bitcoin_inputs",
    "DROP TABLE finance.bitcoin_transactions",
    "DROP TABLE finance.bitcoin_address_reports",
    "DROP TABLE finance.bitcoin_addresses",
    "DROP INDEX finance.transactions_chain_hash_idx",
    """
    ALTER TABLE finance.transactions
      DROP CONSTRAINT transactions_chain_address_check,
      DROP CONSTRAINT transactions_tx_hash_hex_check,
      DROP CONSTRAINT transactions_chain_tx_hash_check,
      DROP COLUMN chain_address,
      DROP COLUMN tx_hash,
      DROP COLUMN chain
    """,
    "DELETE FROM finance.instrument_identifiers WHERE scheme = 'chain_native'",
    """
    ALTER TABLE finance.instrument_identifiers
      DROP CONSTRAINT instrument_identifiers_chain_native_check
    """,
    _scheme_check(""),
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
