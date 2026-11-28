# M8 design: Bitcoin wallets

- Date: 2026-11-27 (brainstormed with the owner 2026-11-27)
- Status: approved by the owner 2026-11-27; plan `docs/superpowers/plans/2026-11-27-bitcoin-wallet.md`
- Builds on: `main` at `5dae7be` (M0–M7 Phase A merged; migrations up to `0045`). Migrations are numbered after whatever is on `main` when implementation starts.
- Parent spec: `docs/superpowers/specs/2026-11-11-financial-gate-phase1-design.md` (§6.3, §7.7, §8, §9, §13 M8, §14)
- Related:
  - the M7 Binance design (`2026-11-22-binance-design.md`), whose §5.12 stores what this milestone pairs with;
  - the M4 transfer-matching design (`2026-11-15-transfer-matching-design.md`), whose engine gains hash evidence here;
  - the M5 IBKR design (`2026-11-18-ibkr-flex-design.md`), whose typed-table rule and fail-closed parsing this follows.

## 1. Intent

The owner keeps bitcoin in a self-custodied wallet. Its balance should count in net worth as exactly as a bank balance does, matching what a block explorer shows, and a withdrawal from Binance to that wallet should be one transfer, not money that leaves Binance and money that arrives from nowhere.

A wallet is watched, never controlled: Lararium learns the wallet's public key, derives its addresses, and reads what the Bitcoin blockchain says happened at them. Nothing that can spend is ever entered.

Because Lararium is open source, the connector covers every way a Bitcoin wallet can be described by public data (every Electrum wallet type, output descriptors from other wallet software, and plain address lists), not only the owner's own wallet type.

### What the owner's wallet shows (read-only probe, 2026-11-27; no values recorded here)

The owner saved their wallet's master public key in the gitignored `private/m8-probe/`, and a probe derived its addresses and read them through mempool.space's public API.

- **Wallet**: Electrum, wallet type "bip32" (one key), script type p2wpkh, master public key with the `zpub` prefix.
- **Use**: only receipts so far; no spend, so no change address has been used. One address received more than once. Electrum's default gap limits (20 receiving, 10 change) miss no used address: the probe looked 30 addresses past each.
- **Batches**: every receipt is a transaction with between 6 and 20 outputs, most of them other people's. A transaction hash alone does not say which part is the owner's.
- **Binance**: the account's one on-chain BTC withdrawal (Binance network `BTC`, `txId` 64 lower-case hex digits) is one of the wallet's receipts, to an address the probe derived. Binance's `amount` equals the wallet's output exactly; Binance's `transactionFee` is not the transaction's chain fee (Binance pays the chain fee out of its own inputs and charges its own fee).
- **Checks that held on real data**: per address, the transactions' outputs less their spent inputs equal the API's funded-less-spent sums; the unspent outputs add up to the same; every transaction's `fee` equals its inputs less its outputs; every amount is a whole number of satoshis.
- **Pacing**: mempool.space answered HTTP 429 four times in about 100 requests sent 0.3 seconds apart.
- **Not seen**: a spend, change, an unconfirmed transaction, a deposit to Binance. Those rules below follow the documented shapes only.

## 2. Goals, non-goals, anti-goals

### Goals

1. A Bitcoin wallet is one connection and one ledger account, described by public data only: an Electrum master public key (any Electrum wallet type), an output descriptor, or a list of addresses. Anything that could spend is refused and never stored.
2. Every address the wallet uses is found, by deriving addresses up to a gap limit, and registered as the account's own identifier.
3. Every transaction touching the wallet is fetched, stored with one typed column per field, and posted: each output the wallet receives, each output it pays to someone else, and the chain fee when the wallet paid it.
4. The account's balance is the sum of its transactions (the blockchain is a complete ledger), checked on every sync against the balances the API reports for the wallet's addresses. A difference is reported, kept until explained, and never corrected silently.
5. A withdrawal from Binance to the wallet, a deposit from the wallet to Binance, and a payment between two of the owner's wallets each pair automatically into one transfer by the same transaction hash, address and amount. Binance's own fee and the wallet's chain fee stay separate postings.
6. BTC has a daily price for every day it is held that Binance's EUR markets cover (from January 2020), whether or not a Binance connection exists; an earlier day shows as missing a price, never as 0 (§5.7).
7. The chain data comes from a public Esplora API by default, and the operator can point it at their own server.

### Non-goals

- **EVM chains** (Ethereum and the chains that run like it). The owner decided on 2026-11-27 that they are a later milestone of their own; §8 corrects the parent spec. The pairing rule here is written for any chain, so that milestone adds evidence, not a new rule.
- **Lightning.** Channel balances cannot be derived from a master public key (§3.1). A channel's opening and closing transactions are ordinary on-chain spends and receipts.
- **Other chains and sidechains** (Liquid, Bitcoin testnets, signet).
- **The Electrum server protocol.** Only Esplora's HTTP API is read. A self-hosted server must speak it (§3.2).
- **Tor.** Requests go over the clearnet to the configured host.
- **Spending, signing, building transactions, fee estimation, coin control.** The client only sends queries.
- **Importing the wallet's labels** (Electrum labels, BIP 329). Descriptions come from what the chain shows.
- **Tax lots and realised gains** (parent §2.2).

