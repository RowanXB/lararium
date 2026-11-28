# M8 Bitcoin wallets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A read-only Bitcoin wallet connector: the owner gives an Electrum master public key, an output descriptor or an address list; Lararium derives the addresses, reads an Esplora server, posts every movement on one `transactions`-mode wallet account, checks it against the reported address sums, prices BTC without needing a Binance connection, and pairs wallet movements with Binance on-chain deposits and withdrawals by transaction hash.

**Architecture:** A new finance sub-package `bitcoin` following the Trading 212 connector's shape (fetch → raw records → typed tables → normalize → reconcile → flags). Keys become embit descriptors at connect time (Electrum's old-seed and 2FA keys get their own derivation). `finance.transactions` gains `chain`, `tx_hash`, `chain_address`; the transfer engine gains a `HASH` tier; Binance fills the new columns on a normalizer bump. BTC is priced by a parametrised version of M7's bar pricer run under the wallet's connection.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy async, Alembic (raw SQL migrations), PostgreSQL 18, procrastinate, httpx, embit (new, MIT), pytest + Hypothesis + testcontainers, React + hey-api client.

**Spec:** `docs/superpowers/specs/2026-11-27-bitcoin-wallet-design.md` (approved 2026-11-27). One amendment, made in Task 13: §5.7's sentence "M7's per-connection walk then uses the same state" is replaced by "each connection keeps its own record of the days it asked for, as M7 does; the bars table is shared, so a day two connections both fetch is stored once". The wallet pricer reuses M7's per-connection state machinery under the wallet's own connection instead of a new shared table; the cost is at most one repeated bar request per symbol, and M7's merged pricer keeps its behaviour.

## Global Constraints

