# Card Refunds Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A card refund counts as negative spending in the period it is booked, and a Monzo authorisation its reversal cancels exactly counts as neither spending nor income and stops being pending.

**Architecture:** A new typed column `finance.transactions.refund` is set by the sources that can tell a card refund: Monzo, eToro Money and the import formats with a card signal. The cash-flow report reads a marked positive flow as negative spending. A Monzo reversal that matches its authorisation exactly reuses the eToro cancellation link (`cancels_transaction_id`): both post on In transit and the authorisation is booked. The transfer engine already handles such pairs and does not change.

**Tech Stack:** Python 3.13, SQLAlchemy (async), Alembic, PostgreSQL 18, pytest with testcontainers and Hypothesis; the web client's types are generated from the OpenAPI document.

**Spec:** `docs/superpowers/specs/2026-11-27-card-refunds-design.md`

## Global Constraints

- Only the Monzo normalizer, the eToro Money records and the import commit set `refund`. The transfer engine never reads it.
- Only the eToro statement ledger and the Monzo normalizer write `cancels_transaction_id`.
- A transaction that is not a refund, an authorisation reversal or the authorisation it cancels is posted, paired and reported exactly as today.
- `card_types` keeps its name and meaning ("not a bank transfer"). The new setting is `card_payment_types`.
- One migration, `0047_finance_transaction_refund.py` (`down_revision = "0046"`). Tasks 2 and 4 add statements to it. If `main` has gained a `0047` by the time this merges, renumber after it.
- `NORMALIZER_VERSION`: Monzo `monzo-norm-5` → `monzo-norm-6` (Task 4, once); eToro `etoro-norm-4` → `etoro-norm-5` (Task 3).
- Public repository: tests, docs and commit messages use invented amounts, times, dates and ids, never the owner's.
- Errors, flags and warnings name fields and ids, never an amount or a merchant's name.
- Documents and commit messages are in English. Commit titles are `<type>: <summary>`; no attribution lines.
- Commit by explicit path, never `git add -A`: others may edit the worktree at the same time.
- Tests run on the remote Docker host: `cd backend && DOCKER_HOST=ssh://docker-test uv run --locked pytest <paths>`. Never local Docker.
- Run only the tests named in each task, plus `uv run --locked ruff check` and `uv run --locked pyright` on the changed files. CI runs the whole suite on the pull request; do not run whole test directories.
- The branch is `fix/claude-card-refunds`.

## Review Focus

1. **A positive row that is not a card payment, on a profile whose `card_types` lists it.** A cheque paid in (`CHQ`), a cash machine code (`ATM`, `CPT`) or interest must not be a refund. Test in Task 2.
2. **A Monzo card refund that is a transfer leg** (Trading 212 paying a card deposit back to the card). It is marked a refund but pairs as today, and once paired it is no flow. Test in Task 4.
3. **The owner voids one side of a Monzo pair.** The other posts as its own record says, and stays there on the next sync instead of moving back and forth. Test in Task 5.
4. **A reversal arriving in a sync whose pages don't hold its authorisation** (the authorisation was fetched only by an earlier sync). It still pairs. Test in Task 5.
5. **A replay under the new version.** It gives the same ledger as the syncs that brought the records one by one. Test in Task 5 (property).

---

### Task 1: The refund mark and the cash-flow report

**Files:**
- Create: `backend/migrations/versions/0047_finance_transaction_refund.py`
- Modify: `backend/src/lararium/modules/finance/models.py` (class `Transaction`)
- Modify: `backend/src/lararium/modules/finance/ledger.py` (`TransactionDetails`, `TransactionRecord` loading, the two detail writes near `include_in_spending`)
- Modify: `backend/src/lararium/modules/finance/schemas.py` (`TransactionOut`)
- Modify: `backend/src/lararium/modules/finance/cashflow.py` (`_FLOWS`, `cash_flow`, module docstring)
- Modify (generated): `web/openapi.json`, `web/src/api/*.gen.ts`
- Test: `backend/tests/finance/test_cashflow.py`, `backend/tests/finance/test_connector_ledger.py`

