# Account Type Catalog, Part B: References, Funding and the Family Rule — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A bank transfer to a payee account shared by several own accounts pairs with the deposit on the account its payment reference names, for all three Trading 212 account types, and a Trading 212 TRANSFER names a counterpart only when the owner has exactly two accounts of that family.

**Architecture:** A `uk_bank_account` identifier registered to no account may carry (account, reference) rows in a new table; one definition of "the account a record's identifier names" reads them and serves pairing, contra choice and import previews. The one-sided registry tier accepts an unmarked deposit when its account's type lists `bank_transfer` funding. Trading 212's sibling inference counts accounts by `type_key` family instead of connections, and a digest in the connection's config replays its normalisation when that set changes.

**Tech Stack:** as Part A. No new dependency.

**Spec:** `docs/superpowers/specs/2026-11-26-account-type-catalog-design.md` (§8 and §9; read §1 to §5 first), with the transfer evidence design `2026-11-21-transfer-evidence-design.md` and the M6 design §5.12.

**Builds on:** `main` with Part A merged (`account_types.py`, `finance.accounts.type_key`). The migration is numbered after the latest on `main` when Task 1 starts (`0039` if Part A's is `0038`).

## Global Constraints

- Docs, code, comments and commit messages in English. Commit titles `<type>: <summary>`; branch `feat/claude-transfer-references-and-families`.
- Commit only the files a task names, by explicit path. Never `git add -A`. No attribution lines.
- No owner data in the repository: no real sort code, account number, reference, amount or date in code, tests or docs.
- No provider's account number or reference in code. The rules are generic; only `account_types.py` says which types they apply to.
- `card_tier` and `weak_tier` do not change. An imported row's `time_unit` is never read in matching.
- Engine invariants are pinned by Hypothesis property tests in the existing style (`tests/finance/transfers/test_properties_evidence.py`, fixtures from `tests/finance/transfers/conftest.py`, `isolated_example`).
- Backend tests: prefix with `DOCKER_HOST=ssh://docker-test`. Run only the focused tests a task names, then `ruff check`, `ruff format --check`, `pyright`, `lint-imports` from `backend/`; CI runs the full suite.
- After any change to a route or schema: `make openapi` and commit `web/openapi.json` and `web/src/api/`.
- Existing pairs must not come apart: every existing transfer test passes unchanged, apart from the three sibling tests Task 4 names.

## Decisions

Choices the spec leaves open, made here:

1. **Storage.** A table of its own, `finance.account_identifier_references`, not a column on the card-merchant holders table (whose `kind` check and meaning are the card merchant's).
2. **Registering references is API-only in this part.** The owner registers three rows once; a form can follow if it recurs. *(To confirm with the owner at hand-off.)*
3. **Replay timing.** The family rule's replay runs at each Trading 212 connection's next sync, triggered by a digest in its config (the Monzo `card_merchants` precedent). No account route runs a normalisation.
4. **The funding arm reads the type at pairing time**, through a boolean column on `eligible_records`; the stored `bank_transfer` flag is untouched.

## Review Focus

1. A payment reference that differs from the registered one only by case or surrounding spaces: it matches. One that differs by an inner space or a character: it does not. (Task 2)
2. Two own accounts registered with the same reference on one identifier: refused at registration, naming the field. (Task 1)
3. A bank transfer whose reference names account X, with same-amount unmarked deposits on X and on Y in the window: it pairs with X's only. With two such deposits on X: it pairs with neither. (Task 3)
4. A record on a bank-transfer-funded account that names a bank account of its own (an IBAN on the row): the funding arm does not apply to it. (Task 3)
5. Adding a Cash ISA after TRANSFERs have been posted naming the other account: after the next sync none names a counterpart, and their transfers reopen or re-pair by amount. Closing the Cash ISA afterwards changes nothing. (Task 4)

---

### Task 1: References on a shared payee account

**Files:**
- Create: `backend/migrations/versions/0039_finance_identifier_references.py`
- Modify: `backend/src/lararium/modules/finance/models.py`, `backend/src/lararium/modules/finance/identifiers.py`, `backend/src/lararium/modules/finance/transfers/engine.py`, `backend/src/lararium/modules/finance/transfers/__init__.py`, `backend/src/lararium/modules/finance/transfers_api.py`, `backend/src/lararium/modules/finance/schemas.py`
- Test: `backend/tests/finance/test_identifiers.py` (or where `register_card_merchant` is tested; find with `git grep -l register_card_merchant backend/tests`), `backend/tests/api/test_identifier_references_api.py` (create, modelled on `tests/api/test_card_merchants_api.py`)

**Interfaces:**
- Produces:
  - table `finance.account_identifier_references (identifier_id uuid, kind text NOT NULL DEFAULT 'uk_bank_account' CHECK (kind = 'uk_bank_account'), account_id uuid NOT NULL REFERENCES finance.accounts, reference text NOT NULL CHECK (reference = upper(btrim(reference)) AND reference <> ''), PRIMARY KEY (identifier_id, reference), UNIQUE (identifier_id, account_id), FOREIGN KEY (identifier_id, kind) REFERENCES finance.account_identifiers (id, kind))`;
  - `identifiers.normalize_reference(text: str) -> str` (strip, then upper-case);
  - `IdentifierRecord.references: tuple[tuple[UUID, str], ...] = ()` (account id, reference; sorted by reference);
  - `async def register_references(ctx: CommandContext, value: str, references: Sequence[tuple[UUID, str]]) -> IdentifierRecord` (`value` is a `uk_bank_account` value; replaces the identifier's whole set; an empty sequence removes them all);
  - `engine.register_references_and_rematch(ctx, value, references, *, heal: bool = True) -> IdentifierRecord`, exported from `transfers/__init__.py`;
  - `POST /api/v1/finance/accounts/identifiers/references` (name `register_identifier_references`), body `IdentifierReferencesRegister{value: str, references: list[IdentifierReference]}`, `IdentifierReference{account_id: UUID, reference: str}`; `IdentifierOut.references: list[IdentifierReference]`.

- [ ] **Step 1: Write the failing tests.**
  - Unit: registering two (account, reference) pairs creates the identifier if absent and returns them; registering again replaces the set; an empty list removes them and leaves the identifier unregistered; `normalize_reference(" ab12 ") == "AB12"` and a stored reference is the normalised one.
  - Refusals (each a `FinanceValidationError` whose message names the field, no value): the same reference twice in one call; the same account twice; an empty or blank reference; an account that is a system account or not the owner's; an identifier that already has `account_id` set; and, the other way, `assign_identifier` and `ensure_identifier` on an identifier that carries references; `dismiss_identifier` on one that carries references.
  - API: the route returns 200 with `references` listed; the refusals are 422; it heals once (model `test_register_and_remove_heal_once`).

- [ ] **Step 2: Run** the two test files. Expected: FAIL.

- [ ] **Step 3: Implement.** Follow `register_card_merchant` for the shape: get or create the identifier, lock its row `with_for_update`, check `account_id IS NULL`, replace the rows, publish `finance.account_identifier.references_changed`. `_assign`'s conditional update gains `AND NOT EXISTS (… references …)`, so the refusal holds under concurrency; `_load` loads references for `uk_bank_account` rows as it loads holders for card merchants. `register_references_and_rematch` mirrors `register_card_merchant_and_rematch`. The route uses `healing_command` and passes `heal=False`, as the card merchant route does.

- [ ] **Step 4: Run** the tests, `pyright`; `make openapi`. Expected: PASS.

- [ ] **Step 5: Commit** `feat: register payment references on a shared payee bank account`.

---

### Task 2: A matching reference names its account

**Files:**
- Modify: `backend/src/lararium/modules/finance/identifiers.py`, `backend/src/lararium/modules/finance/transfers/contra.py`, `backend/src/lararium/modules/finance/transfers/engine.py`, `backend/src/lararium/modules/finance/transfers/queries.py`
- Test: `backend/tests/finance/transfers/test_references.py` (create), `backend/tests/finance/transfers/test_properties_evidence.py`

**Interfaces:**
- Consumes: Task 1's table and `normalize_reference`.
- Produces:
  - `async def named_account(session: AsyncSession, identifier_id: UUID, payment_reference: str | None) -> UUID | None` in `identifiers.py`: the identifier's `account_id`, or, when that is NULL, the account of its reference equal to `normalize_reference(payment_reference)`; `identifier_account` stays for callers with no record in hand;
  - `contra.named_account_sql(t, i) -> ColumnElement[UUID | None]`: the same definition as a SQL expression over a transaction row and its identifier.

- [ ] **Step 1: Write the failing tests** in `test_references.py`, with the conftest's `connected_bank`, `imported_bank`, `connector_tx`, `statement_row` and `heal` fixtures. A shared payee identifier `P` (`uk_bank_account`, registered to no account) carries `(A, "REF-A")` and `(B, "REF-B")`; `A` and `B` are imported accounts; `M` is a connected bank account.
  - A bank transfer out of `M` naming `P` with reference `" ref-a "` and a same-amount bank-transfer row into `A` in the window pair (`ONE_SIDED`), and the outgoing record's contra is In transit from the moment it is posted (not only after a heal).
  - With reference `"REF-B"`, it does not pair with `A`'s row; it pairs with `B`'s when there is one.
  - With no reference, `"REF-C"`, or `"REF A"`: it pairs with nothing, its contra is Unassigned, and the weak tier does not pair it (`names_account` is true).
  - `engine.preview_match` for an import row naming `P` with `"REF-A"` gives the same answer as the posted record gets.
  - `queries.suggestions` does not list `P` while it carries references, and lists it again once they are removed.
  - Changing `P`'s references from `(A, "REF-A")` to `(B, "REF-A")` moves an existing pair's holder: after the rematch the transfer is with `B`'s row or open.

  In `test_properties_evidence.py`, add a property in the file's style: for a generated scenario plus a generated set of references and records naming the shared payee, matching is the same as if each record named an identifier registered directly to the account its reference names, and a record whose reference matches none is never paired by a registry or weak tier.

- [ ] **Step 2: Run** them. Expected: FAIL.

- [ ] **Step 3: Implement.**
  - `eligible_records`: the first arm of the `holder` CASE (`contra.py:322-325`) selects `named_account_sql` in place of `i.account_id`. `names_account` is unchanged.
  - `transfer_contra` (`contra.py:680-683`) and `engine.preview_match` (`engine.py:852`) call `named_account` with the record's payment reference.
  - `rematch_identifiers` already re-runs matching for every transaction naming the identifier; nothing to add.
  - `queries.suggestions` excludes identifiers that have reference rows.

- [ ] **Step 4: Run** the new tests and the existing `tests/finance/transfers/` evidence and card-merchant tests named in the recon (`test_card_merchants.py`, `test_card_deposits.py`, `test_properties_evidence.py`). Expected: PASS.

- [ ] **Step 5: Commit** `feat: a payment reference names the own account behind a shared payee`.

---

### Task 3: An unmarked deposit on a bank-transfer-funded account

**Files:**
- Modify: `backend/src/lararium/modules/finance/transfers/contra.py` (`eligible_records`, `_OWN_RECORDS`), `backend/src/lararium/modules/finance/transfers/pairs.py`, `backend/src/lararium/modules/finance/transfers/engine.py`
- Test: `backend/tests/finance/transfers/test_funding.py` (create), `backend/tests/finance/transfers/test_properties_evidence.py`

**Interfaces:**
- Consumes: `account_types.keys_funded_by("bank_transfer")`; `finance.accounts.type_key`.
- Produces:
  - `eligible_records` column `funded_by_transfer: bool` (the record's account's `type_key` is one of `keys_funded_by("bank_transfer")`), mirrored in `_OWN_RECORDS`;
  - `Pairable.funded_by_transfer: bool = False`;
  - `pairs._may_be_transfer(x: Pairable) -> bool`: `x.bank_transfer or (x.funded_by_transfer and x.holder is None and not x.holders and not x.names_account)`.

- [ ] **Step 1: Write the failing tests** in `test_funding.py`.
  - Pure, on hand-built `Pairable`s (helper as `test_card_merchants.py:375-384`): with `r` identified and naming `c`'s account, `registry_tier(r, c)` is `ONE_SIDED` when `c` is unmarked, names nobody and `funded_by_transfer`; `None` when `funded_by_transfer` is false; `None` when `c.names_account`; `None` when `c.holders`; `None` when neither side is identified. `weak_tier` on two unmarked funded records is `None`. `card_tier` results on the existing cases are unchanged.
  - Database: a Trading 212 `invest` account (`type_key` `trading212.invest`) with a connector DEPOSIT (not a bank transfer, naming nobody), and a connected bank's outgoing bank transfer naming a shared payee whose reference names that account (Task 2): they pair, though the Trading 212 account is no card merchant's holder. The same with the account's `type_key` set to NULL: they do not. Review Focus 3 and 4 as written above.
  - Property (in `test_properties_evidence.py`): for a generated scenario, marking a set of accounts as funded by bank transfer never changes any pair that involves no unmarked record on those accounts.

- [ ] **Step 2: Run** them. Expected: FAIL.

- [ ] **Step 3: Implement.**
  - `eligible_records`: add `type_key` to the `own_account` lateral (`contra.py:312-315`) and select `funded_by_transfer` as `type_key = ANY(:keys)`, the keys bound from `keys_funded_by("bank_transfer")`. Add the column to `_OWN_RECORDS`.
  - `registry_tier` line 106: `if not x.identified and not _may_be_transfer(x): return None`.
  - `_load_records`' `can_pair` (`pairs.py:510-517`) gains an arm: the record is `funded_by_transfer`. Build `Pairable.funded_by_transfer` there and in `engine.preview_match`.
  - `transfers/` importing `account_types` must pass `lint-imports` and create no cycle; if it cannot, pass the keys in from the caller that already imports both.

- [ ] **Step 4: Run** the new tests and all of `tests/finance/transfers/`. Expected: PASS, with no existing test changed.

- [ ] **Step 5: Commit** `feat: pair an identified bank transfer with an unmarked deposit on a transfer-funded account`.

---

### Task 4: The family rule

**Files:**
- Modify: `backend/src/lararium/modules/finance/accounts.py`, `backend/src/lararium/modules/finance/trading212/accounts.py`, `backend/src/lararium/modules/finance/trading212/normalize.py`, `backend/src/lararium/modules/finance/trading212/records.py`
- Test: `backend/tests/finance/trading212/test_normalize.py`, `backend/tests/finance/trading212/test_properties.py`

**Interfaces:**
- Consumes: `account_types.family_keys("trading212")`; `finance.accounts.type_key`.
- Produces:
  - `async def accounts_of_types(session: AsyncSession, keys: Collection[str]) -> list[UUID]` in `accounts.py`: ids of the owner's accounts whose `type_key` is in `keys`, whatever their status, sorted;
  - `trading212.accounts.sibling(run) -> UUID | None`, same signature, new rule;
  - `NORMALIZER_VERSION = "trading212-norm-2"`; config key `FAMILY_KEY = "family_accounts"` holding a digest of the family's account ids.

- [ ] **Step 1: Rewrite and add the tests.**
  - `test_a_transfer_names_the_sibling_account` (`:917`) and `test_a_transfer_without_a_sibling_names_nothing` (`:965`) keep their assertions; their setup now relies on the two accounts' `type_key`s (set by Part A's connector code).
  - `test_no_sibling_unless_one_invest_and_one_isa` (`:1281`) becomes `test_a_sibling_only_with_exactly_two_family_accounts`, parametrised: Invest + Stocks ISA → each names the other; Invest alone → none; Invest + Stocks ISA + a manual account with `type_key` `trading212.cash_isa` → none; the same with the Cash ISA closed → none; Invest + a Cash ISA only (two accounts, one with no `trading212_account` identifier) → none.
  - New, Review Focus 5: post a TRANSFER with Invest + Stocks ISA (it names the sibling); add a Cash ISA account; normalise again with no new records: the stored digest differs, the run replays, and the TRANSFER's `counterparty_identifier_id` is NULL and its contra is Unassigned. Normalise a third time: no replay.
  - `test_properties.py`: the existing replay property holds with the version constant's new value; update its forced-replay value only if it hard-codes `norm-1`.

- [ ] **Step 2: Run** `DOCKER_HOST=ssh://docker-test uv run --locked pytest -q tests/finance/trading212/test_normalize.py -k "sibling or transfer or family"`. Expected: FAIL.

- [ ] **Step 3: Implement.**
  - `sibling(run)`: `ids = await accounts_of_types(run.session, family_keys("trading212"))`; unless `len(ids) == 2` and `run.account_id in ids`, return None; the other account's `trading212_account` identifier (query `account_identifiers` by `account_id` and `kind`) is the sibling; none registered → None. Keep the `run.sibling` cache. Delete the connection counting.
  - `normalize`: compute the digest of the family's account ids (as Monzo's `_digest`, `monzo/normalize.py:1328-1330`), and replay when the version or the digest differs from the stored one (`normalize.py:173`); store both where the version is stored (`:241-246`).
  - A replay must strip a counterpart as well as add one: check that `_post_movement` (`records.py:311-332`) re-posts a stored TRANSFER with `identifier=None` during a replay and that the ledger's change check updates the record; `unidentified_transfers` (`records.py:398-420`) keeps adding counterparts between replays. If a replayed TRANSFER keeps its old counterpart, fix it in `_post_movement`, not by a special case elsewhere.

- [ ] **Step 4: Run** all of `tests/finance/trading212/` and `tests/api/test_trading212_api.py`. Expected: PASS.

- [ ] **Step 5: Commit** `feat: name a Trading 212 transfer's counterpart only between exactly two family accounts`.

---

### Task 5: Wrap-up

- [ ] **Step 1: README** (and `README.zh-CN.md`): how to register references for a shared payee account (the route, with placeholder values); that bank top-ups to Trading 212 Invest, Stocks ISA and Cash ISA then pair on their own while card top-ups still pair by the card rule; the family rule and its limitation (Lararium counts only the accounts added to it); that a newly added family account takes effect at each connection's next sync.
- [ ] **Step 2:** From `backend/`: `ruff check`, `ruff format --check`, `pyright`, `lint-imports`; `make openapi-check`; from `web/`: `pnpm typecheck && pnpm test --run`. Expected: clean.
- [ ] **Step 3:** Set the spec's Status to "implemented" and commit with this plan: `docs: add the account type catalog Part B plan`.
- [ ] **Step 4:** Push and open the PR against `main`; CI runs the full suites. The PR description lists the spec's §13 step 3, and that after deploy each Trading 212 connection replays once (version bump) at its next sync.