- Migrations are raw SQL in `UPGRADE`/`DOWNGRADE` lists, numbered after `main`'s newest when the task starts (today `0045`, so this plan's are `0046` and `0047`; renumber if `main` moved).
- Every field Esplora returns gets a typed column (`sigops` included); amounts in satoshis are `bigint`; BTC quantities in the ledger are `Decimal` satoshis / 10⁸, 8 decimals. No float anywhere on the amount path (`tests/test_no_floats.py`).
- No real figures, addresses, hashes or keys in the repository: tests use published test vectors (BIP 32/44/49/84/86, Electrum's own tests) and invented values.
- The master public key, descriptors and addresses never appear in a log line, a notification, a raw record's `request`, an error message or an HTTP error body. Request bodies of the create and preview routes are never logged.
- Modules depend on `core`, not on each other (`lint-imports`); `finance.bitcoin` may import `finance.*` shared parts and `finance.binance.prices`/`finance.binance.client` (one finance package).
- Docs and commits in English; commit titles `<type>: <summary>`; commit by explicit path, never `git add -A`.
- Tests run on the remote Docker host (`docker --context docker-test` via the forwarded socket); a PR's CI runs the full suite: locally run the task's focused tests plus `ruff`, `pyright`, `lint-imports`.
- Constants the spec pins: default server `https://mempool.space/api` (`LARARIUM_ESPLORA_URL`); 1 request per second; a 429 without `Retry-After` waits 60 s; sync at most every 6 hours unless requested; recheck transactions below 6 confirmations; post at 1 confirmation; gap limits 20/10 (Electrum keys), 20/20 (descriptors); chain value `bitcoin`; Binance networks `BTC` and `SEGWITBTC` map to `bitcoin`.

## Review Focus

1. **A pasted seed phrase or private key** (the most likely owner mistake): refused with the fixed message, never stored or echoed, and absent from logs, raw records and the 422 body. Pinned in Task 1 and Task 11.
2. **The first receiving address preview not matching Electrum** because the xpub's wallet type is ambiguous: an `xpub` single-key wallet must derive p2pkh, never p2sh. Pinned in Task 2.
3. **The same chain transaction seen twice across syncs** (or a sync failing halfway): no double posting, the scan resumes. Pinned in Task 5 and Task 6.
4. **A Binance withdrawal batched with other people's outputs** pairing with exactly the wallet's output, not another output of the same transaction. Pinned in Task 10.
5. **mempool.space rate-limiting a long scan**: waits, never more than one request a second, and a refused sync keeps what it stored. Pinned in Task 4 and Task 5.

---

### Task 1: Wallet input parsing and refusal of private material

**Files:**
- Modify: `backend/pyproject.toml` (add `embit>=0.8` to `dependencies`), `backend/uv.lock` (`uv lock`)
- Create: `backend/src/lararium/modules/finance/bitcoin/__init__.py` (empty), `backend/src/lararium/modules/finance/bitcoin/keys.py`
- Test: `backend/tests/finance/bitcoin/__init__.py`, `backend/tests/finance/bitcoin/test_keys.py`

**Interfaces:**
- Produces (in `keys.py`):
  - `WalletForm = Literal["electrum", "descriptor", "addresses"]`
  - `ElectrumType = Literal["single", "multisig", "2fa", "old"]`
  - `@dataclass(frozen=True) class WalletInput: form: WalletForm; electrum_type: ElectrumType | None; keys: tuple[str, ...]; threshold: int | None; descriptors: tuple[str, ...]; addresses: tuple[str, ...]`
  - `class PrivateMaterialError(FinanceValidationError)`; `class UnsupportedNetworkError(FinanceValidationError)`; `class WalletInputError(FinanceValidationError)`
  - `def refuse_private(text: str) -> None` — raises `PrivateMaterialError` or `UnsupportedNetworkError`; the message never contains the input.
  - `def parse_input(raw: WalletInput) -> WalletInput` — validated and normalised (whitespace stripped, addresses through `identifiers.normalize_identifier("chain_address", …)` and a Bitcoin-mainnet address check).

- [ ] **Step 1: Write the failing tests** in `test_keys.py`:
  - `test_seed_phrase_is_refused`: `refuse_private("abandon " * 11 + "about")` raises `PrivateMaterialError` whose `str()` is exactly `"This looks like a seed phrase. Never enter it anywhere: Lararium needs the master public key from Wallet → Information."` and contains no input word.
  - `test_extended_private_keys_are_refused` (parametrised over `xprv`, `yprv`, `zprv`, `Yprv`, `Zprv`, `tprv` prefixes on a 111-character base58 body): raises `PrivateMaterialError`, message `"This looks like a private key. Never enter it anywhere: Lararium needs the master public key from Wallet → Information."`.
  - `test_wif_private_key_is_refused`: a 52-character base58 string starting `K` and one of 51 starting `5` raise `PrivateMaterialError` (same message).
  - `test_test_network_keys_are_refused` (`tpub`, `upub`, `vpub`, `Upub`, `Vpub`): raises `UnsupportedNetworkError`, message `"Only Bitcoin mainnet wallets are supported."`.
  - `test_public_forms_pass`: the BIP84 vector `zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868RvUUkgDKf31mGDtKsAYz2oz2AGutZYs` and a 128-hex old key pass `refuse_private`.
  - `test_multisig_needs_threshold_within_keys`: `parse_input` with `electrum_type="multisig"`, 3 keys and `threshold=4` raises `WalletInputError`; keys of mixed forms (`xpub` and `Zpub`) raise `WalletInputError`.
  - `test_2fa_takes_two_keys`: anything but two keys raises `WalletInputError`.
  - `test_addresses_are_checked`: a testnet `tb1…` address raises `UnsupportedNetworkError`; a malformed one raises `WalletInputError`.

- [ ] **Step 2: Run** `cd backend && uv run pytest tests/finance/bitcoin/test_keys.py -q` — Expected: FAIL (module missing).

- [ ] **Step 3: Implement `keys.py`.** Detection: twelve or more whitespace-separated lowercase words → seed; base58 strings with a private SLIP-0132 prefix → private key; WIF (51 chars starting `5`, 52 starting `K`/`L`) → private key; 64 hex digits → private key (no public form is 64 hex). Address validation decodes with embit (`embit.script.Script`/`address_to_scriptpubkey`) and checks mainnet HRP/version.

- [ ] **Step 4: Run** the same command — Expected: PASS. Then `uv run ruff check src tests && uv run pyright` — clean.

- [ ] **Step 5: Commit**
```bash
git add backend/pyproject.toml backend/uv.lock backend/src/lararium/modules/finance/bitcoin/__init__.py backend/src/lararium/modules/finance/bitcoin/keys.py backend/tests/finance/bitcoin/__init__.py backend/tests/finance/bitcoin/test_keys.py
git commit -m "feat: parse Bitcoin wallet inputs and refuse private material"
```

### Task 2: Address derivation for every wallet form

**Files:**
- Create: `backend/src/lararium/modules/finance/bitcoin/derive.py`
- Test: `backend/tests/finance/bitcoin/test_derive.py`

**Interfaces:**
- Consumes: `WalletInput`, `parse_input` (Task 1).
- Produces:
  - `Branch = Literal[0, 1]` (0 receiving, 1 change)
  - `@dataclass(frozen=True) class Source: name: str; gaps: dict[Branch, int]` — `name` is `"d0"`, `"d1"`, … (descriptor order) or `"list"`.
  - `class Wallet(Protocol)`: `sources: tuple[Source, ...]`; `def address(self, source: str, branch: Branch, index: int) -> tuple[str, str]` (address, script type: `p2pkh`, `p2sh-p2wpkh`, `p2wpkh`, `p2tr`, `p2sh`, `p2wsh`, `p2sh-p2wsh`); `def listed(self) -> tuple[str, ...]` (the address list, empty otherwise); `descriptors: tuple[str, ...]` (canonical, keys as `xpub`, with checksum; empty for old/2FA/list); `fingerprint: str` (SHA-256 hex of the sorted canonical descriptors, or of the sorted old/2FA keys, or of the sorted addresses).
  - `def build_wallet(spec: WalletInput) -> Wallet`
  - `def first_receiving(spec: WalletInput) -> str`

- [ ] **Step 1: Write the failing tests**, each asserting `wallet.address("d0", 0, 0)[0]` (and change where given) against published vectors:
  - `test_electrum_zpub_is_p2wpkh`: BIP84 zpub above → receiving 0 `bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu`, change 0 `bc1q8c6fshw2dlwun7ekn9qwf37cu2rn755upcp6el`, gaps `{0: 20, 1: 10}`.
  - `test_electrum_ypub_is_p2sh_p2wpkh`: `ypub6Ww3ibxVfGzLrAH1PNcjyAWenMTbbAosGNB6VvmSEgytSER9azLDWCxoJwW7Ke7icmizBMXrzBx9979FfaHxHcrArf3zbeJJJUZPf663zsP` → `37VucYSaXLCAsxYyAPfbSi9eh4iEcbShgf`.
  - `test_electrum_single_xpub_is_p2pkh`: `xpub6BosfCnifzxcFwrSzQiqu2DBVTshkCXacvNsWGYJVVhhawA7d4R5WSWGFNbi8Aw6ZRc1brxMyWMzG3DSSSSoekkudhUd9yLb6qx39T9nMdj` with `electrum_type="single"` → `1LqBGSKuX5yYUonjxT5qGfpUsXKYYWeabA` (Review Focus 2).
  - `test_taproot_descriptor`: `tr(xpub6BgBgsespWvERF3LHQu6CnqdvfEvtMcQjYrcRzx53QJjSxarj2afYWcLteoGVky7D3UKDP9QyrLprQ3VCECoY49yfdDEHGCtMMj92pReUsQ/<0;1>/*)` → `bc1p5cyxnuxmeuwuvkwfem96lqzszd02n6xdcjrs20cac6yqjjwudpxqkedrcr`, gaps `{0: 20, 1: 20}` (embit 0.8.0 derives it; checked 2026-11-27).
  - `test_split_descriptors_equal_multipath`: a `wpkh(<zpub>/0/*)` plus `wpkh(<zpub>/1/*)` pair derives the same addresses and the same `fingerprint` as `wpkh(<zpub>/<0;1>/*)`.
  - `test_bad_checksum_is_refused`: a descriptor with a wrong `#checksum` raises `WalletInputError`.
  - `test_multisig_sorts_keys`: a 2-of-3 Electrum `Zpub` multisig gives the same addresses whatever order the keys are entered, and equals `wsh(sortedmulti(2,…/<0;1>/*))`; take the three keys and expected first address from Electrum's `electrum/tests/test_wallet_vertical.py` (the p2wsh multisig case), cited in the test's docstring.
  - `test_electrum_old_seed`: the old-seed master public key and first receiving address from `test_wallet_vertical.py` (`test_electrum_seed_old`).
  - `test_electrum_2fa`: both 2FA variants (`2fa` p2sh, `2fa_segwit` p2wsh) from `test_wallet_vertical.py`: the owner's two keys give Electrum's first receiving address.
  - `test_fingerprint_ignores_key_version`: the zpub and the same key re-encoded as `xpub` inside `wpkh(...)` give one fingerprint.
  - `test_first_receiving_matches_address`: `first_receiving(spec) == build_wallet(spec).address(sources[0].name, 0, 0)[0]`.

- [ ] **Step 2: Run** `uv run pytest tests/finance/bitcoin/test_derive.py -q` — Expected: FAIL.

- [ ] **Step 3: Implement `derive.py`.** Electrum single keys map `xpub`→`pkh`, `ypub`→`sh(wpkh)`, `zpub`→`wpkh`; multisig `xpub`→`sh(sortedmulti)`, `Ypub`→`sh(wsh(sortedmulti))`, `Zpub`→`wsh(sortedmulti)`; all with `/<0;1>/*`. Old seed: address *i* on branch *c* is `P + sha256d(f"{i}:{c}:".encode() + mpk_bytes)·G` uncompressed p2pkh, via `embit.ec`/`secp256k1.ec_pubkey_tweak_add`. 2FA: port `trustedcoin.get_user_id`, `make_xpub` and TrustedCoin's published signing xpubs from Electrum 4.8.4's `electrum/plugins/trustedcoin/trustedcoin.py` (cite the file and version in a comment), then derive as 2-of-3 sorted multisig of the script type the keys' prefix gives. A `DerivationError` from embit becomes `WalletInputError("The descriptor could not be read.")`.

- [ ] **Step 4: Run** — Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add backend/src/lararium/modules/finance/bitcoin/derive.py backend/tests/finance/bitcoin/test_derive.py
git commit -m "feat: derive Bitcoin wallet addresses from Electrum keys, descriptors and lists"
```

### Task 3: Schema: wallet tables, chain columns on transactions, `chain_native`

**Files:**
- Create: `backend/migrations/versions/0046_finance_bitcoin.py`
- Modify: `backend/src/lararium/modules/finance/models.py` (Transaction: `chain`, `tx_hash`, `chain_address`), `backend/src/lararium/modules/finance/ledger.py` (`ChainFields`, `TransactionInput.chain`, upsert compares and writes it), `backend/src/lararium/modules/finance/connector_run.py` (`ConnectorRun.post(..., chain: ChainFields | None = None)`), `backend/src/lararium/modules/finance/instruments.py` (`IdentifierScheme` gains `"chain_native"`). (`wallet` is already an `AccountSubtype`.)
- Test: `backend/tests/finance/bitcoin/test_schema.py`, `backend/tests/finance/test_ledger.py` (add cases)

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class ChainFields: chain: str; tx_hash: str; address: str | None = None` in `ledger.py`; `TransactionInput.chain: ChainFields | None = None`.
  - Tables (all in `finance`, each with `connection_id uuid NOT NULL REFERENCES core.connections ON DELETE CASCADE`):
    - `bitcoin_addresses(connection_id, source text, branch smallint, address_index integer, address text, script_type text, first_used_at timestamptz, last_used_at timestamptz)`, PK `(connection_id, source, branch, address_index)`, UNIQUE `(connection_id, address)`.
    - `bitcoin_address_reports(connection_id, sync_id uuid, address text, chain_tx_count, chain_funded_txo_count, chain_funded_txo_sum, chain_spent_txo_count, chain_spent_txo_sum, mempool_tx_count, mempool_funded_txo_count, mempool_funded_txo_sum, mempool_spent_txo_count, mempool_spent_txo_sum` all `bigint NOT NULL`), PK `(connection_id, sync_id, address)`.
    - `bitcoin_transactions(connection_id, txid text, version bigint, locktime bigint, size bigint, weight bigint, sigops bigint, fee bigint, confirmed boolean, block_height bigint, block_hash text, block_time timestamptz, first_seen_at timestamptz, last_seen_at timestamptz, left_chain_at timestamptz, fee_transaction_id uuid, net_transaction_id uuid)`, PK `(connection_id, txid)`; both transaction ids `REFERENCES finance.transactions ON DELETE SET NULL`.
    - `bitcoin_inputs(connection_id, txid, input_index integer, prev_txid text, prev_vout bigint, is_coinbase boolean, scriptsig text, scriptsig_asm text, inner_redeemscript_asm text, inner_witnessscript_asm text, sequence bigint, witness text[], prevout_scriptpubkey text, prevout_scriptpubkey_asm text, prevout_scriptpubkey_type text, prevout_scriptpubkey_address text, prevout_value bigint, own boolean NOT NULL)`, PK `(connection_id, txid, input_index)`.
    - `bitcoin_outputs(connection_id, txid, vout integer, scriptpubkey text, scriptpubkey_asm text, scriptpubkey_type text, scriptpubkey_address text, value bigint NOT NULL, own boolean NOT NULL, transaction_id uuid REFERENCES finance.transactions ON DELETE SET NULL)`, PK `(connection_id, txid, vout)`.
    - `finance.transactions` gains `chain text`, `tx_hash text`, `chain_address text`, `CHECK ((chain IS NULL) = (tx_hash IS NULL))`, `CHECK (tx_hash IS NULL OR tx_hash ~ '^[0-9a-f]{64}$')`, `CHECK (chain_address IS NULL OR chain IS NOT NULL)`, and index `transactions_chain_hash_idx (chain, tx_hash) WHERE tx_hash IS NOT NULL`.
    - The instrument identifier scheme CHECK gains `chain_native` (value `^[a-z0-9-]{1,64}$`).

