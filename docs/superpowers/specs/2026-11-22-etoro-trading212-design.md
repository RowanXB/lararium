# M6 design: eToro and Trading 212

- Date: 2026-11-22 (brainstormed with the owner 2026-11-22 to 2026-11-23)
- Status: approved by the owner 2026-11-23; implemented in PR #32, pending the owner's check; plan `docs/superpowers/plans/2026-11-22-etoro-trading212.md`
- Builds on: `main` at `68e0bec` (M0–M5 and the transfer work through PR #31 merged); migrations up to `0029`. Migrations are numbered after whatever is on `main` when implementation starts.
- Parent spec: `docs/superpowers/specs/2026-11-11-financial-gate-phase1-design.md` (§3, §6.3, §7.6, §8.3, §13 M6, §14)
- Related:
  - the M5 IBKR design (`2026-11-18-ibkr-flex-design.md`), whose structure, typed-table rule and reconciliation this follows, and whose §7 lists what it left to M6;
  - the M4 transfer-matching design (`2026-11-15-transfer-matching-design.md`), which left "difference postings (FX, fees)" to the first connector that produces them;
  - the transfer-evidence design (`2026-11-21-transfer-evidence-design.md`) and the owner-pair design (`2026-11-21-owner-pair-records-design.md`), which M6's transfer changes extend.

## 1. Intent

The owner's money at eToro and Trading 212 should count in net worth as exactly as their Monzo and IBKR money does. Each account's value in Lararium should equal what the platform reports, and every trade should keep its cost, fees and exchange rate for the tax work that comes later (parent §2.2). Money moving between Monzo, eToro and Trading 212 must never dip or double-count, including when the two sides are in different currencies.

M6 connects both platforms through their own read-only APIs, and adds an upload of eToro's account statement for what eToro's API does not return. The owner asked for the backend first: every field each source gives is stored typed in the database; screens beyond what entering keys and uploading a file need come later.

### What the owner's data shows (read-only probes, 2026-11-22/23; no values recorded here)

The probes printed only field names, value shapes, counts and yes/no checks, never values.

**Trading 212** (two keys, one per account; every read-only permission ticked):
- An **Invest** account and a **Stocks ISA**, both GBP, both opened days ago.
- Each has exactly one transaction, a `DEPOSIT`; no orders, positions, dividends or pies yet.
- `totalValue` equals `cash.availableToTrade + cash.reservedForOrders + cash.inPies + investments.currentValue` on both.
- Transactions carry a UUID `reference`, a UTC `dateTime` with milliseconds, and no bank details.
- Responses carry `x-ratelimit-*` headers.
- **The two deposits were Apple Pay card payments from Monzo** (owner, 2026-11-23; confirmed in the deployed database, read-only): two Monzo Mastercard payments with an identical merchant (same Monzo merchant id, group and descriptor), equal amounts, 8½ minutes apart. Monzo files them under the category `savings`, not as transfers. The owner may also use bank transfers later.
- The owner will use Pies later.

**eToro** (Read key; a Write key was also tried and deleted, §3.1):
- Three accounts in the Balances API: **Trading** (USD), **Cash** (eToro Money, GBP), **MoneyFarm** (GBP, `subType: ISA`).
- Trading holds one manual, real (non-CFD), unleveraged position and has two closed trades; no copy trading.
- `balances` Trading `balance` equals `aggregate-portfolio` `accountTotalValue`, and PnL `credit` equals `accountAvailableCash`.
- Balance history returns daily snapshots of Cash and Trading (not MoneyFarm).
- eToro Money's cash transactions: bank payments in and out (with the other bank's IBAN or account number and a payment reference), and internal transfers to and from Trading, all GBP, all `settled`.
- `orders:lookup` works for the closed trades' `orderId`s and returns per-execution fees, spread, markup and conversion rate.
- Instrument metadata has no ISIN.
- **The account statement (xlsx)** has sheets Account Summary, Closed Positions, Account Activity, Dividends, Financial Summary and Glossary:
  - Account Activity lists every Trading-account event in date order: "Open Position", "Position closed", "Transfer GBP → USD", "Transfer USD → GBP", "Deposit Conversion Fee", "Withdrawal Conversion Fee", "Withdraw Request", "Withdraw Request Cancelled", "Withdraw Fee", "Withdraw Fee Cancelled". The two GBP → USD transfers and three USD → GBP transfers match, in number, the eToro Money side's two transfers out and three in.
  - A transfer row's `Amount` is USD; its `Details` is only the currency code ("GBP"). The GBP amount is on the eToro Money side (API).
  - A conversion-fee row has a negative `Amount` and a `Realized Equity Change` of 0: the fee is already netted in the transfer, and the row only states it. One of the two GBP → USD transfers has no fee row.
  - Closed Positions has the **ISIN**, FX rates at open and close, spread fees, market spread, profit in USD and GBP.
  - Dividends has withholding tax rate and amounts (no rows yet).
  - Rows sharing a timestamp are common (7 of 18), so running-balance order within one second is not given.
  - The PDF of the same statement lacks many xlsx columns (Realized Equity Change, the FX rates, NWA, withholding-tax amounts, among others); the xlsx looks like a superset.

## 2. Goals, non-goals, anti-goals

### Goals

