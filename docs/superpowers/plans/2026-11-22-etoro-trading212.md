# eToro and Trading 212 (M6) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect the owner's Trading 212 accounts (Invest, Stocks ISA) and eToro accounts (Trading, eToro Money, MoneyFarm ISA) through their read-only APIs, plus an upload of eToro's xlsx account statement, so that:
- positions, cash, trades, dividends and cash movements sync into the ledger, and every field each source gives is stored typed;
- each platform's own reports check the ledger after every sync and upload;
- Monzo ↔ eToro Money, eToro Money ↔ eToro Trading (GBP ↔ USD) and Monzo card deposits ↔ Trading 212 transfers pair, and the owner can pair records that differ in currency or amount.

**Architecture:**
- Two packages, `lararium.modules.finance.trading212` and `lararium.modules.finance.etoro`, each with a client, field lists, a sync (API → raw records), a versioned normalizer (raw records → ledger, typed tables, prices, reported snapshots) and a reconciliation, following `finance.ibkr`.
- Shared pieces: a GET-only HTTP client, typed-table helpers lifted from `ibkr/tables.py`, and a review-item store for API/statement disagreements.
- The transfer engine gains: identifier kinds `etoro_account`, `trading212_account`, `card_merchant` (several holders); a cross-currency tier with a conversion posting; a card-deposit tier by time; owner pairing across currency or amount.

**Tech Stack:** as M5 (FastAPI, SQLAlchemy async, PostgreSQL 18, raw-SQL Alembic, Procrastinate, httpx, React/Vite/TanStack, hey-api). The xlsx reader uses `zipfile` and `xml.etree.ElementTree`. No new runtime dependency.

**Spec:** `docs/superpowers/specs/2026-11-22-etoro-trading212-design.md` (the M6 design; read it first), with the M5 design `2026-11-18-ibkr-flex-design.md`, the M4 design `2026-11-15-transfer-matching-design.md`, the transfer-evidence and owner-pair designs (2026-11-21), and the parent `2026-11-11-financial-gate-phase1-design.md` (§6.3, §6.6, §7.6, §8.3, §13, §14).

**Builds on:** `main` at `68e0bec` or later. **Migrations are numbered after the latest on `main` when Task 3 starts** (0029 on 2026-11-23, so `0030` onward); below they are written `00A`, `00B`, `00C`, `00D`, numbered in the order the tasks land.

## Decisions

Choices the design leaves to the builder, made here so tasks don't have to:

1. **Branch and PR** (owner, 2026-11-23). Implementation continues on PR #32's branch `feat/claude-etoro-trading212-design`, so the design and plan change with the code in one PR. Each phase (A shared, B Trading 212, C eToro, D transfers, E wrap-up) ends green, so it can be reviewed on its own.
2. **Package layout.**

   | File | Holds |
   |---|---|
   | `finance/typed_tables.py` | `column_name`, `parse_decimal`, `parse_date`, `parse_moment`, `typed_values`, `upsert_typed` (lifted from `ibkr/tables.py`, which then imports them) |
   | `finance/readonly_http.py` | `ReadOnlyClient`, `RateLimit`, errors, `USER_AGENT`, `loads_decimal` |
   | `finance/source_reviews.py` | review items and their decisions (design §5.7) |
   | `trading212/{client,fields,sync,normalize,instruments,reconcile}.py` | Trading 212 |
   | `etoro/{client,fields,xlsx,statement,sync,normalize,merge,instruments,reconcile}.py` | eToro |
   | `transfers/pairs.py`, `transfers/engine.py`, `transfers/decisions.py` | the new tiers, the conversion posting, owner pairing |