- [ ] **Step 1: Write the failing tests**:
  - `test_schema.py::test_migration_round_trips`: upgrade to head, downgrade to `0045`, upgrade again (follow `tests/finance/binance/test_migration.py`).
  - `test_schema.py::test_tx_hash_must_be_lower_hex`: inserting a transaction with `tx_hash='AB…'` fails the CHECK.
  - `test_ledger.py::test_chain_fields_are_stored_and_compared`: `upsert_connector_transaction` with `ChainFields("bitcoin", "ab"*32, "bc1q…")` stores the three columns; the same input again is `"unchanged"`; a different address is `"updated"`.

- [ ] **Step 2: Run** `uv run pytest tests/finance/bitcoin/test_schema.py tests/finance/test_ledger.py -q -k "chain or migration or tx_hash"` — Expected: FAIL.

- [ ] **Step 3: Write the migration and model/ledger changes.** `ConnectorRun.post` passes `chain` into `TransactionInput`.

- [ ] **Step 4: Run** — Expected: PASS; also `uv run pytest tests/finance/test_ledger.py -q` stays green.

- [ ] **Step 5: Commit**
```bash
git add backend/migrations/versions/0046_finance_bitcoin.py backend/src/lararium/modules/finance/models.py backend/src/lararium/modules/finance/ledger.py backend/src/lararium/modules/finance/connector_run.py backend/src/lararium/modules/finance/instruments.py backend/tests/finance/bitcoin/test_schema.py backend/tests/finance/test_ledger.py
git commit -m "feat: add Bitcoin wallet tables and chain fields on transactions"
```

### Task 4: Esplora client and fail-closed response parsing

