# Account Type Catalog, Part A: Catalog, Add Account Page, Trading 212 Cash ISA — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The owner adds any account by picking its type from one list, an account remembers its type, and "Trading 212 Cash ISA" is a type whose History CSV imports through a ready profile.

**Architecture:** A new module `finance/account_types.py` holds one tuple of `AccountType` entries (shape, `api`, `files`, `funding`, `family`). Statement profiles stay defined in `importing/catalog.py`; a statement type reads its files from there by name. `finance.accounts` gains `type_key`. The web's Add account page renders `GET /finance/account-types` in place of its two hard-coded groups. No engine, upload route or pairing rule changes.

**Tech Stack:** FastAPI, SQLAlchemy async, PostgreSQL 18, raw-SQL Alembic, React/Vite/TanStack Query, hey-api client, Vitest. No new dependency.

**Spec:** `docs/superpowers/specs/2026-11-26-account-type-catalog-design.md` (§6 and §7; read §1 to §5 first). Part B (`2026-11-26-account-type-catalog-part-b.md`) covers §8 and §9 and starts after this merges.

**Builds on:** `main` at `75c78ff` or later. The migration is numbered after the latest on `main` when Task 3 starts (0037 on 2026-11-26, so `0038`).

## Global Constraints

- Docs, code, comments and commit messages in English. Commit titles `<type>: <summary>`; branch `feat/claude-account-type-catalog`.
- Commit only the files a task names, by explicit path. Never `git add -A`. No attribution lines.
- No owner data in the repository: test files use the real header `Action,Time (UTC),Notes,ID,Total,Currency (Total)` with synthetic values. Never read or copy anything under `private/` except through `make real-check`.
- Every source column is stored. Messages name a column or a rule, never a value.
- A field or an entry goes into the catalog only if a rule or a page reads it.
- Backend tests need Postgres through testcontainers on the remote host: prefix with `DOCKER_HOST=ssh://docker-test`. Run only the focused tests a task names, then `uv run --locked ruff check . && uv run --locked ruff format --check . && uv run --locked pyright && uv run --locked lint-imports` from `backend/`; CI runs the full suite.
- After any change to a route or schema: `make openapi` and commit `web/openapi.json` and `web/src/api/`.
- Existing accounts must post exactly what they post today.

## Review Focus

1. A `type_key` naming a type that has an API (`trading212.invest`) sent to the account create route: refused with 422, no account created. (Task 4)
2. A `type_key` set by `PATCH` on an account a connection created, or on a `snapshots` account: refused with 422. (Task 4)
3. An Invest export (the same required columns, plus trade columns and trade `Action`s) uploaded to a Cash ISA account: nothing posts without the owner's review, and each staged row shows its `Action`, so a trade is recognisable before commit. (Task 1)
4. An account whose stored `type_key` is no longer in the catalog: every page and route treats it as an account with no type. (Tasks 4, 6)
5. A Cash ISA row whose `Currency (Total)` is not three letters, or whose `Time (UTC)` lacks the offset: a row error naming the column, as other profiles give. (Task 1)

---

### Task 1: The Cash ISA's statement profile

**Files:**
- Modify: `backend/src/lararium/modules/finance/importing/catalog.py`
- Test: `backend/tests/finance/importing/test_catalog.py`, `backend/tests/finance/importing/test_trading212_cash_isa.py` (create), `backend/tests/finance/importing/test_real_files.py`
- Modify: `README.md` (Trading 212 section)

**Interfaces:**
- Produces: a `CatalogEntry` with `institution="Trading 212 Cash ISA"`, `label="History CSV"`, `recommended=True`, appended last in `CATALOG`.

- [ ] **Step 1: Write the failing tests** in `test_trading212_cash_isa.py`, following `test_csv_parser.py::test_revolut_exports_through_the_catalog` (look the entry up by `(institution, label)`), with this synthetic file:

```python
CSV = (
    "Action,Time (UTC),Notes,ID,Total,Currency (Total)\n"
    'Deposit,2026-01-05 09:10:11+00:00,"Transaction ID: 11111111-2222-3333-4444-555555555555",'
    '01aaaaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee,25.00,"GBP"\n'
    "Withdrawal,2026-01-06 10:00:00+00:00,,01aaaaaa-bbbb-7ccc-8ddd-ffffffffffff,-10.00,\"GBP\"\n"
)
```

