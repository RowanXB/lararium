# Transfer Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pair the owner's own transfers whose rows print only part of the other side's account, or nothing at all, so income and spending can leave them out.

**Architecture:** M4's matcher stays as it is. What changes is how a record names another own account (`holder` in `contra.eligible_records`), with three new sources: an account number alone, a sub-account's main account, and (weak, `weak_holder`) a sort code alone. All are derived when records are read, from import rows, the registry and `parent_account_id`. On top of that: one new weakest pairing tier, Tembo's full-history coverage, LemFi's interest flag, a main-account command, and two data migrations.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy async, PostgreSQL 18, raw-SQL Alembic migrations, pytest with Hypothesis, React with Vitest.

**Spec:** `docs/superpowers/specs/2026-11-21-transfer-evidence-design.md` (approved 2026-11-21; plan-time calls recorded in its §5.1 and §5.4).

## Global Constraints

- **Branch.** Work on `feat/claude-transfer-evidence`, cut from `docs/claude-transfer-evidence-design` so the PR carries the spec, the plan and the code.
- **Commits.**
  - signed (`git commit -S`), titles `<type>: <summary>`;
  - add files by explicit path, never `git add -A`;
  - no attribution lines.
- **Tests.**
  - Run against the remote host only: `cd backend && DOCKER_HOST=ssh://docker-test uv run pytest <path> -q -p no:cacheprovider`, never local Docker Desktop.
  - Full suite: `DOCKER_HOST=ssh://docker-test make check` from the repo root.
  - Synthetic data only: no real names, account numbers, sort codes or amounts, in tests or in docs.
- **Migrations.**
  - raw SQL, as `UPGRADE` and `DOWNGRADE` lists of statements like `0024_finance_import_row_time_unit.py`;
  - `0025` (Task 2) revises `0024`, and `0026` (Task 6) revises `0025`;
  - a backfill is a module-level string constant, so a test can run it.
- **API changes.** `make openapi`, then commit `web/openapi.json`, `web/src/api/index.ts` and `web/src/api/types.gen.ts`.
- **Docs.** English only. README.md and README.zh-CN.md change together. UI copy is English.
- **M4 invariants hold** (spec §3):
  - `pairs.pair_relation` is the one pair definition;
  - a contra is In transit if and only if it is a leg of a transfer that isn't rejected;
  - results don't depend on arrival order;
  - decisions survive;
  - `tests/finance/transfers/test_performance.py` stays green.

## Review Focus

1. **A printed account number with spaces or dashes** ("1111 2222") matches after stripping non-digits. Task 1 test: `test_spaces_in_a_printed_account_number_are_ignored`.
2. **A duplicate row in a batch that was rolled back** gives no evidence. Task 1 test: `test_a_duplicate_in_a_rolled_back_batch_gives_no_evidence`.
3. **Clearing a main account** removes the pairs it made, and history heals back. Task 4 test: `test_clearing_the_main_account_unpairs_its_history`.
4. **A sort code shared by two registered own accounts** names nobody, even weakly (for example, if a second Monzo account is registered). Task 5 test: `test_a_sort_code_two_own_accounts_share_names_nobody`.
5. **A Tembo statement without its opening or valuation line** keeps today's coverage (first to last row). Task 6 test: `test_tembo_without_an_opening_date_keeps_the_rows_span`.

---

### Task 1: An account number alone names the account (the evidence lookup)

**Files:**
- Modify: `backend/src/lararium/modules/finance/transfers/contra.py`
  - `eligible_records`: `holder` becomes item 1, else item 2 (spec §5.1), plus a new `names_account` column;
  - `_OWN_RECORDS` gains `names_account`;
  - `OwnRecord` gains `holder`, read in `load_own_record`.
- Modify: `backend/src/lararium/modules/finance/transfers/engine.py`, in `_registry_counterpart`.
- Modify: `backend/tests/finance/transfers/conftest.py`, adding a `details` parameter, the `RowDetails` helper and three fixtures.
- Create: `backend/tests/finance/transfers/test_evidence.py`
- Modify: `README.md` and `README.zh-CN.md`, in the transfer matching section: an account number alone names a registered account.

**Interfaces:**
- **Produces, in `contra`:**
  - `eligible_records` columns: `holder: UUID | None` (now the full identifier, else the account number alone) and `names_account: bool`;
  - `OwnRecord.holder: UUID | None = None`, a last field with a default, so existing constructors keep working.