**Files:**
- Modify: `backend/src/lararium/modules/finance/readonly_http.py` (constructor keyword `default_reset_wait: Decimal = Decimal(1)`, used where a 429 gives no reset header, line ~206), `backend/src/lararium/config.py` (`esplora_url: str = "https://mempool.space/api"`, env `LARARIUM_ESPLORA_URL`, trailing slash stripped), `backend/src/lararium/core/connectors.py` (`SyncContext.esplora_url: str = "https://mempool.space/api"`), `backend/src/lararium/worker/jobs.py` (pass it, as `coingecko_api_key` is passed)
- Create: `backend/src/lararium/modules/finance/bitcoin/client.py`, `backend/src/lararium/modules/finance/bitcoin/parse.py`
- Test: `backend/tests/finance/bitcoin/fixtures.py` (synthetic response builders), `backend/tests/finance/bitcoin/test_client.py`, `backend/tests/finance/test_readonly_http.py` (one case)

**Interfaces:**
- Produces (`parse.py`): frozen dataclasses mirroring the documented format with every field, amounts as `int`:
  - `AddressStats(address: str, chain: TxoStats, mempool: TxoStats)`, `TxoStats(tx_count, funded_txo_count, funded_txo_sum, spent_txo_count, spent_txo_sum)`
  - `Status(confirmed: bool, block_height: int | None, block_hash: str | None, block_time: datetime | None)`
  - `Prevout/Output(scriptpubkey, scriptpubkey_asm, scriptpubkey_type, scriptpubkey_address: str | None, value: int)`
  - `Input(txid, vout, is_coinbase, scriptsig, scriptsig_asm, inner_redeemscript_asm: str | None, inner_witnessscript_asm: str | None, sequence, witness: tuple[str, ...], prevout: Prevout | None)`
  - `Tx(txid, version, locktime, size, weight, sigops: int | None, fee: int, vin: tuple[Input, ...], vout: tuple[Output, ...], status: Status)`
  - `class MalformedResponse(DisplayableError)` — message names the endpoint and field path only.
  - `def parse_stats(body) -> AddressStats`, `def parse_txs(body) -> list[Tx]`, `def parse_status(body) -> Status`, `def parse_height(body) -> int`. A non-coinbase input without `prevout`, a missing `fee`, a non-integer amount, an unknown top-level field → `MalformedResponse` (unknown fields are also returned as flag names by `unknown_fields(body) -> set[str]` so the sync can flag them).
- Produces (`client.py`): `class EsploraClient` with `__init__(self, http: httpx.AsyncClient, base_url: str, *, sleep=..., clock=...)`, `async def tip_height(self) -> int`, `async def stats(self, address: str) -> tuple[AddressStats, JSONValue]`, `async def txs(self, address: str, after: str | None = None) -> tuple[list[Tx], JSONValue]` (no `after`: `/address/:a/txs`; with: `/address/:a/txs/chain/:after`), `async def status(self, txid: str) -> tuple[Status | None, JSONValue]` (404 → `None`). Each returns the raw body beside the parsed value. One pool `"esplora"` at `RateLimit(1 request / 1 second)`, `default_reset_wait=Decimal(60)`.

- [ ] **Step 1: Write the failing tests**:
  - `test_readonly_http.py::test_default_reset_wait_applies_without_header`: a 429 with no reset header waits the configured `Decimal(60)` (fake sleep records 60).
  - `test_client.py::test_paces_one_request_a_second`: five calls against a `httpx.MockTransport` with a fake clock record sleeps that keep sends ≥1 s apart (Review Focus 5).
  - `test_client.py::test_429_without_retry_after_waits_60`.
  - `test_client.py::test_history_pages_by_last_seen_txid`: the second call's path is `/address/<a>/txs/chain/<txid>`.
  - `test_client.py::test_missing_prevout_is_malformed`, `test_missing_fee_is_malformed`, `test_float_amount_is_malformed`.
  - `test_client.py::test_unknown_field_is_reported`: an extra `vout[].foo` is returned by `unknown_fields` as `"vout[].foo"`.
  - `test_client.py::test_404_status_is_none`.
  - `test_client.py::test_errors_never_carry_the_address`: every raised error's `str()` lacks the address used.

- [ ] **Step 2: Run** `uv run pytest tests/finance/bitcoin/test_client.py tests/finance/test_readonly_http.py -q` — Expected: FAIL.

- [ ] **Step 3: Implement.** `fixtures.py` builds documented-format JSON with invented hashes (`"aa"*32`, …) and addresses derived from the BIP84 vector.

- [ ] **Step 4: Run** — Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add backend/src/lararium/modules/finance/readonly_http.py backend/src/lararium/config.py backend/src/lararium/core/connectors.py backend/src/lararium/worker/jobs.py backend/src/lararium/modules/finance/bitcoin/client.py backend/src/lararium/modules/finance/bitcoin/parse.py backend/tests/finance/bitcoin/fixtures.py backend/tests/finance/bitcoin/test_client.py backend/tests/finance/test_readonly_http.py
git commit -m "feat: read Esplora address stats, histories and statuses fail-closed"
```

### Task 5: Fetch: scan, history, recheck, storage, identifiers

**Files:**
- Create: `backend/src/lararium/modules/finance/bitcoin/fetch.py`, `backend/src/lararium/modules/finance/bitcoin/accounts.py`
- Test: `backend/tests/finance/bitcoin/fake_esplora.py` (an in-memory chain served through `httpx.MockTransport`), `backend/tests/finance/bitcoin/test_fetch.py`

**Interfaces:**
- Consumes: `Wallet` (Task 2), `EsploraClient` + parse types (Task 4), tables (Task 3).
- Produces:
  - `accounts.py`: `async def wallet_account(ctx: SyncContext) -> UUID` — created on first need with `create_account(..., ledger_type="asset", subtype="wallet", balance_mode="transactions", source_connection_id=…)`, name = the connection's `display_name`, recorded in config `account_id`; `opened_on` set by Task 6.
  - `fetch.py`: `VERSION = "bitcoin-sync-1"`; `@dataclass class FetchResult: tip: int; scanned: dict[str, AddressStats]; changed_txids: set[str]; left_chain: set[str]; conflicts: list[str]` (conflicts = addresses held by another account; never logged); `async def fetch(ctx: SyncContext, client: EsploraClient, wallet: Wallet, cursor: Mapping[str, JSONValue]) -> FetchResult`.
  - Cursor keys it reads/writes: `"tx_counts": {address: int}` (chain + mempool), `"recheck": [txid, …]`.

- [ ] **Step 1: Write the failing tests** (DB, `run` fixture style of `tests/finance/trading212`):
  - `test_scan_stops_after_gap`: used receiving indexes {0, 1, 25} with gaps {0: 20, 1: 10} scans receiving 0…21 and change 0…9 (index 25 is not found: the spec's known limit), and every scanned address is a row of `bitcoin_addresses` and a `chain_address` identifier of the wallet account.
  - `test_raising_gap_finds_later_addresses`: the same chain with receiving gap 30 finds index 25.
  - `test_history_only_for_changed_addresses`: a second fetch with no chain change asks no `/txs` endpoint; one new transaction at address 1 asks only address 1's history and stops at the first stored confirmed txid.
  - `test_recheck_detects_a_dropped_transaction`: a transaction stored with 2 confirmations whose status becomes unconfirmed is in `left_chain`; one the server 404s is too; one with ≥6 confirmations at the last sync is not asked.
  - `test_every_response_is_a_raw_record`: raw records of types `address`, `txs`, `tx_status`, `tip` exist; repeating an identical response stores no second row; no raw record's `request` contains an address (the path is stored as the endpoint template plus the address's row index, e.g. `{"source": "d0", "branch": 0, "index": 3}`).
  - `test_typed_rows_hold_every_field`: one fixture transaction's `bitcoin_transactions`, `bitcoin_inputs`, `bitcoin_outputs` rows equal the fixture's fields, `own` set for the wallet's addresses.
  - `test_address_on_another_account_is_a_conflict`: an address pre-registered to a manual account stays there and is reported in `conflicts`.
  - `test_failed_fetch_keeps_what_it_stored` (Review Focus 3, 5): a transport raising `HttpRetryableError` after 15 addresses leaves those 15 address rows and raw records committed (written through `ctx.session_factory`), and the next fetch asks only from address 15 on.

- [ ] **Step 2: Run** `uv run pytest tests/finance/bitcoin/test_fetch.py -q` — Expected: FAIL.

- [ ] **Step 3: Implement.** Commit each address's stats, raw record and typed row in a short transaction of its own (pattern: `binance/prices.py::_fetch_bars`). Identifier registration with `ensure_identifier`, then `identifier_account` to detect a conflict.

- [ ] **Step 4: Run** — Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add backend/src/lararium/modules/finance/bitcoin/fetch.py backend/src/lararium/modules/finance/bitcoin/accounts.py backend/tests/finance/bitcoin/fake_esplora.py backend/tests/finance/bitcoin/test_fetch.py
git commit -m "feat: scan a Bitcoin wallet's addresses and store its transactions"
```

