# Lararium phase 1 design: personal finance infrastructure

**Languages:** English | [简体中文](./2026-11-11-financial-gate-phase1-design.zh-CN.md)

- Date: 2026-11-11
- Status: Confirmed; implementation is starting according to plan
- License: AGPL-3.0-or-later (open-source project)
- Scope: Phase 1 (the finance core, connecting all financial data sources). It also specifies the interfaces that Phase 1 must put in place in advance so the system can later be extended into a "personal OS".
- Authoritative version: this English version is authoritative; the Chinese translation follows it, and where they differ, the English version prevails.

## 0. How to read this document

The decision log in §15 gives each decision a status:

- **Requirement**: something the project must satisfy.
- **Confirmed**: a design settled after discussion.
- **Default**: the default approach chosen by the designer; it may be changed with good reason.

§16 lists the parts left to the implementer to decide, and §17 lists questions that are not yet confirmed but do not block the start of work.

## 1. Intent

Lararium is a **self-hosted personal financial infrastructure**. Its first goal is to aggregate and track all of one person's assets in one place: brokers, exchanges, banks, self-custodied crypto wallets, and banks that have no API and only allow manual import of transaction statements or deposit information.

It is open-source software: each user deploys their own instance on their own machine and connects their own accounts with their own credentials. It is not a hosted service (see "Open source and compliance" in §3).

The system will be extended with more capabilities over time and may eventually become a "personal OS". The first planned non-financial modules are: to-dos and calendar, health data, and journal. So what matters most at this stage is **forward compatibility**: today's design must not block future modules.

Why build it rather than use an existing open-source project: Firefly III leans towards bookkeeping and budgeting and is weak on investments; Ghostfolio and Wealthfolio lean towards investment portfolios, and bank transactions and transfer inference are not their focus; Beancount + Fava has the best data model, but its plain-text core is suited neither to continuous API sync nor to connecting many modules later. So the core is built in-house, with a data model borrowed from Beancount, and export to Beancount format is supported.

## 2. Goals, non-goals, anti-goals

### 2.1 Phase 1 goals

1. **A trustworthy net-worth view**: for any day (including past dates), the balance and valuation of every account, every asset and every currency, aggregated in the reporting currency. How far back history goes depends on what each data source can actually provide; there is no fixed starting point.
2. **Connect all financial data sources**:
   - Automatic: Monzo, IBKR (Flex Web Service), Binance, SnapTrade Personal (other brokers), eToro (§7.6), self-custodied wallets (read-only).
   - Manual import: banks that can export CSV or OFX.
   - Manual balance snapshots: accounts that can only export PDF, or whose statements can only be viewed in the app; plus assets that have no transactions, such as fixed-term deposits, pensions and property.
3. **The Monzo self-transfer rule**: when Monzo transfers money to one of the user's own other accounts, or receives money from one, the counterpart account's balance increases or decreases accordingly and automatically; when the counterpart account has its own data source, nothing may be counted twice. See §8.
4. **All raw data is retained**, and the ledger can be regenerated from raw data (§6.6).
5. **Adding a new data source only requires adding a connector**: for example, later connecting an Open Banking aggregator to sync all banks automatically must not require changes to the core.
6. **Put the interfaces for the personal OS in place** (§12).
7. **Safe to run long-term**: alerts when a connection breaks, a sync stops, or reconciliation does not balance; data is backed up, and a restore has been rehearsed.

### 2.2 Out of scope for Phase 1

- Any trading, order placement, transfers or withdrawals. The system is read-only.
- A hosted service: no running instances on behalf of others, and no credentials built into the code.
- Budgets, spending-category reports, tax calculation (e.g. UK Capital Gains Tax, CGT). But the data needed for tax must be recorded in full: the cost, fees and exchange rate at execution of every trade, and the account's tax wrapper type (ISA / SIPP / LISA / GIA).
- Read-only MCP (letting agents such as Claude query the data). The interface is reserved and will be added later when needed (§10.3).
- DeFi positions (assets in lending, liquidity pools, staking contracts). In Phase 1, wallets only count the coin and token balances held at addresses.
- PDF statement parsing and LLM extraction (to be added later as a kind of import parser).
- Real-time intraday quotes. End-of-day valuation is enough.
- Personal OS modules (to-dos, calendar, health, journal); only the interfaces are reserved.
- Multi-user, native mobile apps, public internet access (except for the webhook receiver).

### 2.3 Anti-goals: these count as failure even if every feature works

- **Untrustworthy numbers**: net worth is computed wrongly; the same money is counted in two accounts; syncing stops and nobody notices.
- **Precision loss**: amounts pass through floating point.
- **Data lock-in**: data cannot be exported or regenerated, or a data source's history was not retained.
- **Leaks or excess privilege**: credentials or financial data are exposed to the public internet; private keys, seed phrases, or credentials that can trade or withdraw appear in the system; any real personal financial data appears in the repository.
- **Financial assumptions leak into the common layer**: adding a personal OS module later requires changing the structure of common tables.
- **Runaway complexity**: introducing things like microservices, Kafka or Kubernetes into a system maintained for one person.

## 3. Constraints

- **Single-user, self-hosted**: deployed with Docker Compose on an always-on Linux host. It can be a home server, a NAS or a VPS, but not a laptop that goes to sleep. Images must support both amd64 and arm64.
- **Read-only**: all external credentials are granted read-only permissions only; for crypto wallets only addresses and xpubs are stored, and private keys and seed phrases never enter the system. If a data source cannot provide read-only credentials, use a different access path that is read-only (e.g. eToro in §7.6).
- **Network**: only the webhook receiver is exposed to the public internet, via Cloudflare Tunnel, and only for specified paths; all other services are reachable only over the Tailscale private network.
- **Defaults**: the reporting currency defaults to GBP and the time zone to `Europe/London`, because the first institutions to be connected are mainly UK-based; both are configurable.
- **Open source and compliance**:
  - License AGPL-3.0-or-later: anyone may deploy and modify it themselves; anyone who turns it into a hosted service offered to others must publish their modified source code.
  - Bring your own credentials: each user applies for their own Monzo OAuth client, SnapTrade Personal key, IBKR Flex token, Binance key, and so on. SnapTrade's terms explicitly require that personal keys must not be put into distributed applications, nor be used by others.
  - Aggregating bank account information on behalf of others is, in the UK, an account information service (AISP) that requires FCA registration. This project only targets the "deploy it yourself, connect only your own accounts" usage.
  - No real personal data in the repository: all test data is hand-written and synthetic, not "anonymised real responses"; personal notes go in a `private/` directory that is not tracked by git.
  - Documentation may say "supports Monzo, IBKR" and the like, but the project name and logo use no institution's trademarks.