**Interfaces:**
- Produces: `TransactionDetails.refund: bool = False`; the record's details carry `refund`; `TransactionOut.refund: bool`; column `finance.transactions.refund boolean NOT NULL DEFAULT false`.

- [ ] **Step 1: Write the failing tests.** In `test_cashflow.py`, let the `move` fixture pass `refund` through to `TransactionDetails` (add it to the popped keys). Then:

  - `test_a_refund_lowers_spending_and_is_no_income`: `move("-100", date(2026, 11, 2), category="shopping", payee="Shop")` and `move("60", date(2026, 11, 9), category="shopping", payee="Shop", refund=True)`; assert `(flow.income, flow.spending) == (0, 40)`, shopping's `CategoryTotal` is `(income 0, spending 40)`, and the payees are `[PayeeTotal("Shop", Decimal("40"))]`.
  - `test_a_refund_counts_in_its_own_month`: purchase −100 on 2026-10-30, refund +100 on 2026-11-03; the October period's spending is 100, November's is −100, and both periods' income is 0.
  - `test_a_refund_the_source_says_is_not_spending_is_left_out`: `move("60", ..., refund=True, include_in_spending=False)` gives income 0 and spending 0.
  - `test_a_refund_shares_its_category_splits`: a refund of +30 with the splits of `test_category_splits_share_the_amount` (signs reversed) gives groceries spending −20 and eating_out −10.
  - `test_a_positive_flow_that_is_no_refund_is_still_income`: `move("60", ...)` without `refund` is income 60.

  In `test_connector_ledger.py`:

  - `test_a_connector_transaction_records_that_it_is_a_refund`: post with `details=TransactionDetails(refund=True)`; the loaded record says so. Re-post under the same key with `refund=False`; the outcome is `"updated"` and the record says `False`.

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && DOCKER_HOST=ssh://docker-test uv run --locked pytest tests/finance/test_cashflow.py tests/finance/test_connector_ledger.py -k "refund" -v`
Expected: FAIL, `TransactionDetails` has no `refund`.

- [ ] **Step 3: Write migration `0047`** in the style of `0042_finance_transaction_never_transfer.py`: `ALTER TABLE finance.transactions ADD COLUMN refund boolean NOT NULL DEFAULT false`. Downgrade drops it.

- [ ] **Step 4: Carry the column through the ledger** as `include_in_spending` is carried: model, `TransactionDetails`, the record's details, both writes, `TransactionOut`. A difference counts as a change in `upsert_connector_transaction`.

- [ ] **Step 5: Read it in the report.** `_FLOWS` also selects `t.refund`. In `cash_flow`'s loop, a row with `amount > 0` and `row.refund` true goes to the spending branch, guarded by `include_in_spending is not False`. That branch already subtracts a negative amount, so the same code adds a refund's amount with the opposite sign: `spending_by_period[period] -= amount`, and likewise for each category share and for the payee. Rewrite the module docstring's "positive is income, negative is spending" to say that a refund the source marks is negative spending (spec §4.5).

- [ ] **Step 6: Run the tests and see them pass**

Run: the command of Step 2, then `uv run --locked pytest tests/finance/test_structured_schema.py -q`
Expected: PASS.

- [ ] **Step 7: Regenerate the API client**

Run: `make openapi` (from the repository root)
Expected: the transaction schema gains `refund`; nothing else changes.

- [ ] **Step 8: Commit**

```bash
git add backend/migrations/versions/0047_finance_transaction_refund.py \
  backend/src/lararium/modules/finance/models.py backend/src/lararium/modules/finance/ledger.py \
  backend/src/lararium/modules/finance/schemas.py backend/src/lararium/modules/finance/cashflow.py \
  backend/tests/finance/test_cashflow.py backend/tests/finance/test_connector_ledger.py \
  web/openapi.json web/src/api
git commit -m "feat: count a refund the source marks as negative spending"
```