Assert, for the two parsed rows: `booking_date` 2026-01-05 and 2026-01-06; `occurred_at` 09:10:11 UTC on the first; `amount` `Decimal("25.00")` and `Decimal("-10.00")`; `currency == "GBP"`; `external_id` the `ID` cell; `memo` the whole `Notes` cell on the first and empty on the second; `bank_type` `"Deposit"` / `"Withdrawal"`; `description` the `Action`. Then, through staging and commit (as `test_staging.py:1157` uses a catalog entry): both rows commit as `bank_transfer=True`; staging the same file again adds no new row. Add the Review Focus cases 3 and 5: a file with the extra columns `Ticker` and `No. of shares` and a row whose `Action` is `Market buy` stages without posting anything, and that row's `bank_type` is `Market buy`; a file with no `Time (UTC)` column is refused, naming the column; a row with `Currency (Total)` `"GB"` and one with `Time (UTC)` `2026-01-05 09:10:11` are row errors.

- [ ] **Step 2: Run** `DOCKER_HOST=ssh://docker-test uv run --locked pytest -q tests/finance/importing/test_trading212_cash_isa.py`. Expected: FAIL, no such catalog entry.

- [ ] **Step 3: Add the entry** to `CATALOG`:

```python
CatalogEntry(
    "Trading 212 Cash ISA",
    "History CSV",
    CsvProfileConfig(
        format="csv",
        columns=CsvColumns(
            date="Time (UTC)",
            type="Action",
            description=["Action"],
            memo="Notes",
            external_id="ID",
            amount="Total",
            currency="Currency (Total)",
        ),
        date_format="%Y-%m-%d %H:%M:%S%z",
        timezone="UTC",
        # A Cash ISA takes bank transfers only (Trading 212's payment methods): every row is
        # one. An interest row's Action goes here once a real export shows it.
        card_types=[],
    ),
    recommended=True,
),
```

`sign` stays the default `inflow_positive`. If a test shows the parser needs something this config lacks, stop and report; do not change the parser.

- [ ] **Step 4: Update `test_catalog.py` and `test_real_files.py`.** In `test_catalog.py`, extend whatever enumerates institutions or counts entries. In `test_real_files.py`, add `private/t212-cash-isa/` as a directory the Cash ISA entry reads when present, following the existing entries at :180 and :215 (it must parse, stage and commit; skipped when the directory is absent).

- [ ] **Step 5: Run** the three test files. Expected: PASS.

- [ ] **Step 6: README.** In the Trading 212 section add: a Cash ISA has no API; add it as "Trading 212 Cash ISA" and upload the CSV from the app's History page; each export must start on the account's first day or on a day an earlier upload covers; interest rows import as ordinary rows until their `Action` is known. `README.zh-CN.md` gets the same paragraph in Chinese.

- [ ] **Step 7: Commit** `feat: add the Trading 212 Cash ISA History CSV import profile`.

---

### Task 2: The account type catalog

**Files:**
- Create: `backend/src/lararium/modules/finance/account_types.py`
- Test: `backend/tests/finance/test_account_types.py` (create)

**Interfaces:**
- Consumes: `importing.catalog.CATALOG` (Task 1).
- Produces:

```python
Method = Literal["card", "bank_transfer"]

@dataclass(frozen=True)
class AccountShape:
    ledger_type: LedgerType | None
    subtype: AccountSubtype
    tax_wrapper: TaxWrapper
    balance_mode: BalanceMode

@dataclass(frozen=True)
class Api:
    connector: str
    config: Mapping[str, JSONValue]

@dataclass(frozen=True)
class FileFormat:
    label: str
    format: str
    profile: ProfileConfigModel | None
    recommended: bool = False

@dataclass(frozen=True)
class AccountType:
    key: str
    institution: str
    label: str                      # "" when it is its institution's only type
    shape: AccountShape | None
    api: Api | None
    files: tuple[FileFormat, ...]
    funding: frozenset[Method] = frozenset()
    family: str | None = None

    @property
    def name(self) -> str: ...      # f"{institution} {label}".strip()

ACCOUNT_TYPES: tuple[AccountType, ...]
def account_type(key: str | None) -> AccountType | None
def family_keys(family: str) -> frozenset[str]
def keys_funded_by(method: Method) -> frozenset[str]
```

- [ ] **Step 1: Write the failing tests:**

