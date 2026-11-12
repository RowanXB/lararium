# Manual Import Implementation Plan

**Languages:** English | [简体中文](./2026-11-12-manual-import.zh-CN.md)

The English version is authoritative; if the two versions differ, follow this one.

> **Status: deferred (2026-11-12).** The owner mostly relies on the Monzo API and imports by hand only occasionally, so M3 (Monzo) comes first. When this plan is resumed:
> - **Task 1 (documents)** will already have been built by the M3 plan, which stores large API responses as documents. Check what M3 built and skip or adapt Task 1.
> - **Task 4 (OFX)** and the OFX parts of the other tasks are optional. None of the owner's banks currently provides OFX; skip them unless one does. These are `OfxProfileConfig`, `LEDGERBAL` in Decision 13, the OFX tests in Tasks 5 and 7, and the OFX example files.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import bank statements (CSV through per-institution parse profiles, and OFX) into the ledger: upload a file, review the staged rows (new, duplicate, skipped, error), commit them as transactions, and roll a whole batch back.

**Architecture:** Uploaded files become content-addressed **documents** in `core` (a local directory behind a `DocumentStore` interface). A new `lararium.modules.finance.importing` package holds the parse profiles (stored as data), two pure parsers (CSV, OFX), the staging service (parse → dedupe → `finance.import_rows`) and the commit/rollback service, which turns each row into a two-posting transaction through the existing `ledger.post_transaction`. Parsing runs synchronously inside the API request; files are capped at 5 MiB.

**Tech Stack:** as plan 1, plus `python-multipart` for uploads. No OFX library: OFX is parsed by a small hand-written tokenizer (see Decisions).

**Spec:** `docs/superpowers/specs/2026-11-11-financial-gate-phase1-design.md` (this plan covers M2: §7.8, plus the documents object from §6.2)

**Builds on:** plan 1 (`docs/superpowers/plans/2026-11-11-foundation-ledger-core.md`) as merged in `main` (`aef64ee`), including migrations up to `0007`.

## Decisions

Choices the spec leaves open, made here so the tasks don't have to:

