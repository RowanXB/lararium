# eToro Cancelled Withdrawal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An eToro withdrawal request the owner cancelled, and its fee, stay booked transactions that follow the statement's balance but count as neither spending nor income, and can't be paired as a transfer.

**Architecture:** A cancellation records the transaction it cancels in a new column, `finance.transactions.cancels_transaction_id`. The eToro statement ledger finds cancelled pairs among the stored Account Activity rows and posts both sides against In transit with the link. The transfer engine accepts a linked pair on In transit and never offers it for pairing. Reports need no change: they do not read In transit as income or spending.

**Tech Stack:** Python 3.13, SQLAlchemy (async), Alembic, PostgreSQL 18, pytest with testcontainers and Hypothesis; the web client's types are generated from the OpenAPI document.

**Spec:** `docs/superpowers/specs/2026-11-27-etoro-cancelled-withdrawal-design.md`

## Global Constraints

- Only the eToro statement ledger writes `cancels_transaction_id`. No other connector, importer or API route sets it.
- A transaction without the link, and that no link names, is posted, paired and reported exactly as today.
- Errors, flags and log lines name sheets, columns and Types, never an amount, a name or a cell of Details.
- Documents and commit messages are in English. Commit titles are `<type>: <summary>`; no attribution lines.
- Commit by explicit path, never `git add -A`: others may edit the worktree at the same time.
- Tests run on the remote Docker host: `cd backend && DOCKER_HOST=ssh://docker-test uv run --locked pytest <paths>`. Never local Docker.
- Run the focused tests named in each task, plus `uv run --locked ruff check` and `uv run --locked pyright` on the changed files; CI runs the whole suite on the pull request.
- Before the first commit, rename the branch: `git branch -m fix/claude-etoro-cancelled-withdrawal`.

## Review Focus

1. **The owner voids one transaction of a pair.** The other must post as it does today (against Unassigned) and stay there on the next normalize, not move back and forth between In transit and Unassigned. Test in Task 3.
2. **The request and its cancellation arrive in different uploads, in either order.** The ledger must be the same as when one statement holds both. Test in Task 3.
3. **A cancellation row whose amount cell holds eToro's placeholder, not a number.** It cancels nothing, and is flagged as such a row is today. Test in Task 3.
4. **Two cancellations of one amount and one request.** The first cancels the request; the second cancels nothing and posts as today. Test in Task 3.
5. **A request cancelled, then the same amount requested again and not cancelled.** The later request is an ordinary withdrawal: against Unassigned and still a pairing candidate. Test in Task 3.

---

### Task 1: The cancellation link on a transaction

**Files:**
- Create: `backend/migrations/versions/0041_finance_transaction_cancels.py`
- Modify: `backend/src/lararium/modules/finance/models.py` (class `Transaction`)
- Modify: `backend/src/lararium/modules/finance/ledger.py` (`TransactionInput`, `TransactionRecord`, `_validate_transaction`, `post_transaction`, `upsert_connector_transaction`, `_load_transaction`)
- Modify: `backend/src/lararium/modules/finance/schemas.py` (`TransactionOut`)
- Modify (generated): `web/openapi.json`, `web/src/api/*.gen.ts`
- Test: `backend/tests/finance/test_connector_ledger.py`

**Interfaces:**
- Produces: `TransactionInput.cancels_transaction_id: UUID | None = None`; `TransactionRecord.cancels_transaction_id: UUID | None`; `TransactionOut.cancels_transaction_id: UUID | None`; column `finance.transactions.cancels_transaction_id`.

