# Transfer Matching (M4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When money moves between two of the owner's own accounts, both balances change and net worth never dips or double-counts. This works for every source, starting with Monzo, through one generic matching engine with a registry, an In transit account, inferred placeholders, a review page and the interface M2 imports will call.

**Architecture:**
- **Records.** Each account keeps only its own institution's records. A record identified as a transfer posts its other leg against the asset account "In transit" instead of "Unassigned".
- **Placeholders.** For a manual counterpart, an inferred placeholder (In transit → that account) stands in until its real record arrives.
- **Transfers table.** A `finance.transfers` row links the legs.
- **Matching.** It is query-driven (`heal_transfers`): it finds every record whose state disagrees with the rule and matches it. It runs in a short transaction after each sync commits, after every API change, and periodically.

**Tech Stack:** as plan 3 (FastAPI, SQLAlchemy async, PostgreSQL 18, raw-SQL Alembic, Procrastinate, React/Vite/TanStack, hey-api client). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-15-transfer-matching-design.md` (the M4 design; read it first), with its parent `docs/superpowers/specs/2026-11-11-financial-gate-phase1-design.md` §6.3, §6.6, §8, §10.2.

**Builds on:** `main` at `bdcb64a` (migrations up to `0012`). The M2 import plan is being implemented on its own branch at the same time. This plan does not touch M2 code, and whichever branch merges second renumbers its migrations.

## Decisions

Choices the design leaves to the builder, made here so tasks don't have to:

1. **Module layout.**
   - A package `lararium.modules.finance.transfers` holds the engine.
   - `identifiers.py` holds the registry.
   - `transfers_api.py` holds the routes, mounted like `connections_api.py`.
   - `core` is untouched. The worker imports the engine, as it already imports the Monzo module.
2. **Eligibility.** A transaction can be a transfer leg ("an own record") when it is not void, not `origin='inference'`/`'rule'`, and has exactly two postings:
   - one on the system account Unassigned or In transit (its **contra**);
   - one on an account that is not a system account (its **own posting**: own account, instrument, signed quantity).

   Pot moves (contra is the pot), opening adjustments (contra is Opening balances) and multi-posting manual transactions are never eligible.
3. **Source reference.** `source_ref(tx) -> tuple[str, str]` is `("connection", f"{connection_id}:{external_id}")` for connector rows, else `("transaction", str(tx.id))`. Decisions are keyed by it. M2 will add its own kind for import rows.
4. **Direction and window.**
   - A negative own quantity is the sending side (`out`), a positive one the receiving side (`in`).
   - A pairing fits when `in.occurred_at` is within `[out.occurred_at − before, out.occurred_at + after]`.
   - Defaults: `before = 2 days`, `after = 7 days`, `overdue = 7 days`, read from the setting `finance.transfers` (`{"window_before_days", "window_after_days", "overdue_days"}`, each optional, positive integers) when present.
5. **Status and review.**
   - `status` is one of `awaiting | inferred | matched | before_history | rejected` and describes the ledger state.
   - `review_reason` is `several_candidates | amount_differs | NULL` and is orthogonal to it (the design's "needs review").
6. **Matching one record R** (`match_transaction`). First dissolve R's current non-rejected transfer, if any: delete its placeholder and settlement, delete the row, and queue its other own record for matching too. Then:
   1. **Rejected.** R has a `reject` decision: ensure its contra is Unassigned. Done.
   2. **Paired by the owner.** R has a `pair` decision with S, and S is eligible, opposite in sign, same instrument and amount: dissolve S's transfer, link R and S as `matched`. Done.
   3. **Identified through the registry.** R's `counterparty_identifier_id` points at a registry row whose `account_id` X is an own account other than R's own account. Collect the candidates, excluding any with a `never_pair` decision against R:
      - **(i)** transfers between R's account and X in R's role, same instrument and amount, missing R's side, with status `awaiting`, `inferred` or `before_history`, whose other record fits the window;
      - **(ii)** loose records on X: eligible, not linked, contra Unassigned, opposite sign, same instrument and amount, fitting the window, and either `bank_transfer` or identified back to R's account.

      Then:
      - **Exactly one candidate:** link it (deleting any placeholder or settlement) → `matched`.
      - **Two or more:** open R's own transfer (below) with `review_reason = several_candidates`.
      - **None, but one or more with a different amount** (same accounts, instrument and window): open R's own transfer with `amount_differs`.
      - **None at all:** open R's own transfer:
        - **X is manual:** `inferred` with a placeholder.
        - **X is connected, and the counterpart's latest possible time is before X's history** (the earliest non-void record of X's connection posting on X, other than one posted against Opening balances): `before_history`, with a settlement In transit ↔ Opening balances.
        - **Otherwise:** `awaiting`.

      R's contra is re-pointed to In transit.
   4. **Fallback by amount and date.** R is not identified, its `bank_transfer` is true, and its contra is Unassigned. Candidates are open transfers missing a side on R's account in R's role, with the same instrument and amount, fitting the window, and not `never_pair`.
      - **Exactly one:** link it, re-point R to In transit → `matched`.
      - **Two or more:** set `several_candidates` on each candidate transfer; R stays Unassigned.
   5. **Otherwise:** ensure R's contra is Unassigned.
7. **Placeholders and settlements.**
   - **Placeholder:** `origin='inference'`, `status='inferred'`, `occurred_at` and `booking_date` copied from the record it mirrors, description `"Transfer from <A>"` / `"Transfer to <A>"` (A is the record's own account name), postings: counterpart account ±amount and In transit ∓amount.
   - **Settlement:** `origin='rule'`, `status='booked'`, description `"Transfer before <X>'s history"`, postings In transit ∓amount and Opening balances ±amount.
   - Both are derived: replacing or dissolving deletes them (`delete_derived_transaction`), never voids them.
8. **Heal** (`heal_transfers`). Under the advisory lock `pg_advisory_xact_lock(hashtext('finance.transfers'))`, collect the record ids in these states and `match_transaction` each, ordered by `occurred_at, id`:
   - (a) eligible with contra In transit but not a leg of a non-rejected transfer;
   - (b) eligible with contra Unassigned, identified through the registry, no `reject` decision;
   - (c) eligible with contra Unassigned, `bank_transfer`, not linked, with a fallback candidate as in 6.4;
   - (d) legs that are void or ineligible, or whose amount, instrument or accounts no longer fit their transfer, and placeholders whose dates differ from their record's;
   - (e) `pair` decisions not yet realised.

   Repeat until a pass finds nothing (at most 5 passes, then log an error and stop). A second `heal_transfers` right after one writes nothing.
9. **When heal runs.**
   - In the worker, right after a sync commits, in its own `command_context` (actor `Actor("system", "transfers")`); failures are logged, not raised.
   - In every API request that changes identifiers, decisions or voids a transaction, inside that request's transaction.
   - Periodically in `check_transfers` (every 30 minutes).
   - The Monzo normalizer never calls the matcher. It only uses `transfer_contra` for each transaction's contra.
10. **Counterparty fields (Monzo).** The owner's data-model rule (2026-11-15): every field a source provides is stored as a typed field, and the counterparty's bank account lives in the registry, linked by reference. So no JSON:
    - `counterparty_identifier_id`: the registry row of `counterparty.sort_code` + `counterparty.account_number` (digits only), found or created through `get_or_create_identifier` in a short transaction of its own (like the system accounts), when both are present;
    - `counterparty_name` from `counterparty.name`;
    - `payment_reference` from `notes` (non-empty only);
    - `bank_transfer = scheme in {"payport_faster_payments", "bacs", "monzo_to_monzo"}`.
    - **Existing history.** The first sync after deploy fills these fields on every transaction this connection already posted. It reads the latest raw record of each Monzo transaction id, sets the fields through `set_counterparty_fields`, and then sets the connection config flag `counterparty_fields_v1 = true`.
11. **Own identifiers (Monzo).** For every Monzo account with `sort_code` and `account_number`, the normalizer calls `ensure_identifier(ctx, account_id, "uk_bank_account", value)` on every sync. It assigns the registry row to the account when the row is missing or unassigned, does nothing when it is already on that account, and logs a warning when it is on another account (the owner's registration wins).
12. **Balance mode switch.** `set_balance_mode(ctx, account_id, "hybrid")` allows only `snapshots → hybrid`, which values the account exactly as before plus later postings. `assign_identifier` refuses `snapshots` accounts with `FinanceValidationError("switch the account to hybrid before registering its identifiers: a snapshots-only account ignores postings")`.
13. **Foreign keys** from `finance.transfers` to transactions have no cascade. Deleting a linked transaction fails until `unlink_transaction` has run. This protects M2's batch rollback from leaving a dangling transfer.
14. **The invariant check** `find_transfer_violations(session) -> list[UUID]` returns the eligible records whose contra disagrees with being a leg of a non-rejected transfer, plus placeholders and settlements whose transfer is gone. It joins `find_unbalanced_transactions` as an integrity check (tests only; the M9 integrity job will run both).

**Not in this plan:**
- statement import (M2);
- IBKR or other connectors (M5+);
- FX or fee difference postings (the owner's accounts are GBP only);
- moving an account from manual to connected;
- conduit-type intermediaries;
- a transaction list page;
- reconciliation alerts on manual snapshots (M9).

## Global Constraints

- Everything in plans 1 and 3 still applies:
  - Python 3.13, uv lockfile enforced, ruff, pyright strict on `src/`, import-linter;
  - PostgreSQL 18;
  - `NUMERIC`/`Decimal`/strings, and the word `float` nowhere in `src/`;
  - UUIDv7, UTC `timestamptz` plus separate `date`;
  - English UI copy and docs (only the README also has `README.zh-CN.md`);
  - AGPL-3.0-compatible dependencies only.
- Branch `feat/claude-transfer-matching`; commit messages `<type>: <summary>`; signed commits (`git commit -S`).
- Every ledger write goes through `command_context` and is audited.
- Domain code checks every rule the database enforces before writing, so request errors are 4xx with a readable `message`, never 500 (spec §15).
- Synthetic data only. Sort codes, account numbers and names in tests are invented (e.g. `11-22-33` / `12345678`, "Test Owner"); never real ones.
- `make check` green before each commit that ends a task.

## Review Focus

Inputs the spec implies but the main-path tests would not hit, most likely first. Each has a test in its owning task:

- **A Monzo re-sync after a transfer was matched or re-pointed writes nothing.** The contra, postings and counterparty fields compare equal, so there is no flip-flop of postings or events (Task 6: `test_resync_after_match_writes_nothing`).
- **A pending payment to a manual account that is later declined:** the placeholder is deleted and the manual balance is back where it was (Task 6: `test_declined_pending_removes_placeholder`).
- **Matching fails after a sync committed** (the worker died in between): the next heal links it, and net worth was right meanwhile because the record already sat in In transit (Task 6: `test_heal_repairs_after_lost_matching`).
- **A manual account valued from a snapshot taken after some placeholders:** they are absorbed, with no double count (Task 3: `test_placeholder_before_snapshot_absorbed`).
- **Deleting a linked row without unlinking** (an M2 rollback done wrong) fails loudly, and does not leave a transfer pointing at nothing (Task 5: `test_delete_linked_without_unlink_fails`).

## File Structure

```
backend/
  migrations/versions/
    0013_finance_account_identifiers.py   # Task 1
    0014_finance_transfers.py             # Task 2: transactions' counterparty fields, transfers, transfer_decisions
  src/lararium/modules/finance/
    identifiers.py         # Task 1: registry
    accounts.py            # + set_balance_mode (Task 1)
    errors.py              # + DuplicateIdentifierError, IdentifierNotFoundError, TransferNotFoundError (Tasks 1, 5)
    models.py              # + AccountIdentifier, Transfer, TransferDecision; Transaction counterparty fields
    ledger.py              # + counterparty fields, repoint_contra, delete_derived_transaction, set_counterparty_fields (Task 2)
    system_accounts.py     # + "in_transit" (Task 2)
    transfers/
      __init__.py          # re-exports the public names below
      settings.py          # Task 3: TransferSettings, load_transfer_settings
      contra.py            # Task 3: source_ref, transfer_contra, own_leg
      engine.py            # Tasks 3-5: match_transaction, heal_transfers, unlink_transaction, preview_match
      decisions.py         # Task 5: reject, unreject, unpair, pair
      queries.py           # Tasks 3, 6, 7: list/get, candidates, suggestions, due_notifications, find_transfer_violations
    transfers_api.py       # Task 7
    monzo/normalize.py     # Task 6
  src/lararium/worker/jobs.py               # Task 6: heal after sync, check_transfers
  tests/finance/test_identifiers.py         # Task 1
  tests/finance/test_ledger_transfer_support.py   # Task 2
  tests/finance/transfers/conftest.py, test_manual.py, test_connected.py, test_decisions.py, test_imports.py  # Tasks 3-5
  tests/finance/monzo/test_normalize_transfers.py, tests/worker/test_transfer_jobs.py                       # Task 6
  tests/api/test_transfers_api.py           # Task 7
