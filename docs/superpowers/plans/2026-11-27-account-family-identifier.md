# Account family identifier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A move between a provider's own accounts that doesn't name the other account names its family's identifier, pairs only within the family, and is never taken as an external top-up.

**Architecture:** A new identifier kind `account_family` (value: a catalog family key). Its holders are never stored: wherever a record's identifier is resolved (`contra.eligible_records`, `transfer_contra`), a family identifier resolves to the family's other accounts read from `type_key`, one → that account, several → a set. `pairs.registry_tier` gets one family rule (counterpart names the family back, or names nobody within `near_window`, both timed), family evidence outranks shared-payee and funding evidence, and `_twins` pairs a group when one side is alike. The Trading 212 connector names the family on every TRANSFER and loses its sibling/digest/replay code.

**Tech Stack:** Python 3.13, SQLAlchemy async, PostgreSQL 18, raw-SQL Alembic, pytest + Hypothesis, testcontainers on the remote Docker host.

**Spec:** `docs/superpowers/specs/2026-11-27-account-family-identifier-design.md` (read it whole before any task; section numbers below are its).

## Global Constraints

- Branch `feat/claude-account-family-identifier`; commit titles `<type>: <summary>`; no attribution lines; commit only your own files by explicit path (never `git add -A`).
- Migration number `0046`, `down_revision = "0045"`; file `backend/migrations/versions/0046_finance_account_family_identifier.py` (renumbered at merge time if main has moved).
- `NORMALIZER_VERSION = "trading212-norm-4"`.
- The family key in code comes only from `account_types.ACCOUNT_TYPES` (`"trading212"` exists there already); no provider data (account ids, bank details, amounts, times from production) in code, tests or commits. Never read anything under `private/`.
- Tests run only on the remote Docker host: `cd backend && DOCKER_HOST=ssh://docker-test uv run --locked pytest <the test you added or changed> -q`. Run only the tests you add or change; CI runs the rest. Before each commit: `cd backend && uv run --locked ruff check . && uv run --locked ruff format --check . && uv run --locked pyright && uv run --locked lint-imports`.
- Docs and comments in English, in the project's existing plain style (module docstrings state the rule and cite the design section).
- `near_window` is `TransferSettings.near_window` (default `timedelta(minutes=2)`, `transfers/settings.py`).

## Review Focus

1. A family move and a record naming nobody exactly `near_window` apart: inclusive (`<=`), as `card_tier` compares. Pinned in Task 3.
2. A record naming the family on an account no longer in the family (retyped): its own account is still excluded from its holders, and it pairs only with an account still in the family. Pinned in Task 2.
3. An untimed record (an imported row with `time_unit` "day") within the same booking day as a family move: never its counterpart. Pinned in Task 3.
4. Membership change by the account create route and by `PATCH /accounts/{id}` with `type_key`: the ledger agrees with the rule right after the call (no stale In transit contra). Pinned in Task 6.
5. A one-side-alike group where one record of the other side also has a candidate outside the group: pairs none. Pinned in Task 4.

---

### Task 1: The `account_family` identifier kind and its resolution

**Files:**
- Modify: `backend/src/lararium/modules/finance/account_types.py` (add `families()`, `family_of()`; import-time check)
- Modify: `backend/src/lararium/modules/finance/identifiers.py` (kind, normalize, refusals, `named_accounts`)
- Create: `backend/migrations/versions/0046_finance_account_family_identifier.py`
- Modify: `backend/src/lararium/modules/finance/transfers/contra.py` (`transfer_contra` uses `named_accounts`)
- Test: `backend/tests/finance/test_account_family_identifier.py` (new), `backend/tests/finance/test_account_family_identifier_migration.py` (new, in the style of `test_shared_payee_holders_migration.py`)

**Interfaces:**
- Produces:
  - `account_types.families() -> frozenset[str]`: every `family` some type has.
  - `account_types.family_of(type_key: str | None) -> str | None`.
  - `identifiers.IdentifierKind` gains `"account_family"`; `normalize_identifier("account_family", value)` returns `value.strip()` when it is in `families()`, else raises `FinanceValidationError("an account family is one of: <sorted, comma-separated>")`.
  - `async def identifiers.named_accounts(session: AsyncSession, identifier_id: UUID, own_account_id: UUID) -> tuple[UUID | None, frozenset[UUID]]`: (the one account it names, the set it is identified to). For an `account_family` row: the family's accounts other than `own_account_id` (by `type_key in family_keys(value)`, any status); one → `(that, frozenset())`; else `(None, others)`. For any other row: `(identifier_account(...), frozenset())`. Its docstring says `contra.eligible_records` holds its SQL twin (Task 2).
  - `async def identifiers.family_identifier(ctx: CommandContext, family: str) -> UUID`: `get_or_create_identifier(ctx, "account_family", family)`.