- [ ] **Step 1: Write the failing tests** in `test_connector_ledger.py`, using the file's existing fixtures for posting a connector transaction:

  - `test_a_connector_transaction_records_what_it_cancels`: post transaction A, then B with `cancels_transaction_id=A.id`; assert `B.cancels_transaction_id == A.id` on the returned record and on a fresh load.
  - `test_the_cancellation_link_is_replaced_in_place`: re-post B under the same external id with `cancels_transaction_id=None`; the outcome is `"updated"`, B's id is unchanged and its link is `None`. Re-post it once more unchanged; the outcome is `"unchanged"`.
  - `test_a_transaction_cannot_cancel_one_that_is_not_there`: `cancels_transaction_id=uuid.uuid4()` raises `FinanceValidationError`.
  - `test_a_transaction_is_cancelled_by_at_most_one`: a third transaction C with `cancels_transaction_id=A.id` while B holds it raises `FinanceValidationError`.

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && DOCKER_HOST=ssh://docker-test uv run --locked pytest tests/finance/test_connector_ledger.py -k "cancel" -v`
Expected: FAIL, `TransactionInput` has no `cancels_transaction_id`.

- [ ] **Step 3: Write migration `0041`** (`down_revision = "0040"`), in the style of `0040`:

```sql
ALTER TABLE finance.transactions
  ADD COLUMN cancels_transaction_id uuid UNIQUE REFERENCES finance.transactions (id),
  ADD CONSTRAINT transactions_cancels_another CHECK (cancels_transaction_id <> id);
```

Downgrade drops the constraint and the column. If `main` has gained a `0041` by the time this merges, renumber this one after it.

- [ ] **Step 4: Carry the column through the ledger.** Add it to the model, `TransactionInput`, `TransactionRecord` and `TransactionOut`. `_validate_transaction` raises `FinanceValidationError` when the named transaction does not exist or another transaction already cancels it (the one being replaced excepted). `upsert_connector_transaction` reads it with the existing fields, counts a difference as a change, and writes it in its `UPDATE`.

- [ ] **Step 5: Run the tests and see them pass**

Run: the command of Step 2, then `uv run --locked pytest tests/finance/test_ledger.py tests/finance/test_structured_schema.py -q`
Expected: PASS.

- [ ] **Step 6: Regenerate the API client**

Run: `make openapi` (from the repository root)
Expected: `web/openapi.json` and `web/src/api/types.gen.ts` gain `cancels_transaction_id` on the transaction schema, and nothing else changes.

- [ ] **Step 7: Commit**

```bash
git add backend/migrations/versions/0041_finance_transaction_cancels.py \
  backend/src/lararium/modules/finance/models.py backend/src/lararium/modules/finance/ledger.py \
  backend/src/lararium/modules/finance/schemas.py backend/tests/finance/test_connector_ledger.py \
  web/openapi.json web/src/api
git commit -m "feat: record which transaction a transaction cancels"
```

---

### Task 2: The transfer engine accepts a cancelled pair on In transit

**Files:**
- Modify: `backend/src/lararium/modules/finance/transfers/contra.py` (new `in_cancelled_pair`; `eligible_records`)
- Modify: `backend/src/lararium/modules/finance/transfers/queries.py` (`stray_in_transit`, `find_transfer_violations`)
- Modify: `backend/src/lararium/modules/finance/transfers/engine.py` (`_release_stray`)
- Modify: `docs/superpowers/specs/2026-11-15-transfer-matching-design.md` (§5.5, the invariant)
- Test: `backend/tests/finance/transfers/test_cancelled_pairs.py` (new)
- Test: `backend/tests/finance/test_cashflow.py`

**Interfaces:**
- Consumes: `TransactionInput.cancels_transaction_id` (Task 1).
- Produces: `contra.in_cancelled_pair(transaction_id: SQLColumnExpression[UUID] | UUID) -> ColumnElement[bool]`: true when the transaction has `cancels_transaction_id` or another's names it, and neither of the two is void.