- **Produces, in the test conftest:**
  - `RowDetails(sort_code: str | None = None, account_number: str | None = None, account_ending: str | None = None)`, a frozen dataclass;
  - `record_import_rows(..., details: Sequence[RowDetails] | None = None)`, which writes `counterparty_sort_code`, `counterparty_account_number` and `counterparty_account_ending` per row;
  - fixture `imported_bank`: `(name: str, *, identifier: str | None = None, main: AccountRecord | None = None) -> AccountRecord`. It creates a manual `transactions`-mode bank account, with `parent_account_id=main.id` when given, and registers `identifier` when given.
  - fixture `statement_row`: `(account: AccountRecord, quantity: str, *, day: date = DAY, details: RowDetails | None = None, bank_transfer: bool = True, complete: tuple[date, date] = IMPORTED) -> UUID`. It posts a manual-origin transaction naming nobody (contra from `transfer_contra`, as `test_imports.import_row` does), then records it as a committed import row with `details` through `record_import_rows`.
  - fixture `duplicate_row`: `(transaction_id: UUID, details: RowDetails, *, batch_status: Literal["committed", "rolled_back"] = "committed") -> None`. It inserts a new batch with that status, holding one `status="duplicate"` statement row whose `duplicate_of` is the transaction's committed row.

- [ ] **Step 1: Write the failing tests in `test_evidence.py`.** Use the `session`, `run`, `heal`, `imported_bank`, `statement_row` and `duplicate_row` fixtures. Write `x_id = "445566:11112222"` for X, the account a row names, and `y_id = "778899:33334444"` for Y.

```python
async def test_an_account_number_alone_names_the_registered_account(...):
    x = await imported_bank("X", identifier=x_id); y = await imported_bank("Y", identifier=y_id)
    sent = await statement_row(x, "-100")
    received = await statement_row(y, "100", details=RowDetails(account_number="11112222"))
    await heal()
    [t] = await list_transfers(session)
    assert (t.from_account_id, t.to_account_id, t.status) == (x.id, y.id, "matched")
    assert {t.out_transaction_id, t.in_transaction_id} == {sent, received}

async def test_spaces_in_a_printed_account_number_are_ignored(...):  # "1111 2222": same as above
async def test_an_account_number_two_own_accounts_share_names_nobody(...):
    # X "445566:11112222" and Z "990011:11112222" registered: list_transfers(session) == []
async def test_a_named_stranger_is_not_overridden_by_an_account_number(...):
    # the received row names a stranger's full account (counterparty_identifier_id set to
    # get_or_create_identifier("uk_bank_account", "500000:50000001")) and a duplicate row
    # prints X's number: list_transfers(session) == []
async def test_a_row_naming_its_own_accounts_number_is_no_transfer(...):
    # Y's row prints "33334444" (Y's own): list_transfers(session) == []
async def test_the_account_number_on_a_duplicate_row_counts(...):
    # Y's committed row prints nothing; duplicate_row(received, RowDetails(account_number=
    # "11112222")); after heal, the same matched transfer as the first test
@pytest.mark.parametrize("printed_on", ["committed", "duplicate"])
async def test_details_on_either_row_give_the_same_transfer(printed_on, ...):
    # X's number printed on the committed row or on its duplicate: either way
    # [(t.from_account_id, t.to_account_id, t.status) for t in list_transfers] ==
    # [(x.id, y.id, "matched")]
async def test_a_duplicate_in_a_rolled_back_batch_gives_no_evidence(...):
    # duplicate_row(..., batch_status="rolled_back"): list_transfers(session) == []
async def test_two_rows_with_different_account_numbers_name_nobody(...):
    # committed row prints "11112222", duplicate prints "99998888": list_transfers == []
async def test_registering_the_account_later_pairs_history_and_removing_it_reverts(...):
    # X created without identifier: none; run(assign_identifier, x.id, "uk_bank_account",
    # x_id) + heal(): matched; run(unassign_identifier_and_rematch, identifier id): none,
    # and both contras back on Unassigned (contra_account)
```

- [ ] **Step 2: Run the tests and confirm they fail.**
  - Run: `cd backend && DOCKER_HOST=ssh://docker-test uv run pytest tests/finance/transfers/test_evidence.py -q -p no:cacheprovider`
  - Expected: FAIL. The fixtures are missing, then there's no transfer.

- [ ] **Step 3: Implement the conftest additions** (`RowDetails`, `details`, `imported_bank`, `statement_row`, `duplicate_row`), as the Interfaces block specifies.

- [ ] **Step 4: Implement the evidence lookup in `contra.eligible_records`.** It is one more per-record lateral aggregate, keeping the per-record lookup shape (spec §3), over the transaction's import rows:
  - **Which rows.** Rows `r` joined to their batch `b`, with `b.status = 'committed'` and `r.kind = 'statement'`, and either `r.transaction_id = t.id and r.status = 'committed'`, or `r.status = 'duplicate'` with `r.duplicate_of` equal to that committed row's id.
  - **What it collects.**
    - `numbers`: the distinct `regexp_replace(counterparty_account_number, '\D', '', 'g')` over rows with an account number and no sort code;
    - `sort_codes`: the same over sort codes, for rows with a sort code and no account number (Task 5 reads it);
    - `partial`: `bool_or` of any sort code, account number or ending being present.
  - **`holder`:**
    - when `t.counterparty_identifier_id` is set, the registry row's `account_id` (today's lookup);
    - else, when `numbers` has exactly one element, the `account_id` of the registered `uk_bank_account` identifiers whose account-number part (`split_part(value, ':', 2)`) equals it, when exactly one distinct account has it;
    - else null.
  - **`names_account`:** `t.counterparty_identifier_id IS NOT NULL OR partial`.