- [ ] **Step 1: Write the failing tests** in `test_account_family_identifier.py`:
  - `test_normalize_accepts_only_a_catalog_family`: `normalize_identifier("account_family", " trading212 ") == "trading212"`; `"monzo"` and `""` raise `FinanceValidationError`.
  - `test_every_catalog_family_is_an_identifier_value`: for each `f in families()`, `normalize_identifier("account_family", f) == f`.
  - `test_a_family_identifier_takes_no_account_and_no_dismissal`: `assign_identifier`, `ensure_identifier` and `dismiss_identifier` each raise `FinanceValidationError` for a family row. (`HeldKind` stays `card_merchant | uk_bank_account`, so `register_holders` can't be asked; the holders' CHECK refuses one in the database, pinned by the migration test.)
  - `test_named_accounts_of_a_family` (parametrized n = 1, 2, 3 family accounts, one of them closed when n = 3, plus one account of no family type): from the first account, n = 1 → `(None, frozenset())`; n = 2 → `(second, frozenset())`; n = 3 → `(None, {second, third})`; the non-family account never appears; from an account whose type was cleared after it posted, its own id is still excluded.
  - `test_transfer_contra_of_a_family_move`: In transit with exactly one other family account, Unassigned with two or more or none.
- [ ] **Step 2: Write the failing migration test**: upgrade then downgrade on a scratch schema as the 0045 test does; after upgrade an `account_family` row inserts with `account_id NULL`, and with `account_id` set it violates the CHECK; a holder row of kind `account_family` violates the holders' kind CHECK; downgrade clears `counterparty_identifier_id` naming such rows, deletes them and restores the kind CHECK.
- [ ] **Step 3: Run them to see them fail** (`DOCKER_HOST=ssh://docker-test uv run --locked pytest tests/finance/test_account_family_identifier.py tests/finance/test_account_family_identifier_migration.py -q`): FAIL.
- [ ] **Step 4: Implement.** Migration 0046: re-create `account_identifiers_kind_check` with `'account_family'` added to 0035's list (read its exact list); add `CHECK (kind <> 'account_family' OR account_id IS NULL)`; leave the holders' kind CHECK as 0045 set it. Module doc of `identifiers.py` gains a paragraph for the family kind (spec §5.1–5.2). `check_catalog` (or a check beside it run at import) asserts every family normalizes. `transfer_contra` replaces its `identifier_account` call with `named_accounts(session, id, own_account_id)[0]`.
- [ ] **Step 5: Run the tests**: PASS. Lint/pyright as in Global Constraints.
- [ ] **Step 6: Commit** `feat: add an account_family identifier resolved from account types`.

### Task 2: Records naming a family in `eligible_records` and `Pairable`

**Files:**
- Modify: `backend/src/lararium/modules/finance/transfers/contra.py` (`eligible_records`, `_OWN_RECORDS`)
- Modify: `backend/src/lararium/modules/finance/transfers/pairs.py` (`Pairable.family`, `_load_records`)
- Modify: `backend/src/lararium/modules/finance/transfers/heal.py` only if `snapshot_own_records` lists columns by hand
- Test: `backend/tests/finance/transfers/test_family.py` (new; reuse `conftest` fixtures `connected_bank`, `imported_bank`, `connector_tx`, `statement_row`, `heal`, and the `trading212` fixture pattern from `test_payee_holders.py`)

**Interfaces:**
- Consumes: Task 1's `named_accounts` (the SQL must agree with it), `family_identifier`.
- Produces: `eligible_records` columns `holder`, `payee_holders` resolved for a family identifier per spec §5.2, and a new boolean column `family` (it names an `account_family` row); `names_account` true for it (it is an identifier that is no card merchant: already so). `Pairable.family: bool = False`, loaded by `_load_records`; `_load_records`' `can_pair` already admits it (it names an identifier).

- [ ] **Step 1: Write the failing tests** in `test_family.py`:
  - `test_eligible_row_of_a_family_move` (parametrized n = 1, 2, 3 Trading 212-typed accounts; build accounts with `create_account(..., type_key=...)`, the Cash ISA manual): a connector record on the first naming `family_identifier(ctx, "trading212")` has `family` true, `names_account` true, and `(holder, payee_holders)` equal to `named_accounts(...)` (with `payee_holders` NULL where the set is empty).
  - `test_a_retyped_account_still_excludes_itself`: record posted on A (family), then A's `type_key` cleared via `set_type_key`: its row's holders are the other family accounts, never A.