- **Hard constraints of the data sources** (research results as of 2026-11-11, links in §18; re-verify before implementation):
  - **Monzo**: older history can only be fetched within 5 minutes after the user completes SCA authorisation in the app; after that, only roughly the last 90 days can be fetched. Requests outside that range may return success with silently truncated results, so check the date range of the returned data. Webhooks have only one event type, `transaction.created`, and community reports say they are sometimes delivered more than once and occasionally not delivered. This API is only for use by the developer themselves (or a small number of allow-listed users).
  - **IBKR**: Flex Web Service tokens are generated in Client Portal, with a selectable validity from 6 hours to 1 year, and can be bound to an IP. Data is end-of-day. The Web API / TWS used by individual accounts requires a gateway login and periodic re-authentication (including 2FA), which makes it unsuitable for unattended operation.
  - **Binance**: read-only API key; binding an IP whitelist is recommended (this requires a fixed egress IP). Trade history can only be fetched one trading pair at a time.
  - **SnapTrade Personal**: free, up to 20 broker connections, but the pricing page says "free for a limited time". Choose read-only (`connectionType=read`) when connecting a broker. The data available differs by broker; for example, some brokers provide only holdings and no transaction history.
  - **eToro**: has its own public API, authenticated with a Public API Key plus a User Key, which can read positions, balances, P&L and transaction history. It has not yet been established whether this key can be set to read-only. SnapTrade also supports eToro, with read-only access.
  - **On-chain data**: third-party APIs change. For example, Etherscan moved Base, BNB, OP and other chains out of its free tier at the end of 2025.
  - **Manual import**: export formats vary widely between institutions: some can export CSV, OFX or QIF, some only have PDF statements, and some fintech apps only allow downloading statements inside the app. The import design must be configured per institution and must not assume any single format.
  - **Future automatic sync**: GoCardless Bank Account Data has stopped accepting new sign-ups. Enable Banking has a "restricted mode": it can only read accounts you have linked yourself, its terms state it is for evaluation or personal use only, the number of linked accounts may be limited, and each authorisation lasts at most 90 days; the official pages do not state a price for this mode, and the claim that it is "free" comes from third-party documentation. Its officially stated coverage is 30 countries in the European Economic Area (EEA); the UK is not among them, and whether UK banks are supported has not been confirmed.

## 4. Architecture

### 4.1 How data flows

```
Sources      Monzo · IBKR Flex · Binance · SnapTrade · eToro · on-chain
   │         CSV/OFX upload · manual snapshots
   │         scheduled pull │ webhook = "sync now" │ file → staging → review
   ▼
[raw]        immutable: every response / file as received, with hash
   │         versioned normalizers (replayable)
   ▼
[ledger]     accounts · instruments · transactions · postings
   │         balance snapshots · transfer matching · reconciliation
   ▼
[valuation]  prices · FX → net worth · holdings · allocation
   │
   ├─▶ REST / OpenAPI ──▶ web UI (React SPA)
   ├─▶ curated views ───▶ BI · exports (Beancount, Parquet) · MCP (later)
   └─▶ events (outbox) ─▶ notifications · future modules
```

### 4.2 Processes

| Process | Technology | Permissions | Network |
|---|---|---|---|
| web | Static files built with React + Vite, served by Caddy | No database access; only calls api | Tailscale only |
| api | FastAPI | Reads and writes the ledger and configuration; handles OAuth callbacks and credential entry, so it can write credentials | Tailscale only |
| worker | Python + Procrastinate | Runs connectors, normalizers, transfer matching, valuation, notifications; can read credentials | Not exposed |
| webhook receiver | A tiny Python ASGI app | Can only insert into the webhook inbox table | Exposed to the public internet via Cloudflare Tunnel, only for specified paths |
| mcp (later) | Official MCP Python SDK | Can only `SELECT` from a few curated views | Local machine or Tailscale |
| postgres | PostgreSQL 18+ | Roles in §11.1 | Internal container network only |

api and worker are two processes running from the same Python codebase. The whole backend is a **modular monolith**, not split into microservices.

### 4.3 Code structure rules

- One Python package, split into two layers: `core` (the common layer) and `modules/finance` (the finance module). Each future personal OS module is a new directory under `modules/`.
- **Dependencies go one way only**: modules may depend on core; core may not depend on any module; modules may not reference each other directly. Modules can only interact through events or through link tables in core. This rule is enforced in CI with import-linter.
- Every module registers itself the same way: database migrations, API routes, MCP tools, event handlers, frontend pages.

## 5. Tech stack

The specific libraries are default choices; the implementer may replace them with comparable libraries if there is good reason, but must document the reason.

| Layer | Choice |
|---|---|
| Backend language | Python 3.13+; uv for dependency management, ruff for linting and formatting, pyright in strict mode |
| API | FastAPI + Pydantic v2, with auto-generated OpenAPI docs |
| Database access | SQLAlchemy 2 (typed) + Alembic migrations |
| Background jobs | Procrastinate (uses Postgres as the queue; supports scheduled jobs, retries and locks) |
| Database | PostgreSQL 18+ (uses `uuidv7()`, `NUMERIC`, `JSONB`, `LISTEN/NOTIFY`) |
| Frontend | React + Vite + TanStack Router / Query / Table + shadcn/ui + ECharts; TypeScript client generated from OpenAPI |
| Data source SDKs | Binance: CCXT plus a few native SAPI calls; SnapTrade: official Python SDK; eToro, Monzo, IBKR Flex: direct HTTP calls |
| On-chain data | BTC: descriptor-derived addresses (e.g. bdkpython or embit), with balance lookups through an Esplora-compatible API or a self-hosted Electrum server; EVM chains: data services such as Alchemy or Blockscout; all behind replaceable interfaces |
| Crypto prices | CoinGecko (the free tier is enough), with Binance public market data as a supplement |
| Deployment | Docker Compose: postgres, api, worker, webhook-in, web (Caddy), cloudflared |
| Network | Tailscale (private access); Cloudflare Tunnel (for the webhook receiver only) |
| Secrets | SOPS + age to encrypt environment configuration; credentials stored in the database with application-level encryption |
| Notifications | Apprise (specific channels configured by the user) |
| Monitoring | healthchecks (heartbeats for scheduled syncs); structured logs |
| Backup | `pg_dump` + WAL archiving (point-in-time recovery) + restic encrypted off-site backup |
| Later: MCP | Official MCP Python SDK |