web/src/pages/TransfersPage.tsx (+ .test.tsx), AccountsPage.tsx (+ .test.tsx), router.tsx     # Task 8
README.md, README.zh-CN.md, docs/superpowers/specs/*.md                                        # Task 9
```

---

### Task 1: The registry and the balance-mode switch

**Files:**
- Create: `migrations/versions/0013_finance_account_identifiers.py`, `modules/finance/identifiers.py`, `tests/finance/test_identifiers.py`
- Modify: `modules/finance/models.py`, `modules/finance/accounts.py`, `modules/finance/errors.py`

**Interfaces:**
- Consumes: `accounts.get_account`, `command_context`.
- Produces:
  - **The migration.** `finance.account_identifiers (id uuid PK DEFAULT uuidv7(), kind text NOT NULL CHECK (kind IN ('uk_bank_account')), value text NOT NULL, account_id uuid REFERENCES finance.accounts, dismissed_at timestamptz, created_by text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (kind, value))`, plus `CHECK (kind <> 'uk_bank_account' OR value ~ '^[0-9]{6}:[0-9]{8}$')` and `CHECK (account_id IS NULL OR dismissed_at IS NULL)`. A row is any bank account a source has named: with `account_id` it is one of the owner's accounts; without, a counterparty account not (yet) known to be the owner's; `dismissed_at` marks one the owner said is not theirs.
  - **`IdentifierKind = Literal["uk_bank_account"]`**; the frozen dataclass `IdentifierRecord(id, kind, value, account_id, dismissed_at, created_by, created_at)`.
  - **`normalize_identifier(kind, value: str) -> str`.** It strips spaces and dashes and splits the sort code from the account number on `:` or whitespace or `/`. An input that isn't 6 + 8 digits raises `FinanceValidationError("a UK bank account is a 6-digit sort code and an 8-digit account number")`.
  - **`get_or_create_identifier(ctx, kind, value) -> UUID`.** Normalises, then finds or creates the row (unassigned), serialised by `pg_advisory_xact_lock(hashtext('finance.identifier.' || kind || ':' || value))`; callers in a long sync call it in a short transaction of its own, as with the system accounts. M2 imports use it too.
  - **`assign_identifier(ctx, account_id, kind, value) -> IdentifierRecord`** (the owner registering one). It refuses `snapshots` accounts (Decision 12) and non-asset/liability accounts. A row already assigned to another account raises `DuplicateAccountIdentifierError` (409, names that account). An unassigned or dismissed row is assigned (clearing `dismissed_at`).
  - **`ensure_identifier(ctx, account_id, kind, value) -> bool`** (Decision 11; True when it assigned).
  - **`unassign_identifier(ctx, identifier_id) -> IdentifierRecord`** (the row stays, as transactions reference it; raises `AccountIdentifierNotFoundError` 404).
  - **`dismiss_identifier(ctx, identifier_id) -> IdentifierRecord`** (only for unassigned rows, else `FinanceValidationError`).
  - **`list_identifiers(session, *, account_id: UUID | None = None) -> list[IdentifierRecord]`** and **`identifier_account(session, identifier_id) -> UUID | None`.**
  - **`accounts.set_balance_mode(ctx, account_id, mode: BalanceMode) -> AccountRecord`** (Decision 12; other changes raise `FinanceValidationError`).
  - Each write emits an event (`finance.account_identifier.assigned` / `.unassigned` / `.dismissed`, `finance.account.updated`) and records a change.

- [ ] **Step 1: Write the failing tests:**
  - `test_normalize_accepts_common_spellings`: `"11-22-33 12345678"`, `"112233/12345678"` and `"112233:12345678"` all give `"112233:12345678"`; `"1122 1234"` raises.
  - `test_get_or_create_then_assign_unassign`: get_or_create twice gives one row; assign, `identifier_account` returns the account; unassign keeps the row.
  - `test_assigned_elsewhere_rejected`: assigning to a second account gives a `DuplicateAccountIdentifierError` that names the first account.
  - `test_dismiss_only_unassigned_and_assign_clears_it`.
  - `test_snapshots_account_refuses_identifier`: then `set_balance_mode(..., "hybrid")` succeeds and the assignment works.
  - `test_balance_mode_switch_only_snapshots_to_hybrid`.
  - `test_ensure_identifier_idempotent_and_never_steals`: on another account it returns False and changes nothing.
  - `test_concurrent_get_or_create_one_row`: two real sessions (as in `test_concurrent_callers_share_one_system_account`) get the same id.
  - `test_value_check_enforced_by_database`: a raw insert of a bad value fails.
- [ ] **Step 2: Run** `cd backend && uv run pytest tests/finance/test_identifiers.py -q`; expected: FAIL (module missing).
- [ ] **Step 3: Implement** the migration, model, functions and errors (map `DuplicateAccountIdentifierError` 409 and `AccountIdentifierNotFoundError` 404 in `api.install` like the existing ones).
- [ ] **Step 4: Run** `make backend-check`; expected: green.
- [ ] **Step 5: Commit** `feat: add the registry of the owner's account identifiers`.

### Task 2: Ledger support for transfers

**Files:**
- Create: `migrations/versions/0014_finance_transfers.py`, `modules/finance/transfers/__init__.py`, `tests/finance/test_ledger_transfer_support.py`
- Modify: `modules/finance/models.py`, `modules/finance/ledger.py`, `modules/finance/system_accounts.py`

**Interfaces:**
- Consumes: Task 1.
- Produces:
  - **The migration.**
    - `finance.transactions` gains `counterparty_identifier_id uuid REFERENCES finance.account_identifiers`, `counterparty_name text`, `payment_reference text`, `bank_transfer boolean NOT NULL DEFAULT false`, and an index on `counterparty_identifier_id`.
    - `finance.transfers`:
      - `id`, `from_account_id`, `to_account_id` (`CHECK from <> to`), `instrument_id`, `amount numeric CHECK (amount > 0)`;
      - `status` with the Decision 5 CHECK, `review_reason` with its CHECK;
      - `out_transaction_id`, `in_transaction_id`, `placeholder_transaction_id`, `settlement_transaction_id`, each `UNIQUE` and `REFERENCES finance.transactions` with no cascade;
      - `CHECK (out_transaction_id IS NOT NULL OR in_transaction_id IS NOT NULL)`;
      - `opened_at timestamptz NOT NULL`, `matched_at`, `notified_at`, `created_at`.
    - `finance.transfer_decisions`:
      - `id`, `kind CHECK IN ('reject','never_pair','pair')`;
      - `subject_kind`, `subject_ref` (NOT NULL), `other_kind`, `other_ref` (NULL unless `never_pair`/`pair`), `created_at`;
      - `UNIQUE NULLS NOT DISTINCT (kind, subject_kind, subject_ref, other_kind, other_ref)`.
  - **`ledger.CounterpartyFields`**: frozen; `identifier_id: UUID | None = None`, `name: str | None = None`, `reference: str | None = None`, `bank_transfer: bool = False`.
  - **`ledger.TransactionInput.counterparty: CounterpartyFields = CounterpartyFields()`.** `post_transaction` writes its four columns; `upsert_connector_transaction` compares them with the other fields, so a change is an update. `TransactionRecord` gains `counterparty: CounterpartyFields`.
  - **`ledger.repoint_contra(ctx, transaction_id, from_account_id, to_account_id) -> bool`.** It changes the account of the transaction's single posting on `from_account_id`. It returns False if there is none, and raises `FinanceValidationError` if there are several. It emits `finance.transaction.updated` and records a change.
  - **`ledger.delete_derived_transaction(ctx, transaction_id) -> None`.** Only for `origin IN ('inference','rule')`, else `FinanceValidationError`. It emits `finance.transaction.deleted`.
  - **`ledger.set_counterparty_fields(ctx, transaction_id, fields: CounterpartyFields) -> bool`.** It writes only when different; returns whether it wrote.
  - **`SystemAccountKey`** gains `"in_transit"` → "In transit", `asset`/`in_transit`/`transactions`. The other keys stay `equity`/`other`.

- [ ] **Step 1: Write the failing tests:**
  - `test_counterparty_fields_round_trip`.
  - `test_upsert_treats_changed_counterparty_as_update` and `test_upsert_same_counterparty_unchanged`.
  - `test_repoint_contra_moves_one_posting_and_keeps_balance`.
  - `test_delete_derived_refuses_connector_rows`.
  - `test_in_transit_is_an_asset_counted_in_net_worth`: 100 GBP parked there shows in `net_worth_series`.
  - `test_transfer_needs_a_leg_and_distinct_accounts`: database CHECKs.
- [ ] **Step 2: Run** `uv run pytest tests/finance/test_ledger_transfer_support.py -q`; expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make backend-check`; expected: green.
- [ ] **Step 5: Commit** `feat: give the ledger what transfer matching needs`.

### Task 3: The engine, for manual counterparts

**Files:**
- Create: `transfers/settings.py`, `transfers/contra.py`, `transfers/engine.py`, `transfers/queries.py`, `tests/finance/transfers/conftest.py`, `tests/finance/transfers/test_manual.py`
- Modify: `transfers/__init__.py`, `models.py` (ORM `Transfer`, `TransferDecision`)

**Interfaces:**
- Consumes: Tasks 1 and 2.
- Produces:
  - **`TransferSettings(window_before: timedelta, window_after: timedelta, overdue: timedelta)`** and `load_transfer_settings(session) -> TransferSettings` (Decision 4).
  - **`OwnLeg(account_id, instrument_id, quantity: Decimal, contra_account_id)`** and `own_leg(session, transaction_id) -> OwnLeg | None` (Decision 2).
  - **`source_ref(session, transaction_id) -> tuple[str, str]`** (Decision 3).
  - **`transfer_contra(session, *, source: tuple[str, str] | None, own_account_id: UUID, counterparty_identifier_id: UUID | None) -> UUID`.** It returns the id of In transit or Unassigned per design §5.6 (`source=None` for a transaction not yet posted: steps 1 and 2 are skipped).
  - **`TransferStatus`, `ReviewReason`, `TransferRecord`** (all columns, `amount: Decimal`).
  - **`match_transaction(ctx, transaction_id) -> None`** (Decision 6), **`heal_transfers(ctx) -> int`** (Decision 8; returns how many records it matched).
  - **`queries.get_transfer(session, transfer_id)`**, **`list_transfers(session, *, statuses: Collection[TransferStatus] | None = None) -> list[TransferRecord]`** (newest `opened_at` first), **`find_transfer_violations(session) -> list[UUID]`** (Decision 14).
  - Tests' **conftest:**
    - `connector_tx(...)`: posts a connector transaction through `upsert_connector_transaction`, with its contra from `transfer_contra` and the given counterparty fields, as a normalizer would;
    - `manual_bank(...)`: a `hybrid` manual asset account, with an optional opening snapshot;
    - `ids`: the system account ids.
    - Tests run `heal_transfers` the way the worker does.

- [ ] **Step 1: Write the failing tests** (all with A a connected bank and B a manual `hybrid` account registered as `112233:12345678`):
  - **`test_outgoing_to_manual_account`.** A −100 naming B's identifier, then heal:
    - A's contra is In transit;
    - a placeholder posts B +100 / In transit −100;
    - the transfer is `inferred`;
    - In transit's balance is 0, B is snapshot + 100, net worth is unchanged;
    - `find_transfer_violations == []`.
  - `test_incoming_from_manual_account`: the mirror image (B −100).
  - `test_unidentified_stays_unassigned`: an unassigned identifier, or none.
  - **`test_identifier_of_own_account_is_not_a_transfer`:** A's own identifier registered on A, and an A record naming it.
  - `test_heal_twice_writes_nothing`: the audit log rows before and after the second heal are equal.
  - **`test_placeholder_follows_its_record`:** A's amount changes to −120 → the placeholder is 120; A voided → the placeholder and transfer are gone, B is back.
  - **`test_placeholder_before_snapshot_absorbed`** (Review Focus): B gets a snapshot dated after the placeholder → B's balance equals the snapshot.
  - **`test_registering_later_matches_history`:** three past A transfers naming an unassigned identifier, then `assign_identifier` + heal → three `inferred` transfers.
- [ ] **Step 2: Run** `uv run pytest tests/finance/transfers/test_manual.py -q`; expected: FAIL.
- [ ] **Step 3: Implement** `contra.py`, `settings.py`, `engine.py` (Decision 6 steps 1, 3 and 5 with a manual X; Decision 7 placeholders; Decision 8) and the queries.
- [ ] **Step 4: Run** `make backend-check`; expected: green.
- [ ] **Step 5: Commit** `feat: match transfers to manual accounts with inferred placeholders`.

### Task 4: Connected counterparts, the fallback and review

**Files:**
- Create: `tests/finance/transfers/test_connected.py`
- Modify: `transfers/engine.py`, `transfers/queries.py`

**Interfaces:**
- Consumes: Task 3.
- Produces:
  - Decision 6 step 3 with a connected X (awaiting, loose records, `before_history` and its settlement), step 4 (the fallback), and review reasons.
  - **`queries.candidates(session, transfer_id) -> list[CandidateRecord]`**: `CandidateRecord(transaction_id, account_id, quantity: Decimal, occurred_at, description, reason: Literal["exact", "amount_differs"])`, for the review UI.

- [ ] **Step 1: Write the failing tests** (A and C on two synthetic connections, each registered):
  - **`test_connected_both_orders_same_result`:** sending first then receiving, and in a fresh ledger receiving first then sending.
    - Each ends `matched` with both contras In transit and In transit at 0.
    - The intermediate state is `awaiting` with In transit at ±100, and net worth never moves.
  - **`test_receiver_without_identifier_matched_by_fallback`:** C +100 with `bank_transfer` and no identifier → `matched`.
  - **`test_card_refund_same_amount_not_linked`** (`bank_transfer` false) → the transfer stays `awaiting`.
  - `test_outside_window_not_linked`: C at +8 days.
  - `test_two_candidates_need_review`: `several_candidates`, nothing re-pointed on the candidates; `candidates()` lists both.
  - `test_different_amount_needs_review`: `amount_differs`.
  - **`test_before_history_settles_against_opening_balances`:** A's transfer is 30 days older than C's earliest record → `before_history`, In transit at 0, no review. When an older C record arrives later, heal re-links it as `matched` and deletes the settlement.
  - **`test_concurrent_heals_link_once`:** two real sessions (`create_engine(backend_url)`, as in `test_concurrent_callers_share_one_system_account`) heal at once → one transfer, no violations.
- [ ] **Step 2: Run** `uv run pytest tests/finance/transfers/test_connected.py -q`; expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make backend-check`; expected: green.
- [ ] **Step 5: Commit** `feat: match transfers between connected accounts through In transit`.

### Task 5: The owner's decisions and the interface for imports

**Files:**
- Create: `transfers/decisions.py`, `tests/finance/transfers/test_decisions.py`, `tests/finance/transfers/test_imports.py`
- Modify: `transfers/engine.py`, `transfers/__init__.py`, `modules/finance/errors.py`

**Interfaces:**
- Consumes: Tasks 3 and 4.
- Produces:
  - **`reject(ctx, transfer_id) -> TransferRecord`.** It stores `reject` for every own record. The status becomes `rejected`, the placeholder and settlement are deleted, and the contras are re-pointed to Unassigned.
  - **`unreject(ctx, transfer_id) -> None`.** It deletes those decisions and the row, then matches the records again.
  - **`unpair(ctx, transfer_id) -> None`.** Only for `matched` transfers with two own records; it stores `never_pair` both ways, dissolves the transfer and matches each record again.
  - **`pair(ctx, transfer_id, transaction_id) -> TransferRecord`.** The transaction must be among `candidates()` with reason `exact`; it stores `pair` and matches.
  - All of them raise `TransferNotFoundError` (404), or `FinanceValidationError` (422) when not allowed in the transfer's state, and end with `heal_transfers`.
  - **`unlink_transaction(ctx, transaction_id) -> None`.** For a caller about to delete a row: it dissolves the row's transfer, then matches the transfer's other records. Its placeholder comes back if needed.
  - **`preview_match(session, *, account_id, instrument_id, quantity: Decimal, occurred_at: datetime, counterparty: CounterpartyFields) -> TransferRecord | None`.** It answers which open transfer a row not yet posted would link to (Decision 6 steps 3 and 4 without writing). It returns None when there is none, or more than one.
  - **`unassign_identifier`** (Task 1) is wrapped in the engine as `unassign_identifier_and_rematch(ctx, identifier_id)`, which matches every record naming that identifier again.

- [ ] **Step 1: Write the failing tests:**
  - **`test_reject_survives_heal`:** reject an `inferred` transfer → A's contra is Unassigned, there is no placeholder, and after a re-upsert of A plus heal it is still rejected.
  - `test_unreject_restores`.
  - **`test_unpair_splits_and_never_repairs`:** two connected records `matched`, unpair → both go back to `awaiting`, and heal does not link them again.
  - `test_pair_picks_one_of_two_candidates` and `test_pair_refuses_non_candidate` (422).
  - **`test_import_row_replaces_placeholder`:** B gets a manual-origin transaction +100 with `bank_transfer` (a simulated M2 row), then heal.
    - The transfer is `matched` and the placeholder is gone.
    - B's balance is unchanged from before the row (one +100, not two).
  - `test_preview_match_finds_the_transfer` and `test_preview_match_none_when_ambiguous`.
  - **`test_unlink_then_delete_restores_placeholder`:** the M2 rollback order.
  - **`test_delete_linked_without_unlink_fails`** (Review Focus): an `IntegrityError` from the foreign key.
  - `test_removing_identifier_reverts_to_unassigned`.
- [ ] **Step 2: Run** `uv run pytest tests/finance/transfers/test_decisions.py tests/finance/transfers/test_imports.py -q`; expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make backend-check`; expected: green.
- [ ] **Step 5: Commit** `feat: keep the owner's transfer decisions and expose matching to imports`.

### Task 6: Monzo and the worker

**Files:**
- Create: `tests/finance/monzo/test_normalize_transfers.py`, `tests/worker/test_transfer_jobs.py`
- Modify: `modules/finance/monzo/normalize.py` (bump `NORMALIZER_VERSION` to `monzo-norm-2`), `worker/jobs.py`, `tests/finance/monzo/fixtures.py` (accounts gain synthetic `sort_code`/`account_number`)

**Interfaces:**
- Consumes: Tasks 1-5.
- Produces:
  - **The normalizer:**
    - computes `CounterpartyFields` (Decision 10) for each non-pot transaction;
    - posts the contra from `transfer_contra(source=("connection", f"{connection_id}:{tx_id}") if posted before else None, ...)` (for a new transaction steps 1 and 2 can't apply, so passing the reference always is also correct);
    - passes them to the upsert;
    - ensures each account's own identifier (Decision 11);
    - backfills existing history once (Decision 10, config flag `counterparty_fields_v1`).
  - **The worker:**
    - `run_sync` calls `heal_transfers` in its own `command_context` after a successful sync commits (Decision 9);
    - a new periodic job `check_transfers` (`cron="*/30 * * * *"`) heals, then sends one notification per `queries.due_notifications(session, now)` and marks them with `queries.mark_notified(ctx, transfer_ids)`.
  - **`due_notifications(session, now) -> list[tuple[UUID, str]]`** covers:
    - `awaiting` transfers older than `overdue` with `notified_at IS NULL`: text `"A transfer of 100.00 GBP from Monzo to Broker has been in transit for 8 days."`;
    - transfers with a `review_reason` and `notified_at IS NULL`: `"A transfer of 100.00 GBP from Monzo to Bank A needs your review."`.