- [ ] **Step 5: Read the new `holder` in the engine.** Add `holder` to `OwnRecord`, filled by `load_own_record`. `engine._registry_counterpart(session, record)` returns `record.holder`, unless it equals `record.account_id`. `transfer_contra` and `preview_match` stay unchanged (spec §5.1).

- [ ] **Step 6: Run the new tests and the transfer and import suites.**
  - Run: `cd backend && DOCKER_HOST=ssh://docker-test uv run pytest tests/finance/transfers tests/finance/importing/test_transfers.py -q -p no:cacheprovider`
  - Expected: all pass, `test_performance.py` included.

- [ ] **Step 7: Update the READMEs**, saying that an account number alone, matching one registered own account, identifies it.

- [ ] **Step 8: Commit.**

```bash
git add backend/src/lararium/modules/finance/transfers/contra.py backend/src/lararium/modules/finance/transfers/engine.py backend/tests/finance/transfers/conftest.py backend/tests/finance/transfers/test_evidence.py README.md README.zh-CN.md
git commit -S -m "feat: let an imported row's account number alone name the registered own account"
```

### Task 2: LemFi's interest payouts are no bank transfers

**Files:**
- Modify: `backend/src/lararium/modules/finance/importing/parsed.py`. `ParsedRow` gains `interest: bool = False`.
- Modify: `backend/src/lararium/modules/finance/importing/pdf/__init__.py`. `_row` sets `interest=layout_row.interest`.
- Modify: `backend/src/lararium/modules/finance/importing/profiles.py`. `CsvProfileConfig` gains `interest_descriptions: list[str] = Field(default_factory=list)`: a row whose description is one of these pays interest, for a bank that prints no type codes.
- Modify: `backend/src/lararium/modules/finance/importing/csv_parser.py`. Set `interest`.
- Modify: `backend/src/lararium/modules/finance/importing/catalog.py`. The LemFi CSV entry gets `interest_descriptions=["Savings Interest Payout"]`.
- Modify: `backend/src/lararium/modules/finance/models.py`, `importing/staging.py` and `schemas.py`, for the import row's `interest` (model, `_row_values`, `ImportRowRecord`, `ImportRowOut`).
- Modify: `backend/src/lararium/modules/finance/importing/commit.py`. In `_counterparty`, `bank_transfer` becomes `not row.interest and is_bank_transfer(...)`.
- Create: `backend/migrations/versions/0025_finance_import_row_interest.py`
- Tests:
  - `backend/tests/finance/importing/test_lemfi.py`
  - `backend/tests/finance/importing/test_csv_parser.py`
  - `backend/tests/finance/importing/test_catalog.py`
  - `backend/tests/finance/importing/test_transfers.py`
  - `backend/tests/finance/importing/test_staging.py`, for the migration test, with its `_migration` helper
- Modify the generated client: `web/openapi.json`, `web/src/api/index.ts`, `web/src/api/types.gen.ts`.
- Modify: `web/src/pages/ImportBatchPage.test.tsx`. The row factory gains `interest: false`, which the regenerated `ImportRowOut` type requires.

**Interfaces:**
- Produces:
  - `ParsedRow.interest: bool`;
  - `ImportRow.interest: Mapped[bool]` (NOT NULL, default false);
  - `ImportRowOut.interest: bool`;
  - `CsvProfileConfig.interest_descriptions: list[str]`;
  - migration 0025's constants `ADD_COLUMN`, `BACKFILL_INTEREST` and `NO_TRANSFER`.

- [ ] **Step 1: Write the failing tests.**

```python
# test_lemfi.py
def test_interest_payout_rows_pay_interest():
    rows = _rows(_parse(LemfiStatement([[...a payout row, a received row...]], savings=True)))
    assert [r.interest for r in rows] == [True, False]
# test_csv_parser.py
def test_interest_descriptions_mark_interest_rows():  # config with interest_descriptions=["Interest"]
    assert [r.interest for r in parsed_rows] == [True, False]
# test_catalog.py
def test_lemfi_csv_marks_its_interest():
    assert _csv("LemFi", "CSV").interest_descriptions == ["Savings Interest Payout"]
# test_transfers.py (importing)
async def test_an_interest_payout_is_no_bank_transfer(...):  # commit a LemFi savings PDF
    assert (await session.scalar(select(t.bank_transfer).where(...payout...))) is False
# test_staging.py
async def test_0025_marks_lemfi_interest_imported_before(...):
    # commit a LemFi savings PDF with a payout; set interest=false, bank_transfer=true on it;
    # run BACKFILL_INTEREST and NO_TRANSFER; the row's interest is true, its transaction's
    # bank_transfer false, and no other row changed
```