### Task 6: Normalize: movements in the ledger

**Files:**
- Create: `backend/src/lararium/modules/finance/bitcoin/normalize.py`, `backend/src/lararium/modules/finance/bitcoin/instruments.py`
- Test: `backend/tests/finance/bitcoin/test_normalize.py`, `backend/tests/finance/bitcoin/strategies.py`, `backend/tests/finance/bitcoin/test_properties.py`

**Interfaces:**
- Consumes: typed tables (Task 3/5), `ConnectorRun.post(..., chain=ChainFields(...))`, `ConnectorRun.contra_for`, `fees` pattern (`binance/records/__init__.py::fees`), `wallet_account`.
- Produces:
  - `instruments.py`: `async def bitcoin(ctx: SyncContext) -> UUID` — finder order `chain_native:bitcoin` → `binance_asset:BTC` → `coingecko:bitcoin` → the one `crypto` with symbol `BTC` → create (`crypto`, 8 decimals, symbol `BTC`, name `Bitcoin`); then `add_identifiers` for the missing three; several `BTC` crypto instruments → create a new one and flag `"several BTC instruments: merge them"`. Runs through `ConnectorRun.shared`.
  - `normalize.py`: `NORMALIZER_VERSION = "bitcoin-norm-1"`; `@dataclass class NormalizeReport(created: int, updated: int, unchanged: int, flagged: set[str])`; `async def normalize(ctx: SyncContext, txids: Collection[str] | None) -> NormalizeReport` (`None` = every stored transaction: the replay, run when config `normalizer_version` differs, which then rebuilds typed rows from raw records first).
  - External ids and descriptions (exact): receipt `f"{txid}:{vout}"` "Received bitcoin"; payment `f"{txid}:{vout}"` "Sent bitcoin"; fee `f"{txid}:fee"` "Bitcoin network fee"; shared `f"{txid}:net"` "Shared transaction" (flag `"shared transaction"`, raised once per sync that posts one; the flag names no hash).
  - `occurred_at = block_time`; booking date by `ConnectorRun.post` (London).

- [ ] **Step 1: Write the failing tests** (`test_normalize.py`), quantities in satoshis converted with `Decimal(n) / 10**8`:
  - `test_receipt_posts_each_own_output`: a 2-input stranger transaction paying the wallet's addresses 0 and 1 (outputs 30_000 and 20_000 sat) and 5 others posts two transactions `"<txid>:<vout>"`, each `+value` on the wallet against Unassigned, with `ChainFields("bitcoin", txid, <own address>)`; no fee transaction.
  - `test_spend_posts_payments_and_fee_not_change`: all inputs own (100_000), outputs 60_000 to a stranger, 39_000 change to own change address 0, fee 1_000 → `"<txid>:0"` −0.0006 counterparty identifier = the stranger's `chain_address` (unassigned), `"<txid>:fee"` −0.00001 `is_fee` against Unassigned; change posts nothing; account balance falls by 0.00061.
  - `test_spend_to_own_addresses_is_fee_only`.
  - `test_shared_transaction_posts_net_and_flags`: inputs partly own → one `"<txid>:net"` of own outputs less own inputs, no `ChainFields.address`, flagged once.
  - `test_address_less_output_with_value_is_a_payment` and `test_op_return_posts_nothing`.
  - `test_unconfirmed_is_not_posted_until_confirmed`: posted on the sync after it has a block, dated `block_time`.
  - `test_dropped_transaction_is_voided_and_reposted`: `left_chain` voids its ledger transactions (`voided_by="connector"`), sets `left_chain_at`, flags `"a confirmed transaction left the chain"`; confirming again in another block un-voids them with the new time.
  - `test_resync_is_unchanged` (Review Focus 3): normalising the same stored data twice gives `created == 0`, `unchanged == n` the second time.
  - `test_account_opens_on_first_block_day`.
  - `test_coinbase_output_is_a_receipt`.
  - `test_out_of_order_block_times_keep_their_own`: two blocks whose `block_time`s decrease post each movement at its own block's time.
  - `test_bitcoin_instrument_is_shared_with_binance`: with an existing instrument carrying `binance_asset:BTC`, the wallet uses it and adds `chain_native:bitcoin` and `coingecko:bitcoin`.
  - `test_replay_on_version_change`: a stored config `normalizer_version="bitcoin-norm-0"` replays every raw record and ends with the same ledger.
  - `test_properties.py::test_movements_sum_to_the_wallets_change` (Hypothesis, `strategies.py` builds random transactions over own/stranger inputs and outputs): for every transaction, the sum of the wallet-account postings it created equals own outputs − own inputs; and after normalising a random chain, the account's balance equals Σ(funded − spent) over the fake chain's addresses.