- [ ] **Step 1: Write the failing tests** in `test_cancelled_pairs.py`, with the fixtures of `tests/finance/transfers/conftest.py` (`connected_bank`, `run`, `ids`, `heal`, `assert_consistent`, `in_transit_balance`, `contra_account`). A local helper `_pair(bank, amount)` posts, with `upsert_connector_transaction`, a request (`-amount` on the bank's account, `+amount` on In transit, `bank_transfer=True`) and its cancellation (the reverse, `cancels_transaction_id` the request's id), three minutes apart.

  - `test_a_cancelled_pair_stays_in_transit`: after `_pair` and `await heal()`, both contras are still `ids.in_transit`, `await assert_consistent(session)` passes, `await in_transit_balance(session, ids) == 0`, and `finance.transfers` has no row.
  - `test_a_cancelled_pair_is_no_pairing_candidate`: with a registered second bank holding a receipt of the same amount and day, heal opens no transfer naming either transaction of the pair, and neither id is in `snapshot_own_records`.
  - `test_a_pair_that_leaves_money_in_transit_is_a_violation`: re-post the cancellation with another amount; `find_transfer_violations` returns both ids.
  - `test_a_pair_with_a_void_side_is_no_pair`: void the cancellation (`void_transaction`); after `await heal()` the request's contra is `ids.unassigned`.

  In `test_cashflow.py`:

  - `test_a_cancelled_pair_is_neither_income_nor_spending`: the same two transactions inside the test's range; `cash_flow` reports income `0` and spending `0`, and no payee.

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && DOCKER_HOST=ssh://docker-test uv run --locked pytest tests/finance/transfers/test_cancelled_pairs.py tests/finance/test_cashflow.py -k "cancelled" -v`
Expected: the first three FAIL (heal moves both contras to Unassigned; no violation is reported). The cash-flow test may already pass: it pins behaviour the reports have today.

- [ ] **Step 3: Implement `in_cancelled_pair`** in `contra.py`, then use it in three places:

  - `queries.stray_in_transit`: a transaction of a cancelled pair is not stray.
  - `engine._release_stray`: returns without re-pointing when the transaction is in a cancelled pair.
  - `contra.eligible_records`: leaves such a transaction out.

- [ ] **Step 4: Report an unsettled pair** in `find_transfer_violations`: both ids of a cancelled pair unless each has exactly one posting on In transit, the two are in one instrument, and they sum to zero. Extend the function's docstring with this case.

- [ ] **Step 5: Run the tests and see them pass**

Run: the command of Step 2 without `-k`, then `uv run --locked pytest tests/finance/transfers -q -x --ignore=tests/finance/transfers/test_performance.py`
Expected: PASS.

- [ ] **Step 6: Amend the M4 design §5.5.** The invariant sentence becomes: a transaction's contra leg is In transit if and only if it is a leg of a transfer that is not `rejected`, or it is one of a cancelled pair (a transaction with `cancels_transaction_id` and the one it names, neither void), whose two In transit postings are in one instrument and sum to zero. Reference the cancelled-withdrawal design.

- [ ] **Step 7: Commit**

```bash
git add backend/src/lararium/modules/finance/transfers/contra.py \
  backend/src/lararium/modules/finance/transfers/queries.py \
  backend/src/lararium/modules/finance/transfers/engine.py \
  backend/tests/finance/transfers/test_cancelled_pairs.py backend/tests/finance/test_cashflow.py \
  docs/superpowers/specs/2026-11-15-transfer-matching-design.md
git commit -m "feat: keep a cancelled pair on In transit and out of pairing"
```

---

### Task 3: The eToro statement ledger posts cancelled pairs

**Files:**
- Modify: `backend/src/lararium/modules/finance/etoro/statement_ledger.py` (module documentation, new `_cancellations`, `cash_events`)
- Modify: `backend/src/lararium/modules/finance/connector_run.py` (`Run.post`)
- Modify: `backend/src/lararium/modules/finance/etoro/normalize.py` (`NORMALIZER_VERSION`)
- Modify: `docs/superpowers/specs/2026-11-22-etoro-trading212-design.md` (§5.7 and the table row "Withdraw Request then Withdraw Request Cancelled")
- Test: `backend/tests/finance/etoro/test_normalize_statement.py`
- Test: `backend/tests/finance/etoro/test_properties.py` (run, not changed)