- [ ] **Step 2: Run the tests and confirm they fail.**
  - Run: `cd backend && DOCKER_HOST=ssh://docker-test uv run pytest tests/finance/importing/test_lemfi.py tests/finance/importing/test_csv_parser.py tests/finance/importing/test_catalog.py tests/finance/importing/test_transfers.py tests/finance/importing/test_staging.py -q -p no:cacheprovider -k interest`
  - Expected: FAIL. `interest` is undefined.

- [ ] **Step 3: Write migration 0025.**
  - `ADD_COLUMN`: `ALTER TABLE finance.import_rows ADD COLUMN interest boolean NOT NULL DEFAULT false`
  - `BACKFILL_INTEREST`: `UPDATE finance.import_rows SET interest = true WHERE kind = 'statement' AND description = 'Savings Interest Payout'`. That is LemFi's fixed text, and no other catalog bank prints it.
  - `NO_TRANSFER`: `UPDATE finance.transactions AS t SET bank_transfer = false FROM finance.import_rows AS r WHERE r.transaction_id = t.id AND r.interest AND t.bank_transfer`
  - Downgrade drops the column. Corrected `bank_transfer` values stay: they were wrong.

- [ ] **Step 4: Implement the parser, model, staging, schema and commit changes** listed under Files.

- [ ] **Step 5: Run the tests and confirm they pass.** Same command as Step 2, without `-k`. Expected: PASS.

- [ ] **Step 6: Regenerate the client and add `interest: false` to the web row factory.** Run `make openapi && make web-check`. Expected: web tests pass.

- [ ] **Step 7: Commit.**

```bash
git add web/src/pages/ImportBatchPage.test.tsx backend/migrations/versions/0025_finance_import_row_interest.py backend/src/lararium/modules/finance/importing/parsed.py backend/src/lararium/modules/finance/importing/pdf/__init__.py backend/src/lararium/modules/finance/importing/profiles.py backend/src/lararium/modules/finance/importing/csv_parser.py backend/src/lararium/modules/finance/importing/catalog.py backend/src/lararium/modules/finance/models.py backend/src/lararium/modules/finance/importing/staging.py backend/src/lararium/modules/finance/schemas.py backend/src/lararium/modules/finance/importing/commit.py backend/tests/finance/importing/test_lemfi.py backend/tests/finance/importing/test_csv_parser.py backend/tests/finance/importing/test_catalog.py backend/tests/finance/importing/test_transfers.py backend/tests/finance/importing/test_staging.py web/openapi.json web/src/api/index.ts web/src/api/types.gen.ts
git commit -S -m "fix: keep LemFi's interest payouts out of transfer matching"
```

### Task 3: A sub-account's bank transfers name its main account

**Files:**
- Modify: `backend/src/lararium/modules/finance/transfers/contra.py`. `holder` gains item 3: when `t.counterparty_identifier_id` is null, `numbers` is empty and `not names_account`, and also `t.bank_transfer`, the own account's `source_kind = 'manual'` and `parent_account_id IS NOT NULL`, then it is `parent_account_id`.
- Modify: `backend/tests/finance/transfers/test_evidence.py`

**Interfaces:**
- Consumes: Task 1's `imported_bank(..., main=...)`, `statement_row` and `RowDetails`.
- Produces: `holder` item 3 (spec §5.1).

- [ ] **Step 1: Write the failing tests.** M is a main account and S its sub-account, made with `imported_bank("S", main=m)`.

```python
async def test_a_sub_accounts_deposit_pairs_with_its_main_accounts_payment(...):
    # m row -100, s row +100 (naming nobody), same day: one matched transfer m -> s
async def test_a_sub_accounts_row_after_the_main_accounts_last_import_gets_a_placeholder(...):
    # m imported (complete) up to DAY; s row +100 on DAY + 3 recorded with complete through
    # DAY + 3: the transfer is "inferred" and len(await placeholders(session)) == 1
async def test_a_sub_accounts_row_inside_the_main_accounts_full_days_settles(...):
    # m imports list DAY in full with no matching row; s row +100 on DAY: "before_history",
    # no placeholder
async def test_a_sub_account_row_naming_an_account_ignores_the_main(...):
    # s row prints RowDetails(account_number="50000001"): list_transfers == []
async def test_interest_on_a_sub_account_is_no_transfer(...):
    # s row with bank_transfer=False: list_transfers == []
async def test_a_connected_account_with_a_parent_is_unaffected(...):
    # create_account(..., parent_account_id=m.id, source_connection_id=<a connection>) and
    # a connector record naming nobody, bank transfer: list_transfers == []
```

- [ ] **Step 2: Run the tests and confirm they fail.** Run `cd backend && DOCKER_HOST=ssh://docker-test uv run pytest tests/finance/transfers/test_evidence.py -q -p no:cacheprovider -k sub_account`. Expected: FAIL, with no transfer.

- [ ] **Step 3: Add item 3 to `holder`** in `eligible_records`. Read the own account's `source_kind` and `parent_account_id` by a per-record lookup on the own posting's account, not a join.