---

### Task 2: Imports mark card refunds

**Files:**
- Modify: `backend/src/lararium/modules/finance/importing/profiles.py` (`CsvProfileConfig`, `OfxProfileConfig`, `QifProfileConfig`, `PdfProfileConfig`; new `is_card_payment`; module docstring)
- Modify: `backend/src/lararium/modules/finance/importing/catalog.py` (the entries of spec §4.2)
- Modify: `backend/src/lararium/modules/finance/importing/commit.py` (`_card_types` → `_profile_codes`, the `TransactionInput` in the commit loop)
- Modify: `backend/migrations/versions/0047_finance_transaction_refund.py`
- Test: `backend/tests/finance/importing/test_profiles.py`, `test_catalog.py`, `test_commit.py`; `backend/tests/finance/test_card_refund_migration.py` (new)

**Interfaces:**
- Consumes: `TransactionDetails.refund` (Task 1).
- Produces: `card_payment_types: CardTypes` on every profile config, default `[]`, except `OfxProfileConfig`, whose default is `["POS"]`; `is_card_payment(card_payment_types: list[str], bank_type: str | None, card_number: str | None) -> bool`: true when `card_number` is not None or `bank_type.strip()` is in `card_payment_types`.

- [ ] **Step 1: Write the failing tests.**

  - `test_profiles.py::test_a_card_number_or_a_card_payment_type_is_a_card_payment`: `is_card_payment(["VIS"], "VIS", None)`, `is_card_payment([], None, "123456******1234")`; not `is_card_payment(["VIS"], "CHQ", None)`; not `is_card_payment([], None, None)`; `OfxProfileConfig(format="ofx").card_payment_types == ["POS"]`.
  - `test_catalog.py::test_card_payment_types`: per entry, the exact list of spec §4.2: HSBC PDF and midata `[")))", "VIS"]`; Lloyds CSV and PDF `["DEB"]`; Revolut Current CSV `["Card Payment", "Card Refund"]`; BOC UK PDF `["Pre-Authorization Completed"]`; HSBC OFX `["POS"]`; every other entry `[]`. Also, for every entry, no code of `card_payment_types` is a cash machine, cheque or interest code (`ATM`, `CPT`, `CHQ`, `CHECK`, `Gross Interest`, `Interest`, `Interest Adjustment`).
  - `test_commit.py::test_a_positive_card_row_commits_as_a_refund`: a CSV profile with a `type` column and `card_payment_types=["VIS"]`, staging three rows: `VIS` +60.00, `VIS` −100.00, `CHQ` +25.00. After commit only the first row's transaction has `refund` true.
  - `test_commit.py::test_a_card_number_marks_a_refund_whatever_its_type`: a positive row with a card number and no type commits as a refund.
  - `test_card_refund_migration.py`, in the style of `test_shared_payee_holders_migration.py` (migrate to `0046`, seed, upgrade to `0047`):
    - a stored profile with the HSBC PDF config gains `card_payment_types` `[")))", "VIS"]` and `version + 1`;
    - a profile the owner defined (any other config) is untouched;
    - a committed positive `VIS` row's transaction becomes a refund;
    - a negative one, a `CHQ` one and a void one do not;
    - a staged, uncommitted batch's `profile_config` gains the types.

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && DOCKER_HOST=ssh://docker-test uv run --locked pytest tests/finance/importing/test_profiles.py tests/finance/importing/test_catalog.py tests/finance/importing/test_commit.py tests/finance/test_card_refund_migration.py -k "card_payment or refund" -v`
Expected: FAIL, `is_card_payment` is not defined.

- [ ] **Step 3: Add the setting and `is_card_payment`** to `profiles.py`, and the lists to `catalog.py` (new constants beside `_HSBC_CARD_TYPES` etc.). Say in the module docstring how `card_types` and `card_payment_types` differ (spec §4.2).