- [ ] **Step 2: Run** `uv run pytest tests/finance/bitcoin/test_normalize.py tests/finance/bitcoin/test_properties.py -q` — Expected: FAIL.

- [ ] **Step 3: Implement.** Classify each stored chain transaction by its `own` inputs: none (receipt), all (spend), some (shared). Contra via `run.contra_for(key, account_id, other_postings=0, identifier_id=...)`. A movement that now posts nothing is voided (`run.void`).

- [ ] **Step 4: Run** — Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add backend/src/lararium/modules/finance/bitcoin/normalize.py backend/src/lararium/modules/finance/bitcoin/instruments.py backend/tests/finance/bitcoin/test_normalize.py backend/tests/finance/bitcoin/strategies.py backend/tests/finance/bitcoin/test_properties.py
git commit -m "feat: post a Bitcoin wallet's receipts, payments and fees"
```

### Task 7: Reconciliation against reported address sums

**Files:**
- Create: `backend/src/lararium/modules/finance/bitcoin/reconcile.py`
- Test: `backend/tests/finance/bitcoin/test_reconcile.py`

**Interfaces:**
- Consumes: `FetchResult.scanned` (Task 5), `connector_run.reported_snapshot`, `connector_run.differences`, `connector_flags.publish_reconciliation`.
- Produces: `PLATFORM = "the Bitcoin blockchain"`; `async def check(ctx: SyncContext, account_id: UUID, account_name: str, scanned: Mapping[str, AddressStats], at: datetime) -> list[Mismatch]` (writes one `reported` BTC snapshot = Σ(`chain.funded_txo_sum − chain.spent_txo_sum`)/10⁸ at `at`); `async def publish(ctx, mismatches: list[Mismatch] | None) -> dict[str, JSONValue]` with `label="a Bitcoin wallet"`. Warning text: `f"{account_name} BTC differs from the Bitcoin blockchain on {day} by {difference}"`.

- [ ] **Step 1: Write the failing tests** (model on `tests/finance/trading212/test_reconcile.py`):
  - `test_consistent_wallet_has_no_warning`.
  - `test_missing_transaction_warns_once`: delete one posted receipt's raw/typed rows before normalising: one warning with the exact text, one notification; a second sync with the same state notifies nothing new.
  - `test_snapshot_never_changes_the_balance`: the account's valuation equals its postings whatever the snapshot says (`transactions` mode).
  - `test_agreeing_again_clears_the_warning`.

- [ ] **Step 2: Run** `uv run pytest tests/finance/bitcoin/test_reconcile.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement** after the Trading 212 module.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit**
```bash
git add backend/src/lararium/modules/finance/bitcoin/reconcile.py backend/tests/finance/bitcoin/test_reconcile.py
git commit -m "feat: check a Bitcoin wallet's balance against its addresses' reported sums"
```

### Task 8: The connector, cadence, flags and registration

**Files:**
- Create: `backend/src/lararium/modules/finance/bitcoin/sync.py`
- Modify: `backend/src/lararium/modules/finance/__init__.py` (`"bitcoin": BitcoinConnector()`), `backend/src/lararium/modules/finance/account_types.py` (`AccountType("bitcoin_wallet", "Bitcoin", "wallet", None, Api("bitcoin", {}), ())`, so `AccountType.name` is "Bitcoin wallet")
- Test: `backend/tests/finance/bitcoin/test_sync.py`, `backend/tests/finance/test_account_types.py` (one case)

**Interfaces:**
- Consumes: Tasks 2, 4–7; `load_credential` (purpose `DESCRIPTOR_PURPOSE = "descriptor"`), `connector_flags.publish_flags`.
- Produces: `class BitcoinConnector` with `name = "bitcoin"`, `version = VERSION`, `capabilities = frozenset({"balances", "transactions"})`, `check_access` → `True`, `sync(ctx) -> JSONValue`. `SYNC_EVERY = timedelta(hours=6)`; the cursor's `"synced_at"`; a sync not `ctx.requested` and within `SYNC_EVERY` of `synced_at` returns the cursor unchanged and sends no request. The credential holds `WalletInput` as JSON (`{"form", "electrum_type", "keys", "threshold", "descriptors", "addresses"}`); config holds `{"form", "electrum_type", "gaps": {"<source>": {"0": n, "1": n}}, "fingerprint"}`.
- Flags (exact names): `"address held by another account"` (count only, never the address), `"shared transaction"`, `"a confirmed transaction left the chain"`, `"no used address found"` (first sync scanned and found nothing), `"unknown field <path>"`.

- [ ] **Step 1: Write the failing tests**:
  - `test_sync_end_to_end`: a fake chain with one receipt and one spend → posted ledger, no warning, cursor `synced_at` set.
  - `test_sync_waits_six_hours_unless_requested`: a second sync 1 h later sends no request; with `requested=True` it syncs.
  - `test_first_sync_with_nothing_used_flags`.
  - `test_wallet_type_is_listed`: `account_type("bitcoin_wallet").api.connector == "bitcoin"`.
  - `test_registry_has_bitcoin`: `registry.connector("bitcoin")` is a `BitcoinConnector`.