1. **Trading 212: one connection per account** (Invest, Stocks ISA), syncing positions, cash, orders, dividends, transactions and pies into a broker account each.
2. **eToro: one connection** syncing three accounts: eToro Trading (USD broker), eToro Money (GBP cash), MoneyFarm ISA (balance only).
3. **The eToro statement fills what the API lacks.** Uploaded to the eToro connection, it supplies the Trading account's cash events (transfers, conversion fees, withdrawals, dividends, overnight fees, adjustments) and enriches what the API already recorded (ISIN, FX rates, spreads, GBP profit, withholding tax). Where both give a value that makes a posting and they disagree, both are kept and the owner is asked (owner, 2026-11-23; §5.7).
3a. **Trading 212's CSV export is imported too** (owner, 2026-11-23, by the rule that every format is imported unless another is its superset): uploaded to the account's Trading 212 connection, it adds what the API lacks (the deposit's card "Transaction ID" in `Notes`, and whatever its trade rows carry) under the same precedence as the eToro statement (§5.3a). **It is optional** (owner, 2026-11-23): a connection that never receives one syncs, posts, reconciles and pairs in full from the API, and nothing prompts for it.
4. **Every field each source returns is stored typed**, API and statement alike (owner's rule of 2026-11-15, amended 2026-11-21: derivable fields too; only a field with a better home goes there instead). An unknown field or enum value is flagged by name, never by value.
5. **Records are the truth; reports check them** (M5's approach A). After every sync and upload, each account's positions and cash are checked against what the platform reports.
6. **Transfers pair, across currencies.** Monzo ↔ eToro Money and eToro Money ↔ eToro Trading pair automatically; the latter posts the conversion and its fee (parent §8.3). Monzo card deposits into Trading 212 pair automatically when unambiguous. The owner can pair two records of different currencies by hand.
7. **Read-only, enforced.** Neither connector can trade or move money, whatever the key allows (§5.1).
8. **Regeneration.** Ledger rows come from stored raw records through versioned, deterministic normalizers (parent §6.6).

### Non-goals

- **Web UI beyond four forms** (add Trading 212, add eToro, upload an eToro statement, upload a Trading 212 export). Cross-currency pairing, the "statement needed" prompt and review of conflicts are API and notifications only in M6 (owner, 2026-11-23).
- **CFDs, leverage, copy trading (mirrors), Smart Portfolios, eToro Options, Crypto wallet, Spaceship, Plaee.** The owner holds none. Their rows and balances are flagged and not posted (§5.6); reconciliation shows their effect.
- **MoneyFarm positions or transactions.** eToro's API gives its balance only.
- **Telling Trading 212 accounts apart for bank-transfer deposits by payment reference.** There is no such deposit yet (owner's rule: no features for hypothetical cases). It is reconsidered when the first one appears; until then the owner pairs it by hand (§5.12).
- **eToro's PDF statement.** The xlsx is a superset (§1); Task 0 confirms it.
- **Tax calculations** (out of Phase 1). **Runtime NAV alerts, stale-sync heartbeats** (M9).

### Anti-goals

- A connector holding a key that can trade, and syncing anyway.
- An account's balance in Lararium differing from the platform's report, and nobody being told.
- Re-syncing, re-uploading or uploading overlapping statements changing or duplicating a ledger row.
- The API and the statement both posting the same trade.
- Personal data (names, date of birth, IBANs, account numbers) reaching logs, warnings or notifications. It is stored typed, as M5 does.
- Net worth dipping while an eToro Money → Trading transfer waits for its statement.
- A new subsystem beyond what these cases need.

## 3. Constraints

### 3.1 eToro facts (checked against eToro's API portal 2026-11-22 and the owner's probes)

- **Auth.** Headers `x-api-key` (Public API Key), `x-user-key` (User Key) and `x-request-id` (a fresh UUID per request). Base URL `https://public-api.etoro.com`.
- **User-Agent.** eToro's Cloudflare refuses Python urllib's default User-Agent (error 1010, "browser_signature_banned"; seen in the probe). Every request sends `User-Agent: Lararium/<version>`.
- **Keys.** Created in eToro's settings with Environment (Real/Demo) and Permission (Read/Write); SMS verification; optional expiry and IP allow-list. **Scopes are not selectable.** `GET /api/v1/me` returns the key's `scopes`.
  - A Read key had 16 read scopes, including `trade.real:read`, `money.balance:read` and `money.cash-transactions:read`.
  - A Write key had the same plus 10 write scopes (`trade.real:write`, `crypto:write`, `sub-accounts:write`, …).
  - **Neither has `money.transfer:read`**, so `GET /api/v1/money/transfers/{id}` (the USD leg, exchange rate and fee of an internal transfer) returns 403 with either key. The USD side of eToro Money ↔ Trading transfers is therefore only in the statement.
- **Rate limits.** 60 requests per minute per user key in a shared pool, with separate 60/min pools for portfolio/PnL/instrument-breakdown and for aggregate-portfolio, and 120/min for market data. Headers `ratelimit-limit`, `ratelimit-remaining`, `ratelimit-reset`, `ratelimit-policy`; 429 with `Retry-After`.
- **Endpoints used** (all GET):
  - `/api/v1/me` (ids, scopes, personal fields);
  - `/api/v1/trading/info/real/pnl`, `/api/v1/trading/info/aggregate-portfolio`, `/api/v2/trading/info/instrument-breakdown` (open positions, cash, totals);
  - `/api/v1/trading/info/trade/history?minDate=` (closed trades; each request's look-back under one year; ends on a short page);
  - `/api/v2/trading/info/orders:lookup?orderId=` (execution detail);
  - `/api/v1/balances` and `/api/v1/balances/history` (current balances of every account; daily end-of-day snapshots for the last 12 months, at most 365 days per request);
  - `/api/v1/money/accounts/cash/{accountId}/transactions` (eToro Money; `pageToken` pagination, no date filter);
  - `/api/v1/data/positions/closed-events/history`, `/api/v1/data/stockback/payments`;
  - market data: `/api/v1/market-data/instruments`, `/api/v2/market-data/instruments`, instrument types, exchanges, closing prices, daily candles.
  - `/api/v1/sub-accounts/me/accounts` needs `sub-accounts:write`; a Read key gets 403. It is not called.
- **Numbers.** Trading amounts are JSON numbers; eToro Money amounts are decimal strings. All are parsed as `Decimal` from the raw text, never through float.
- **Time.** API timestamps are ISO 8601, mostly UTC `Z`, with 0–7 fractional digits, some without a zone (`instrument-breakdown` `openTime`). The statement's `dd/mm/yyyy HH:MM:SS` carries no zone (§5.8).
- **Currency.** The Trading account's currency is read from `accountCurrency`/`accountCurrencyId`, not assumed (it is USD for the owner). Asset currency is an ISO code on aggregate-portfolio and instrument-breakdown; there is no id-to-code endpoint.

### 3.2 Trading 212 facts (checked against the OpenAPI bundle of 2026-10-17, current on 2026-11-22, and the owner's probes)

- **Auth.** HTTP Basic, API key as user and secret as password. Base URL `https://live.trading212.com/api/v0`. **One key pair per account**; only Invest and Stocks ISA are supported.
- **Permissions** are ticked per key. The connector needs `account`, `portfolio`, `metadata`, `history:orders`, `history:dividends`, `history:transactions`, and uses `orders:read` and `pies:read` when present. There is no API to read a key's permissions; a missing one returns 403 naming the scope.
- **Endpoints used** (all GET): `/equity/account/summary` (1 per 5 s), `/equity/positions` (1 per s), `/equity/history/orders`, `/equity/history/dividends`, `/equity/history/transactions` (20 per minute each; `limit` ≤ 50; follow `nextPagePath` until null), `/equity/metadata/instruments` (1 per 50 s), `/equity/metadata/exchanges` (1 per 30 s), `/equity/orders` (1 per 5 s), `/equity/pies` and `/equity/pies/{id}` (deprecated by Trading 212 but working; 1 per 30 s and 1 per 5 s).
- **Pagination quirks** reported by users: `nextPagePath` sometimes a full path, sometimes a query string; 404s deep in transactions. The probe saw only single pages.
- **Currency.** Everything in the account's primary currency (GBP), except `fill.price`, `averagePricePaid` and `currentPrice` (instrument currency). Currency-conversion fees, stamp duty and the like arrive in `fill.walletImpact.taxes[]`.
- **Not documented:** the sign of `transactions.amount` for `WITHDRAW`, `FEE` and `TRANSFER`, of `taxes[].quantity` and of `walletImpact.netValue`; how `netValue` relates to price, quantity, `fxRate` and taxes; how a transfer between Invest and ISA appears; history depth; key expiry. Enums grow without notice.

### 3.3 What M6 builds on

- **The connector framework (M3)**: connections, encrypted credentials (`store_credential`, AAD `connection_id:purpose`), raw records (deduplicated by content hash), `SyncContext`, `run_sync` (failure counting, `needs_reauth` on `CredentialRejectedError`, healing after commit), `schedule_syncs` every 30 minutes, `upsert_connector_transaction` keyed by `(connection, external_id)`.
- **M5's patterns**: a parser of our own that types every field and flags unknown ones; documents in `core.documents`; an upload route on the connection (`upload_ibkr_statement`) that ingests a file like a fetched statement; reconciliation against `reported` snapshots, replacing the connection's reconciliation warnings in full each run; `listing()` for instruments (by own id, then ISIN/FIGI in the same quote currency); IBKR's typed tables.
- **One source per account** (parent §6.3): "The same account may not receive entries from two sources at the same time." So the eToro statement is uploaded to the eToro connection and normalised by it; the generic import (`POST /imports`), which refuses connection-sourced accounts, is not used.
- **Instruments**: schemes include `etoro_id` already; there is no Trading 212 scheme. `(scheme, value)` is unique, so a second listing of one ISIN does not get the ISIN identifier (M5 §5.6).
- **Transfer matching (M4 and later)**: a leg is exactly its own posting and the contra (`LEG_POSTINGS = 2`); `transfer_contra` sends a record naming another own account to In transit; tiers DECIDED, MUTUAL, ONE_SIDED, WEAK all require **the same instrument and opposite equal amounts**; other amounts only open review (`amount_differs`); window −1 to +5 booking days. Identifier kinds are `uk_bank_account` and `ibkr_account`, `UNIQUE (kind, value)`. The owner can pair two records (`POST /transactions/{id}/pair`), same instrument and amount only.
- **Valuation (M1)**: `transactions`, `snapshots` and `hybrid` balance modes; latest price and FX rate on or before the day.

### 3.4 Project rules

As M5 §3.3: `Decimal`/`NUMERIC` and no floats; UUIDv7; audited writes through `command_context`; `core` finance-agnostic; domain checks before database rules, 4xx never 500; English docs and UI copy; signed commits; synthetic test data only (no real names, amounts, ids or dates in the repo, tests or PR text); tests on the remote Docker host; parsers fail closed; Hypothesis property tests pin the engine's invariants; root-cause fixes, no mechanisms for hypothetical cases.

## 4. Approaches considered

### 4.1 Scope

Both platforms in one milestone (parent §13). They share the new transfer work (currencies that differ), the instrument merge by ISIN, and the patterns of M5; each connector is otherwise independent. **Builder's call:** the plan may ship them as more than one PR (for example Trading 212, then eToro, then the transfer changes), each green on its own.

### 4.2 Where the eToro Trading account's records come from (owner chose A, 2026-11-23)

The API returns no cash history for the Trading account: no deposits, withdrawals, conversions, fees or dividends (§3.1). The statement does.

1. **A. Trades from the API, cash events from the statement (chosen).** Opens and closes post from the API within 30 minutes. The statement posts what the API lacks, and its trade rows are matched to the API's by Position ID and only enrich them (§5.7). Cost: two sources for one account, so merge and conflict rules.
2. **B. The statement is the Trading account's only source.** One source, no merging. But every trade leaves the positions unreconciled until the owner downloads a statement, so the "statement needed" prompt fires after every trade.
3. **C. API only; derive cash events from the daily balance snapshots.** Rejected: it breaks "records are the truth"; a fee could not be told from a conversion.

Within the parent's rule, the account still has **one source, the eToro connection**: the statement is an input to that connection, as IBKR's uploaded closed-account statement is.

### 4.3 How eToro's accounts are modelled (owner chose separate accounts, 2026-11-23)

1. **Separate accounts (chosen)**: Trading, eToro Money and MoneyFarm each a finance account, as eToro reports them (separate ids, currencies and balances). Reconciliation then points at the account that differs; eToro Money (an IBAN, a card) stays apart from invested money.
2. **One account holding GBP, USD and positions**: a GBP → USD transfer would be a conversion inside one account. Rejected: the cross-currency pairing is needed anyway (Trading → a bank account in GBP, §5.12), and cash and investments would mix.

### 4.4 Cadence of statement uploads (owner, 2026-11-23: "as you recommend")

On prompt, not on a schedule: Lararium notifies "upload an eToro statement" when the Trading account's cash differs from eToro's report and the difference can be explained by records the statement will bring (§5.11).

### 4.5 Parsing

- **eToro xlsx: our own reader on the standard library** (`zipfile` and the XML parser), refusing any document type declaration, as M5 refuses one in Flex XML. **Builder's call:** no new dependency for six sheets of cells; a library would also convert values that must be typed from the cell text.
- **JSON: parsed with `Decimal` for numbers** (`json.loads(..., parse_float=Decimal, parse_int=...)`), keeping the raw text in raw records.

## 5. Design

### 5.1 Connections and keys

**Trading 212** (connector `trading212`; capabilities balances, holdings, transactions):
- One connection per account. Credentials: purpose `api_key` and `api_secret`. Config: `account_kind` (`invest` or `isa`, chosen by the owner) and the account map.
- On creation, the first request is `account/summary`; its `id` and `currency` identify the account. A second connection whose `id` is already connected is refused (409).
- A 401 raises `CredentialRejectedError` (`needs_reauth`, notify). A 403 on a required scope fails the sync naming the permission to tick; on `orders:read` or `pies:read` it skips that part with a one-time flag.

**eToro** (connector `etoro`):
- One connection. Credentials: purpose `public_api_key` and `user_key`. Config: the account map (eToro account id → finance account id), and `realCid`/`gcid` from `/me`.
- **Read-only gate.** Every sync starts with `/me`. If any scope does not end in `:read` (an allow-list: eToro's `money:transfer` shows not every write-capable scope is named `:write`), the sync refuses (`CredentialRejectedError`, "this key can trade or move money; create a Read key"). Nothing else is called.
- A 401/403 on `/me` raises `CredentialRejectedError`.

**Both connectors** send only GET, to their own host, with redirects off (so credentials can't follow a redirect), and the User-Agent of §3.1. The HTTP client refuses any other method; this is the enforcement of goal 7, whatever a key allows.

**Updating** a connection's keys returns a `needs_reauth` connection to `healthy` and queues a sync, as IBKR's does. Neither platform reports key expiry, so there is no expiry reminder.

### 5.2 Fetching and pacing

**When a sync is due.** Every 30-minute tick. A sync fetches only what is new (below), so ticks are cheap.

**Trading 212, per sync:**
1. `account/summary`, `positions`, and `orders` (pending) when permitted.
2. `history/orders`, `history/dividends`, `history/transactions`, newest first, following `nextPagePath` (accepting a full path or a query string), until a page holds only rows already stored, or the end. The first sync reads to the end.
3. Every 24 hours, and on the first sync: `metadata/instruments` and `metadata/exchanges`, and `pies` plus each `pies/{id}` when permitted.
4. Requests keep to each endpoint's limit and honour `x-ratelimit-reset` on 429.
5. **Full re-walk.** Once a week the history endpoints are walked to the end again, so a row a pagination bug skipped is found. A row seen once and later missing is not deleted (§6).

**eToro, per sync:**
1. `/me` (read-only gate, §5.1).
2. `balances` (all account types, `includeZeroBalances`), PnL, aggregate-portfolio, v2 instrument-breakdown.
3. eToro Money transactions for each Cash account, newest first, until a page holds only stored ids.
4. Trade history from the latest stored close date minus 7 days (overlap for late rows); the first sync goes back in windows of 364 days to the account's opening (the statement's Date Created when uploaded, else until a window returns nothing).
5. `orders:lookup` once for each `orderId` not yet looked up (open positions and closed trades).
6. Balance history since the latest stored snapshot minus 7 days; the first sync takes 365 days.
7. Once a day: closed-events, stockback, market-data metadata for held instruments, and daily candles for held instruments (prices, §5.10).
8. Pacing within eToro's pools; 429 honours `Retry-After`.

**Errors.** Retryable errors (5xx, timeouts, 429 after retries) fail the sync as an ordinary failure; the owner is told after 3 in a row (M3). A response that does not match the expected shape (a required field missing) fails the sync naming the endpoint and field.

### 5.3 The eToro statement upload

- `POST /connections/{id}/etoro-statement`: an xlsx file, at most 16 MiB. 409 until the connection's first sync has finished (so the accounts and the API's records exist). 202, then ingested and normalised in the request's command, like `upload_ibkr_statement`.
- The file is stored as a document (`source = "finance.etoro"`). Each row of each data sheet becomes a raw record (`etoro.statement.account_summary`, `.closed_position`, `.account_activity`, `.dividend`, `.financial_summary`), with the row's cells exactly as text and the statement's period (Account Summary's Start Date and End Date). The Glossary is static text and stays only in the document.
- **The statement must be the owner's Trading account.** Account Summary's username and currency must match the connection (`/me` `username`; the Trading account's currency); otherwise 422, naming neither.
- **Sheets and columns** are checked against the plan's table. A sheet or column not in it, with values, is flagged by name. A required column missing fails the upload naming the sheet and column.
- **Overlapping statements** write each row once. A statement row has no id (except Position ID on trade rows), so its key is: date-time, Type, Details, Amount, Units, Position ID, plus its occurrence number among identical rows of that statement. The same row in two overlapping statements has the same key. **Builder-check call to verify in Task 0:** that eToro lists the same event identically in two statements covering it.
- A statement's period is recorded; reconciliation uses it (§5.11).

### 5.3a The Trading 212 CSV upload

- **Optional.** Nothing depends on it: the API alone posts every record, reconciliation compares against the API's reports only, and no warning or notification asks for an export. An upload only adds its typed fields and any row the API skipped.
- What the audit showed (deposits only so far): columns `Action`, `Time (UTC)`, `Notes`, `ID`, `Total`, `Currency (Total)`. `ID` equals the API's transaction `reference`, and `Time (UTC)` equals its `dateTime`. `Notes` carries "Transaction ID: <id>", which the API does not return. Trade rows' columns are unknown until the owner's first trade.
- `POST /connections/{id}/trading212-export`: a CSV file, at most 16 MiB; 409 until the connection's first sync has finished; 202, then ingested and normalised like the eToro statement. The owner downloads it from the app (History → export); Lararium never calls `POST /history/exports`, which is a write that notifies the owner's phone.
- The file is a document (`source = "finance.trading212"`); each row is a raw record `trading212.export_row` with its cells as text.
- **Columns.** Required: `Action`, `Time (UTC)`, `ID`, `Total`, `Currency (Total)`. Every other column is typed by Decision 3's rule once known; a column not yet known, with a value, is flagged by name and kept in the raw record. `Notes` is stored as text, and "Transaction ID: <id>" also as a typed `notes_transaction_id`.
- **Keys.** A row whose `ID` is an API transaction's `reference` belongs to that record (`transaction:<ID>`); the row only enriches it. A row of another `Action` (a trade, a dividend, interest) is matched to the API's record by the rule the first real rows show (the plan's Task 0 records it); until then such rows are stored typed, flagged, and not posted, because the API posts those events.
- **Precedence** is the eToro statement's (§5.7): the API's values make the postings; a CSV row the API lacks (a skipped page) posts from the CSV; a disagreement in a posting value is a review item. The CSV must belong to the connection's account: its `ID`s that the API knows must be on that account, else 422.

### 5.4 Storage

- **Raw records**: every API response row and statement row (§5.3), record types `etoro.*` and `trading212.*`. Request metadata: the endpoint and query (never a key).
- **Typed tables**, keyed by what they describe (the plan lists every field and its home):

| Table | Keyed by | Holds |
|---|---|---|
| `finance.trading212_accounts` | account | account id, kind, currency, and every `account/summary` field per sync day (as a reported snapshot's detail) |
| `finance.trading212_instruments` | instrument | ticker, name, short name, type, currency, ISIN, working schedule, exchange, extended hours, max open quantity, added on |
| `finance.trading212_orders` | transaction | order id, fill id, created/filled at, type, side, status, strategy, quantity/value fields, limit/stop prices, time in force, initiated from, extended hours, trading method, fill type, fx rate, net value, realised P/L |
| `finance.trading212_order_taxes` | transaction, row | name, quantity, currency, charged at |
| `finance.trading212_dividends` | transaction | reference, type, amount, amount in euro, gross per share, ticker currency, quantity, paid on |
| `finance.trading212_transactions` | transaction | reference, type, amount, currency, date-time |
| `finance.trading212_positions` | reported snapshot | quantity, available for trading, in pies, average price, current price, created at, wallet impact (cost, value, P/L, FX impact) |
| `finance.trading212_pies` | account, pie, day | pie id, cash, progress, status, results, dividend details, settings (name, goal, dates, initial investment, dividend action, shares), each instrument's owned quantity and current and expected share |
| `finance.etoro_users` | connection | `/me`: ids (gcid, real/demo CID), username, names, gender, date of birth, language, player level, avatar URL, scopes |
| `finance.etoro_accounts` | account | eToro account id, type, sub-type, currency, and every Balances field per sync (as reported snapshots' detail) |
| `finance.etoro_instruments` | instrument | instrument id, display name, symbol, type id, exchange id, industry id, price source, flags, images; ISIN and statement names once a statement brings them |
| `finance.etoro_positions` | reported snapshot | every PnL, aggregate and instrument-breakdown position field (rates, units, amounts, conversion rates, fees, taxes, SL/TP, settlement, P/L) |
| `finance.etoro_trades` | transaction | every trade-history field; the `orders:lookup` order and execution fields; every Closed Positions column |
| `finance.etoro_cash_transactions` | transaction | every eToro Money field: type, subtype, direction, status, posted at, counterparty name and type, bank identifiers, payment reference, card details, transfer id, original amount and currency, conversion rate |
| `finance.etoro_activity` | transaction | every Account Activity column of the statement row behind the record, and for an enrichment-only row (§5.7) the record it enriches |
| `finance.etoro_dividends` | transaction | every Dividends column |
| `finance.etoro_statements` | document | period, Account Summary and Financial Summary figures, Date Created |
| `finance.etoro_balance_history` | account, date | every snapshot field |
| `finance.etoro_closed_events`, `finance.etoro_stockback` | connection, row | every field |

- **Generic broker tables (M5's open question): not made.** The two platforms share few fields under different meanings; each gets its own tables, as IBKR has.
- **Trading 212's CSV export** is stored in `finance.trading212_export_rows` (keyed by connection and `ID`, plus the record it enriches), one column per known column (§5.3a).

### 5.5 Accounts

Each connector creates its accounts on first sight, `asset`, balance mode `transactions` unless stated, source the connection, named as below (the owner may rename):

| Account | Type | Named | Tax wrapper |
|---|---|---|---|
| Trading 212 Invest | `broker` | `Trading 212 Invest ···<last 4 of id>` | `gia` |
| Trading 212 Stocks ISA | `broker` | `Trading 212 ISA ···<last 4>` | `isa` |
| eToro Trading | `broker` | `eToro Trading` | `gia` |
| eToro Money | `bank` | `eToro Money <currency>` (one per Cash account) | `none` |
| MoneyFarm ISA | `broker`, balance mode **`snapshots`** | `MoneyFarm ISA` | `isa` |

- **Registry.** The connectors register their own accounts' platform ids: a new identifier kind `etoro_account` (value: eToro account id) on eToro Trading, eToro Money and MoneyFarm (a snapshots account carries none; MoneyFarm's id stays in `etoro_accounts`); Trading 212's account id under a new kind `trading212_account`. As Monzo and IBKR do, a connector never takes a row the owner assigned elsewhere.
- **The eToro Money account's own bank details** (its IBAN or sort code and account number) are not in the API. The owner registers them as `uk_bank_account` on eToro Money once (README step), as for IBKR's receiving account; Suggestions offers them after the first Monzo payment.
- **Opening balances.** Where the earliest records do not reach the account's opening, one opening adjustment per instrument is posted against Opening balances the day before the earliest record, as M5 §5.7 does:
  - eToro Trading: none. **The first statement uploaded must start on or before the account's Date Created** (Account Summary), so the statement and the API together cover the account's whole life; an earlier start than that is refused with 422 naming the date it must start by (builder-check call: otherwise API trades before the statement's start would move cash on top of an opening that already contains them, and eToro's Realized Equity includes invested money, so it is not cash). Later statements may cover any period;
  - eToro Money: from the earliest balance-history snapshot, when its transactions don't reach back;
  - Trading 212: from the earliest `account/summary` only if the history is short of it (history depth is undocumented).

### 5.6 Instruments

- **Lookup**, as M5's `listing()`: by the platform id (`etoro_id`; a new scheme `trading212_ticker`), then by ISIN with the same quote currency, so one listing held at IBKR, eToro and Trading 212 is one instrument. A hit lacking the platform id gets it (`add_identifiers`).
- **Trading 212**: ISIN, name and currency come with every history and position row; metadata adds type and exchange. Kind: `STOCK` → `equity`, `ETF` → `etf`; any other type is flagged and its rows not posted. Trading 212 renames tickers; an instrument is found by ISIN first when the ticker is new (§6).
- **eToro**: the API has no ISIN, so an instrument is created by `etoro_id` with symbol, display name and the asset currency from aggregate-portfolio. **When a statement brings the ISIN** (Closed Positions; the Activity's Position ID links it to the instrument), it is added; if another instrument already holds that ISIN in the same currency, the two are merged per M5's rule: the existing instrument gains `etoro_id`, and every row referencing the eToro one (postings, prices, reported snapshots, identifiers, typed tables) moves to it before the eToro one is deleted. **Builder-check call:** merging is a one-way, audited operation the plan specifies; until a statement brings the ISIN, an eToro instrument stands alone.
- Kind from eToro's instrument type (5 stocks → `equity`, 6 ETF → `etf`); any other type, and any position that is not `settlementType: real` with leverage 1 and no mirror, is flagged and not posted.
- `decimals`: Trading 212 quantities to 8 places (users report 8-dp fractional shares); eToro units to 6 (the API's fractional examples). Task 0 confirms against real values.

### 5.7 Records in the ledger

Every record is one connector transaction. Fees and taxes are separate postings on the account (`is_fee`), countered on Unassigned per currency, as M5 does. Rounding follows M5 §5.7: a residual within half a unit of the price's last decimal place times the quantity is one more Unassigned posting (memo "rounding"); beyond that the sync fails naming the record.

**Trading 212:**
- **Order fill, `type = TRADE`** (key `order:<fill id>`): ± quantity of the instrument with `cost_per_unit` (buy) or `price_per_unit` (sell) in GBP, being |`netValue` − taxes| / quantity as Task 0 establishes; the GBP cash leg is `walletImpact.netValue`; each `taxes[]` entry a fee posting in its currency. `fill.price` (instrument currency), `fxRate` and realised P/L are typed fields. **Task 0 pins** the signs and whether `netValue` includes the taxes; the mapping is written once that is known, and the tests use synthetic rows shaped like the real ones.
- **Other fill types** (`STOCK_SPLIT`, `SPIN_OFF`, `FOP`, stock dividends, acquisitions): quantity to or from Unassigned, cash if any, as M5's corporate actions. `FOP` (a share transfer in) goes through `transfer_contra`.
- **Dividend** (key `dividend:<reference>`): `amount` in GBP against Unassigned.
- **Transaction** (key `transaction:<reference>`): `DEPOSIT`/`WITHDRAW` through `transfer_contra` (§5.12); `FEE`, `INTEREST_ON_FREE_CASH`, `LENDING_INTEREST` against Unassigned; `TRANSFER` through `transfer_contra`. The sign is taken from `amount` when signed, else from the type, as Task 0 establishes. An unknown type is flagged and not posted.
- **Cancelled or rejected orders** (no fill) are stored typed and post nothing.

**eToro Trading:**
- **Open** (key `open:<positionId>`; from the API, or from the statement's "Open Position" row if the API has none): + units of the instrument with `cost_per_unit` in **USD** (the account currency: `amount` / `units`), and − `amount` USD. The open rate in asset currency, the conversion rate, spreads, markup and fees from `orders:lookup` are typed fields. eToro charges no separate opening fee to the cash for real stocks. Execution `fees` and `taxes` are typed, not posted (ruling R30, after Task 11): the statement's rows are the only source that posts SDRT, overnight fees and dividends; until a statement covers them, the missing cash is the "upload a statement" prompt (§5.11).
- **Close** (key `close:<positionId>`; trade history, or the statement's "Position closed"): − units with `price_per_unit` in USD, + (`investment` + `netProfit`) USD. Trade-history `fees` are typed, not posted (amended in Task 11, ruling R30: by PnL's `totalFees` they are the position's overnight fees and dividends, which the statement posts as rows of their own, so posting them on the close would count them twice; `netProfit` is taken to leave them out, as Closed Positions' Profit(USD) does, and a statement that says otherwise opens a review on the cash or profit). Closed Positions' columns are typed fields.
- **One key per event, a fixed precedence** (builder-check call, so regeneration and upload order give the same ledger, parent §6.6):
  - The event posts once, under its key, from whichever sources are stored. **When the API has the event, its values make the postings**; the statement's only enrich (`etoro_activity`, `etoro_trades` columns). When only the statement has it, the statement's values post. A statement arriving before the API therefore posts first, and the API's later arrival re-posts the same transaction in place if its values differ (`upsert_connector_transaction`).
  - **A disagreement** is a difference, between the two sources, in a value that makes a posting or its date: units, cash amount, profit, or the booking date after the time-zone rule (§5.8). It is stored as a review item on the transaction, both values kept, and notified once. Other shared fields (names, rates, spreads) are kept from both sources without review, so rounding cannot flood it. Only two descriptions of the same thing are compared (ruling R39): an open described as the position as opened (PnL, the "Open Position" row) and one described as the part a partial close closed (trade history, Closed Positions) are different facts, so while the sources are at different stages no review is opened or closed and the API's values post; they are compared once both describe the same part.
  - **Resolving** a review item is an owner decision, stored keyed by the event key and the field, as M4 stores its decisions, and replayed by regeneration: "keep the API's" closes it; "take the statement's" makes that field's posting come from the statement. A later change in either source's value reopens it. An open item whose field both sources now state and agree on closes on its own (resolved `api`, by "sources agree"), with no notification and no decision recorded; one a source stops stating stays open. The profit makes no posting (a close posts its units and cash), so a "profit" resolution changes no posting: it settles the item.
- **Cash events, statement only** (key per §5.3):
  - "Transfer GBP → USD" / "Transfer USD → GBP": ± `Amount` USD, a transfer leg identified as eToro Money (§5.12);
  - "Deposit Conversion Fee", "Withdrawal Conversion Fee": no postings (the fee is netted in the transfer; `Realized Equity Change` is 0); typed, and used as the conversion's fee (§5.12);
  - "Withdraw Request" (− USD) and "Withdraw Request Cancelled" (+ USD): through `transfer_contra`, `bank_transfer` true (§5.12);
  - "Withdraw Fee" and "Withdraw Fee Cancelled": fee postings, against Unassigned;
  - dividends (with withholding tax from the Dividends sheet as a separate fee posting), overnight fees, SDRT, interest, refunds, credits, adjustments, stocks lending: against Unassigned;
  - any other Type is flagged and not posted.
- **The ledger's cash equals the statement's balance.** The cash a row moves is its `Realized Equity Change`, except open and close rows, whose cash is the `Amount` (− for an open, + for a close), and "Withdraw Fee Cancelled", which moves nothing (its fee was already refunded by a positive "Withdraw Fee" row; in the owner's data both `Balance` and `Realized Equity` stay unchanged on it while its `Realized Equity Change` is positive). Rows sharing one second are not listed in balance order, so the check is per second: the balance after the second's rows equals the balance before plus their cash. (Task 0, 2026-11-23: this holds on every second of the owner's statement.)

**eToro Money** (key `cash:<id>`): `amount` in its currency, debit negative. `bankTransfer` goes through `transfer_contra` with `bank_transfer` true and the counterparty's bank identifier (ruling R28, 2026-11-23): a GB IBAN, or a sort code and account number, is a `uk_bank_account`; an account number alone (the probe's `bankAccountNumber`) is account-number evidence (transfer-evidence design §5.1, item 2), stored as the transaction's `counterparty_account_number`, so the own account registered under that number is the one it names; an IBAN outside the UK names nothing and is flagged ("bank transfer to a non-UK account", never its value); `internalTransfer` through `transfer_contra` with the counterparty identified as the eToro account the transfer names (§5.12); card transactions against Unassigned (as Monzo's card payments). Only `settled` rows post; others are stored and post when settled.

**MoneyFarm ISA:** no records. Each sync records its balance as a `reported` snapshot (snapshots mode values it).

### 5.8 Time (pinned)

- **API timestamps.** `occurred_at` is the instant given (UTC when zoned). A timestamp without a zone (instrument-breakdown `openTime`) is not used for `occurred_at`; the same event's zoned time is.
- **Statement times are read as UTC.** Task 0 (2026-11-23) found every open and close at the same minute as the API's UTC time, but all of the owner's rows fall in GMT, when UTC and Europe/London agree. UTC is taken (the API's own clock); a statement row whose time disagrees with the API's for the same event, as a London clock would by an hour in summer, raises the review of §5.7, so a wrong guess shows up on the first summer trade.
- **`booking_date`**: the event's date in Europe/London, the owner's reporting zone, except eToro balance-history snapshots, whose `date` is used as given. **Task 0 pins** the cut-off of eToro's end-of-day snapshot by comparing it with the records of that day.
- **Reported snapshots**: `as_of` is the fetch time for current balances and positions; the end of `date` (UTC unless Task 0 shows otherwise) for balance history.

### 5.9 Typed fields

Every field goes to its ledger home when it has one (dates to the transaction; quantities, prices, cash and fees to postings; ISIN and platform ids to instrument identifiers; account ids to the registry; descriptions to the transaction; prices and FX rates to `finance.prices` and `finance.fx_rates`; reported positions and cash to `reported` snapshots). Every other field has a column in the tables of §5.4, derivable or not. Personal fields (`/me` names, date of birth; counterparties' names and bank identifiers) are stored typed and never logged.

### 5.10 Prices and FX rates

- **Trading 212**: each sync records each position's `currentPrice` (instrument currency) for the day (source `trading212`); each fill records its price on its day. There is no historical price endpoint; days before the connection with neither a fill nor another source's price show as `missing_price` (§7).
- **eToro**: daily candles back to each held instrument's first open date, and each sync's current rate, recorded as prices in asset currency (source `etoro`).
- A later write for the same date replaces an earlier one from the same source.
- **No FX rates are written by M6** (builder-check call). Valuation's `v1_fx_rate` takes the latest rate of any source, so a broker's execution rate (eToro's and Trading 212's include their conversion markup) would value every account's USD that day. The platforms' rates stay typed fields (`fxRate`, conversion rates, balance-history `exchangeRate`/`usdRate`). USD ↔ GBP for valuation keeps coming from the existing sources (IBKR's daily Conversion Rates for the owner). A day with no rate shows as `missing_fx`, as today.

### 5.11 Reconciliation

After every sync and every statement upload, per account, as M5 §5.11, except that a connection's reconciliation warnings are owned by record (ruling R35): the cursor keeps the texts it last published and those the owner was told, and exactly those are replaced in full, never warnings matched by their text; each new difference is notified once, counted only once a send succeeds; a run that cannot reconcile keeps the last check's warnings and adds "eToro Trading could not be reconciled"; nothing is corrected automatically. A day of balance history on or before the last day the latest statement covers in full is covered by it, so a difference on it is an ordinary one; that day is the UTC date of its end when the End Date's time is 23:59:59, else the day before (a statement downloaded on its end day ends mid-day, and the rest of that day is not covered; the next statement overlaps it, and its rows are written once by key):

- **Trading 212**: each instrument's quantity against `positions`; GBP cash against `availableToTrade + reservedForOrders + inPies`, at the sync's time.
- **eToro Trading**: each instrument's units against the open positions; USD cash against balance history's Trading `cash` on each snapshot day since the latest statement's start, and against PnL `credit` plus frozen cash now.
- **eToro Money**: each currency against its Balances `balance`.
- **MoneyFarm**: none (snapshots).
- **"Upload a statement" instead of a difference.** When eToro Trading's cash differs and eToro Money has internal transfers to or from Trading after the latest statement's end, or the difference appears after an Open/Close's day while the API shows fees or dividends on a position (`overnightFeesAndDividends`, `totalFees` changing), the warning reads "upload an eToro statement covering <date> onward" and is notified once: it names no end date (ruling R34), so its text changes only when a new statement moves its start (the first day the latest statement does not cover in full, or the account's earliest record before the first). Otherwise it is an ordinary difference.

### 5.12 Transfers

**Monzo ↔ eToro Money (GBP).** eToro Money's bank rows carry the other bank's account; Monzo's carry eToro Money's once the owner registers it (§5.5). Mutually identified, same amount: M4 matches them, no change.

**eToro Money ↔ eToro Trading (GBP ↔ USD): a new cross-currency pair.**
- Both legs are identified: the eToro Money row's counterparty is the Trading account (the connector sets the counterparty identifier to Trading's `etoro_account`), and the statement's transfer row is identified as eToro Money's. Task 0 verifies that the eToro Money row names the Trading account (and not MoneyFarm) in a way the connector can read (counterparty name or type).
- **Pairing rule (new, in the engine):** two legs on different own accounts, mutually identified, opposite directions, **different fiat instruments**, whose `occurred_at` are within **N minutes** of each other (the card-deposit window, §5.12), each the only such candidate for the other → matched. Amounts are not compared (the rate is unknown in advance), which is why both sides must be identified and unique. (Amended after Task 0, 2026-11-23: in the owner's data every eToro Money internal transfer has 2 or 3 same-direction candidates inside M4's day window, but exactly one statement transfer row at the same second; eToro stamps both sides with one clock.)
- **Until the statement arrives**, the eToro Money leg sits in In transit, so net worth does not dip; the "upload a statement" warning names it.
- **Settlement** (a derived transaction posted when matched, voided when unpaired), posted on In transit so that it nets to zero per currency:
  - **GBP → USD.** eToro Money sent G GBP; Trading was credited U USD; the conversion-fee row says F USD. Settlement: − G GBP, + (U + F) USD with `price_per_unit` = (U + F) / G, then − F USD (`is_fee`) against Unassigned.
  - **USD → GBP.** Trading sent U USD; eToro Money was credited G GBP; the fee row says F USD. Settlement: − (U − F) USD, + G GBP with `price_per_unit` = G / (U − F), and − F USD (`is_fee`) against Unassigned.
  - The fee is the statement's conversion-fee row of the same date-time (or the row right after the transfer row), or 0 when there is none. Task 0 confirms the fee is netted this way in both directions (the fee rows' Realized Equity Change is 0). The plan writes both directions out with worked numbers.
- **Where the fee comes from (generic):** a leg may carry a typed "conversion fee" (instrument, amount) that its connector supplies; the engine uses it in the settlement. eToro sets it from the conversion-fee row; other connectors leave it empty.

**Owner pairs records whose sides differ** (parent §13: "differ in currency or amount"). `POST /transactions/{id}/pair` accepts two fiat records, directions opposite, that differ:
- **in currency**: the settlement is as above with fee 0 (the difference is all exchange rate). This covers eToro Trading → a UK bank in GBP (a "Withdraw Request" whose payout arrives in GBP), which neither side identifies;
- **in amount, same currency**: the settlement posts the difference as a fee (`is_fee`) on the sending side's In transit leg against Unassigned. This covers a deposit that arrives net of a platform fee.
Automatic same-currency pairing still requires equal amounts (builder-check call: no real record yet arrives net of a fee; Trading 212 may charge one on card deposits beyond an allowance, and such a deposit goes to the owner until one is seen and its shape known).

**Monzo card deposits into Trading 212.**
- A new identifier kind `card_merchant` (value: the Monzo merchant id). **One merchant may name several own accounts** (Invest and ISA share Trading 212's merchant), unlike other kinds; the registry gains that for this kind only. The owner registers it once from Suggestions (offered when a Monzo card payment's merchant has a website on a connected platform's domain, `trading212.com`).
- A Monzo card payment to a registered merchant names the merchant's registry row, whose holders are "one of these accounts". **It stays on Unassigned until it is paired** (amended in implementation, Task 15): such evidence never identifies a record on its own, as weak and account-number evidence don't; it pairs only with a deposit that is there, and the matched transfer then moves both legs to In transit, to that one account. No one-sided "one of these accounts" transfer is opened, so the transfers table keeps one counterpart account. Card deposits arrive within the same second on both sides, so there is no real in-transit period to model; the cost is that net worth dips by the deposit between the Monzo sync and the Trading 212 sync (at most one sync interval), and an unpaired card payment does not show as awaiting. Until it is paired, the owner can pair it by hand (`POST /transactions/{id}/pair`). The registration is `POST /accounts/identifiers/card-merchants`; the suggestion is `GET /transfer-suggestions`'s `card_merchant` kind (API only: no screen, per the non-goals).
- **Pairing rule (new):** a Trading 212 `DEPOSIT` on one of those accounts, same amount, whose `dateTime` is within **N minutes** of the Monzo payment's time, being the only such candidate on both sides → matched. More than one candidate → `several_candidates`, for the owner. N is pinned in Task 0 from the owner's two deposits (8½ minutes apart, so N must be well under 4); the provisional value is 2.
- A refund (Trading 212 `WITHDRAW` back to the card) pairs the same way in reverse.

**Bank-transfer deposits into Trading 212** (none yet): unidentified on the Trading 212 side; M4's `bank_transfer` rule pairs them by amount and date when one candidate exists. With two accounts receiving the same amount close together, the owner pairs by hand. Payment-reference identification waits for a real deposit (non-goal).

**Trading 212 Invest ↔ ISA** (`TRANSFER`, none yet): both rows are from connected accounts; with neither naming the other, they open no transfer by themselves. The connector identifies a `TRANSFER` as the account's sibling when the owner has exactly two Trading 212 connections, so the pair is mutually identified and matched by M4 (same currency, same amount). Task 0 cannot verify it (no data); the real check after the first such transfer does (§10).

**eToro Money ↔ MoneyFarm.** MoneyFarm is a snapshots account and carries no identifier, so eToro Money's leg goes to Unassigned; MoneyFarm's balance moves in the same sync's snapshot, so net worth does not dip.

### 5.13 Notifications

Through the existing notifier, never with keys, ids beyond last four characters, or values beyond what the message needs:
- a key rejected (`needs_reauth`), or an eToro key with write scopes (§5.1);
- a missing Trading 212 permission (which one to tick);
- 3 failed syncs in a row, and the recovery (M3);
- a reconciliation difference, or "upload an eToro statement covering <date> onward", once per new warning text (§5.11);
- an API/statement disagreement on a trade (§5.7), once;
- flagged fields, enum values, sheets, columns, instrument types or settlement types, once per new set.

### 5.14 API and UI

**API** under `/api/v1`, owner-only:
- `POST /connections/trading212`: display name, account kind, API key, API secret → 201 `ConnectionOut`; refuses a key whose account is already connected (409) or that gets 401 (422); queues a sync.
- `PUT /connections/{id}/trading212`: new key and secret.
- `POST /connections/etoro`: display name, Public API Key, User Key → 201; refuses a key with write scopes (422, naming none of them beyond "write"); queues a sync.
- `PUT /connections/{id}/etoro`: new keys.
- `POST /connections/{id}/etoro-statement`: §5.3.
- `POST /connections/{id}/trading212-export`: §5.3a.
- `POST /transactions/{id}/pair`: accepts different fiat instruments or different amounts (§5.12).
- `GET /accounts/{id}/identifiers` and the registry routes accept the new kinds.
- Review items from §5.7 are listed by a `GET` on the connection (`/connections/{id}/reviews`) and resolved by `POST .../reviews/{id}/resolve` with `{"take": "api" | "statement"}`, stored as an owner decision (§5.7). **Builder-check call:** the minimal shape; the UI comes later.

Audit arguments never include keys.

**Connections page:** an "Add Trading 212" form (kind, key, secret as password fields), an "Add eToro" form (two password fields), on an eToro connection an "Upload eToro statement (xlsx)" file input, and on a Trading 212 connection an "Upload Trading 212 export (CSV, optional)" file input. Nothing else (owner, 2026-11-23).

## 6. Edge cases

| Case | Handling |
|---|---|
| Re-sync, overlapping history pages or statement periods | Keys per §5.3 and §5.7; identical rows write nothing. |
| Trading 212 pagination skips a page | The weekly full re-walk finds it (§5.2); reconciliation shows its effect meanwhile. |
| A Trading 212 row that later disappears from the API | Kept; a stored row is never deleted for missing from a later walk. |
| Trading 212 renames a ticker | Found by ISIN; the new ticker added to the instrument (`add_identifiers` takes only a missing scheme, so the plan replaces the scheme's value audited). |
| A partial close at eToro | Trade history returns one row per closed position; the remainder has its own `positionId` and `parentPositionId`. Each row is its own close; synthetic tests cover it, since the owner has none. |
| An eToro position that is CFD, leveraged or copied | Flagged, not posted; reconciliation shows it. |
| A statement uploaded before its trades are in the API | The statement posts them; when the API's rows arrive, their values make the postings and the statement's enrich (§5.7). |
| A statement for another eToro user, or not an eToro statement | 422 (§5.3). |
| API and statement disagree on a trade | Both kept, a review item, notified once (§5.7). |
| eToro Money → Trading, statement not yet uploaded | GBP in In transit; "upload a statement" warning (§5.11, §5.12). |
| A GBP → USD transfer with no conversion-fee row | Fee 0 (seen in the owner's data). |
| Two transfers between eToro Money and Trading within the window | Each must be the only candidate; otherwise `several_candidates` for the owner. |
| Withdraw Request then Withdraw Request Cancelled | Both post; they net to zero on the account. |
| Two Monzo card deposits of the same amount minutes apart | Paired by time within N minutes when unique; else the owner pairs. |
| A Trading 212 key without `pies:read` or `orders:read` | That part skipped, flagged once. |
| Trading 212 pies endpoints removed | Their 404 skips pies, flagged once; positions already carry `quantityInPies`. |
| An eToro key that gains write scopes | Sync refused (§5.1). |
| eToro balance history older than 12 months | Not available; the earliest snapshot stands (opening, §5.5). |
| Unknown enum values, fields, sheets, columns | Stored raw, flagged by name, not posted when they would change postings. |
| A statement position Lararium cannot place (no Closed Positions row to say it is real, or no ISIN, type or currency) | Not posted, flagged "a statement position Lararium cannot place" (ruling R40): the flag is found in full every run, so it clears on the next sync or upload after which the position can be placed, and it is not raised for an open the statement does not close made after the latest sync's fetch, which the next sync is expected to bring. |

## 7. Left for later milestones

- Screens: cross-currency pairing, review items, statement prompts, pies (owner, 2026-11-23: backend first).
- Identifying Trading 212 bank-transfer deposits by payment reference, once one exists.
- A price source for Trading 212-only instruments before the connection's first day.
- M9: runtime NAV checks, stale-sync heartbeats, reconciliation views.

## 8. Left to the builder

- Table and column names, indexes and constraints, following the finance schema's conventions; the plan lists every field with its home.
- Pacing within the published limits; warning and notification wording; the forms' layout.
- **Task 0 (before any code), with the owner's real data, printing shapes and checks only.** It is a gate (builder-check call): a check that fails stops the plan, and the design is amended with the owner before the tasks that depend on it, except where a fallback is written below.
  1. eToro statement time zone (§5.8) and balance-history cut-off;
  2. that Realized Equity Change (plus API cash for opens and closes) reproduces each Account Activity `Balance` (§5.7);
  3. that the same events are listed identically in two overlapping statements (the owner downloads two);
  4. that each eToro Money internal transfer has exactly one statement transfer row within the window, and how its counterparty names Trading (§5.12). Fallback: where two candidates exist, the owner pairs them (`several_candidates`), so a failure here does not stop the plan;
  5. the time gap between each Monzo card payment and its Trading 212 deposit, to pin N (§5.12). Fallback: if the gaps do not separate the owner's two deposits, they are paired by the owner;
  6. the xlsx-over-PDF superset (every PDF field present in the xlsx);
  7. a Trading 212 CSV export's columns (§5.3a): done for deposits (§5.3a); trade rows after the first trade;
  8. `decimals` for units and quantities (§5.6).
  Trading 212's sign conventions and `netValue` formula wait for the owner's first trade; the plan writes the mapping from the docs and the real check confirms it (§10).

## 9. Answers from the owner (2026-11-22/23)

- **Probes first**, before the design: done (§1).
- **eToro key**: Read. A Write key was tried for `money.transfer:read`; it lacks it too, and was deleted.
- **eToro Trading's records: A** (trades from the API, cash events from the statement), and **every statement field stored, enriching what exists**.
- **Statement cadence**: on Lararium's prompt.
- **eToro accounts modelled separately.**
- **Trading 212 deposits**: by Apple Pay so far, maybe bank transfer later. **Pies** will be used.
- **Backend first**; UI later, except what entering keys and uploading needs.
- **Trading 212's CSV export is imported** as well as the API (2026-11-23, after the audit showed a field the API lacks), **optionally**: everything works without it.

## 10. Verification

**Tests**, synthetic data only:
- **Parsing**: every endpoint's shape, unknown fields and enums, missing required fields; the xlsx reader on a synthetic workbook with every sheet (shared strings, inline strings, numbers), a DTD refused, a missing column, an extra column.
- **Read-only gate**: an eToro key with a write scope is refused before any other call; the HTTP client refuses non-GET.
- **Ledger mapping**: Trading 212 buy, sell, taxes in two currencies, dividend, deposit, withdrawal, fee, interest, a split; eToro open, close, partial close, each statement cash event, a dividend with withholding tax. Each balances; re-normalising writes nothing.
- **Merging**: API first then statement, statement first then API, give the same ledger; a disagreement → one review item, once; resolving it toward the statement re-posts, and regeneration replays the decision.
- **First statement**: one starting after Date Created is refused; one covering it is accepted, and a later partial one adds only new rows.
- **Overlapping statements**: two overlapping uploads write each row once.
- **Reconciliation**: consistent data, no warning; a missing transfer → "upload a statement", once; a missing trade → a difference, once.
- **Transfers**: Monzo ↔ eToro Money; eToro Money → Trading in both arrival orders, with and without a fee row, net worth constant until settlement and moved by exactly the fee after; owner pairing GBP ↔ USD; two Monzo card deposits minutes apart into two Trading 212 accounts, each paired by time; ambiguous ones left to the owner.
- **Instruments**: one ISIN at IBKR, eToro (after a statement) and Trading 212 in the same currency is one instrument.
- **Property tests (Hypothesis)**: for generated consistent eToro histories (API rows and statement rows, uploaded in random overlapping pieces and orders), the ledger is identical, every transaction balances, reconciliation finds no difference, and regeneration gives the same ledger. The same for Trading 212 history walked in random page splits.

**The owner's check, not committed:**
- Task 0 (§8).
- After deploying: each account's positions and cash match the platform's app; the Monzo ↔ eToro Money and eToro Money ↔ Trading transfers show matched; the two Apple Pay deposits show matched to the right Trading 212 accounts; `make real-check` includes the eToro statement.
- After the owner's first Trading 212 trade (and first Invest ↔ ISA transfer): rerun the probe; confirm signs, `netValue` and the transfer mapping, and fix the mapping if they differ.
