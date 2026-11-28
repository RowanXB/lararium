# Income statistics with the owner's categories: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An Income page and an Overview card showing where income came from. The source states self-produced income kinds (interest, dividend, share lending, reward), coin rewards are valued in the reporting currency, and the owner sets categories kept apart from the source's marks.

**Architecture:** A typed `transactions.income_kind`, set by each source, plus a `transaction_owner_categories` table keyed by source reference. The cash-flow flows query becomes the single shared definition of a flow and of its effective category; the new income report and the existing cash-flow report both read it. The web gets an Income tab and a "Where it came from" card.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy async, PostgreSQL 18, Alembic, pytest + Hypothesis (testcontainers on `docker-test`); React 19, TanStack Query/Router, vitest, hey-api generated client.

**Spec:** `docs/superpowers/specs/2026-11-27-income-categories-design.md`. Read it before any task.

## Global Constraints

- The source's marks are never overwritten: `transactions.category` is never written by this work, and `income_kind` is written only by source code (normalizers, the import commit), never by the owner.
- Income kinds: exactly `interest`, `dividend`, `share_lending`, `reward`.
- Owner category normalisation: trim, collapse inner whitespace runs to `_`, lower-case; 1 to 64 characters after normalising, else `FinanceValidationError`.
- Effective category, in order: the owner's category (every flow of its transaction); else `income_kind` for a positive flow only; else the source's `category`; else none. An owner category replaces category splits.
- A non-fiat posting is a flow only when its transaction has an `income_kind`, of either sign. It is valued at the instrument's latest price on or before the booking day (`as_of_date desc, source = 'manual' desc, recorded_at desc`), times `finance.v1_fx_rate(price currency, reporting, booking day)`. Without a price or rate it is left out and counted in `missing_count`.
- Migration `0049`, revises `0048` (renumbered twice: #53 took 0047, #52 took 0048).
- `NORMALIZER_VERSION` bumps: `trading212-norm-5` (#52 took trading212-norm-4), `etoro-norm-6` (#53 took etoro-norm-5), `ibkr-norm-5`, `binance-norm-3`. Monzo is unchanged.
- Money is Decimal / Decimal strings, never floats. Tests and docs use invented amounts and names.
- UI copy is English. Docs English only.
- Tests: run only the new or changed test file (TDD red/green), plus `ruff check`, `ruff format --check`, `pyright`, `lint-imports` (backend) or `pnpm lint`, `pnpm typecheck` if present (web). Never run whole test directories: CI runs the rest. Backend tests run with `DOCKER_HOST=ssh://docker-test`.
- Commit only the files you changed, by explicit path; never `git add -A`. Commit titles `<type>: <summary>`.

## Review Focus

1. **A re-import of the same file after rollback keeps the owner's category.** The key is the dedupe key, so the mark is found again. Test in Task 4.
2. **The CSV is committed first and the PDF later, as duplicates.** The transaction still gets `interest`, and loses it when the PDF batch is rolled back. Test in Task 2.
3. **A coin reward on a day with no price yet** (today's reward, before the day's close is recorded) is counted in `missing_count`, not as zero. Test in Task 5.
4. **A BNB trade commission or a stock arriving from a corporate action never shows as income or spending** after non-fiat flows are admitted. Test in Task 5.
5. **The owner marks 500 entries at once, one of them voided meanwhile.** Nothing is written and the response names the void one. Tests in Task 4 (limit 500, void refused).

---

### Task 1: Schema and ledger field

**Files:**
- Create: `backend/migrations/versions/0047_finance_income_categories.py`
- Modify: `backend/src/lararium/modules/finance/models.py` (Transaction; new `TransactionOwnerCategory`)
- Modify: `backend/src/lararium/modules/finance/ledger.py` (`TransactionDetails`, `_load_transaction`, `_details_data`, `_details_values`)
- Test: `backend/tests/finance/test_ledger.py`, `backend/tests/finance/test_income_categories_migration.py`

**Interfaces:**
- Produces: `IncomeKind = Literal["interest", "dividend", "share_lending", "reward"]` in `ledger.py`; `TransactionDetails.income_kind: IncomeKind | None = None`; `models.Transaction.income_kind: Mapped[str | None]`; `models.TransactionOwnerCategory` with `source_kind`, `source_ref` (composite PK), `category`, `decided_at` (server default now), `decided_by`.

- [ ] **Step 1: Write the failing tests.** In `test_ledger.py`: `test_income_kind_round_trips` posts a transaction with `TransactionDetails(income_kind="interest")`, reloads it and asserts `record.details.income_kind == "interest"`; `test_upsert_updates_income_kind` upserts the same connector transaction first without, then with `income_kind="dividend"` and asserts the outcome `"updated"` and the stored value. In the migration test, mirroring `test_shared_payee_holders_migration.py`: at 0046, commit an import batch with one row `interest=true` and one `interest=false`, plus a later committed batch whose duplicate row (`duplicate_of` the false row) has `interest=true`. Upgrade to 0047 and assert both transactions get `income_kind = 'interest'`. Downgrade and assert the column and table are gone.
- [ ] **Step 2: Run them and see them fail.** `cd backend && DOCKER_HOST=ssh://docker-test uv run --locked pytest tests/finance/test_ledger.py -k income_kind tests/finance/test_income_categories_migration.py -q`. Expected: fail (no field / no revision).
- [ ] **Step 3: Write the migration.** Write it in 0046's style (module docstring explaining why, `UPGRADE`/`DOWNGRADE` lists):
  - `ALTER TABLE finance.transactions ADD COLUMN income_kind text CHECK (income_kind IN ('interest','dividend','share_lending','reward'))`.
  - `CREATE TABLE finance.transaction_owner_categories (source_kind text NOT NULL, source_ref text NOT NULL, category text NOT NULL CHECK (category <> '' AND length(category) <= 64 AND category = lower(category)), decided_at timestamptz NOT NULL DEFAULT now(), decided_by text NOT NULL, PRIMARY KEY (source_kind, source_ref))`.
  - Grant the app role the same privileges other finance tables get. Check `infra/postgres/roles.sql` and earlier migrations for the pattern.
  - Backfill: set `income_kind = 'interest'` on the transaction of every committed row (`status = 'committed'`) where that row, or any row with `duplicate_of` = it whose batch is committed, has `interest`.
- [ ] **Step 4: Add the model fields and `TransactionDetails.income_kind`.** Thread it through `_load_transaction`, `_details_data` and `_details_values`. `upsert_connector_transaction` already compares `details` as a whole, so a changed kind counts as an update.
- [ ] **Step 5: Run the tests and see them pass.** Same command. Then run `ruff`, `pyright` and `lint-imports`.
- [ ] **Step 6: Commit.** `feat: store a source's income kind and the owner's categories`.

### Task 2: Interest from statement imports

**Files:**
- Create: `backend/src/lararium/modules/finance/importing/income_kinds.py`
- Modify: `backend/src/lararium/modules/finance/importing/commit.py` (`commit_batch`, `rollback_batch`)
- Test: `backend/tests/finance/importing/test_income_kinds.py`

**Interfaces:**
- Consumes: Task 1's column.
- Produces: `async def refresh_income_kinds(session: AsyncSession, batch_id: UUID) -> None`. It recomputes `income_kind` for every live transaction this batch's rows reach: its committed rows' transactions, and the transactions of the committed rows its duplicate rows point at. Each gets `'interest'` if its committed row, or any duplicate of that row in a committed batch, has `interest`; otherwise NULL.

- [ ] **Step 1: Write the failing tests.**
  - `test_interest_row_marks_its_transaction`: commit a batch with an interest row; its transaction has `income_kind == "interest"`, the other row's has None.
  - `test_duplicate_interest_row_marks_the_committed_transaction`: commit a CSV-like batch (row `interest=False`), then a second batch on the same account whose row is a duplicate with `interest=True`; the first transaction becomes `"interest"`. Rolling back the second batch sets it back to None.
  - `test_source_category_untouched`: the transaction's `category` is unchanged throughout.
  Build batches with the helpers in `staging_helpers.py`.
- [ ] **Step 2: Run them and see them fail.** `DOCKER_HOST=ssh://docker-test uv run --locked pytest tests/finance/importing/test_income_kinds.py -q`.
- [ ] **Step 3: Implement `refresh_income_kinds`.** Use one `UPDATE … FROM` over the reached transactions. Call it in `commit_batch` after the rows loop and before `heal_transfers`, and in `rollback_batch` after the batch status update. Voided transactions are skipped.
- [ ] **Step 4: Run the tests and see them pass.** Then run lint and pyright.
- [ ] **Step 5: Commit.** `feat: mark a statement's interest rows as interest income`.

### Task 3: Income kinds from connectors

**Files:**
- Modify: `backend/src/lararium/modules/finance/connector_run.py` (`Run.post` gains `income_kind: IncomeKind | None = None`, passed as `details=TransactionDetails(income_kind=…)`)
- Modify: `trading212/records.py`, `trading212/normalize.py` (version)
- Modify: `etoro/statement_ledger.py` (`_Kind` gains `income_kind: IncomeKind | None = None`; `_unassigned` passes it), `etoro/normalize.py` (version)
- Modify: `ibkr/records.py` (`_post` gains `income_kind`; `cash_transaction` maps `type`), `ibkr/normalize.py` (version)
- Modify: `binance/records/__init__.py` (`post` gains `income_kind`), `binance/records/earn.py` (`post_reward`), `binance/records/spot.py` (`dividend`), `binance/normalize.py` (version)
- Test: the existing normalizer test modules of each source (`tests/finance/trading212/…`, `tests/finance/etoro/test_normalize_statement.py`, `tests/finance/ibkr/test_normalize_records.py`, `tests/finance/binance/…`); add one test per mapping next to each source's existing dividend or interest test.

**Interfaces:**
- Consumes: `IncomeKind`, `TransactionDetails.income_kind` (Task 1).

**Mappings (exact):**
- Trading 212: `INTEREST_ON_FREE_CASH` → `interest`; `LENDING_INTEREST` → `share_lending`; `dividend()` → `dividend`.
- eToro Account Activity: `interest`, `interest payment` → `interest`; `dividend` → `dividend`; `stocks lending` → `share_lending`.
- IBKR `CashTransaction.type`: `Dividends`, `Payment In Lieu Of Dividends` → `dividend`; `Broker Interest Received`, `Bond Interest Received` → `interest`. First confirm these strings against IBKR's Flex Query reference for CashTransaction `type`. If the reference spells one differently, use the reference's spelling and say so in the report. Leave every other type unmarked.
- Binance: `post_reward` → `reward`; `spot.dividend` → `reward`.

- [ ] **Step 1: Write the failing tests,** one per mapping row above. Each normalizes a minimal record and asserts the transaction's `income_kind`. Add one negative case per source (Trading 212 `DEPOSIT`, eToro `overnight fee`, IBKR `Withholding Tax`, Binance a trade) asserting None.
- [ ] **Step 2: Run them and see them fail.** Run each changed test file alone.
- [ ] **Step 3: Implement the mappings and bump the four `NORMALIZER_VERSION`s** to the values in Global Constraints.
- [ ] **Step 4: Run the tests and see them pass.** Then run lint and pyright.
- [ ] **Step 5: Commit.** `feat: mark interest, dividends, share lending and rewards from each connector`.

### Task 4: Owner categories

**Files:**
- Create: `backend/src/lararium/modules/finance/owner_categories.py`
- Modify: `backend/src/lararium/modules/finance/schemas.py`, `backend/src/lararium/modules/finance/api.py`
- Test: `backend/tests/finance/test_owner_categories.py`, `backend/tests/api/test_finance_api.py`

**Interfaces:**
- Consumes: `transfers.contra.source_columns` (the source reference of a transaction, as SQL), Task 1's table.
- Produces:
  - `def normalise_category(text: str) -> str` (raises `FinanceValidationError`).
  - `async def set_owner_category(ctx: CommandContext, transaction_ids: Sequence[UUID], category: str | None) -> None`: all or nothing. An unknown id raises `TransactionNotFoundError`; a void one raises `FinanceValidationError` naming it. `None` deletes the rows. It writes `decided_by` from the actor type.
  - `async def known_owner_categories(session: AsyncSession) -> list[str]` (distinct, sorted).
  - `def owner_category_join(t: type[models.Transaction]) -> …`: the SQL the reports join on, matching `source_columns(t)` to the table. Task 5 uses it; return whatever SQLAlchemy construct fits the text query, or expose the equivalent SQL fragment.
  - Schemas: `TransactionCategoriesUpdate(transaction_ids: list[UUID] = Field(min_length=1, max_length=500), category: str | None)`.
  - Route: `PUT /finance/transaction-categories`, name `set_transaction_categories`, 204, run in `command_context(session, actor, "set_transaction_categories", …)`.

- [ ] **Step 1: Write the failing tests.**
  - `test_normalise_category` cases: `" Family  money "` → `"family_money"`, `"SALARY"` → `"salary"`; `""`, `"   "` and 65 characters raise an error.
  - `test_set_and_clear_leave_source_marks`: a transaction with `category="income"` and `income_kind="interest"`; after set then clear, both are unchanged and there is no owner row.
  - `test_category_survives_reimport`: import a file, set the category, roll back, re-import the same file; the new transaction's owner category is found through `known_owner_categories` and through the join.
  - `test_void_transaction_refused`: nothing is written.
  - API: `test_set_transaction_categories` returns 204; 501 ids return 422; an unknown id returns 404.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.** Upsert with `ON CONFLICT (source_kind, source_ref) DO UPDATE`.
- [ ] **Step 4: Run the tests and see them pass.** Then run lint and pyright.
- [ ] **Step 5: Commit.** `feat: let the owner categorise transactions`.

### Task 5: One flows definition: effective category and coin income

**Files:**
- Modify: `backend/src/lararium/modules/finance/cashflow.py`
- Test: `backend/tests/finance/test_cashflow.py`

**Interfaces:**
- Consumes: Task 4's join; Task 1's `income_kind`.
- Produces, in `cashflow.py`:
  - `@dataclass(frozen=True) class Flow`: `transaction_id: UUID`, `day: date`, `account_id: UUID | None` (the asset or liability account it posts to, not In transit, first by position), `account_name: str | None`, `instrument_id: UUID`, `instrument_code: str`, `quantity: Decimal` (the flow, signed: + income), `amount: Decimal | None` (reporting currency), `category: str | None` (effective), `has_owner_category: bool`, `source_category: str | None`, `income_kind: str | None`, `owner_category: str | None`, `payee: str | None`, `description: str`, `counterparty_name: str | None`, `include_in_spending: bool | None`.
  - `async def flows(session, start: date, end: date, excluded: Collection[UUID]) -> list[Flow]`: today's `_FLOWS` and the rate lookup, extended per Global Constraints.
  - `cash_flow` keeps its signature and output shape and reads `flows()`. Category splits apply only when `has_owner_category` is false.

- [ ] **Step 1: Write the failing tests.**
  - `test_owner_category_wins_and_covers_splits`: a transaction with source category `"transfers"` and splits; set owner `"family"`. All of its income is under `"family"`.
  - `test_income_kind_groups_positive_flows_only`: an eToro-like dividend transaction (gross +10 and tax −1.5 on Unassigned, `income_kind="dividend"`). The categories are `("dividend", income 10)` and `(None, spending 1.5)`.
  - `test_coin_reward_valued_on_its_day`: BTC with an EUR price of 50000 on 2026-11-02, a EUR→GBP rate of 0.85, and a reward of 0.0001 BTC booked 2026-11-02 with `income_kind="reward"`. Income is `Decimal("4.25")`.
  - `test_coin_reward_without_price_is_missing`: `missing_count == 1` and the income is unchanged.
  - `test_coin_without_income_kind_is_not_a_flow`: a BNB fee on Unassigned with no kind, and a stock arriving on Unassigned. Neither appears.
  - Extend the existing Hypothesis-free invariant: periods sum to the total, and categories sum to the total for income and for spending.
- [ ] **Step 2: Run them and see them fail.** `DOCKER_HOST=ssh://docker-test uv run --locked pytest tests/finance/test_cashflow.py -q`.
- [ ] **Step 3: Implement.** Replace `rates` with a valuation CTE keyed `(instrument_id, day)`. For fiat it is `1 × v1_fx_rate(instrument, reporting, day)`; for non-fiat, the latest price lateral (as in `valuation.py`) times `v1_fx_rate(price currency, reporting, day)`. The effective category is computed in SQL: `coalesce(oc.category, case when -p.quantity > 0 then t.income_kind end, t.category)`.
- [ ] **Step 4: Run the test file and see it pass.** Then run lint and pyright.
- [ ] **Step 5: Commit.** `feat: count coin rewards and the owner's categories in cash flow`.

### Task 6: Income report and endpoint

**Files:**
- Create: `backend/src/lararium/modules/finance/income.py`
- Modify: `schemas.py`, `api.py`
- Test: `backend/tests/finance/test_income.py`, `backend/tests/api/test_finance_api.py`

**Interfaces:**
- Consumes: `cashflow.flows`, `cashflow.period_start`, `cashflow._periods` (make it public as `periods_of`), `cash_flow(...).hidden_accounts`, `owner_categories.known_owner_categories`.
- Produces:
  - `income.IncomeReport` with `start`, `end`, `bucket`, `total`, `missing_count`, `periods: list[IncomePeriod(start, total, by_category: list[IncomeCategory])]`, `categories: list[IncomeCategory(category: str | None, amount)]` (largest first, then by name), `entries: list[IncomeEntry]` (newest first, then by transaction id), `known_categories: list[str]`, `hidden_moved_in: Decimal`.
  - `IncomeEntry` has the fields of spec §4.5. It is the positive flows of one transaction and instrument, summed, with `quantity` in the source instrument.
  - `async def income_report(session, start, end, bucket, excluded, account_id: UUID | None = None, category: str | None = None) -> IncomeReport`. The `category` filter (`"uncategorised"` means None) narrows `entries` only. `account_id` narrows everything, and `hidden_moved_in` is 0 when it is set.
  - Route `GET /finance/income`, name `get_income`, same range defaults as `get_cash_flow`, response `IncomeOut`.

- [ ] **Step 1: Write the failing tests.**
  - `test_entries_totals_and_filters`: covers the account filter, the category filter, entries newest first, and an unpriced reward counted in `missing_count` but not listed in entries' totals. Its entry is listed with `amount=None`.
  - `test_agrees_with_cash_flow` (Hypothesis, a strategy of a few signed fiat flows across two accounts, some categorised, plus one hidden account move): `report.total + report.hidden_moved_in == cash_flow(...).income`, `sum(categories) == total`, `sum(periods) == total`. Keep examples few (`max_examples=25`) as the transfers property tests do.
  - API: `test_get_income` returns 200 with the shape.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them and see them pass.** Then run lint and pyright.
- [ ] **Step 5: Regenerate the client.** Run `make openapi`; commit `web/openapi.json` and `web/src/api` with this task.
- [ ] **Step 6: Commit.** `feat: report income by category, period and entry`.

### Task 7: The Income page

**Files:**
- Create: `web/src/pages/IncomePage.tsx`, `web/src/pages/IncomePage.test.tsx`, `web/src/components/CategoryPicker.tsx`
- Modify: `web/src/router.tsx` (route `/income`; the Overview section's pages become `[{to:"/",label:"Overview"},{to:"/income",label:"Income"}]`), `web/src/lib/category.ts` (`incomeKindLabel(kind)`: `share_lending` → "Share lending", others capitalised)

**Interfaces:**
- Consumes: `getIncome`, `setTransactionCategories`, `IncomeOut` from `../api` (Task 6); `RangePicker`, `ShareChart`, the `Table` and `Checkbox` components, `categoryLabel`, `formatMoney`, `formatQuantity`, `bucketFor`.
- Produces: `CategoryPicker({value, known, onChange})`, a combobox over `known` that also accepts a typed new value. With no known categories it suggests `salary` and `family`.

Layout and copy as in spec §4.6:
- Range and account select at the top; then Total and Uncategorised figures.
- "By category": `ShareChart` and a list. Clicking a category sets the `category` query filter; clicking again clears it.
- "By month": `CashFlowChart`-like stacked bars are optional. A simple per-period list of totals is enough if stacking needs new chart work: say which you built.
- The entries table has columns: checkbox, Date, Account, From / description, Amount (the reporting amount; under it the source quantity and code when the instrument differs from the reporting currency; "No price" when `amount` is null), Source (the income kind label, else `categoryLabel(source_category)`, else "—"), Category (a per-row `CategoryPicker`).
- A selection bar: "N selected", a `CategoryPicker`, Apply, Clear. After a write, invalidate the income and cash-flow queries.

- [ ] **Step 1: Write the failing tests** (mock `../api` as `OverviewPage.test.tsx` does):
  - `shows totals, categories and entries`;
  - `filters by account` (calls `getIncome` with `account_id`);
  - `sets one entry's category` (calls `setTransactionCategories` with `{transaction_ids:[id], category:"salary"}`);
  - `sets many at once and clears`;
  - `shows the source column unchanged after categorising`;
  - `shows a coin reward's quantity and No price when unpriced`.
- [ ] **Step 2: Run them and see them fail.** `cd web && pnpm vitest run src/pages/IncomePage.test.tsx`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them and see them pass.** Then run the web lint and typecheck scripts from `package.json`.
- [ ] **Step 5: Commit.** `feat: add the Income page`.

### Task 8: "Where it came from" on the Overview

**Files:**
- Modify: `web/src/pages/OverviewPage.tsx`, `web/src/pages/OverviewPage.test.tsx`

**Interfaces:**
- Consumes: `CashFlowOut.categories[].income` (effective categories after Task 5), `categoryLabel`, `BarRow`.

- [ ] **Step 1: Write the failing test.** `shows where income came from`: with `FLOW.categories` holding income in two categories, the card titled "Where it came from" lists them largest first, and a link goes to `/income`. Remove or adjust the test that asserts "Where your money is".
- [ ] **Step 2: Run it and see it fail.** `pnpm vitest run src/pages/OverviewPage.test.tsx`.
- [ ] **Step 3: Implement.** Replace the "Where your money is" card with "Where it came from". Its bars use `CHART.income` (add it to `lib/theme.ts` if absent, as the income tone used by `text-income`). The empty state reads "No income in these dates". Remove `byAccount` and `largest` if nothing else uses them.
- [ ] **Step 4: Run it and see it pass.** Then run lint and typecheck.
- [ ] **Step 5: Commit.** `feat: show where income came from on the Overview`.