- [ ] **Step 2: Run** `uv run pytest tests/finance/bitcoin/test_sync.py tests/finance/test_account_types.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement** `sync`: load credential → `build_wallet` (gap limits from config) → `fetch` → `lock_connection` → `normalize(changed ∪ left_chain)` (or replay) → `reconcile.check/publish` → `publish_flags` → cursor.
- [ ] **Step 4: Run** — Expected: PASS; `uv run lint-imports` clean.
- [ ] **Step 5: Commit**
```bash
git add backend/src/lararium/modules/finance/bitcoin/sync.py backend/src/lararium/modules/finance/__init__.py backend/src/lararium/modules/finance/account_types.py backend/tests/finance/bitcoin/test_sync.py backend/tests/finance/test_account_types.py
git commit -m "feat: sync Bitcoin wallets as a connector"
```

### Task 9: BTC prices without a Binance connection

**Files:**
- Modify: `backend/src/lararium/modules/finance/binance/client.py` (`BinanceClient.public(http, *, clock=..., sleep=...) -> BinanceClient`: no key, a signer that raises `RequestRefused("Binance request refused: a public client sends no signed query")`), `backend/src/lararium/modules/finance/binance/prices.py` (parametrise by holdings)
- Create: `backend/src/lararium/modules/finance/bitcoin/prices.py`
- Test: `backend/tests/finance/binance/test_prices.py` (stays green; one new case), `backend/tests/finance/bitcoin/test_prices.py`

**Interfaces:**
- Produces (in `binance/prices.py`):
  - `HeldFn = Callable[[AsyncSession, UUID], Awaitable[dict[str, Held]]]`, `PairsFn = Callable[[AsyncSession, UUID], Awaitable[dict[str, tuple[str, str]]]]`, `FlagFn = Callable[[SyncContext, Sequence[str]], Awaitable[None]]`.
  - `async def held_on(session: AsyncSession, accounts: Collection[UUID]) -> dict[str, Held]` (the body of today's `_held` after its account lookup; `_held` becomes a call to it).
  - `update_prices(ctx, client, *, held: HeldFn = _held, pairs: PairsFn = _pairs, flag: FlagFn = _flag, sleep=...)`; `price_assets` and `coingecko_prices` take the same `held`/`pairs` keywords. Defaults keep M7's behaviour exactly.
- Produces (in `bitcoin/prices.py`): `async def update_btc_prices(ctx: SyncContext, account_id: UUID) -> None` — builds `BinanceClient.public(ctx.http)`, fetches `exchangeInfo` (catalogue endpoint `exchange-info`) into a pairs map `{symbol: (baseAsset, quoteAsset)}`, and calls `update_prices` with `held=lambda s, _: held_on(s, [account_id])`, `pairs=` that map, `flag=` the wallet's flag publisher. Its `_mark_asked` state lands in the wallet connection's config (`bars_asked`), as the amended spec says.
- Called from `BitcoinConnector.sync` after reconciliation; a pricing failure is logged (`"BTC prices failed: %s"`, status only) and the sync continues.

- [ ] **Step 1: Write the failing tests**:
  - `binance/test_prices.py::test_public_client_refuses_signed_queries`.
  - `bitcoin/test_prices.py::test_wallet_only_btc_is_priced_from_first_day` (spec §11): no Binance connection; a wallet holding BTC from 2024-03-01 (invented) gets `binance` prices for every day to yesterday from fake `BTCEUR` bars, recorded on the shared instrument in EUR.
  - `bitcoin/test_prices.py::test_days_before_eur_markets_are_missing`: BTC held from 2019-06-01 with bars only from 2020-01-03 → days before have no price and the flag `"no price for BTC"` is raised once; valuation of those days is `missing_price`, never 0.
  - `bitcoin/test_prices.py::test_same_day_is_not_written_twice`: an existing `binance` price for a day (from a Binance connection) is not duplicated.
- [ ] **Step 2: Run** `uv run pytest tests/finance/binance/test_prices.py tests/finance/bitcoin/test_prices.py -q` — Expected: new cases FAIL, existing pass.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — Expected: PASS (all of `tests/finance/binance/test_prices.py` too).
- [ ] **Step 5: Commit**
```bash
git add backend/src/lararium/modules/finance/binance/client.py backend/src/lararium/modules/finance/binance/prices.py backend/src/lararium/modules/finance/bitcoin/prices.py backend/src/lararium/modules/finance/bitcoin/sync.py backend/tests/finance/binance/test_prices.py backend/tests/finance/bitcoin/test_prices.py
git commit -m "feat: price BTC held in a wallet from Binance's public bars"
```

### Task 10: The `HASH` evidence tier

**Files:**
- Modify: `backend/src/lararium/modules/finance/transfers/contra.py` (`eligible_records` gains columns `chain`, `tx_hash`, `chain_address`, and `address_holder`: the account the `chain_address` identifier with that value is registered to, else NULL), `backend/src/lararium/modules/finance/transfers/pairs.py`
- Test: `backend/tests/finance/transfers/test_hash.py`, `backend/tests/finance/transfers/test_properties_hash.py`

**Interfaces:**
- Produces (`pairs.py`):
  - Tiers renumbered by constant: `DECIDED = 1`, `HASH = 2`, `MUTUAL = 3`, `ONE_SIDED = 4`, `WEAK = 5`; `type Tier = Literal[1, 2, 3, 4, 5]`. Nothing persists tier numbers (checked: only `pairs.py` compares them), but grep `tests/` for literal tier numbers and update.
  - `Pairable` gains `chain: str | None = None`, `tx_hash: str | None = None`, `chain_address: str | None = None`, `address_holder: UUID | None = None`.
  - `def hash_tier(r: Pairable, c: Pairable) -> Tier | None` — `HASH` when both have the same non-null `chain`, `tx_hash` and `chain_address`, different accounts, the same instrument, `r.quantity == -c.quantity != 0`, and the registry does not disagree: `address_holder` is NULL or equals the receiving record's account (spec §5.8 "the registry wins").
  - `def _hash_edges(records: list[Pairable], decisions: _Decisions) -> _Edges` — groups by `(chain, tx_hash, chain_address)`; adds `HASH` edges not kept apart by a decision; no window check.
  - `_load_records`' `can_pair` gains `e.c.tx_hash.is_not(None)`; `pair_relation` merges `_hash_edges` into `same` before `best`/`exact` are computed.

- [ ] **Step 1: Write the failing tests** (`test_hash.py`, using the Binance and Bitcoin fixtures):
  - `test_binance_withdrawal_pairs_with_wallet_receipt`: Binance withdrawal (−0.01 BTC, network `BTC`, hash H, address A) and the wallet's receipt (+0.01, H, A) three days later pair as one transfer; Binance's fee stays its own transaction; net worth over the whole period changes by the fee only.
  - `test_batch_pairs_only_the_matching_output` (Review Focus 4): one chain transaction H paying the wallet at A (0.01) and B (0.02) and a stranger, with two Binance withdrawals (A 0.01, B 0.02): two transfers, each with its own output.
  - `test_same_address_and_amount_twice_is_review`.
  - `test_wallet_payment_pairs_with_binance_deposit`.
  - `test_two_own_wallets_pair`.
  - `test_registry_wins_over_hash`: A registered to a manual account → no `HASH` edge; the manual placeholder stands; moving A to the wallet account then pairs.
  - `test_hash_ignores_the_window`: receipt 30 days after the withdrawal still pairs.
  - `test_properties_hash.py::test_hash_pairs_exactly_matching_records` (Hypothesis over random hash/address/amount collisions): a pair forms iff chain, hash, address match with opposite equal amounts and no tie; no record is in two transfers.
- [ ] **Step 2: Run** `uv run pytest tests/finance/transfers/test_hash.py tests/finance/transfers/test_properties_hash.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new tests and the whole transfer suite: `uv run pytest tests/finance/transfers -q` — Expected: PASS.
- [ ] **Step 5: Commit**
```bash
git add backend/src/lararium/modules/finance/transfers/contra.py backend/src/lararium/modules/finance/transfers/pairs.py backend/tests/finance/transfers/test_hash.py backend/tests/finance/transfers/test_properties_hash.py
git commit -m "feat: pair on-chain movements of the same transaction, address and amount"
```
(Add any test file whose literal tier numbers changed to the `git add`.)

### Task 11: Binance fills the chain fields