### Anti-goals

- A seed phrase, a private key or an extended private key accepted, stored, logged or echoed back, even in an error message.
- A wallet balance that differs from the blockchain's because an address was not scanned, a transaction was missed or posted twice, or a transaction that later left the chain was kept.
- A Binance withdrawal and the wallet's receipt both counted, so net worth rises by the amount.
- The master public key or a descriptor anywhere but the encrypted credential store; an address or a transaction hash in a log line or a notification. (The identifier registry audits a registered address as it audits a bank account number.)
- BTC valued at 0 because no price was fetched.

## 3. Constraints

### 3.1 Electrum facts (checked against Electrum 4.8.4's source and documentation, 2026-11-27)

- **What Electrum exports.** Only the master public key of each keystore, at Wallet → Information, which also shows the wallet type and script type. Electrum cannot export an output descriptor (issue #5694 is open).
- **Key forms** (SLIP-0132 version bytes; Electrum's `constants.py`):

| Wallet | What Information shows | Script |
|---|---|---|
| Electrum seed, segwit | `zpub` (derived at `m/0'`) | p2wpkh |
| Electrum seed, standard | `xpub` (at `m`) | p2pkh |
| BIP39 seed or hardware wallet | `xpub` / `ypub` / `zpub` at the chosen path | p2pkh / p2wpkh-p2sh / p2wpkh |
| Multisig, m of n | one key per cosigner, all `xpub`, all `Ypub` or all `Zpub` | p2sh / p2wsh-p2sh / p2wsh, keys sorted per address |
| 2FA (TrustedCoin) | the owner's two keys; the third is TrustedCoin's, derived from TrustedCoin's published key and the owner's two | 2 of 3, p2sh (`xpub`) or p2wsh (`Zpub`) |
| Imported addresses | the address list | per address |
| Pre-2.0 ("old") seed | 128 hex digits: an uncompressed public key, not BIP32 | p2pkh |

- **An `xpub` is ambiguous**: one key is p2pkh, several keys are p2sh multisig. The owner says which (§5.1).
- **Derivation.** The exported key is at account level: receiving address *i* is `<key>/0/i`, change address *i* is `<key>/1/i`. An old seed's address *i* on branch *c* is the key's point plus `sha256d("i:c:" + key bytes)·G`, uncompressed, p2pkh.
- **Gap limits.** Per wallet file: 20 for receiving, 10 for change, by default. The receiving limit can be changed (`changegaplimit`); addresses can also be created past it by hand.
- **No Taproot wallets** in Electrum. Output descriptors from other software can be Taproot (§5.1).
- **Lightning** keys come from a private key the master public key does not reveal. A watcher sees only the on-chain transactions that fund or close a channel.

### 3.2 Esplora facts (checked against `Blockstream/esplora/API.md`, mempool.space's docs and the probe, 2026-11-27)

- **Address stats**: `GET /address/:address` → `chain_stats` and `mempool_stats`, each `tx_count`, `funded_txo_count`, `funded_txo_sum`, `spent_txo_count`, `spent_txo_sum`. There is no balance field: the balance is funded less spent.
- **History**: `GET /address/:address/txs` returns up to 50 unconfirmed transactions and the 25 newest confirmed; `GET /address/:address/txs/chain/:last_seen_txid` pages further back, 25 at a time, newest first.
- **Transaction**: `GET /tx/:txid` and each history entry give `txid`, `version`, `locktime`, `size`, `weight`, `fee`, `vin[]` (`txid`, `vout`, `is_coinbase`, `scriptsig`, `scriptsig_asm`, `inner_redeemscript_asm`, `inner_witnessscript_asm`, `sequence`, `witness[]`, `prevout`), `vout[]` (`scriptpubkey`, `scriptpubkey_asm`, `scriptpubkey_type`, `scriptpubkey_address`, `value`) and `status` (`confirmed`, `block_height`, `block_hash`, `block_time`, null while unconfirmed). mempool.space also returns `sigops`, which the document does not list. `GET /tx/:txid/status` gives the status alone. `GET /blocks/tip/height` gives the chain tip.
- **Amounts** are satoshis (whole numbers in every response the probe read).
- **Hosts.** mempool.space and blockstream.info both serve it without a key today. mempool.space publishes no rate numbers, answers 429 when exceeded, and may ban repeat offenders; its privacy policy keeps the IP address and requests in server logs for 10 days. Blockstream moved its keyed, metered API to `enterprise.blockstream.info`; its keyless `/api` still answers, with rate limits and no promise of how long.
- **Self-hosting.** Blockstream's and mempool's `electrs` forks serve this API. romanz `electrs`, Fulcrum and ElectrumX speak only the Electrum protocol, so they cannot be used (§2 non-goals).
- **Privacy.** The server learns every address asked about and the caller's IP, and can link them to one owner (Electrum's FAQ says the same of Electrum servers). The owner accepted this for the default public server on 2026-11-27.
- **Block time** is the block header's timestamp: set by the miner, up to about two hours off, and not always increasing from one block to the next.

### 3.3 Binance facts (from M7's §5.12 and the M7 probe)

- Each deposit and withdrawal row stores `network`, `tx_hash` (lower-case, set only for on-chain rows), `address` and `amount`; a deposit also `source_address`, a withdrawal also `transactionFee`.
- Binance's BTC networks (its public coin list, 2026-11-27): `BTC` and `SEGWITBTC` are the Bitcoin chain; `LIGHTNING` is Lightning; `BSC` and `ETH` are BTC tokens on other chains and never reach a Bitcoin address.
- Batching is not documented by Binance; the probe saw it on every receipt.
- Whether a deposit's `amount` equals the output Binance received is not documented and the owner has made no BTC deposit to see it.

### 3.4 Project rules

- Every field a source returns gets a typed column, even when derivable.
- No real figures in repository documents: examples use invented amounts, addresses and dates.
- Connectors cover the platform completely; what no real data has exercised is said to be unverified, and its tests are built from documented shapes and published test vectors.
- Modules depend on `core`, never on each other; finance sub-packages may use the finance package's shared parts.

## 4. Approaches considered

### 4.1 How the wallet's balance is kept (owner chose B, 2026-11-27)

- **A. As Binance does (`hybrid`)**: a reported balance per sync is the account's balance, and the records explain the changes between two reports. Built for an exchange, whose internal movements the API may not all list; on a blockchain it only adds a race to manage (a transaction arriving between a balance read and a history read) with nothing to gain.
- **B. Chosen: the transactions are the balance, the reported balances check it.** The blockchain is complete, and the probe confirmed the transactions add up to the reported sums. Each sync still reads every address's sums and compares their total with the account's balance; a difference is a warning, never a correction.
- **C. Reported balances only.** Rejected: without transactions there is nothing to pair with Binance by hash.

### 4.2 Scope (owner chose Bitcoin only, 2026-11-27)

The parent spec's M8 is Bitcoin and EVM chains. The owner holds only bitcoin, in one Electrum wallet, so EVM has no real data to accept against and is a different platform (tokens, contracts, keyed indexers). It becomes its own later milestone. Within Bitcoin, the scope is complete: every Electrum wallet type, descriptors and address lists (§5.1).

### 4.3 Where the chain data comes from (owner chose a public server, 2026-11-27)

- **Chosen: a public Esplora server by default** (mempool.space), with its base URL set by the operator. No setup, and no more exposure than Electrum's own default servers already have.
- **A self-hosted Bitcoin node**: complete privacy, at the cost of about 700 GB, days of first sync and one more service to run. Available by changing the URL to a self-hosted Esplora server.
- **The Electrum protocol** (TCP JSON-RPC): would reach more self-hosted servers, but has no fee or input values (each input's previous transaction must be fetched and decoded) and its scripthash methods are being replaced in protocol 1.7. Left for later.

### 4.4 What a ledger transaction is (designer's call)

- **One ledger transaction per chain transaction** is the obvious shape, but a transfer leg has exactly two postings (M4), and one chain transaction can carry several of the owner's movements: two Binance withdrawals batched into one transaction, or one spend paying Binance and someone else.
- **Chosen: one ledger transaction per movement.** Each output the wallet receives, each output it pays to another address, and the fee it pays are their own transactions, each with the address it names. Their sum is the chain transaction's effect on the wallet. A Binance withdrawal then pairs with exactly the output it paid (§5.8).

## 5. Design

### 5.1 The connection

- **An account type `bitcoin_wallet`** ("Bitcoin wallet", institution "Bitcoin") with `Api("bitcoin", {})`. One connection is one wallet and one ledger account.
- **What the owner enters**, in one of three forms:
  1. **Electrum master public key**: the wallet type (single key; multisig *m* of *n*; 2FA; old seed) and the key or keys Wallet → Information shows. A single `xpub` / `ypub` / `zpub` gives p2pkh / p2wpkh-p2sh / p2wpkh. Multisig takes *n* keys of one form and *m*, sorted per address. 2FA takes the owner's two keys; the third is derived as Electrum's `trustedcoin.py` derives it. An old seed's key is the 128 hex digits.
  2. **Output descriptor** (Bitcoin Core, Sparrow, other wallets): one with a `<0;1>` multipath step, or a receiving and a change descriptor. Every script type the descriptor library derives for public keys: `pkh`, `sh(wpkh)`, `wpkh`, `tr` (key path), and `sh` / `wsh` / `sh(wsh)` around `multi` or `sortedmulti`. A checksum, when given, is checked.
  3. **Addresses**: a list (Electrum's "imported addresses" wallet, or any address the owner wants watched). Nothing is derived; the list is the wallet.
- **Everything is turned into descriptors** when the connection is created, and both the owner's input and the descriptors are kept. Electrum's old-seed and 2FA keys are the exceptions the descriptor language cannot express; they derive through their own code, tested against Electrum's own test vectors.
- **Before saving**, the form shows the first receiving address it derived, so the owner can compare it with Electrum's Addresses tab. A wrong wallet type shows here as a different address.
- **Refused, never stored, never echoed**: anything private (an extended private key of any prefix, a WIF private key, 64 hex digits that are not a known public form, and twelve or more words, which is a seed phrase), and keys of other networks (`tpub`, `upub`, `vpub` and their multisig forms). The refusal names what was recognised ("this looks like a seed phrase: never enter it anywhere; Lararium needs the master public key from Wallet → Information") without repeating any of it. Request bodies of this route are never logged, as for the other credential routes.
- **Storage.** The owner's input and the descriptors are a credential (encrypted at rest, purpose `descriptor`): a master public key reveals every past and future address. The connection's config holds what is not secret: the form, the wallet type, the gap limits and a fingerprint (the SHA-256 of the sorted descriptors) that refuses connecting the same wallet twice (409, as for a duplicate Binance account). No secret is needed to read the chain, so `check_access` returns true.
- **Gap limits**, per branch and changeable on the connection: 20 receiving and 10 change for Electrum keys (Electrum's defaults), 20 and 20 for descriptors (BIP 44). An address list has none.
  - Changed on the connection card through `PATCH /api/v1/connections/{id}/bitcoin/gaps`; a change requests a sync. (decided during implementation, 2026-11-27)
  - Inputs are capped: at most 20 keys, 2 descriptors and 10,000 addresses; a key or address at most 1,000 characters, a descriptor at most 10,000. (decided during implementation, 2026-11-27) Private material is refused by the server only: the web form sends what it is given, and the server refuses it without storing, logging or echoing it. Entering a seed phrase or a private key is the person's mistake, not one the code guards against twice (owner, 2026-11-28).

### 5.2 Fetching and pacing

- **The server** is `LARARIUM_ESPLORA_URL`, default `https://mempool.space/api`, read through the read-only client (GET only, that one host, no redirects).
- **Pacing**: one request a second. A 429 waits out its `Retry-After`, or 60 seconds when it gives none (the shared client's default is 1 second, so this client sets its own), within the client's retry limit; a sync that is still refused stops and is retried at the next sync. The probe's 429s came at three requests a second.
- **A long first scan commits as it goes** (through `ctx.session_factory`, as Binance's first sync does): the addresses, raw records and typed rows already fetched survive a failed sync, so the next one resumes instead of starting over. Posting happens only once a sync's scan and history are complete.
- **How often**: a wallet syncs at most every 6 hours, or when the owner presses "Sync now" (`SyncContext.requested`). Transactions are posted only once confirmed (§5.5), and blocks come about every 10 minutes, so more often buys little and costs the public server.
- **One sync** (new order, decided during implementation, 2026-11-27): tip, recheck, scan, history, prices, lock, post, check, flags.
  1. **The tip height.** It is refused if it is below a stored block that was at least 6 confirmations deep, or more than `100 + minutes since the last sync / 5` blocks above the previous tip. A history transaction or status more than 2 blocks above the tip is refused too.
  2. **Recheck**, before the scan. Every stored transaction with fewer than 6 confirmations at the last sync is asked for again (`/tx/:txid/status`): one that left the chain or moved to another block is updated (§5.5). A transaction found gone (404), unconfirmed again, or confirmed without a block time makes the sync forget the histories of the addresses it touches, so a replacing (RBF) or double-spending transaction is found in the same sync.
  3. **Scan.** For each branch of each descriptor, the stats of addresses 0, 1, 2, … until *gap* addresses in a row have never had a transaction. Each scanned address is stored and registered (§5.3). A branch stops at 100,000 addresses ("The wallet has more addresses than Lararium scans.").
  4. **History.** An address's history is asked for only when its reported count (confirmed plus unconfirmed) differs from the count whose history is fully stored (`history_tx_count`), or its confirmed count differs from the confirmed part of that history (`history_chain_count`): a transaction the recheck still found unconfirmed may confirm before its address's stats are read, which leaves the total as it was (decided during implementation, 2026-11-27). The pages run newest first and stop only at a transaction that was at least 6 blocks deep when that address's history last completed (`history_height`); the sync fails if more distinct transactions arrive than the count says. A history counts as whole only by what the walk holds, never by what the stats claim: a walk to the end holds what it saw; one that stops at the stopping point holds the last whole history and what it saw anew. Only a whole history records the count and the stopping point; one short of the stats' count (a server whose history lags its counts, or answers short) is walked in full at the next sync (decided during implementation, 2026-11-28).
  5. **Prices** (§5.7), then the sync **locks** its connection.
  6. **Post** what changed, **check** the balance (§5.6), publish flags.
- **Amounts and ids.** Any satoshi amount or address sum above 2,100,000,000,000,000 (the coin's total supply) is a malformed response, and so is a transaction id (a transaction's or an input's) that is not 64 lower-case hex digits, the only form the ledger's hash column takes and an exchange's hash is compared in (decided during implementation, 2026-11-27).
- **A failed sync keeps what it committed.** The next sync asks every address's stats again from index 0 and only the histories it lacks. What still has to be posted is kept in the database (`bitcoin_transactions.pending`) and cleared only by the posting, in that sync's own transaction, so a sync that fails after fetching loses nothing (decided during implementation, 2026-11-27).
- **Cost.** About one request per scanned address per sync, plus history pages for the addresses that changed. The owner's wallet is about 35 requests. A wallet with a thousand used addresses takes about 20 minutes a sync at this pace; that is accepted, and the scan is the first thing the plan's pacing tests cover.
- **Raw records.** Every response is a raw record (`record_type` the endpoint), deduplicated by content hash; address stats are reports and get a sighting per sync.

### 5.3 Storage

Typed tables, one column per documented field (and `sigops`, which mempool.space returns), every amount in satoshis as a `bigint`:

- **Addresses**: connection, descriptor, branch, index, address, script type, first and last seen used.
- **Address reports**: connection, address, sync, the ten stats fields.
- **Transactions**: connection, `txid`, `version`, `locktime`, `size`, `weight`, `sigops`, `fee`, `confirmed`, `block_height`, `block_hash`, `block_time`, when first and last seen, and the ledger transactions it posted (fee or net).
- **Inputs**: connection, `txid`, index, every input field with `prevout` flattened, `witness` as `text[]`, and whether the prevout is the wallet's.
- **Outputs**: connection, `txid`, `vout`, every output field, whether it is the wallet's, and the ledger transaction it posted.

Each table's row links to the ledger transaction it posted, as Binance's do. Names, keys and indexes are left to the builder.

**Every scanned address is a `chain_address` identifier of the wallet's account** (through `ensure_identifier`), so that a Binance withdrawal to any of them names this account. A bech32 address (one beginning `bc1`, in any case) is case-insensitive, so the registry and Binance's chain fields store it in lower case, as the wallet derives it: a withdrawal Binance reports to the address in upper case names the same account and pairs by hash (decided during implementation, 2026-11-27). An address already registered to another account stays where it is: the connection flags it ("an address of this wallet is registered to *account*"), and the owner moves it. This is the case of an owner who registered their wallet's address on a manual wallet account under M7 (§6).

### 5.4 The account and the instrument

- **One account per connection**: `asset` / `wallet`, balance mode `transactions`, source the connection, named as the owner names the connection ("Bitcoin wallet" by default). It opens on the date of the wallet's first confirmed transaction. An account made while the wallet had none has no open date until a sync finds one, which sets it once; a set date is never moved (decided during implementation, 2026-11-27). The chain holds the whole history, so there is no opening adjustment.
- **Bitcoin** is one instrument wherever it is held. A new identifier scheme `chain_native` (value `bitcoin`) names a chain's own coin. The wallet finds the instrument by `chain_native` `bitcoin`; else by `binance_asset` `BTC`; else by `coingecko` `bitcoin`; else the one `crypto` instrument with symbol `BTC`; else creates one (`crypto`, 8 decimals). It then adds whichever of the three identifiers the instrument lacks, so Binance's finder (M7 §5.5) and the price sources (§5.7) find the same instrument. Several `crypto` instruments with symbol `BTC` are not guessed between: a new one is created and flagged for the owner to merge, as in M7.

### 5.5 Transactions in the ledger

A chain transaction touches the wallet when one of its inputs spends, or one of its outputs pays, an address the wallet scanned. It becomes one ledger transaction per movement (§4.4), each a connector transaction on the wallet's account with chain `bitcoin`, its hash, and the address the movement is at (§5.8), dated `block_time`:

| The chain transaction | Ledger transactions (external id) | Postings |
|---|---|---|
| No input is the wallet's (a receipt) | one per output paying the wallet (`<txid>:<vout>`) | + value; contra from `transfer_contra` (Unassigned unless paired) |
| Every input is the wallet's (a spend) | one per output paying another address (`<txid>:<vout>`), counterparty that address | − value; contra from `transfer_contra` |
| | the fee (`<txid>:fee`) | − `fee` (`is_fee`) against Unassigned |
| | outputs paying the wallet are change | none |
| Some inputs are the wallet's, some not (a shared transaction: coinjoin, payjoin) | one (`<txid>:net`), flagged once | the wallet's outputs less its inputs, against Unassigned; no fee posting, no pairing |

- **The sum** of a chain transaction's ledger transactions is always its outputs to the wallet less its inputs from the wallet. A spend to the wallet's own addresses only is its fee alone.
- **An output with no address** (a script the API names no address for) that carries value is a payment without a counterparty. A zero-value output (`OP_RETURN`) posts nothing.
- **A paid address** becomes a `chain_address` identifier (unassigned unless it is an own account's), so it appears in Suggestions as "Bitcoin address", as Binance's do.
- **A receipt has no counterparty identifier**: its inputs are the sender's addresses (for Binance, its hot wallet), which say nothing about whose account it came from. Hash evidence pairs it (§5.8).
- **Confirmed only.** An unconfirmed transaction is stored typed and shown on the connection ("1 unconfirmed incoming"), and posted when it has one confirmation. Pending postings count in balances, and an unconfirmed transaction can be replaced or dropped, so counting it would move net worth by money that may never arrive. A Binance withdrawal waiting for its block sits in In transit meanwhile, which is what it is.
- **Leaving the chain.** A transaction with fewer than 6 confirmations is rechecked each sync. One that is no longer confirmed has its ledger transactions voided by the connector (and posted again, with the new block's time, if it confirms again); one the server no longer knows is voided and flagged.
- **A fee that does not add up.** A spend whose inputs less outputs differ from the server's `fee` posts nothing new, keeps whatever it posted before, stays pending and is flagged "a transaction's fee does not add up"; the rest of the sync goes on. A shared transaction is flagged when its movement is first posted. (decided during implementation, 2026-11-27)
- **Coinbase outputs** (mining) are receipts.
- **Description**: "Received bitcoin", "Sent bitcoin", "Bitcoin network fee", "Shared transaction", in the house style; wording is the builder's.
- **Normalizer.** Versioned; a new version replays every stored transaction from the typed rows (§5.3), which the fetch builds from the raw records as it stores them, rather than parsing the raw records again (decided during implementation, 2026-11-27).

### 5.6 Checking the balance

- Each sync writes one `reported` snapshot of BTC on the account: the sum over every scanned address of `chain_stats.funded_txo_sum − chain_stats.spent_txo_sum` (confirmed only, the same basis as posting), as of the sync. The check is made at the later of the sync's time and the newest block time, because a block may be stamped up to two hours ahead (decided during implementation, 2026-11-27). The account is in `transactions` mode, so the snapshot is a check, never the balance, as Trading 212's is.
- The difference between that snapshot and the account's posted balance is found and published through the shared `differences` / `publish_reconciliation` path: a warning on the connection, notified once, kept until a later sync agrees. It is never corrected automatically.
- **What a difference means**: a transaction the history did not return, a posting rule wrong for a shape no real data has shown (the shared transaction, an address-less output), or the server answering inconsistently between the scan and the history. Because the two numbers come from separate endpoints, a missed address cannot be caught this way: the scan's gap rule is the only protection, and the gap limits are the owner's to raise (§6).

### 5.7 Prices

- **Binance's public daily bars** price BTC for every day a connected Bitcoin wallet holds it, whether or not any Binance connection exists. The bars and the list of trading pairs need no key. The EUR conversion, the price date and the one-source-per-day rule are M7 §5.10's.
- **Each connection keeps its own record of the days it asked for, as M7 does; the bars table is shared**, so a day two connections both fetch is stored once. Prices run before the sync locks its connection (§5.2), so BTC first posted in a sync is priced by the next one (at most 6 hours later, or at once with "Sync now"). "No price for BTC" stands while any held day has no price. (decided during implementation, 2026-11-27)
- *Superseded by the second bullet (decided during implementation, 2026-11-27): each connection records the days it asked in its own config (`bars_asked`), as M7's walk does, and no shared per-instrument table of days was built.* **This is more than moving M7's pricer.** M7's pricer takes the pairs from a connection's exchange information, keeps the days it has fetched in that connection's config, and finds the assets held through that connection's accounts. A wallet-only owner has none of these, so the shared pricer gets its own state: the pairs fetched without a connection, and the days fetched per instrument in a shared table, with the assets held found across every Binance wallet account and connected Bitcoin wallet account. M7's per-connection walk then uses the same state, so the two never fetch the same days twice. Where it lives is the builder's call within §3.4.
- **Coverage.** Binance's EUR markets begin in January 2020 (BTCUSDT from 2017, but M7 never assumes a stablecoin is worth a dollar). CoinGecko (id `bitcoin`), when the operator has set a key, prices a day Binance cannot, but its free plan reaches back only 365 days. So BTC held before January 2020 shows `missing_price` for those days, flagged once, and the owner can enter prices by hand. This is a known limit for other owners; the owner's own wallet has no transaction before 2020 (probe, 2026-11-27).
- **No price** is never 0.

### 5.8 Transfers: pairing by transaction hash

- **New columns on `finance.transactions`**: `chain`, `tx_hash` and `chain_address` (the address of the output the movement is). The parent spec (§6.3) asked for the chain and hash; none existed. They are set only for on-chain movements.
  - **The wallet** sets them on every movement it posts (§5.5); the address is the receiving output's address for a receipt and the paid address for a payment. A shared transaction's movement gets the chain and hash, no address.
  - **Binance** sets them on the principal transaction of an on-chain deposit or withdrawal whose `network` is `BTC` or `SEGWITBTC` (chain `bitcoin`): the `tx_hash` of §5.12, and the address the output paid (a withdrawal's `address`, a deposit's `address`, which is Binance's own deposit address), a bech32 one lower-cased (§5.3). Fee and refund transactions get none. Existing rows are filled by a normalizer version bump and replay. Other networks stay empty until their chain's milestone maps them.
- **A new evidence tier, `HASH`**, below the owner's decisions and above every other tier: two eligible records on two different own accounts, of one instrument, with the same `chain`, `tx_hash` and `chain_address`, and quantities of opposite sign and equal size, are the two sides of one transfer. The booking window does not apply (a withdrawal is applied hours before its block). It is built as the other tiers are: a column in the eligible records, a field on `Pairable`, an edge builder joined into the pair relation.
- **What pairs:**
  - **Binance → wallet.** The withdrawal names the wallet's account (its address is the account's identifier), so it opens an awaiting transfer as soon as Binance syncs. When the wallet's receipt arrives, `HASH` pairs them and its contra moves to In transit. Binance's `transactionFee` stays Binance's fee transaction.
  - **Wallet → Binance.** The wallet's payment to Binance's deposit address pairs with Binance's deposit of the same hash, address and amount. The wallet's chain fee stays the wallet's fee transaction. (No real deposit has shown whether Binance credits exactly the output; if it credits less, the pair fails the amount and stays for the owner to pair.)
  - **Wallet → own wallet.** One connected wallet's payment pairs with the other's receipt.
- **A hash pair is never held back by the coverage gate**, since a shared hash is no amount-and-date guess. Identical twins in one transaction pair in a fixed order, as other twins do. (decided during implementation, 2026-11-27)
- **Several of the owner's movements in one chain transaction** pair one to one, because each is its own movement with its own address and amount. Two that share an address and an amount are a tie and go to review.
- **The registry wins over the hash** (designer's call). When the shared address is registered to an account other than the record's own (the receiving account for a receipt), `HASH` makes no edge: the owner's registration says where that address's money goes, and the engine already acts on it (a placeholder on a manual account). The address-conflict flag (§5.3) tells the owner; moving the address re-matches, and `HASH` then pairs. (Production has no `chain_address` registered today, 2026-11-27, so the owner's wallet is not affected.)
- **The pair loader** today loads only records that are bank transfers or name an identifier, an own account or a card merchant; a wallet receipt names none, so a record with a `tx_hash` is added to that condition.
- **A wallet receipt that pairs with nothing** (from a stranger, an exchange not connected, a mined coin) stays on Unassigned with its hash shown, for the owner to pair or leave.
- **Heal** already runs after every sync, so whichever side syncs second completes the transfer.

### 5.9 Notifications and flags

- A reconciliation difference (§5.6).
- An address of the wallet registered to another account (§5.3).
- A shared transaction (§5.5). Like the flag for a transaction that left the chain, below, it is an event flag: notified once per connection, as Binance's event flags are, and kept on the connection (decided during implementation, 2026-11-27).
- A spend whose fee does not add up (§5.5).
- "No used address found", on every sync until the wallet is first used (decided during implementation, 2026-11-27).
- A confirmed transaction that later left the chain (§5.5).
- The server failing: the connection's existing `failing` status and its notification.

### 5.10 API and UI

- A create route for the account type (§5.1) and a route that previews the first receiving address without saving.
- Add account → "Bitcoin wallet": the three forms of §5.1, the wallet type for an Electrum key, the gap limits under "Advanced", and short steps for finding the master public key in Electrum. The derived address preview before saving.
- The connection card: the wallet's form and type (never the key), addresses scanned and used per branch, the last check (agrees, or the difference), unconfirmed transactions waiting, and the server it reads.
- Credits the server's operator where its data shows, if its terms ask (mempool.space's do not, as far as read).
- The OpenAPI client is regenerated.

## 6. Edge cases

- **An address used beyond the gap limit** (Electrum lets an owner create addresses past it) is never scanned, and its coins are missing from both the posted and the checked balance. The owner raises the gap limit on the connection, and the next sync scans further. The UI says, beside the gap limits, that the wallet's balance in Electrum is the number to compare.
- **An xpub entered with the wrong wallet type** derives different addresses; the preview address (§5.1) is the guard, and a wallet that scans with no used address at all says so on its first sync.
- **The owner's existing manual wallet account.** An owner who followed M7 §5.12 has a manual wallet account holding the wallet's address and BTC snapshots. Connecting the wallet flags the address (§5.3); the owner moves the address to the new account and closes the manual one, or both count. This is not automated, because closing an account is the owner's decision.
- **The same coins in two connections** (an Electrum key and an address list that includes its addresses): the second connection's addresses are already registered, so it is flagged; the duplicate fingerprint only catches the same descriptors.
- **Binance batches two of the owner's withdrawals into one transaction**: two receipts, two pairs (§5.8).
- **A withdrawal Binance marks complete before the transaction confirms**: In transit until the wallet posts the receipt.
- **A Binance row whose hash arrives later** (processing): no `HASH` edge until a later Binance sync stores it; heal pairs then.
- **Block times out of order** between consecutive blocks: each movement keeps its block's time; nothing assumes they increase.
- **A reorganisation deeper than 6 blocks** is not rechecked; the balance check (§5.6) would show it.
- **BTC held before January 2020** has no price on those days (§5.7).
- **Lightning in use.** Channel funding is a payment to an unknown address and closing is a receipt; the channel's balance in between is not counted. The connection form says so; an owner who uses Lightning can record the channel balance as a manual account.
- **A server returning a history without `prevout`, or without `fee`**: refused as a malformed response (fail closed), the sync fails, nothing is posted from it.

## 7. Left for later

- **EVM wallets**: their own milestone. It maps EVM networks to chains in §5.8's columns, adds token instruments by contract address and the spam-hiding rule (parent §6.3), and reuses `HASH`.
- **The Electrum protocol and Tor**, for owners whose own server speaks only the Electrum protocol.
- **Wallet labels** (BIP 329 export) as descriptions.
- **Lightning**, if a node or wallet exposes a read-only balance.
- **Thinning check snapshots** (one per day), shared with M7's TODO.

## 8. Changes to the parent spec

- §7.7: Phase 1 wallets are Bitcoin only (owner, 2026-11-27); EVM chains move to a later milestone of their own. A wallet's balance is its transactions, checked against reported balances. Chain data comes from an Esplora server (default mempool.space, configurable).
- §13: M8 is "Bitcoin wallets"; a new later milestone "EVM wallets" follows M10, unnumbered until scheduled.
- §6.3: the transaction's chain and hash are now columns (§5.8), with the output address.
- §14: the acceptance bullet "wallets the balances shown on block explorers" holds for Bitcoin; "withdraw crypto from Binance to your own wallet" is accepted with BTC.

## 9. Left to the builder

- Table, column and index names; the shared pricer's home; the client's structure within §5.2's pacing.
- The detection rules for private material (§5.1) beyond the listed forms, erring towards refusing.
- The wording of descriptions, warnings and help text; the UI layout.
- Which descriptor library: embit (MIT, pure Python) is the expected one. embit derives the BIP 86 Taproot test vector correctly (checked 2026-11-27), so Taproot descriptors are supported.
- The Esplora paging form (`/txs/chain/:last_seen_txid`) was not exercised by the probe (no address had 25 transactions); it is used as documented, and the history-count check of §5.2 guards it (decided during implementation, 2026-11-27).

## 10. Answers from the owner (2026-11-27)

- **Scope.** Bitcoin only; EVM later as its own milestone.
- **The owner's wallet.** One Electrum wallet, only for holding bitcoin; wallet type bip32, script type p2wpkh.
- **Balance.** Transactions are the balance, reported balances check it (§4.1 B).
- **Chain data.** A public server by default, configurable.
- **Designer's calls the owner reviews with this document**: one ledger transaction per movement (§4.4); posting only confirmed transactions (§5.5); syncing at most every 6 hours (§5.2); the registry winning over the hash (§5.8); prices from Binance's bars with no price before 2020 (§5.7); a new `chain_native` instrument scheme (§5.4); the master public key stored encrypted (§5.1).
- **Prices (2026-11-27).** The wallet's BTC and Binance's BTC share one price from Binance's public market data; the owner asked whether the wallet should use another platform's price and was told one instrument has one price.
- **Probe.** The owner saved the master public key to the gitignored probe folder and the read-only probe ran on 2026-11-27.

## 11. Verification

- **Derivation** against published vectors: BIP 32, 44, 49, 84, 86, SLIP-0132 prefixes, Electrum's own test vectors for old seeds, multisig and 2FA, and descriptor checksums. Electrum's test file has only 2-of-2 multisig cases built from public keys, so an Electrum multisig of *m* < *n*, and a descriptor's unsorted `multi` key order, are derived as the descriptor library does but not checked against Electrum's vectors (decided during implementation, 2026-11-27).
- **Refusals**: every private form of §5.1 is refused, and a test proves the input appears in no response, log record or raw record.
- **Posting**, with synthetic Esplora responses built from the documented format: receipt, spend with change, spend to own addresses, shared transaction, address-less output, coinbase, unconfirmed then confirmed, confirmed then dropped. A property test: for any transaction, the movements posted sum to the wallet's outputs less its inputs, and the posted balance equals the reported sums.
- **Pairing**, in the engine's property tests: `HASH` pairs exactly the records sharing chain, hash, address and opposite equal amounts, regardless of the window; ties go to review; no record pairs twice.
- **Pacing**: a 429 is waited out; a sync never sends more than one request a second.
- **Prices**: BTC held only in a wallet, with no Binance connection, is priced from the first day held.
- **After deploy, by the owner**: connect the Electrum wallet with its master public key; the preview address matches Electrum's first receiving address; the balance equals Electrum's and mempool.space's; the Binance BTC withdrawal shows as one transfer, with Binance's fee separate; no reconciliation difference; net worth unchanged by the transfer except the fee.
