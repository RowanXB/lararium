# Structured Monzo Fields Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every Monzo field the owner chose (tiers 1 and 2) is stored as a typed field, for future syncs and for the history already in `core.raw_records`.

**Architecture:** One migration adds typed columns to `finance.transactions`, `finance.accounts` and `core.entities`, plus `finance.transaction_category_splits` and `finance.declined_transactions`. The Monzo normalizer fills them. Bumping `NORMALIZER_VERSION` makes the next sync replay the latest raw record of every Monzo transaction it fetched through the normalizer, which never posts one the ledger does not already hold: history is backfilled with the same code that handles new syncs.

**Tech Stack:** Python 3.13, FastAPI, async SQLAlchemy, PostgreSQL 18, raw-SQL Alembic migrations, pytest.

**Spec:** this document (the owner approved the field placement in conversation, 2026-11-15); data rule in `docs/superpowers/specs/2026-11-11-financial-gate-phase1-design.md` (§6, "Fees are a separate posting"; §5, merchants are entities).

## Decisions (approved by the owner, 2026-11-15)

1. **The owner's rule.** Every field a source provides is stored typed, unless it is derivable or has a better structured home. Raw copies don't count.
2. **Not in this plan:**
   - The owner's own sort code and account number belong to M4's registry (`ensure_identifier`). The counterparty name, sort code and account number, and the payment reference, belong to M4.
   - IBAN/BIC (`payment_details.iban`) and account owners other than the owner (joint accounts) wait until they are needed. Today every account has one owner, the owner, which is `owner_entity_id` already.