1. **The other leg of an imported row.** A bank row is single-entry; the ledger is double-entry. Each row becomes a transaction with two postings: the imported account, and one system account **"Unassigned (imports)"** (`ledger_type` `equity`, subtype `other`, balance mode `transactions`), created on first use and remembered in the setting `finance.import_contra_account_id`. Net worth counts only asset and liability accounts, so this account never changes net worth. Transfer matching (M4) will later re-point these legs.
2. **Sign convention.** Profiles speak in **inflow** (the account's balance goes up) and **outflow**. On a credit card (a liability), a purchase is an outflow (the balance becomes more negative) and a payment to the card is an inflow. A profile maps either one signed amount column with `sign: "inflow_positive" | "outflow_positive"`, or two unsigned columns `inflow` and `outflow` (quantity = inflow − outflow).
3. **Which accounts accept imports.** Only open, manual accounts whose balance mode is `transactions` or `hybrid`. Valuation ignores transactions on `snapshots` accounts, so importing into one would be silently invisible; it is refused instead.
4. **Errors block the commit; zeros and filtered rows are skipped.** A row that fails to parse, or that breaks the balance chain (Decision 12), is stored with status `error` and its message, and a batch with any error row cannot be committed (fix the profile or the file, then upload again). A row whose amount is zero, or that the profile's `include_only` filter leaves out (e.g. a `PENDING` card payment), has status `skipped` with the reason in `note`, and does not block. Duplicates are never committed. There is no per-row override in M2.
5. **Deduplication key** (spec §6.4), per account:
   - the row has an external ID (OFX `FITID`, or a mapped CSV column): `sha256("ext", account, external_id)`;
   - otherwise, the row has a running balance: `sha256("bal", account, date, quantity, currency, description, balance)`;
   - otherwise: `sha256("seq", account, date, quantity, currency, description, n)`, where `n` counts earlier rows in the **same file** with the same date, quantity, currency and description. Two genuine identical purchases get `n = 0` and `n = 1`; a re-export that contains both reproduces both keys.

   Quantities and balances are canonicalised with `to_plain_str(value.normalize())`; descriptions are case-folded with whitespace collapsed. A row is a duplicate when a **committed** row on the same account has the same key. A partial unique index on committed rows is the database backstop.
6. **Dates.** `booking_date` is always the date as written in the file, never shifted by UTC conversion.
   - Date-only values (CSV, or an OFX `DTPOSTED` without a time): `occurred_at` is local midnight of that date, with `occurred_tz` set to the zone's name.
   - CSV values with a time (a `date_format` containing `%H`, `%I`, `%M`, `%S` or `%p`, e.g. `15 Oct, 2026 09:05`): the wall-clock time is read in the profile's `timezone` and `occurred_tz` is set to it.
   - An OFX `DTPOSTED` with a time is parsed with its offset (UTC when the file gives none, per the OFX spec), and its booking date is still the `YYYYMMDD` written in the file.
   - "Local" means the profile's `timezone` when set, else `core.timezone`.
7. **Rollback voids; it never deletes.** Rolling back a committed batch voids its transactions (already-void ones are left alone) and marks its rows `rolled_back`, so the same file can be imported again. Discarding a staged batch only marks it `discarded`. Documents are never deleted.
8. **OFX without a library.** `ofxtools` is GPL-3.0 (compatible with AGPL) but converts every timestamp to UTC, which loses the date as written. Statement transactions need only a few leaf fields, so a small tokenizer that handles both OFX 1.x SGML (unclosed leaf tags) and 2.x XML is simpler and deterministic.
9. **Profiles are edited as JSON in the UI**, validated by the server. A form builder can come later.
10. **Imported transactions keep `origin = 'manual'`**; their provenance is the `finance.import_rows.transaction_id` link. This avoids changing the `transactions.origin` CHECK.
11. **Fees.** A profile may map a `fee` column. The fee is charged on top of the amount, so the account's balance changes by `quantity − fee`. A row with a non-zero fee becomes three postings: the account `+quantity`; the account `−fee` with `is_fee = true`; and the contra account `−(quantity − fee)`.
12. **The balance chain is checked.** When rows carry a running balance, staging walks them in chronological order (the profile's `row_order`), separately for each instrument. It keeps an expected balance: it adds each importable row's `quantity − fee`, and whenever a row states a balance, that balance must equal the expected one. The stated balance then becomes the new expected one.
    - A mismatch makes that row an `error`: "balance doesn't follow from the previous row: expected X, the file says Y". A wrong `sign`, a skipped row that actually moved money, or a misread column is caught here instead of silently distorting net worth.
    - Skipped rows don't count. Duplicate rows do, because they are real lines of the statement. A parse-error row resets the chain.
13. **The opening balance is aligned automatically.** A statement rarely starts at the account's first day, so the sum of its rows is not the account's balance.
    - **Deriving it.** Staging works out the statement's opening balance per instrument: from the chronologically first row with a balance (its balance minus the net of the rows up to and including it), or else from the OFX `LEDGERBAL` (closing balance minus the net of all importable rows).
    - **Comparing.** It compares that with the ledger: the sum of the account's non-void postings in that instrument booked before the statement's first date.
    - **Adjusting.** If the two differ, staging adds one row per instrument with `kind = 'opening_adjustment'`, a negative `position`, the description "Opening balance adjustment", the difference as its quantity, and booking date the day before the statement's first date. Its other leg goes to a second system account **"Opening balances"** (`equity`, setting `finance.opening_balance_account_id`).
    - **What it means.** On a first import this row is the opening balance. Later, a non-zero adjustment usually means a statement period is missing: the review page says so, and the user can discard the batch and import the gap first.
    - **When it doesn't apply.** Hybrid accounts skip the adjustment, because their snapshots anchor the balance; the chain check still runs. A file with neither balances nor `LEDGERBAL` gets the batch warning "This file has no balances, so its opening balance can't be checked".
    - **At commit.** The adjustment is recomputed. If the ledger moved since staging (another batch was committed), the commit is refused with `ImportBatchStaleError`, and the user uploads the file again.

**Not in this plan:** pairing staged rows with inferred postings (needs M4), alerts when a snapshot and the ledger drift apart (M9), regenerating the ledger from documents (§6.6; this plan stores the document, a snapshot of the profile and the parser version so it can be done later), PDF parsing, background workers.

## Global Constraints

- Everything in plan 1's Global Constraints still applies: Python 3.13, uv with the lockfile enforced, ruff, pyright strict on `src/`, import-linter contracts, PostgreSQL 18, `NUMERIC`/`Decimal`/strings and the word `float` nowhere in `src/`, UUIDv7, UTC `timestamptz` plus separate `date`, English UI copy, bilingual docs (English primary, `*.zh-CN.md`), AGPL-3.0-compatible dependencies only.
- Branch `feat/claude-manual-import`; commit messages `<type>: <summary>`; sign commits with the `local-signed-git-commit` skill.
- Every write goes through `command_context` (one audit row per request, events in the outbox). New write routes reuse `ActorDep`, so the cross-site check applies to uploads too.
- **No real statements anywhere in the repository.** Example and test files are hand-written and synthetic, with the fictional institution "Example Bank". The owner's real exports stay in `private/`.
- Amount limits: imported quantities and balances go through `money.require_amount` (at most 40 digits on each side of the point); a value that fails becomes a row error, never a 500.
- Upload limit `MAX_IMPORT_BYTES = 5 * 1024 * 1024`; row limit `MAX_IMPORT_ROWS = 20_000`.
- `core` stays finance-agnostic: `core.documents` knows nothing about imports, and the `source` it records is a free string (`"finance.import"`).

## Review Focus

Inputs the spec implies but the main-path tests would not hit, most likely first. Each has a test in its owning task. The first two were found by checking the owner's real exports (LemFi and Revolut CSVs; the tests use synthetic copies of their structure only):

- **A statement that starts mid-history:** the account's balance after import equals the statement's closing balance, not the sum of its rows (Task 5: `test_opening_adjustment_from_first_balance`; Task 7: `test_example_csv_end_to_end`).
- **A pending row exported before it completes:** it is skipped by the `include_only` filter, so importing the completed version later does not count it twice (Task 3: `test_include_only_skips_before_parsing`; Task 6: `test_pending_then_completed_counted_once`).

- **The same statement exported twice with overlapping date ranges:** only the rows not already committed are new (Task 6: `test_overlapping_export_commits_only_new_rows`).
- **Two genuine identical purchases on one day:** both are imported, and re-importing the same file marks both as duplicates (Task 5: `test_identical_rows_in_one_file_are_both_new`; Task 6: `test_reimporting_same_file_marks_all_duplicate`).
- **A credit-card statement:** a purchase makes the liability more negative and a payment brings it towards zero (Task 6: `test_liability_import_signs`).
- **Date-only rows across the BST change:** `booking_date` stays the date written in the file and is not shifted by UTC conversion (Task 5: `test_date_only_rows_keep_booking_date_across_dst`).
- **Excel-style CSVs:** a UTF-8 BOM before the header and a cp1252 `£` decode correctly; a file that isn't valid in the profile's encoding is a clear file-level error, never mojibake (Task 3: `test_utf8_bom_header`, `test_cp1252_pound_sign`, `test_wrong_encoding_is_file_error`).

---

## File Structure

```
backend/
  migrations/versions/
    0008_core_documents.py          # Task 1
    0009_finance_import_profiles.py # Task 2
    0010_finance_import_batches.py  # Task 5
  src/lararium/
    config.py                       # + document_dir (Task 1)
    deps.py                         # + get_document_store (Task 7)
    api/app.py                      # + app.state.document_store (Task 7)
    core/
      documents.py                  # Task 1: DocumentStore, LocalDocumentStore, store/read
      errors.py                     # + DocumentNotFoundError, DocumentIntegrityError
      models.py                     # + Document
    modules/finance/
      errors.py                     # + import errors (Tasks 2–6)
      models.py                     # + ImportProfile, ImportBatch, ImportRow
      schemas.py                    # + import schemas (Task 7)
      api.py                        # + import routes (Task 7)
      importing/
        __init__.py
        profiles.py                 # Task 2: config models + CRUD
        parsed.py                   # Task 3: ParsedRow, RowError, amount/description helpers
        csv_parser.py               # Task 3
        ofx_parser.py               # Task 4
        staging.py                  # Task 5
        commit.py                   # Task 6
        examples/                   # Task 7: synthetic Example Bank files
          example-bank-csv.json
          example-bank.csv
          example-bank-ofx.json
          example-bank.ofx
  tests/
    core/test_documents.py
    finance/importing/test_profiles.py, test_csv_parser.py, test_ofx_parser.py,
                     test_staging.py, test_commit.py
    api/test_import_api.py
web/src/
  pages/ImportsPage.tsx, ImportBatchPage.tsx, ImportProfilesPage.tsx (+ .test.tsx)
  router.tsx                        # + three routes, "Imports" and "Profiles" nav
infra/compose.yaml, backup/backup.sh, README.md, README.zh-CN.md   # Task 9
backend/Dockerfile                                                 # Task 9
```

---

### Task 1: Documents (core)

**Files:**
- Create: `backend/migrations/versions/0008_core_documents.py`, `backend/src/lararium/core/documents.py`, `backend/tests/core/test_documents.py`
- Modify: `backend/src/lararium/core/models.py`, `backend/src/lararium/core/errors.py`, `backend/src/lararium/config.py`

**Interfaces:**
- Consumes: `CommandContext`, `command_context`, the `session`/`run` fixtures.
- Produces:
  - `class DocumentStore(Protocol)`: `def put(self, sha256: str, content: bytes) -> None`; `def get(self, sha256: str) -> bytes`.
  - `class LocalDocumentStore(DocumentStore)`: `__init__(self, root: Path)`. Stores at `root / sha[:2] / sha[2:4] / sha`, writing a temporary file in the same directory and `os.replace`-ing it into place; `put` of an existing hash is a no-op.
  - `@dataclass(frozen=True) class DocumentRecord`: `id: UUID, sha256: str, size_bytes: int, media_type: str, original_filename: str, source: str, created_at: datetime`.
  - `async def store_document(ctx: CommandContext, store: DocumentStore, content: bytes, *, filename: str, media_type: str, source: str) -> DocumentRecord`: writes the content first, then inserts with `ON CONFLICT (sha256) DO NOTHING` and returns the existing row on conflict (keeping the first filename). It emits `core.document.stored` only for a new row and calls `ctx.record_change("document", {...})`.
  - `async def get_document(session: AsyncSession, document_id: UUID) -> DocumentRecord`: raises `DocumentNotFoundError`.
  - `async def read_document(session: AsyncSession, store: DocumentStore, document_id: UUID) -> bytes`: re-hashes and raises `DocumentIntegrityError` on mismatch or a missing file.
  - `Settings.document_dir: Path = Path("/var/lib/lararium/documents")` (env `LARARIUM_DOCUMENT_DIR`).

Migration `0008`: `core.documents(id uuid PK DEFAULT uuidv7(), sha256 text NOT NULL UNIQUE CHECK (sha256 ~ '^[0-9a-f]{64}$'), size_bytes bigint NOT NULL CHECK (size_bytes >= 0), media_type text NOT NULL, original_filename text NOT NULL, source text NOT NULL, created_at timestamptz NOT NULL DEFAULT now())`, then `REVOKE UPDATE, DELETE ON core.documents FROM lararium_backend` (documents are immutable raw data, like the audit log).

- [ ] **Step 1: Write the failing tests** in `tests/core/test_documents.py`, using a `store` fixture `LocalDocumentStore(tmp_path)`:
  - `test_store_and_read_round_trip`: storing `b"date,amount\n"` with filename `a.csv` returns a record with `size_bytes == 12` and a 64-hex `sha256`, and `read_document` returns the same bytes.
  - `test_same_content_is_stored_once`: storing the same bytes as `a.csv` and then `b.csv` returns the same `id`; `original_filename == "a.csv"`; exactly one `core.document.stored` event exists for it.
  - `test_tampered_file_is_an_integrity_error`: overwrite the stored file's bytes, then `read_document` raises `DocumentIntegrityError`; delete the file and it raises `DocumentIntegrityError` too.
  - `test_unknown_document_is_not_found`: `get_document(session, uuid7-like random UUID)` raises `DocumentNotFoundError`.
  - `test_backend_role_cannot_update_or_delete_documents`: as the backend session, `UPDATE core.documents SET source = 'x'` and `DELETE FROM core.documents` each raise `ProgrammingError` (insufficient privilege). Use a savepoint for each.
- [ ] **Step 2: Run** `cd backend && uv run --locked pytest tests/core/test_documents.py -q`; expected: FAIL (import error).
- [ ] **Step 3: Implement** the migration, the `Document` ORM model, the errors and `core/documents.py` as specified.
- [ ] **Step 4: Run** the same command; expected: 5 passed. Then `make backend-check`; expected: green.
- [ ] **Step 5: Commit** `feat: add content-addressed documents to core`.

### Task 2: Parse profiles

**Files:**
- Create: `backend/migrations/versions/0009_finance_import_profiles.py`, `backend/src/lararium/modules/finance/importing/__init__.py`, `.../importing/profiles.py`, `backend/tests/finance/importing/test_profiles.py`
- Modify: `backend/src/lararium/modules/finance/models.py`, `.../finance/errors.py`

**Interfaces:**
- Produces (in `importing/profiles.py`). All config models are Pydantic models with `ConfigDict(extra="forbid", frozen=True)`:
  - `class AmountFormat`: `decimal_separator: Literal[".", ","] = "."`; `thousands_separator: Literal["", ",", ".", " ", "'"] = ""`. Validator: the two must differ.
  - `class CsvColumns`: `date: str`; `description: list[str]` (min 1; joined with one space); `amount: str | None = None`; `inflow: str | None = None`; `outflow: str | None = None`; `balance: str | None = None`; `currency: str | None = None`; `external_id: str | None = None`; `fee: str | None = None`. Validator: exactly one of `amount`, or both `inflow` and `outflow`.
  - `class CsvProfileConfig`:
    - `format: Literal["csv"]`
    - `encoding: Literal["utf-8-sig", "utf-8", "cp1252", "latin-1"] = "utf-8-sig"`
    - `delimiter: str = ","` (exactly one character)
    - `skip_rows: int = 0` (0–50)
    - `columns: CsvColumns`
    - `date_format: str`. Validated by formatting `datetime(2026, 12, 31, 23, 59, 58)` with it and parsing the result back: the parsed date must be 2026-12-31. A format with `%H`, `%I`, `%M`, `%S` or `%p` carries a time (property `has_time: bool`).
    - `timezone: str | None = None`, an IANA name checked with `ZoneInfo`
    - `include_only: dict[str, list[str]] = {}`, column → allowed values (exact match after stripping); each list has at least one value
    - `row_order: Literal["oldest_first", "newest_first"] = "oldest_first"`
    - `sign: Literal["inflow_positive", "outflow_positive"] = "inflow_positive"`
    - `amount_format: AmountFormat = AmountFormat()`
  - `class OfxProfileConfig`: `format: Literal["ofx"]`; `sign: Literal["inflow_positive", "outflow_positive"] = "inflow_positive"`; `timezone: str | None = None`.
  - `type ProfileConfig = Annotated[CsvProfileConfig | OfxProfileConfig, Field(discriminator="format")]`.
  - `@dataclass(frozen=True) class ImportProfileRecord`: `id: UUID, name: str, config: CsvProfileConfig | OfxProfileConfig, version: int, updated_at: datetime`.
  - `async def create_profile(ctx, *, name: str, config: CsvProfileConfig | OfxProfileConfig) -> ImportProfileRecord`
  - `async def update_profile(ctx, profile_id: UUID, *, name: str, config: CsvProfileConfig | OfxProfileConfig) -> ImportProfileRecord`: increments `version`.
  - `async def get_profile(session, profile_id: UUID) -> ImportProfileRecord`; `async def list_profiles(session) -> list[ImportProfileRecord]` (by name).
  - Errors in `finance/errors.py`: `ImportProfileNotFoundError`, `DuplicateProfileNameError`.

Migration `0009`: `finance.import_profiles(id uuid PK DEFAULT uuidv7(), name text NOT NULL, format text NOT NULL CHECK (format IN ('csv','ofx')), config jsonb NOT NULL, version integer NOT NULL DEFAULT 1 CHECK (version >= 1), owner_entity_id uuid NOT NULL REFERENCES core.entities, created_at/updated_at timestamptz NOT NULL DEFAULT now(), CHECK (config->>'format' = format))`, plus a unique index on `lower(btrim(name))`. Names are stripped on write, and an empty name is a `FinanceValidationError`.

- [ ] **Step 1: Write the failing tests:**
  - `test_amount_xor_inflow_outflow`: `CsvColumns(date="D", description=["X"])` raises; giving `amount` together with `inflow`/`outflow` raises; `inflow` without `outflow` raises.
  - `test_separators_must_differ`: `AmountFormat(decimal_separator=",", thousands_separator=",")` raises.
  - `test_unknown_key_rejected`: validating `{"format": "csv", ..., "colour": "red"}` raises.
  - `test_date_format_must_round_trip`: `"%d/%m/%Y"`, `"%b %d, %Y"`, `"%d %b, %Y %H:%M"` and `"%Y-%m-%d %H:%M:%S"` are accepted (the last two with `has_time`); `"%d/%m"` and `"not a date"` are rejected.
  - `test_timezone_and_filters_validated`: `timezone="Europe/Nowhere"` is rejected; `include_only={"State": []}` is rejected.
  - `test_update_bumps_version`: create → `version == 1`; update → `version == 2` with the new config.
  - `test_duplicate_name_case_insensitive`: creating `"Example Bank"` then `" example bank "` raises `DuplicateProfileNameError`.
- [ ] **Step 2: Run** `uv run --locked pytest tests/finance/importing/test_profiles.py -q`; expected: FAIL.
- [ ] **Step 3: Implement** as specified. Store `config.model_dump(mode="json")`; load it back with a `TypeAdapter(ProfileConfig)`. Detect the duplicate name through the unique index, translating sqlstate `23505` for that index into `DuplicateProfileNameError`.
- [ ] **Step 4: Run**; expected: 7 passed; `make backend-check` green.
- [ ] **Step 5: Commit** `feat: add import parse profiles stored as data`.

### Task 3: CSV parser

**Files:**
- Create: `.../importing/parsed.py`, `.../importing/csv_parser.py`, `backend/tests/finance/importing/test_csv_parser.py`
- Modify: `.../finance/errors.py` (+ `FileFormatError(FinanceError)`)

**Interfaces:**
- Consumes: `CsvProfileConfig`, `AmountFormat` (Task 2); `money.require_amount`.
- Produces:
  - In `parsed.py`:
    - `@dataclass(frozen=True) class ParsedRow`: `position: int, booking_date: date, occurred_at: datetime | None, description: str, quantity: Decimal, fee: Decimal, currency: str | None, balance: Decimal | None, external_id: str | None, raw: dict[str, str]`.
      - `quantity` is already in ledger sign (inflow positive).
      - `fee` is `Decimal(0)` when no fee is mapped or the cell is empty; the balance changes by `quantity - fee`.
      - `occurred_at` is aware when the source gives a time, else `None`.
    - `@dataclass(frozen=True) class RowError`: `position: int, message: str, raw: dict[str, str]`.
    - `@dataclass(frozen=True) class SkippedRow`: `position: int, reason: str, raw: dict[str, str]`. Used for rows the `include_only` filter leaves out.
    - `@dataclass(frozen=True) class ParsedStatement`: `rows: list[ParsedRow | RowError | SkippedRow]`, `closing_balance: Decimal | None = None`, `closing_balance_date: date | None = None`. Only OFX sets the closing fields.
    - `MAX_IMPORT_ROWS = 20_000`.
    - `def parse_amount(text: str, fmt: AmountFormat) -> Decimal`: strips whitespace; accepts one leading `+`/`-`, or wrapping parentheses meaning negative; strips one leading currency symbol from `£ $ €` after the sign; removes thousands separators; maps the decimal separator to `.`; then requires `^[0-9]+(\.[0-9]+)?$` with ASCII digits only and calls `require_amount`. Anything else raises `ValueError` with a message naming the text.
    - `def clean_description(parts: list[str]) -> str`: joins the parts with a space, collapses whitespace, strips, and truncates to 1000 characters.
  - In `csv_parser.py`: `PARSER_VERSION = "csv-1"`; `def parse_csv(content: bytes, config: CsvProfileConfig, local_tz: ZoneInfo) -> ParsedStatement`. `local_tz` is the profile's `timezone` when set; the caller resolves it.

Rules for `parse_csv`:
- Decode with `config.encoding`. `UnicodeDecodeError` raises `FileFormatError("the file is not valid <encoding>")`.
- Drop `skip_rows` physical lines, then read with `csv.DictReader(delimiter=config.delimiter)`.
- A header missing any mapped column, or any `include_only` column, raises `FileFormatError` naming the missing columns. A file with no data rows raises `FileFormatError("no rows")`. More than `MAX_IMPORT_ROWS` data rows raises `FileFormatError`.
- Lines whose cells are all empty are ignored. `position` is the 0-based index among the remaining data rows.
- `raw` is the row's mapped and `include_only` columns as written.
- **Filter first.** A row whose `include_only` column holds a value not in the list becomes `SkippedRow(reason="State is PENDING")` (the column name and value) **before** any other field is parsed. A pending row typically has an empty completion date and balance, and must not become an error.
- For each remaining row, any of the following gives a `RowError` with a specific message, never an exception:
  - a date that does not match `date_format`;
  - an amount, balance or fee that fails `parse_amount`;
  - empty amount cells: for `amount`, an empty cell; for `inflow`/`outflow`, both empty (one empty cell counts as 0);
  - a currency cell that is not three ASCII letters (it is upper-cased and stripped first).
- **Empty optional cells.** An empty balance cell gives `balance = None`; an empty fee cell gives `fee = 0`.
- **Dates and times.** When `config.has_time`, the parsed wall-clock time is made aware with `local_tz` (`occurred_at`), and `booking_date` is its date as written. Otherwise `occurred_at = None`.
- **Signs.** With `amount`, quantity = amount for `inflow_positive` and −amount for `outflow_positive`; with `inflow`/`outflow`, quantity = inflow − outflow, and `sign` is ignored. The fee is taken as written; a positive fee is a charge.

- [ ] **Step 1: Write the failing tests** (configs built inline; bytes literals):
  - `test_signed_amount_column`: `Date,Description,Amount\n10/11/2026,Coffee,-3.50\n11/11/2026,Salary,2000.00\n` with `%d/%m/%Y` gives quantities `Decimal("-3.50")` and `Decimal("2000.00")` and booking dates `2026-11-10` and `2026-11-11`.
  - `test_outflow_positive_sign`: the same file with `sign="outflow_positive"` gives `3.50` and `-2000.00`.
  - `test_inflow_outflow_columns`: `Paid in` empty and `Paid out` `"1,234.56"` with `thousands_separator=","` gives `-1234.56`.
  - `test_parentheses_and_currency_symbol`: `"(£12.00)"` gives `-12.00`; `"€1.234,50"` with decimal `,` and thousands `.` gives `1234.50`.
  - `test_utf8_bom_header`: `b"\xef\xbb\xbfDate,Description,Amount\n..."` with encoding `utf-8-sig` finds the `Date` column.
  - `test_cp1252_pound_sign`: the description bytes `b"Caf\xe9 \xa3"` with `cp1252` give `"Café £"`.
  - `test_wrong_encoding_is_file_error`: cp1252 bytes `b"\xa3"` decoded as `utf-8` raise `FileFormatError`.
  - `test_missing_column_is_file_error`: the message contains the missing column name.
  - `test_bad_row_is_row_error_not_exception`: rows with date `31/02/2026`, amount `"12.3.4"`, amount `"١٢"` and currency `"POUND"` each become a `RowError` with the right position, and the valid rows around them still parse.
  - `test_blank_lines_ignored_and_positions_contiguous`.
  - `test_datetime_dates_with_comma_format`: with the header `Date,Description,Details,Money out (GBP),Money in (GBP),Balance (GBP)`, quoted cells (`"15 Oct, 2026 09:05"` and `"1,000.00"`), `date_format="%d %b, %Y %H:%M"` and `local_tz=Europe/London`, the row has `booking_date == date(2026, 10, 15)` and `occurred_at == datetime(2026, 10, 15, 9, 5, tzinfo=ZoneInfo("Europe/London"))`, and its description joins `Description` and `Details`.
  - `test_include_only_skips_before_parsing`: with header `Type,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance`, `include_only={"State": ["COMPLETED"]}` and `columns.date="Completed Date"`, a `PENDING` row with empty `Completed Date` and `Balance` becomes a `SkippedRow` whose reason names `State` and `PENDING`, not a `RowError`.
  - `test_fee_column`: amount `-10.00` with fee `0.50` gives `quantity == -10.00` and `fee == 0.50`; an empty fee cell gives `fee == 0`.
  - `test_currency_symbol_in_quoted_amounts`: `"£12,345.67"` with `thousands_separator=","` gives `12345.67`.
  - `test_too_many_rows`: `MAX_IMPORT_ROWS + 1` rows raise `FileFormatError` (monkeypatch the limit to 3 to keep the test small).
- [ ] **Step 2: Run** `uv run --locked pytest tests/finance/importing/test_csv_parser.py -q`; expected: FAIL.
- [ ] **Step 3: Implement** `parsed.py` and `csv_parser.py`.
- [ ] **Step 4: Run**; expected: all pass; `make backend-check` green.
- [ ] **Step 5: Commit** `feat: parse CSV statements through profiles`.

### Task 4: OFX parser

**Files:**
- Create: `.../importing/ofx_parser.py`, `backend/tests/finance/importing/test_ofx_parser.py`

**Interfaces:**
- Consumes: `ParsedRow`, `RowError`, `ParsedStatement`, `parse_amount`, `clean_description`, `MAX_IMPORT_ROWS`, `FileFormatError` (Task 3); `OfxProfileConfig` (Task 2).
- Produces: `PARSER_VERSION = "ofx-1"`; `def parse_ofx(content: bytes, config: OfxProfileConfig) -> ParsedStatement`.

Rules:
- **Decoding.** Decode as UTF-8 when the header says `ENCODING:UTF-8` or the XML declaration says `encoding="UTF-8"`, otherwise as cp1252. Undecodable bytes raise `FileFormatError`.
- **Tokenizing.** Tokenize everything after the header with `<(/?)([A-Za-z0-9.]+)>([^<]*)`. A leaf's value is the stripped text after its opening tag, with `html.unescape` applied. Aggregates (`STMTTRN`, `STMTRS`, `CCSTMTRS`, `BANKTRANLIST`) are closed in both OFX 1.x and 2.x.
- **One statement per file.** Exactly one `STMTRS` or `CCSTMTRS` is allowed; zero or several raise `FileFormatError` ("export one account per file").
- **Currency.** Each row's currency is the statement's `CURDEF`, overridden by a transaction-level `CURRENCY/CURSYM` or `ORIGCURRENCY/CURSYM` when present.
- **Fields of each `STMTTRN`.** `DTPOSTED` is required. `TRNAMT` is required and parsed with `parse_amount` using `AmountFormat()`. `FITID` becomes `external_id` (`None` when missing). The description is `clean_description([NAME, MEMO])`, skipping missing parts. `raw` holds every leaf of the transaction.
- **`DTPOSTED`.** The format is `YYYYMMDD[HHMMSS[.XXX]][[±H[H][.MM]][:TZ]]`. `booking_date` comes from the first 8 characters. `occurred_at` is `None` without a time part; with one, it is aware, using the bracketed offset or UTC when there is none. A malformed value is a `RowError`.
- **Signs.** `TRNAMT` is already from the account holder's view, so `inflow_positive` uses it as written and `outflow_positive` negates it.
- **Fees and balances.** Rows have `fee = 0` and `balance = None`.
- **Closing balance.** `LEDGERBAL/BALAMT` becomes `closing_balance` (same sign rule), and the date of `LEDGERBAL/DTASOF` becomes `closing_balance_date`. When it is absent, both are `None`.

- [ ] **Step 1: Write the failing tests** with two synthetic files written in the test module (Example Bank, fake FITIDs):
  - `test_sgml_v1_statement`: OFX 1.02 SGML with unclosed leaf tags, `CURDEF GBP` and two `STMTTRN`. It gives two rows with the right quantities, external IDs, `currency == "GBP"`, and `occurred_at is None` for `DTPOSTED 20261110`.
  - `test_xml_v2_statement`: OFX 2.2 XML with closing tags. It gives the same rows.
  - `test_dtposted_with_offset_keeps_written_date`: `20261110233000[-5:EST]` gives `booking_date == date(2026, 11, 10)` and `occurred_at == datetime(2026, 11, 11, 4, 30, tzinfo=UTC)`.
  - `test_entities_unescaped`: `<NAME>M&amp;S` gives the description `"M&S"`.
  - `test_credit_card_statement`: `CCSTMTRS` with a purchase `-25.00` and a payment `100.00` keeps those signs.
  - `test_ledgerbal_is_closing_balance`: `<LEDGERBAL><BALAMT>1234.56<DTASOF>20261130` gives `closing_balance == Decimal("1234.56")` and `closing_balance_date == date(2026, 11, 30)`; a file without it gives `None`.
  - `test_multiple_statements_rejected` and `test_no_statement_rejected`: each raises `FileFormatError`.
  - `test_bad_trnamt_is_row_error`.
- [ ] **Step 2: Run**; expected: FAIL.
- [ ] **Step 3: Implement** `ofx_parser.py`.
- [ ] **Step 4: Run**; expected: all pass; `make backend-check` green.
- [ ] **Step 5: Commit** `feat: parse OFX 1.x and 2.x statements`.

### Task 5: Staging

**Files:**
- Create: `backend/migrations/versions/0010_finance_import_batches.py`, `.../importing/staging.py`, `backend/tests/finance/importing/test_staging.py`, `backend/tests/finance/importing/conftest.py`
- Modify: `.../finance/models.py`, `.../finance/errors.py`

**Interfaces:**
- Consumes:
  - from Task 1: `store_document`, `DocumentStore`;
  - from Task 2: `get_profile`;
  - from Tasks 3 and 4: `parse_csv`, `parse_ofx`, `PARSER_VERSION`s;
  - from plan 1: `accounts.get_account`, `instruments.get_currency`, `instruments.get_instrument`, `get_timezone`, `to_plain_str`.
- Produces (in `staging.py`):
  - `BatchStatus = Literal["staged", "committed", "discarded", "rolled_back"]`.
  - `RowStatus = Literal["new", "duplicate", "skipped", "error", "committed", "rolled_back"]`.
  - `RowKind = Literal["statement", "opening_adjustment"]`.
  - `@dataclass(frozen=True) class ImportRowRecord`: `id, position, kind: RowKind, status: RowStatus, booking_date: date | None, occurred_at: datetime | None, description: str, quantity: Decimal | None, fee: Decimal | None, instrument_id: UUID | None, balance: Decimal | None, external_id: str | None, error: str | None, note: str | None, raw: dict[str, str], transaction_id: UUID | None`.
  - `@dataclass(frozen=True) class ImportBatchRecord`: `id, account_id, profile_id, profile_version: int, document_id, filename: str, status: BatchStatus, created_at, committed_at: datetime | None, rolled_back_at: datetime | None, counts: dict[RowStatus, int]` (every status present, zeros included), `warnings: list[str]`.
  - `OPENING_SETTING = "finance.opening_balance_account_id"`. `async def get_or_create_system_account(ctx, setting: str, name: str) -> UUID` creates or reuses an `equity`/`other`/`transactions` account remembered in that setting, and creates a new one if the setting points at a missing account. Task 6 uses it for both system accounts (Decisions 1 and 13).
  - `async def ledger_balance_before(session, account_id: UUID, instrument_id: UUID, day: date) -> Decimal`: the sum of the account's postings in that instrument whose transaction is not void and is booked before `day`.
  - `def opening_adjustments(rows: list[ParsedRow], instrument_of: Callable[[ParsedRow], UUID], closing: tuple[Decimal, date] | None, row_order: str) -> dict[UUID, tuple[Decimal, date]]`: a pure function returning the statement's opening balance and first date per instrument (Decision 13). Instruments without a balance or closing balance are absent.
  - `def check_balance_chain(rows: list[ParsedRow], instrument_of: Callable[[ParsedRow], UUID], row_order: str) -> dict[int, str]`: pure; maps position → error message for chain breaks (Decision 12). Callers pass only importable rows, with `None` standing in for a parse error so the chain resets.
  - `def dedupe_key(account_id: UUID, row: ParsedRow, currency: str, occurrence: int) -> str`: per Decision 5.
  - `async def stage_import(ctx, store: DocumentStore, *, account_id: UUID, profile_id: UUID, content: bytes, filename: str, media_type: str, default_instrument_id: UUID | None) -> ImportBatchRecord`
  - `async def get_batch(session, batch_id) -> ImportBatchRecord`; `async def list_batches(session) -> list[ImportBatchRecord]` (newest first); `async def list_batch_rows(session, batch_id) -> list[ImportRowRecord]` (by position).
  - Errors: `ImportNotAllowedError` (account not open, not manual, or balance mode `snapshots`), `ImportBatchNotFoundError`.

Migration `0010`:
- `finance.import_batches`. Columns: `id`; `account_id` FK; `profile_id` FK; `profile_version int`; `profile_config jsonb` (a snapshot of the profile at upload); `parser_version text`; `document_id` FK `core.documents`; `filename text`; `default_instrument_id` FK NULL; `status text CHECK IN (...)`; `warnings jsonb NOT NULL DEFAULT '[]'`; `owner_entity_id`; `created_at`; `committed_at NULL`; `rolled_back_at NULL`. CHECKs tie `committed_at` to statuses `committed`/`rolled_back` and `rolled_back_at` to `rolled_back`.
- `finance.import_rows`. Columns: `id`; `batch_id` FK `ON DELETE CASCADE`; `account_id` FK (denormalised for the index); `position int`; `kind text NOT NULL DEFAULT 'statement' CHECK IN ('statement','opening_adjustment')`; `status text CHECK IN (...)`; `booking_date date`; `occurred_at timestamptz`; `occurred_tz text`; `description text NOT NULL DEFAULT ''`; `quantity numeric`; `fee numeric NOT NULL DEFAULT 0`; `instrument_id` FK; `balance numeric`; `external_id text`; `dedupe_key text`; `error text`; `note text`; `raw jsonb NOT NULL`; `transaction_id` FK `finance.transactions` NULL.
  - Constraints:
    - `UNIQUE (batch_id, position)`;
    - `CHECK ((status = 'error') = (error IS NOT NULL))`;
    - a CHECK that rows in `new`, `duplicate`, `committed` and `rolled_back` have `booking_date`, `occurred_at`, `quantity`, `instrument_id` and `dedupe_key`; skipped rows may lack them;
    - `CHECK ((kind = 'opening_adjustment') = (position < 0))`;
    - `CHECK ((status IN ('committed','rolled_back')) = (transaction_id IS NOT NULL))`;
    - NaN/Infinity CHECKs on `quantity`, `fee` and `balance`, as in migration 0005.
  - Index: `CREATE UNIQUE INDEX import_rows_committed_once ON finance.import_rows (account_id, dedupe_key) WHERE status = 'committed'`.

Rules for `stage_import`:
1. **Account check.** The account must be open, `source_kind == "manual"`, and in balance mode `transactions` or `hybrid`; otherwise `ImportNotAllowedError` with a message saying why.
2. **Default currency.** `default_instrument_id`, when given, must be a fiat instrument, else `NotACurrencyError`.
3. **Store the document.** `store_document(ctx, store, content, filename=..., media_type=..., source="finance.import")`.
4. **Parse** by `profile.config.format`, with `local_tz` = `ZoneInfo(profile timezone)` or `get_timezone(session)`. `FileFormatError` propagates, and `command_context` rolls back everything (the stored file may remain; it is content-addressed and harmless).
5. **Classify each row:**
   - a `SkippedRow` becomes `skipped`, with its reason in `note`;
   - a `RowError` becomes `error`;
   - otherwise resolve the currency: the row's code via `get_currency`, else the default instrument's code. An unknown code, or no currency at all, becomes an `error` with a message telling the user to pick a currency at upload or map a currency column;
   - `quantity == 0` and `fee == 0` becomes `skipped`, with the note "zero amount";
   - otherwise compute the key, with occurrence counting per Decision 5;
   - the row is `duplicate` if a committed row on the account has that key or an earlier row of this batch does; else `new`.
6. **Balance chain.** Run `check_balance_chain` over the rows still `new` or `duplicate`, passing parse errors as resets. Each reported position becomes `error` with that message (Decision 12).
7. **Opening adjustment.** For `transactions` accounts only, and only when the batch has no `error` rows, compare `opening_adjustments(...)` with `ledger_balance_before(...)` per instrument. Each difference adds a row per Decision 13: `kind='opening_adjustment'`, status `new`, and `dedupe_key = sha256("open", account, date, quantity, currency)`. `position` is `-1, -2, …` in instrument-symbol order. When balances are unavailable, add the batch warning from Decision 13 instead.
8. **Times.** `occurred_at` is the row's value, or local midnight of `booking_date` in the local zone (step 4), with `occurred_tz` set to the zone name.
9. **Event.** Emit `finance.import.staged` with the batch id and counts.

Add a `store` fixture (`LocalDocumentStore(tmp_path)`) and a `csv_profile` fixture (an Example Bank CSV profile with `Date,Description,Amount` and `%d/%m/%Y`) in `tests/finance/importing/conftest.py`.

- [ ] **Step 1: Write the failing tests:**
  - `test_stage_counts_and_rows`: a 3-row file (one valid, one bad date, one zero amount) gives counts `new 1, error 1, skipped 1`; rows in position order; the error row's message mentions the date.
  - `test_identical_rows_in_one_file_are_both_new`: two identical `10/11/2026,Coffee,-3.50` rows both give `new`, with different `dedupe_key`s.
  - `test_external_id_duplicate_within_file`: an OFX file with the same FITID twice gives `new` then `duplicate`.
  - `test_snapshots_account_refused`, `test_closed_account_refused`: each raises `ImportNotAllowedError`.
  - `test_file_format_error_persists_nothing`: a bad header raises `FileFormatError`, and no batch or document row remains.
  - `test_currency_resolution`: with no currency column and no default, every row is an `error`; with default GBP, rows are `new` with the GBP id; a currency column with `XYZ` makes that row an `error`.
  - `test_date_only_rows_keep_booking_date_across_dst`: rows dated `29/03/2026` and `25/10/2026` (the BST changes) with `core.timezone` `Europe/London` keep those booking dates, and `occurred_at` is local midnight (`2026-03-29T00:00Z` and `2026-10-24T23:00Z`).
  - `test_batch_snapshots_profile`: updating the profile after staging leaves the batch's `profile_version` and `profile_config` unchanged.
  - `test_balance_chain_break_is_error`: in a three-row file with balances 100.00, 90.00 and 95.00 and amounts +100.00, −10.00 and +10.00, the third row is an `error` whose message contains `expected 100.00` and `95.00`.
  - `test_balance_chain_catches_wrong_sign`: a correct file staged with `sign="outflow_positive"` makes every row after the first an `error`.
  - `test_balance_chain_ignores_skipped_rows`: a filtered `PENDING` row between two chained rows does not break the chain.
  - `test_newest_first_chain`: the same rows in reverse with `row_order="newest_first"` raise no errors.
  - `test_opening_adjustment_from_first_balance`: a first import whose first row is `-18.00` with balance `1482.00` adds one `opening_adjustment` row of `1500.00`, dated the day before the first row.
  - `test_no_adjustment_when_ledger_matches`: after committing the opening batch, staging the next month's statement (which starts at the previous closing balance) adds no adjustment.
  - `test_gap_creates_adjustment`: staging a statement that starts higher than the ledger, because a month was skipped, adds an adjustment for the difference.
  - `test_hybrid_account_has_no_adjustment` and `test_no_balances_adds_warning`.
  - `test_opening_from_ofx_ledgerbal`: an OFX file with `LEDGERBAL` and no running balances derives the opening balance from it.
  - `test_multi_currency_chains_separately`: a file with a currency column, where GBP and EUR rows each carry their own balance, raises no chain error and adds one adjustment per currency.
- [ ] **Step 2: Run**; expected: FAIL.
- [ ] **Step 3: Implement** the migration, the ORM models and `staging.py`. Keep `check_balance_chain` and `opening_adjustments` pure, and test them through `stage_import`.
- [ ] **Step 4: Run**; expected: all pass; `make backend-check` green.
- [ ] **Step 5: Commit** `feat: stage imported statements with deduplication`.

### Task 6: Commit, discard, roll back

**Files:**
- Create: `.../importing/commit.py`, `backend/tests/finance/importing/test_commit.py`
- Modify: `.../finance/errors.py`

**Interfaces:**
- Consumes:
  - from Task 5: everything;
  - from plan 1: `ledger.post_transaction`, `ledger.void_transaction`, `TransactionInput`, `PostingInput`, `accounts.create_account`, `get_setting`/`set_setting`, `valuation.balances_asof`.
- Produces:
  - `CONTRA_SETTING = "finance.import_contra_account_id"`. The contra account comes from `get_or_create_system_account(ctx, CONTRA_SETTING, "Unassigned (imports)")` (Decision 1), and the opening account from `get_or_create_system_account(ctx, OPENING_SETTING, "Opening balances")` (Decision 13).
  - `async def commit_batch(ctx, batch_id: UUID) -> ImportBatchRecord`
  - `async def discard_batch(ctx, batch_id: UUID) -> ImportBatchRecord`
  - `async def rollback_batch(ctx, batch_id: UUID) -> ImportBatchRecord`
  - Errors: `ImportBatchStateError` (wrong status for the action), `ImportHasErrorsError`, `ImportBatchStaleError` (the ledger moved since staging, Decision 13).

Rules:
- Every action first locks the batch row with `SELECT … FOR UPDATE`.
- **`commit_batch`:**
  - The status must be `staged`.
  - There must be no `error` rows.
  - The account is re-checked with the same rule as staging, since its mode may have changed since.
  - For `transactions` accounts, the expected adjustment per instrument is recomputed with `opening_adjustments` over the batch's statement rows and `ledger_balance_before`. If it differs from the staged adjustment rows (including one appearing or disappearing), raise `ImportBatchStaleError("the account changed since this file was uploaded; discard it and upload again")`.
  - Then, for each `new` row by position (adjustment rows first, since their positions are negative):
    - if a committed row with the same `(account_id, dedupe_key)` now exists, the row becomes `duplicate`;
    - otherwise `post_transaction` with `occurred_at`, `occurred_tz`, `booking_date` and `description`, all postings in the row's instrument:
      - a statement row without a fee: the account `+quantity` and the contra account `−quantity`;
      - a statement row with a fee (Decision 11): the account `+quantity`, the account `−fee` with `is_fee=True`, and the contra account `−(quantity − fee)`;
      - an adjustment row: the account `+quantity` and the opening account `−quantity`.

      The row becomes `committed` with its `transaction_id`.
  - Finally the batch becomes `committed` with `committed_at = now()`, and the action emits `finance.import.committed` with counts.
- **`discard_batch`:** `staged` → `discarded`.
- **`rollback_batch`:**
  - The status must be `committed`.
  - For each `committed` row, void its transaction unless it is already void; the row becomes `rolled_back`.
  - The batch becomes `rolled_back` with `rolled_back_at`, and the action emits `finance.import.rolled_back`.

- [ ] **Step 1: Write the failing tests** (stage through `stage_import` with the Task 5 fixtures, then act):
  - `test_commit_posts_balanced_transactions`: after committing a 2-row file, `balances_asof` shows the account at the sum of the rows, the contra account holds the negation, and net worth changes by the account's sum only.
  - `test_reimporting_same_file_marks_all_duplicate`: commit, then stage the same bytes again: all rows are `duplicate`, and committing the second batch posts nothing.
  - `test_overlapping_export_commits_only_new_rows`: commit rows for 1–10 Nov, then stage an export for 5–15 Nov: the overlapping rows are `duplicate`, the rest `new`.
  - `test_commit_rechecks_duplicates`: stage the same file as batches A and B, commit A, then commit B: B's rows end `duplicate` without an error.
  - `test_errors_block_commit`: raises `ImportHasErrorsError`, and the batch stays `staged`.
  - `test_commit_twice_refused`: raises `ImportBatchStateError`.
  - `test_commit_refused_after_account_switched_to_snapshots`: raises `ImportNotAllowedError`.
  - `test_rollback_voids_and_allows_reimport`: after rollback the account balance is back to its previous value, the rows are `rolled_back`, and staging the same file again gives `new` rows.
  - `test_rollback_skips_already_void_transactions`: void one transaction by hand first; rollback succeeds and emits no second voided event for it.
  - `test_liability_import_signs`: on a liability account, an `inflow`/`outflow` file with a purchase (outflow 25.00) and a payment (inflow 100.00) gives quantities `-25.00` and `100.00`.
  - `test_contra_account_reused`: two committed batches use the same contra account id.
  - `test_fee_posting`: a row with amount `-10.00` and fee `0.50` posts three postings, the fee one has `is_fee`, and the account's balance moves by `-10.50`.
  - `test_opening_adjustment_committed_and_rolled_back`: committing a first import leaves the account at the file's closing balance; the "Opening balances" account holds the negated opening amount; net worth equals the closing balance. Rolling back voids the adjustment too.
  - `test_stale_batch_refused`: stage month 2 (which needs an adjustment because month 1 is missing), then stage and commit month 1, then commit month 2. It raises `ImportBatchStaleError`, and re-staging month 2 gives no adjustment.
  - `test_pending_then_completed_counted_once`: an export where a payment is `PENDING` (skipped), committed; then a later export where the same payment is `COMPLETED`. The second batch has it as `new`, and the account counts it exactly once.
  - `test_discard`: staged → discarded; committing it afterwards raises `ImportBatchStateError`.
- [ ] **Step 2: Run**; expected: FAIL.
- [ ] **Step 3: Implement** `commit.py`.
- [ ] **Step 4: Run**; expected: all pass; `make backend-check` green.
- [ ] **Step 5: Commit** `feat: commit, discard and roll back import batches`.

### Task 7: REST API and example files

**Files:**
- Create:
  - the example files `.../importing/examples/example-bank-csv.json`, `example-bank.csv`, `example-bank-ofx.json`, `example-bank.ofx`;
  - the test `backend/tests/api/test_import_api.py`.
- Modify: `backend/pyproject.toml` + `uv.lock` (add `python-multipart`, Apache-2.0), `backend/src/lararium/api/app.py`, `backend/src/lararium/deps.py`, `.../finance/schemas.py`, `.../finance/api.py`, `web/openapi.json`, `web/src/api/*` (regenerated).

**Interfaces:**
- Consumes: Tasks 1–6.
- Produces:
  - **Wiring.**
    - In `create_app`: `app.state.document_store = LocalDocumentStore(settings.document_dir)`.
    - In `deps.py`: `def get_document_store(request: Request) -> DocumentStore` and `DocumentStoreDep`.
    - In the API test `conftest`: point `LARARIUM_DOCUMENT_DIR` (or the `Settings` object) at `tmp_path`.
  - **Schemas.** Amounts use `DecimalStr`.
    - `ImportProfileCreate { name: str (1–100 chars), config: ProfileConfig }`.
    - `ImportProfileOut { id, name, config, version, updated_at }`.
    - `ImportBatchOut { id, account_id, profile_id, profile_version, document_id, filename, status, created_at, committed_at, rolled_back_at, counts: dict[str, int], warnings: list[str] }`.
    - `ImportRowOut { id, position, kind, status, booking_date, description, quantity: DecimalStr | None, fee: DecimalStr | None, instrument_id, balance: DecimalStr | None, external_id, error, note, raw: dict[str, str], transaction_id }`.
    - `ImportBatchDetailOut(ImportBatchOut) { rows: list[ImportRowOut] }`.
  - **Routes.** The route name is the operationId:

    | Method and path | Name | Response |
    |---|---|---|
    | `GET /import-profiles` | `list_import_profiles` | |
    | `POST /import-profiles` | `create_import_profile` | 201 |
    | `PUT /import-profiles/{profile_id}` | `update_import_profile` | |
    | `POST /imports` | `stage_import` | 201 → `ImportBatchDetailOut` |
    | `GET /imports` | `list_import_batches` | |
    | `GET /imports/{batch_id}` | `get_import_batch` | `ImportBatchDetailOut` |
    | `POST /imports/{batch_id}/commit` | `commit_import` | |
    | `POST /imports/{batch_id}/discard` | `discard_import` | |
    | `POST /imports/{batch_id}/rollback` | `rollback_import` | |

    `stage_import` takes multipart form fields `account_id`, `profile_id`, optional `instrument_id`, and `file`. Its audit args are those ids plus `filename`, `size_bytes` and `sha256`, never the content.
  - **Upload limit.** The route reads the upload in 64 KiB chunks and stops beyond `MAX_IMPORT_BYTES` with 413 `{"error": "file_too_large"}`. An empty file is 422 `file_format`.
  - **Error map.**

    | Error | Status | Code |
    |---|---|---|
    | `ImportProfileNotFoundError` | 404 | `import_profile_not_found` |
    | `ImportBatchNotFoundError` | 404 | `import_batch_not_found` |
    | `DuplicateProfileNameError` | 409 | `duplicate_profile_name` |
    | `ImportNotAllowedError` | 422 | `import_not_allowed` |
    | `FileFormatError` | 422 | `file_format` |
    | `ImportBatchStateError` | 409 | `import_batch_state` |
    | `ImportHasErrorsError` | 409 | `import_has_errors` |
    | `ImportBatchStaleError` | 409 | `import_batch_stale` |

  - **Example files.** All synthetic, for "Example Bank".
    - `example-bank.csv`: `dd/mm/yyyy` dates, `Paid in`/`Paid out`/`Balance` columns (the running balance starts from an opening balance of 1,000.00, so a first import adds an opening adjustment), a thousands separator, a zero-amount row and two identical purchases on one day, about 12 rows.
    - `example-bank-csv.json`: its profile.
    - `example-bank.ofx`: OFX 1.x SGML, GBP, 5 transactions with FITIDs.
    - `example-bank-ofx.json`: `{"format": "ofx"}`.

- [ ] **Step 1: Write the failing tests:**
  - `test_example_csv_end_to_end`: create the profile from `example-bank-csv.json`, create a manual `transactions` account, and upload `example-bank.csv` with GBP: 201, `counts.new` equals the number of non-zero rows, and `counts.skipped == 1`. Then commit: holdings show the account at the file's closing `Balance`. Upload again: all rows are `duplicate`.
  - `test_example_ofx_end_to_end`: the same flow with the OFX example.
  - `test_upload_too_large_is_413`: `MAX_IMPORT_BYTES + 1` bytes.
  - `test_upload_to_snapshots_account_is_422`: code `import_not_allowed`.
  - `test_profile_config_validation_is_422` and `test_duplicate_profile_name_is_409`.
  - `test_cross_site_upload_refused`: a multipart POST with `Sec-Fetch-Site: cross-site` gives 403.
  - `test_import_operation_ids`: the nine names above appear in `render_openapi()`.
  - `test_rollback_via_api`: commit, then roll back: status `rolled_back`, and holdings are back to zero.
- [ ] **Step 2: Run** `uv run --locked pytest tests/api/test_import_api.py -q`; expected: FAIL.
- [ ] **Step 3: Implement** the dependency (`uv add python-multipart`), the wiring, schemas, routes, error map and example files. Then run `make openapi`.
- [ ] **Step 4: Run** `make check`; expected: green, with `web/openapi.json` and `web/src/api` regenerated and committed.
- [ ] **Step 5: Commit** `feat: add import REST API with Example Bank samples`.

### Task 8: Web UI

**Files:**
- Create: `web/src/pages/ImportsPage.tsx`, `ImportBatchPage.tsx`, `ImportProfilesPage.tsx`, and a `.test.tsx` for each.
- Modify: `web/src/router.tsx`.

**Interfaces:**
- Consumes: the generated client functions `listImportProfiles`, `createImportProfile`, `updateImportProfile`, `stageImport`, `listImportBatches`, `getImportBatch`, `commitImport`, `discardImport`, `rollbackImport`, `listAccounts`, `listInstruments`, `getFinanceSettings`; `formatMoney`/`formatQuantity` from `lib/format.ts`.
- Produces:
  - routes `/imports`, `/imports/$batchId`, `/import-profiles`;
  - nav entries "Imports" and "Profiles", after "Prices".

Behaviour:
- **`/imports` (ImportsPage).**
  - **Upload form:**
    - "Account": only open manual accounts in mode `transactions` or `hybrid`, with a note when none exist;
    - "Profile";
    - "Currency": fiat instruments, defaulting to the reporting currency, with an option "From the file" for profiles that map a currency or for OFX;
    - "File".
  - **Submitting** sends `stageImport` with the form data. On success it navigates to the batch page; on failure it shows the server `message`.
  - **Below the form**, a table of batches: filename, account, status, counts, created time.
- **`/imports/$batchId` (ImportBatchPage).**
  - **Header:** counts per status, and the batch `warnings`.
  - **Adjustment rows** are shown first, labelled "Opening balance adjustment". On an account that already has imported history, the label reads "Balance gap: a statement period may be missing".
  - **Row table:** position, date, description, amount via `formatMoney(quantity, code)` where `code` is the row instrument's ISO code, status badge, the error message on error rows, and the note on skipped rows.
  - **When the batch is `staged`:**
    - "Commit" is disabled while `counts.error > 0`, with the text "Fix the errors in the file or profile and upload again";
    - "Discard" is available.
  - **When it is `committed`:** "Roll back" asks for confirmation first ("This voids N transactions").
  - **Queries:** after any action, invalidate `["imports"]`, `["import-batch", id]`, `["holdings"]` and `["net-worth"]`.
- **`/import-profiles` (ImportProfilesPage).** A list of profiles, and a create/edit form with "Name" and a monospace "Config (JSON)" textarea whose placeholder is a minimal CSV config. Before sending, `JSON.parse` the config; a parse error shows "Config is not valid JSON" without sending. A server 422 shows its message.
- **No `Number(...)` or `parseFloat` on amounts anywhere.**

- [ ] **Step 1: Write the failing tests** (mock `../api` as the existing page tests do):
  - `ImportsPage`: `test_upload_sends_form_fields`: choosing an account, profile and file, then submitting, calls `stageImport` with `body` containing `account_id`, `profile_id`, `instrument_id` (GBP) and the `File`. `test_only_importable_accounts_listed`: a `snapshots` account and a closed account are not offered.
  - `ImportBatchPage`: `test_commit_disabled_with_errors`; `test_rollback_requires_confirmation`: `rollbackImport` is not called until the confirm button is clicked; `test_amounts_rendered_from_strings`: quantity `"-1234.56"` renders as `-£1,234.56`.
  - `ImportProfilesPage`: `test_invalid_json_not_sent`; `test_server_validation_message_shown`.
- [ ] **Step 2: Run** `cd web && pnpm test --run`; expected: FAIL.
- [ ] **Step 3: Implement** the three pages and the routes, reusing `LabelledSelect`, `TextField` and the `ui/` components.
- [ ] **Step 4: Run** `make check`; expected: green.
- [ ] **Step 5: Commit** `feat: add import and profile pages to the web UI`.

### Task 9: Deployment, backups and docs

**Files:**
- Modify: `backend/Dockerfile`, `infra/compose.yaml`, `infra/backup/backup.sh`, `backend/tests/infra/test_compose.py`, `backend/tests/infra/test_backup.py`, `README.md`, `README.zh-CN.md`.

**Interfaces:**
- Consumes: `Settings.document_dir` (Task 1).
- Produces: the named volume `documents` mounted into `api` at `/var/lib/lararium/documents`, and a backup output `documents-<UTC timestamp>.tar` next to each `.dump`.

Changes:
- **`backend/Dockerfile`.** Before `USER lararium`, run `mkdir -p /var/lib/lararium/documents && chown lararium /var/lib/lararium/documents`. A new named volume copies this ownership on first mount.
- **`compose.yaml`.**
  - `api` gets `volumes: [documents:/var/lib/lararium/documents]` and `LARARIUM_DOCUMENT_DIR: /var/lib/lararium/documents`.
  - Add `documents: {}` under `volumes`.
  - `migrate` does not mount it.
- **`backup.sh`.**
  - After the dump, write `"${compose[@]}" exec -T api tar -C /var/lib/lararium/documents -cf - . > "$out_docs.partial"` and then `mv` it into place, using the same timestamp as the dump.
  - restic backs up both.
  - A missing `api` container fails the backup loudly rather than writing a dump without documents.
- **README, both languages:**
  - A section "Importing statements":
    - the flow (upload → review → commit, roll back);
    - which accounts accept imports (Decision 3);
    - the profile format, with every key from Task 2;
    - the inflow/outflow sign rule, with the credit-card example;
    - how duplicates are recognised (Decision 5);
    - the "Unassigned (imports)" account (Decision 1);
    - where the Example Bank files are.
  - In the backup section: documents are backed up as a tar alongside the dump, and restoring means untarring into the `lararium_documents` volume (give the one-line `docker run --rm -v lararium_documents:/d -i alpine tar -C /d -xf - < documents-….tar`).

- [ ] **Step 1: Write the failing tests:**
  - `test_compose.py`: `api` mounts `documents` at `/var/lib/lararium/documents` and sets `LARARIUM_DOCUMENT_DIR`; `migrate` and `web` do not mount it.
  - `test_backup.py`: the script references `/var/lib/lararium/documents`, writes a `.partial` before `mv`, and keeps `umask 077` before both outputs.
- [ ] **Step 2: Run**; expected: FAIL.
- [ ] **Step 3: Implement**; write both READMEs (the Chinese one translated from the English).
- [ ] **Step 4: Verify.**
  - Run `make check`; expected: green.
  - Then run the compose smoke test:
    1. `make up` on a fresh volume set.
    2. As the owner over `127.0.0.1:8080`, create the example profile and a manual `transactions` account.
    3. Upload `example-bank.csv` and commit it.
    4. Run `make backup`. Expected: both the `.dump` and the `documents-….tar` exist, and the tar lists one file.
  - Finally `make down`.
- [ ] **Step 5: Commit** `feat: persist and back up import documents`.

---

## Self-review notes

- **Spec coverage, §7.8.**
  - File stored as a document: Task 1, Task 5.
  - Per-institution profiles stored as data: Task 2.
  - Generic CSV mapper: Task 3.
  - OFX: Task 4.
  - Staging that shows new, duplicate and error rows: Tasks 5 and 8.
  - Commit to the ledger: Task 6.
  - Batch rollback: Tasks 6, 7 and 8.
  - Example profiles with synthetic files only: Task 7.
  - Liabilities use the same flow: Task 6 `test_liability_import_signs`.
  - Fintech multi-currency exports: a profile currency column, Tasks 3 and 5.
  - Pairing with inferred postings is deferred to M4 (stated above).
- **Spec coverage, §6.2 documents.** Content hash, media type, size, original filename and source, a local directory behind a swappable interface: Task 1.
- **Spec coverage, §6.4 deduplication.** Decision 5, and Tasks 5 and 6.
- **Types.** `ParsedRow`, `RowError` and `ParseResult` are defined once (Task 3) and used by Tasks 4 and 5. Record types are defined in Task 5 and returned by Task 6. API names match the Task 8 client names, since hey-api camel-cases the operationIds.