- [ ] **Step 4: Run the transfer suites.** Run `cd backend && DOCKER_HOST=ssh://docker-test uv run pytest tests/finance/transfers tests/finance/importing/test_transfers.py -q -p no:cacheprovider`. Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add backend/src/lararium/modules/finance/transfers/contra.py backend/tests/finance/transfers/test_evidence.py
git commit -S -m "feat: let a sub-account's bank transfers name its main account"
```

### Task 4: The owner sets a manual account's main account

**Files:**
- Modify: `backend/src/lararium/modules/finance/accounts.py`. Add `set_main_account`.
- Modify: `backend/src/lararium/modules/finance/schemas.py`. Add `MainAccountUpdate`.
- Modify: `backend/src/lararium/modules/finance/transfers_api.py`. Add `POST /accounts/{account_id}/main-account`.
- Modify: `web/src/pages/AccountsPage.tsx` and `web/src/pages/AccountsPage.test.tsx`.
- Modify the generated client: `web/openapi.json`, `web/src/api/index.ts`, `web/src/api/types.gen.ts`.
- Test: `backend/tests/api/test_transfers_api.py`
- Modify: `README.md` and `README.zh-CN.md`, explaining how to give a savings account its main account and what it changes.

**Interfaces:**
- **Consumes:** Task 3's item 3.
- **Produces:**
  - `accounts.set_main_account(ctx: CommandContext, account_id: UUID, main_account_id: UUID | None) -> AccountRecord`;
  - `schemas.MainAccountUpdate(main_account_id: UUID | None)`;
  - the API route `set_main_account`, returning `AccountOut` and running under `healing_command(session, actor, "set_main_account", args)`.
- **Refusals,** each a 422 (`FinanceValidationError`) unless noted:
  - a system account (`require_owner_account(record, "given a main account")`);
  - a connected account ("its connector sets it");
  - a main account that is the account itself, closed, a system account, or has a main account of its own;
  - an account that is already some account's main account, when setting rather than clearing;
  - an unknown main account (404, `AccountNotFoundError`).

  Clearing (`None`) is always allowed. A change publishes `finance.account.updated`.

- [ ] **Step 1: Write the failing API tests** in `test_transfers_api.py`, through the HTTP client fixtures that file already uses.

```python
async def test_setting_a_main_account_pairs_its_history(...):
    # two manual accounts with imported rows -100 / +100 naming nobody: no transfer;
    # POST .../accounts/{s}/main-account {"main_account_id": m}: 200, parent_account_id == m,
    # and GET /transfers lists one matched transfer m -> s
async def test_clearing_the_main_account_unpairs_its_history(...):
    # then {"main_account_id": null}: 200, GET /transfers == []
async def test_a_main_account_is_refused_where_it_cant_hold(...):
    # 422 for: a connected account; main == itself; a closed main; a main with a main of
    # its own; an account that is already another's main. 404 for an unknown main id.
```

- [ ] **Step 2: Run the tests and confirm they fail.** Run `cd backend && DOCKER_HOST=ssh://docker-test uv run pytest tests/api/test_transfers_api.py -q -p no:cacheprovider -k main_account`. Expected: FAIL with 404 Not Found (no route).

- [ ] **Step 3: Implement the command, the schema and the route.** Use a conditional `update(...).where(...)` like `set_balance_mode`.

- [ ] **Step 4: Run the tests and confirm they pass.** Same command. Expected: PASS.

- [ ] **Step 5: Add the web control and its test.**
  - On the Accounts page, each manual account that isn't a system account gets a "Main account" select.
  - Its options are "None" plus the open own non-system accounts other than this one that have no main account.
  - Choosing one calls `setMainAccount` and refetches accounts.
  - Test, in `AccountsPage.test.tsx`: "sets a manual account's main account". It picks the main account and expects the POST body `{ main_account_id: "<id>" }`.

- [ ] **Step 6: Regenerate the client and run the web tests.** Run `make openapi && make web-check`. Expected: web tests pass.

- [ ] **Step 7: Update the READMEs.**

- [ ] **Step 8: Commit.**

```bash
git add backend/src/lararium/modules/finance/accounts.py backend/src/lararium/modules/finance/schemas.py backend/src/lararium/modules/finance/transfers_api.py backend/tests/api/test_transfers_api.py web/src/pages/AccountsPage.tsx web/src/pages/AccountsPage.test.tsx web/openapi.json web/src/api/index.ts web/src/api/types.gen.ts README.md README.zh-CN.md
git commit -S -m "feat: let the owner give a savings account its main account"
```

### Task 5: A sort code alone pairs weakly

**Files:**
- Modify: `backend/src/lararium/modules/finance/transfers/contra.py`. Add a `weak_holder` column to `eligible_records` and `_OWN_RECORDS`.
- Modify: `backend/src/lararium/modules/finance/transfers/pairs.py`:
  - `Tier` gains `4`, as `WEAK`;
  - `Pairable` gains `weak_holder` and `names_account`, which `_load_records` selects;
  - a new `weak_tier`, called from `_registry_edges`.