- [ ] **Step 2: Run to fail.**
- [ ] **Step 3: Implement.** In `eligible_records`, resolve a family row's accounts per record: the family's type keys come from the catalog as a literal (for each family in `families()`, its `family_keys`), joined as `select array_agg(a.id) from finance.accounts a where a.type_key = any(<keys of i.value>) and a.id <> legs.account_id` in a lateral (the existing laterals show the pattern and why). `holder` gains a CASE arm before the registry lookup: a family row with exactly one other account → that account; `payee_holders` becomes coalesce of the payee set and the family set when it has two or more. Update both docstrings (`eligible_records`' column list, `Pairable`'s fields) to say what a family record carries and that `identifiers.named_accounts` is the twin.
- [ ] **Step 4: Run to pass.** Lint/pyright.
- [ ] **Step 5: Commit** `feat: resolve a record naming a family to the family's other accounts`.

### Task 3: The family rule in `registry_tier`, and family evidence first

**Files:**
- Modify: `backend/src/lararium/modules/finance/transfers/pairs.py`
- Test: `backend/tests/finance/transfers/test_family.py` (unit cases), `backend/tests/finance/transfers/test_properties_family.py` (new Hypothesis properties, in the style of `test_timed_receipts.py`: in-memory `Pairable` lists through `_registry_edges` and `pair_relation`-free helpers)

**Interfaces:**
- Consumes: `Pairable.family`, `timed`, `holder`, `payee_holders` (Task 2).
- Produces:
  - `registry_tier(r: Pairable, c: Pairable, near_window: timedelta = DEFAULTS.near_window) -> Tier | None` (`DEFAULTS` from `transfers.settings`); `_registry_edges` passes `settings.near_window`.
  - `def _names_nobody(x: Pairable) -> bool`: `x.holder is None and not x.holders and not x.payee_holders and not x.names_account`.
  - `def _family_pair(x: Pairable, other: Pairable, near_window: timedelta) -> bool`: `x.family`; `other.account_id == x.holder or other.account_id in x.payee_holders`; and either `other.family` and `x.account_id` is `other.holder` or in `other.payee_holders`, or `_names_nobody(other) and x.timed and other.timed and abs(x.occurred_at - other.occurred_at) <= near_window`.
  - In `registry_tier`, when `r.family or c.family`: return `MUTUAL` if both are family, both identified and each `holder` is the other's account; `ONE_SIDED` if `_family_pair(r, c) or _family_pair(c, r)`; else `None`. No other rule applies to a family record (it goes before the payee and identified arms; the different-accounts check stays first).
  - `_registry_edges`: a first pass collects `familied: set[UUID]`, the records naming nobody that are some family move's counterpart by `_family_pair` (same instrument, opposite equal amount, in the window). An edge from a non-family pair is dropped when a side `x` in `familied` is `_funded_only(x)` or the other side has `payee_holders` (spec §5.3 item 3). Module docstring and `pair_relation`'s docstring gain the family rule.

