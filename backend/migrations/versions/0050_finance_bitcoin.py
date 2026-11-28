"""Finance Bitcoin wallet: the connector's tables (addresses, per-sync address reports, the
transactions with their inputs and outputs as the indexer gave them), the chain columns that
name an on-chain transaction on `finance.transactions`, and the `chain_native` instrument
scheme (Bitcoin wallet design).

`chain` / `tx_hash` / `chain_address` let the transfer engine pair an on-chain movement with an
exchange's withdrawal or deposit without reading a connector's tables; Binance fills them too.

Revision ID: 0050
Revises: 0049
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The list as 0049 leaves it (0041 added `binance_asset`); the downgrade restores exactly this.
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
      -- What the address's last complete history held (chain + mempool transactions, and of
      -- them the confirmed) and the chain tip then: a sync asks the history again only when
      -- either count differs (a transaction confirming leaves the first as it was), and stops
      -- paging at a transaction that was 6 blocks deep at that tip. NULL until one completes.
      history_tx_count bigint,
      history_chain_count bigint,
      history_height bigint,
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
      -- The chain tip when the server last reported the transaction, for the recheck of those
      -- under 6 confirmations then; NULL once the server no longer knows it.
      seen_at_height bigint,
      -- Set by every fetch write that inserts the row or changes its status or which inputs
      -- and outputs are the wallet's; cleared by the normalizer in the sync's own
      -- transaction, so a sync that fails after the fetch leaves it set.
      pending boolean NOT NULL DEFAULT true,
      -- Whether the row was confirmed when last normalized: its movements are posted.
      normalized_confirmed boolean NOT NULL DEFAULT false,
      left_chain_at timestamptz,
      fee_transaction_id uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      net_transaction_id uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      PRIMARY KEY (connection_id, txid)
    )
    """,
    """
    CREATE INDEX bitcoin_transactions_pending_idx ON finance.bitcoin_transactions (connection_id)
      WHERE pending
    """,
    """
    CREATE INDEX bitcoin_transactions_fee_transaction_idx
      ON finance.bitcoin_transactions (fee_transaction_id)
      WHERE fee_transaction_id IS NOT NULL
    """,
    """
    CREATE INDEX bitcoin_transactions_net_transaction_idx
      ON finance.bitcoin_transactions (net_transaction_id)
      WHERE net_transaction_id IS NOT NULL
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
    """
    CREATE INDEX bitcoin_outputs_transaction_idx
      ON finance.bitcoin_outputs (transaction_id)
      WHERE transaction_id IS NOT NULL
    """,
    # Bech32 addresses are case-insensitive, and chain_address now stores them lower-cased
    # (identifiers.normalize_identifier): a stored upper-case one is lower-cased so its
    # holder still matches. One whose lower-cased form another row already has (or would
    # get) is left as it is: two registrations of one address are the owner's to merge.
    """
    UPDATE finance.account_identifiers AS i SET value = lower(i.value)
    WHERE i.kind = 'chain_address' AND i.value ~* '^bc1' AND i.value <> lower(i.value)
      AND (SELECT count(*) FROM finance.account_identifiers AS o
           WHERE o.kind = 'chain_address' AND lower(o.value) = lower(i.value)) = 1
    """,
]

DOWNGRADE = [
    "DROP INDEX finance.bitcoin_outputs_transaction_idx",
    "DROP INDEX finance.bitcoin_transactions_net_transaction_idx",
    "DROP INDEX finance.bitcoin_transactions_fee_transaction_idx",
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