- Modify: `backend/src/lararium/modules/finance/transfers/queries.py`. Add `weak_transfers`.
- Modify: `backend/src/lararium/modules/finance/transfers/decisions.py`. `unpair` refuses a weak pair.
- Modify: `backend/src/lararium/modules/finance/transfers/__init__.py`. Export `weak_transfers`.
- Modify: `backend/src/lararium/modules/finance/schemas.py` and `transfers_api.py`. Add `TransferOut.weak`, set in `_transfers_out`.
- Modify: `web/src/pages/TransfersPage.tsx` and `web/src/pages/TransfersPage.test.tsx`.
- Modify the generated client: `web/openapi.json`, `web/src/api/index.ts`, `web/src/api/types.gen.ts`.
- Modify: `backend/tests/finance/transfers/test_evidence.py` and `README.md`, `README.zh-CN.md`.

**Interfaces:**
- **Consumes:** Task 1's `sort_codes` and `names_account`.
- **Produces:**
  - **`weak_holder`:** when `counterparty_identifier_id` is null, `numbers` is empty and `sort_codes` has exactly one element, it is the one own account whose registered `uk_bank_account` has that sort-code part (`split_part(value, ':', 1)`). Otherwise null.
  - **`pairs`:** `WEAK: Tier = 4`, plus `Pairable.weak_holder: UUID | None = None` and `Pairable.names_account: bool = False`. The defaults keep `preview_match` and the existing tests building `Pairable` as they do.
  - **`pairs.weak_tier(r: Pairable, c: Pairable) -> Tier | None`:** `WEAK` when the two are on different accounts, neither is identified, and one of them, `w`, has `w.weak_holder == other.account_id`. Both must also be bank transfers, and `other.names_account` must be false. Otherwise `None`.
  - **`queries.weak_transfers(session: AsyncSession, transfers: Collection[TransferRecord]) -> frozenset[UUID]`:** the ids of matched transfers whose two own records both have no `holder` other than their own account, and that no `pair` decision links.
  - **`TransferOut.weak: bool = False`.**

- [ ] **Step 1: Write the failing tests.** X is a BOC-like account. H is an HSBC-like account, registered `"404040:55556666"`; its sort code is `404040`.

```python
async def test_a_sort_code_alone_pairs_a_unique_counterpart(...):
    # x row -100 RowDetails(sort_code="40-40-40"), h row +100 naming nobody: one matched
    # x -> h, and weak_transfers(session, transfers) == {its id}
async def test_a_sort_code_alone_never_pairs_with_a_row_naming_an_account(...):
    # h row names a stranger (counterparty_identifier_id): list_transfers == []
async def test_a_sort_code_alone_with_two_candidates_pairs_nothing_and_asks_nothing(...):
    # two h rows +100 the same day: list_transfers == [] (no review item either)
async def test_a_sort_code_alone_opens_no_transfer_and_no_placeholder(...):
    # no h row: list_transfers == [] and await placeholders(session) == []
async def test_a_sort_code_two_own_accounts_share_names_nobody(...):
    # H and K both registered with sort code 404040: list_transfers == []
async def test_a_named_counterpart_outranks_a_sort_code(...):
    # x row -100 with H's sort code; y row +100 printing X's account number; h row +100
    # naming nobody: the matched transfer is x -> y; weak_transfers == set()
async def test_unpairing_a_sort_code_pair_is_refused_and_reject_undo_restores_it(...):
    # run(unpair, t.id) raises FinanceValidationError; run(reject, t.id): both contras on
    # Unassigned; run(unreject, t.id) + heal(): matched again
```

- [ ] **Step 2: Run the tests and confirm they fail.** Run `cd backend && DOCKER_HOST=ssh://docker-test uv run pytest tests/finance/transfers/test_evidence.py -q -p no:cacheprovider -k sort_code`. Expected: FAIL.

- [ ] **Step 3: Add `weak_holder`, and plumb `weak_holder` and `names_account`** into `_OWN_RECORDS` and `_load_records`.

- [ ] **Step 4: Add `weak_tier` and its edges in `_registry_edges`.**
  - It applies only where `registry_tier` returns `None`.
  - It skips pairs that `decisions.apart` keeps apart.
  - The gate is `known.holds_from(other, counterpart_bounds(w, settings)[0])`, as `_one_sided_known` uses it.
  - Everything after edges (strongest tier, `exact`, `partner`, `_twins`) is unchanged.

- [ ] **Step 5: Add `weak_transfers`, the refusal in `unpair`** ("a pair made by sort code alone can only be rejected"), **and `TransferOut.weak`.**

- [ ] **Step 6: Run the transfer suites.** Run `cd backend && DOCKER_HOST=ssh://docker-test uv run pytest tests/finance/transfers tests/finance/importing/test_transfers.py tests/api/test_transfers_api.py -q -p no:cacheprovider`. Expected: PASS.