All dependency licenses must be compatible with AGPL-3.0; the libraries chosen so far all use permissive licenses such as MIT, Apache-2.0, BSD and PostgreSQL. Run a dependency license scan before the official release.

What was not chosen, and why:

- **No Next.js**: a single-user private app gets no benefit from SSR / RSC, which instead enlarge the server-side attack surface (the React2Shell vulnerability of December 2025 allowed unauthenticated remote code execution, CVSS 10); behaviour changes a lot between major versions; and Server Actions do not amount to an API that other clients can reuse.
- **No SQLite**: it has no true decimal type, and its concurrent write capability is weak.
- **No Redis or Kafka**: Postgres is enough to handle queues and events.

## 6. Data model

This section specifies only concepts and invariants. Concrete table structures, naming and indexes are left to the implementer.

### 6.1 Schemas

- `core`: common objects (§6.2), raw data, connector state, the webhook inbox, credentials, events, the audit log.
- `finance`: accounts, instruments, transactions, postings, balance snapshots, prices, FX rates, transfer matches, import batches.
- Procrastinate uses its own schema.
- Each future module gets its own schema.

### 6.2 Common objects (core)

Names and structures must be generic, but Phase 1 only builds what finance can use right now:

- **Entities**: people, institutions, places. Banks, brokers, exchanges, Monzo merchants and transfer counterparties are all entities, not finance-specific tables. Entities can have aliases and external IDs (e.g. a Monzo merchant id).
- **Documents**: imported statement files, IBKR Flex XML, large raw responses, and later receipts. Stored by content hash (sha256), recording media type, size, original filename and source. In Phase 1 the storage backend is a local directory, but the interface must allow switching to S3-compatible storage.
- **Links**: a typed edge between any two objects, e.g. "transaction ↔ receipt".
- **Connections**: each configured data source instance (§7.1).
- **Raw records**, **webhook inbox**, **credentials**: see §7 and §11.
- **Events**: implemented with an outbox table. The envelope fields are fixed: type, source, object ID, time, data, version, with no finance-specific fields.
- **Audit log**: every write records: actor type (user / rule / connector / Agent / system), actor ID, command, parameters, time, changes, correlation ID.

Every object has an `owner` field, which defaults to the entity representing the user themselves, leaving room for adding family members later. In Phase 1, joint accounts count 100% towards net worth.

### 6.3 Finance objects (finance)

- **Instrument**: type (fiat, stock, ETF, fund, bond, crypto asset, option, CFD, other), number of decimal places, quote currency (only for instruments that have a price). An instrument can have multiple identifiers: ISIN, FIGI, ticker@exchange, IBKR conid, eToro instrument id, chain + contract address, CoinGecko id. The same instrument must be mergeable into a single record whether it comes from IBKR, eToro, SnapTrade or Binance. An instrument can be marked "hidden", for example spam airdrop tokens received in a wallet; once hidden, it does not count towards net worth.
- **Account**:
  - Bookkeeping type: asset, liability, income, expense, equity.
  - Subtype: bank, savings pot, broker, exchange, wallet, in-transit clearing account, manual asset.
  - Institution (entity), owner, tax wrapper (ISA / SIPP / LISA / GIA / none), status, and opening and closing dates.
  - Parent account: for example, a Monzo pot is a child account of the Monzo main account; Binance spot, funding and earn can be child accounts under one Binance account.
  - **Balance mode**: derived from transactions, from snapshots, or both, reconciled against each other.
  - **Single authoritative source**: one connection, or "manual". The same account may not receive entries from two sources at the same time.
- **Account identifiers (the "my accounts" registry)**: the identifier type (sort code + account number, IBAN, card last digits, payee name alias, deposit reference, wallet address, xpub, Monzo user id, Monzo pot id, "conduit"-type institution) plus the normalised value, pointing to an account. Transfer inference relies on it (§8). An xpub wallet is one account, and all addresses derived from it are identifiers of that account.
- **Transactions and postings**:
  - A transaction records: the instant it occurred (`timestamptz`), the booking date and value date (`date`), description, counterparty entity, status (pending / booked / inferred / confirmed / void), origin (connector / manual / inferred / rule), and it can be traced back to its raw records. On-chain transactions additionally record the chain and the transaction hash.
  - A posting records: account, instrument, signed quantity; with cost or price attached when needed (trades, currency exchange). Fees (including on-chain gas fees) are a separate posting.
- **Balance snapshot**: account, instrument, quantity, point in time, source. There are two kinds: "institution-reported" and "manually entered".
- **Prices and FX rates**: instrument (or currency pair), price, point in time, source.
- **Transfer matches**: see §8.
- **Import batches and staging rows**: see §7.8.

### 6.4 Invariants

- **Amount precision**: all amounts and quantities use unbounded-precision `NUMERIC` in the database, `Decimal` in Python, and are transmitted as strings in JSON. Floating point is forbidden anywhere along the whole chain, with lint or type checks as a safeguard. Do not use "integers in the smallest unit": ETH's smallest unit, wei, has 18 decimal places, and an int64 can only hold about 9.2 ETH. Each instrument records its own number of decimal places, used for display and rounding.
- **Double-entry balance**: each transaction's postings must balance. Transfers within the same instrument balance by quantity; trades and currency exchanges balance after conversion by cost or price, with a configurable tolerance. This is validated by the service layer on write, and a periodic integrity job also re-checks the whole database.
- **Every quantity has an instrument**: there is no such thing as "an amount without a currency".
- **Deduplication**: records from data sources are unique by "(connection, external ID)". Records without an external ID (e.g. CSV rows) are deduplicated by a hash of their normalised content, and genuine duplicates must be distinguishable: two purchases on the same day with the same amount and the same description are two records, and can be told apart by adding the row's sequence number within the file or the running balance from the statement.
- **IDs**: always UUIDv7.

### 6.5 Time model