- [ ] **Step 4: Mark refunds at commit.** Read both lists from the batch's `profile_config`. A statement row's transaction gets `details=TransactionDetails(refund=row.quantity > 0 and not row.interest and is_card_payment(...))`. Opening adjustments get no mark.

- [ ] **Step 5: Extend migration `0047`**, as `0028` updates LemFi profiles:
  - each stored profile whose config matches a catalog entry of spec §4.2 with types gets that entry's list and `version + 1`. Match by `format` plus `layout` for PDFs, and by the entry's identifying columns and date format for CSVs and the OFX entry. Owner-defined profiles that match no entry are left alone;
  - batches with `status = 'staged'` get the same keys merged into `profile_config`;
  - `UPDATE finance.transactions SET refund = true` for each committed statement row: positive `quantity`, not `interest`, transaction not void, and a card number or a `bank_type` in the `card_payment_types` of its batch's profile (`import_batches.profile_id`, as the first statement just updated it). It runs after that statement. A committed batch's `profile_config` stays as it was at staging;
  - downgrade drops nothing extra; the column drop removes the marks.

- [ ] **Step 6: Run the tests and see them pass**

Run: the command of Step 2.
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/src/lararium/modules/finance/importing/profiles.py \
  backend/src/lararium/modules/finance/importing/catalog.py \
  backend/src/lararium/modules/finance/importing/commit.py \
  backend/migrations/versions/0047_finance_transaction_refund.py \
  backend/tests/finance/importing/test_profiles.py backend/tests/finance/importing/test_catalog.py \
  backend/tests/finance/importing/test_commit.py backend/tests/finance/test_card_refund_migration.py
git commit -m "feat: mark imported card refunds by each bank's card payment types"
```

Before Step 3, confirm BOC UK's `Pre-Authorization Completed` against the statement's printed terms (the BOC parser's `_KINDS` comment names them). If they do not make it a card payment, BOC's list is `[]`; change the test and say so in the task report.

---

### Task 3: eToro Money marks card refunds

**Files:**
- Modify: `backend/src/lararium/modules/finance/connector_run.py` (`Run.post`)
- Modify: `backend/src/lararium/modules/finance/etoro/records.py` (the cash transaction posting; module docstring)
- Modify: `backend/src/lararium/modules/finance/etoro/normalize.py` (`NORMALIZER_VERSION = "etoro-norm-5"`)
- Test: `backend/tests/finance/etoro/test_normalize_api.py`

**Interfaces:**
- Consumes: `TransactionDetails.refund` (Task 1).
- Produces: `Run.post(..., details: TransactionDetails | None = None)`, passed through to `TransactionInput.details`.

- [ ] **Step 1: Write the failing tests**, built like `test_card_payment_against_unassigned`:

  - `test_a_card_refund_is_marked_a_refund`: a settled `card` item, `transactionSubtype` `refund`, `direction` `credit`, 9.99; it posts +9.99 on eToro Money against Unassigned with `refund` true, and no flag.
  - `test_a_card_credit_of_another_subtype_is_no_refund_and_is_flagged`: the same with `paymentReceived`; `refund` false, and the run flags `cash_transactions` `transactionSubtype` with the value `paymentReceived`.
  - `test_a_card_payment_is_no_refund`: the existing debit card item has `refund` false.

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && DOCKER_HOST=ssh://docker-test uv run --locked pytest tests/finance/etoro/test_normalize_api.py -k "card" -v`
Expected: FAIL.

- [ ] **Step 3: Implement.** Add `details` to `Run.post`. In `records.py`, for `kind == "card"` and `direction == "credit"`: `refund` is `transactionSubtype == "refund"`; any other subtype calls `run.flag_value("cash_transactions", "transactionSubtype", subtype)`. Bump the version. Add one sentence on card refunds to the module docstring.

- [ ] **Step 4: Run the tests and see them pass**

Run: the command of Step 2.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/src/lararium/modules/finance/connector_run.py \
  backend/src/lararium/modules/finance/etoro/records.py \
  backend/src/lararium/modules/finance/etoro/normalize.py \
  backend/tests/finance/etoro/test_normalize_api.py