**Interfaces:**
- Consumes: `TransactionInput.cancels_transaction_id` (Task 1); the engine's handling of a pair (Task 2).
- Produces: `Run.post(..., cancels: UUID | None = None)`, passed on as `cancels_transaction_id`; `statement_ledger._cancellations(rows: Sequence[Row]) -> dict[str, str]`, each cancelling row's key to the key of the row it cancels; `NORMALIZER_VERSION = "etoro-norm-4"`.

- [ ] **Step 1: Change the two existing tests and write the new ones** in `test_normalize_statement.py`, with `activity_row`, `build_statement`, `transfer_in`, `_ledger`, `_key` and `ledger.upload` as the neighbouring tests use them. `_ledger`'s query gains the id of the transaction each one cancels, returned as that transaction's external id, so a test can read the link.

  - `test_withdraw_request_and_cancel_net_zero` (changed): the request's postings are `(TRADING, "USD", -100.00)` and `("In transit", "USD", 100.00)`; the cancellation's are the reverse and it cancels the request; the Trading account's USD is `1260.00`; `find_transfer_violations` is empty.
  - `test_withdraw_fee_and_cancel` (changed): the `-5.00` fee and the `+5.00` refund both post against In transit with `is_fee` on the Trading posting; the refund cancels the fee; "Withdraw Fee Cancelled" posts nothing.
  - `test_a_refund_needs_its_cancelled_row`: a `-5.00` and a `+5.00` "Withdraw Fee" row with no "Withdraw Fee Cancelled" row; both post against Unassigned, with no link.
  - `test_a_refund_one_second_from_its_cancelled_row`: the `+5.00` "Withdraw Fee" row at 11:00:01 and "Withdraw Fee Cancelled" at 11:00:00; the refund cancels the fee, both against In transit. With the refund at 11:00:02 instead, both fee rows post against Unassigned, with no link.
  - `test_a_cancellation_in_a_later_statement`: upload a statement with the request only (it posts against Unassigned), then one with the cancellation only; the request is now against In transit under the same transaction id, and the cancellation cancels it. A second test, `test_a_request_uploaded_after_its_cancellation`, uploads them the other way round and asserts the same ledger.
  - `test_a_cancellation_with_no_request`: the cancellation posts against Unassigned, with no link.
  - `test_the_later_of_two_requests_is_the_cancelled_one`: requests of `-100.00` at 09:00 and 10:00, a cancellation at 11:00; the 10:00 request and the cancellation are against In transit and linked, the 09:00 request against Unassigned.
  - `test_a_second_cancellation_cancels_nothing`: one request, cancellations at 11:00 and 12:00; the 12:00 one posts against Unassigned, with no link.
  - `test_a_request_after_the_cancellation_is_a_withdrawal`: a request, its cancellation, then a request of the same amount; the last posts against Unassigned and `bank_transfer` is true.
  - `test_a_cancellation_without_a_number_cancels_nothing`: the cancellation's `Realized Equity Change` cell holds "-"; the request posts against Unassigned and the run's flags are those such a row raises today.
  - `test_a_side_the_owner_voided_ends_the_pair`: after a pair is posted, void the cancellation as the owner (`void_transaction` with a user actor), normalize again twice; each time the request is against Unassigned and the second run reports nothing updated.

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && DOCKER_HOST=ssh://docker-test uv run --locked pytest tests/finance/etoro/test_normalize_statement.py -k "withdraw or cancel or refund or request" -v`
Expected: FAIL, the postings are against Unassigned and nothing is linked.