- Machine timestamps are always stored in UTC (`timestamptz`).
- Times with local meaning additionally store the IANA time zone (e.g. `Europe/London`), for example the time zone the user was in when a transaction occurred. The zone must be stored rather than taken from the viewer: the browser only knows where the viewer is now, not where the event happened. Store the zone name, not just an offset like `+08:00`: an offset can display a past instant but cannot do calendar arithmetic across daylight-saving changes.
- What to store depends on the kind of time:

  | Kind | Examples | Stored as |
  |---|---|---|
  | Something that already happened | transactions, sleep samples, when a journal entry was written | UTC instant (`timestamptz`) + IANA zone |
  | Planned or recurring | future calendar events, "every Monday 09:00" | local wall-clock time + IANA zone; the UTC instant is derived and recomputed when recurrence or tzdata changes it |
  | Floating (follows the user) | "take medication at 08:00 wherever I am" | local wall-clock time, no zone |
  | All-day | birthdays, all-day events, booking dates | `date` |

  Planned times keep the wall clock because that is the user's intent: "every Monday 09:00 London" is 09:00 UTC in winter and 08:00 UTC in summer, and a fixed UTC instant would also drift if the zone's rules change before the event.
- "Dates" and "instants" are stored separately: booking dates, value dates, and later all-day events are all `date`.
- Every record has two times: occurrence time (when it happened in business terms) and ingestion time (when the system learned about it). This makes it possible both to replay "net worth as it was seen on a given day" and to make corrections after the fact.
- Timestamps from a source must be parsed according to that source's own time zone rules. For example, the implementer needs to verify which time zone the IBKR Flex time fields use.

### 6.6 Regeneration, and preserving the user's decisions

- Raw records are immutable. Ledger rows that come from data sources are generated by normalizers and can be deleted at any time and regenerated from raw data.
- **The user's decisions are stored separately**: confirming or rejecting a transfer match, manually editing a description or counterparty, hiding an instrument, manually entered transactions and snapshots. These decisions are located by stable external references (source + external ID) and are re-applied automatically after the ledger is regenerated. This way, upgrading a normalizer does not wipe out the user's work.
- Normalizers carry a version number. Regeneration must be deterministic: the same raw data plus the same decisions yields the same ledger.

## 7. Data ingestion

### 7.1 Connector contract (finance-agnostic)

- The base interface does only one thing: **sync**. The input is a connection and a cursor; the output is a batch of raw records and a new cursor.
- Each connector declares which capabilities it supports: balances, holdings, transactions, webhook, device push, outbound actions (writing back to external systems). No Phase 1 connector uses "device push" or "outbound actions", but the contract must include both: the future to-do and health modules will need them.
- When a connector has the "outbound actions" capability, an external ID mapping table is needed: local ID ↔ external ID ↔ external version (an etag or similar). The rule is "only one authoritative source per object type".
- **Connection lifecycle**: created → authorising → healthy → needs re-authorisation → disabled. Each connection records the time of the last successful sync, the latest error, and the credential expiry time (e.g. for the IBKR Flex token).
- **Scheduling**: each connection syncs on a schedule; a webhook only triggers "sync once now"; there are also backfill jobs. Only one sync can run for a given connection at a time, guaranteed by a lock.
- **Raw records** contain: connection, record type or endpoint name, request parameters, fetch time, content (JSONB; large XML and CSV are stored as documents and referenced), content hash, connector version.
- **Normalizers**: turn raw records into ledger commands; they are versioned separately per "(source, record type)" and are replayable pure functions.

### 7.2 Monzo

- **Authorisation**: the user creates an OAuth client for their own account at developers.monzo.com (confidential type, which can obtain a refresh token). After authorising, SCA must also be confirmed in the Monzo app.
- **Run a full backfill immediately after authorisation**, and it must complete within the 5-minute window. After that, incremental sync: at least once a day, plus webhook triggers. Incremental syncs keep an overlap period with the previous cursor. Every time, check the date range of the returned data to guard against silently truncated results.
- **Mapping**:
  - Monzo account → bank account.
  - pot → child account. Moves between the main account and a pot are internal transfers; both sides are known, so they are paired directly.
  - Balance endpoint results → institution-reported snapshots, used for reconciliation.
  - Merchants → entities.
  - Counterparty information in transactions (sort code, account number, name, Monzo user id, etc., according to what is actually returned) → inputs to transfer inference.
- **Webhook**: register `transaction.created`, pointing at the webhook receiver, with a random secret in the path. On receipt, treat it only as a "sync now" signal and do not trust the payload itself.
- **Re-authorisation**: when the refresh token becomes invalid, send a notification to remind the user, and provide a re-authorisation entry point in the UI.
- **The implementer must verify**: the actual fields for counterparty information and pot moves in the API responses; exactly how far back history can be fetched within the authorisation window (sources disagree).

### 7.3 IBKR (Flex Web Service)

- **Credentials**: the Flex token and query ID, stored as credentials.
- **Two kinds of Flex Query**:
  - Activity: positions, cash per currency, trades, cash transactions (deposits and withdrawals, dividends, withholding tax, interest, fees), corporate actions, FX rates.
  - Trade Confirmation: the day's trades.
- **Flow**: call SendRequest first, then GetStatement, in two steps. Run daily after IBKR has generated its statements. The original XML is stored as a document, and the parsed results are stored as raw records.
- **Mapping**:
  - Each IBKR account → broker account, with multi-currency cash.
  - Positions → institution-reported snapshots, with the cost basis reported by IBKR and the day's price attached.
  - Trades → transactions with cost, commission, and the FX rate at execution.
  - Deposits and withdrawals → the other side of a transfer match, paired with outgoing transfers from Monzo.
- **History**: a single Flex query has a maximum time range, which needs to be verified. Older history can be fetched in segments, or exported from Client Portal and brought in through manual import.
- **Token expiry**: record the expiry date and send a reminder two weeks in advance.

### 7.4 Binance

- **Credentials**: read-only API key; binding an IP whitelist is recommended.
- **Primarily balance snapshots**: the balances of each wallet, such as spot, funding and earn (flexible and locked), recorded as snapshots per child account. If the user actually uses futures or margin accounts, those must be covered too.
- **Transfer-related records**: fiat and crypto deposit and withdrawal history. These are the other side of transfer matches, for example GBP transferred from Monzo into Binance, or crypto withdrawn from Binance to a self-custodied wallet (paired by on-chain transaction hash).
- **Trade records**: not required in Phase 1, because balances come from snapshots. Whatever can be fetched cheaply (e.g. Convert history) can be stored as raw data for later cost calculation.
- **Implementation**: mainly CCXT; earn and funding account endpoints that CCXT does not cover are called directly via native SAPI.

### 7.5 SnapTrade Personal