git commit -m "feat: mark an eToro Money card refund as a refund"
```

---

### Task 4: Monzo stores its card fields and marks refunds

**Files:**
- Modify: `backend/migrations/versions/0047_finance_transaction_refund.py`
- Modify: `backend/src/lararium/modules/finance/models.py`, `ledger.py`, `schemas.py` (two typed details)
- Modify: `backend/src/lararium/modules/finance/monzo/normalize.py` (`_details`; `NORMALIZER_VERSION = "monzo-norm-6"`)
- Modify (generated): `web/openapi.json`, `web/src/api/*.gen.ts`
- Test: `backend/tests/finance/monzo/test_normalize.py`, `test_normalize_transfers.py`

**Interfaces:**
- Consumes: `TransactionDetails.refund` (Task 1).
- Produces:
  - `TransactionDetails.card_lifecycle_id: str | None = None` (Monzo `metadata.mastercard_lifecycle_id`);
  - `TransactionDetails.card_reversal: bool | None = None` (Monzo `metadata.is_reversal`: `"true"` → True, `"false"` → False, absent → None, anything else → None and the path noted unmapped);
  - columns `card_lifecycle_id text`, `card_reversal boolean`;
  - the Monzo refund rule `_is_refund(tx) -> bool`: `scheme == "mastercard"` and `amount > 0`. Task 5 narrows it for a paired reversal.

- [ ] **Step 1: Write the failing tests** with `make_transaction`:

  - `test_a_positive_card_transaction_is_a_refund`: `amount=1000, scheme="mastercard"` posts with `refund` true; `amount=-1000` and a positive `scheme="payport_faster_payments"` do not.
  - `test_card_metadata_is_stored_typed`: `metadata={"mastercard_lifecycle_id": "mclifecycle_TEST1", "is_reversal": "true"}` gives `card_lifecycle_id == "mclifecycle_TEST1"` and `card_reversal is True`. The connection gets no "not stored yet" warning for either path.
  - `test_normalize_transfers.py::test_a_card_refund_from_a_registered_merchant_still_pairs`: copy the existing test of a Trading 212 withdrawal paid back to the registered card. The positive Monzo transaction has `refund` true and still pairs as before, and `cash_flow` over its day shows no income or spending from it.

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && DOCKER_HOST=ssh://docker-test uv run --locked pytest tests/finance/monzo/test_normalize.py tests/finance/monzo/test_normalize_transfers.py -k "refund or card_metadata" -v`
Expected: FAIL.

- [ ] **Step 3: Implement.**
  - Add the two columns to migration `0047`, and carry them as Task 1 carried `refund`.
  - In `_details`, read the two metadata keys through the `_Reader`, so they are mapped, and set `refund`.
  - Bump the version.
  - Regenerate the API client with `make openapi`.

- [ ] **Step 4: Run the tests and see them pass**

Run: the command of Step 2.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/migrations/versions/0047_finance_transaction_refund.py \
  backend/src/lararium/modules/finance/models.py backend/src/lararium/modules/finance/ledger.py \
  backend/src/lararium/modules/finance/schemas.py backend/src/lararium/modules/finance/monzo/normalize.py \
  backend/tests/finance/monzo/test_normalize.py backend/tests/finance/monzo/test_normalize_transfers.py \
  web/openapi.json web/src/api
git commit -m "feat: store Monzo card lifecycle fields and mark card refunds"
```

---

### Task 5: A Monzo reversal cancels its authorisation

**Files:**
- Create: `backend/src/lararium/modules/finance/monzo/reversals.py`
- Modify: `backend/src/lararium/modules/finance/monzo/normalize.py` (`normalize`, `_Normalizer.transaction`, warnings; module docstring)
- Modify: `docs/superpowers/specs/2026-11-27-etoro-cancelled-withdrawal-design.md` (§2: a second writer), `docs/superpowers/specs/2026-11-15-transfer-matching-design.md` (§5.5), `docs/superpowers/specs/2026-11-22-binance-design.md` (the Pay row: `PAY_REFUND` is a refund when Phase B builds it)
- Test: `backend/tests/finance/monzo/test_reversals.py` (new), `test_normalize.py`, `test_sync.py`

**Interfaces:**
- Consumes: `card_lifecycle_id`, `card_reversal`, `_is_refund` (Task 4); `upsert_connector_transaction`'s `cancels_transaction_id`; the In transit system account (`get_or_create_system_account(command, "in_transit")`).
- Produces: `reversals.reversal_pairs(copies: Iterable[dict[str, Any]]) -> dict[str, str]`, which maps a reversal's Monzo id to its authorisation's. It is pure: the input is raw Monzo transactions and nothing else. Rules from spec §4.3:
  - a reversal has `metadata.is_reversal == "true"`, `amount > 0` and `scheme == "mastercard"`, and no decline;
  - its authorisation has the same `metadata.mastercard_lifecycle_id` and currency, `amount == -reversal.amount`, empty `settled`, no decline, no `is_reversal`, and `created` before the reversal's;
  - reversals are taken in order of `created`, then id. Each takes the latest such authorisation that no earlier reversal took.

- [ ] **Step 1: Write the failing unit tests** in `test_reversals.py` (no database):

  - `test_an_exact_reversal_pairs`: authorisation `tx_A` −1000, unsettled, lifecycle `L`; reversal `tx_R` +1000 later, same `L`, `is_reversal` `"true"`; returns `{"tx_R": "tx_A"}`.
  - `test_a_shared_lifecycle_without_a_reversal_pairs_nothing`: two settled −700 and −360 on one lifecycle; returns `{}`.
  - `test_a_partial_reversal_pairs_nothing`: +700 against −1000; returns `{}`.
  - `test_a_settled_authorisation_is_not_reversed`: the authorisation has `settled`; returns `{}`.
  - `test_two_authorisations_of_one_amount_pair_the_latest`: two unsettled −1000 on `L`, one reversal; the later authorisation pairs.
  - `test_a_declined_reversal_pairs_nothing`.

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && DOCKER_HOST=ssh://docker-test uv run --locked pytest tests/finance/monzo/test_reversals.py -v`
Expected: FAIL, no module `reversals`.

- [ ] **Step 3: Implement `reversal_pairs`.**

- [ ] **Step 4: Run them and see them pass.** Same command. Expected: PASS.

- [ ] **Step 5: Write the failing normalizer tests** in `test_normalize.py`, using `_normalize`:

  - `test_a_reversed_authorisation_and_its_reversal_post_on_in_transit`:
    - one sync with both;
    - both contras are In transit, the authorisation's status is `booked` with `value_date` None, and the reversal's `cancels_transaction_id` is the authorisation's id;
    - neither is a refund, and In transit's balance is 0;
    - `cash_flow` over the day reports income and spending 0.
  - `test_a_reversal_in_a_later_sync`: the authorisation alone first (pending, Unassigned), then the reversal alone in a second sync whose pages do not include the authorisation. The ledger ends as in the test above.
  - `test_a_replay_repairs_the_old_posting`: this reproduces production as `monzo-norm-5` left it.
    - Store both raw copies with `store_raw_record`.
    - Post both with `upsert_connector_transaction` under their Monzo ids in the old shape: contra Unassigned, the authorisation pending, no link.
    - Set the connection's `normalizer_version` to `"monzo-norm-5"`.
    - Sync once with neither in the pages.

    The replay pairs them as in the first test. A second replay (version reset again) reports both unchanged.
  - `test_a_voided_reversal_dissolves_the_pair`: after pairing, a sync brings the reversal with `decline_reason`; the reversal is void, and the authorisation is pending against Unassigned. The next sync, unchanged, leaves it so.
  - `test_the_owner_voiding_the_authorisation_leaves_the_reversal_a_refund`: void the authorisation with `void_transaction`, then sync again. The reversal posts against Unassigned, marked a refund and with no link, and the authorisation stays void. One further sync changes nothing.
  - `test_an_unmatched_reversal_is_a_refund_and_warns`: a +700 reversal against a −1000 authorisation. The reversal is a refund against Unassigned, the authorisation stays pending, and the connection's warnings include one naming the reversal's Monzo id.

  In `test_sync.py`:

  - `test_a_reversed_authorisation_is_no_longer_fetched`: after a sync that pairs them, `oldest_pending` for the account is None.

  A property test, `test_reversals.py::test_any_arrival_order_gives_one_ledger`, uses Hypothesis and the database fixtures. It takes a small generated history: authorisations, exact and partial reversals, settled card payments and refunds on a few lifecycles. It splits the history into syncs in a generated order. Normalizing it sync by sync, and normalizing it as one sync followed by a version replay, both give the same set of (external id, status, contra account, refund, cancels external id). Every cancelled pair sums to zero on In transit.

- [ ] **Step 6: Run them and see them fail**

Run: `cd backend && DOCKER_HOST=ssh://docker-test uv run --locked pytest tests/finance/monzo/test_normalize.py tests/finance/monzo/test_sync.py tests/finance/monzo/test_reversals.py -k "revers or no_longer_fetched or arrival" -v`
Expected: FAIL.

- [ ] **Step 7: Wire it into the normalizer.**
  - Before the replay and the pages are posted, `normalize` computes `pairs = reversal_pairs(...)` over the latest stored copies (`_latest_transactions`, which include this sync's pages) and gives it to the `_Normalizer`.
  - In `transaction()`, an authorisation that `pairs` names posts with In transit as its contra and status `booked`.
  - A reversal in `pairs` first makes sure its authorisation is posted as above. If the authorisation is not among this run's pages, it is re-posted from its latest copy. The reversal then posts with In transit, `cancels_transaction_id` set to the authorisation's id, and `refund` false.
  - A transaction posted earlier with a link, whose latest copy no longer pairs, has its authorisation re-posted from its own latest copy, which means pending against Unassigned when Monzo left `settled` empty.
  - An `is_reversal` transaction that pairs nothing adds the warning `Monzo transaction {monzo_id}: a reversal that matches no authorisation; counted as a refund` once, as `_unmapped_warning` is added.
  - An owner's void is never rewritten: a pair with a void side is no pair, and the other side posts as its own record says.
  - Update the module docstring with a bullet for a reversed authorisation.

- [ ] **Step 8: Run the tests and see them pass**

Run: the command of Step 6.
Expected: PASS.

- [ ] **Step 9: Amend the three designs** (spec §6). eToro cancelled withdrawal §2: the link has a second writer, Monzo's normalizer (card reversals design). M4 §5.5: the cancelled-pair case names a Monzo reversal and its authorisation. Binance design, the Pay row: a `PAY_REFUND` is marked a refund when Phase B builds `pay/transactions`.

- [ ] **Step 10: Commit**

```bash
git add backend/src/lararium/modules/finance/monzo/reversals.py \
  backend/src/lararium/modules/finance/monzo/normalize.py \
  backend/tests/finance/monzo/test_reversals.py backend/tests/finance/monzo/test_normalize.py \
  backend/tests/finance/monzo/test_sync.py \
  docs/superpowers/specs/2026-11-27-etoro-cancelled-withdrawal-design.md \
  docs/superpowers/specs/2026-11-15-transfer-matching-design.md \
  docs/superpowers/specs/2026-11-22-binance-design.md
git commit -m "feat: post a Monzo authorisation and its exact reversal as a cancelled pair"
```

---

## After merge and deployment (owner)

The checks of spec §4.6, in production, read-only:

1. The two Monzo reversals and their authorisations are on In transit, and both authorisations are booked.
2. In transit's balance is unchanged.
3. The Monzo sync no longer fetches from those authorisations.
4. Each month with a refund shows spending and income each lower by that month's refunds.
5. The Revolut partial refund's payee shows what was kept.
