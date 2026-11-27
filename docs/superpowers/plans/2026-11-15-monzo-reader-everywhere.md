# Read Every Monzo Field Through the Reader

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Monzo normalizer reads every field of Monzo's objects through `_Reader`, the reader the structured-fields plan introduced (`2026-11-15-structured-monzo-fields.md`, Decision 10), so no field Monzo states in an unexpected shape fails a sync, is stored as its Python repr, or is silently taken for another value.

**Architecture:** `_Reader` (in `backend/src/lararium/modules/finance/monzo/normalize.py`) becomes the only code that looks inside Monzo's JSON objects. It gains a time reader, the shapes the older code checked by hand (amounts by name, a list of texts, a field with no known shape), and required reads for the core fields. Every other read in the normalizer goes through it.

**Tech Stack:** Python 3.13, async SQLAlchemy, PostgreSQL 18, pytest.

**Base:** branch `feat/claude-structured-monzo-fields` (PR #11), not merged yet.

## Context

PR #11 reads its new typed fields through `_Reader`. The fields the normalizer read before it still trust Monzo's format:

| Field | Today, in a shape Monzo does not send |
|---|---|
| `settled` | a malformed time raises and fails the sync; `false` or `0` passes for pending |
| `metadata.pot_id` | a non-string, or a non-object `metadata`, posts a pot move against Unassigned silently |
| `merchant.name`, `counterparty.name`, `description` (the description, here and in `finance.declined_transactions`) and `notes` (the posting memo) | a non-string is stored as its Python repr |
| an account's `description` and `type` (its name) | a list or an object raises `TypeError` |
| a pot's `name` | a non-string is stored as its repr |
| a pot's `deleted` | any truthy value, such as `"false"`, closes the pot |
| a pot's `created` | an unreadable time raises |
| a pot's `balance` and `currency` | a non-integer is taken as an amount (`true` is 0.01), an unknown currency raises |
| `amount`, `currency`, `created`, `id` | `true` posts 0.01; a float posts a fraction of a minor unit; a time without an offset is read in the server's time zone |
| the balance's `balance` and `currency` | as for `amount` |
| `fees`, `categories`, `labels`, `atm_fees_detailed`, `isa_wrapper` | read by hand, so `""` warns instead of reading as absent |

## Decisions

1. **One reader.** Only `_Reader` looks inside a Monzo object. Absent, null and `""` read as None, silently. A value of another type reads as None and warns with its source path (`settled`, `metadata.pot_id`, `counterparty.name`, `pot.deleted`, …), as Decision 10 of the structured-fields plan says. What None then means is each field's own rule, below.
2. **Times.** A stated time is read only by `_readable_time` (the `_ISO_TIME` rule the replay's SQL uses too). A time it cannot read is None with a warning, never an error.
3. **What None means for each older field:**
   - `settled`: the transaction is pending, so it is fetched again until it reads as settled.
   - `metadata.pot_id`: no pot, so the move posts against Unassigned, as for a pot deleted before the connection existed. Now with a warning when it was stated.
   - The description: the next of merchant name, counterparty name and `description`, else `""`. The memo: `""`.
   - An account's name: the next of `description` and `type`, else "Monzo". A pot's name: "Pot".
   - A pot's `deleted`: not deleted. Closing a pot cannot be undone by a later sync, so an unreadable flag never closes one.
   - A pot's `created`: the account's `opened_on` is left empty.
   - A pot's `balance`, or a `currency` it cannot read or has no instrument for: no snapshot this sync, and the goal amount keeps its stored value. A balance the ledger cannot store warns the same way.
   - `updated`: the statement falls back to `created`, as when it is absent.
   - A deleted pot's `updated`: it closes, and its balance holds, from the sync's fetch time instead (the deleted-pot rule of PR #27).
4. **Core fields fail loudly** (`MonzoFieldError`, which names the path, never the value). These are the fields without which the sync cannot post what it must:
   - a transaction's `id` and `amount`, and its `currency` and `created` where it is posted or recorded as declined (a reversed one, amount 0, needs neither);
   - an account's `id` and `created`: the sync's full backfill starts from `created` (`sync.py`), and an account with nothing else posted dates its opening by it;
   - a pot's `id`: the account map key, which a pot move posts against;
   - the balance's `balance` and `currency`: the account's snapshot, its opening and its reconciliation.

   All of them fail today too, by `KeyError`, `ValueError` or a database error; the reader makes the failure the same everywhere and closes the shapes that passed silently (`true`, a float, a time without an offset). This keeps the owner's rule as given, so it needs no new approval. `MonzoFieldError` is a `DisplayableError`, so the worker shows its message as the sync's `last_error`: it names only the path.
5. **Not in this plan:** `sync.py` reads an account's `id`, `created` and `closed`, and each transaction's `created`, to page and to register webhooks. They are core except `closed`, and they fail loudly already; `closed` only decides which accounts get a webhook. A follow-up can move them onto the reader.

## Tasks

### Task 1: Tests for each field's wrong shape

**Files:** `backend/tests/finance/monzo/test_normalize.py`, `backend/tests/worker/test_runner.py`

- [ ] **Step 1: Write the failing tests:**
  - `test_older_transaction_fields_warn_and_read_as_unknown`, parametrized over `settled` (`false`, `0`, `"not a time"`, a time without an offset), `metadata` (a string), `metadata.pot_id` (a number), `merchant.name`, `counterparty`, `counterparty.name`, `description` and `notes` (a number), `updated` (a number): the transaction posts, the field reads as None (pending, Unassigned, the next description, an empty memo), and the one warning is the field's path.
  - `test_a_declined_description_reads_through_the_reader`: a declined transaction whose merchant name is a number stores the counterparty's name, with the `merchant.name` warning.
  - `test_older_pot_fields_warn_and_read_as_unknown`, parametrized over `name`, `deleted` (`"false"`), `created`, `balance` (`true`, `"5000"`), `currency` (a number, `XTS`): no sync fails, and the field's rule from Decision 3 holds.
  - `test_older_account_fields_warn_and_read_as_unknown`, over `description` and `type` (a list).
  - `test_core_fields_fail_loudly`, parametrized over each core field of Decision 4, missing and in a wrong shape: `normalize` raises `MonzoFieldError` naming the path.
  - `test_empty_shapes_read_as_absent`: `fees`, `categories`, `labels`, `atm_fees_detailed` set to `""` post with no warning.
  - In `test_runner.py`, `test_last_error_names_the_monzo_field`.
- [ ] **Step 2: Run** `cd backend && uv run pytest tests/finance/monzo/test_normalize.py tests/worker/test_runner.py -q`. Expected: the new tests FAIL.

### Task 2: Route every read through the reader

**Files:** `backend/src/lararium/modules/finance/monzo/normalize.py`

- [ ] **Step 1:** Give `_Reader` `time`, `amounts`, `texts`, `warn_if_stated`, `states`, `text(keep_empty=True)` and the required reads `required_text`, `required_minor_units` and `required_time`, which raise `MonzoFieldError`.
- [ ] **Step 2:** Read every field in `account`, `pot`, `transaction`, `declined`, `_postings`, `_details`, `_category_splits`, `_description`, `_statement_time`, `opening` and `_normalize` through it. An account's or pot's unknown source fields are the reader's unreadable ones that are `SourceDetails` fields.
- [ ] **Step 3:** Make `MonzoFieldError` a `DisplayableError`, so `jobs._describe` shows its message.
- [ ] **Step 4: Run** the tests of Task 1, then `make check`. Expected: PASS.
- [ ] **Step 5: Commit** `fix: read every Monzo field through the reader, and fail clearly on a core one`.