- **Purpose**: connect brokers other than IBKR that have no better access path. Which brokers to connect is up to the user, who connects them in SnapTrade's connection portal.
- **Authorisation**: uses the user's own Personal plan client ID and consumer key; choose read-only (`connectionType=read`) when connecting a broker.
- **Data**: holdings (with price and average cost), balances, trading activity. When a broker does not provide transaction history, the account is handled in snapshot mode.
- **Webhook**: events such as connection broken and holdings updated go into the webhook receiver, and are likewise treated only as "sync now" signals.
- **Avoid duplication**: IBKR is already connected directly and must not be connected again through SnapTrade; each account can only have one authoritative source.
- **Replaceable**: the pricing page says "free for a limited time", so it is only a connector that can be swapped out.

### 7.6 eToro

There are two paths, chosen according to the read-only constraint:

- **Preferred: eToro's own public API**, provided a read-only key can be created. It authenticates with a Public API Key plus a User Key and can read positions, balances, P&L and transaction history, with more complete data than going through a third party.
- **Otherwise, through SnapTrade**: SnapTrade's access to eToro is read-only.

The implementer must first verify whether an eToro key can be restricted to read-only, and only then decide which path to take. The two paths must not both record entries at the same time.

### 7.7 Self-custodied wallets

- **Read-only**: only addresses and xpubs are stored; private keys and seed phrases never enter the system.
- **BTC**: derive addresses locally from a descriptor, scan up to the gap limit, and query the UTXO balance and transactions of each address. The data comes from an Esplora-compatible API (e.g. mempool.space); if privacy matters, connect to a self-hosted Electrum server.
- **EVM chains**: query native coin and ERC-20 token balances, plus incoming and outgoing transfers. Data sources sit behind replaceable interfaces (e.g. Alchemy, Blockscout). Token metadata and prices are linked to CoinGecko by contract address. Spam airdrop tokens can be hidden by the user (§6.3).
- **Other chains** (e.g. Solana, Tron): added as new data source implementations when needed.
- **Chain scope**: Phase 1 supports BTC and EVM chains (Ethereum and the major L2s) by default.
- **Transfer matching**: on-chain transaction hashes and addresses are identifiers, used to pair Binance withdrawals and deposits with the wallet's incoming and outgoing transfers.

### 7.8 Manual import

- **Flow**: upload a file in the UI → the file is stored as a document → parsed with a "parse profile" → goes into the staging area → the user reviews it → committed to the ledger. A batch can be rolled back as a whole.
- **Parse profiles** are per institution: column mapping, date format, sign convention, encoding. Profiles are stored as data, so adding an institution requires no code change. Phase 1 must provide a generic CSV mapper, plus OFX parsing. The example profiles shipped in the repository are paired only with synthetic example files.
- **Three kinds of handling, by export capability**:
  - Banks that can export CSV or OFX: go through import.
  - Accounts that only have PDF statements: handled as "balance snapshots only" (see below).
  - Fintech accounts whose statements can only be downloaded in the app: if CSV export is available, go through import; otherwise handle as balance snapshots. For multi-currency wallets, record a balance for each currency separately; mark accounts such as ISA and LISA with the corresponding tax wrapper.
- **The staging area** shows: which rows are new, which are duplicates, and which can be paired with existing inferred postings (§8).
- **Manual snapshots and manual assets**: for accounts with no transactions (fixed-term deposits, pensions, property, cars, etc.), enter the balance or valuation directly. Fixed-term deposits can record the interest rate and maturity date.
- **Manual holdings are entered by quantity**: any instrument can be entered manually by quantity (e.g. 0.5 BTC, 100 shares of some ETF) and valued from a price source (§9); for instruments with no price source, a price can be entered manually, or the valuation can be entered directly in the reporting currency.
- **The same applies to liabilities**: liability accounts such as credit cards and loans use the same import and snapshot flow as bank accounts.
- **Accounts that can only export PDF**: in Phase 1 they are treated as "balance snapshots only" accounts. The transfer rule still generates inferred postings on them, which are then checked against manually entered balance snapshots; a difference beyond the tolerance triggers an alert. PDF parsing will be added later.

### 7.9 Webhook receiver

- A tiny ASGI app. Phase 1 has two routes, Monzo and SnapTrade; others such as Notion can be added later.
- Verify the signature where possible; otherwise check the secret in the path. Then write the raw request into the inbox table and return 200 immediately.
- The database role it uses can only insert into the inbox table. The worker reads the inbox and schedules the corresponding sync.
- It is the only part of the whole system exposed to the public internet, so it must be as small as possible and have no read capability whatsoever.

## 8. Transfer matching (the Monzo self-transfer rule)

This is a **generic engine** in the core; Monzo is just the first data source to use it.

1. The **"my accounts" registry** (§6.3) records the identifiers of all of the user's accounts.
2. When an account A that has a data source shows an outgoing or incoming transfer, and the counterparty information matches an account B in the registry:
   - **B has no data source (a manual account)**: generate an inferred posting directly on B with status `inferred`, and B's balance changes immediately. Later, when B's statement is imported, the import flow first tries to pair with existing inferred postings (amount, currency, date window, reference); if it pairs, the status changes to `confirmed` and no new transaction is created. If B only has balance snapshots, check against the snapshots.
   - **B has its own data source** (e.g. IBKR, Binance): A's outgoing transfer is first recorded in the "in-transit" clearing account. Once B's data source reports the corresponding deposit, the in-transit amount is cleared and marked as matched. If it is still unmatched after a configured number of days, an alert is sent.
3. Currency exchange and fees are each separate postings. For example, GBP is sent and USD arrives; the gas fee of an on-chain transfer is recorded separately.
4. **The counterparty information does not match the registry**: no inference is made. Suggestions can be offered based on fuzzy name matching; once the user confirms, the identifier is added to the registry so it matches automatically next time.
5. The user's confirmations and rejections of matches are "user decisions" and are preserved after the ledger is regenerated (§6.6).
6. Parameters such as the date window and amount tolerance are configurable; the defaults are set by the implementer.
7. **Edge cases to cover**: one outgoing transfer arriving in several parts; refunds and reversals; transfers between two Monzo accounts (e.g. a joint account); pot moves (internal transfers); recurring transfers; withdrawals and deposits between Binance and self-custodied wallets (paired by transaction hash); transfers through an intermediary institution (see the next item).
8. **Transfers through an intermediary institution** (e.g. through a remittance service such as Wise): the payee that Monzo sees is the intermediary, not the final destination account.
   - If the user has their own account at the intermediary (with a balance), treat it as an ordinary own account and match the two legs of the transfer separately.
   - If the intermediary is only a remittance conduit, register it in the registry as a "conduit"-type identifier: the outgoing transfer is first recorded as in transit and waits to be paired with the incoming entry on the destination account; FX differences and fees are recorded as separate postings.