```python
def test_keys_are_unique(): ...
def test_every_type_has_an_api_or_a_file(): ...
def test_a_file_without_a_profile_is_on_a_type_with_an_api(): ...
def test_a_family_has_at_least_two_types(): ...

def test_the_entries():
    assert [(t.key, t.name) for t in ACCOUNT_TYPES] == [
        ("monzo", "Monzo"),
        ("ibkr", "Interactive Brokers"),
        ("etoro", "eToro"),
        ("trading212.invest", "Trading 212 Invest"),
        ("trading212.stocks_isa", "Trading 212 Stocks ISA"),
        ("trading212.cash_isa", "Trading 212 Cash ISA"),
        ("hsbc_uk", "HSBC UK"),
        ("lloyds", "Lloyds"),
        ("boc_uk", "BOC UK"),
        ("revolut.current", "Revolut Current"),
        ("revolut.savings", "Revolut Savings"),
        ("tembo", "Tembo"),
        ("lemfi", "LemFi"),
    ]

def test_statement_files_are_the_import_catalogs():
    # every CATALOG entry is exactly one type's file, in CATALOG's order, same config object
    ...

def test_trading212():
    cash = account_type("trading212.cash_isa")
    assert cash.api is None
    assert cash.shape == AccountShape("asset", "savings_pot", "isa", "transactions")
    assert cash.funding == frozenset({"bank_transfer"})
    assert family_keys("trading212") == {
        "trading212.invest", "trading212.stocks_isa", "trading212.cash_isa"}
    assert keys_funded_by("bank_transfer") == family_keys("trading212")
    assert account_type("trading212.invest").api == Api("trading212", {"account_kind": "invest"})
    assert account_type("trading212.stocks_isa").api == Api("trading212", {"account_kind": "isa"})

def test_an_unknown_key_is_no_type():
    assert account_type("gone") is None and account_type(None) is None
```

- [ ] **Step 2: Run** `uv run --locked pytest -q tests/finance/test_account_types.py`. Expected: FAIL, module missing.