3. **Never copied anywhere:** `metadata.device_fingerprint` and `metadata.ip_address_attempt`. They are security data about the owner's device and network, not finance, and spreading them widens what a leak exposes. They stay in the raw records.
4. **Left in the raw records** (Monzo-internal plumbing): card, FPS and dedupe ids, hold decisions, ledger timestamps, feature flags, `can_*`, `merchant.emoji`, `merchant.suggested_tags`, `merchant.disable_feedback`, `address.short_formatted`, `address.zoom_level`.
5. **Transactions** gain:
   - `original_quantity` and `original_instrument_id`, from `local_amount` and `local_currency`. They are signed like the account's own posting (Monzo's `local_amount` has the sign of `amount`), and stored always, even when they equal the posted amount.
   - A currency Lararium has no instrument for leaves both NULL and raises an unmapped-field warning (Decision 10), so a sync never fails on an exotic currency. (This changes the earlier "store the code as text" idea: amounts in minor units need the currency's decimals, which only the instrument knows.)
   - `category` (Monzo's category, or a custom category's id as given).
   - `labels text[]` (empty when null).
   - `include_in_spending` and `payment_scheme` (Monzo's `scheme`).
   - `place_entity_id` (Decision 6).
6. **Merchants: one place per Monzo merchant id.**
   - Lararium merges merchant organizations by name, and one name can cover several Monzo merchant ids with different addresses, logos and `online` flags (seen in the owner's data).
   - So each Monzo merchant id gets a `core.entities` row of kind `place`, tagged with the external id `monzo_merchant`. It holds:
     - `address_street`, `address_city`, `address_region`, `address_postcode`, `address_country` and `address_formatted`;
     - `latitude` and `longitude`, as `numeric` built with `Decimal(str(x))`, never a binary fraction;
     - `location_approximate` and `online`;
     - `mcc` (from the transaction's `metadata.mcc`), `website` (from `merchant.metadata.website`) and `card_acceptor_website` (from `metadata.card_acceptor_website`);
     - `logo_url` (from `merchant.logo`).
   - The brand organization stays as today. A transaction links both: `counterparty_entity_id` (the brand) and `place_entity_id`.
   - Every transaction records its merchant's place first, whatever the ledger makes of it (posted, declined, or reversed to amount 0), so every Monzo merchant id the source shows gets its place. A place is identified by the merchant id alone: one without a readable name is named by its id, and only the brand organization, known by its name, is skipped.
   - Each sync writes each place once, before any transaction is applied, from everything that sync's transactions (the replay's included) say about the merchant, name included: folded by statement time (Monzo's `updated`, else `created`), each value a newer statement states replaces an older one, and one no newer statement states is kept. So neither the order the sync meets them in nor how many disagree decides, and an unchanged sync writes nothing. Accepted limit: the statement time is not stored, so across syncs a value only stated in an older transaction refetched later can replace a newer stored one; a sync's window always holds a merchant's newest transactions, so this needs Monzo to change an old transaction outside it.
   - The organization lookup filters on `kind = 'organization'`, so the shared external-id scheme does not collide.
7. **Category splits** go in `finance.transaction_category_splits (transaction_id, position, category, quantity)`. They are written only when `categories` is a real split: more than one entry, or one entry other than `{category: amount}`. Otherwise the split is derivable.
8. **Fees are separate postings** (spec §6; M2 uses the same convention).
   - Monzo's `amount` is what reached the balance. `fees` maps a kind to a signed amount in minor units (a charge is negative).
   - Postings, in order: the account `amount − Σfees`, the contra `−amount`, then one posting on the account per non-zero fee, `is_fee = true`, with the fee kind as its memo. The contra stays at position 1, where M4 expects it.
   - Example: a cash deposit `amount 12300, fees {"deposit.cash": -100}` posts `+124.00`, `−123.00`, then `−1.00` (fee).
9. **Accounts and pots** gain `product_type`, `owner_type`, `goal_amount`, `locked`, `round_up` and `round_up_multiplier`, and fill the existing `tax_wrapper`.
   - Account: `product_type` and `owner_type`.
   - Pot:
     - `product_type` is the pot's `type`;
     - `goal_amount` is in the pot's currency;
     - `locked`, `round_up` and `round_up_multiplier`;
     - `tax_wrapper` comes from `isa_wrapper`: `""` gives `none`, a known value (case-insensitive, in the allowed set) gives that value. A missing key or an unknown value leaves the column unchanged, and an unknown value raises the `pot.isa_wrapper` warning of Decision 10, so a sync never fails on it.
   - They are updated on every sync, written only when changed, and audited.
10. **Unmapped fields warn instead of failing.** Some field shapes have no sample in the owner's data: `atm_fees_detailed` is always null, `labels` always null, and a currency may have no instrument. When one of these turns up non-null, or in an unexpected shape, the connection gets a warning that stays until the owner clears the warnings: `Monzo field <name> is not stored yet; it stays in the raw records`. The rest of the transaction is stored as usual.
    - Every typed field is read through one reader (`_Reader` in `normalize.py`): absent, null and `""` read as None; a value of another type reads as None too and warns with its path in the source (`category`, `merchant.address.city`, `pot.goal_amount`, …), so a malformed value is never taken for an absent one. A `decline_reason` that is stated in any shape makes the transaction a decline, never posted; one it cannot read is stored as `""` with the `decline_reason` warning. What None then means is each store's own rule: a transaction's details are replaced with each copy, a place keeps the stored detail, and an account or pot keeps the field (`SourceDetails.unknown`), as for an unknown tax wrapper.
11. **Declined transactions** go in `finance.declined_transactions`, outside the ledger. It holds the connection, the Monzo id (unique per connection), the account, `occurred_at`, the signed quantity and instrument, the description, `counterparty_entity_id`, `place_entity_id` and `decline_reason`. A re-sync upserts and writes nothing when unchanged. A pending transaction that comes back declined is voided in the ledger, as today, and gets its declined row.
12. **Backfill by replay.**
    - `NORMALIZER_VERSION` becomes `monzo-norm-2`, kept in the connection config under `normalizer_version`.
    - When the stored value differs, `normalize` first runs the latest raw record of every Monzo transaction this connection fetched through `transaction()`, before this sync's own pages and skipping the ids those pages contain. Then it stores the version. `transaction()` decides what each copy is (posted, reversed or declined) exactly as for a sync; the replay only forbids posting one the ledger does not already hold, so a transaction seen only in a discarded fetch attempt is never posted by it, while its merchant's place and a decline are still recorded.
    - "Latest" means the greatest Monzo `updated`, with ties broken by `fetched_at` and then the record id. `fetched_at` alone would be wrong, because a record whose content was seen before keeps its first fetch time. An `updated` that is not a valid ISO timestamp counts as missing (oldest), so it can neither fail the sync nor win.
    - The migration's downgrade removes `normalizer_version`, so a later upgrade replays again.
    - The replay goes through the normal upserts, so an owner's void survives. A replay only writes what the code it runs knows, so each change that needs history rewritten must bump `NORMALIZER_VERSION` again once it lands: M4 (PR #14) also uses `monzo-norm-2`, and PR #10 (pot names) none. Whichever of them merges after this PR bumps it to the next unused value (`monzo-norm-3`, …) and updates the version tests; until then M4 keeps its `counterparty_fields_v1` backfill.
13. **Migration number.** `0013`, revising `0012` (main today). The PR is renumbered after M4 (0013/0014) and M2 (0013–0015) land, whichever merges first.
14. **API and UI are unchanged** in this PR (a follow-up shows the fields).

## Global Constraints

- Tests use synthetic data only: invented merchant names, ids and amounts. No real names, amounts or account numbers in code, tests, commits or the PR.
- No `float` anywhere in `backend/src` (`tests/test_no_floats.py`). JSON numbers become `Decimal(str(value))`, after rejecting `bool`.
- Docs, code comments and commit messages are in English. Commits are signed, `<type>: <summary>`.
- Raw SQL in the migration, in the style of `0012_finance_voided_by.py` (an `UPGRADE`/`DOWNGRADE` statement list). New tables get their grants from the default privileges in `0001`.
- Every `numeric` column has `CHECK (x NOT IN ('NaN', 'Infinity', '-Infinity'))`, as in `0004`.

## Review Focus

1. **Fixtures without the new keys.** A transaction without `fees`, `categories`, `scheme`, `labels`, `include_in_spending` or `local_*`, as in today's `make_transaction`, still posts, with NULL/empty fields and no fee posting (Task 5: `test_transaction_without_new_keys_still_posts`).
2. **A re-sync writes nothing.** Re-normalizing the same records reports everything `unchanged`, emits no `finance.*` events and leaves declined rows and places untouched (Task 5: `test_resync_with_details_writes_nothing`).
3. **The owner's void survives the replay** (Task 6: `test_replay_keeps_owner_void`).
4. **A pending transaction that comes back declined** is void in the ledger and has a declined row (Task 5: `test_pending_then_declined_is_voided_and_recorded`).
5. **A local currency with no instrument** (e.g. `XTS`) posts the transaction with NULL original fields and the warning (Task 5: `test_unknown_local_currency_warns`).

---

### Task 1: Schema

**Files:**
- Create: `backend/migrations/versions/0013_structured_source_fields.py`
- Modify: `backend/src/lararium/modules/finance/models.py`, `backend/src/lararium/core/models.py`
- Test: `backend/tests/finance/test_structured_schema.py`

**Interfaces:**
- Produces:
  - ORM columns as in Decisions 5, 6 and 9: `Transaction.original_quantity`, `original_instrument_id`, `category`, `labels`, `include_in_spending`, `payment_scheme` and `place_entity_id`; `Account.product_type`, `owner_type`, `goal_amount`, `locked`, `round_up` and `round_up_multiplier`; the twelve `Entity` place columns.
  - Models `TransactionCategorySplit` and `DeclinedTransaction`.
- Constraints:
  - `CHECK ((original_quantity IS NULL) = (original_instrument_id IS NULL))`;
  - `mcc ~ '^[0-9]{4}$'`;
  - `labels text[] NOT NULL DEFAULT '{}'`;
  - splits: `PRIMARY KEY (transaction_id, position)`, with `transaction_id` `ON DELETE CASCADE`;
  - declined: `UNIQUE (connection_id, external_id)`, `owner_entity_id NOT NULL`, `recorded_at` and `updated_at` default `now()`.

- [ ] **Step 1: Write the failing tests:**
  - `test_original_amount_needs_both_columns`: an UPDATE setting only `original_quantity` raises `IntegrityError`;
  - `test_mcc_must_be_four_digits`;
  - `test_declined_unique_per_connection`;
  - `test_numeric_columns_reject_nan`: `goal_amount = 'NaN'` raises;
  - `test_downgrade_then_upgrade`: follow the pattern in `tests/infra/test_migrations_env.py` or `tests/core/test_baseline.py`, whichever already runs migrations.
- [ ] **Step 2: Run** `cd backend && uv run pytest tests/finance/test_structured_schema.py -q`. Expected: FAIL (columns missing).
- [ ] **Step 3: Write the migration and ORM models.**
- [ ] **Step 4: Run the tests again.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: add typed columns and tables for structured source fields`.

### Task 2: Ledger transaction details

**Files:**
- Modify: `backend/src/lararium/modules/finance/ledger.py`
- Test: `backend/tests/finance/test_connector_ledger.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class CategorySplit: category: str; quantity: Decimal`.
  - `@dataclass(frozen=True) class TransactionDetails` with these fields, all defaulting to `None` or `()`:
    - `original_quantity: Decimal | None`
    - `original_instrument_id: UUID | None`
    - `category: str | None`
    - `labels: tuple[str, ...]`
    - `include_in_spending: bool | None`
    - `payment_scheme: str | None`
    - `place_entity_id: UUID | None`
    - `category_splits: tuple[CategorySplit, ...]`
  - `TransactionInput.details: TransactionDetails = TransactionDetails()`.
  - `TransactionRecord.details: TransactionDetails` is loaded, and `_change_data` includes it, with Decimals as plain strings.
- Behaviour:
  - `post_transaction` writes the details and the splits (positions 0…n).
  - `upsert_connector_transaction` compares `details` (splits included) along with the other fields. A difference is an update that rewrites the columns and replaces the splits.
  - Validation: a split's quantity is finite; `original_quantity` and `original_instrument_id` are both set or both None, else `FinanceValidationError`.

- [ ] **Step 1: Write the failing tests:**
  - `test_details_are_written_and_loaded`;
  - `test_changed_details_update_in_place` (same id, outcome `updated`, splits replaced);
  - `test_same_details_are_unchanged` (outcome `unchanged`, no event);
  - `test_original_amount_needs_both_fields`.
- [ ] **Step 2: Run** `uv run pytest tests/finance/test_connector_ledger.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement the details in `ledger.py`.**
- [ ] **Step 4: Run the tests again.** Expected: PASS, with the rest of `tests/finance` still green.
- [ ] **Step 5: Commit** `feat: give ledger transactions typed source details`.

### Task 3: Places, account details and declined transactions

**Files:**
- Modify: `backend/src/lararium/core/entities.py`, `backend/src/lararium/modules/finance/accounts.py`
- Create: `backend/src/lararium/modules/finance/declined.py`
- Test: `backend/tests/core/test_entities.py`, `backend/tests/finance/test_accounts.py`, `backend/tests/finance/test_declined.py`

**Interfaces:**
- Produces:
  - `core.entities`:
    - `@dataclass(frozen=True) class PlaceDetails`, with the twelve fields of Decision 6, all defaulting to `None`;
    - `async def get_or_create_place(ctx, name: str, *, external_id: tuple[str, str], details: PlaceDetails) -> UUID`. It is serialised on `pg_advisory_xact_lock(hashtext('place:' + scheme + ':' + value))`. It finds a `kind='place'` row by external id, or creates it. It updates `name` and the details when they differ, recording the change as `entity:<id>`.
  - `finance.accounts`:
    - `@dataclass(frozen=True) class SourceDetails` with these fields, all defaulting to `None`:
      - `product_type: str | None`
      - `owner_type: str | None`
      - `goal_amount: Decimal | None`
      - `locked: bool | None`
      - `round_up: bool | None`
      - `round_up_multiplier: Decimal | None`
      - `tax_wrapper: TaxWrapper | None` (None leaves the column as it is)
    - `async def set_source_details(ctx, account_id: UUID, details: SourceDetails) -> bool`. It writes only when something differs, publishes `finance.account.updated`, and returns whether it wrote. `AccountRecord` and `_change_data` gain the six new fields.
  - `finance.declined`:
    - `@dataclass(frozen=True) class DeclinedInput`, with these fields:
      - `connection_id: UUID`
      - `external_id: str`
      - `account_id: UUID`
      - `occurred_at: datetime`
      - `quantity: Decimal`
      - `instrument_id: UUID`
      - `description: str`
      - `counterparty_entity_id: UUID | None`
      - `place_entity_id: UUID | None`
      - `decline_reason: str`
    - `async def upsert_declined(ctx, d: DeclinedInput) -> Literal["created", "updated", "unchanged"]`. It uses `INSERT … ON CONFLICT (connection_id, external_id) DO UPDATE … WHERE (columns) IS DISTINCT FROM (excluded)`, sets `updated_at = now()` on an update, and records `declined_transaction:<id>` when it writes.

- [ ] **Step 1: Write the failing tests:**
  - `test_place_created_then_found_by_external_id`;
  - `test_place_details_updated_when_changed`;
  - `test_place_not_confused_with_organization` (an organization and a place with the same external id stay two rows);
  - `test_set_source_details_writes_once` (a second identical call returns False, with no event);
  - `test_set_source_details_keeps_tax_wrapper_when_none`;
  - `test_upsert_declined_created_unchanged_updated`.
- [ ] **Step 2: Run** `uv run pytest tests/core/test_entities.py tests/finance/test_accounts.py tests/finance/test_declined.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement the three functions.**
- [ ] **Step 4: Run the tests again.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: add places, account source details and declined transactions`.

### Task 4: Fixtures for the new Monzo fields

**Files:**
- Modify: `backend/tests/finance/monzo/fixtures.py`

**Interfaces:**
- Produces: `make_merchant(**overrides) -> dict[str, Any]`. It returns an invented merchant with `id`, `group_id`, `name`, `logo`, `online`, `category`, `metadata={"website": ...}` and an `address` with the eleven Monzo keys, including the numbers `latitude` and `longitude`. `make_pot` gains `type`, `isa_wrapper=""`, `locked`, `round_up`, `round_up_multiplier=None` and an optional `goal_amount`. `make_account` gains `product_type="standard"` and `owner_type="personal"`. `make_transaction` stays without the new keys (Review Focus 1).

Folded into Task 5's first commit; no tests of its own.

### Task 5: The Monzo normalizer fills the fields

**Files:**
- Modify: `backend/src/lararium/modules/finance/monzo/normalize.py`
- Test: `backend/tests/finance/monzo/test_normalize.py`

**Interfaces:**
- Consumes: Tasks 2–4.
- Behaviour (Decisions 5–11):
  - **`_Normalizer.transaction`:**
    - builds `TransactionDetails` from the transaction, and links the place that `prepare_places` wrote for its `merchant.id` (Decision 6);
    - splits the fees into postings (Decision 8);
    - for a declined transaction, calls `upsert_declined` after `void_if_posted`.
  - **`account()` and `pot()`** call `set_source_details` on every sync, mapped or new.
  - **Unmapped fields** are collected in `_Normalizer.unmapped: set[str]`. `normalize` appends one warning per field (Decision 10's text) to the connection's warnings, unless that warning is already there. It never removes them.
- `NormalizeReport` keeps `skipped_declined` (declined, not posted).

- [ ] **Step 1: Write the failing tests:**
  - `test_transaction_details_are_stored`: a card payment with an invented merchant, `local_amount -1000 USD`, `scheme "mastercard"`, `include_in_spending true` and `metadata.mcc "5411"` stores the original `-10.00` USD, the category, the scheme and the place; the place holds the address fields, a latitude equal to `Decimal("51.5")` and the MCC.
  - `test_two_merchant_ids_one_name_two_places`: one organization, two places.
  - `test_cash_deposit_fee_is_a_separate_posting`: `amount 12300`, `fees {"deposit.cash": -100}` gives postings `124.00`, `-123.00` and `-1.00` (`is_fee`, memo `deposit.cash`); the account balance is `123.00`.
  - `test_split_categories_stored_only_when_split`.
  - `test_declined_recorded_not_posted`: a declined row with its reason; no ledger row.
  - `test_pending_then_declined_is_voided_and_recorded`.
  - `test_account_and_pot_details`: the pot's `isa_wrapper "ISA"` gives `tax_wrapper 'isa'`, and `goal_amount 100000` gives `1000.00`.
  - `test_unknown_isa_wrapper_warns`: the column is left as it is and the `pot.isa_wrapper` warning is raised.
  - `test_atm_fees_detailed_warns_once`: two syncs leave one warning.
  - `test_unknown_local_currency_warns`.
  - `test_transaction_without_new_keys_still_posts`.
  - `test_resync_with_details_writes_nothing`.
  - `test_device_fingerprint_and_ip_never_stored`: a transaction whose metadata has `device_fingerprint` and `ip_address_attempt` leaves neither value in any `finance.*` or `core.entities` column. Check with a text search over `row_to_json` of those tables.
- [ ] **Step 2: Run** `uv run pytest tests/finance/monzo -q`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement the mapping in `normalize.py`.**
- [ ] **Step 4: Run** `uv run pytest tests/finance -q`. Expected: PASS, the existing Monzo tests included.
- [ ] **Step 5: Commit** `feat: store Monzo transaction, merchant, account and pot fields typed`.

### Task 6: Backfill by replay

**Files:**
- Modify: `backend/src/lararium/core/raw_records.py`, `backend/src/lararium/modules/finance/monzo/normalize.py`
- Test: `backend/tests/core/test_raw_records.py` (create if missing), `backend/tests/finance/monzo/test_normalize.py`

**Interfaces:**
- Produces:
  - `async def list_raw_records(session, connection_id: UUID, record_type: str) -> list[RawRecord]`, ordered by `fetched_at, id`.
  - In `normalize.py`: `NORMALIZER_VERSION = "monzo-norm-2"` and `NORMALIZER_VERSION_KEY = "normalizer_version"`, with the replay of Decision 12. The version is written with the other config keys, in the same `update_config` call.

- [ ] **Step 1: Write the failing tests:**
  - `test_replay_fills_history_outside_this_sync`: a raw page stored by an earlier sync, for a transaction already posted without details, with no `normalizer_version` in the config. A sync whose pages do not include it fills the details, and the config then holds `monzo-norm-2`.
  - `test_replay_uses_the_latest_by_updated`: the same id in two records, the older `updated` fetched later, so the newer `updated` wins.
  - `test_replay_runs_once`: a second sync replays nothing (report counts).
  - `test_replay_keeps_owner_void`.
  - `test_replay_records_old_declines`.
- [ ] **Step 2: Run** `uv run pytest tests/finance/monzo/test_normalize.py -q -k replay`. Expected: FAIL.
- [ ] **Step 3: Implement `list_raw_records` and the replay.**
- [ ] **Step 4: Run** `make check` from the repository root. Expected: everything passes, the OpenAPI check included (the API is unchanged).
- [ ] **Step 5: Commit** `feat: backfill structured Monzo fields by replaying raw records`.

### Task 7: Docs and the deploy check

**Files:**
- Modify: `docs/superpowers/plans/2026-11-12-connectors-monzo.md` (one note under its normalizer decisions pointing here)

- [ ] **Step 1: Add the note:** structured fields, the replay on a `NORMALIZER_VERSION` change, and what stays raw.
- [ ] **Step 2: Commit** `docs: point the Monzo connector plan at the structured fields`.
- [ ] **Step 3: Owner's check after deploy (not committed).** After the first sync on the NAS, read-only counts must match the raw inventory:
  - every connector transaction except the opening balance adjustments, voided ones (by the owner or by the connector) and ones in a currency without an instrument has `original_instrument_id`;
  - places: the number of distinct merchant ids;
  - declined rows: the number of declined ids;
  - exactly one `is_fee` posting.