9. **UI**: a transfer review page that lists in-transit items with the number of days they have been in transit, match suggestions and conflicts, each of which can be confirmed or rejected individually.

## 9. Valuation and views

- **An account's balance on a given day**:
  - Transaction-derived accounts: the sum of postings up to that day.
  - Snapshot accounts: the most recent snapshot on or before that day.
  - Accounts with both: take the most recent snapshot as the base and add the postings after that snapshot; also reconcile against later snapshots, and alert when the difference exceeds the tolerance.
- **Valuation** = quantity × the most recent price on or before that day × the most recent FX rate on or before that day (converting from the quote currency to the reporting currency).
- **Sources of prices and FX rates**:
  - Daily prices and FX rates from IBKR Flex.
  - Holding prices reported by SnapTrade and eToro.
  - Crypto assets: CoinGecko (by asset ID or contract address), with Binance public market data as a supplement.
  - ECB reference rates (e.g. obtained through Frankfurter), for common currencies. Currencies the ECB does not cover (e.g. some African and South American currencies) need a separate FX source, chosen by the implementer.
  - Manually entered prices and valuations.

  When no price can be found, it must be explicitly marked as "missing price" and must never be treated as 0.
- **Net worth history**: may be materialised daily or computed at query time; this is up to the implementer. But after prices or historical data change, it must be possible to recompute it.
- **Curated SQL views** (versioned) are the shared contract for the UI, BI, exports, and the future MCP:
  - daily net worth;
  - holdings (groupable by account, instrument, asset class, currency, tax wrapper, institution);
  - reconciliation differences;
  - stale connections;
  - unmatched transfers;
  - instruments with missing prices.

## 10. Interfaces

### 10.1 REST API

- Paths carry a version, e.g. `/api/v1`. Writes are "commands": they go through the service layer, are recorded in the audit log, and carry an actor. Reads are "queries".
- The OpenAPI document is the frontend's only contract. The frontend's TypeScript client is generated from it, and CI checks that the two stay consistent.
- In Phase 1 the only actors are "user" and "system / connector", but the audit model must already support "rule" and "Agent".

### 10.2 Web UI (Phase 1)

- **Net worth**: total, breakdown by account, asset class, currency and tax wrapper, and a history chart.
- **Accounts and holdings**.
- **Connections**: status, last sync time, entry points for connecting and re-authorising, and credential entry for each data source (Binance key, eToro key, wallet addresses and xpubs, etc.).
- **Import**: upload, staging review, commit or roll back.
- **Transfers**: in-transit items, match suggestions, confirm or reject.
- **My accounts registry**: maintain the identifiers of each account.
- **Instruments**: merge duplicate instruments, hide spam tokens, handle instruments with missing prices.
- **Settings**: reporting currency, time zone, etc.

The UI must be usable in a mobile browser (accessed through Tailscale).

### 10.3 Later: read-only MCP

Not in Phase 1. Its purpose is to let agents such as Claude query the data in natural language and answer ad-hoc questions that the fixed UI does not cover, such as "how much more did I spend this month than last month", "what share of total assets is in US stocks", or "has the money I transferred to the other bank last week been matched"; once calendar, health and journal are connected later, it can also answer cross-domain questions.

What is reserved now: the curated views in §9, the design of the `mcp_ro` database role (it can only `SELECT` from those views and cannot read credentials or raw data), and the "MCP tools" item in module registration. When it is added later:

- If it is used only on the user's own devices (e.g. Claude Code on the local machine), it need not be exposed to the public internet at all.
- If it is to be used from claude.ai on the web or from the mobile app, those connect from Anthropic's cloud, so a public endpoint with OAuth login is required; at that point the constraint "only the webhook receiver is exposed to the public internet" must be re-evaluated.
- To let agents write, build a separate "operational MCP" that calls the same command layer: sensitive operations require the user's confirmation and can be undone.

### 10.4 Events

- Written to the outbox table; consumers are woken up via `LISTEN/NOTIFY`.
- In Phase 1 the only consumer is notifications: connection broken, sync stale, in-transit transfer overdue, reconciliation mismatch, missing price.
- Event types carry a module prefix, e.g. `finance.transaction.created`; the data format carries a version number.

## 11. Security and operations

### 11.1 Database roles

| Role | Used by | Permissions |
|---|---|---|
| owner (migrations) | Only when running migrations | Create and alter tables |
| backend | api, worker | Read and write business tables; read and write credentials |
| webhook_in | webhook receiver | Can only insert into the inbox table |
| mcp_ro (later) | mcp | Can only `SELECT` from curated views |

### 11.2 Credentials

- Encrypted at the application layer and stored in the database. The master key comes from an environment variable; the environment configuration is encrypted with SOPS + age, and no plaintext appears in git.
- Credentials must not appear in logs, error messages, events or API responses.
- Check permissions when credentials are entered: for data sources whose permissions can be queried (e.g. Binance keys), refuse to save the credential if it is not read-only.

### 11.3 Identity

- In Phase 1 the system can only be accessed from within Tailscale, and Tailscale establishes identity; the exact mechanism is up to the implementer. Code obtains identity through a "current user" abstraction, so switching to passkeys or OIDC later only changes this one place.
- The README must clearly state the security assumptions: access only through Tailscale, read-only credentials, never store private keys. Deployers may not be aware of these, so defaults alone cannot be relied on.

### 11.4 Backup

- Daily `pg_dump`, plus WAL archiving (restorable to any point in time).
- Document storage is backed up as well; encrypted with restic and stored off-site.
- Write a restore procedure document, and actually rehearse a restore once.

### 11.5 Monitoring and notifications

- After each scheduled sync completes, send a heartbeat to healthchecks; alert if a heartbeat is not sent on time.
- When a connection's health status changes, push a notification through Apprise.
- Logs use a structured format, with credentials always redacted.

## 12. Interfaces for the personal OS (Phase 1 must get these right)

The first planned modules, and possible ways to connect them (for reference only, not implemented in Phase 1):