- [ ] **Step 3: Implement the module.** Values the tests do not pin:
  - API types have `shape=None`. `monzo`, `ibkr`, `etoro` have `Api(connector, {})`; `ibkr` has one file `FileFormat("Flex statement", "xml", None)`, `etoro` one `FileFormat("Account statement", "xlsx", None)`, the two Trading 212 API types one `FileFormat("History CSV", "csv", None)`.
  - Statement types (`hsbc_uk` … `lemfi`): `shape=AccountShape(None, "bank", "none", "transactions")`, `api=None`, files built from the `CATALOG` entries whose `institution` equals the type's `name`.
  - `trading212.cash_isa`: files from `CATALOG` the same way (its `name` is the entry's institution).
  - Funding `{"card", "bank_transfer"}` on the two Trading 212 API types; family `"trading212"` on all three; empty and `None` everywhere else.
  - Invariants are checked by a function run at import (raise `RuntimeError`), which the first four tests also call.
  - Import `importing.catalog` in a way that passes `lint-imports` and creates no cycle: nothing in `finance/accounts.py` or `importing/` may import `account_types`.

- [ ] **Step 4: Run** the tests, `pyright`, `lint-imports`. Expected: PASS.

- [ ] **Step 5: Commit** `feat: add the account type catalog`.

---

### Task 3: Accounts remember their type

**Files:**
- Create: `backend/migrations/versions/0038_finance_account_type_key.py` (style of `0032_finance_counterparty_account_number.py`)
- Modify: `backend/src/lararium/modules/finance/models.py` (`Account`), `backend/src/lararium/modules/finance/accounts.py`, `backend/src/lararium/modules/finance/trading212/accounts.py`
- Test: `backend/tests/finance/test_accounts.py`, `backend/tests/finance/trading212/test_normalize.py`, the migration tests' location used by 0032 (find with `git grep -l 0032 backend/tests`)

**Interfaces:**
- Produces:
  - column `finance.accounts.type_key text NULL`;
  - `AccountRecord.type_key: str | None`;
  - `create_account(..., type_key: str | None = None)`;
  - `async def set_type_key(ctx: CommandContext, account_id: UUID, type_key: str | None) -> bool` (True when it changed; publishes `finance.account.updated` with the change, as `rename_account` does);
  - `trading212.accounts.TYPE_KEYS: Mapping[str, str] = {"invest": "trading212.invest", "isa": "trading212.stocks_isa"}`.

- [ ] **Step 1: Write the failing tests:**
  - `test_accounts.py`: `create_account(..., type_key="trading212.cash_isa")` stores it and `get_account` returns it; `set_type_key` changes it, returns False when unchanged, and the change is in the audit data.
  - `trading212/test_normalize.py`: after a first normalise, the `invest` connection's account has `type_key == "trading212.invest"` and the `isa` one `"trading212.stocks_isa"`; an account created before (set its `type_key` to NULL by SQL) gets it on the next normalise.
  - Migration: upgrading with one Trading 212 `invest` and one `isa` connection's accounts present fills their `type_key`; a manual account and a Monzo account stay NULL; downgrade drops the column.

- [ ] **Step 2: Run** the three tests. Expected: FAIL.

- [ ] **Step 3: Migration.**

```sql
ALTER TABLE finance.accounts ADD COLUMN type_key text;
UPDATE finance.accounts a
   SET type_key = CASE c.config ->> 'account_kind'
                    WHEN 'invest' THEN 'trading212.invest'
                    WHEN 'isa' THEN 'trading212.stocks_isa' END
  FROM core.connections c
 WHERE a.source_connection_id = c.id AND c.connector = 'trading212';
```

- [ ] **Step 4: `accounts.py` and `models.py`.** Add the column to `Account`, `AccountRecord`, `_load` and `_change_data`; add the `create_account` parameter and `set_type_key`. `accounts.py` stores the string and does not import `account_types`.

- [ ] **Step 5: Trading 212.** In `trading212/accounts.account()`: pass `type_key=TYPE_KEYS[kind]` to `create_account`, and for a found account call `set_type_key` and count a change as `accounts_updated` (as `set_source_details`' result is).

- [ ] **Step 6: Run** the tests, `pyright`. Expected: PASS.

- [ ] **Step 7: Commit** `feat: store an account's type key and set it on Trading 212 accounts`.

---

### Task 4: API

**Files:**
- Modify: `backend/src/lararium/modules/finance/api.py`, `backend/src/lararium/modules/finance/schemas.py`
- Test: `backend/tests/api/test_finance_api.py`, `backend/tests/api/test_import_api.py` (the operation-id set at :356)
- Regenerate: `web/openapi.json`, `web/src/api/`

**Interfaces:**
- Consumes: `account_types.ACCOUNT_TYPES`, `account_type`; `accounts.create_account(type_key=)`, `set_type_key`; `staging.require_importable`.
- Produces:
  - `GET /api/v1/finance/account-types` (name `list_account_types`) → `list[AccountTypeOut]`:

```python
class AccountShapeOut(_Out):
    ledger_type: LedgerType | None
    subtype: AccountSubtype
    tax_wrapper: TaxWrapper
    balance_mode: BalanceMode

class AccountTypeApiOut(_Out):
    connector: str
    config: dict[str, JSONValue]

class AccountTypeFileOut(_Out):
    label: str
    format: str
    recommended: bool
    config: ProfileConfig | None      # the profile; None when the type's API reads it

class AccountTypeOut(_Out):
    key: str
    institution: str
    label: str
    name: str
    shape: AccountShapeOut | None
    api: AccountTypeApiOut | None
    files: list[AccountTypeFileOut]
    funding: list[str]                # sorted
    family: str | None
```

  - `AccountCreate.type_key: str | None = None`; `AccountOut.type_key: str | None`;
  - `AccountRename` becomes `name: str | None = None`, `type_key: str | None = None`, with a validator requiring at least one field to be set (`model_fields_set`).

- [ ] **Step 1: Write the failing tests** in `test_finance_api.py`:

```python
async def test_account_types_lists_the_catalog(client):
    types = (await client.get(f"{API}/account-types")).json()
    assert [t["key"] for t in types][:6] == [
        "monzo", "ibkr", "etoro",
        "trading212.invest", "trading212.stocks_isa", "trading212.cash_isa"]
    cash = types[5]
    assert cash["api"] is None and cash["family"] == "trading212"
    assert cash["funding"] == ["bank_transfer"]
    assert [f["label"] for f in cash["files"]] == ["History CSV"]
    assert cash["files"][0]["config"]["format"] == "csv"

async def test_create_from_a_type(client):
    # body: name, ledger_type asset, subtype savings_pot, balance_mode transactions,
    # tax_wrapper isa, type_key trading212.cash_isa, no institution_name
    # → 201; type_key returned; institution_entity_id is Trading 212's organisation

async def test_create_from_a_type_refuses_a_disagreeing_shape(client):
    # same with subtype "bank" → 422, error "type_mismatch"; no account created

async def test_create_from_a_statement_type_takes_either_side(client):
    # type_key hsbc_uk with ledger_type liability → 201

async def test_create_refuses_an_api_type_and_an_unknown_one(client):
    # trading212.invest → 422 "type_not_creatable"; "nope" → 422 "unknown_type"

async def test_patch_sets_the_type_of_an_importable_account(client):
    # manual transactions account; PATCH {"type_key": "lloyds"} → 200, name unchanged

async def test_patch_refuses_what_cannot_take_imports(client):
    # snapshots account → 422; API type key → 422; unknown key → 422
    # PATCH {} → 422; PATCH {"type_key": None} on a typed account clears it

async def test_rename_still_works(client): ...
```

- [ ] **Step 2: Run** them. Expected: FAIL.

- [ ] **Step 3: Implement.**
  - `list_account_types`: build from `ACCOUNT_TYPES`.
  - Create with `type_key`: the type must exist (`unknown_type`) and have no API (`type_not_creatable`); `subtype`, `tax_wrapper`, `balance_mode` must equal the shape's, and `ledger_type` too when the shape states one (`type_mismatch`); the institution is `get_or_create_organization(ctx, type.institution)` and a non-null `institution_name` in the body is refused (`type_mismatch`). Without `type_key` the route is unchanged.
  - `PATCH`: `name` renames as today. A non-null `type_key` must exist and have no API, and the account must pass `require_importable` (map `ImportNotAllowedError` to 422 with its message). `type_key: null`, when sent, clears it. Both changes happen in one command.
  - Error bodies use the route file's existing `{"error": …, "message": …}` shape; messages name the field and the type's name, no value.
  - Add `list_account_types` to the operation-id set in `test_import_api.py`.

- [ ] **Step 4: Run** the tests, `pyright`; then `make openapi`. Expected: PASS, and `make openapi-check` clean.

- [ ] **Step 5: Commit** `feat: list account types and create or retype an account by type`, including the regenerated client.

---

### Task 5: The Add account page lists account types

**Files:**
- Modify: `web/src/pages/AddAccountPage.tsx`, `web/src/pages/ConnectionsPage.tsx`, `web/src/pages/ImportsPage.tsx`
- Create: `web/src/lib/accountTypes.ts`
- Test: `web/src/pages/AddAccountPage.test.tsx`, `web/src/pages/ConnectionsPage.test.tsx`, `web/src/pages/ImportsPage.test.tsx`, `web/src/lib/accountTypes.test.ts` (create)

**Interfaces:**
- Consumes: `listAccountTypes`, `AccountTypeOut` from the generated client (Task 4).
- Produces:
  - `lib/accountTypes.ts`:
    - `byInstitution(types: AccountTypeOut[]): [string, AccountTypeOut[]][]` (catalog order, grouped by `institution`);
    - `statementFormats(type: AccountTypeOut): ImportCatalogEntryOut[]` (its files that have a `config`, as `{institution: type.name, label, format, config, recommended}`);
    - `tileDetail(type: AccountTypeOut): string` (see Step 3);
    - `typeOf(types: AccountTypeOut[], account: AccountOut): AccountTypeOut | undefined` (undefined for a null or unknown `type_key`).
  - `ConnectionsPage`: `PROVIDERS` and `Provider` are removed; `ConnectForm({ type, onAdded }: { type: AccountTypeOut; onAdded?: () => void })` switches on `type.api.connector`; `AddTrading212Form` takes `kind: "invest" | "isa"` from `type.api.config.account_kind` and no longer shows its "Account type" select.
  - `UploadForm({ formats, type, account }: { formats?: ImportCatalogEntryOut[]; type?: AccountTypeOut; account?: AccountOut })`.

- [ ] **Step 1: Write the failing tests.**
  - `accountTypes.test.ts`: `tileDetail` for an API type with a file → `"Syncs automatically · CSV export optional"`; API type with no file → `"Syncs automatically"`; statement type → `"Upload statements · PDF statement recommended"` (the recommended file's label); `trading212.cash_isa` → `"Upload its History CSV"`. `typeOf` returns undefined for `type_key: "gone"`.
  - `AddAccountPage.test.tsx`: with a mocked `listAccountTypes` returning Monzo, the three Trading 212 types and HSBC UK: the page shows headings "Trading 212", "Monzo", "HSBC UK" and the tiles "Invest", "Stocks ISA", "Cash ISA", and no "Connect" or "Upload statements" heading; picking "Stocks ISA" shows the Trading 212 key form with no "Account type" select and submits `account_kind: "isa"`; picking "Cash ISA" shows the upload form with the name prefilled `Trading 212 Cash ISA` and no "money I have / money I owe" choice, and submitting calls `createAccount` with `objectContaining({ type_key: "trading212.cash_isa", subtype: "savings_pot", tax_wrapper: "isa", balance_mode: "transactions", ledger_type: "asset", institution_name: null })` and `stageImport`; picking "HSBC UK" still asks the side and sends `type_key: "hsbc_uk"`; "Another bank's file" and "Type it in yourself" behave as before.
  - `ConnectionsPage.test.tsx`: replace the `PROVIDERS` loop with one `ConnectForm` render per API type fixture.
  - `ImportsPage.test.tsx`: `UploadForm` with `account` set shows no account select and stages into that account; without the new props it behaves as before.

- [ ] **Step 2: Run** `cd web && pnpm test --run src/pages/AddAccountPage.test.tsx src/lib/accountTypes.test.ts src/pages/ConnectionsPage.test.tsx src/pages/ImportsPage.test.tsx`. Expected: FAIL.

- [ ] **Step 3: Implement.**
  - `Picker`: one `Group` per institution from `byInstitution`, a `Tile` per type titled `type.label || type.institution`, detail `tileDetail(type)`; then the unchanged "Something else" group. Query key `["account-types"]`.
  - `Choice` becomes `{ kind: "type"; type: AccountTypeOut } | { kind: "other-file" } | { kind: "manual" }`. A type with an API renders `ConnectForm`; one without renders `StatementPanel` with `statementFormats(type)` and `type`.
  - `UploadForm` with `type`: the new-account name defaults to `type.name`; the side is asked only when `type.shape.ledger_type` is null; `createAccount` gets the shape's fields and `type_key`, and no `institution_name`. With `account`: the account select is hidden and that account is used.
  - `tileDetail` copy, exactly: API → `"Syncs automatically"`, plus `" · CSV export optional"` when it has a `csv` file; no API with one file → `` `Upload its ${file.label}` ``; no API with several → `` `Upload statements · ${recommended.label} recommended` ``.

- [ ] **Step 4: Run** the tests and `pnpm typecheck`. Expected: PASS.

- [ ] **Step 5: Commit** `feat: list account types on the Add account page`.

---

### Task 6: Upload from an account

**Files:**
- Modify: `web/src/pages/AccountsPage.tsx`
- Test: `web/src/pages/AccountsPage.test.tsx`

**Interfaces:**
- Consumes: `typeOf`, `statementFormats` (Task 5); `UploadForm({ formats, account })`.

- [ ] **Step 1: Write the failing tests:** an open manual account with `type_key: "trading212.cash_isa"` shows an "Upload file" button that reveals the upload form for that account with no account select; a Trading 212 account with `type_key: "trading212.invest"` shows an "Upload file" link to `/connections`; an account with `type_key: null` or `type_key: "gone"`, and a closed account with a statement type, show neither.

- [ ] **Step 2: Run** `pnpm test --run src/pages/AccountsPage.test.tsx`. Expected: FAIL.

- [ ] **Step 3: Implement.** `Panel` gains `"upload"`. In `AccountCard`: when `typeOf` gives a type with statement formats and `isImportable(account)`, a `PanelButton` "Upload file" reveals `<UploadForm formats={statementFormats(type)} account={account} />`; when the type has an API and files, a `Link` "Upload file" to `/connections` instead.

- [ ] **Step 4: Run** the test and `pnpm typecheck`. Expected: PASS.

- [ ] **Step 5: Commit** `feat: upload a statement from its account`.

---

### Task 7: Wrap-up

- [ ] **Step 1:** From `backend/`: `uv run --locked ruff check . && uv run --locked ruff format --check . && uv run --locked pyright && uv run --locked lint-imports`. From the root: `make openapi-check`. From `web/`: `pnpm typecheck && pnpm test --run`. Expected: all clean.
- [ ] **Step 2:** `DOCKER_HOST=ssh://docker-test make real-check` when `private/t212-cash-isa/` exists. Expected: the Cash ISA file parses, stages and commits. Report only pass or fail and counts, never a value from the file.
- [ ] **Step 3:** Set the spec's Status to "approved 2026-11-26; Part A implemented" and commit it with this plan: `docs: add the account type catalog design and Part A plan`.
- [ ] **Step 4:** Push and open the PR against `main`; CI runs the full suites. The PR description lists the after-deploy steps of the spec's §13 (1 and 2).
