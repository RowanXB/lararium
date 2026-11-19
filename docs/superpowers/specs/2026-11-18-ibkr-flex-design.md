# M5 design: IBKR Flex

- Date: 2026-11-18
- Status: implemented on branch `feat/claude-ibkr-flex` (approved for planning, see §9; updated after the audit of the owner's real statements, 2026-11-18)
- Builds on: `main` with M0–M4 merged, plus the M2 import PR (#12), which adds `core.documents`; migrations up to `0018`
- Parent spec: `docs/superpowers/specs/2026-11-11-financial-gate-phase1-design.md` (§6.3, §6.5, §6.6, §7.1, §7.3, §8, §9, §13 M5, §14)
- Related: the M4 transfer-matching design (`docs/superpowers/specs/2026-11-15-transfer-matching-design.md`), whose engine pairs IBKR deposits and withdrawals; the M3 Monzo plan (`docs/superpowers/plans/2026-11-12-connectors-monzo.md`), whose connector this one follows.

## 1. Intent

The owner's brokerage money should count in net worth as exactly as their Monzo balance does. The IBKR part of net worth should equal what IBKR itself reports, and every trade should keep its cost, commission and exchange rate for the tax work that comes later (spec §2.2). Money moving between Monzo and IBKR must never dip or double-count.

M5 connects Interactive Brokers through the Flex Web Service. The connection runs daily and unattended, with a read-only token the owner creates. IBKR's records are the ledger's truth; IBKR's own reports check them.

### What the owner's use shows (owner, and the audit of the real statements, 2026-11-18; no values recorded here)

- **Three IBKR accounts at IB-UK**, all GBP-based cash accounts, all opened less than a year ago:
  - a general investment account (GIA);
  - the general account that comes with the ISA;
  - the ISA itself, **closed**. IBKR reports its customer type as `IRA-Individual Savings Account/UK`.
- **No trades yet in any of them**: no positions on any day, and nothing but GBP. The year's records are:
  - deposits from outside (Monzo) into the GIA and into the ISA;
  - the ISA's monthly "minimum activity fee";
  - internal transfers among the three accounts.
- **The owner expects to trade stocks and ETFs, and to convert currencies, later.** So M5 builds the full mapping now (approach A, §4.2, full scope), tested on synthetic data.
- **The closed ISA** must be in the ledger, or the other side of its transfers is left unexplained.
  - Flex Web Service cannot select a closed account.
  - Running the same query by hand in Client Portal, with the page's accounts including closed ones, returns all three (verified).
- Lararium has no manual IBKR account yet.

## 2. Goals, non-goals, anti-goals

### Goals

1. **One connection per Flex token.** Every account the Flex statement lists, open or closed, syncs daily into a broker account of its own.
2. **Positions and per-currency cash come from IBKR's records** (approach A, §4): trades, currency conversions, cash transactions, corporate actions and position transfers. After every sync they are checked against IBKR's reported positions and cash, and a difference alerts the owner.
3. **Every field of the sections the ledger uses is stored as a typed field**, or in its better structured home. The owner's rule from 2026-11-15 applies; the exceptions are values that can be derived and values that have a better home.
   - The owner's query ticks every section, field and option, personal fields included (owner, 2026-11-18); the whole statement is kept as a document.
   - A section outside the table in the plan that has rows, or a field outside it that has a value, is flagged to the owner, and typed when real data first shows it. This is M2's bar: real files, not hypothetical fields.
4. **Daily prices and FX rates come from IBKR**, so holdings are valued on every past day of the history, not only today.
5. **On every day IBKR reports**, Lararium's value of each IBKR account equals IBKR's NAV minus accruals, within rounding (spec §14).
6. **Transfers pair automatically.** Monzo ↔ IBKR transfers pair through M4's engine. Transfers between two IBKR accounts (the ISA and the GIA) pair automatically, because IBKR names the other account.
7. **The token is looked after.** The owner is reminded 14 days before the token expires, and a rejected token asks for a new one.
8. **Regeneration.** The ledger rows come from raw records through a versioned, deterministic normalizer (spec §6.6).

### Non-goals

- **The Trade Confirmation Flex query** (spec §7.3 listed it). It only adds same-day trades, and end-of-day valuation is enough (spec §2.2).
- **Options, futures, bonds, CFDs and other asset categories.** Their rows are flagged and not posted (§5.6). The owner holds none.
- **Modelling accrued dividends and interest.** The owner holds mostly accumulating ETFs, so the accruals are small; NAV is compared without them (§5.11).
- **Transfers whose two sides differ in currency or amount**, with FX and fee postings. They move to M6 (eToro sends USD to HSBC). M4 had left them to "the first connector that produces them", and IBKR does not produce them.
- **Adopting an existing manual account into a connection.** The owner has none; the README says to connect first and register identifiers after.
- **Telling apart two own accounts that share one receiving bank account by the payment reference.** See §5.12; this is reconsidered in M6, where Trading 212's ISA and invest accounts are both live.
- **Runtime NAV alerts and stale-sync heartbeats** (M9). Tax calculations (out of Phase 1). eToro and Trading 212 (M6).

### Anti-goals

- An IBKR balance in Lararium differs from IBKR's report and nobody is told.
- A re-sync, or two fetch windows that overlap, changes or duplicates a ledger row.
- Personal data from IBKR (name, address, email, date of birth) reaches the logs, warnings or notifications. It is stored, typed, at the owner's choice.
- A sync exceeds IBKR's rate limits or fails on statement generation that is merely late.
- A new subsystem beyond what these cases need.

## 3. Constraints

### 3.1 IBKR facts (checked in IBKR's documentation on 2026-11-18; the sources are listed in the plan)

- **Protocol.** Flex Web Service version 3, at `https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService`.
  - `SendRequest?t=<token>&q=<query id>&v=3&fd=yyyymmdd&td=yyyymmdd` returns a `ReferenceCode`.
  - `GetStatement?t=<token>&q=<reference code>&v=3` returns the statement XML, or the same error XML (`Status`, `ErrorCode`, `ErrorMessage`).
  - A `User-Agent` header is required.
- **Date range.** `fd`/`td` override the query's saved period and may cover up to 365 days. History is served back to the start of the fourth previous calendar year.
- **Rate limits.** 1 request per second and at most 10 per minute, per token.
- **Timing.** Activity data is updated once a day, at close of business. IBKR advises pulling the previous day's data at the start of the following day.
- **Error codes.**
  - Retry later: 1001, 1003–1009, 1018, 1019 (generation in progress), 1021.
  - The token or configuration needs the owner: 1011 (inactive), 1012 (token expired), 1013 (IP restriction), 1014 (query invalid), 1015 (token invalid), 1016 (account invalid).
- **Token.**
  - Created in Client Portal, valid from 6 hours to 1 year, optionally bound to an IP address.
  - A new token invalidates the old one.
  - No API returns the expiry date.
- **Statement sections.**
  - Open Positions gives positions at the end of the period only.
  - Prior Period Positions gives a price per position per prior day, but no quantity.
  - NAV Summary in Base (`EquitySummaryInBase`) gives cash, stock, interest and dividend accruals and the total, per report date, in the account's base currency.
  - Conversion Rates gives each currency's rate to the base currency, per date.
  - The Transfers section names the other side's company and account (attributes `company`, `account` and `accountName`).
- **Time and format.**
  - Trade times are shown in US Eastern time.
  - The query's date and time format is configurable; a time format with a zone suffix (`HH:mm:ss TimeZone`, giving e.g. `10:19:55 EDT`) exists.
- **FX trades.** A currency conversion is a Trades row with `assetCategory="CASH"`, `symbol="GBP.USD"` (the pair's base and quote), `quantity` in the base currency, `tradePrice` in quote per base, and `proceeds` in the quote currency. Its commission is charged in the account's base currency.

### 3.2 What M5 builds on

- **The connector framework (M3).** M5 uses connections, encrypted credentials, raw records, `SyncContext`, `run_sync` (failure counting, `needs_reauth` on `CredentialRejectedError`, notifications) and `schedule_syncs` every 30 minutes.
- **`upsert_connector_transaction`.** It replaces a connector transaction in place when it differs, and writes nothing when it is identical.
- **Transfer matching (M4).**
  - `transfer_contra` decides a record's contra (Unassigned or In transit).
  - A record is a transfer leg only with exactly two postings, its own and the contra (`LEG_POSTINGS`).
  - An unidentified record pairs by amount and date only when `bank_transfer` is true.
  - `heal_transfers` runs after every sync commits.
  - A connected counterpart's history starts at its connection's earliest non-opening record (`before_history`).
- **Valuation (M1).**
  - A `transactions`-mode account's balance is the sum of its postings, and `reported` snapshots on it are ignored by valuation; Monzo stores its reported balance the same way, for reconciliation only.
  - `v1_valuation_asof` takes the latest price and FX rate on or before the day, so no valuation SQL changes.
  - A transaction balances when each instrument's residual is at most half its last decimal place.
- **Documents (M2, PR #12).** `core.documents` holds the original files by content hash. M5 cannot start until that PR is merged; it numbers its migrations after `0018`.
- **The registry (M4).** `finance.account_identifiers` has one kind, `uk_bank_account`; M4 said further kinds are added by the milestones that need them. A connector registers its own accounts.

### 3.3 Project rules

Everything in the earlier plans' global constraints applies:
- `Decimal`/`NUMERIC`/strings and no floats; UUIDv7; audited writes through `command_context`; `core` stays finance-agnostic;
- domain checks before every database rule, and 4xx, never 500;
- English docs and UI copy; signed commits;
- synthetic test data only, with no real names, amounts, account ids or dates in the repo, tests or PR text;
- tests run on the remote Docker host;
- parsers fail closed;
- Hypothesis property tests pin the engine's invariants;
- fixes close root causes, without new mechanisms for rare cases.

## 4. Approaches considered

### 4.1 Scope

**Considered:** connect eToro and Trading 212 together with IBKR.

**Chosen (owner, 2026-11-18):** IBKR alone in M5; eToro and Trading 212 in M6, before Binance (M7), wallets (M8), SnapTrade (M9) and the wrap-up (M10).

**Considered after the audit:** build only the cash flows, since there are no trades yet. **Chosen (owner, 2026-11-18): the full mapping.** The owner expects to trade, and wants it ready.

**Why:**
- Each source needs its own verification against the real account.
- Three sources would make one review-heavy PR.
- The shared parts (instruments merged across brokers, positions reported as snapshots, transfer pairing) are generic ledger parts already. M5 builds them without anything IBKR-specific leaking in.

### 4.2 How IBKR enters the ledger (owner chose A, 2026-11-18)

1. **A. Records are the truth (chosen).** Positions and per-currency cash are the sum of IBKR's records: trades, conversions, cash transactions, corporate actions, transfers. IBKR's reported positions and cash are stored as `reported` snapshots and checked against the ledger after every sync (§5.11).
   - Every past day's balance is exact.
   - A record type that is not handled shows up as a reconciliation difference the next morning, instead of hiding.
   - It is the Monzo model.
   - The records are needed anyway: for cost and commission data (tax), and for the deposits that pair with Monzo.
   - Its cost: every event that changes a quantity must be posted. For stocks and ETFs that is a short list.
2. **B. Reports are the truth (`hybrid` mode).** Each sync's reported positions and cash are the balance; records only fill the time between reports.
   - Rejected because IBKR reports positions only at the end of a fetched period, so the backfilled year would still depend on records alone.
   - An unhandled event would be hidden by the next report while the history and the tax data stay wrong.
   - A report's cut-off in New York time would have to line up with record times.
3. **Reports only, no records.** Rejected: it cannot pair transfers or keep tax data (spec §7.3, §2.2).

### 4.3 Parsing

**Considered:** depend on the `ibflex` library (MIT).

**Rejected (builder's call), because it:**
- raises on currency codes outside its list and on the zone-suffixed time format;
- reports unknown attributes only as Python warnings, while M5 must name them to the owner;
- converts values, which M5 must type itself anyway.

**Chosen:** a small parser of our own, built on the standard library's XML parser. It refuses any document type declaration and keeps every attribute as the string IBKR sent.

## 5. Design

### 5.1 The connection

- **Connector `ibkr`.**
  - Capabilities: balances, holdings, transactions.
  - Credential purpose: `flex_token`.
  - Config: `query_id` (not a secret) and `account_map` (IBKR account id → finance account id).
  - Cursor: `{"history_from", "fetched_to", "fetched_at"}` (§5.3).
- **Creating a connection.** The owner enters a display name, the token, the date it expires, and the query id. The connection is `healthy` from the start and a sync is queued; there is no authorisation step. `credential_expires_at` is the end of the expiry date in London.
- **A rejected token.** Error codes 1011–1016 raise `CredentialRejectedError`, which sets `needs_reauth` and notifies the owner with IBKR's error message. Neither the token nor the URL is ever included.
- **Updating the connection.** The owner can update the token, its expiry date and the query id; this returns a `needs_reauth` connection to `healthy` and queues a sync.
- **Expiry reminder.** When a sync runs within 14 days of `credential_expires_at`, it notifies once per expiry date. `config["expiry_notified_for"]` stores the expiry it reminded about.

### 5.2 The Flex query the owner sets up

The owner's query (created 2026-11-18, and described in the README) is:
- an Activity Flex Query in XML, with every account selected;
- **every section, with every field and every option ticked**: summary and lot positions; executions, orders, closed lots and the other trade levels; Cash Report with the currency breakout and base summary; and so on;
- date format `yyyyMMdd`, time format `HH:mm:ss TimeZone`, separator `;`;
- Include Currency Rates, Audit Trail Fields, Offsetting Trade/Cancel Pairs and Breakout by Day all set to Yes.

What that query returns (audit, 2026-11-18):
- **One statement per account per day** (Breakout by Day), so the parser reads a statement as the union of its daily statements.
- **Single-row sections** such as Account Information and Change in NAV are attributes of the section element, not child rows.
- **Detail and summary rows.** Cash Transactions repeat each row at `levelOfDetail="SUMMARY"`, without a `transactionID` or time; only `DETAIL` rows are read. The Cash Report's `BaseCurrency` row is a total of the others, and is not read.
- **Repeated rows.** Conversion Rates repeat for every account and day, and are read once per date and currency pair.

**How the parser treats fields:**
- **A required field is missing.** The sync fails, naming the section and field ("select it in the Flex query"). This is fail-closed: a missing id or date cannot be guessed.
- **A section the ledger uses** (the plan's table). Every field the audit showed is typed. An attribute not in the table that has a value is flagged as a connection warning naming the section and field, never the value, and stays in the stored document.
- **Sections the plan lists as derivable or empty** (Statement of Funds, MTM Performance Summary, Change in NAV and the like). They are not typed. They are flagged only if a section never seen with rows starts having them.

### 5.3 Fetching

**Request flow:**
1. One `SendRequest` per fetch window.
2. Then `GetStatement`, first after 5 seconds, and again on 1019 or 1009, until the statement arrives or 2 minutes pass.
3. Requests from one connection are spaced at least 6 seconds apart, which keeps them inside 10 a minute.
4. Every request carries `User-Agent: Lararium/<version>`.

**Error handling:**
- Retryable errors that persist fail the sync as an ordinary failure. The next 30-minute tick retries, and the owner is told after 3 consecutive failures (M3's rule).
- Configuration errors raise `CredentialRejectedError` (§5.1).

**When a sync is due.** When no successful fetch has happened since the latest 06:00 Europe/London. The 30-minute ticks therefore cost nothing once the day's fetch has succeeded. "Sync now" follows the same rule.

**What is fetched:**
- **First sync (empty cursor).**
  - The first window is `[yesterday − 364 days, yesterday]`, with yesterday in Europe/London.
  - If an account's `dateOpened` is earlier than that, earlier windows of up to 365 days follow, back to the later of the earliest `dateOpened` and 1 January of the fourth previous year.
  - For the owner, one window covers everything.
- **Later syncs.** One window `[fetched_to − 6 days, yesterday]`.
  - The 7-day overlap catches late corrections; overlapping rows are written once (§5.4).
  - A gap longer than 365 days, after an outage, is split into windows.

**Order.** Windows are fetched and stored oldest first, in the sync's transaction, and normalised together.

### 5.4 Storage

- **Documents.** Each statement body is stored as a document (`source = "finance.ibkr"`, `application/xml`). The worker therefore mounts the `documents` volume, which today only the api mounts.
- **Raw records.** Each row of each section becomes a raw record.
  - Record type: `ibkr.account_information`, `ibkr.open_position`, `ibkr.position_lot`, `ibkr.prior_period_position`, `ibkr.cash_report`, `ibkr.trade`, `ibkr.trade_detail` (every level of Trades but executions), `ibkr.cash_transaction`, `ibkr.conversion_rate`, `ibkr.corporate_action`, `ibkr.transfer`, or `ibkr.equity_summary` (NAV).
  - Content: the account id, the statement's period for period-level rows, and the row's attributes exactly as strings.
  - Request: the query id, the window, and the document's sha256.
  - Raw records already dedupe by content hash, so a row that overlapping windows repeat unchanged is stored once.
- **Keys.** Ledger rows are keyed by `(connection, external_id)`, where `<accountId>` is the IBKR account id:
  - `trade:<accountId>:<transactionID>`;
  - `cash:<accountId>:<transactionID>`;
  - `ca:<accountId>:<transactionID>`;
  - `transfer:<accountId>:<transactionID>`;
  - `opening:<accountId>:<instrument id>`.
  - `transactionID` is a required field. The same id with different content in one fetch fails the sync, following the convention that a duplicate id blocks the batch. The error names the section, the `transactionID` and the account's last four digits, never the full account id.

### 5.5 Accounts

**One finance account per IBKR account:**
- type: `asset`/`broker`, balance mode `transactions`;
- source: the connection, with institution entity "Interactive Brokers";
- one account for every currency and every position;
- named `IBKR ···<last four of the account id>`, which the owner may rename;
- `opened_on` = `dateOpened`.

**The closed ISA is uploaded once.** The Flex Web Service cannot select a closed account (audit, 2026-11-18).
- The owner sets Client Portal's account selector to include closed accounts, runs the same query by hand over the ISA's life, and uploads the XML file to the connection. The file includes all accounts, about 10 MB for a year.
- The upload is stored as a document and goes through the same parser and normalizer as a fetched statement. Its rows of accounts the Flex Web Service fetches are left out, in every run and replay: they repeat fetched ones, and an older copy among them must not undo what was fetched since (a correction, the account's details, the opening).
- No second login or token is used.

**A closed account** (`dateClosed` present) is closed on that date. Valuation already leaves a closed account out from its closing date. Its history stays, so the ISA's records explain the other side of its movements.

**Tax wrapper.** The connector sets it from Account Information's `customerType`, through the existing `SourceDetails.tax_wrapper`:
- a type containing "Individual Savings Account" is `isa`;
- `Individual` is `gia`;
- anything else leaves it as it is.

**IBKR account ids in the registry:**
- A new identifier kind `ibkr_account`, with the value being the account id in upper case.
- The connector assigns its own accounts' ids on every sync, as Monzo does with sort codes: it never takes a row the owner assigned elsewhere, and it assigns a row the owner dismissed.
- This is what lets a Transfers row naming the other IBKR account identify it (§5.7).

### 5.6 Instruments

**Finding an instrument:**
- **An instrument is a listing**: a security traded in one currency. A UCITS ETF listed in GBP and in USD has one ISIN but two conids, two prices and two positions, so it is two instruments.
- It is looked up by `ibkr_conid`, then by ISIN (or FIGI) **with the same quote currency**. Merging that way means the same listing held at eToro later, or entered by hand, is one instrument.
- A hit by ISIN or FIGI that lacks the conid gets it added: a new `add_identifiers` adds only schemes the instrument does not have yet, and never `iso4217`.
- The registry holds an ISIN once (`instrument_identifiers` is keyed by scheme and value), so a second listing with the same ISIN does not get the ISIN identifier. Its ISIN and FIGI are still typed fields in `finance.ibkr_instruments`. (Builder-check call: the owner's accumulating ETFs commonly have a GBP and a USD listing.)

**Creating an instrument:**
- `symbol` and `name` come from IBKR's symbol and description.
- The quote instrument is the row's currency.
- `decimals` is 4, the precision of IBKR's fractional shares; the audit verifies it.
- Kind: `STK` with sub-category `ETF` is `etf`; other `STK` is `equity`; `FUND` is `fund`.

**Other categories.** `CASH` rows are currency conversions (§5.7), not instruments. Any other asset category (`OPT`, `FUT`, `BOND`, `CFD`, …) is flagged once per category and its rows are not posted; the reconciliation (§5.11) then shows their effect.

**Instrument-level fields.** IBKR's listing exchange, sub-category, multiplier, issuer and similar are stored once per instrument in `finance.ibkr_instruments`, not repeated on each row.

### 5.7 Records in the ledger

Every row below is one connector transaction. Its postings on the IBKR account are listed first. Fees and taxes are separate postings on the account (`is_fee`), and their counterpart is one Unassigned posting per currency, as Monzo's fee postings are.

**Trade, buy** (`quantity > 0`):
- `+quantity` of the instrument, with `cost_per_unit = tradePrice` in the trade currency;
- `proceeds` (negative) in the trade currency;
- `ibCommission` in its currency (`is_fee`, memo "commission"), and `taxes` (`is_fee`, memo "taxes"), each only when non-zero;
- Unassigned `+` their total per currency.

**Trade, sell** (`quantity < 0`): the same, with `price_per_unit = tradePrice` instead of cost. Realised P/L is not a posting. A posting weighed by price balances, and IBKR's realised P/L is stored as a typed field.

**Currency conversion** (`assetCategory = CASH`, symbol `BASE.QUOTE`):
- `quantity` of the base currency, with `price_per_unit = tradePrice` in the quote currency, whichever the side; currencies have no lots;
- `proceeds` in the quote currency;
- the commission, as above.

**Rounding.** IBKR's `proceeds` is rounded to cents, so a trade's residual (quantity × `tradePrice` + `proceeds`) is normally at most half a cent, within the ledger's tolerance. If a rounded `tradePrice` (an FX rate, say) leaves more, the residual is posted as one more Unassigned posting in that currency (memo "rounding"), but only up to |quantity| × half a unit of `tradePrice`'s last decimal place, plus the currency tolerance. Beyond that the sync fails, naming the record and not the amounts, because the difference is not rounding. The cash posting is always IBKR's `proceeds`, and `tradePrice` stays the unit cost or price. (Builder-check call.)

**Cash transaction:**
- `amount` in its currency, and the contra.
- For type "Deposits/Withdrawals", the contra comes from `transfer_contra`, with `bank_transfer = true` and no counterparty identifier. This is the deposit that pairs with Monzo (§5.12).
- **Every "Deposits/Withdrawals" row is posted; internal movements come from Transfers.** The audit (2026-11-18) shows movements between two IBKR accounts only in the Transfers section (`type="INTERNAL"`, naming the other account in `account`), never in Cash Transactions, so every "Deposits/Withdrawals" row is a movement with an outside bank, and none is posted as a bank transfer between IBKR accounts. A movement IBKR ever listed in both sections would be posted twice, and the reconciliation (§5.11) would show the difference.
- Every other type (dividends, withholding tax, broker interest, other fees, payment in lieu, commission adjustments) posts against Unassigned.

**Corporate action:**
- the quantity change of the instrument, if any;
- the cash `proceeds`, if any;
- Unassigned for the remainder, per instrument and currency.

A split, a spin-off or a merger is therefore quantity from or to Unassigned, which is equity and does not count in net worth. The action's type, value and realised P/L are typed fields.

**Transfer** (Transfers section):
- the quantity of the instrument (or the amount, for cash), and the contra from `transfer_contra`;
- when `account` (the other side's account) is set, the counterparty identifier is that `ibkr_account` row;
- postings carry no cost, so the In transit leg balances by quantity. The transfer price and position amount, which are the cost basis for tax, are typed fields.

An ISA ↔ GIA transfer is then identified on both sides and matched by M4's engine. Securities move through In transit exactly as cash does.

**Opening adjustment.**
- When the earliest window an account has starts with non-zero cash or positions, one "Opening balance adjustment" per instrument is posted against Opening balances, dated the day before the account's earliest record.
- Cash comes from the earliest Cash Report's `startingCash`. Positions come from the earliest day the window reports them, minus what the ledger holds by that day, so a later difference no postings explain stays a reconciliation difference.
- This only happens when an account is older than its earliest stored window.
- A run holding an account's earliest stored window sets its opening afresh. A later window leaves it as it is. So a closed account's first upload sets its opening, and a second one reaching further back moves the opening before the new history, or voids it once the history reaches the day the account opened, so the movements it brings are never counted twice.

### 5.8 Time (pinned)

- **`occurred_at`.** The row's date and time in the zone its suffix names (`EST`/`EDT` → `America/New_York`, `GMT`/`BST` → `Europe/London`); any other suffix fails closed. A row with a date but no time is 00:00 in `America/New_York`. `occurred_tz` is that zone.
- **`booking_date`.** The row's `reportDate`: the IBKR statement day the row belongs to, for every section. It is the day whose reported positions and cash include the row, so reconciliation (§5.11) compares like with like. The trade date and other dates are typed fields.
- **`value_date`.** `settleDate`, or `settleDateTarget` for trades.
- **Reported snapshots.** `as_of_date` is the report date. `as_of` is the end of that day in New York (23:59:59.999999 `America/New_York`).
- **Prices and FX rates.** `as_of_date` is the row's date.
- **"Yesterday" and 06:00.** Both are Europe/London (§5.3). At 06:00 in London it is 01:00 or later in New York, so London's yesterday is a complete New York trading day.

### 5.9 Typed fields

**Each field goes to its home in the ledger when there is one:**
- dates go to the transaction;
- quantities, prices, proceeds, commissions and taxes go to postings;
- currencies go to instruments;
- conid, ISIN, FIGI and CUSIP go to instrument identifiers;
- account ids go to the registry;
- the description goes to the transaction's description;
- daily prices go to `finance.prices`, with source `ibkr`;
- conversion rates go to `finance.fx_rates`, with source `ibkr`;
- reported positions and ending cash go to `finance.balance_snapshots`, of kind `reported`.

**A field derivable from stored data is not stored again:** `netCash`, `tradeMoney`, `positionValue` and `priorMtmPnl`.

**Everything else has a column in an IBKR table**, keyed by the ledger row it describes:

| Table | Keyed by | Holds (examples; the plan lists every field) |
|---|---|---|
| `finance.ibkr_accounts` | account | IBKR account id, base currency, account type, customer type, capabilities, trading permissions, IB entity, date opened/funded/closed, last traded date, tax-lot method, and the personal fields (name, addresses, email, date of birth) at the owner's choice |
| `finance.ibkr_instruments` | instrument | conid, listing exchange, sub-category, multiplier, issuer, security id type |
| `finance.ibkr_trades` | transaction | transaction id, trade id, exec/order ids, trade date and time, buy/sell, open/close, exchange, order type, cost basis, realised P/L, MTM P/L, close price, FX rate to base, notes/codes |
| `finance.ibkr_cash_transactions` | transaction | transaction id, type, code, trade id, related instrument, FX rate to base |
| `finance.ibkr_corporate_actions` | transaction | transaction id, action id, type, value, amount, realised P/L, code |
| `finance.ibkr_transfers` | transaction | transaction id, type, direction, company, account, account name, cash transfer, transfer price, position amount (and in base), P/L, client reference, code |
| `finance.ibkr_positions` | reported snapshot | mark price, cost basis price and money, open price, unrealised P/L, percent of NAV, side, FX rate to base |
| `finance.ibkr_position_lots` | reported snapshot, lot | each lot's open date and time, cost basis, originating order and transaction |
| `finance.ibkr_trade_details` | account, level of detail, row | the order, closed-lot and wash-sale levels of Trades (executions are the ledger rows) |
| `finance.ibkr_cash_reports` | account, currency, period | every Cash Report figure: starting and ending cash, ending settled cash, deposits, withdrawals, commissions, dividends, interest, fees, taxes, net trades, FX translation |
| `finance.ibkr_nav` | account, report date | cash, stock, interest accruals, dividend accruals, total, and the other components, in base currency |

These are IBKR's own tables. **Generic broker tables (a builder-check call):** whether eToro's or Trading 212's fields are shared enough for those tables is M6's call, made against those APIs once they are verified. Designing them now would be guesswork.

### 5.10 Prices and FX rates

- **Prices.** Each Prior Period Positions row, and each Open Positions `markPrice`, is the instrument's price on that date in its currency (`record_price`, source `ibkr`). A later write for the same date replaces the earlier one.
- **FX rates.** Each Conversion Rates row is `record_fx_rate(from → to, rate, date, source "ibkr")`. The rates are to the account's base currency. The v1 lookup already uses a direct rate, the inverse of one, or a rate via EUR, whichever is most recent. For a GBP-based account converting to the GBP reporting currency, the rates are direct.
- **If IBKR leaves gaps.** If the first real positions show days on which a position was held with no IBKR price, valuation shows them as `missing_price` (never as 0). The plan then adds the day's last trade price for those days only.

### 5.11 Reconciliation

**The check.** Each instrument's reported quantity (Open Positions for securities; the Cash Report's `endingCash` for each currency, not the `BASE_SUMMARY` row) must equal the sum of the account's non-void postings in that instrument with `booking_date` at or before the report's day, within the instrument's tolerance. An instrument the ledger holds and the report omits counts as reported 0.

**Where it runs.** After every sync and every upload, for every account of the connection:
- **At the account's latest stored statement day**, separately for currencies (the latest Cash Report `toDate`) and securities (the latest Open Positions day that reported a position). A kind with no such day is not checked there. The connection's reconciliation warnings (those beginning `IBKR ···`) are replaced in full by what this finds, so they describe the stored reports whatever order statements arrive in: an older statement uploaded late neither clears nor adds a warning about the current day, except by what it posts.
- **At the end of each window this run fetched**, for the kinds whose section the statement has (an empty Open Positions section says IBKR holds nothing). A difference found only there (an earlier day, or a day whose Open Positions were empty) is notified, never kept as a warning.

**A difference:**
- names the account's last four digits, the instrument and the date;
- is sent once per new warning text, as Monzo's reconciliation is;
- is never corrected automatically.

**NAV.** It is not checked at runtime in M5 (M9). The tests and the owner's check (§10) compare Lararium's valuation of the account on a report date with IBKR's NAV total minus interest and dividend accruals.

### 5.12 Transfers

- **Monzo ↔ IBKR.**
  - The owner registers IBKR's receiving bank account (as Monzo shows it) on the GIA. They also register the account IBKR pays from, if it differs; it shows up in Suggestions after a withdrawal.
  - Monzo's record is identified. IBKR's deposit or withdrawal is an unidentified bank transfer, so M4 pairs them by amount and date: −1 to +5 booking days, as a mutual singleton.
  - In between, the money sits in In transit and net worth never moves.
  - Transfers before IBKR's history are `before_history`; there are none for the owner.
- **Between IBKR accounts.** Both sides are IBKR records naming each other's account (§5.7, verified), so they are matched as mutually identified.
- **Monzo → ISA (owner's call, 2026-11-18).** No new mechanism. The audit shows one deposit into each of the ISA and the GIA.
  - If the ISA shared the GIA's receiving bank account, a Monzo deposit that went to the ISA is taken as a transfer to the GIA. It stays `awaiting` and shows once as overdue on the Transfers page.
  - The owner rejects it. Both records then sit on Unassigned, and net worth is right.
  - It affects only the history of the closed ISA. M6, with Trading 212's two live accounts, decides whether a payment reference should tell accounts apart.

### 5.13 Notifications

Through the existing notifier, never with the token, URLs or values beyond what the message needs:
- token rejected (`needs_reauth`);
- token expiring within 14 days, naming the last day it works;
- 3 failed syncs in a row, and the recovery afterwards (M3's rule). The connection's `last_error`, and so this message, says what failed: for a statement that does not match the query, the section and field (§5.2); for IBKR's errors, the code;
- a reconciliation difference, once per new warning text (§5.11);
- flagged sections, fields or asset categories, once per new set.

### 5.14 API and UI

**API**, under `/api/v1`, owner-only like the rest:
- `POST /connections/ibkr` (`create_ibkr_connection`): display name, token, token expiry date, query id → 201 `ConnectionOut`; queues a sync; an expiry date before today (London) is refused with 422, as by the update;
- `PUT /connections/{id}/ibkr` (`update_ibkr_connection`): token, expiry date, query id → `ConnectionOut`; `needs_reauth` → `healthy`, and queues a sync;
- `POST /connections/{id}/ibkr-statement` (`upload_ibkr_statement`): a Flex XML file of at most 64 MiB → 202; ingested and normalized like a fetched statement; 409 until the connection's first sync has finished, which writes the same rows;
- the existing list, sync-now and disable routes work for IBKR as they are.

Audit arguments never include the token.

**Connections page:**
- an "Add Interactive Brokers" form, whose token field is a password field cleared after submit;
- per IBKR connection:
  - the token expiry date, highlighted within 14 days;
  - an "Update token" form;
  - an "Upload a Flex statement" file input.

Holdings and net worth need no new page: IBKR holdings appear in the existing views with IBKR's prices.

## 6. Edge cases

| Case | Handling |
|---|---|
| Overlapping fetch windows, re-syncs | Rows keyed by `transactionID`; an identical row writes nothing (§5.4). |
| A trade IBKR cancels or corrects | IBKR lists the cancellation as a row of its own (code `Ca`), posted like any other (audit verifies). If IBKR instead drops the original row, the reconciliation reports the difference. |
| Fractional shares | 4 decimals (audit verifies). |
| Split, reverse split, spin-off, merger | A corporate action row: quantity to or from Unassigned; a spun-off instrument is created like any other. |
| Dividend, withholding tax, interest, fees | Cash transaction against Unassigned. |
| GBP → USD inside IBKR, including automatic conversions (`AFx`) | A currency conversion (§5.7). |
| Deposit credited a business day after Monzo sent it | Inside M4's window. |
| IBKR's withdrawal fee | A separate cash transaction ("Other Fees"), not a transfer leg; the withdrawal itself still pairs. |
| The closed ISA | Uploaded once from a Client Portal run; its own closed account with its full history, tax wrapper `isa`; left out of valuation from its closing date. |
| The ISA's monthly minimum activity fee | A cash transaction ("Other Fees") against Unassigned. |
| Breakout by Day, summary rows, repeated rates | Merged, skipped and de-duplicated by the parser (§5.2). |
| ISA ↔ GIA, cash or positions | Matched through `ibkr_account` identifiers (§5.12). |
| Statement not ready yet (1003–1008, 1019) | Retried within the request, then at the next tick; counted as a failure only when it persists. |
| Too many requests (1018) | An ordinary failure, retried at the next tick. |
| A response with no statements, or not a `FlexQueryResponse` | The sync fails ("the Flex query returned no statements: select every account") and the cursor stays (§5.3). |
| Token expired or replaced | `needs_reauth` and a notification; the owner updates it (§5.1). |
| An asset category M5 does not handle | Flagged, not posted; reconciliation shows its effect. |
| A field outside the list / a required field missing | Flagged by name / the sync fails naming it (§5.2). |
| An account older than IBKR serves | An opening adjustment per instrument (§5.7). |
| One instrument at IBKR and elsewhere | Merged by ISIN or FIGI in the same currency (§5.6). |
| One ETF listed in GBP and in USD | Two instruments (§5.6). |

## 7. Left for later milestones

- **M6** (eToro, Trading 212):
  - transfers whose sides differ in currency or amount, with FX and fee postings;
  - whether a payment reference tells two own accounts with one receiving bank account apart;
  - generic broker detail tables, if the fields are shared;
  - the ISA tax wrapper on Trading 212.
- **M9:** a runtime NAV check, stale-sync heartbeats, the reconciliation views.

## 8. Left to the builder

- Table and column names, indexes and constraints, following the existing finance schema's conventions.
- The parser's structure, the request pacing within IBKR's limits, the warning and notification wording, and the UI layout.
- The plan lists every Flex field with its home; anything the audit (plan Task 0) shows differently is settled there.

## 9. Answers from the owner (2026-11-18)

- **Scope.** IBKR only in M5; eToro and Trading 212 in M6. The later milestones move up one (spec §13).
- **Use.** One IBKR GIA, under a year old, stocks and ETFs only, mostly accumulating. GBP comes from Monzo and goes back to it, with some conversion to USD inside IBKR.
- **ISA.** A closed IBKR ISA must be included. Its movements to and from the GIA happened inside IBKR, deposits probably came from Monzo, and it never paid out to Monzo.
- **After the audit:**
  - all three IBKR accounts have only GBP cash movements so far;
  - the owner chose the full mapping anyway (future trades);
  - the owner chose a query with every section and field, personal ones included.
- **Accruals.** Mostly accumulating ETFs, so accruals are not modelled, and NAV is compared without them.
- **No manual IBKR account** exists in Lararium.
- **Approach A** (records are the truth).
- **Monzo → ISA:** no new mechanism; misattributed historic deposits are rejected by hand once.
- **eToro and Trading 212 (for M6).** Trading 212 has an ISA in use; eToro has an ISA that is not used. eToro holds USD, which may go back to HSBC UK.

## 10. Verification

**Tests**, with synthetic Flex XML only (invented account ids, symbols, conids and amounts):
- **Parsing.** A statement with every section; missing and extra fields; zone suffixes; a duplicate `transactionID`.
- **Fetching.** The error codes, polling, pacing, windows (backfill, overlap, a gap over 365 days), and the 06:00 rule.
- **Ledger mapping.**
  - A buy, a sell, a conversion, fees in two currencies, a dividend, a split, a position transfer, a deposit and a withdrawal.
  - Each balances, and re-normalising writes nothing.
- **Reconciliation.** A consistent statement gives no warning; a missing trade gives one warning, once.
- **Transfers.**
  - A Monzo payment to IBKR's registered receiving account and IBKR's deposit two days later are matched, in both arrival orders, and net worth never moves.
  - An ISA → GIA position transfer is matched.
- **NAV.** On a synthetic statement, valuation equals NAV total minus accruals.
- **Property tests (Hypothesis).**
  - For generated consistent statement histories fetched in random overlapping windows and orders, the ledger is identical, every transaction balances, the reconciliation finds no difference, and regenerating from raw records gives the same ledger.
  - This also covers `transactions`-mode accounts, which the existing property tests do not.

**The owner's check, not committed:**
- **Early, before building.** A one-off fetch of the owner's real statement, printing only section names, field names, value shapes and counts (plan Task 0).
- **At the end.**
  - Each account's positions and cash match IBKR's app.
  - The valuation matches NAV minus accruals on a few days.
  - Known Monzo ↔ IBKR transfers show as matched, and ISA ↔ GIA movements as matched.