3. **Column naming.** A typed column is the snake_case of the field's dotted JSON path below the row (`walletImpact.fxRate` → `wallet_impact_fx_rate`; `instrument.isin` → `instrument_isin`), or of the statement header with non-alphanumerics collapsed to `_` (`Spread Fees (USD)` → `spread_fees_usd`, `Long / Short` → `long_short`). Arrays of objects are child tables keyed by the parent and the item's index (`trading212_order_taxes`, `etoro_position_executions`). Types: `numeric` for numbers, `text`, `timestamptz` for zoned instants (a zone-less one is `timestamp`), `date`, `boolean`. `fields.py` of each package is the single list of known paths, required paths and personal paths; the migration, the normalizer and the README table read the same names.
4. **Known fields.** Trading 212: every path of the OpenAPI bundle of 2026-10-17 for the endpoints of design §3.2 (the research note in the shared notes folder lists them). eToro: every path the probe summary showed (`private/m6-probe/summary-readkey.txt`) plus the SPEC fields for trade history, `orders:lookup`, balances, balance history, cash transactions, closed-events, stockback, instruments. Statement: the columns of the six sheets the probe showed (`private/m6-probe/statement-summary.txt`). Any other path or column with a value is flagged by name (`Trading 212 flagged: …`, `eToro flagged: …`) and kept in the raw record.
5. **HTTP.**
   - `USER_AGENT = f"Lararium/{package version}"`; `follow_redirects=False`; a request whose URL host differs from the client's base host, or whose method is not GET, raises `ReadOnlyViolation` before sending.
   - JSON is parsed with `json.loads(text, parse_float=Decimal, parse_int=Decimal)`; the raw text is what the raw record stores.
   - 401 → `HttpAuthError` (a `CredentialRejectedError`); 403 → `HttpForbiddenError(scope: str | None)` (Trading 212's text `Scope( X ) missing for API key` gives `X`); 429 waits `Retry-After`, `ratelimit-reset` or `x-ratelimit-reset` (seconds or epoch) and retries up to 3 times, then `HttpRetryableError`; 5xx and timeouts → `HttpRetryableError`. No message contains a key, a header value or a URL query.
   - Pacing: each endpoint has a `RateLimit(requests, per_seconds)`; the client spaces calls to keep within it. Sleep and clock are injectable.
6. **Trading 212 constants.** `BASE = "https://live.trading212.com"`; limits per design §3.2; page `limit=50`; a weekly full re-walk when `cursor["rewalk_at"]` is older than 7 days; metadata and pies when `cursor["metadata_at"]` is older than 24 hours.
7. **eToro constants.** `BASE = "https://public-api.etoro.com"`; `x-request-id` a fresh `uuid4()` per request; pools: default 60/min, portfolio 60/min, aggregate 60/min, market data 120/min; trade-history windows of 364 days; cash transactions `pageSize=500`; balance history `fromDate` = latest stored date − 7 (first sync: today − 364).
8. **Normalizer versions** `trading212-norm-1`, `etoro-norm-1`, stored in the connection config; bumping replays every raw record (IBKR's pattern). Monzo's goes to `monzo-norm-5` (Task 15).
9. **External ids.**
   - Trading 212: `order:<fill id>`, `order-none:<order id>` (no fill; typed row only, no transaction), `dividend:<reference>`, `transaction:<reference>`, `opening:<account id>:<instrument id>`.
   - eToro: `open:<positionId>`, `close:<positionId>`, `cash:<eToro Money id>`, `activity:<row key>` (statement cash events, §5.3's key), `opening:<account id>:<instrument id>`.
   - One key with different content in one ingest raises the package's `DataError`, naming the record type and key's kind, never values.
10. **Statement row key** (design §5.3): `sha256` of `sheet | Date | Type | Details | Amount | Units / Contracts | Position ID | occurrence`, hex, first 32 characters; `occurrence` counts identical tuples within the statement from 0.
11. **Time.** API `occurred_at` as given (UTC). Statement times in the zone Task 0 pins (Decision 11a), recorded in `occurred_tz`. `booking_date` = the Europe/London date of `occurred_at`; balance-history snapshots use their `date`.
12. **Reconciliation warnings.** `<account name> <SYMBOL> differs from <platform>'s report on YYYY-MM-DD by <amount>`; `eToro Trading: upload an eToro statement covering YYYY-MM-DD to YYYY-MM-DD`. Warnings beginning with a connection's account names are replaced in full each run; a notification is sent only for a text not already present.
13. **Read-only gate.** `etoro.client.write_scopes(scopes) -> list[str]` returns scopes ending `:write` or equal to `money:transfer`. Non-empty → `ReadOnlyGateError` (a `CredentialRejectedError`) with the message "This eToro key can trade or move money. Create a key with Read permission." and no scope names.
14. **Review items** (design §5.7): table `finance.source_reviews(id uuidv7, connection_id, transaction_id, event_key text, field text, api_value text, statement_value text, opened_at, resolved_at, resolution text CHECK IN ('api','statement'), resolved_by)`, unique open item per `(connection_id, event_key, field)`. A resolution is also an owner decision: `finance.source_decisions(connection_id, event_key, field, take, decided_at, decided_by, PK (connection_id, event_key, field))`, read by the normalizer, so regeneration replays it.
15. **Leg fee** (design §5.12): `finance.transfer_leg_fees(transaction_id PK → finance.transactions, instrument_id, amount numeric CHECK (amount >= 0))`, created in `00B` (Task 9) because Task 11 writes it before Task 14 reads it. Only connectors write it; the engine reads it.
16. **Card merchants** (design §5.12): identifier kind `card_merchant`, value `^merch_[0-9A-Za-z]+$` (Monzo's merchant id), `account_id` NULL; holders in `finance.account_identifier_holders(identifier_id, account_id, PK both)`. `TransferSettings.card_deposit_window` = `timedelta(minutes=N)`, N from Task 0 (provisional 2).

11a. **What Task 0 found** (partly, 2026-11-23; no values recorded):
    - **Check 3 passes.** A second eToro statement whose period overlaps the first: all 20 Account Activity and Closed Positions rows have the same key (Decision 10) in both; none only in one. It also starts before Date Created.
    - **Check 5: N.** Each Monzo card payment and its Trading 212 deposit are 0 seconds apart (CSV `Time (UTC)` and API `dateTime` agree to the second); the two deposits are 512 seconds apart. N = 2 minutes stands.
    - **Check 7, Trading 212 CSV (deposits only so far).** Columns `Action`, `Time (UTC)`, `Notes`, `ID`, `Total`, `Currency (Total)`. `ID` equals the API's `reference` and the time equals the API's `dateTime`. `Notes` carries a "Transaction ID: …" the API does not return, so the API is not a superset of the CSV; trade rows' columns are unknown until the first trade.
    - Checks 1, 2, 4, 6 and 8 remain.
    The rest is filled in by Task 0. Until then the provisional values above stand, and Tasks 8–12 (which use the zone, the decimals and check 4's rule) do not start; the others may, since N is settled (check 5).

**Not in this plan:** screens beyond the three forms; Trading 212 bank-transfer deposits by payment reference; CFDs, leverage, copy trading, Options, Crypto wallet; FX rates into `finance.fx_rates`; posting Trading 212 export rows of an `Action` other than deposits and withdrawals before the first real trade shows how they match API records (Task 13a stores and flags them).

## Global Constraints

- **Everything in plans 1, 3, 4 and M5 still applies:** Python 3.13, the uv lockfile enforced, ruff, pyright strict on `src/`, import-linter; PostgreSQL 18; `NUMERIC`/`Decimal`/strings and the word `float` nowhere in `src/`; UUIDv7; UTC `timestamptz` plus a separate `date`; English UI copy and docs (only the README also has `README.zh-CN.md`); AGPL-3.0-compatible dependencies only.
- **Git.** Branch `feat/claude-etoro-trading212-design` (PR #32); commit messages `<type>: <summary>`; signed commits (`local-signed-git-commit` skill); commit by explicit path, never `git add -A`.
- **Writes.** Through `command_context`, audited. The worker acts as `Actor("connector", "trading212")` or `Actor("connector", "etoro")`; the API as the owner.
- **Errors.** Domain checks before every database rule; 4xx with a readable `message`, never 500.
- **Read-only.** Only GET, only to the connector's own host, no redirects (Decision 5). An eToro key with a write scope is never used beyond `/me` (Decision 13).
- **Synthetic data only.** Fixtures are hand-written: invented account ids, position ids, ISINs (valid check digits), symbols, amounts, dates. The owner's files stay in `private/m6-probe/`. No real value reaches code, tests, commits or PR text.
- **Network.** No outbound HTTP in tests: `httpx.MockTransport`, injected sleep and clock.
- **Secrets and personal data.** Keys are `Secret` everywhere and never in logs, audit args, events, API responses, warnings or notifications. Personal fields (`/me` names and date of birth, counterparties' names and bank identifiers, the statement's Name and Username) are stored typed, never logged or put in messages.
- **Tests.** On the remote Docker host: `DOCKER_HOST=ssh://docker-test make check`, green before each commit that ends a task.

## Review Focus

Inputs the design implies that the main-path tests would not hit, most likely first. Each has a test in its owning task:

- **The same eToro statement uploaded twice, and two overlapping statements:** each event written once, nothing updated on the second upload (Task 11: `test_overlapping_statements_write_once`).
- **An eToro Money → Trading transfer whose statement arrives days later:** GBP in In transit, net worth constant on every day, then one conversion posting (Task 16: `test_conversion_waits_for_statement`).
- **Two equal Apple Pay deposits minutes apart into Invest and ISA:** each paired to its own account when the gaps separate them; both left to the owner when they don't (Task 17: `test_two_card_deposits_paired_by_time`, `test_card_deposits_too_close_left_to_owner`).
- **Trading 212's `nextPagePath` as a bare query string, then a page repeating the previous one:** the walk follows the first and stops on the second, without looping (Task 4: `test_next_page_forms_and_repeat_stop`).
- **An eToro key that gains a write scope after the connection was made:** the sync calls `/me` only, sets `needs_reauth`, and posts nothing (Task 9: `test_write_scope_stops_sync`).

---

## File Structure

```
backend/
  migrations/versions/
    00A_finance_trading212.py      # Task 3: trading212_* tables, trading212_account kind, trading212_ticker scheme
    00B_finance_etoro.py           # Task 9: etoro_* tables, etoro_account kind, source_reviews, source_decisions, transfer_leg_fees
    00C_finance_card_merchants.py  # Task 15: card_merchant kind, account_identifier_holders
    00D_finance_trading212_export.py  # Task 13a: trading212_export_rows
  src/lararium/modules/finance/
    typed_tables.py, readonly_http.py, source_reviews.py
    trading212/…, etoro/…
    ibkr/tables.py                 # imports from typed_tables (Task 1)
    identifiers.py, instruments.py, models.py
    transfers/pairs.py, engine.py, decisions.py, settings.py, contra.py
    monzo/normalize.py             # card_merchant identification (Task 15)
    connections_api.py, transfers_api.py, schemas.py, __init__.py
  tests/finance/{test_typed_tables.py,test_readonly_http.py}
  tests/finance/trading212/…, tests/finance/etoro/…, tests/finance/transfers/test_cross_currency.py, test_card_deposits.py
  tests/api/test_trading212_api.py, test_etoro_api.py
web/src/pages/ConnectionsPage.tsx (+ .test.tsx)
README.md, README.zh-CN.md, Makefile (real-check)
```

---

## Phase A: shared

### Task 0: Audit the owner's real data (gate)

Not code. The implementer extends `private/m6-probe/` (gitignored), the owner runs it, and only the printed summary is read. **Each check either passes, or the plan stops and the design is amended with the owner** (design §8), except checks 4 and 5, whose fallback is owner pairing.

- [ ] **Step 1: Extend `statement_probe.py`** to print, never values:
  1. for each statement trade row and the API row with the same Position ID (from `raw/etoro`, read by the script, never by the implementer): the difference between the statement's time and the API's UTC time, as a count per whole-hour offset → the statement zone (check 1);
  2. for each Activity row in date order, whether `previous Balance + cash` equals `Balance` within 0.01, where cash is Realized Equity Change, or the API's amount for Open/Close rows; count of passes and failures, and the failing rows' Types (check 2);
  3. given two statement files with overlapping periods, the count of rows in the overlap with an equal key (Decision 10) in both, and the count only in one (check 3);
  4. for each eToro Money `internalTransfer` (from `raw/etoro`), the number of statement transfer rows of the matching direction within −1 to +5 days, and the distinct `counterparty.name` classes with their counts (check 4);
  5. for each Trading 212 `DEPOSIT` and each Monzo card payment to the Trading 212 merchant (from a read-only query the owner runs, writing only times and amounts' equality), the minute gaps between each deposit and each payment (check 5);
  6. for each xlsx column, whether its header appears in the PDF text, and for each PDF text line with a number, whether that number appears in the xlsx (counts only) (check 6);
  7. a Trading 212 CSV export the owner downloads from the app: headers, value classes, row counts (check 7);
  8. the maximum decimal places of eToro units and Trading 212 quantities seen (check 8).
- [ ] **Step 2: The owner** downloads a second eToro statement overlapping the first, and a Trading 212 CSV export of each account, into `private/m6-probe/statement/`, and runs both probes.
- [ ] **Step 3: Record the findings** as Decision 11a (no values), set N (Decision 16), the statement zone (Decision 11), the decimals (Task 5, Task 10), and, after the first trade, record how trade rows of the Trading 212 export match API records (Task 13a). Check 7 already decided the export is imported, optionally (owner, 2026-11-23).
- [ ] **Step 4: Commit** the plan amendment `docs: record what the M6 audit of the owner's data showed`.

### Task 1: Typed-table helpers

**Files:**
- Create: `finance/typed_tables.py`, `tests/finance/test_typed_tables.py`
- Modify: `ibkr/tables.py` (import the moved helpers; IBKR-specific key functions stay)

**Interfaces:**
- Produces:
  - `def column_name(path: str) -> str` (Decision 3);
  - `def parse_decimal(value: str, label: str) -> Decimal`, `parse_date`, `parse_moment` (as in `ibkr/tables.py` today);
  - `async def typed_values(session: AsyncSession, table: str, values: Mapping[str, str | Decimal | bool | None]) -> Fields` (casts to each column's type, read from the catalog once per table);
  - `async def upsert_typed(ctx: CommandContext, table: str, key: Fields, values: Fields) -> bool` (True when written; nothing written when identical).

- [ ] **Step 1: Write the failing tests:** `test_column_name_paths` (`walletImpact.fxRate` → `wallet_impact_fx_rate`; `Spread Fees (USD)` → `spread_fees_usd`; `Long / Short` → `long_short`; `FX rate at open (USD)` → `fx_rate_at_open_usd`); `test_upsert_typed_writes_once` on a scratch table.
- [ ] **Step 2: Run** `uv run pytest tests/finance/test_typed_tables.py -q`; expected: FAIL.
- [ ] **Step 3: Implement** by moving the generic code; IBKR's tests must pass unchanged.
- [ ] **Step 4: Run** `make backend-check`; expected: green.
- [ ] **Step 5: Commit** `refactor: share typed-table helpers between connectors`.

### Task 2: Read-only HTTP client

**Files:**
- Create: `finance/readonly_http.py`, `tests/finance/test_readonly_http.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class RateLimit(requests: int, per_seconds: int)`;
  - `class ReadOnlyClient(http: httpx.AsyncClient, base_url: str, headers: Callable[[], Mapping[str, str]], limits: Mapping[str, RateLimit], *, sleep=asyncio.sleep, clock=monotonic)`;
  - `async def get(self, path: str, *, pool: str, params: Mapping[str, str] | None = None) -> Response` where `Response(status: int, text: str, json: JSONValue, headers: Mapping[str, str])`, `json` parsed with `loads_decimal`;
  - `def loads_decimal(text: str) -> JSONValue`;
  - errors `ReadOnlyViolation`, `HttpAuthError(CredentialRejectedError)`, `HttpForbiddenError(scope: str | None)`, `HttpRetryableError`, all subclasses of `HttpError(DisplayableError)`.

- [ ] **Step 1: Write the failing tests:**
  - `test_only_get_to_own_host`: a path that resolves to another host, or an absolute URL, raises `ReadOnlyViolation` and sends nothing.
  - `test_redirect_not_followed`: a 302 is an `HttpRetryableError`, and the redirect target is never requested.
  - `test_user_agent_sent`.
  - `test_numbers_are_decimal`: `{"a": 0.1, "b": 12}` → `Decimal("0.1")`, `Decimal("12")`.
  - `test_429_waits_reset_then_retries`: `Retry-After: 3`, then 200 → one sleep of 3; `x-ratelimit-reset` as an epoch is converted with the injected clock.
  - `test_403_scope_parsed`: body `Scope( pies:read ) missing for API key` → `scope == "pies:read"`.
  - `test_pacing_per_pool`: `RateLimit(1, 5)` → two calls are ≥ 5 s apart on the injected clock.
  - `test_errors_never_contain_secrets`: headers' values and query values never appear in `str()`/`repr()` of any error.
- [ ] **Step 2: Run** `uv run pytest tests/finance/test_readonly_http.py -q`; expected: FAIL.
- [ ] **Step 3: Implement** per Decision 5.
- [ ] **Step 4: Run** `make backend-check`; expected: green.
- [ ] **Step 5: Commit** `feat: add a read-only HTTP client for broker connectors`.

## Phase B: Trading 212

### Task 3: Trading 212 client, fields and schema

**Files:**
- Create: `trading212/__init__.py`, `trading212/client.py`, `trading212/fields.py`, `migrations/versions/00A_finance_trading212.py`, `tests/finance/trading212/fixtures.py`, `test_client.py`, `test_schema.py`
- Modify: `identifiers.py` (kind `trading212_account`), `instruments.py` (scheme `trading212_ticker`), `models.py`

**Interfaces:**
- Consumes: Tasks 1–2.
- Produces:
  - `class Trading212Client(http, key: Secret, secret: Secret, *, sleep, clock)` with `summary() -> dict`, `positions() -> list[dict]`, `pending_orders() -> list[dict] | None` (None on 403), `history(kind: Literal["orders", "dividends", "transactions"], seen: Callable[[dict], bool]) -> list[dict]`, `instruments() -> list[dict]`, `exchanges() -> list[dict]`, `pies() -> list[dict] | None`, `pie(pie_id: int) -> dict | None`;
  - `history` follows `nextPagePath` (a full path, `?query` or bare query), stops at null, at a page where every item is `seen`, or at a page identical to the previous one (Review Focus), and at 300 pages (then `HttpRetryableError("history did not end")`);
  - `fields.ENDPOINTS: Mapping[str, EndpointSpec(record_type: str, table: str, required: frozenset[str], known: frozenset[str], children: Mapping[str, str])]`, `PERSONAL: frozenset[str]` (empty for Trading 212);
  - fixtures `summary_json(...)`, `order_item(...)`, `dividend_item(...)`, `transaction_item(...)`, `position_json(...)`, `instrument_json(...)`, `pie_json(...)` with synthetic defaults.
- The migration: the tables of design §5.4 for Trading 212, one column per known path (Decision 3), each with `connection_id`, the key of its row, `raw_record_id`; the identifier kind and the instrument scheme CHECKs extended.

- [ ] **Step 1: Write the failing tests:**
  - `test_basic_auth_header` (key and secret base64'd without line wrapping).
  - `test_next_page_forms_and_repeat_stop` (Review Focus): pages with `nextPagePath` `"/api/v0/equity/history/orders?limit=50&cursor=9"`, then `"?limit=50&cursor=8"`, then `"limit=50&cursor=7"`, then a page equal to the last → 3 requests after the first, no loop.
  - `test_history_stops_at_seen_page`.
  - `test_missing_scope_names_permission`: 403 on `history:orders` → `HttpForbiddenError(scope="history:orders")`; on `pies:read` → `pies()` returns None.
  - `test_schema_has_every_known_field`: every `known` path of every endpoint has its column (via `column_name`) in its table.
- [ ] **Step 2: Run** `uv run pytest tests/finance/trading212 -q`; expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make backend-check`; expected: green.
- [ ] **Step 5: Commit** `feat: add the Trading 212 client, field lists and tables`.

### Task 4: Trading 212 sync

**Files:**
- Create: `trading212/sync.py`, `tests/finance/trading212/test_sync.py`
- Modify: `finance/__init__.py` (register `trading212`)

**Interfaces:**
- Consumes: Task 3; `store_raw_record`, `SyncContext`.
- Produces: `class Trading212Connector` (`name = "trading212"`, `version`, `capabilities = {"balances", "holdings", "transactions"}`, `sync(ctx) -> JSONValue`, `check_access(ctx) -> bool` returning True); cursor `{"account_id", "metadata_at", "rewalk_at", "seen": {"orders": <newest fill id>, "dividends": <newest reference>, "transactions": <newest reference>}}`.
- Raw records: `trading212.summary`, `.position`, `.pending_order`, `.order`, `.dividend`, `.transaction`, `.instrument`, `.exchange`, `.pie`, `.pie_detail`; content is the item's JSON text parsed by `loads_decimal`; request `{"endpoint", "params"}`.

- [ ] **Step 1: Write the failing tests:**
  - `test_first_sync_walks_everything_and_stores_raw`.
  - `test_later_sync_stops_at_seen`.
  - `test_weekly_rewalk`: with `rewalk_at` 8 days old, the walk goes to the end and `rewalk_at` moves.
  - `test_metadata_and_pies_daily`.
  - `test_401_needs_reauth`; `test_missing_required_scope_fails_naming_it`: the error text is "Trading 212 key lacks the history:orders permission".
  - `test_account_id_mismatch_refused`: a summary `id` different from the cursor's → the sync fails ("this key belongs to another Trading 212 account").
- [ ] **Step 2: Run** `uv run pytest tests/finance/trading212/test_sync.py -q`; expected: FAIL.
- [ ] **Step 3: Implement**, calling `normalize` (Task 5) at the end, as IBKR's sync does.
- [ ] **Step 4: Run** `make backend-check`; expected: green.
- [ ] **Step 5: Commit** `feat: sync Trading 212 accounts into raw records`.

### Task 5: Trading 212 normalizer and reconciliation

**Files:**
- Create: `trading212/normalize.py`, `trading212/instruments.py`, `trading212/reconcile.py`, `tests/finance/trading212/test_normalize.py`, `test_reconcile.py`

**Interfaces:**
- Consumes: Tasks 1, 3, 4; `upsert_connector_transaction`, `transfer_contra`, `record_price`, `record_snapshot`, `add_identifiers`, `get_or_create_identifier`.
- Produces: `NORMALIZER_VERSION = "trading212-norm-1"`; `async def normalize(ctx: SyncContext, raw_record_ids: Sequence[UUID]) -> NormalizeReport` (a dataclass with IBKR's `NormalizeReport` fields, declared in each package); `async def listing(ctx, instrument: Mapping[str, str]) -> UUID | None` (ticker → ISIN+currency → create).

**Posting rules:** design §5.7 (Trading 212). Until the owner's first trade, the sign rules are: `fill.quantity` positive for BUY and negative for SELL whatever its sign in the row (side decides); cash = −`netValue` for BUY and +`netValue` for SELL when `netValue` ≥ 0, else `netValue` as given; each `taxes[]` entry a fee posting of −|quantity|. Rows whose result does not balance within the rounding rule fail the sync naming the fill id. Transactions: DEPOSIT, INTEREST_ON_FREE_CASH, LENDING_INTEREST +|amount|; WITHDRAW, FEE −|amount|; TRANSFER with `amount` as given, and flagged once (`Trading 212 flagged: TRANSFER`) so the owner's real check confirms its sign. Account: `asset`/`broker`/`transactions`, named per design §5.5, tax wrapper from the connection's `account_kind`. Pies: rows in `trading212_pies`; no postings. Reconciliation: design §5.11 at the sync's time; `reported` snapshots per instrument and for GBP cash (`availableToTrade + reservedForOrders + inPies`).

- [ ] **Step 1: Write the failing tests:**
  - `test_buy_usd_stock_with_fx_fee`: BUY 3 `ZZQA_US_EQ` at 100.00 USD, `fxRate` 0.8, `netValue` 241.20 GBP, taxes `CURRENCY_CONVERSION_FEE` 1.20 GBP → `ZZQA +3 @cost 80.00 GBP`, GBP −240.00, GBP −1.20 `is_fee`, Unassigned +1.20 (weights +240.00 −240.00 −1.20 +1.20 = 0, as `ledger._weight` counts them; the account's GBP falls by 241.20, the `netValue`, as IBKR's commission does); `trading212_orders` and `trading212_order_taxes` rows.
  - `test_sell_weighs_by_price`; `test_split_from_unassigned`; `test_dividend_against_unassigned`.
  - `test_deposit_through_transfer_contra`: a DEPOSIT → `bank_transfer` false (card, design §5.12), contra Unassigned until a card merchant is registered.
  - `test_cancelled_order_no_transaction`.
  - `test_same_isin_merges_with_ibkr_listing`: an instrument with ISIN `GB00ZZQAAAA1`, GBP, created by IBKR → the Trading 212 row reuses it and adds `trading212_ticker`.
  - `test_renamed_ticker_found_by_isin`.
  - `test_unknown_type_flagged_not_posted`.
  - `test_reconcile_consistent_no_warning`; `test_missing_fill_warns_once`.
  - `test_renormalize_writes_nothing`.
- [ ] **Step 2: Run** `uv run pytest tests/finance/trading212 -q`; expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make backend-check`; expected: green.
- [ ] **Step 5: Commit** `feat: normalize and reconcile Trading 212 accounts`.

### Task 6: Trading 212 API and form

**Files:**
- Create: `tests/api/test_trading212_api.py`
- Modify: `connections_api.py`, `schemas.py`, `web/src/pages/ConnectionsPage.tsx` (+ `.test.tsx`), `web/openapi.json`, `web/src/api/*` (`make openapi`)

**Interfaces:**
- Produces: `POST /api/v1/connections/trading212` `create_trading212_connection`, body `Trading212ConnectionIn {display_name: str (1-200), account_kind: Literal["invest", "isa"], api_key: SecretStr (1-500), api_secret: SecretStr (1-500)}` → 201 `ConnectionOut`; it calls `account/summary` once (409 if that account id is already connected, 422 on 401); `PUT /api/v1/connections/{id}/trading212` `update_trading212_connection` with the two keys → `ConnectionOut`, `needs_reauth` → `healthy`, sync deferred.
- Form: "Add Trading 212" with display name, a select Invest / Stocks ISA, two password fields cleared after submit; help "Create an API key in the Trading 212 app under Settings → API (Beta), for this account only, with read permissions."

- [ ] **Step 1: Write the failing tests:** `test_create_hides_keys` (response, audit, events); `test_same_account_twice_409`; `test_bad_key_422`; `test_update_from_needs_reauth`; vitest `adds a Trading 212 connection and clears the keys`.
- [ ] **Step 2: Run** `uv run pytest tests/api/test_trading212_api.py -q` and `cd web && pnpm test --run`; expected: FAIL.
- [ ] **Step 3: Implement**, then `make openapi`.
- [ ] **Step 4: Run** `make check`; expected: green.
- [ ] **Step 5: Commit** `feat: connect Trading 212 through the API and the Connections page`.

## Phase C: eToro

### Task 7: eToro client and read-only gate

**Files:**
- Create: `etoro/__init__.py`, `etoro/client.py`, `etoro/fields.py`, `tests/finance/etoro/fixtures.py`, `test_client.py`

**Interfaces:**
- Consumes: Task 2.
- Produces:
  - `class EtoroClient(http, api_key: Secret, user_key: Secret, *, sleep, clock)` with `me()`, `pnl()`, `aggregate()`, `breakdown()`, `balances()`, `balance_history(from_date: date, to_date: date)`, `cash_transactions(account_id: str, seen: Callable[[dict], bool]) -> list[dict]`, `trade_history(min_date: date) -> list[dict]` (pages until a short page), `order_lookup(order_id: int) -> dict | None` (None on 404), `closed_events()`, `stockback()`, `instruments(ids: Sequence[int])`, `instrument_types()`, `exchanges()`, `candles(instrument_id: int, start: date, end: date) -> list[dict]`;
  - `def write_scopes(scopes: Iterable[str]) -> list[str]`; `class ReadOnlyGateError(CredentialRejectedError)`;
  - `fields.ENDPOINTS`, `fields.PERSONAL = {"firstName", "middleName", "lastName", "dateOfBirth", "gender", "avatarUrl", "username", "counterparty.name", "bankTransferTransactionDetails.bankIdentifier[].value", "bankTransferTransactionDetails.paymentReference"}`.

- [ ] **Step 1: Write the failing tests:** `test_headers_and_request_id_fresh`; `test_write_scopes` (parametrised: `trade.real:write` → listed, `money:transfer` → listed, `trade.real:read` → not); `test_trade_history_windows_and_short_page`; `test_cash_transactions_page_token`; `test_cloudflare_403_is_retryable` (a 403 HTML body with "error code: 1010" is `HttpRetryableError` naming "User-Agent", not a credential error).
- [ ] **Step 2: Run** `uv run pytest tests/finance/etoro/test_client.py -q`; expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make backend-check`; expected: green.
- [ ] **Step 5: Commit** `feat: add the eToro client with a read-only gate`.

### Task 8: The xlsx reader and statement parser

**Files:**
- Create: `etoro/xlsx.py`, `etoro/statement.py`, `tests/finance/etoro/statement_fixtures.py`, `test_xlsx.py`, `test_statement.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Sheet(name: str, rows: tuple[tuple[str, ...], ...])`; `def read_workbook(body: bytes) -> tuple[Sheet, ...]` (shared strings, inline strings, numbers as their text, booleans; sheet order from the workbook); `XlsxError(ValueError)` for a DOCTYPE, a missing part, or more than 100 000 cells;
  - `@dataclass(frozen=True) class StatementRow(sheet: str, key: str, cells: Mapping[str, str])`; `@dataclass(frozen=True) class EtoroStatement(period_from: datetime, period_to: datetime, created: datetime, username: str, currency: str, rows: tuple[StatementRow, ...], flagged: frozenset[str])`; `def parse_statement(body: bytes, zone: ZoneInfo) -> EtoroStatement`; `StatementError(ValueError)` naming sheet and column only;
  - `statement_fixtures.build_statement(summary: Mapping[str, str], activity: Sequence[Mapping[str, str]], closed: Sequence[Mapping[str, str]] = (), dividends: Sequence[Mapping[str, str]] = ()) -> bytes` writing a valid synthetic xlsx.
- Sheets read: Account Summary and Financial Summary (label → value rows), Closed Positions, Account Activity, Dividends (header row then data). Glossary skipped. Required columns: Account Activity `Date`, `Type`, `Details`, `Amount`, `Units / Contracts`, `Realized Equity Change`, `Realized Equity`, `Balance`, `Position ID`; Closed Positions `Position ID`, `Open Date`, `Close Date`, `Units / Contracts`, `Amount`, `Profit(USD)`, `ISIN`; Account Summary `Username`, `Currency`, `Date Created`, `Start Date`, `End Date`. Row keys per Decision 10.

- [ ] **Step 1: Write the failing tests:** `test_reads_shared_and_inline_strings`; `test_doctype_refused`; `test_too_many_cells_refused`; `test_parses_every_sheet`; `test_missing_required_column_names_it`; `test_extra_column_with_values_flagged`; `test_identical_rows_get_occurrence_keys` (two identical overnight-fee rows → keys differ by occurrence only); `test_same_row_same_key_across_files`; `test_errors_never_contain_cell_values`.
- [ ] **Step 2: Run** `uv run pytest tests/finance/etoro/test_xlsx.py tests/finance/etoro/test_statement.py -q`; expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make backend-check`; expected: green.
- [ ] **Step 5: Commit** `feat: read eToro account statements from xlsx`.

### Task 9: eToro schema, sync and statement ingest

**Files:**
- Create: `migrations/versions/00B_finance_etoro.py`, `etoro/sync.py`, `finance/source_reviews.py`, `tests/finance/etoro/test_sync.py`, `test_schema.py`
- Modify: `identifiers.py` (kind `etoro_account`), `models.py`, `finance/__init__.py` (register `etoro`)

**Interfaces:**
- Consumes: Tasks 1, 7, 8.
- Produces:
  - the tables of design §5.4 for eToro, plus `source_reviews` and `source_decisions` (Decision 14) and `transfer_leg_fees` (Decision 15);
  - `class EtoroConnector` (`name = "etoro"`); cursor `{"gcid", "real_cid", "trade_history_to", "balance_history_to", "seen_cash": {<account id>: <newest id>}, "daily_at", "looked_up": [orderId…]}`;
  - `async def ingest_statement(ctx: SyncContext, body: bytes, filename: str) -> IngestResult` (document `source = "finance.etoro"`, raw records `etoro.statement.<sheet>` with content `{"cells": …, "key": …, "period": …}`), refusing (422-mapped `StatementRejected`) a statement whose Username or Currency doesn't match, or the connection's first statement when its Start Date is after Date Created;
  - `source_reviews.open_review(ctx, connection_id, transaction_id, event_key, field, api_value, statement_value) -> bool` (False when an identical open item exists), `resolve_review(ctx, review_id, take: Literal["api", "statement"]) -> None` (writes the decision), `async def decision_for(session, connection_id, event_key, field) -> Literal["api", "statement"] | None`, `async def list_reviews(session, connection_id) -> list[ReviewOut]`.
- Sync order: design §5.2 (eToro).

- [ ] **Step 1: Write the failing tests:**
  - `test_write_scope_stops_sync` (Review Focus): `/me` returns `trade.real:write` → only `/me` was requested, `needs_reauth`, no raw records but the scopes record.
  - `test_first_sync_fetches_every_endpoint_once`; `test_trade_history_windows_back_to_creation`; `test_balance_history_overlap_7_days`; `test_orders_looked_up_once`.
  - `test_statement_other_user_refused`; `test_first_statement_must_cover_creation`; `test_later_partial_statement_accepted`.
  - `test_schema_has_every_known_field` (API paths and statement columns).
- [ ] **Step 2: Run** `uv run pytest tests/finance/etoro/test_sync.py tests/finance/etoro/test_schema.py -q`; expected: FAIL.
- [ ] **Step 3: Implement**, calling `normalize` (Task 10) after a sync and after an ingest.
- [ ] **Step 4: Run** `make backend-check`; expected: green.
- [ ] **Step 5: Commit** `feat: sync eToro and ingest its account statements`.

### Task 10: eToro accounts, instruments and API records

**Files:**
- Create: `etoro/normalize.py`, `etoro/instruments.py`, `tests/finance/etoro/test_normalize_api.py`

**Interfaces:**
- Consumes: Task 9; as Task 5.
- Produces: `NORMALIZER_VERSION = "etoro-norm-1"`; `async def normalize(ctx, raw_record_ids) -> NormalizeReport`; `async def listing(ctx, instrument_id: int, symbol: str, name: str, currency: str, isin: str | None) -> UUID | None`; `async def merge_instruments(ctx, keep: UUID, drop: UUID) -> None`: repoints **every row that references `drop`** to `keep` (postings, their cost and price instruments, prices, balance snapshots, identifiers, the typed tables' instrument columns, and any other column with a foreign key to `finance.instruments`, found from the catalog so a later table is covered), then deletes `drop`, audited. A price of `drop` on a date and source `keep` already has is discarded (`keep`'s stays); any other unique conflict fails the merge naming the table, before anything is written.

**Rules:** design §5.5–§5.7 for accounts (one per Balances entry; MoneyFarm `snapshots` with a `reported` snapshot per sync; other account types flagged), instruments (kinds 5 → `equity`, 6 → `etf`; non-real, leveraged or mirrored positions flagged), opens and closes from the API (USD cost), eToro Money transactions (`settled` only), prices from candles and current rates (source `etoro`), `etoro_balance_history` and `reported` cash snapshots per day, `etoro_users` from `/me`.

- [ ] **Step 1: Write the failing tests:**
  - `test_three_accounts_created` (Trading broker USD gia; eToro Money bank GBP; MoneyFarm snapshots isa).
  - `test_open_from_api_cost_in_usd`: `amount` 500.00, `units` 4 → `+4 @cost 125.00 USD`, USD −500.00; `etoro_positions` and `etoro_trades` (lookup fields) rows.
  - `test_close_from_trade_history`: `investment` 500.00, `netProfit` 20.00, `fees` 0 → −4 @price 130.00 USD, USD +520.00.
  - `test_partial_close_two_positions` (`parentPositionId`).
  - `test_cash_bank_transfer_identified_by_iban`: a GBP credit whose bank identifier is a registered `uk_bank_account` → In transit.
  - `test_internal_transfer_names_trading`: an `internalTransfer` debit → counterparty identifier = Trading's `etoro_account` (per Task 0 check 4's rule).
  - `test_pending_cash_row_not_posted`.
  - `test_cfd_flagged_not_posted`.
  - `test_merge_after_positions_recorded`: an API sync records an eToro position (a `reported` snapshot and an `etoro_positions` row) for an instrument without an ISIN; a statement then brings an ISIN an IBKR instrument in the same currency holds → one instrument remains; the snapshot, the typed rows, the postings and the prices all reference it; no row references the deleted id.
  - `test_personal_fields_stored_not_logged` (caplog empty of them).
- [ ] **Step 2: Run** `uv run pytest tests/finance/etoro/test_normalize_api.py -q`; expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make backend-check`; expected: green.
- [ ] **Step 5: Commit** `feat: normalize eToro accounts, positions, trades and eToro Money`.

### Task 11: The statement in the ledger: cash events, enrichment, precedence

**Files:**
- Create: `etoro/merge.py`, `tests/finance/etoro/test_normalize_statement.py`
- Modify: `etoro/normalize.py`

**Interfaces:**
- Consumes: Tasks 9, 10.
- Produces: `@dataclass(frozen=True) class TradeEvent(units: Decimal, cash: Decimal, profit: Decimal | None, booking_date: date, occurred_at: datetime)`; `def posting_values(api: TradeEvent | None, statement: TradeEvent | None, takes: Mapping[str, Literal["api", "statement"]]) -> TradeEvent`; `def disagreements(api: TradeEvent, statement: TradeEvent) -> list[tuple[str, str, str]]` (field, api value, statement value; fields `units`, `cash`, `profit`, `booking_date`).

**Rules:** design §5.7 (eToro Trading, statement): cash events and their keys; conversion-fee rows typed, no postings, and written to `transfer_leg_fees` for their transfer row (Task 14 reads it; the row right after or at the same time); ISIN and Closed Positions columns onto `etoro_trades`/`etoro_instruments`, merging instruments by ISIN; dividends with withholding tax as a fee posting; precedence and review items per Decision 14.

- [ ] **Step 1: Write the failing tests:**
  - `test_transfer_rows_are_legs`: "Transfer GBP → USD" +1260.00 → a USD leg on Trading, identified as eToro Money.
  - `test_conversion_fee_row_no_postings_and_leg_fee`: fee −6.30 → no transaction; `transfer_leg_fees` row 6.30 USD on the transfer's transaction.
  - `test_withdraw_request_and_cancel_net_zero`; `test_withdraw_fee_and_cancel`.
  - `test_dividend_with_withholding_tax`.
  - `test_statement_enriches_api_trade`: same Position ID → postings unchanged, `etoro_trades` gains ISIN and FX columns, the instrument gains the ISIN.
  - `test_statement_first_then_api_same_ledger`: compared by external id and postings with `test_statement_enriches_api_trade`'s result.
  - `test_disagreement_opens_one_review`: units differ → one review item, one notification; a second normalize adds neither.
  - `test_resolve_toward_statement_reposts_and_replays`: resolve `statement` → postings from the statement; delete ledger rows, regenerate → same.
  - `test_overlapping_statements_write_once` (Review Focus): two uploads sharing 10 rows, and the same upload twice → each event once; the second upload counts all unchanged.
  - `test_isin_merges_with_existing_instrument`.
  - `test_unknown_activity_type_flagged`.
- [ ] **Step 2: Run** `uv run pytest tests/finance/etoro/test_normalize_statement.py -q`; expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make backend-check`; expected: green.
- [ ] **Step 5: Commit** `feat: post eToro statement cash events and enrich API trades`.

### Task 12: eToro reconciliation and the statement prompt

**Files:**
- Create: `etoro/reconcile.py`, `tests/finance/etoro/test_reconcile.py`
- Modify: `etoro/normalize.py`

**Interfaces:**
- Consumes: Tasks 10, 11; the shape of `ibkr/reconcile.py`'s `reconcile` and `publish`.
- Produces: `async def check(ctx: SyncContext, accounts: Mapping[str, UUID]) -> Checked`, which compares per design §5.11 and decides "upload a statement" per its rule, with warnings per Decision 12.

- [ ] **Step 1: Write the failing tests:** `test_consistent_no_warning`; `test_unmatched_internal_transfer_prompts_statement_once` (eToro Money −1000 GBP to Trading, no statement since → the prompt names the dates; two more syncs, no new notification; after the upload, cleared); `test_difference_without_cause_is_a_difference`; `test_money_balance_checked`; `test_moneyfarm_not_reconciled`.
- [ ] **Step 2: Run** `uv run pytest tests/finance/etoro/test_reconcile.py -q`; expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make backend-check`; expected: green.
- [ ] **Step 5: Commit** `feat: reconcile eToro accounts and prompt for statements`.

### Task 13: eToro API and forms

**Files:**
- Create: `tests/api/test_etoro_api.py`
- Modify: `connections_api.py`, `schemas.py`, `ConnectionsPage.tsx` (+ test), `web/openapi.json`, `web/src/api/*`

**Interfaces:**
- Produces: `POST /api/v1/connections/etoro` `create_etoro_connection` `{display_name, public_api_key: SecretStr, user_key: SecretStr}` → 201 (calls `/me`; 422 on write scopes or 401; 409 if that `realCid` is connected); `PUT /api/v1/connections/{id}/etoro`; `POST /api/v1/connections/{id}/etoro-statement` `upload_etoro_statement` (xlsx, ≤ 16 MiB; 409 before the first sync; 422 per Task 9; 202); `GET /api/v1/connections/{id}/reviews` → `list[ReviewOut {id, transaction_id, field, api_value, statement_value, opened_at}]`; `POST /api/v1/connections/{id}/reviews/{review_id}/resolve` `{"take": "api" | "statement"}` → 204.
- Forms: "Add eToro" (display name, two password fields; help "Create a key in eToro under Settings → Trading → API Key Management, Environment Real, Permission Read."), and on an eToro connection "Upload eToro statement (xlsx)" (help "Download it from eToro's web app: Portfolio → History → Account Statement, as Excel. The first one must start on or before the day you opened the account.").

- [ ] **Step 1: Write the failing tests:** `test_create_refuses_write_key_422`; `test_create_hides_keys`; `test_upload_before_first_sync_409`; `test_upload_ingests_and_heals`; `test_upload_other_user_422`; `test_upload_too_large_413`; `test_reviews_list_and_resolve`; vitest `adds an eToro connection`, `uploads an eToro statement`.
- [ ] **Step 2: Run** the API and web tests; expected: FAIL.
- [ ] **Step 3: Implement**, then `make openapi`.
- [ ] **Step 4: Run** `make check`; expected: green.
- [ ] **Step 5: Commit** `feat: connect eToro and upload its statements through the API and the Connections page`.

### Task 13a: Trading 212 export upload (optional)

The owner decided on 2026-11-23 to import the export as well as the API, and to keep it optional (design §3a, §5.3a). **Nothing may depend on it.** Without an upload, Trading 212 syncs, posts, reconciles and pairs in full, and no warning or notification asks for one.

**Files:**
- Create: `trading212/export.py`, `migrations/versions/00D_finance_trading212_export.py`, `tests/finance/trading212/test_export.py`, `tests/api/test_trading212_export_api.py`
- Modify: `trading212/normalize.py`, `trading212/fields.py` (`EXPORT_REQUIRED`, `EXPORT_KNOWN`), `connections_api.py`, `schemas.py`, `ConnectionsPage.tsx` (+ test), `web/openapi.json`, `web/src/api/*`

**Interfaces:**
- Consumes: Tasks 5 and 9 (`source_reviews`, `decision_for`); the CSV parsing of the M2 importer is not reused (its profile model is for bank statements).
- Produces:
  - `@dataclass(frozen=True) class ExportRow(key: str, cells: Mapping[str, str])`;
  - `def parse_export(body: bytes) -> tuple[tuple[ExportRow, ...], frozenset[str]]`, which returns the rows and the flagged columns. It reads UTF-8 with an optional BOM. Required columns: `Action`, `Time (UTC)`, `ID`, `Total`, `Currency (Total)`. A missing one raises `ExportError` naming it. The key is `ID`.
  - `async def ingest_export(ctx: SyncContext, body: bytes, filename: str) -> IngestResult`. It stores the document (`source = "finance.trading212"`) and raw records `trading212.export_row`, then normalises.
  - `POST /api/v1/connections/{id}/trading212-export` `upload_trading212_export`. It takes a CSV of at most 16 MiB and returns 202. It returns 409 before the first sync, and 422 when an `ID` the API knows belongs to another account. The file input on a Trading 212 connection reads "Upload Trading 212 export (CSV, optional)".
- The table `finance.trading212_export_rows` has: connection, `ID`, the transaction it enriches (nullable), one column per known column (Decision 3), and `notes_transaction_id` parsed from `Notes` matching `^Transaction ID: (\S+)$`.

**Rules:** design §5.3a.
- A row whose `ID` is an API transaction `reference` enriches that record.
- A deposit or withdrawal row the API lacks posts as `transaction:<ID>`. The sign comes from `Action`, `Total` is the amount, and the key is the same one the API would use, so a later API row only enriches it.
- Other actions are stored and flagged (`Trading 212 flagged: export action <Action>`), and post nothing.
- A disagreement on a posting value becomes a review item (Decision 14). There, `take: "statement"` means the export.

- [ ] **Step 1: Write the failing tests:**
  - `test_parse_export_columns`: the audited header; a BOM; a missing `ID` names it; an extra column with a value is flagged.
  - `test_notes_transaction_id_typed`.
  - `test_export_row_enriches_api_deposit`: the postings are unchanged, and the export row links the transaction.
  - `test_export_only_deposit_posts_then_api_enriches`: the same ledger as API-first.
  - `test_trade_row_flagged_not_posted`.
  - `test_upload_twice_writes_nothing`.
  - `test_other_accounts_ids_refused_422`.
  - `test_works_without_export`: a full Trading 212 history synced with no upload gives no export warning, flag or notification, and reconciles clean.
  - vitest `uploads a Trading 212 export`.
- [ ] **Step 2: Run** `uv run pytest tests/finance/trading212/test_export.py tests/api/test_trading212_export_api.py -q`; expected: FAIL.
- [ ] **Step 3: Implement**, then `make openapi`.
- [ ] **Step 4: Run** `make check`; expected: green.
- [ ] **Step 5: Commit** `feat: import Trading 212 exports alongside the API, optionally`.

## Phase D: transfers

### Task 14: Cross-currency pairing and the conversion posting

**Files:**
- Create: `tests/finance/transfers/test_cross_currency.py`
- Modify: `transfers/pairs.py`, `transfers/engine.py`, `transfers/contra.py` (load the leg fee)

**Interfaces:**
- Consumes: `transfer_leg_fees` (Decision 15; written by Task 11).
- Produces:
  - `def cross_currency_tier(r: Pairable, c: Pairable) -> Tier | None`: MUTUAL when both are identified naming each other's account, opposite signs, both instruments `fiat` and different; else None;
  - `_cross_currency_edges` in `pair_relation`, used only when each record is the other's only cross-currency candidate in the window (else `several_candidates`);
  - `async def _post_conversion(ctx, sending: OwnRecord, receiving: OwnRecord, fee: Decimal, system: _System) -> UUID` per design §5.12, origin `rule`, status `booked`, voided when the pair is undone;
  - `Pairable.fiat: bool`.

- [ ] **Step 1: Write the failing tests:**
  - `test_gbp_to_usd_with_fee`: eToro Money −1000.00 GBP (identified → Trading), Trading +1260.00 USD (identified → Money), leg fee 6.30 USD → matched; the conversion posts In transit −1000.00 GBP, +1266.30 USD with `price_per_unit` 1.2663 USD, and −6.30 USD `is_fee` against Unassigned +6.30 USD; In transit is 0 in GBP and in USD.
  - `test_usd_to_gbp_with_fee`: Trading −1000.00 USD, leg fee 5.00 USD, eToro Money +790.00 GBP → the conversion posts In transit −995.00 USD, +790.00 GBP with `price_per_unit` 790/995 GBP, and −5.00 USD `is_fee`; In transit is 0 in both.
  - `test_no_fee_row_fee_zero`.
  - `test_conversion_waits_for_statement` (Review Focus): the GBP leg alone for 3 days → In transit holds it; net worth (valued with a fixed GBP/USD rate) equal on every day until the USD leg arrives; afterwards it differs by exactly the fee and the rate effect.
  - `test_two_candidates_left_to_owner`.
  - `test_same_currency_rules_unchanged`: the existing transfer test suite passes untouched.
  - `test_unpair_voids_conversion`.
- [ ] **Step 2: Run** `uv run pytest tests/finance/transfers/test_cross_currency.py -q`; expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make backend-check`; expected: green.
- [ ] **Step 5: Commit** `feat: pair transfers across currencies with a conversion posting`.

### Task 15: Card merchants in the registry, and Monzo naming them

**Files:**
- Create: `migrations/versions/00C_finance_card_merchants.py`, `tests/finance/transfers/test_card_merchants.py`
- Modify: `identifiers.py`, `transfers/contra.py`, `transfers/pairs.py` (`Pairable.holders: frozenset[UUID]`), `transfers/queries.py` (suggestions), `monzo/normalize.py` (`NORMALIZER_VERSION = "monzo-norm-5"`), `transfers_api.py`

**Interfaces:**
- Produces:
  - `async def register_card_merchant(ctx, merchant_id: str, account_ids: Sequence[UUID]) -> IdentifierRecord` (all accounts own, asset, not snapshots-mode; replaces the holder set);
  - Monzo's normalizer sets `counterparty_identifier_id` to the `card_merchant` row when a card payment's merchant id is registered (it is resolved on every normalize, so registering later re-identifies on the next sync's replay);
  - `transfer_contra` sends a record whose identifier has holders other than its own account to In transit;
  - a suggestion kind `card_merchant` in `GET /transfer-suggestions`: a Monzo merchant id with ≥ 1 card payment whose merchant website's host ends with a connected platform's domain (`trading212.com`, `etoro.com`), offering that connection's accounts; `POST /accounts/identifiers/card-merchants` `{merchant_id, account_ids}` registers.

- [ ] **Step 1: Write the failing tests:** `test_register_two_holders`; `test_snapshots_account_refused_422`; `test_monzo_card_payment_identified_after_register` (replay re-identifies an earlier payment); `test_card_payment_goes_to_in_transit`; `test_suggestion_offered_for_trading212_merchant`; `test_other_kinds_still_single_holder`.
- [ ] **Step 2: Run** `uv run pytest tests/finance/transfers/test_card_merchants.py -q`; expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make backend-check`; expected: green.
- [ ] **Step 5: Commit** `feat: register card merchants that pay into own accounts`.

### Task 16: Card deposits paired by time

**Files:**
- Create: `tests/finance/transfers/test_card_deposits.py`
- Modify: `transfers/pairs.py`, `transfers/settings.py` (Decision 16)

**Interfaces:**
- Produces: `def card_tier(r: Pairable, c: Pairable, window: timedelta) -> Tier | None`: ONE_SIDED when one side's holders include the other's account, the other is not identified, same instrument, opposite equal amounts, and `|occurred_at|` gap ≤ `window`; used only when unique on both sides.

- [ ] **Step 1: Write the failing tests:**
  - `test_two_card_deposits_paired_by_time` (Review Focus): Monzo −50.00 at 10:00:00 and −50.00 at 10:08:32 to a merchant held by Invest and ISA; Invest DEPOSIT +50.00 at 10:00:20, ISA +50.00 at 10:08:50 → each matched to its own, In transit 0.
  - `test_card_deposits_too_close_left_to_owner`: the second payment at 10:01:00 → both `several_candidates`.
  - `test_amount_differs_not_paired`.
  - `test_refund_pairs_in_reverse`.
  - `test_outside_window_not_paired`.
- [ ] **Step 2: Run** `uv run pytest tests/finance/transfers/test_card_deposits.py -q`; expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make backend-check`; expected: green.
- [ ] **Step 5: Commit** `feat: pair card deposits into broker accounts by time`.

### Task 17: Owner pairing across currency or amount

**Files:**
- Create: `tests/finance/transfers/test_owner_pair_differs.py`
- Modify: `transfers/decisions.py` (`pair_records`, `pair`), `transfers_api.py`, `transfers/queries.py` (pair candidates)

**Interfaces:**
- Produces: `pair_records(ctx, transaction_id, other_id)` and `pair(ctx, transfer_id, transaction_id)` accept two fiat records of opposite direction that differ in instrument (conversion with fee 0) or in amount in one instrument (the difference a fee on the sending side's In transit leg against Unassigned), posted by `_post_conversion` (Task 14) generalised to `fee_instrument`; `GET /transactions/{id}/pair-candidates` lists such records after the equal ones, marked `differs: "currency" | "amount"`.

- [ ] **Step 1: Write the failing tests:** `test_owner_pairs_usd_withdrawal_with_gbp_receipt`; `test_owner_pairs_net_of_fee_same_currency` (−100.00 and +99.30 → fee 0.70); `test_non_fiat_refused_422`; `test_same_direction_refused_422`; `test_unpair_voids_conversion`; `test_candidates_mark_differs`.
- [ ] **Step 2: Run** `uv run pytest tests/finance/transfers/test_owner_pair_differs.py -q`; expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make backend-check`; expected: green.
- [ ] **Step 5: Commit** `feat: let the owner pair records that differ in currency or amount`.

## Phase E: invariants, docs, the owner's check

### Task 18: Property tests

**Files:**
- Create: `tests/finance/etoro/strategies.py`, `test_properties.py`, `tests/finance/trading212/strategies.py`, `test_properties.py`
- Use the shared Hypothesis settings in `tests/finance/conftest.py`.

**Properties** (design §10): for a generated consistent eToro history (opens, closes, partial closes, transfers both ways with and without fees, dividends, overnight fees, withdraw request and cancel), split into API rows and statements uploaded as random overlapping pieces in random order: `test_ledger_independent_of_order`, `test_every_transaction_balances`, `test_no_reconciliation_difference`, `test_renormalize_writes_nothing`, `test_regenerate_from_raw` (also with a resolved review decision). For Trading 212, history walked in random page splits with repeats: the same five.

- [ ] **Step 1: Write the strategies and the property tests.**
- [ ] **Step 2: Run** `uv run pytest tests/finance/etoro/test_properties.py tests/finance/trading212/test_properties.py -q`; expected: PASS, or a minimised counterexample; fix its root cause in the owning module and add a regular test there.
- [ ] **Step 3: Run** `make backend-check`; expected: green.
- [ ] **Step 4: Commit** `test: pin eToro and Trading 212 normalization invariants with property tests`.

### Task 19: Docs, real-check and the owner's check

**Files:**
- Modify: `README.md`, `README.zh-CN.md` (in step), `Makefile` (`real-check` takes `ETORO_STATEMENT=<path>` and runs the statement parser and an in-memory normalize on it, printing counts only), the M6 design's status line.

**README, both languages, sections "Connecting Trading 212" and "Connecting eToro":** creating the keys (Trading 212: per account, read permissions, `orders:execute` and `pies:write` off; eToro: Real, Read); what each connection creates; registering eToro Money's bank details and the Trading 212 card merchant (Suggestions); downloading and uploading the eToro statement, and that the first must start by the account's opening; what "upload an eToro statement" and review items mean; the limits (no MoneyFarm holdings; no CFDs or copy trading; Trading 212 bank deposits paired by amount, or by hand).

- [ ] **Step 1: Write the failing test:** `tests/test_makefile.py::test_real_check_accepts_etoro_statement` (the target passes the variable through).
- [ ] **Step 2: Run** it; expected: FAIL.
- [ ] **Step 3: Implement**, and write both READMEs.
- [ ] **Step 4: Run** `make check`; expected: green.
- [ ] **Step 5: Commit** `docs: describe connecting eToro and Trading 212`.
- [ ] **Step 6: The owner's check** (not committed; outcomes recorded in the PR as pass/fail per item, no values). After deploying, the owner: creates the three connections; compares each account's positions and cash with each app; uploads the eToro statement and checks the transfers between eToro Money and Trading are matched with their fees; registers eToro Money's bank details and the Trading 212 merchant, and checks the Monzo → eToro Money payments and the two Apple Pay deposits are matched to the right accounts; runs `make real-check ETORO_STATEMENT=…`. After the first Trading 212 trade (and first Invest ↔ ISA transfer), reruns `private/m6-probe/probe.py` and confirms the sign rules of Task 5; a difference is a `fix:` commit with synthetic data.

## Self-review notes

- **Design coverage:**

  | Design section | Task(s) |
  |---|---|
  | §5.1 connections and keys | 2, 3, 6, 7, 9, 13 |
  | §5.2 fetching | 3, 4, 7, 9 |
  | §5.3 statement upload | 8, 9, 13 |
  | §5.3a Trading 212 export | 13a |
  | §5.4 storage | 3, 9, 13a |
  | §5.5 accounts | 5, 10 |
  | §5.6 instruments | 5, 10, 11 |
  | §5.7 records | 5, 10, 11 |
  | §5.8 time | Task 0, Decision 11 |
  | §5.9 typed fields | 1, 3, 9 |
  | §5.10 prices | 5, 10 |
  | §5.11 reconciliation | 5, 12 |
  | §5.12 transfers | 10, 11, 14, 15, 16, 17 |
  | §5.13 notifications | 4, 5, 9, 11, 12 |
  | §5.14 API and UI | 6, 13, 15, 17 |
  | §6 edge cases | tests in Tasks 3–17 |
  | §8 Task 0 | Task 0 |
  | §10 verification | 18, 19 |

- **Types:** `ReadOnlyClient` (Task 2) is used by Tasks 3 and 7; `typed_values`/`upsert_typed` (Task 1) by Tasks 5, 10, 11; `ingest_statement` (Task 9) by Task 13; `source_reviews` (Task 9) by Tasks 11 and 13; `transfer_leg_fees` (created in `00B` by Task 9, written by Task 11, read by Task 14); `_post_conversion` (Task 14) is reused by Task 17; `Pairable.holders` (Task 15) by Task 16.
- **Gate:** Tasks 8–12 depend on Task 0's remaining findings (zone, decimals, check 4's rule); the others do not (Decision 11a).