- [ ] **Step 7: Update the Transfers page and its test.**
  - Under Recent, a matched transfer with `weak` shows Reject and no Unpair.
  - The test file's transfer fixtures gain `weak: false`, which the regenerated type requires.
  - Test: "offers only Reject on a pair made by sort code". It renders a weak matched transfer and expects no "Unpair" button and one "Reject" button.
  - Run `make openapi && make web-check`. Expected: PASS.

- [ ] **Step 8: Update the READMEs and the M4 design.**
  - **READMEs:** a sort code alone pairs only a unique counterpart that names no account, and such a pair can be rejected, not unpaired.
  - **M4 design** (`docs/superpowers/specs/2026-11-15-transfer-matching-design.md`): §5.3 and §5.7 each get one sentence pointing to `2026-11-21-transfer-evidence-design.md` for the evidence it adds (an account number alone, a main account, a sort code alone as the weakest tier).

- [ ] **Step 9: Commit.**

```bash
git add backend/src/lararium/modules/finance/transfers/contra.py backend/src/lararium/modules/finance/transfers/pairs.py backend/src/lararium/modules/finance/transfers/queries.py backend/src/lararium/modules/finance/transfers/decisions.py backend/src/lararium/modules/finance/transfers/__init__.py backend/src/lararium/modules/finance/schemas.py backend/src/lararium/modules/finance/transfers_api.py backend/tests/finance/transfers/test_evidence.py web/src/pages/TransfersPage.tsx web/src/pages/TransfersPage.test.tsx web/openapi.json web/src/api/index.ts web/src/api/types.gen.ts README.md README.zh-CN.md docs/superpowers/specs/2026-11-15-transfer-matching-design.md
git commit -S -m "feat: pair a row that prints only a sort code with a unique counterpart"
```

### Task 6: A Tembo statement lists everything from the account's opening

**Files:**
- Modify: `backend/src/lararium/modules/finance/importing/pdf/tembo.py`. When both the opening and the valuation date are printed, the `LayoutResult` gets `period=(account_opened_on, valued_on)` and `period_complete=True`. Otherwise it is unchanged. The printed period's first-to-last-row check stays.
- Modify: `backend/src/lararium/modules/finance/transfers/pairs.py`. In `_load_known`, an account and instrument whose committed batch has `complete_from` equal to its `import_statements.account_opened_on` gets `first_full_day = date.min`.
- Modify: `backend/tests/finance/importing/synthetic_pdfs.py`. `TemboStatement` gains `opened: str | None = "03/02/2027"` and `valued: str | None = "01/06/2027"`; `None` leaves that line out.
- Create: `backend/migrations/versions/0026_finance_tembo_full_history.py`
- Tests:
  - `backend/tests/finance/importing/test_pdf_layouts.py`
  - `backend/tests/finance/importing/test_staging.py`
  - `backend/tests/finance/importing/test_transfers.py`
- Modify: `README.md` and `README.zh-CN.md`, replacing the sentence that a Tembo statement lists in full only the days between its first and last row.

**Interfaces:**
- **Consumes:** `staging._covered`, unchanged: a statement made on its period's last day lists in full through the day before. Also `pdf/__init__`'s closing date rule, unchanged, which leaves the Plan value undated.
- **Produces:** migration 0026's constants `PERIOD` and `COVERAGE`.

- [ ] **Step 1: Write the failing tests.**

```python
# test_pdf_layouts.py
def test_tembo_lists_everything_from_opening_to_valuation():
    st = _tembo(TemboStatement([[row on 03/02/2027]], opened="03/02/2027", valued="01/06/2027"))
    assert (st.period, st.period_complete) == ((date(2027, 2, 3), date(2027, 6, 1)), True)
    assert st.closing_balance_date is None
def test_tembo_without_an_opening_date_keeps_the_rows_span():
    st = _tembo(TemboStatement([[...]], opened=None))
    assert st.period == (first_row_day, last_row_day) and st.period_complete is False
# test_staging.py
async def test_a_tembo_statement_lists_in_full_from_opening_to_the_day_before_valuation(...):
    # read from finance.import_batches (ImportBatchRecord doesn't carry coverage):
    assert (complete_from, complete_to) == (date(2027, 2, 3), date(2027, 5, 31))
async def test_0026_gives_tembo_batches_imported_before_their_full_history(...):
    # commit a Tembo statement; set its batch's coverage and its statement's period back to
    # the rows' span; run PERIOD and COVERAGE; they equal what staging gives now
# test_transfers.py (importing)
async def test_a_transfer_into_tembo_on_its_opening_day_pairs(...):
    # a connected account pays the identifier registered to the Tembo account on the opening
    # day; the Tembo statement is imported: one matched transfer, no placeholder
async def test_registering_tembo_after_its_import_counts_its_deposit_once(...):
    # import first, then register the identifier and heal: matched, and the Tembo account's
    # balance equals the statement's closing (the 2026-11-21 production case)
```