- [ ] **Step 1: Write the failing tests:**
  - **`test_faster_payment_to_registered_account`:** a synthetic FPS transaction to a registered manual account, normalize + heal → `inferred`, and the counterparty fields are stored, with the reference from `notes` and the identifier row created unassigned-then-matched.
  - **`test_monzo_registers_its_own_identifiers`:** the owner's prior registration on another account is kept.
  - **`test_resync_after_match_writes_nothing`** (Review Focus): `NormalizeReport` counts every transaction `unchanged`, and no `finance.transaction.updated` event is written.
  - **`test_declined_pending_removes_placeholder`** (Review Focus).
  - **`test_existing_history_gets_fields_once`:** transactions posted by the M3 normalizer (fields empty) get their fields from raw records on the next sync, and the flag stops a second pass.
  - **`test_two_monzo_connections_match`:** the end-to-end connected case through two normalizers.
  - **`test_heal_repairs_after_lost_matching`** (Review Focus): normalize and commit without heal; net worth is already right; `check_transfers` then links it.
  - **`test_overdue_and_review_notified_once`:** a recording notifier gets one message each across two runs.
  - **`test_heal_failure_does_not_fail_sync`.**
- [ ] **Step 2: Run** `uv run pytest tests/finance/monzo/test_normalize_transfers.py tests/worker/test_transfer_jobs.py -q`; expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make backend-check`; expected: green, including the existing Monzo tests, updated only where a fixture account gained its sort code.
- [ ] **Step 5: Commit** `feat: match Monzo transfers and check them from the worker`.

### Task 7: API

**Files:**
- Create: `modules/finance/transfers_api.py`, `tests/api/test_transfers_api.py`
- Modify: `modules/finance/__init__.py` (mount the router), `modules/finance/schemas.py`, `modules/finance/api.py` (the void route heals; map the new errors), `transfers/queries.py` (suggestions), `web/openapi.json` and `web/src/api/*` via `make openapi`

**Interfaces:**
- Consumes: Tasks 1-6.
- Produces (under `/api/v1`, owner-only, the existing cross-site rules; mutating routes end with `heal_transfers` in the request's transaction):
  - **Identifiers:**
    - `GET /accounts/{account_id}/identifiers` → `list[IdentifierOut]`;
    - `POST /accounts/{account_id}/identifiers` (`{kind, value}`) → 201 `IdentifierOut`;
    - `DELETE /accounts/{account_id}/identifiers/{identifier_id}` → 204 (unassigns; the registry row stays);
    - `POST /accounts/{account_id}/balance-mode` (`{balance_mode}`) → `AccountOut`.
  - **Transfers:**
    - `GET /transfers?status=` → `list[TransferOut]`. Each carries the account names, `amount` (string), `currency`, `status`, `review_reason`, `opened_at`, `days_in_transit` (int, for `awaiting`), `overdue` (bool), and the own record and placeholder ids.
    - `GET /transfers/{id}/candidates` → `list[CandidateOut]`.
    - `POST /transfers/{id}/reject | /unreject | /unpair` → `TransferOut` or 204.
    - `POST /transfers/{id}/pair` (`{transaction_id}`) → `TransferOut`.
  - **Suggestions:**
    - `GET /transfer-suggestions` → `list[SuggestionOut {identifier_id, sort_code, account_number, names: list[str], outgoing: int, incoming: int, latest: date}]`, built by `queries.suggestions(session)`: registry rows with no account and not dismissed that transactions name, with the distinct `counterparty_name`s they appeared under, sorted by `outgoing + incoming` desc then `latest` desc.
    - `POST /transfer-suggestions/{identifier_id}/dismiss` → 204.
    - Registering a suggestion is the identifiers POST.

- [ ] **Step 1: Write the failing tests:**
  - `test_register_identifier_matches_history`: the POST, then `GET /transfers` shows `inferred`.
  - `test_snapshots_account_identifier_422_with_message`.
  - `test_duplicate_identifier_409`.
  - `test_reject_and_unreject_round_trip`.
  - `test_pair_candidate_flow`.
  - `test_suggestions_group_and_dismiss`.
  - `test_void_of_a_leg_heals`: voiding A's record through the API removes the placeholder.
  - `test_unknown_transfer_404`.
- [ ] **Step 2: Run** `uv run pytest tests/api/test_transfers_api.py -q`; expected: FAIL.
- [ ] **Step 3: Implement**, then run `make openapi`.
- [ ] **Step 4: Run** `make check`; expected: green (openapi-check included).
- [ ] **Step 5: Commit** `feat: add the transfers and identifiers API`.

### Task 8: Web UI

**Files:**
- Create: `web/src/pages/TransfersPage.tsx`, `web/src/pages/TransfersPage.test.tsx`
- Modify: `web/src/router.tsx` (route `/transfers`, nav "Transfers" after "Accounts"), `web/src/pages/AccountsPage.tsx`, `web/src/pages/AccountsPage.test.tsx`

**Interfaces:**
- Consumes: the Task 7 client functions (hey-api camel-cases the route names: `listTransfers`, `listTransferCandidates`, `rejectTransfer`, `unrejectTransfer`, `unpairTransfer`, `pairTransfer`, `listTransferSuggestions`, `dismissTransferSuggestion`, `listAccountIdentifiers`, `addAccountIdentifier`, `removeAccountIdentifier`, `setAccountBalanceMode`).
- Produces:
  - **The Transfers page**, usable at phone width, with four sections:
    - **Needs attention** (a `review_reason`, or `overdue`): each with its candidates, Pair and Reject.
    - **In transit** (`awaiting`): days in transit.
    - **Suggestions**: Register (pick an existing account) and Dismiss.
    - **Recent**: matched, inferred and before-history, one row each (`"Monzo → Bank A · 100.00 GBP · inferred"`), collapsed by default, with Reject (or Unpair for matched, Undo for rejected).
    - API errors are shown via `lib/errors` as on the other pages.
  - **On the Accounts page**, each asset or liability account gets an "Identifiers" disclosure:
    - its list, with Remove;
    - an add form (sort code, account number);
    - for a `snapshots` account, the line "Switch to hybrid to register identifiers" with a button.

- [ ] **Step 1: Write the failing tests** (vitest, mocked client as in the existing page tests):
  - `renders sections from the API`.
  - `pairs a candidate`.
  - `rejects a transfer`.
  - `registers a suggestion against an account`.
  - `adds and removes an identifier`.
  - `offers the hybrid switch for a snapshots account`.
- [ ] **Step 2: Run** `cd web && pnpm test --run`; expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make check`; expected: green.
- [ ] **Step 5: Commit** `feat: add the transfers page and account identifiers to the web UI`.

### Task 9: Docs and the owner's check

**Files:**
- Modify:
  - `README.md` and `README.zh-CN.md`, kept in step: a "Transfers between your accounts" section covering registering identifiers, hybrid accounts with an opening snapshot, the Transfers page and what each section means;
  - `docs/superpowers/specs/2026-11-11-financial-gate-phase1-design.md`:
    - §8.2: a placeholder is replaced by the receiver's real record rather than marked confirmed;
    - §15: decision-log rows for the unified In transit rule, GBP-only transfers in M4, and snapshots-mode accounts carrying no identifiers;
  - the M4 design doc's status line ("implemented").

- [ ] **Step 1: Write the docs.**
- [ ] **Step 2: Run** `make check`; expected: green.
- [ ] **Step 3: Commit** `docs: describe transfer matching`.
- [ ] **Step 4: The owner's local check** (not committed, no real data in the repo). After deploying, the owner:
  - creates each manual account as `hybrid` with an opening snapshot;
  - registers its identifiers, or registers them from Suggestions;
  - checks on the Transfers page that the known own transfers show as `inferred`, that In transit is 0, and that each manual account's balance matches its own statement since the snapshot.

## Self-review notes

- **Design coverage:**
  - §5.1 rule and §5.2 accounts: Tasks 2 and 3;
  - §5.3 registry: Task 1, and Task 6 for own identifiers;
  - §5.4 counterparty fields, including existing history: Tasks 2 and 6;
  - §5.5 transfers and the invariant: Tasks 2 and 3;
  - §5.6 contra: Task 3;
  - §5.7 matching: Tasks 3 and 4;
  - §5.8 placeholders and the M2 interface: Tasks 3 and 5;
  - §5.9 decisions: Task 5;
  - §5.10 suggestions: Task 7;
  - §5.11 notifications: Task 6;
  - §5.12 API and UI: Tasks 7 and 8;
  - §6 edge cases: tests in Tasks 3-6;
  - §10 verification: the tests named above, plus Task 9 Step 4.
- **Types:**
  - `CounterpartyFields` (Task 2) is used by Tasks 3-7;
  - `TransferRecord` and `match_transaction`/`heal_transfers` (Task 3) by Tasks 4-7;
  - `candidates` (Task 4) by Tasks 5 and 7;
  - `due_notifications`/`mark_notified` (Task 6) by the worker;
  - `unassign_identifier_and_rematch` (Task 5) by Task 7's DELETE.