- [ ] **Step 1: Write the failing unit tests** in `test_family.py` (pure `Pairable`s via a local `_pairable` like `test_payee_holders.py`'s; accounts I, S, C family, M Monzo; `T0` an instant):
  - `test_two_family_moves_pair_within_the_family`: I −100 family `payee_holders={S,C}` vs S +100 family `payee_holders={I,C}` → `ONE_SIDED` both ways; with `holder` set to each other's account (two-account family) → `MUTUAL`; vs a family move on M (not in the set) → `None`.
  - `test_a_family_move_takes_a_record_naming_nobody_only_within_the_near_window`: I −100 family timed at T0 vs C +100 `bank_transfer=True` timed at T0+2min → `ONE_SIDED`; at T0+2min+1s → `None`; untimed at T0 → `None`; S +100 `funded_by_transfer=True` (a DEPOSIT) timed at T0+30s → `ONE_SIDED`.
  - `test_a_family_move_is_never_a_payee_or_card_record_s`: a Monzo −100 naming a payee held by {I,S,C} vs S +100 family → `None`; `card_tier` of a Monzo card payment whose merchant holders include S vs S +100 family (connector source) → `None`.
- [ ] **Step 2: Write the failing properties** in `test_properties_family.py` (`@settings(max_examples=300, deadline=None)`; draw 2–5 records over accounts {I, S, C, M} with amounts from {100, 200}, instants within ±10 minutes and ±2 days of a base, `family` / `bank_transfer` / `funded_by_transfer` / `timed` / payee naming drawn, `holder`/`payee_holders` set consistently with §5.2 for a 3-account family):
  - `test_a_family_record_pairs_only_inside_its_family`: every same-amount edge `_registry_edges` gives that touches a family record has both records on family accounts, and the non-family side (if any) names nobody, is timed and within `near_window` of it.
  - `test_family_evidence_comes_first`: removing every family record from the input never removes a non-family edge that touches no `familied` record, and every non-family edge it adds back touches a record in `familied` (computed on the full input).
  - `test_time_only_narrows_family_pairs`: with every record's `timed` off, the family edges are exactly those between two family records.
- [ ] **Step 3: Run to fail.**
- [ ] **Step 4: Implement** per Interfaces.
- [ ] **Step 5: Run to pass**, plus the existing `tests/finance/transfers/test_payee_holders.py` and `test_funding.py` (they call `registry_tier` directly; they must pass unchanged). Lint/pyright.
- [ ] **Step 6: Commit** `feat: pair a family move only within its family, by the near window for unnamed sides`.

### Task 4: Interchangeable on one side, and links by time (§5.7)

**Files:**
- Modify: `backend/src/lararium/modules/finance/transfers/pairs.py` (`_twins`, its call in `pair_relation`)
- Test: `backend/tests/finance/transfers/test_properties_family.py` (properties), `backend/tests/finance/transfers/test_family.py` (unit)

**Interfaces:**
- Produces: `_twins(records, exact, untakeable, near_window: timedelta) -> dict[UUID, UUID]`. A group qualifies when today's conditions hold except that `alike(found) and alike(others)` becomes `alike(found) or alike(others)`. Links: when every record of the group is `timed` and each has exactly one record of the other side within `near_window` of its `occurred_at`, those; else `zip(order(others), order(found))` as today. Docstring states the owner's decision (2026-11-27) and why any assignment gives the same ledger.

- [ ] **Step 1: Write the failing unit test** `test_one_side_alike_pairs_in_order`: senders I −100 at T0 and S −100 at T0+17s (both family, set {other, C}), receivers C +100 rows at T0 and T0+17s (bank transfers, timed, identical account/instrument/amount/booking date): `pair_relation`-free call of `_twins` on the `exact` map `_registry_edges` + the tier filter give → I↔first row, S↔second; with the rows untimed → the fixed order (by time then reference on each side); with a third C +100 row in the window → no twins.
- [ ] **Step 2: Write the failing property** `test_one_side_alike_any_assignment_gives_the_same_balances`: draw n in 2..3, n receivers alike on one account, n senders on distinct accounts, all mutually candidates; for every permutation the per-(account, instrument) sums of linked quantities are equal; `_twins` returns a bijection between the two sides equal to the time-order zip; adding one record with a candidate outside the group, or marking one pair untakeable, yields no twins.
- [ ] **Step 3: Run to fail.** **Step 4: Implement.** **Step 5: Run to pass**, plus the existing twin tests by name: `tests/finance/transfers/test_connected.py` (the test whose docstring cites `pairs._twins`, line ~1222) and `test_not_listed.py` (the one at ~709). Lint/pyright.
- [ ] **Step 6: Commit** `feat: pair interchangeable records when one side is alike, linking by time`.

### Task 5: Trading 212 TRANSFERs name the family

**Files:**
- Modify: `backend/src/lararium/modules/finance/trading212/accounts.py` (remove `sibling`, `family`; add `family_identifier(run) -> UUID`; module doc)
- Modify: `backend/src/lararium/modules/finance/trading212/records.py` (`_post_movement`; remove `unidentified_transfers`; module doc)
- Modify: `backend/src/lararium/modules/finance/trading212/normalize.py` (`NORMALIZER_VERSION`, remove `FAMILY_KEY`, `_digest`, the family part of `replay`, the unidentified-transfers part of `_movements`; module doc)
- Modify: `backend/src/lararium/modules/finance/trading212/state.py` (remove `Run.sibling`)
- Test: `backend/tests/finance/trading212/test_normalize.py` (replace `test_a_transfer_names_the_sibling_account`, `test_a_transfer_without_a_sibling_names_nothing`, `test_a_sibling_only_with_exactly_two_family_accounts`, `test_a_new_family_account_strips_the_counterpart_at_the_next_sync`, `test_a_family_account_added_during_a_sync_is_replayed_at_the_next` and their helpers)

**Interfaces:**
- Consumes: `identifiers.family_identifier` / `get_or_create_identifier(cmd, "account_family", FAMILY)` through `run.shared(("identifier", "account_family", FAMILY), ...)`, as `accounts.account` does for `trading212_account`.
- Produces: every TRANSFER posted with `CounterpartyFields(identifier_id=<family row>, never_transfer=False)`; DEPOSIT/WITHDRAW unchanged.

- [ ] **Step 1: Replace the tests**:
  - `test_a_transfer_names_the_family`: with one connection, a TRANSFER names the `account_family` row whose value is `"trading212"`; a DEPOSIT names nothing; the `TRANSFER` flag is still raised.
  - `test_transfers_between_two_accounts_pair_mutually`: the body of the old sibling test, asserting both TRANSFERs name the family row and the transfer is `matched` with In transit at 0.
  - `test_a_third_family_account_needs_no_replay`: Invest and ISA synced with a TRANSFER pair; a manual Cash ISA created with `type_key="trading212.cash_isa"`; the next normalize reports `replayed` false; after a heal, the TRANSFERs' eligible rows carry the two-account set (`holder` None) and the ledger is consistent (`assert_consistent` from transfers' conftest, or the In transit sum check the old test used).
  - `test_the_version_bump_replays_once`: a connection stored with `normalizer_version` `"trading212-norm-3"` replays and re-posts its TRANSFER naming the family; the next run does not replay.