- **To-dos and calendar**: e.g. Notion (official API and webhooks). Notion Calendar does not store events itself; it displays events from the underlying Google, iCloud or Outlook calendars, plus dated entries in Notion databases, so the calendar should connect directly to the underlying calendars.
- **Health data**: e.g. Apple Health. It has no cloud API; do a manual full export the first time, then use an app such as Health Auto Export to push JSON on a schedule.
- **Journal**: either a built-in journal module or reading from an external note-taking tool. For example, Capacities' official API is still in beta and no endpoint for listing all objects was found, so a full export is the most reliable option; plain-Markdown tools such as Obsidian can have their files read directly.

For these modules, Phase 1 must:

1. Keep core and finance separate, with dependencies going one way only (§4.3).
2. Keep the names and structures of common objects free of financial assumptions (§6.2).
3. Include the "device push" and "outbound actions" capabilities, as well as external ID mapping, in the connector contract (§7.1).
4. Follow the time model in §6.5. Future sleep and journal data store a UTC instant plus the IANA zone; planned and recurring calendar events store local wall-clock time plus the IANA zone; all-day events store a date.
5. Give each module its own schema and database role; the future MCP is off by default for each module and is enabled one module at a time by the user. Health and journal data will also have to pass through an "AI gateway" later, which decides whether it may be sent to external models.
6. Include "Agent" in the audit log's actor model. The future AI assistant module will be `modules/lares` (each domain agent is a "lar"); its agents act through this actor type and the AI gateway in item 5.
7. Future device push goes to a receiving endpoint inside the Tailscale private network, with one token per device. The design of the webhook receiver and inbox must be reusable for this.

## 13. Milestones (suggested order)

- **M0 Skeleton**: code repository, Docker Compose, Postgres and its roles, migrations, CI (ruff, pyright, tests, import-linter, OpenAPI consistency check), backup scripts.
- **M1 Ledger core**: core and finance schemas; a ledger service with invariant checks; manual accounts, manual snapshots, manual holdings entered by quantity; the daily net worth view; a minimal usable UI.
- **M2 Manual import**: generic CSV mapper, OFX parsing, staging review, batch rollback; at least one example parse profile (with synthetic data).
- **M3 Monzo**: OAuth, backfill within the 5-minute window, incremental sync, pots, the webhook receiver, re-authorisation reminders.
- **M4 Transfer matching**: registry, inferred postings, the in-transit clearing account, intermediary institutions, review UI. The Monzo self-transfer rule works end to end at this step: tests in the repository use synthetic statements, and the user separately verifies it locally with their own real statements.
- **M5 IBKR Flex**: positions, cash, trades, deposits and withdrawals; prices and FX rates; valuation; pairing with outgoing transfers from Monzo.
- **M6 Binance**: balance snapshots for each wallet, deposits and withdrawals, CoinGecko prices.
- **M7 Self-custodied wallets**: BTC (xpub / descriptor) and EVM chains; pairing with Binance withdrawals and deposits by transaction hash.
- **M8 Brokers**: SnapTrade (the brokers the user connects) and eToro (path chosen per §7.6).
- **M9 Wrap-up**: reconciliation alerts, Beancount and Parquet export, restore rehearsal, dependency license scan.

The roadmap after that:

- **Phase 2**: find an Open Banking aggregator that covers UK banks and is available to individuals, to sync all banks automatically (whether Enable Banking supports the UK is not yet confirmed; see §17); PDF statement parsing; user-defined rules; add read-only MCP as needed.
- **Further out**: personal OS modules, in the order calendar and to-dos → health → journal.

## 14. Acceptance criteria

- For any chosen day, the net worth computed by the system matches a manual calculation, within the tolerance.
- Each data source's balances match what the institution itself reports: Monzo must match exactly; IBKR must match the NAV it reports, Binance, SnapTrade and eToro their reported balances and holdings, and wallets the balances shown on block explorers, each within the tolerance.
- Transfer money from Monzo to a manually imported bank account: both balances change correctly; importing that bank's CSV afterwards does not create duplicate entries.
- Transfer money from Monzo to IBKR: the money first appears in the in-transit account; once IBKR reports the deposit, it is paired automatically. Throughout the process, net worth neither loses a chunk out of nowhere nor counts anything twice.
- Withdraw crypto from Binance to your own wallet: both sides are paired automatically by transaction hash, and gas fees and service fees are recorded separately.
- Delete all ledger rows that come from data sources, then regenerate from raw data: the result is exactly the same as before, and all of the user's decisions are still there.
- Invalidate the Monzo token: an alert is received within the configured time.
- Restore an environment from backup: the data is complete.
- Tests prove that: the webhook_in role cannot read any table; saving a Binance key that is not read-only is rejected.
- There is no floating point on the amount path, guaranteed by lint or type checks.
- There is no real personal data in the repository; all test data is synthetic.

## 15. Decision log

| Decision | Status |
|---|---|
| Aggregate and track all assets; data sources include Monzo, IBKR, Binance, SnapTrade Personal, eToro, self-custodied wallets, plus manual import and balance snapshots for banks | Requirement |
| Monzo self-transfers automatically increase or decrease the counterpart account's balance | Requirement |
| Extend into a personal OS later; prioritise forward compatibility at this stage | Requirement |
| Open source under AGPL-3.0-or-later; bring your own credentials, self-deploy, no hosted service | Confirmed |
| Python backend + React SPA + PostgreSQL, no Next.js | Confirmed |
| worker as a separate process; MCP as a separate read-only service (MCP not built in Phase 1) | Confirmed |
| All financial data sources are connected in Phase 1 | Confirmed |
| Data layer design: raw data layer, double-entry ledger, event table, modular monolith | Confirmed |
| eToro: connect directly to its API if a read-only key is available, otherwise go through SnapTrade | Default |
| Accounts that can only export PDF are handled as balance snapshots in Phase 1 | Default |
| Wallets support BTC and EVM chains by default in Phase 1; DeFi positions are out of scope | Default |
| Add a crypto price source in Phase 1; any instrument can be entered manually by quantity | Default |
| Transfers through an intermediary institution are handled as either the "own account" or the "conduit" case | Default |
| Reporting currency defaults to GBP and time zone to Europe/London, both configurable | Default |
| Specific libraries and tools (FastAPI, Procrastinate, CCXT, CoinGecko, Caddy, Apprise, etc.) | Default |
| Deploy on an always-on Linux host | Default |
| The user's decisions are stored separately and re-applied after the ledger is regenerated | Default |
| Joint accounts count 100% in Phase 1 | Default |
| All test data is synthetic; personal notes go in `private/`, which is not tracked by git | Confirmed |
| Documentation is bilingual: English is the primary version; Chinese translations live alongside as `*.zh-CN.md` | Confirmed |
| Milestone order (M0–M9) and the roadmap after it | Default |
| Project name: Lararium (renamed from financial-gate before publication); the future AI assistant module is named `lares` | Confirmed |
| A posting's cost or price is in another instrument than the posting's own; otherwise it could rescale the quantity and balance anything. The database enforces this too | Default |
| An instrument has an ISO 4217 code if and only if it is a fiat currency | Default |
| Domain code checks every rule the database enforces before writing; a database error a request still causes is a 4xx (`constraint_violation`, `invalid_value`) or a 503 (`query_timeout`) with a readable message, never a 500 | Default |
| An amount has at most 40 digits on each side of the decimal point | Default |
| A net-worth series covers at most ten years (3,653 days), and every database statement times out after 30 seconds | Default |
| The API refuses cross-site requests, reads included, and the app can't be framed by other pages | Default |
| Every API error carries a human-readable `message`; the web UI shows it as is | Default |