**Files:**
- Modify: `backend/src/lararium/modules/finance/binance/records/capital.py` (`deposit`, `withdrawal`), `backend/src/lararium/modules/finance/binance/records/__init__.py` (`post(..., chain: ChainFields | None = None)`), `backend/src/lararium/modules/finance/binance/normalize.py` (`NORMALIZER_VERSION = "binance-norm-3"`)
- Test: `backend/tests/finance/binance/test_transfers.py` (new cases)

**Interfaces:**
- Produces: `CHAINS: Mapping[str, str] = {"BTC": "bitcoin", "SEGWITBTC": "bitcoin"}` in `capital.py`; `def chain_fields(label: str, row: Row) -> ChainFields | None` — `None` unless `chain_hash(row)` is set and `network` is in `CHAINS`; the address is the row's `address` (withdrawal: destination; deposit: Binance's deposit address), lower-cased only when it begins `0x`. Set on the principal transaction only when it is a transfer leg (not an ended withdrawal), never on fee or refund transactions.

- [ ] **Step 1: Write the failing tests**:
  - `test_btc_withdrawal_carries_chain_fields`, `test_segwitbtc_maps_to_bitcoin`, `test_bsc_btc_has_no_chain_fields`, `test_lightning_has_none`, `test_fee_and_refund_have_none`, `test_ended_withdrawal_has_none`.
  - `test_version_bump_fills_existing_rows`: rows normalised under `binance-norm-2` gain the fields after a sync under `binance-norm-3`.
- [ ] **Step 2: Run** `uv run pytest tests/finance/binance/test_transfers.py -q` — Expected: new cases FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/finance/binance -q` — Expected: PASS.
- [ ] **Step 5: Commit**
```bash
git add backend/src/lararium/modules/finance/binance/records/capital.py backend/src/lararium/modules/finance/binance/records/__init__.py backend/src/lararium/modules/finance/binance/normalize.py backend/tests/finance/binance/test_transfers.py
git commit -m "feat: record the chain, hash and address of Binance's on-chain BTC movements"
```

### Task 12: API routes and the web UI

**Files:**
- Modify: `backend/src/lararium/modules/finance/connections_api.py` (schemas `BitcoinWalletCreate`, `BitcoinPreviewIn/Out`, routes, `ConnectionOut.bitcoin: BitcoinStateOut | None`), `web/src/pages/ConnectionsPage.tsx` (`AddBitcoinForm`, `case "bitcoin"` in `ConnectForm`, card branch), `web/openapi.json`, `web/src/api/*.gen.ts` (`make openapi`)
- Test: `backend/tests/api/test_bitcoin_connections.py`, `web/src/pages/ConnectionsPage.test.tsx` (one case; follow the Binance form's test)

**Interfaces:**
- Consumes: `parse_input`, `refuse_private`, `build_wallet`, `first_receiving`.
- Produces:
  - `POST /api/v1/connections/bitcoin` (`name="create_bitcoin_connection"`, 201): body `{display_name, form, electrum_type?, keys?, threshold?, descriptors?, addresses?, gaps?}` with key material as `SecretStr`; 422 bodies are the fixed messages of Task 1 only; 409 `{"error": "already_connected", "message": "this wallet is already connected"}` by `config.fingerprint`; stores the credential (purpose `descriptor`), config, status `healthy`, `defer_sync`.
  - `POST /api/v1/connections/bitcoin/preview` → `{"first_receiving_address": str, "script_type": str}`; stores nothing.
  - `BitcoinStateOut`: `form`, `electrum_type`, `scanned` and `used` per branch, `last_check` (`"agrees"` or the warning text), `unconfirmed` count, `server` (host only). Never a key or descriptor.
  - UI copy (exact): form choice labels "Electrum master public key", "Output descriptor", "Address list"; help "In Electrum: Wallet → Information → Master Public Key. Never enter your seed phrase."; Lightning note "Lightning channel balances can't be seen from a public key; record them as a manual account."; gap help "Compare the balance with Electrum's. If it is lower, raise the gap limit."
- [ ] **Step 1: Write the failing tests**:
  - `test_create_and_sync_is_queued`; `test_duplicate_wallet_is_409`; `test_seed_phrase_422_does_not_echo` (Review Focus 1: the response body, the captured logs (`caplog`) and every `core.raw_records`/`core.events` row lack each input word); `test_preview_derives_bip84_vector` (returns `bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu`, writes no row); `test_state_never_carries_the_key`.
  - Web: entering the zpub shows the preview address; submitting calls `createBitcoinConnection`.
- [ ] **Step 2: Run** `uv run pytest tests/api/test_bitcoin_connections.py -q` and `cd web && npm test -- ConnectionsPage` — Expected: FAIL.
- [ ] **Step 3: Implement**; `make openapi`.
- [ ] **Step 4: Run** both — Expected: PASS; `make openapi-check` clean.
- [ ] **Step 5: Commit**
```bash
git add backend/src/lararium/modules/finance/connections_api.py backend/tests/api/test_bitcoin_connections.py web/src/pages/ConnectionsPage.tsx web/src/pages/ConnectionsPage.test.tsx web/openapi.json web/src/api
git commit -m "feat: connect a Bitcoin wallet from the Add account page"
```

### Task 13: Documents and the full check

**Files:**
- Modify: `docs/superpowers/specs/2026-11-11-financial-gate-phase1-design.md` (§6.3, §7.7, §13, §14 per M8 design §8), `docs/superpowers/specs/2026-11-27-bitcoin-wallet-design.md` (the §5.7 amendment in this plan's header; §9 Taproot note: embit derives the BIP 86 vector, so Taproot descriptors are supported), `README.md` and `README.zh-CN.md` (connector list: Bitcoin wallets, read-only, by master public key / descriptor / addresses; `LARARIUM_ESPLORA_URL`), `.env.example` or the compose env docs if they list settings.

- [ ] **Step 1: Edit the documents** as listed (no real figures).
- [ ] **Step 2: Run** `make check` on the remote Docker host setup — Expected: green (ruff, pyright, lint-imports, pytest, license check incl. embit MIT, openapi, web).
- [ ] **Step 3: Commit**
```bash
git add docs/superpowers/specs/2026-11-11-financial-gate-phase1-design.md docs/superpowers/specs/2026-11-27-bitcoin-wallet-design.md README.md README.zh-CN.md
git commit -m "docs: record M8 Bitcoin wallets in the specs and READMEs"
```
- [ ] **Step 4: After deploy, by the owner** (spec §11): connect the Electrum wallet with its master public key; the preview matches Electrum's first receiving address; the balance equals Electrum's and mempool.space's; the Binance BTC withdrawal shows as one transfer with Binance's fee separate; no reconciliation difference; net worth unchanged by the transfer except the fee.