- [ ] **Step 3: Implement `_cancellations(rows)`**, a pure function of the stored Account Activity rows (as `_fee_links` is), rows taken by `(_when(row), row.id)`:

  - A "Withdraw Request Cancelled" row with cash `a > 0` cancels the latest earlier "Withdraw Request" row with cash `-a` that nothing cancels yet.
  - A "Withdraw Fee" row with cash `a > 0` is a refund when a "Withdraw Fee Cancelled" row at most one second before or after it has `Amount` of absolute value `a`. Each "Withdraw Fee Cancelled" row vouches for one refund: the nearest in time that no other vouches for, the earlier row (by time, then row id) on a tie. A refund cancels the latest earlier "Withdraw Fee" row with cash `-a` that nothing cancels yet.
  - A row whose cash is not a number cancels nothing and is cancelled by nothing.

- [ ] **Step 4: Post the pairs in `cash_events`.** Add `cancels` to `Run.post`. For each pair with a row among the run's rows, post both rows (the cancelled one first, the other read from `statement.stored` when the run did not bring it): the same key, time, description, amount, counterparty fields and `is_fee` as today, the contra the In transit system account, and `cancels` on the cancelling row. Then, if `run.live` finds either of the two no longer live, post the live one again the way it posts today. A row in no pair posts as today.

- [ ] **Step 5: Set `NORMALIZER_VERSION = "etoro-norm-4"`** and update the module documentation of `statement_ledger.py`: the "Withdraw Request" and "Withdraw Fee" lines say what a cancelled pair does.

- [ ] **Step 6: Run the tests and see them pass**

Run: the command of Step 2, then `uv run --locked pytest tests/finance/etoro -q`
Expected: PASS, the five properties of `test_properties.py` included (their generated histories already hold a withdraw request and its cancellation).

- [ ] **Step 7: Run the owner's real statements**

Run: `DOCKER_HOST=ssh://docker-test make real-check ETORO_STATEMENT=<path>` (from the repository root). The owner gives the path of the eToro statement (xlsx); ask for it, do not search for it.
Expected: PASS. Report its summary only, never a row or an amount.

- [ ] **Step 8: Amend the M6 design.** §5.7: "Withdraw Request" and "Withdraw Request Cancelled" go through `transfer_contra` unless they are a cancelled pair, which posts against In transit, linked; the same for "Withdraw Fee" and its refund. The table row reads: both post against In transit, the cancellation linked to its request; neither is spending or income. Reference the cancelled-withdrawal design.

- [ ] **Step 9: Commit**

```bash
git add backend/src/lararium/modules/finance/etoro/statement_ledger.py \
  backend/src/lararium/modules/finance/etoro/normalize.py \
  backend/src/lararium/modules/finance/connector_run.py \
  backend/tests/finance/etoro/test_normalize_statement.py \
  docs/superpowers/specs/2026-11-22-etoro-trading212-design.md
git commit -m "fix: post a cancelled eToro withdrawal against In transit, not as spending"
```

---

### Task 4: Open the pull request

- [ ] **Step 1: Lint and type-check the changed files**

Run: `cd backend && uv run --locked ruff check . && uv run --locked ruff format --check . && uv run --locked pyright && uv run --locked lint-imports`
Expected: no findings.

- [ ] **Step 2: Commit the design and this plan**

```bash
git add docs/superpowers/specs/2026-11-27-etoro-cancelled-withdrawal-design.md \
  docs/superpowers/plans/2026-11-27-etoro-cancelled-withdrawal.md
git commit -m "docs: design and plan for a cancelled eToro withdrawal"
```

- [ ] **Step 3: Push and open the pull request** against `main`, titled `fix: post a cancelled eToro withdrawal against In transit, not as spending`. Its description states what changes for the owner (November's spending and income each fall by the pair's and the fee's value), the new column and migration `0041`, and the check to make after deployment. Let CI run the whole suite.

- [ ] **Step 4: After deployment (the owner's go-ahead, not part of the pull request).** A read-only query in production confirms: the four transactions of the owner's cancelled withdrawal on the Trading account are booked with their contra on In transit; the cancellation and the refund each have `cancels_transaction_id`; the sum of In transit's postings is what it was before; `find_transfer_violations` is empty.
