# M7 design: Binance

- Date: 2026-11-22 (brainstormed with the owner 2026-11-22 to 2026-11-23)
- Status: approved by the owner 2026-11-23; plan `docs/superpowers/plans/2026-11-22-binance.md`
- Builds on: `main` at `68e0bec` (M0–M5 and the transfer work through PR #31 merged; migrations up to `0029`). M7 does not need M6 (PR #32): it works before or after it, and uses two of M6's transfer rules where they exist (§5.12). Migrations are numbered after whatever is on `main` when implementation starts: after M6's `0030`–`0036`, Phase A's are `0037`–`0040`.
- Parent spec: `docs/superpowers/specs/2026-11-11-financial-gate-phase1-design.md` (§6.3, §7.4, §8, §9, §11.2, §13 M7 and M8, §14)
- Related:
  - the M5 IBKR design (`2026-11-18-ibkr-flex-design.md`), whose structure, typed-table rule, fail-closed parsing and reconciliation this follows;
  - the M6 design (`2026-11-22-etoro-trading212-design.md`, on PR #32's branch), whose read-only gate and queries-only client this mirrors;
  - the M4 transfer-matching design (`2026-11-15-transfer-matching-design.md`), whose engine pairs Binance's deposits and withdrawals;
  - the endpoint catalogue (`2026-11-22-binance-endpoint-fields.md`), which lists every endpoint M7 reads with its parameters, limits and response fields, and where a real response differed from Binance's documentation.

## 1. Intent

The owner's crypto on Binance should count in net worth as exactly as their bank balance does, and every movement on Binance should be explained by a record the ledger holds. The Binance part of net worth must equal what Binance itself reports, in every wallet. Money moving between a bank, Binance and a self-custody wallet must never dip or double-count.

M7 connects Binance through its REST API with a key that can only read. Binance's reported balances hold each wallet's balance; Binance's records explain how the balance got there; the two are compared on every sync.

Lararium is open source, so the connector covers Binance as completely as its read API allows, and not only the products the owner's own account uses (owner, 2026-11-23).

### What the owner's account shows (read-only probe, 2026-11-23; no values recorded here)

The probe sent only queries, with a key that had only "Enable Reading", and printed field names, value shapes, counts, Binance's own enum values and yes/no checks.

- **A reading-only key reads everything.** Every balance endpoint answered, the two futures hosts included. The key was an HMAC key; `apiRestrictions` returned exactly the documented fields, with only `enableReading` true.
- **Two wallets hold something:** Spot, and Earn with one flexible position that subscribes automatically. Funding, both futures wallets, locked Earn, staking, on-chain yields, dual investment and loans are empty. No margin account exists: the margin account endpoints answer with `created: false`, and every margin history with `-3003`.
- **Records that exist:** an on-chain deposit, on-chain withdrawals, a convert, spot trades in three symbols, and several hundred each of asset dividends, flexible subscriptions, redemptions and rewards. Fiat orders, card payments, P2P, Pay, wallet-to-wallet transfers, dust, staking, loans, margin and futures income returned nothing.
- **History reaches the account's first record** (about 13 months) for deposits, withdrawals, convert, dividends, flexible Earn and trades; no retention limit was hit. Wallet-to-wallet transfers older than 6 months are refused.
- **Records explain the Spot balance exactly.** Over the 30 daily Spot snapshots, every day's change equals the signed sum of that day's records. Over the whole history, three of four Spot assets add up exactly to the reported balance.
- **One inflow is in no endpoint.** The fourth asset is short by its first inflow: a P2P purchase about 13 months ago (owner, 2026-11-23), older than the P2P history's 6-month lookback.
- **How flexible Earn pays.** A `BONUS` reward is paid into Spot as an asset dividend of the same asset and amount, and an `AUTO` subscription of that amount follows a few hours later. A `REALTIME` reward accrues inside the position, with no subscription. All subscriptions − redemptions + `REALTIME` rewards equal the position, to within the reward still accruing that day.
- **LD-prefixed Spot assets are not the position.** `/api/v3/account` lists an `LD<asset>` balance; its amount and its daily change are well below the position's and the subscriptions'. The Earn wallet's line in the wallet-balance detail equals the flexible position.

## 2. Goals, non-goals, anti-goals

### Goals

1. One Binance connection per Binance account, with a key that can only read; a key that can do anything else is refused.
2. Every Binance wallet is a ledger account whose balance is what Binance reports: Spot, Funding, Earn, Cross Margin, Isolated Margin, USDⓈ-M Futures, COIN-M Futures, Options, Trading Bots and Copy Trading, plus crypto-loan debt.
3. Every record type the read API documents is fetched, stored with one typed column per field, and posted to the ledger where it moves a balance.
4. On every sync, the change between two reported balances is compared with the records in between; a difference is reported, kept until explained, and never corrected silently.
5. History from the account's first record, as far as each endpoint reaches; what the API cannot explain becomes one opening adjustment per asset.
6. Deposits and withdrawals carry the transaction hash, network and addresses as typed columns, so that M8 can pair them with self-custody wallets by hash. Withdrawal fees are separate postings.
7. A daily price for every asset held, so valuation never shows 0 for an unpriced asset.
8. Anything the connector cannot handle is reported by name: an unknown field, record type, enum value, wallet or permission.

### Non-goals

- **Trading, transferring, subscribing or withdrawing.** The client only sends queries.
- **Pairing by transaction hash.** M8 does it, when wallets are connected (parent §13). M7 stores what M8 needs.
- **Sub-account consolidation.** A sub-account with its own key is its own connection; transfers between a master and its sub-accounts are not paired.
- **Portfolio Margin records.** A Portfolio Margin account's balance is read and reported as not fully supported (§5.13).
- **Open orders, order books, tax lots and realised-gain reports.** Trades keep their cost; lot matching is later work (parent §2.2).
- **Auto-Invest plan history.** Binance documents no endpoint; its buys appear as trades or converts where the API lists them.

### Anti-goals

- A sync that can change anything on Binance.
- A wallet's balance that differs from what Binance reports because a record was missing or posted twice.
- An asset counted twice because two endpoints both list it (LD-prefixed mirrors; a reward and its payout).
- A key or a secret anywhere but the encrypted credential store; an address or a transaction hash in a log line or a notification. (The identifier registry audits a registered address as it audits a bank account number.)
- An unpriced asset valued at 0, or a stablecoin assumed to be worth 1.

## 3. Constraints

### 3.1 Binance facts (checked against Binance's OpenAPI schemas and the probe, 2026-11-22/23)

- **Hosts.** `api.binance.com` (Spot `/api`, wallet and products `/sapi`), `fapi.binance.com` (USDⓈ-M futures), `dapi.binance.com` (COIN-M futures), `eapi.binance.com` (options), `papi.binance.com` (Portfolio Margin).
- **Signing.** `timestamp` in milliseconds, `recvWindow` up to 60,000, the signature last in the query, the key in `X-MBX-APIKEY`. The payload is percent-encoded before signing (since 2026-01-15; documented for Spot, Wallet and Margin). HMAC-SHA256 keys ("system generated") can still be created, are the only key type documented on every host, and worked on the three hosts probed. Binance recommends Ed25519 ("self-generated") and documents it for `api.binance.com` only.
- **Permissions.** `GET /sapi/v1/account/apiRestrictions` returns one boolean per permission. Binance's key page offers permissions the endpoint did not list (Alpha withdrawals, prediction trading), so the list is not closed.
- **Limits.** Per IP: 6,000 weight a minute on `/api`, 12,000 on `/sapi`; per account (UID): 180,000 a minute on `/sapi`. Used weight comes back in `x-mbx-used-weight-1m`, `x-sapi-used-ip-weight-1m` and `x-sapi-used-uid-weight-1m`. A 429 carries `Retry-After`; a 418 is an IP ban. Heavy endpoints: fiat orders 45,000 (UID), withdrawal history 18,000 (UID), rebate 12,000 (UID), account snapshot 2,400 (IP).
- **Two read endpoints are POST**: `POST /sapi/v1/asset/get-funding-asset` and `POST /sapi/v3/asset/getUserAsset`. Both are queries.
- **Balances.** `GET /sapi/v1/asset/wallet/balance` lists ten wallets; with `needBalanceDetail=true` each wallet lists its assets. The product endpoints give more detail per wallet (catalogue).
- **Daily account snapshot** (`/sapi/v1/accountSnapshot`): the last 30 days, taken at 23:59:59 UTC; Spot, Margin and Futures only.
- **Windows and lookbacks** (catalogue, per endpoint): deposits and withdrawals 90-day windows; convert, Simple Earn and margin transfers 30 days; dividends and loans 180 days; staking 89 days. Documented overall lookbacks: wallet-to-wallet transfers and P2P 6 months, Pay 18 months, margin interest, borrow/repay and capital flow 90 days, USDⓈ-M income 3 months.
- **Trades need a symbol.** `/api/v3/myTrades` answers for one symbol at a time; there is no "all my trades". A symbol removed from `exchangeInfo` cannot be asked for.
- **Times.** Epoch milliseconds, except withdrawal `applyTime` and `completeTime`, which are `YYYY-MM-DD HH:MM:SS` text in UTC.
- **Transaction ids.** An on-chain `txId` is `0x` + 64 hex digits or 64 bare hex digits. A transfer inside Binance has `transferType` 1 and a `txId` beginning "Off-chain transfer" or "Internal transfer".
- **GBP.** Every GBP spot pair is halted; the last GBP bar is from December 2023. EUR pairs trade (`EURUSDT`).
- **No endpoint** for Auto-Invest history, Trading Bots or Copy Trading records.

### 3.2 Price-source facts (checked 2026-11-22)

- **Binance klines** (`GET /api/v3/klines`, no key): daily bars back to each pair's listing; the close is at 23:59:59 UTC.
- **CoinGecko Demo plan**: an API key, 100 calls a minute, 10,000 a month, history limited to the past 365 days. `/coins/{id}/history` gives the 00:00 UTC price of a date. `/exchanges/binance/tickers` maps Binance's base assets to CoinGecko ids; there is no official symbol map. Its terms require attribution and discourage storing data (refresh within 24 hours, delete when access ends), with no exception for personal use.
- **ECB reference rates** (through Frankfurter, no key): one rate per currency per working day, from 1999.

### 3.3 What M7 builds on

- **Connectors (M3)**: `Connector` with `sync` and `check_access`, `run_sync`'s failure counting and `needs_reauth`, the 30-minute scheduler, encrypted credentials, raw records deduplicated by content hash, `upsert_connector_transaction`.
- **Ledger and valuation (M1)**: postings weighed by `cost_per_unit` or `price_per_unit` in any other instrument; `hybrid` balance mode (the latest snapshot plus postings after it; before an account's first snapshot, the sum of its postings); the latest price and FX rate on or before the day; `record_price` needs a fiat quote; a missing price is `missing_price`, never 0.
- **Transfers (M4, PRs #29 and #31)**: `transfer_contra`, In transit, the identifier registry, placeholders on manual hybrid accounts, `before_history` (per account), the owner pairing two records by hand, and the rules that a transfer leg is a transaction of exactly two postings and that the amount-and-date fallback needs one side identified and the other a `bank_transfer`.
- **Instruments**: kinds include `crypto`; identifier schemes include `coingecko` and `chain_contract`.

### 3.4 Project rules

- Every field a source returns gets a typed column; the raw record does not count (owner, 2026-11-21).
- Decimal only; UUIDv7; audited writes through `CommandContext`; `core` never imports `modules/finance`.
- Tests use synthetic data only; the owner's values never enter the repository. Tests run on the remote Docker host.
- Engine invariants are pinned with Hypothesis property tests.

## 4. Approaches considered

### 4.1 How Binance enters the ledger (owner chose the combination, 2026-11-23)

- **A. Reported balances plus deposits and withdrawals.** Each wallet's balance is its latest report; only deposits and withdrawals are posted. Cheap, and the balance is always right, but nothing says where a change came from.
- **B. Records are the truth, reports only check them** (M5's way). Every record is posted and the balance is their sum. It explains everything, but any record Binance does not list (an old P2P order, a transfer older than 6 months, Auto-Invest) leaves the balance wrong until someone notices.
- **C. Reports only.** Rejected: a `snapshots` account cannot carry identifiers, so withdrawals could not pair with wallets (parent §14).
- **Chosen: A's balance with B's records.** Wallet accounts are `hybrid`. The latest report is the balance; every record is posted and moves the balance between two reports; the two are compared on every sync. A missing record cannot make a balance wrong, and it cannot go unnoticed either.

### 4.2 Scope (owner chose everything, 2026-11-23)

- **Only what the owner's account uses** (Spot, flexible Earn), with a detector for anything else. Recommended first, because the other products have no real data to check against.
- **Chosen: every wallet and record type the API documents.** The project is open source and other accounts use other products. The cost is that most of the mapping is built from Binance's documented shapes alone; §10 lists what is verified and what is not, and §4.1's choice keeps an unverified rule from corrupting a balance.

### 4.3 Where a wallet's balance is read

- **From each product's own endpoint, summed.** Detailed, but the products overlap (the LD mirror, tokens such as WBETH that live in Spot), so sums double-count.
- **Chosen: Binance's own grouping.** The wallet-balance detail decides which wallet holds which asset; the product endpoints supply detail and are stored typed. The exceptions are wallets whose value is not a plain holding (margin, futures, options), read from their own account endpoints (§5.6).

### 4.4 Price source (designer's call, approved by the owner 2026-11-23)

- **CoinGecko first** (the parent spec's wording). Needs a key, reaches back 365 days on the free plan (the owner's history is longer), and its terms discourage keeping the data.
- **Chosen: Binance's own daily bars first, CoinGecko for what Binance cannot price.** No key, full history, and the price is the market the asset is actually held on. A fiat value needs one more step, through `EURUSDT` and the ECB's rates (§5.10).

## 5. Design

### 5.1 The connection

- **Connector `binance`.** Capabilities: balances, holdings, transactions.
- **Credentials.** `api_key`, and either `api_secret` (HMAC) or `private_key` with an optional `private_key_passphrase` (Ed25519, PEM). Config: `key_type`, the account `uid`, and `account_map` (wallet → finance account id).
- **Creating a connection.** The owner enters a display name and the key material. The first request is the gate below, then `/api/v3/account` for the `uid`. A second connection with a `uid` already connected is refused (409). The connection is `healthy` and a sync is queued.
- **Read-only gate.** Every sync starts with `apiRestrictions`. Every boolean in the answer that is true must be `enableReading` or `ipRestrict`; any other true value, known or not, refuses the sync (`CredentialRejectedError`: "this key can do more than read: <field>"). Nothing else is called. `enableReading` false is refused the same way.
- **Queries only.** The HTTP client holds a fixed list of (method, host, path) it may call, GET except the two POST queries of §3.1; anything else raises before a request is made. Redirects are off, so the key never follows one.
- **A rejected key** (HTTP 401, or codes `-2014`, `-2015`, `-1022`) raises `CredentialRejectedError`: `needs_reauth`, and the owner is told with Binance's code. Updating the key returns the connection to `healthy` and queues a sync.
- **No expiry reminder.** Binance does not report one for a reading-only key.
- **Ed25519 off `api.binance.com` is unverified** (§10): Binance documents it for that host only. If a futures or options host refuses the signature, the sync says so by name, recommends an HMAC key, and the rest proceeds.

### 5.2 Fetching and pacing

**Order of a sync:**
1. The gate (§5.1), then server time (`/api/v3/time`) for the clock offset.
2. Balances of every wallet (§5.6), each request with its own cutoff.
3. Records, each endpoint from its own cursor (below).
4. Any balance read that a record's time falls into is repeated (§5.6).
5. Prices (§5.10), then normalising, opening adjustments (§5.8), reconciliation (§5.11) and `heal_transfers`.

**When a sync is due.** When the last successful one is at least 4 hours old. "Sync now" always runs: the owner's request is carried to the sync, which the connector framework does not do today (it queues the same job as the scheduler), so M7 adds that. Monzo and IBKR ignore it and behave as before.

**Cursors.** One per endpoint (and per symbol for trades): `fetched_to`. A later sync asks from `fetched_to − 7 days`, in windows of the endpoint's maximum span; rows repeated by the overlap are written once (§5.3). The 7 days catch a status that changes later (a deposit confirming, a withdrawal failing).

**First sync.** Each history endpoint is walked backwards, newest window first, in its maximum windows, to the earlier of July 2017 (Binance's launch) and its documented lookback. A refused window older than one that was answered means the endpoint's history ends there: it ends that endpoint's walk and is not an error. Walking backwards is what makes that safe, since everything newer is already stored. An empty window never ends a walk. The daily Spot, Margin and Futures snapshots of the last 30 days are fetched once. The first sync is resumable: cursors are saved per endpoint, so an interrupted backfill continues where it stopped.

**Trades, which need a symbol:**
- **First sync:** every symbol in `exchangeInfo`, halted ones included (about 3,750 requests, roughly 15 minutes at the `/api` limit).
- **Later syncs:** every symbol both of whose assets the account has ever shown (in a balance or a record), from each symbol's last trade id.
- **On an unexplained change** (§5.11) of an asset in Spot or a margin wallet: every symbol with that asset on either side is asked once for the interval, then the check runs again. A round trip through an asset never seen before is found this way.
- The same applies to margin trades (`/sapi/v1/margin/myTrades`) when a margin account exists, and to futures trades for the symbols the income history names.

**Pacing.** The client tracks weight per limit (IP per host, UID per endpoint), stays under 80% of each, and reads the used-weight headers. On 429 it waits `Retry-After` and retries; on 418 the sync stops and fails. Three failed syncs in a row notify the owner (M3's rule).

**A product the account does not have** (the margin account's `-3003`, an unsupported endpoint's `-30105`, a 404) is recorded once as "not present" and skipped, not failed. The list of such codes is fixed; any other error fails the sync.

### 5.3 Storage

- **Raw records.** Each returned row is a raw record, type `binance.<record>` (for example `binance.deposit`, `binance.spot_trade`, `binance.earn_flexible_reward`), with the request's parameters. Raw records dedupe by content hash, so an unchanged row repeated by overlapping windows is stored once.
- **Typed tables.** One `finance.binance_<record>` table per record type, one column per response field (catalogue), linked to its ledger transaction where it has one. Reported positions and account details have their own tables, one row per report.
- **Fail closed, flag by name.** A field the catalogue does not list, or an enum value it does not list, is flagged once by name (never by value) and the row is stored; a row with an unknown type or status that decides its posting is stored and not posted, and reconciliation shows its effect. A missing required field fails the sync, naming the endpoint and field.
- **Keys.** Ledger rows are keyed by `(connection, external_id)`: `<record>:<id>`, with Binance's own id (`deposit:<id>`, `withdrawal:<id>`, `convert:<orderId>`, `spot_trade:<symbol>:<id>`, `dividend:<id>`, `earn_flexible_subscription:<purchaseId>`, …). A record with no id (a flexible reward) is keyed by its fields plus its occurrence number among rows identical in every field; identical rows share a time, so every window that covers it returns all of them and the number is stable. The occurrence number is part of the raw record's content, because raw records are unique by content. The catalogue names each record's key.
- **A changed row** (a status change) updates its typed row and its transaction in place.

### 5.4 Accounts

**One finance account per Binance wallet**, created the first time the wallet shows a non-zero balance or a record:

| Account | Type | Balance mode |
|---|---|---|
| Spot, Funding, Earn, Cross Margin, Isolated Margin, USDⓈ-M Futures, COIN-M Futures, Options, Trading Bots, Copy Trading | `asset` / `exchange` | `hybrid` |
| Crypto Loans (the debt) | `liability` / `other` | `hybrid` |

- Named `Binance <wallet>`, which the owner may rename; source: the connection, institution entity "Binance".
- A margin wallet holds signed quantities: a borrowed asset is negative. Isolated Margin is one account; its pairs are detail in the typed table.
- Fiat held on Binance (EUR, for example) is in the Spot account, in the existing fiat instrument.

### 5.5 Instruments

- **A new identifier scheme `binance_asset`** (upper case): Binance's asset code.
- **Finding an instrument.** By `binance_asset`; else by the `coingecko` id when the asset maps to one (§5.10); else, when exactly one existing `crypto` instrument has the asset's symbol, that one, which gains the `binance_asset` identifier; else a new one. So a coin the owner already holds in a manual wallet account, or later in an M8 wallet, is one instrument. Several existing instruments with the symbol are not guessed between: a new one is created and flagged for the owner to merge.
- **Creating one.** Kind `crypto`, symbol and name from `capital/config/getall`, 8 decimals. An asset that Binance marks as legal money (`isLegalMoney`) is the existing fiat instrument of that ISO code. When Lararium has no instrument of that code, the asset is not counted and is flagged by name: no instrument is made (a currency's decimals are not guessed), its balances get no snapshot, and its records are stored typed and not posted.
- **LD-prefixed assets are never instruments.** They are dropped where they appear (§5.6).
- **Tokens that stand for a staked asset** (WBETH, BNSOL) and yield tokens (BFUSD, RWUSD) are ordinary instruments with their own prices.
- **Futures and options contracts are not instruments.** A position is detail in a typed table; its value reaches net worth through the wallet's balance (§5.6).

### 5.6 Reported balances

Each sync writes one `reported` snapshot per account and asset.

- **Each balance request has its own cutoff.** A snapshot's `as_of` is the moment (Binance's clock) just before the request its balance came from; the moment just after the response is stored with it.
- **A read a movement falls into is repeated.** A balance read while a movement was happening may or may not contain it, and the `hybrid` rule would then count it twice or not at all. So after the records are fetched, a read with any record's time between its two moments (widened by 2 seconds, for times Binance gives to the second) is made again, up to three times. A wallet whose read is still unclear gets no snapshot in that sync; its previous one and the records after it stand.
- A transfer between two wallets read one after the other needs nothing more: it is after the first wallet's cutoff and before the second's.

| Wallet | Balance per asset | Checked on real data |
|---|---|---|
| Spot | `/api/v3/account`: `free + locked`, LD-prefixed assets dropped | yes: equals `getUserAsset` and the wallet detail |
| Funding | `get-funding-asset`: `free + locked + freeze + withdrawing` | shape only (empty) |
| Earn | wallet-balance detail, wallet "Earn" | yes: equals the flexible position |
| Cross Margin | `/sapi/v1/margin/account`: `netAsset` | no |
| Isolated Margin | `/sapi/v1/margin/isolated/account`: base and quote `netAsset`, summed per asset | no |
| USDⓈ-M, COIN-M Futures | the account's `assets[]`: wallet balance + unrealised profit | shape only (zero) |
| Options | the options margin account (`/eapi/v1/marginAccount`): equity per asset | no |
| Trading Bots, Copy Trading | wallet-balance detail | no |
| Crypto Loans | − `totalDebt` per loan coin, from the ongoing loan orders | no |

- **An asset that disappears** from a wallet gets one snapshot of 0, so the old one stops counting.
- **Zero balances are not written** for assets the account never held.
- **The wallet list is checked.** A wallet name the connector does not know, with a non-zero balance, is flagged by name and gets an account of its own, so its value still counts.
- **Cross-check.** Zero or non-zero per wallet in the wallet-balance list must agree with what the table's source says; a disagreement is flagged.
- **Product detail** (flexible and locked positions, staking, on-chain yields, dual investment, discount buy, loans, futures and options positions, isolated pairs) is stored typed with every sync and not posted.
- **The 30 daily Spot snapshots** fetched on the first sync are written as `reported` snapshots at 23:59:59 UTC of their day (LD-prefixed assets dropped). Margin and Futures daily snapshots likewise, where they exist.

### 5.7 Records in the ledger

Every record is one connector transaction on the wallet's account. Fees are separate postings (`is_fee`) whose counterpart is one Unassigned posting per asset, as in M5. A swap of one asset for another is two postings on the account, the received one weighed by its cost in the given one. Rounding follows M5 §5.7.

**Three rules that apply to every product:**

1. **A movement between two Binance wallets is one transaction with two postings**, − on one account and + on the other, with no In transit: one record describes both sides.
2. **A movement two endpoints both list is posted once.** For a wallet-to-wallet movement the first of these that lists it posts it: the wallet-to-wallet transfer history, the margin transfer history, a futures income row of type `TRANSFER`. The others are linked to that transaction (same asset, amount and wallets, times within 60 seconds) and not posted.
3. **A payout is posted where it lands.** A reward row with an asset dividend of the same asset and amount within 26 hours is linked to the dividend and not posted; the dividend posts on Spot. A reward row with no such dividend accrues inside the product and posts on the product's account.

| Record | Postings | Checked |
|---|---|---|
| Deposit, credited (`status` 1 or 6) | + `amount` on Spot or Funding (`walletType`); contra from `transfer_contra` (§5.12) | yes |
| Withdrawal, not cancelled, rejected or failed | two transactions: − `amount` on the wallet with the contra from `transfer_contra` (a transfer leg has exactly two postings); and `withdrawal:<id>:fee`, − `transactionFee` (`is_fee`) against Unassigned | yes (completed ones) |
| Spot trade | buyer: + `qty` of the base with cost `price` in the quote, − `quoteQty`; seller: the reverse with `price_per_unit`; − `commission` of `commissionAsset` (`is_fee`) | yes |
| Convert | − `fromAmount`, + `toAmount` with its cost, on the wallet `walletType` names | yes |
| Asset dividend | + `amount` on Spot against Unassigned | yes |
| Flexible Earn subscription | − `amount` on `sourceAccount` (Spot or Funding), + on Earn | yes |
| Flexible Earn redemption | − `amount` on Earn, + on `destAccount` | yes |
| Flexible Earn reward | rule 3: `BONUS` is its dividend; `REALTIME` is + `rewards` on Earn against Unassigned | yes |
| Dust conversion | per detail: − `amount` of the small asset, + `transferedAmount` of BNB with its cost, − `serviceChargeAmount` (`is_fee`) | no |
| Wallet-to-wallet transfer | rule 1, the two wallets from `type` | no |
| Locked Earn, on-chain yields: subscription, redemption, reward | as flexible; rewards by rule 3 | no |
| BFUSD, RWUSD subscription and redemption | a swap of the asset for the token on the wallet that holds it; rewards by rule 3 | no |
| ETH and SOL staking: stake, redeem, wrap, unwrap | a swap between the asset and its token; rate rewards change no quantity and are typed only; boost rewards by rule 3 | no |
| Dual investment, discount buy | subscription moves the invested asset to Earn; settlement returns the settled asset and amount | no |
| Crypto loan borrow, repay | borrow: + loan coin on Spot, − on Crypto Loans; repay: the reverse; interest − on Crypto Loans (`is_fee`) | no |
| Margin trade | as a spot trade, on the margin account | no |
| Margin interest, liquidation fee | − on the margin account (`is_fee`) | no |
| Margin borrow, repay | typed only: the net quantity does not change | no |
| Margin capital flow | typed only: it repeats the rows above | no |
| Futures income (every `incomeType` but `TRANSFER`) | ± `income` of `asset` on the futures account against Unassigned; commissions and funding paid are `is_fee` | no |
| Futures and options trades, positions | typed only: income and bill rows carry the money | no |
| Options bill | ± amount on Options against Unassigned; transfers by rule 2 | no |
| Fiat deposit or withdrawal (`fiat/orders`) | ± `amount` of the fiat on Spot, contra from `transfer_contra` with `bank_transfer = true`; `totalFee` typed | no |
| Card purchase or sale (`fiat/payments`) | two transactions: the fiat arriving (or leaving), and the swap of fiat for crypto with `totalFee` as a fee; paid from the cash balance, only the swap | no |
| P2P order, completed | as a card purchase, on Funding: the fiat leg and the swap at `unitPrice` | no |
| Pay transaction | ± each `fundsDetail` amount on the wallet it names, against Unassigned | no |
| Rebate | + `amount` on Spot against Unassigned, unless rule 3 links it to a dividend | no |

- **A pending record** is stored typed and posted when Binance counts it in the balance (a credited deposit; a withdrawal from the moment it is applied).
- **A record that fails after it was posted** is not voided, because the balance really fell and rose again. A withdrawal that is cancelled, rejected or fails stops being a transfer leg (its contra moves to Unassigned), and a refund transaction (`withdrawal:<id>:refund`: + `amount` and + `transactionFee` against Unassigned) is dated `completeTime`, or the report time of the sync that first saw the status when Binance gives none.
- **Realised profit is never a posting.** A posting weighed by its price balances; Binance's own profit figures are typed fields.
- **"No" in the last column** means the rule follows Binance's documented shape and no real record has tested it. Because the wallet's balance comes from Binance's report (§4.1), a wrong rule shows as a reconciliation difference and cannot make the balance wrong.

### 5.8 Opening balances and history before the first sync

- Before an account's first reported snapshot, its balance is the sum of its postings (the `hybrid` rule), so history comes from the records.
- **Opening adjustment.** For each account and asset: the earliest reported balance minus the sum of all records up to it. When that is not zero, one "Opening balance adjustment" is posted against Opening balances, dated the day before the account's earliest record. For the owner this is the P2P purchase the API no longer lists.
- **It is derived, not stored as a decision.** Every normalising run sets it afresh from the records and the earliest report, so regenerating from raw records gives the same ledger, and a corrected posting rule or a late record from before the earliest report moves it. It is posted only once the first sync's backfill has finished.
- What the records and reports disagree about after the earliest report is a reconciliation difference (§5.11), never part of the opening.
- A wallet with no record at all starts at its first reported snapshot.

### 5.9 Time (pinned)

- **`occurred_at`**: the record's own time in UTC; `occurred_tz` is UTC. A deposit: `completeTime`, else `insertTime`. A withdrawal: `applyTime`, the moment the amount leaves the balance (the probe's exact reconciliation used it). The catalogue names each record's time field.
- **`booking_date`**: the date of `occurred_at` in UTC. Binance's day, its daily snapshot and its daily bars are all UTC, so a day's records, report and price belong together.
- **Reported snapshots**: `as_of` as in §5.6; `as_of_date` its UTC date.

### 5.10 Prices and FX rates

- **Price date.** A price dated D is the price at the end of D in UTC.
- **Binance bars (source `binance`).** For each asset held on a day, the daily close of its market, converted to EUR by Binance's own EUR market: the asset's EUR pair when it has one; else its USDT pair divided by `EURUSDT`; else through another quote asset that has one of those. The price is recorded in EUR. USDT, USDC and other stablecoins are priced the same way, never assumed. Fetched bars are stored typed (`finance.binance_klines`).
- **CoinGecko (source `coingecko`), optional.** Used only for an asset Binance cannot price, and only when the operator has set a CoinGecko key (an environment variable; the app works without it). With a key set, the id of every Binance asset the account holds (priced by Binance or not) comes from `/exchanges/binance/tickers` and is stored as the instrument's `coingecko` identifier, which is also what joins it to the same coin elsewhere (§5.5); an asset with no id there is never given one by symbol. The price is the 00:00 UTC value of D + 1, quoted in the reporting currency when CoinGecko supports it, else in USD. The UI credits CoinGecko where its prices show.
- **One source per asset and day.** A CoinGecko price is not written for a day that has a Binance one.
- **No price.** An asset neither source can price shows as `missing_price` and is named in a one-time flag; the owner can enter a price or a `coingecko` id by hand.
- **ECB reference rates (source `ecb`).** A daily job records the ECB's rates (through Frankfurter) with `record_fx_rate`, back to the earliest date any account needs. EUR prices then value in any reporting currency the ECB covers; another currency shows `missing_fx`. This is the FX source the parent spec planned (§9) and no milestone had built.
- **An ECB rate only fills a gap.** It is written for a currency pair and day only when no other source has a rate between those two currencies on that day, in either direction, and it is removed when one arrives. Valuation's lookup takes the latest rate of any source and breaks a same-day tie by write order, so otherwise an ECB rate could replace IBKR's and move an IBKR account away from IBKR's own NAV. The plan adds a test that an IBKR-valued account's value on every day is unchanged by the ECB rates; if the lookup's direct, inverse and via-EUR paths cannot guarantee it, the lookup gets a new version that prefers a non-ECB path on the same day.
- **Backfill.** Prices are fetched from each asset's first held day; later syncs add the days since the last one.

### 5.11 Reconciliation

- **The check.** For each account and asset, between two consecutive reported snapshots: the reported change against the sum of the account's non-void postings with `occurred_at` in between.
- **Futures.** The postings are compared with the change in wallet balance (a typed field), since the reported balance also carries unrealised profit.
- **Earn's accruing reward.** A flexible position grows all day with its real-time reward, while the record of a day's reward is dated at the day's end. Between two reports the position is therefore ahead of its records by the reward still accruing at the later report, or behind them by what had already accrued at the earlier one (seen on the first real sync, 2026-11-24). A difference in either direction no larger than the asset's largest `REALTIME` reward of the last two days is not a difference.
- **A difference** is stored (`finance.binance_reconciliation_differences`: account, asset, the two snapshot times, the reported change, the posted sum), named in the connection's warnings, and notified once. It is never corrected automatically.
- **Every interval a sync touched is checked again**: the newest one, and any earlier one in which the sync added, changed or removed a posting. So a difference closes when the record that explains it arrives, and a change to an old record that no longer fits its interval opens one.
- **Unexplained change, then a trade sweep** (§5.2): a Spot or margin difference first triggers the symbol sweep for that asset and interval; only what remains is a difference.
- **At the first sync** the difference at the earliest snapshot is the opening adjustment (§5.8), not a warning.

### 5.12 Transfers

- **A new identifier kind `chain_address`**: an address the owner's own account answers to. The value is the address as Binance gives it, lower-cased when it begins `0x`. The owner registers their wallet addresses against a wallet account (a manual `hybrid` one today, a connected one after M8); Binance's own deposit addresses are not registered.
- **On-chain withdrawal.** The counterparty identifier is `address`. To a registered address it is a transfer: In transit, with a placeholder on a manual wallet account (M4 §5.8), so net worth does not move by more than the fee. To an unregistered address it goes to Unassigned and the address appears in Suggestions.
- **On-chain deposit.** The counterparty identifier is `sourceAddress` when Binance gives one; otherwise the record is unidentified and goes to Unassigned until the owner pairs it.
- **For M8.** Each deposit and withdrawal row stores `tx_id` as given, `tx_hash` (the lower-cased hash, set only for on-chain rows: `transferType` 0 and not an "Off-chain transfer" or "Internal transfer" id), `network`, `address`, `address_tag`, `source_address` and `tx_key`. M8 pairs a wallet's transaction with the Binance row of the same network and hash. The M4 design's note that hash pairing belongs to M7 is superseded by the parent spec's §13 (M8).
- **A transfer inside Binance** (`transferType` 1) names another Binance user, not an address; it is unidentified.
- **Fiat by bank** (`fiat/orders`): Binance's row is an unidentified `bank_transfer`. It pairs by amount and date only when the bank's row is identified, so the owner registers the bank account Binance's fiat channel pays into or from (as their bank shows it) on the Spot account, as they do for IBKR. When the bank sent more than Binance credited, the amounts differ: with M6's settlement the owner pairs them and the fee is posted; without it they stay unpaired.
- **Card purchases.** A bank's card payment is not a `bank_transfer` and names no account, so it pairs only through M6's `card_merchant` kind, once that exists and the owner registers Binance's merchant.
- **P2P.** The bank payment goes to a stranger's account, different each time, so nothing can be registered. The owner can pair the bank's row and the order's fiat leg by hand.
- **Unpaired, nothing is lost.** The bank's outflow and Binance's fiat credit both sit on Unassigned and cancel there, and net worth is right throughout.
- **Between Binance wallets**: no transfer object; one transaction (§5.7 rule 1).
- **Only a transfer leg can pair.** Of the records the connector posts, only those named above as a transfer's leg (a deposit, and a withdrawal that has not ended) say so; everything else is marked as never a transfer, so a fee, a refund, a dividend or a reward can never be paired with a card payment or a bank row. A record type added later that is a leg (Phase B's fiat deposits and withdrawals, a card purchase's or a P2P order's fiat leg) must say so too, or a pairing the owner makes by hand is dissolved.
- **Before history.** `before_history` is decided per account (M4), so each Binance wallet account has its own start: its earliest non-opening record.

### 5.13 What is reported instead of handled

Each of these is flagged once, by name, in the connection's warnings and one notification:
- a permission field the gate does not know (it also refuses the key when true);
- a wallet name, endpoint field, record type, status or enum value the catalogue does not list;
- a symbol with trades that `exchangeInfo` no longer lists (its trades cannot be fetched);
- **Portfolio Margin enabled** (`account/info`): its balance is read from the Portfolio Margin account endpoint into one account, and its records are not posted;
- **Trading Bots or Copy Trading with a balance**: counted from the report; Binance has no record endpoint for them, so their changes show as reconciliation differences against transfers in and out;
- an asset no source can price;
- a fiat currency Lararium has no instrument for (it is not counted).

Not inspected for unknown fields: the envelope of a later page of a paged balance read (only the first page's is kept), and an answer replaced by a read made again.

### 5.14 Notifications

Through the existing notifier, never with the key, an address, a hash or an amount:
- the key rejected, or a key that can do more than read, naming the permission;
- three failed syncs in a row and the recovery;
- a new reconciliation difference: the account, the asset and the two times;
- the new flags of §5.13, together on one line;
- the first sync finished, with the number of records per type and the opening adjustments made.

### 5.15 API and UI

**API**, under `/api/v1`, owner-only:
- `POST /connections/binance` (`create_binance_connection`): display name, key type, key material → 201 `ConnectionOut`; 422 with the permission's name when the key can do more than read; 409 for a `uid` already connected;
- `PUT /connections/{id}/binance` (`update_binance_connection`): new key material → `ConnectionOut`; `needs_reauth`, `failing` or `disabled` → `healthy` (a new key is how a disabled connection comes back), and queues a sync;
- the existing list, sync-now and disable routes;
- open reconciliation differences and the flags of §5.13 are returned with each Binance connection in the existing connections list.

Audit arguments never include key material.

**Connections page:**
- an "Add Binance" form: key type, the key, the secret or private key (password fields, cleared after submit), and the steps to create a reading-only key;
- per Binance connection: the wallets found, the progress of the first sync, open reconciliation differences, and the flags of §5.13.

**Accounts**: registering a `chain_address` on a wallet account, next to the existing identifier kinds.

Holdings and net worth need no new page.

## 6. Edge cases

| Case | What happens |
|---|---|
| A record arrives after the snapshot that already contains it | Its `occurred_at` is before the snapshot's `as_of`, so the `hybrid` rule does not add it again. |
| A record occurs while the balances are being read | That wallet's balance is read again, so its snapshot is cut on one side of the record (§5.6). |
| Binance lists a record only later, with a time inside an old read | Known limit: the balance may be off by that record until the next report, and the two intervals around that read show equal and opposite differences, which stay open. The read's two moments are stored, so the cause is visible. The same holds for a trade fetched after the snapshots of its sync were cut: by the trade sweep, or in a symbol that only a read made again first showed (it is fetched by the next sync). |
| A fiat currency Lararium has no instrument for | Not counted: no snapshot, its records stored typed and not posted, flagged once by its code. The other side of a trade or convert in it is not posted either: after the account's earliest report it shows as a reconciliation difference; before that (most of a first sync's history) it is inside that asset's opening adjustment. |
| A trade whose amounts do not agree within the rounding tolerance | The sync stops, naming the record, on every sync until the rule is corrected in code. The record is stored, so it posts once the rule is right; Binance does not amend a trade, so nothing is fetched again. |
| A code that counted as a crypto instrument (before Binance marked it legal money, or through another connection) once Lararium has the currency | The asset is the currency from then on. At the next report the crypto instrument reads 0 and the currency the amount: two equal and opposite reconciliation differences, which stay open. Earlier postings stay on the crypto instrument until Lararium replays its records. |
| A withdrawal fails after being posted | The next sync within the 7-day overlap sees the status; the withdrawal stops being a transfer and a refund is posted at the time the amount came back (§5.7). |
| A deposit is credited but still locked (`status` 6) | Posted: Binance counts it in the balance. |
| An asset bought and sold between two syncs, never seen before | The quote asset's change is unexplained; the symbol sweep finds both trades. |
| A symbol was delisted | Its trades cannot be fetched; the change shows as a difference and the symbol is flagged if a record names it. |
| A transfer between wallets older than 6 months | Not listed; it is inside the two wallets' opening adjustments. |
| The owner's P2P purchase 13 months ago | The opening adjustment of that asset. |
| A reward and its dividend arrive in different syncs | The reward is posted on the product until the dividend arrives, then linked and its posting removed; the 7-day overlap covers it. |
| Two flexible rewards identical in every field | The occurrence number is in the raw content and the key, so both are kept. |
| A margin account that does not exist, a product not offered in the account's region | Recorded as not present; skipped. |
| A borrowed margin asset | A negative quantity; valued negative. |
| Unrealised futures profit | In the futures account's reported balance; reconciliation uses the wallet balance. |
| A stablecoin loses its peg | Its price comes from its market, so its value follows. |
| No bar for a day (a pair halted) | The latest earlier price is used, as valuation already does. |
| The reporting currency is not an ECB currency | `missing_fx`, never 0. |
| The key gains a permission later | The next sync's gate refuses it. |
| A 418 | The sync stops at once and fails; the next tick retries after `Retry-After`. |
| Two connections for one Binance account | Refused by `uid`. |
| The first sync is interrupted | It resumes from the saved cursors; opening adjustments are fixed only when it finishes. |

## 7. TODOs for earlier designs, and what is left for later

**Earlier designs that M7 shows could be better** (recorded, not changed in M7; owner, 2026-11-23):
- **M4 design, §2 and §6**: it says hash pairing belongs to M7; the parent spec says M8. Correct the M4 text.
- **Parent §9, valuation v1**: a price must be quoted in fiat, so a crypto-quoted market (USDT) has to be converted when it is recorded. Letting valuation chain through a non-fiat quote would keep the market's own price.
- **Parent §9, FX**: the ECB source was planned and never built; M5 values through IBKR's rates and M6 writes none. Once M7's `ecb` source exists, decide which source wins on a day that has several.
- **M5 and M6 reconciliation**: differences are warnings rebuilt on each run. M7 stores them with the interval they cover; the same table would serve IBKR, eToro and Trading 212, and the parent's "reconciliation differences" view.
- **M3 connector framework**: each connector has its own "when is a sync due" rule (M5's 06:00, M7's 4 hours) and its own pacing. A `due` hook and a shared weight-aware pacing helper would remove the copies.
- **M3, M6**: the read-only gate exists three times (eToro's scopes, Trading 212's, Binance's permissions). `check_access` could own it.
- **M1 snapshots**: a `hybrid` connector account writes a snapshot per asset per sync. Thinning old snapshots (keep one a day) needs a rule before the table grows large.
- **M5, typed tables**: one table per record type is many tables for Binance. A shared way to declare "raw record → typed columns" would cut the repetition for the next connector.
- **Foundation, currencies**: the foundation seeds only ten currencies. Seeding the full ISO 4217 list with each currency's decimals would let a connector count any fiat it meets; until then Binance leaves out and flags such an asset (owner, 2026-11-25).

**Left for later milestones:**
- **M8**: wallets, hash pairing with the rows of §5.12, CoinGecko prices by contract address.
- **M10**: the reconciliation-differences view over all connectors.
- **Later**: Portfolio Margin records; sub-account transfers; Auto-Invest history if Binance documents it; RSA keys; tax lots.

## 8. Left to the builder

- Table and column names, indexes and constraints, following the finance schema's conventions. Tables with identical field sets may be one table with a `product` column.
- The client's structure, the exact pacing numbers within §5.2's limits, and how the first sync commits as it goes.
- The wording of warnings, notifications and the key-creation steps; the UI layout.
- The fixed list of "not present" error codes, starting from the ones the probe saw.
- The plan lists every endpoint's fields with their homes, from the catalogue; where a real response differs from the catalogue, the response wins and the catalogue is corrected.

## 9. Answers from the owner (2026-11-22/23)

- **Scope.** Assume every wallet and every way in and out is used. After the probe showed only Spot and flexible Earn in use: still build everything now, because the project is open source and should support the platform as completely as it can.
- **Core value.** The most complete features and data. The combination of §4.1 was confirmed.
- **Probe.** The owner ran the read-only probe on their own account with a reading-only HMAC key.
- **Key permissions.** Every reading permission, no writing one: on Binance that is "Enable Reading" alone.
- **First inflow.** A P2P purchase of a stablecoin, about 13 months before the probe.
- **Earlier designs.** Improvements are recorded as TODOs (§7), not made in M7.
- **Approval (2026-11-23).** The design as written, including the designer's calls: Binance's bars before CoinGecko (§4.4), ECB rates that only fill gaps (§5.10), no dependence on M6 (§5.12), the withdrawal fee and a failed withdrawal's refund as their own transactions (§5.7), the derived opening adjustment (§5.8), instrument matching (§5.5) and the scope's edges (§2, §5.13).

## 10. Verification

**Tests**, with synthetic responses only (invented ids, addresses, hashes, assets and amounts, shaped like the catalogue):
- **Gate and client.** A key with any other permission, known or unknown, is refused after one request; the client refuses another host, another method and an unlisted path; both signers against known vectors; pacing under each limit; 429 and 418.
- **Parsing.** Every record type in the catalogue; an unknown field, an unknown enum value and a missing required field.
- **Fetching.** Windows walked newest first on the first sync, cursors, the 7-day overlap, a refused old window, empty old windows, a product that is not present, the symbol sets for trades, the sweep after an unexplained change, a first sync interrupted and resumed, and "Sync now" inside the four hours.
- **Ledger mapping.** Every row of §5.7's table balances; re-normalising writes nothing; the three rules (one transaction for two wallets, a movement listed twice, a reward and its dividend in either arrival order).
- **Balances.** LD-prefixed assets dropped; an asset that disappears; an unknown wallet; the opening adjustment; a movement during a balance read; a transfer between two wallets' reads.
- **Reconciliation.** A consistent history gives no difference; a missing record gives one difference once, closed when the record arrives; the accruing-reward allowance; futures on wallet balance.
- **Transfers.** A withdrawal to a registered address and the wallet's placeholder; the fee as its own transaction; a withdrawal that fails after posting; a fiat deposit paired with a bank row naming the registered account; an internal Binance transfer left unidentified.
- **Prices.** A direct EUR pair, a USDT pair, a two-step path, a stablecoin, a halted pair, an asset with no market and no CoinGecko key, the ECB rates filling only gaps, and an IBKR-valued account unchanged by them.
- **Property tests (Hypothesis).** For generated consistent account histories across several wallets, fetched in random overlapping windows and orders and with reports at random moments: every transaction balances; every account's balance equals the latest report plus the records after it; the reconciliation finds no difference; removing one record produces exactly one difference of that record's amount; regenerating from raw records gives the same ledger.

**What real data has verified, and what it has not:**
- **Verified on the owner's account:** the gate, HMAC signing on `api`, `fapi` and `dapi`; Spot and Earn balances; deposits, withdrawals, trades, convert, dividends and flexible Earn, including exact Spot reconciliation and the reward rule.
- **Built from Binance's documented shapes only:** Funding, margin, futures, options, locked Earn, staking, yield tokens, dual investment, discount buy, loans, dust, wallet-to-wallet transfers, fiat, cards, P2P, Pay, rebates, Portfolio Margin's balance, Ed25519 signing, and which wallet each of their records lands in. The first real record of each is checked against its reconciliation; a wrong rule is corrected then.

**The owner's check, not committed:**
- **Before building (plan Task 0).** Re-run the probe's summary offline for any shape the plan still needs; no new key is required.
- **At the end.** Each wallet's balance matches the Binance app; the valuation of Binance matches the app's estimate on a few days within market movement; the opening adjustment is the P2P purchase; no reconciliation difference is open; a withdrawal to a registered address shows as a transfer with its fee separate.