- [ ] **Step 2: Run to fail.** **Step 3: Implement** (delete the dead code; a stale `family_accounts` key in an existing connection's config is ignored; say so in `normalize.py`'s doc). **Step 4: Run to pass**, only the tests above. Lint/pyright (dead imports).
- [ ] **Step 5: Commit** `feat: name the Trading 212 family on every TRANSFER and drop the sibling replay`.

### Task 6: Membership changes heal; the real shape end to end; docs

**Files:**
- Modify: `backend/src/lararium/modules/finance/api.py` (`create_account`, `rename_account`)
- Test: `backend/tests/api/test_accounts_family.py` (new) or the existing accounts API test module if one covers `type_key` (grep `rename_account` in `backend/tests/api/`)
- Test: `backend/tests/finance/transfers/test_family.py` (integration of the 2026-11-26 shape)
- Modify: `docs/superpowers/specs/2026-11-26-account-type-catalog-design.md` (§8.1 last limitation → closed, pointer; §9 → replaced, pointer and one-line restatement; §11 family test bullet), `README.md` and `README.zh-CN.md` (Trading 212 section: TRANSFERs name the family, pair within it; the known limitations of spec §8), module docs not yet touched.

**Interfaces:**
- Consumes: everything above. `transfers_api.healing_command` (lock first, heal at the end); if lint-imports forbids `api.py` importing it, take `transfers.lock_transfers(ctx)` first and `transfers.heal_transfers(ctx)` last inside the existing `command_context`.

- [ ] **Step 1: Write the failing API test** `test_adding_a_family_account_heals_at_once`: Invest and Stocks ISA (connected, typed) with a TRANSFER pair matched MUTUAL; `POST /api/v1/finance/accounts` with `type_key: "trading212.cash_isa"` → right after, a heal returns 0 and the TRANSFERs' contras agree with the rule (In transit while still a matched pair, no stray); the same through `PATCH /accounts/{id}` setting `type_key` on an existing manual account and clearing it again.
- [ ] **Step 2: Write the failing integration test** `test_two_equal_moves_into_the_cash_isa_pair_by_time`: Invest and Stocks ISA connected, Cash ISA manual listed from its opening; TRANSFER −100 on Stocks at T0 and on Invest at T0+18s (family); two Cash ISA statement rows +100 `bank_transfer` timed to the second at T0 and T0+18s; plus a Monzo payment −100 naming a payee held by all three, two days earlier, with its own +100 bank-transfer Cash ISA row then. After heal: three matched transfers, Stocks→first row, Invest→second row, Monzo→its own row; `assert_consistent`.
- [ ] **Step 3: Run to fail.** **Step 4: Implement** the healing in both routes when the type set or cleared (old or new) has a family (`account_types.family_of`). **Step 5: Run to pass.** Lint/pyright.
- [ ] **Step 6: Update the docs** listed above (README.zh-CN.md mirrors README.md).
- [ ] **Step 7: Commit** `feat: heal when an account joins or leaves a family` and `docs: describe the family identifier in the catalog design and README` (two commits, explicit paths).

## Execution notes

- Models: Tasks 1, 2, 4, 5, 6 standard; Task 3 most capable (the rule and its properties); final whole-branch review most capable.
- After #48 merges: merge main, take the next free migration number, push, open the PR; CI runs the full suite.