- [ ] **Step 2: Run the tests and confirm they fail.** Run `cd backend && DOCKER_HOST=ssh://docker-test uv run pytest tests/finance/importing/test_pdf_layouts.py tests/finance/importing/test_staging.py tests/finance/importing/test_transfers.py -q -p no:cacheprovider -k tembo`. Expected: FAIL.

- [ ] **Step 3: Implement the layout change, the `_load_known` rule and the synthetic builder's two fields.**

- [ ] **Step 4: Write migration 0026,** for batches with `profile_config ->> 'layout' = 'tembo_statement'` whose statement has `account_opened_on` and `document_date`:
  - `PERIOD` sets the statement's `period_from` to `account_opened_on` and `period_to` to `document_date`.
  - `COVERAGE` sets the batch's `covered_from` to `account_opened_on` and `covered_to` to `greatest(covered_to, document_date)`.
  - `COVERAGE` also sets `complete_from` to `account_opened_on` and `complete_to` to `document_date - 1`, leaving both null when that range is empty.
  - Downgrade: nothing to undo. Write a no-op, with a comment saying why.

- [ ] **Step 5: Run the tests and confirm they pass.** Same command as Step 2, without `-k`. Expected: PASS.

- [ ] **Step 6: Update the READMEs.**

- [ ] **Step 7: Commit.**

```bash
git add backend/src/lararium/modules/finance/importing/pdf/tembo.py backend/src/lararium/modules/finance/transfers/pairs.py backend/tests/finance/importing/synthetic_pdfs.py backend/migrations/versions/0026_finance_tembo_full_history.py backend/tests/finance/importing/test_pdf_layouts.py backend/tests/finance/importing/test_staging.py backend/tests/finance/importing/test_transfers.py README.md README.zh-CN.md
git commit -S -m "fix: treat a Tembo statement as listing everything from the account's opening"
```

### Task 7: Property tests for the new evidence

**Files:**
- Create: `backend/tests/finance/transfers/test_properties_evidence.py`. It holds its own small scenario strategy; `strategies.py` stays untouched.

**Interfaces:**
- **Consumes:** Tasks 1, 3 and 5. It uses the conftest's `imported_bank`, `statement_row`, `duplicate_row` and `RowDetails`, and `pairs.WEAK`.
- **Scenario:**
  - **Accounts:** two or three manual `transactions`-mode accounts, each with a registered identifier, and one sub-account of the first. Every account's imports list every scenario day in full.
  - **Transfers:** one to four between them, with amounts from `(Decimal(50), Decimal(100))` so they collide.
  - **Each transfer's evidence:** one of
    - the receiving row prints the sender's account number;
    - the sending row prints the receiver's account number;
    - sub-account (when one side is the sub-account);
    - the sending row prints the receiver's sort code alone.
  - **Noise:** bank-transfer rows of the same amounts naming nobody, or naming a stranger (`"500000:50000001"`).
  - **Duplicates:** each row may get a duplicate, with the details on either the committed row or the duplicate.
  - **Arrival:** in a drawn order.
- **Settings:** follow `test_properties.py`'s settings and its CI example count.

- [ ] **Step 1: Write the property test, `test_new_evidence_is_order_independent_idempotent_and_never_anticipates_an_imported_row`.** For each scenario and two drawn arrival orders, assert:
  - the set of `(from_account_id, to_account_id, amount, status)` from `list_transfers` and every record's contra are equal for both orders;
  - a second `heal()` changes no transfer and no contra;
  - `await placeholders(session) == []`, since every day is imported in full;
  - no transfer has exactly one own record whose evidence is only `weak_holder`;
  - net worth equals the sum of the accounts' true balances (`net_worth_on`).

- [ ] **Step 2: Run it.**
  - Run: `cd backend && DOCKER_HOST=ssh://docker-test uv run pytest tests/finance/transfers/test_properties_evidence.py -q -p no:cacheprovider`
  - Expected: PASS. A failure is a real defect, to be fixed at its root in the task that owns the code, with the shrunk example kept as a regression test there.

- [ ] **Step 3: Commit.**

```bash
git add backend/tests/finance/transfers/test_properties_evidence.py
git commit -S -m "test: pin order independence and no anticipation for the new transfer evidence"
```

## Finish

- [ ] `DOCKER_HOST=ssh://docker-test make check` from the repo root: green.
- [ ] `DOCKER_HOST=ssh://docker-test make real-check REAL_ROOT=<the owner's private/statements>`: green, results unchanged.
- [ ] Push `feat/claude-transfer-evidence` and open a PR against `main`, not merged. The PR body:
  - links the spec and this plan;
  - lists the migrations;
  - states the expected production effect (spec §6).

After the owner merges and deploys (the owner's call; not part of this plan's execution):

- [ ] Set Revolut Savings' main account to Revolut, and LemFi Savings' to LemFi, on the Accounts page.
- [ ] Register Tembo's bank account from the TEMBO suggestion on the Transfers page.
- [ ] Rerun the read-only production counts from the spec's §6 and report them:
  - unpaired own transfers, by pair of accounts;
  - Needs attention;
  - every statement's closing balance against the ledger.