## 16. Left to the implementer

- Concrete table structures, naming, indexes, and the package directory structure.
- The internal implementation of normalizers; default values for matching parameters.
- Whether net worth is materialised or computed on the fly.
- The visual design and interaction details of the UI.
- Specific library versions; replacing them with comparable libraries where necessary (documenting the reason and confirming the license is compatible with AGPL-3.0).
- The specific choice of on-chain data services (behind replaceable interfaces).
- Which path eToro takes (per the rule in §7.6, verifying the key's permissions first).
- Which provider to use as the second FX source besides the ECB.
- Exactly how the master key is stored.
- Details of the test strategy. Sample data for connector tests is hand-written and synthetic, based on each API's documentation; property-based testing covers the ledger invariants.
- Whether to also deploy a BI tool such as Metabase.

## 17. Open questions (not blocking the start of work)

- **Automatic sync for UK banks** (only needed in Phase 2): Enable Banking's officially stated coverage is 30 EEA countries; the UK is not in the EEA, and whether UK banks are supported has not been confirmed. When Phase 2 comes, one can register and check the available banks by country in its console; if the UK is not supported, look for another aggregator that covers the UK and is available to individuals.
- **Whether an eToro key can be set to read-only**: verify before implementing M8.
- **TODO: ingress hardening.** The API trusts the `Tailscale-User-Login` header, so anything that reaches Caddy on `127.0.0.1:8080` without going through `tailscale serve` can claim to be the owner. Two things can reach it:
  1. **Other machines on the LAN, when the host runs Linux with Docker Engine older than 28.** Those versions let the local network route to container addresses past a `127.0.0.1` binding. Docker Desktop (macOS, Windows) is not affected, because the LAN can't reach its VM. Before deploying on such a host (e.g. Synology's Docker 24), upgrade Docker or add a host firewall rule. Running Tailscale as a sidecar container would also remove the dependency on the host, but it adds an auth key, a state volume and another tailnet node, so do it only if host independence is wanted for its own sake. If it is built, keep it optional: a compose override file added with `-f` switches a deployment between the sidecar and the host's `tailscale serve`, using the same images and the same API code (both deliver the identity in the same header).
  2. **Any process on the host itself.** Accepted on a single-user host.
- **TODO: identity for non-browser clients.** When the first client runs on a tagged node (the AI assistant, MCP or scripts), verify whether `tailscale serve` sends identity headers for it. If it doesn't, choose then between Tailscale's whois and a per-client API token; a token doesn't tie the API further to Tailscale.

## 18. References

- Monzo SCA and history window: [Monzo community: SCA changes for developer apps](https://community.monzo.com/t/strong-customer-authentication-upcoming-changes-to-developer-apps/78763) · [Open Banking: Monzo](https://openbanking.atlassian.net/wiki/spaces/AD/pages/1135542651/Monzo+Bank+Limited) · [TrueLayer: Monzo ~88–90 days](https://support.truelayer.com/hc/en-us/articles/360016450337-Why-am-I-seeing-only-88-days-of-transactions-on-Monzo-requests)
- Monzo webhook: [Monzo community: inconsistent webhook delivery](https://community.monzo.com/t/inconsistent-monzo-webhook-delivery-missing-transaction-data-on-my-website-integration/184837)
- IBKR: [Flex Web Service access token](https://www.interactivebrokers.com/docs/web-api/flex-web-service/client-portal-configuration/enable-and-create-access-token) · [IBKR Web API](https://www.interactivebrokers.com/campus/ibkr-api-page/webapi-doc/)
- SnapTrade: [SnapTrade Personal](https://snaptrade.com/personal) · [Personal vs Commercial](https://docs.snaptrade.com/docs/personal-vs-commercial) · [Pricing](https://snaptrade.com/pricing) · [Broker access guide](https://support.snaptrade.com/snaptrade-broker-access-guide)
- eToro: [eToro API: authentication](https://api-portal.etoro.com/core/getting-started/authentication) · [eToro API: portfolio breakdown](https://api-portal.etoro.com/api-reference/trading--real/get-portfolio-breakdown.md) · [eToro public API launch](https://www.etoro.com/news-and-analysis/press-releases/etoro-marks-15-years-of-social-investing-with-launch-of-public-apis-and-expansion-of-copytradertm-to-the-us/)
- Bank aggregation: [GoCardless Bank Account Data announcement](https://gocardless.com/bank-account-data/announcement) · [Enable Banking terms](https://enablebanking.com/terms) · [Enable Banking FAQ](https://enablebanking.com/docs/faq) · [Enable Banking homepage](https://enablebanking.com/) · [Actual Budget: Enable Banking](https://actualbudget.org/docs/advanced/bank-sync/enable-banking)
- On-chain: [Etherscan free tier changes](https://info.etherscan.com/whats-changing-in-the-free-api-tier-coverage-and-why)
- Next.js risk: [Vercel: CVE-2025-55182](https://vercel.com/changelog/cve-2025-55182)
- Personal OS: [Notion Calendar: manage calendars & events](https://www.notion.com/help/manage-your-calendars-and-events) · [Notion API upgrade guide 2025-09-03](https://developers.notion.com/docs/upgrade-guide-2025-09-03) · [Health Auto Export](https://apps.apple.com/app/id1115567069) · [Capacities API overview](https://developers.capacities.io/api/overview) · [TimescaleDB 2.23.0 (supports PG18)](https://newreleases.io/project/github/timescale/timescaledb/release/2.23.0)
- UK CGT reference implementation: [cgt-calc](https://pypi.org/project/cgt-calc/)
- License: [GNU AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.html)
