# Foundation and Ledger Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the skeleton and ledger core of Lararium: create accounts by hand, enter balance snapshots and holding quantities, record double-entry transactions, enter prices and FX rates, and see net worth for any day in the web UI.

**Architecture:** Python modular monolith: `lararium.core` is the shared layer, `lararium.modules.finance` is the finance module, and `lararium.api` mounts the modules onto FastAPI. All data lives in PostgreSQL 18. Valuation logic is written as versioned SQL functions, shared by the UI and, later, by BI and MCP. The frontend is a React + Vite SPA, with an API client generated from OpenAPI.

**Tech Stack:** Python 3.13, uv, FastAPI, Pydantic v2, SQLAlchemy 2 (async, psycopg 3), Alembic, pytest + pytest-asyncio + testcontainers; PostgreSQL 18; React, Vite, TanStack Router / Query, shadcn/ui, ECharts, @hey-api/openapi-ts, Vitest + Testing Library; Docker Compose, Caddy.

**Spec:** `docs/superpowers/specs/2026-11-11-financial-gate-phase1-design.md` (this plan covers its M0 and M1)

**Plan series:** Phase 1 is split by milestone into several plans; each one, once done, yields software that runs and is tested. Each plan is written only after the previous one has landed, so it can build on the interfaces in real code:

1. **Foundation and ledger core** (this plan: M0, M1)
2. Manual import (M2)
3. Connector framework and Monzo (M3)
4. Transfer matching (M4)
5. Investment and crypto sources (M5–M8: IBKR, Binance, self-custody wallets, brokers)
6. Operations wrap-up (M9)

Of the spec's shared objects, this plan builds only the ones the ledger core needs: entities, settings, events and the audit log. Documents, links, connections, raw records, the webhook inbox and credentials wait for the plans that use them (2, 3); the same goes for the two database roles `webhook_in` and `mcp_ro`.

## Global Constraints

- Python 3.13+; dependencies managed with uv; ruff for linting and formatting; pyright in strict mode on `src/`.
- PostgreSQL 18+, using `uuidv7()`, `NUMERIC`, `JSONB`, `LISTEN/NOTIFY`.
- Amounts and quantities: unbounded-precision `NUMERIC` in the database, `Decimal` in Python, strings in JSON. No floating point anywhere along the chain: `float` never appears in source, and the database has no `real` or `double precision` columns. `test_no_float_in_source` also scans comments and docstrings, so don't write that word anywhere in `src/`.
- All IDs are UUIDv7 (database default `uuidv7()`).
- Machine timestamps are stored as UTC `timestamptz`; dates are stored separately as `date`; `core.timezone` defaults to `Europe/London`.
- The reporting currency defaults to GBP (setting `finance.reporting_currency`).
- Dependency direction: `lararium.core` must not import `lararium.modules` or `lararium.api`; `lararium.modules` must not import `lararium.api`. Enforced by import-linter. When a second module appears, add an independence contract ("modules are independent of each other").
- The system is read-only: no order placement, transfer or withdrawal features.
- This plan opens no public entry point; access is via Tailscale only.
- No Next.js, SQLite, Redis or Kafka.
- Docker images must build on amd64 and arm64.
- Branch name `feat/claude-…`; commit message format `<type>: <summary>`, where type is one of feat, fix, chore, refactor, docs, style, perf, test. Use the `local-signed-git-commit` skill to handle signing when committing.
- Development environment requirements: Docker (with buildx, daemon running), uv, Node 24, pnpm 11, Python 3.13, git.
- Open source: license AGPL-3.0-or-later (stated in both `pyproject.toml` and `web/package.json`). No real personal data in the repository: all test data is hand-written and synthetic, and no credentials are built into the code; personal notes go in `private/`, which is not tracked by git. Confirm that any new dependency's license is compatible with AGPL-3.0.
- Docs are in English. Only the README also has a Chinese translation, `README.zh-CN.md`. UI copy is English.

## Review Focus

The spec implicitly requires correct handling of these five kinds of input or situation, but none of them is hit by any task's main-path tests, so they are the easiest places for users to get caught out. Each one already has a test added to the task responsible for it:

- **UK summer time boundary**: a snapshot recorded at 2026-03-31 23:30 UTC belongs to 1 April in London time (Task 7: `test_snapshot_as_of_date_uses_reporting_timezone`).
- **Several snapshots on one day**: when a balance is entered twice on the same day, the latest one that day is used (Task 8: `test_same_day_snapshots_latest_time_wins`).
- **Currency with no FX rate** (e.g. NGN): flagged as missing an FX rate rather than counted as 0 in net worth, and net worth reports the number of missing items (Task 8: `test_missing_fx_is_flagged_not_zero`).
- **Client sends amounts as JSON numbers**: returns 422 instead of silently going through floating point (Task 9: `test_amounts_must_be_strings`).
- **Extreme precision**: a quantity with 12 integer digits and 18 decimal places round-trips through the database and the API unchanged (Task 9: `test_extreme_precision_round_trip`).

---

## File Structure

```
lararium/
├── Makefile                      # check / backend-check / openapi / web-check / up
├── README.md                     # how to run it locally and on the server
├── README.zh-CN.md               # Chinese translation of README.md
├── .gitignore
├── .github/workflows/ci.yml
├── infra/
│   ├── compose.yaml              # db, migrate, api, web
│   ├── env.example
│   ├── caddy/Caddyfile
│   ├── postgres/roles.sql        # roles and database, shared by tests and deployment
│   ├── postgres/init/00-roles.sh
│   └── backup/backup.sh, restore.sh
├── backend/
│   ├── pyproject.toml, uv.lock, alembic.ini, Dockerfile
│   ├── migrations/env.py
│   ├── migrations/versions/      # 0001_core_baseline … 0006_finance_valuation_v1
│   ├── src/lararium/
│   │   ├── __init__.py           # __version__
│   │   ├── config.py             # Settings (environment variables prefixed LARARIUM_)
│   │   ├── db.py                 # engine and session factory
│   │   ├── core/                 # shared layer, knows nothing about modules
│   │   │   ├── actors.py         # Actor
│   │   │   ├── json_types.py     # JSONValue (no float)
│   │   │   ├── errors.py
│   │   │   ├── timeutil.py       # local_date
│   │   │   ├── models.py         # ORM mappings for the core tables
│   │   │   ├── commands.py       # command context: audit log + events
│   │   │   ├── settings_store.py
│   │   │   └── entities.py
│   │   ├── modules/
│   │   │   ├── registry.py       # ModuleSpec, MODULES
│   │   │   └── finance/
│   │   │       ├── __init__.py   # MODULE
│   │   │       ├── errors.py, money.py, models.py
│   │   │       ├── instruments.py, accounts.py, ledger.py
│   │   │       ├── snapshots.py, pricing.py, valuation.py
│   │   │       ├── schemas.py    # Pydantic models for the API
│   │   │       └── api.py        # /api/v1/finance routes
│   │   └── api/
│   │       ├── app.py, deps.py, core_routes.py, export_openapi.py
│   └── tests/
│       ├── conftest.py, test_package.py, test_no_floats.py
│       ├── core/, finance/, api/
└── web/
    ├── package.json, pnpm-lock.yaml, vite.config.ts, tsconfig.json
    ├── openapi-ts.config.ts, openapi.json, Dockerfile, index.html
    └── src/
        ├── main.tsx, router.tsx
        ├── api/                  # generated client, do not edit by hand
        ├── lib/format.ts (+ format.test.ts)
        ├── components/ui/…       # shadcn components
        ├── components/NetWorthChart.tsx
        └── pages/NetWorthPage.tsx, AccountsPage.tsx, InstrumentsPage.tsx (+ a .test.tsx for each)
```

---

### Task 1: Repository and backend toolchain

**Files:**
- Create: `backend/pyproject.toml`, `backend/src/lararium/__init__.py`, `backend/src/lararium/core/__init__.py`, `backend/src/lararium/modules/__init__.py`, `backend/src/lararium/api/__init__.py`, `Makefile`
- Test: `backend/tests/test_package.py`, `backend/tests/test_no_floats.py`

**Interfaces:**
- Produces: package `lararium` (`__version__ = "0.1.0"`); command `make backend-check`, which runs `ruff check`, `ruff format --check`, `pyright`, `lint-imports` and `pytest` in that order.

- [ ] **Step 1: Create the branch, and commit the design doc and this plan**

```bash
git switch -c feat/claude-foundation-ledger-core
git add docs/ LICENSE .gitignore
git commit -m "docs: add license, phase-1 design and foundation plan"
```

- [ ] **Step 2: Create `backend/pyproject.toml` and the four empty packages**

Contents of `pyproject.toml`:
- `[project]`: name `lararium`, version `0.1.0`, `requires-python = ">=3.13"`, `license = "AGPL-3.0-or-later"`; dependencies `fastapi`, `uvicorn[standard]`, `pydantic>=2`, `pydantic-settings`, `sqlalchemy[asyncio]>=2.0`, `psycopg[binary]>=3.2`, `alembic`, `tzdata`.
- `[dependency-groups] dev`: `pytest`, `pytest-asyncio`, `httpx`, `testcontainers[postgres]`, `ruff`, `pyright`, `import-linter`.
- The build backend is hatchling, with package directory `src/lararium`.
- `[tool.pytest.ini_options]`: `asyncio_mode = "auto"`, `asyncio_default_fixture_loop_scope = "session"`, `asyncio_default_test_loop_scope = "session"`, `testpaths = ["tests"]`.
- `[tool.ruff]`: `line-length = 100`, `target-version = "py313"`; lint rules select `E, F, I, UP, B, ASYNC, DTZ, RUF` (DTZ forbids datetimes without a timezone, RUF032 forbids constructing a Decimal from a floating-point literal).
- `[tool.pyright]`: `typeCheckingMode = "strict"`, `include = ["src"]`, `pythonVersion = "3.13"`.
- `[tool.importlinter]`: `root_package = "lararium"`, with two forbidden contracts matching the dependency direction in Global Constraints.

`.gitignore` and `LICENSE` (full AGPL-3.0 text) are already at the repository root, so don't create them again; `.gitignore` already includes `private/`, `backups/`, `infra/.env`, `.venv/`, `__pycache__/`, `node_modules/`, `web/dist/`.

- [ ] **Step 3: Write the failing tests**

```python
# backend/tests/test_package.py
import lararium


def test_package_version() -> None:
    assert lararium.__version__ == "0.1.0"
```

```python
# backend/tests/test_no_floats.py
import re
from pathlib import Path

SRC = Path(__file__).parents[1] / "src" / "lararium"


def test_no_float_in_source() -> None:
    offenders = [
        f"{path.relative_to(SRC)}:{lineno}"
        for path in SRC.rglob("*.py")
        for lineno, line in enumerate(path.read_text().splitlines(), start=1)
        if re.search(r"\bfloat\b", line)
    ]
    assert offenders == []
```

- [ ] **Step 4: Run them and confirm they fail**

Run: `cd backend && uv sync && uv run pytest -q`
Expected: FAIL, `AttributeError: module 'lararium' has no attribute '__version__'`

- [ ] **Step 5: Write `__version__ = "0.1.0"` in `src/lararium/__init__.py`; add a `backend-check` target to the root `Makefile`**

`backend-check` runs, in the `backend/` directory and in this order: `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, `uv run lint-imports`, `uv run pytest -q`. For now the `check` target depends only on `backend-check`.

- [ ] **Step 6: Run all checks**

Run: `make backend-check`
Expected: no errors from ruff or pyright; lint-imports shows both contracts KEPT; pytest shows `2 passed`

- [ ] **Step 7: Commit**

```bash
git add Makefile backend/
git commit -m "chore: scaffold backend package and tooling"
```

---

### Task 2: Test database and migration baseline

**Files:**
- Create: `infra/postgres/roles.sql`, `infra/postgres/init/00-roles.sh`
- Create: `backend/src/lararium/config.py`, `backend/src/lararium/db.py`
- Create: `backend/alembic.ini`, `backend/migrations/env.py`, `backend/migrations/versions/0001_core_baseline.py`
- Test: `backend/tests/conftest.py`, `backend/tests/core/test_baseline.py`

**Interfaces:**
- Produces:
  - `class Settings(BaseSettings)` (`env_prefix="LARARIUM_"`): `database_url: str`, `auth_mode: Literal["dev", "tailscale"] = "tailscale"`, `owner_login: str | None = None` (a model validator rejects tailscale mode without `owner_login`; tests and OpenAPI export pass `auth_mode="dev"` explicitly); `def get_settings() -> Settings` (with `lru_cache`).
  - `def create_engine(url: str) -> AsyncEngine`; `def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]` (`expire_on_commit=False`).
  - Database roles: `lararium_owner` (owns the schemas, used only to run migrations), `lararium_backend` (used by the application, with only select/insert/update/delete privileges). Database name `lararium`.
  - Test fixtures: `owner_url`, `backend_url` (session-scoped, `postgresql+psycopg://…` strings); `session` (one `AsyncSession` per test, connected as `lararium_backend`, whose outer transaction is rolled back when the test ends).

- [ ] **Step 1: Write `infra/postgres/roles.sql`** (shared by tests and deployment)

```sql
CREATE ROLE lararium_owner LOGIN PASSWORD :'owner_pw';
CREATE ROLE lararium_backend LOGIN PASSWORD :'backend_pw';
CREATE DATABASE lararium OWNER lararium_owner;
\connect lararium
REVOKE ALL ON SCHEMA public FROM PUBLIC;
```

`00-roles.sh` runs it with `psql -v ON_ERROR_STOP=1 -v owner_pw="$LARARIUM_OWNER_PASSWORD" -v backend_pw="$LARARIUM_BACKEND_PASSWORD" -U "$POSTGRES_USER" -f /lararium/roles.sql`.

- [ ] **Step 2: Write `tests/conftest.py`**

- A session-scoped fixture starts `PostgresContainer("postgres:18")`, mounts `infra/postgres/` read-only at `/lararium` in the container, runs `psql -v ON_ERROR_STOP=1 -v owner_pw=owner -v backend_pw=backend -U <container superuser> -f /lararium/roles.sql` inside the container, and asserts that the exit code is 0.
- From that, derive `owner_url` (`lararium_owner:owner`) and `backend_url` (`lararium_backend:backend`), both pointing at the `lararium` database.
- A session-scoped fixture runs `alembic upgrade head` with `owner_url` (setting `sqlalchemy.url` through `alembic.config.Config`).
- `session` fixture: build an async engine from `backend_url`, open a connection and `begin()` an outer transaction, create `AsyncSession(bind=conn, join_transaction_mode="create_savepoint")`, and roll back the outer transaction when the test ends. This way a `commit()` in the code under test only releases a savepoint and doesn't leak into other tests.

- [ ] **Step 3: Write the failing tests**

```python
# backend/tests/core/test_baseline.py
import pytest
from sqlalchemy import exc, text
from sqlalchemy.ext.asyncio import AsyncSession


async def test_schemas_exist(session: AsyncSession) -> None:
    rows = await session.execute(
        text("select nspname from pg_namespace where nspname in ('core', 'finance') order by 1")
    )
    assert rows.scalars().all() == ["core", "finance"]


async def test_backend_role_cannot_create_tables(session: AsyncSession) -> None:
    with pytest.raises(exc.ProgrammingError, match="permission denied"):
        await session.execute(text("create table finance.should_fail (id int)"))


async def test_no_float_columns(session: AsyncSession) -> None:
    rows = await session.execute(text(
        "select table_schema || '.' || table_name || '.' || column_name"
        " from information_schema.columns"
        " where table_schema in ('core', 'finance')"
        " and data_type in ('real', 'double precision')"
    ))
    assert rows.scalars().all() == []


async def test_default_timezone_setting(session: AsyncSession) -> None:
    value = await session.scalar(text("select value from core.settings where key = 'core.timezone'"))
    assert value == "Europe/London"


async def test_exactly_one_self_entity(session: AsyncSession) -> None:
    rows = await session.execute(
        text("select kind, owner_entity_id = id from core.entities where is_self")
    )
    assert rows.all() == [("person", True)]
```

- [ ] **Step 4: Run them and confirm they fail**

Run: `cd backend && uv run pytest tests/core/test_baseline.py -q`
Expected: FAIL (alembic can't find its config or migrations; if the schemas already exist, the `test_schemas_exist` assertion fails)

- [ ] **Step 5: Write `config.py`, `db.py`, the Alembic config and migration `0001_core_baseline`**

`migrations/env.py` uses a sync engine and reads the connection string from `sqlalchemy.url` in the alembic config or from the `LARARIUM_MIGRATION_DATABASE_URL` environment variable (directly, not through `Settings`, which would also demand the app's runtime variables). All migrations are written as raw SQL with `op.execute`. Contents of `0001_core_baseline`:

```sql
CREATE SCHEMA core AUTHORIZATION lararium_owner;
CREATE SCHEMA finance AUTHORIZATION lararium_owner;
GRANT USAGE ON SCHEMA core, finance TO lararium_backend;
ALTER DEFAULT PRIVILEGES FOR ROLE lararium_owner IN SCHEMA core, finance
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO lararium_backend;

CREATE TABLE core.settings (
  key text PRIMARY KEY,
  value jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO core.settings (key, value) VALUES ('core.timezone', '"Europe/London"');

CREATE TABLE core.entities (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  kind text NOT NULL CHECK (kind IN ('person', 'organization', 'place')),
  name text NOT NULL,
  aliases text[] NOT NULL DEFAULT '{}',
  external_ids jsonb NOT NULL DEFAULT '{}',
  is_self boolean NOT NULL DEFAULT false,
  owner_entity_id uuid REFERENCES core.entities (id),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX entities_single_self ON core.entities (is_self) WHERE is_self;

CREATE TABLE core.events (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  type text NOT NULL,
  source text NOT NULL,
  subject_id uuid,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  data jsonb NOT NULL,
  version int NOT NULL CHECK (version >= 1),
  published_at timestamptz
);

CREATE TABLE core.audit_log (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  actor_type text NOT NULL CHECK (actor_type IN ('user', 'rule', 'connector', 'agent', 'system')),
  actor_id text NOT NULL,
  command text NOT NULL,
  args jsonb NOT NULL,
  changes jsonb NOT NULL DEFAULT '{}',
  correlation_id uuid NOT NULL,
  occurred_at timestamptz NOT NULL DEFAULT now()
);
```

Three more things:
- Insert the self entity (`kind='person'`, `name='Me'`, `is_self=true`), then use a second statement to set its `owner_entity_id` to its own id.
- Create the trigger function `core.notify_event()`: `AFTER INSERT ON core.events`, executing `pg_notify('core_events', NEW.id::text)`.
- Write the downgrade: drop both schemas.

- [ ] **Step 6: Run them and confirm they pass**

Run: `cd backend && uv run pytest tests/core/test_baseline.py -q`
Expected: `5 passed`

- [ ] **Step 7: Commit**

```bash
git add infra/postgres backend/
git commit -m "feat: add database roles, test harness and core baseline migration"
```

---

### Task 3: Shared layer — command context, events, audit, settings, entities

**Files:**
- Create: `backend/src/lararium/core/{actors.py, json_types.py, errors.py, timeutil.py, models.py, commands.py, settings_store.py, entities.py}`
- Modify: `backend/tests/conftest.py` (add the `actor` and `run` fixtures)
- Test: `backend/tests/core/test_commands.py`, `backend/tests/core/test_settings_store.py`, `backend/tests/core/test_entities.py`, `backend/tests/core/test_timeutil.py`

**Interfaces:**
- Consumes: the `session` and `backend_url` fixtures from Task 2.
- Produces:
  - `ActorType = Literal["user", "rule", "connector", "agent", "system"]`; `@dataclass(frozen=True, slots=True) class Actor: type: ActorType; id: str`.
  - `JSONValue = None | bool | int | str | list["JSONValue"] | dict[str, "JSONValue"]`. Float is deliberately left out: a Decimal must be converted to a string before it goes in.
  - `class CommandContext`: attributes `session: AsyncSession`, `actor: Actor`, `correlation_id: UUID`; method `emit(event_type: str, subject_id: UUID | None, data: Mapping[str, JSONValue], version: int = 1) -> None`, where the event's `source` is the part of `event_type` before the first dot; method `record_change(key: str, value: JSONValue) -> None`.
  - `command_context(session: AsyncSession, actor: Actor, command: str, args: Mapping[str, JSONValue], correlation_id: UUID | None = None) -> AsyncContextManager[CommandContext]`: on normal exit it writes one `core.audit_log` row, writes all emitted `core.events`, then calls `session.commit()`; on an exception it calls `session.rollback()`, leaves nothing behind, and re-raises the exception.
  - `class SettingNotFoundError(Exception)` (in `core/errors.py`).
  - `async def get_setting(session: AsyncSession, key: str) -> JSONValue`; `async def set_setting(ctx: CommandContext, key: str, value: JSONValue) -> None` (upsert); `async def get_timezone(session: AsyncSession) -> ZoneInfo` (reads `core.timezone`).
  - `def local_date(ts: datetime, tz: ZoneInfo) -> date`: raises `ValueError` when `ts` has no timezone.
  - `async def get_self_entity_id(session: AsyncSession) -> UUID`; `async def get_or_create_organization(ctx: CommandContext, name: str) -> UUID`: matches name or alias case-insensitively; on a match returns the existing entity, otherwise creates a new organization whose owner is self.
  - Test fixtures: `actor` returns `Actor("user", "test")`; `run(fn, *args, **kwargs)` executes `await fn(ctx, *args, **kwargs)` inside `command_context(session, actor, "test", {})` and returns the result.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/core/test_commands.py
import pytest
from sqlalchemy import text

from lararium.core.commands import command_context


async def test_command_context_writes_audit_and_events(session, actor) -> None:
    async with command_context(session, actor, "test.ping", {"n": 1}) as ctx:
        ctx.emit("test.pinged", subject_id=None, data={"n": 1})
    audit = (await session.execute(
        text("select actor_type, actor_id, command, args from core.audit_log")
    )).one()
    assert tuple(audit) == ("user", "test", "test.ping", {"n": 1})
    event = (await session.execute(
        text("select type, source, data, version, published_at from core.events")
    )).one()
    assert tuple(event) == ("test.pinged", "test", {"n": 1}, 1, None)


async def test_command_context_persists_nothing_on_error(session, actor) -> None:
    with pytest.raises(RuntimeError):
        async with command_context(session, actor, "test.fail", {}) as ctx:
            ctx.emit("test.failed", subject_id=None, data={})
            raise RuntimeError("boom")
    assert await session.scalar(text("select count(*) from core.audit_log")) == 0
    assert await session.scalar(text("select count(*) from core.events")) == 0


async def test_event_insert_notifies_listeners(backend_url: str) -> None:
    import psycopg

    dsn = backend_url.replace("postgresql+psycopg://", "postgresql://")
    async with (
        await psycopg.AsyncConnection.connect(dsn, autocommit=True) as listener,
        await psycopg.AsyncConnection.connect(dsn, autocommit=True) as writer,
    ):
        await listener.execute("LISTEN core_events")
        cursor = await writer.execute(
            "insert into core.events (type, source, data, version)"
            " values ('test.x', 'test', '{}', 1) returning id"
        )
        (event_id,) = await cursor.fetchone()
        notify = await anext(listener.notifies(timeout=5))
        await writer.execute("delete from core.events where id = %s", (event_id,))
    assert (notify.channel, notify.payload) == ("core_events", str(event_id))
```

```python
# backend/tests/core/test_settings_store.py
import pytest

from lararium.core.errors import SettingNotFoundError
from lararium.core.settings_store import get_setting, get_timezone, set_setting


async def test_get_and_set_setting(session, run) -> None:
    assert await get_setting(session, "core.timezone") == "Europe/London"
    await run(set_setting, "core.timezone", "Asia/Shanghai")
    assert await get_setting(session, "core.timezone") == "Asia/Shanghai"
    assert str(await get_timezone(session)) == "Asia/Shanghai"


async def test_unknown_setting_raises(session) -> None:
    with pytest.raises(SettingNotFoundError):
        await get_setting(session, "nope")
```

```python
# backend/tests/core/test_entities.py
from lararium.core.entities import get_or_create_organization, get_self_entity_id


async def test_organization_matched_case_insensitively(run) -> None:
    first = await run(get_or_create_organization, "Example Bank")
    second = await run(get_or_create_organization, "example bank")
    assert first == second


async def test_self_entity_id_is_stable(session) -> None:
    assert await get_self_entity_id(session) == await get_self_entity_id(session)
```

```python
# backend/tests/core/test_timeutil.py
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from lararium.core.timeutil import local_date

LONDON = ZoneInfo("Europe/London")


def test_local_date_crosses_midnight_in_bst() -> None:
    assert local_date(datetime(2026, 3, 31, 23, 30, tzinfo=UTC), LONDON) == date(2026, 4, 1)


def test_local_date_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError):
        local_date(datetime(2026, 3, 31, 23, 30), LONDON)  # noqa: DTZ001
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `cd backend && uv run pytest tests/core -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'lararium.core.commands'`

- [ ] **Step 3: Implement the files under `core/`, and add the `actor` and `run` fixtures to `conftest.py`**

`models.py` uses SQLAlchemy 2 typed declarative to map the four core tables; `id` uses `server_default=text("uuidv7()")`. `command_context` is implemented with `@asynccontextmanager`.

- [ ] **Step 4: Run them and confirm they pass**

Run: `cd backend && uv run pytest tests/core -q && uv run pyright && uv run lint-imports`
Expected: all core tests pass; pyright reports 0 errors; all import contracts KEPT

- [ ] **Step 5: Commit**

```bash
git add backend/
git commit -m "feat: add command context with audit log, outbox events and settings"
```

---

### Task 4: Instruments and money helpers

**Files:**
- Create: `backend/src/lararium/modules/finance/{__init__.py, errors.py, money.py, models.py, instruments.py}`, `backend/migrations/versions/0002_finance_instruments.py`
- Test: `backend/tests/finance/conftest.py`, `backend/tests/finance/test_money.py`, `backend/tests/finance/test_instruments.py`

**Interfaces:**
- Consumes: `CommandContext`, `get_setting`, `get_self_entity_id`.
- Produces:
  - `def tolerance(decimals: int) -> Decimal`: returns half of the last decimal place, i.e. `Decimal(1).scaleb(-decimals) / 2`.
  - `def to_plain_str(value: Decimal) -> str`: fixed-point notation, never scientific notation, keeping any existing trailing zeros.
  - `InstrumentKind = Literal["fiat", "equity", "etf", "fund", "bond", "crypto", "option", "cfd", "other"]`.
  - `IdentifierScheme = Literal["iso4217", "isin", "figi", "ticker_mic", "ibkr_conid", "etoro_id", "chain_contract", "coingecko"]`.
  - `@dataclass(frozen=True) class InstrumentIdentifier: scheme: IdentifierScheme; value: str`.
  - `@dataclass(frozen=True) class InstrumentRecord`: `id: UUID`, `kind: InstrumentKind`, `symbol: str`, `name: str`, `decimals: int`, `quote_instrument_id: UUID | None`, `hidden: bool`, `identifiers: tuple[InstrumentIdentifier, ...]` (sorted by scheme).
  - `def normalize_identifier(scheme: IdentifierScheme, value: str) -> str`: first strips leading and trailing whitespace; uppercases `iso4217`, `isin` and `figi`; lowercases `coingecko` and `chain_contract`; leaves the rest unchanged.
  - `async def create_instrument(ctx: CommandContext, *, kind: InstrumentKind, symbol: str, name: str, decimals: int, quote_instrument_id: UUID | None, identifiers: Sequence[InstrumentIdentifier]) -> InstrumentRecord`: emits `finance.instrument.created`.
  - `async def find_by_identifier(session: AsyncSession, scheme: IdentifierScheme, value: str) -> InstrumentRecord | None`
  - `async def get_currency(session: AsyncSession, iso_code: str) -> InstrumentRecord`: raises `UnknownCurrencyError` when not found.
  - `async def get_instrument(session: AsyncSession, instrument_id: UUID) -> InstrumentRecord`: raises `InstrumentNotFoundError` when not found.
  - `async def set_hidden(ctx: CommandContext, instrument_id: UUID, hidden: bool) -> InstrumentRecord`: emits `finance.instrument.updated`.
  - `async def list_instruments(session: AsyncSession, *, include_hidden: bool) -> list[InstrumentRecord]`: sorted by symbol.
  - `finance/errors.py`: `FinanceError(Exception)` as the base class, with subclasses `UnknownCurrencyError`, `NotACurrencyError`, `InstrumentNotFoundError`, `DuplicateIdentifierError`, `AccountNotFoundError`, `AccountAlreadyClosedError`, `TransactionNotFoundError`, and `LedgerImbalanceError` (with attribute `residuals: dict[UUID, Decimal]`).
  - Setting key constant `REPORTING_CURRENCY_KEY = "finance.reporting_currency"`.
  - Test fixtures (`tests/finance/conftest.py`): `gbp`, `usd`, `eur`, the results of `await get_currency(session, "GBP")` and so on.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/finance/test_money.py
from decimal import Decimal

from lararium.modules.finance.money import to_plain_str, tolerance


def test_tolerance_is_half_of_last_decimal_place() -> None:
    assert tolerance(2) == Decimal("0.005")
    assert tolerance(0) == Decimal("0.5")
    assert tolerance(18) == Decimal("5E-19")


def test_to_plain_str_never_uses_exponent() -> None:
    assert to_plain_str(Decimal("1E-18")) == "0.000000000000000001"
    assert to_plain_str(Decimal("1.50")) == "1.50"
    assert to_plain_str(Decimal("-2E+3")) == "-2000"
```

```python
# backend/tests/finance/test_instruments.py
import pytest

from lararium.core.settings_store import get_setting
from lararium.modules.finance.errors import DuplicateIdentifierError
from lararium.modules.finance.instruments import (
    InstrumentIdentifier, create_instrument, find_by_identifier, get_currency, list_instruments,
    set_hidden,
)


async def test_seeded_currencies(session) -> None:
    gbp = await get_currency(session, "gbp")
    jpy = await get_currency(session, "JPY")
    assert (gbp.kind, gbp.symbol, gbp.decimals) == ("fiat", "GBP", 2)
    assert jpy.decimals == 0
    assert gbp.identifiers == (InstrumentIdentifier("iso4217", "GBP"),)


async def test_create_crypto_with_normalized_identifier(session, run, usd) -> None:
    btc = await run(
        create_instrument, kind="crypto", symbol="BTC", name="Bitcoin", decimals=8,
        quote_instrument_id=usd.id, identifiers=[InstrumentIdentifier("coingecko", " Bitcoin ")],
    )
    found = await find_by_identifier(session, "coingecko", "BITCOIN")
    assert found is not None and found.id == btc.id
    assert found.identifiers == (InstrumentIdentifier("coingecko", "bitcoin"),)


async def test_duplicate_identifier_rejected(run) -> None:
    with pytest.raises(DuplicateIdentifierError):
        await run(
            create_instrument, kind="fiat", symbol="GBX", name="dup", decimals=2,
            quote_instrument_id=None, identifiers=[InstrumentIdentifier("iso4217", "gbp")],
        )


async def test_hidden_instrument_left_out_of_default_list(session, run, usd) -> None:
    spam = await run(
        create_instrument, kind="crypto", symbol="SPAM", name="Airdrop", decimals=18,
        quote_instrument_id=usd.id, identifiers=[],
    )
    await run(set_hidden, spam.id, True)
    assert spam.id not in [i.id for i in await list_instruments(session, include_hidden=False)]
    assert spam.id in [i.id for i in await list_instruments(session, include_hidden=True)]


async def test_reporting_currency_defaults_to_gbp(session) -> None:
    assert await get_setting(session, "finance.reporting_currency") == "GBP"
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `cd backend && uv run pytest tests/finance -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'lararium.modules.finance'`

- [ ] **Step 3: Write migration `0002_finance_instruments`**

```sql
INSERT INTO core.settings (key, value) VALUES ('finance.reporting_currency', '"GBP"');

CREATE TABLE finance.instruments (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  kind text NOT NULL CHECK (kind IN ('fiat', 'equity', 'etf', 'fund', 'bond', 'crypto', 'option', 'cfd', 'other')),
  symbol text NOT NULL,
  name text NOT NULL,
  decimals int NOT NULL CHECK (decimals BETWEEN 0 AND 30),
  quote_instrument_id uuid REFERENCES finance.instruments (id),
  hidden boolean NOT NULL DEFAULT false,
  owner_entity_id uuid NOT NULL REFERENCES core.entities (id),
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (kind <> 'fiat' OR quote_instrument_id IS NULL)
);

CREATE TABLE finance.instrument_identifiers (
  instrument_id uuid NOT NULL REFERENCES finance.instruments (id) ON DELETE CASCADE,
  scheme text NOT NULL CHECK (scheme IN ('iso4217', 'isin', 'figi', 'ticker_mic', 'ibkr_conid', 'etoro_id', 'chain_contract', 'coingecko')),
  value text NOT NULL,
  PRIMARY KEY (scheme, value)
);
```

Seed data: GBP, USD, EUR, CNY, HKD, CHF, CAD, AUD, SGD (all with 2 decimals), JPY (0 decimals). All are `kind='fiat'`, `name` is the full English name, the owner is the self entity, and each has one `iso4217` identifier whose value equals the symbol.

- [ ] **Step 4: Implement `money.py`, `errors.py`, `models.py` (the instrument parts) and `instruments.py`; put an empty placeholder `MODULE` in `finance/__init__.py` for now**

`create_instrument` normalizes identifiers before inserting them; on a primary-key conflict it catches `IntegrityError` and turns it into `DuplicateIdentifierError`.

- [ ] **Step 5: Run them and confirm they pass**

Run: `cd backend && uv run pytest tests/finance -q`
Expected: `7 passed`

- [ ] **Step 6: Commit**

```bash
git add backend/
git commit -m "feat: add instruments with identifiers and decimal helpers"
```

---

### Task 5: Accounts

**Files:**
- Create: `backend/src/lararium/modules/finance/accounts.py`, `backend/migrations/versions/0003_finance_accounts.py`
- Modify: `backend/src/lararium/modules/finance/models.py`
- Test: `backend/tests/finance/test_accounts.py`; add a `make_account` fixture to `backend/tests/finance/conftest.py`

**Interfaces:**
- Consumes: `get_or_create_organization`, `get_self_entity_id`, `CommandContext`.
- Produces:
  - `LedgerType = Literal["asset", "liability", "income", "expense", "equity"]`
  - `AccountSubtype = Literal["bank", "savings_pot", "broker", "exchange", "wallet", "in_transit", "manual_asset", "other"]`
  - `TaxWrapper = Literal["none", "isa", "sipp", "lisa", "gia"]`
  - `BalanceMode = Literal["transactions", "snapshots", "hybrid"]`
  - `@dataclass(frozen=True) class AccountRecord`: `id`, `name`, `ledger_type`, `subtype`, `institution_entity_id: UUID | None`, `owner_entity_id: UUID`, `tax_wrapper`, `parent_account_id: UUID | None`, `balance_mode`, `source_kind: Literal["manual", "connection"]`, `source_connection_id: UUID | None`, `status: Literal["open", "closed"]`, `opened_on: date | None`, `closed_on: date | None`.
  - `async def create_account(ctx, *, name: str, ledger_type: LedgerType, subtype: AccountSubtype, balance_mode: BalanceMode, institution_entity_id: UUID | None = None, tax_wrapper: TaxWrapper = "none", parent_account_id: UUID | None = None, opened_on: date | None = None) -> AccountRecord`: raises `AccountNotFoundError` when the parent account doesn't exist; emits `finance.account.created` with `data={"account_id": str(id)}`.
  - `async def rename_account(ctx, account_id: UUID, name: str) -> AccountRecord`: emits `finance.account.updated`.
  - `async def close_account(ctx, account_id: UUID, closed_on: date) -> AccountRecord`: raises `AccountAlreadyClosedError` when closing an account that is already closed; emits `finance.account.closed`.
  - `async def get_account(session, account_id: UUID) -> AccountRecord`: raises `AccountNotFoundError` when not found.
  - `async def list_accounts(session) -> list[AccountRecord]`: sorted by name.
  - Test fixture `make_account(**overrides) -> AccountRecord`. Defaults: `name="Test"`, `ledger_type="asset"`, `subtype="bank"`, `balance_mode="snapshots"`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/finance/test_accounts.py
import uuid
from datetime import date

import pytest
from sqlalchemy import text

from lararium.core.entities import get_or_create_organization, get_self_entity_id
from lararium.modules.finance.accounts import close_account, create_account
from lararium.modules.finance.errors import AccountAlreadyClosedError, AccountNotFoundError


async def test_create_manual_bank_account(session, run) -> None:
    manual_bank = await run(get_or_create_organization, "Example Bank")
    acc = await run(
        create_account, name="Current account", ledger_type="asset", subtype="bank",
        balance_mode="hybrid", institution_entity_id=manual_bank,
    )
    assert (acc.source_kind, acc.source_connection_id, acc.status, acc.tax_wrapper) == (
        "manual", None, "open", "none",
    )
    assert acc.owner_entity_id == await get_self_entity_id(session)


async def test_lisa_wrapper_is_kept(make_account) -> None:
    acc = await make_account(name="Cash LISA", tax_wrapper="lisa")
    assert acc.tax_wrapper == "lisa"


async def test_pot_is_child_of_parent(make_account) -> None:
    parent = await make_account(name="Monzo")
    pot = await make_account(name="Savings pot", subtype="savings_pot", parent_account_id=parent.id)
    assert pot.parent_account_id == parent.id


async def test_unknown_parent_rejected(make_account) -> None:
    with pytest.raises(AccountNotFoundError):
        await make_account(parent_account_id=uuid.uuid4())


async def test_close_account(run, make_account) -> None:
    acc = await make_account()
    closed = await run(close_account, acc.id, date(2026, 6, 30))
    assert (closed.status, closed.closed_on) == ("closed", date(2026, 6, 30))
    with pytest.raises(AccountAlreadyClosedError):
        await run(close_account, acc.id, date(2026, 7, 1))


async def test_create_account_emits_event(session, make_account) -> None:
    acc = await make_account()
    data = await session.scalar(
        text("select data from core.events where type = 'finance.account.created'")
    )
    assert data == {"account_id": str(acc.id)}
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `cd backend && uv run pytest tests/finance/test_accounts.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'lararium.modules.finance.accounts'`

- [ ] **Step 3: Write migration `0003_finance_accounts`**

```sql
CREATE TABLE finance.accounts (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  name text NOT NULL,
  ledger_type text NOT NULL CHECK (ledger_type IN ('asset', 'liability', 'income', 'expense', 'equity')),
  subtype text NOT NULL CHECK (subtype IN ('bank', 'savings_pot', 'broker', 'exchange', 'wallet', 'in_transit', 'manual_asset', 'other')),
  institution_entity_id uuid REFERENCES core.entities (id),
  owner_entity_id uuid NOT NULL REFERENCES core.entities (id),
  tax_wrapper text NOT NULL DEFAULT 'none' CHECK (tax_wrapper IN ('none', 'isa', 'sipp', 'lisa', 'gia')),
  parent_account_id uuid REFERENCES finance.accounts (id),
  balance_mode text NOT NULL CHECK (balance_mode IN ('transactions', 'snapshots', 'hybrid')),
  source_kind text NOT NULL DEFAULT 'manual' CHECK (source_kind IN ('manual', 'connection')),
  source_connection_id uuid,  -- the foreign key to core.connections is added in plan 3
  status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
  opened_on date,
  closed_on date,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((source_kind = 'manual') = (source_connection_id IS NULL)),
  CHECK ((status = 'closed') = (closed_on IS NOT NULL))
);
```

- [ ] **Step 4: Implement `accounts.py`, extend `models.py`, and add `make_account` to `tests/finance/conftest.py`**

- [ ] **Step 5: Run them and confirm they pass**

Run: `cd backend && uv run pytest tests/finance -q`
Expected: the 6 new tests and all earlier tests pass

- [ ] **Step 6: Commit**

```bash
git add backend/
git commit -m "feat: add accounts with tax wrappers, parents and closing"
```

---

### Task 6: Double-entry ledger

**Files:**
- Create: `backend/src/lararium/modules/finance/ledger.py`, `backend/migrations/versions/0004_finance_ledger.py`
- Modify: `backend/src/lararium/modules/finance/models.py`
- Test: `backend/tests/finance/test_ledger_residuals.py`, `backend/tests/finance/test_ledger.py`

**Interfaces:**
- Consumes: `tolerance`, `get_instrument`, `get_timezone`, `local_date`, `get_self_entity_id`, `make_account`, `gbp`, `usd`.
- Produces:
  - `TransactionStatus = Literal["pending", "booked", "inferred", "confirmed", "void"]`; `TransactionOrigin = Literal["connector", "manual", "inference", "rule"]`.
  - `@dataclass(frozen=True) class PostingInput`: `account_id: UUID`, `instrument_id: UUID`, `quantity: Decimal`, `cost_per_unit: Decimal | None = None`, `cost_instrument_id: UUID | None = None`, `price_per_unit: Decimal | None = None`, `price_instrument_id: UUID | None = None`, `is_fee: bool = False`, `memo: str = ""`.
  - `@dataclass(frozen=True) class TransactionInput`: `occurred_at: datetime` (must have a timezone), `description: str`, `postings: Sequence[PostingInput]`, `booking_date: date | None = None`, `status: TransactionStatus = "booked"`, `origin: TransactionOrigin = "manual"`, `occurred_tz: str | None = None`, `value_date: date | None = None`, `counterparty_entity_id: UUID | None = None`.
  - `PostingRecord`: all fields of `PostingInput`, plus `id: UUID` and `position: int`. `TransactionRecord`: `id`, `occurred_at`, `booking_date`, `description`, `status`, `origin`, and `postings: tuple[PostingRecord, ...]` sorted by position.
  - `def compute_residuals(postings: Sequence[PostingInput]) -> dict[UUID, Decimal]`: a pure function. Each posting's "weight" is computed as follows: with a cost, `(cost_instrument_id, quantity × cost_per_unit)`; otherwise, with a price, `(price_instrument_id, quantity × price_per_unit)`; with neither, `(instrument_id, quantity)`. Sum per instrument and return only the entries whose sum is not 0.
  - `async def post_transaction(ctx, tx: TransactionInput) -> TransactionRecord`: the absolute value of every residual must be ≤ `tolerance(decimals of that instrument)`, otherwise it raises `LedgerImbalanceError(residuals)` and writes nothing. When `booking_date` is empty, it is derived as `local_date(occurred_at, reporting timezone)`. Emits `finance.transaction.posted`.
  - `async def void_transaction(ctx, transaction_id: UUID) -> TransactionRecord`: sets the status to `void` and emits `finance.transaction.voided`; raises `TransactionNotFoundError` when not found.
  - `async def find_unbalanced_transactions(session) -> list[UUID]`: recomputes in the database with the same rules, skips void transactions, and returns the ids of unbalanced transactions. A future scheduled integrity job will call it.

- [ ] **Step 1: Write the failing tests for the pure function**

```python
# backend/tests/finance/test_ledger_residuals.py
from decimal import Decimal
from uuid import uuid4

from lararium.modules.finance.ledger import PostingInput, compute_residuals

A, B, C, GBP, USD, ETH = (uuid4() for _ in range(6))


def test_simple_transfer_balances() -> None:
    assert compute_residuals([
        PostingInput(A, GBP, Decimal("-100.00")), PostingInput(B, GBP, Decimal("100.00")),
    ]) == {}


def test_price_converts_fx_leg() -> None:
    assert compute_residuals([
        PostingInput(A, GBP, Decimal("-100")),
        PostingInput(B, USD, Decimal("125"), price_per_unit=Decimal("0.8"), price_instrument_id=GBP),
    ]) == {}


def test_imbalance_reported_per_instrument() -> None:
    assert compute_residuals([
        PostingInput(A, GBP, Decimal("-100")), PostingInput(B, GBP, Decimal("99.99")),
    ]) == {GBP: Decimal("-0.01")}


def test_eighteen_decimals_sum_exactly() -> None:
    assert compute_residuals([
        PostingInput(A, ETH, Decimal("-1.000000000000000001")),
        PostingInput(B, ETH, Decimal("0.999999999999999000")),
        PostingInput(C, ETH, Decimal("0.000000000000001001"), is_fee=True),
    ]) == {}
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `cd backend && uv run pytest tests/finance/test_ledger_residuals.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'lararium.modules.finance.ledger'`

- [ ] **Step 3: Implement `PostingInput` and `compute_residuals` in `ledger.py`**

- [ ] **Step 4: Run them and confirm they pass**

Run: `cd backend && uv run pytest tests/finance/test_ledger_residuals.py -q`
Expected: `4 passed`

- [ ] **Step 5: Write the failing tests for the persistence part**

```python
# backend/tests/finance/test_ledger.py
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from lararium.modules.finance.errors import LedgerImbalanceError
from lararium.modules.finance.ledger import (
    PostingInput, TransactionInput, find_unbalanced_transactions, post_transaction,
    void_transaction,
)

LATE_31_MARCH = datetime(2026, 3, 31, 23, 30, tzinfo=UTC)


async def test_booking_date_derived_in_reporting_timezone(run, make_account, gbp) -> None:
    monzo, manual_bank = await make_account(name="Monzo"), await make_account(name="Manual bank")
    tx = await run(post_transaction, TransactionInput(
        occurred_at=LATE_31_MARCH, description="Monzo to manual bank",
        postings=[PostingInput(monzo.id, gbp.id, Decimal("-500.00")),
                  PostingInput(manual_bank.id, gbp.id, Decimal("500.00"))],
    ))
    assert tx.booking_date == date(2026, 4, 1)
    assert [p.quantity for p in tx.postings] == [Decimal("-500.00"), Decimal("500.00")]


async def test_unbalanced_rejected_and_nothing_written(session, run, make_account, gbp) -> None:
    a, b = await make_account(name="A"), await make_account(name="B")
    with pytest.raises(LedgerImbalanceError) as err:
        await run(post_transaction, TransactionInput(
            occurred_at=LATE_31_MARCH, description="bad",
            postings=[PostingInput(a.id, gbp.id, Decimal("-100")),
                      PostingInput(b.id, gbp.id, Decimal("99.99"))],
        ))
    assert err.value.residuals == {gbp.id: Decimal("-0.01")}
    assert await session.scalar(text("select count(*) from finance.transactions")) == 0


async def test_residual_within_tolerance_accepted(run, make_account, gbp, usd) -> None:
    a, b = await make_account(name="A"), await make_account(name="B")
    tx = await run(post_transaction, TransactionInput(
        occurred_at=LATE_31_MARCH, description="fx",
        postings=[PostingInput(a.id, gbp.id, Decimal("-100.00")),
                  PostingInput(b.id, usd.id, Decimal("125.00"),
                               price_per_unit=Decimal("0.80003"), price_instrument_id=gbp.id)],
    ))
    assert tx.status == "booked"


async def test_void_transaction(run, make_account, gbp) -> None:
    a, b = await make_account(name="A"), await make_account(name="B")
    tx = await run(post_transaction, TransactionInput(
        occurred_at=LATE_31_MARCH, description="x",
        postings=[PostingInput(a.id, gbp.id, Decimal("-1")), PostingInput(b.id, gbp.id, Decimal("1"))],
    ))
    assert (await run(void_transaction, tx.id)).status == "void"


async def test_integrity_check_finds_tampered_transaction(session, run, make_account, gbp) -> None:
    a, b = await make_account(name="A"), await make_account(name="B")
    tx = await run(post_transaction, TransactionInput(
        occurred_at=LATE_31_MARCH, description="x",
        postings=[PostingInput(a.id, gbp.id, Decimal("-1")), PostingInput(b.id, gbp.id, Decimal("1"))],
    ))
    await session.execute(
        text("update finance.postings set quantity = 2 where transaction_id = :t and quantity = 1"),
        {"t": tx.id},
    )
    assert await find_unbalanced_transactions(session) == [tx.id]
```

- [ ] **Step 6: Run them and confirm they fail**

Run: `cd backend && uv run pytest tests/finance/test_ledger.py -q`
Expected: FAIL, `ImportError: cannot import name 'TransactionInput'`

- [ ] **Step 7: Write migration `0004_finance_ledger`**

```sql
CREATE TABLE finance.transactions (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  occurred_at timestamptz NOT NULL,
  occurred_tz text,
  booking_date date NOT NULL,
  value_date date,
  description text NOT NULL DEFAULT '',
  counterparty_entity_id uuid REFERENCES core.entities (id),
  status text NOT NULL CHECK (status IN ('pending', 'booked', 'inferred', 'confirmed', 'void')),
  origin text NOT NULL CHECK (origin IN ('connector', 'manual', 'inference', 'rule')),
  owner_entity_id uuid NOT NULL REFERENCES core.entities (id),
  recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX transactions_booking_date ON finance.transactions (booking_date);

CREATE TABLE finance.postings (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  transaction_id uuid NOT NULL REFERENCES finance.transactions (id) ON DELETE CASCADE,
  position int NOT NULL,
  account_id uuid NOT NULL REFERENCES finance.accounts (id),
  instrument_id uuid NOT NULL REFERENCES finance.instruments (id),
  quantity numeric NOT NULL CHECK (quantity <> 0),
  cost_per_unit numeric,
  cost_instrument_id uuid REFERENCES finance.instruments (id),
  price_per_unit numeric,
  price_instrument_id uuid REFERENCES finance.instruments (id),
  is_fee boolean NOT NULL DEFAULT false,
  memo text NOT NULL DEFAULT '',
  UNIQUE (transaction_id, position),
  CHECK ((cost_per_unit IS NULL) = (cost_instrument_id IS NULL)),
  CHECK ((price_per_unit IS NULL) = (price_instrument_id IS NULL))
);
CREATE INDEX postings_account_instrument ON finance.postings (account_id, instrument_id);
```

- [ ] **Step 8: Implement `post_transaction`, `void_transaction` and `find_unbalanced_transactions`**

- [ ] **Step 9: Run them and confirm they pass**

Run: `cd backend && uv run pytest tests/finance -q`
Expected: all pass

- [ ] **Step 10: Commit**

```bash
git add backend/
git commit -m "feat: add double-entry ledger with balance checks"
```

---

### Task 7: Balance snapshots, prices, FX rates

**Files:**
- Create: `backend/src/lararium/modules/finance/snapshots.py`, `backend/src/lararium/modules/finance/pricing.py`, `backend/migrations/versions/0005_finance_snapshots_prices.py`
- Modify: `backend/src/lararium/modules/finance/models.py`
- Test: `backend/tests/finance/test_snapshots.py`, `backend/tests/finance/test_pricing.py`; add a `btc` fixture to `tests/finance/conftest.py` (crypto, 8 decimals, quoted in USD)

**Interfaces:**
- Consumes: `get_timezone`, `local_date`, `get_account`, `get_instrument`, `get_self_entity_id`.
- Produces:
  - `SnapshotKind = Literal["reported", "manual"]`.
  - `@dataclass(frozen=True) class SnapshotRecord`: `id`, `account_id`, `instrument_id`, `quantity: Decimal`, `as_of: datetime`, `as_of_date: date`, `kind: SnapshotKind`.
  - `async def record_snapshot(ctx, *, account_id: UUID, instrument_id: UUID, quantity: Decimal, as_of: datetime, kind: SnapshotKind = "manual") -> SnapshotRecord`: raises `ValueError` when `as_of` has no timezone; `as_of_date = local_date(as_of, reporting timezone)`; emits `finance.snapshot.recorded`. Quantities are signed: negative for liabilities.
  - `async def record_price(ctx, *, instrument_id: UUID, quote_instrument_id: UUID, price: Decimal, as_of_date: date, source: str = "manual") -> None`: the quote currency must be fiat, otherwise it raises `NotACurrencyError`; for a given (instrument, quote currency, date, source) only the latest entry is kept (upsert); emits `finance.price.recorded`.
  - `async def record_fx_rate(ctx, *, base_instrument_id: UUID, quote_instrument_id: UUID, rate: Decimal, as_of_date: date, source: str = "manual") -> None`: both sides must be fiat, otherwise it raises `NotACurrencyError`; raises `ValueError` when `rate <= 0`; same upsert rule as above; emits `finance.fx_rate.recorded`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/finance/test_snapshots.py
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from lararium.modules.finance.snapshots import record_snapshot


async def test_snapshot_as_of_date_uses_reporting_timezone(run, make_account, gbp) -> None:
    acc = await make_account()
    snap = await run(record_snapshot, account_id=acc.id, instrument_id=gbp.id,
                     quantity=Decimal("1200.00"), as_of=datetime(2026, 3, 31, 23, 30, tzinfo=UTC))
    assert (snap.as_of_date, snap.kind) == (date(2026, 4, 1), "manual")


async def test_snapshot_rejects_naive_datetime(run, make_account, gbp) -> None:
    acc = await make_account()
    with pytest.raises(ValueError):
        await run(record_snapshot, account_id=acc.id, instrument_id=gbp.id,
                  quantity=Decimal("1"), as_of=datetime(2026, 3, 31, 23, 30))  # noqa: DTZ001
```

```python
# backend/tests/finance/test_pricing.py
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from lararium.modules.finance.errors import NotACurrencyError
from lararium.modules.finance.pricing import record_fx_rate, record_price

DAY = date(2026, 11, 10)


async def test_same_day_same_source_price_is_replaced(session, run, btc, usd) -> None:
    await run(record_price, instrument_id=btc.id, quote_instrument_id=usd.id,
              price=Decimal("60000"), as_of_date=DAY)
    await run(record_price, instrument_id=btc.id, quote_instrument_id=usd.id,
              price=Decimal("61000"), as_of_date=DAY)
    rows = (await session.execute(
        text("select price from finance.prices where instrument_id = :i"), {"i": btc.id}
    )).scalars().all()
    assert rows == [Decimal("61000")]


async def test_fx_rate_requires_currencies(run, btc, gbp) -> None:
    with pytest.raises(NotACurrencyError):
        await run(record_fx_rate, base_instrument_id=btc.id, quote_instrument_id=gbp.id,
                  rate=Decimal("50000"), as_of_date=DAY)


async def test_fx_rate_must_be_positive(run, usd, gbp) -> None:
    with pytest.raises(ValueError):
        await run(record_fx_rate, base_instrument_id=usd.id, quote_instrument_id=gbp.id,
                  rate=Decimal("0"), as_of_date=DAY)
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `cd backend && uv run pytest tests/finance/test_snapshots.py tests/finance/test_pricing.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'lararium.modules.finance.snapshots'`

- [ ] **Step 3: Write migration `0005_finance_snapshots_prices`**

```sql
CREATE TABLE finance.balance_snapshots (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  account_id uuid NOT NULL REFERENCES finance.accounts (id),
  instrument_id uuid NOT NULL REFERENCES finance.instruments (id),
  quantity numeric NOT NULL,
  as_of timestamptz NOT NULL,
  as_of_date date NOT NULL,
  kind text NOT NULL CHECK (kind IN ('reported', 'manual')),
  owner_entity_id uuid NOT NULL REFERENCES core.entities (id),
  recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX balance_snapshots_lookup
  ON finance.balance_snapshots (account_id, instrument_id, as_of_date DESC, as_of DESC);

CREATE TABLE finance.prices (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  instrument_id uuid NOT NULL REFERENCES finance.instruments (id),
  quote_instrument_id uuid NOT NULL REFERENCES finance.instruments (id),
  price numeric NOT NULL CHECK (price >= 0),
  as_of_date date NOT NULL,
  source text NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (instrument_id, quote_instrument_id, as_of_date, source)
);

CREATE TABLE finance.fx_rates (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  base_instrument_id uuid NOT NULL REFERENCES finance.instruments (id),
  quote_instrument_id uuid NOT NULL REFERENCES finance.instruments (id),
  rate numeric NOT NULL CHECK (rate > 0),
  as_of_date date NOT NULL,
  source text NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (base_instrument_id, quote_instrument_id, as_of_date, source),
  CHECK (base_instrument_id <> quote_instrument_id)
);
```

- [ ] **Step 4: Implement `snapshots.py` and `pricing.py`, and add the `btc` fixture**

Upserts use `INSERT … ON CONFLICT (…) DO UPDATE SET price = EXCLUDED.price, recorded_at = now()`; the FX rate table works the same way.

- [ ] **Step 5: Run them and confirm they pass**

Run: `cd backend && uv run pytest tests/finance -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add backend/
git commit -m "feat: add balance snapshots, prices and fx rates"
```

---

### Task 8: Valuation functions (v1)

**Files:**
- Create: `backend/migrations/versions/0006_finance_valuation_v1.py`, `backend/src/lararium/modules/finance/valuation.py`
- Test: `backend/tests/finance/test_valuation.py`

**Interfaces:**
- Consumes: all services and fixtures from Tasks 4–7.
- Produces (SQL functions; BI and MCP will use the same ones later):
  - `finance.v1_fx_rate(p_base uuid, p_quote uuid, p_day date) RETURNS numeric`
  - `finance.v1_balances_asof(p_day date) RETURNS TABLE (account_id uuid, instrument_id uuid, quantity numeric, snapshot_quantity numeric, snapshot_date date)`
  - `finance.v1_valuation_asof(p_day date) RETURNS TABLE (account_id uuid, account_name text, instrument_id uuid, symbol text, quantity numeric, unit_price numeric, price_currency_id uuid, fx_rate numeric, value numeric, status text)`
  - `finance.v1_net_worth_series(p_start date, p_end date) RETURNS TABLE (day date, value numeric, missing_count int)`
- Produces (Python wrappers, in `valuation.py`):
  - `@dataclass(frozen=True) class BalanceRow`: `account_id`, `instrument_id`, `quantity: Decimal`, `snapshot_quantity: Decimal | None`, `snapshot_date: date | None`
  - `HoldingStatus = Literal["ok", "missing_price", "missing_fx"]`
  - `@dataclass(frozen=True) class HoldingRow`: `account_id`, `account_name: str`, `instrument_id`, `symbol: str`, `quantity: Decimal`, `unit_price: Decimal | None`, `price_currency_id: UUID | None`, `fx_rate: Decimal | None`, `value: Decimal | None`, `status: HoldingStatus`
  - `@dataclass(frozen=True) class NetWorthPoint`: `day: date`, `value: Decimal`, `missing_count: int`
  - `async def balances_asof(session, day: date) -> list[BalanceRow]`
  - `async def holdings_asof(session, day: date) -> list[HoldingRow]`: sorted by account name, then symbol.
  - `async def net_worth_series(session, start: date, end: date) -> list[NetWorthPoint]`: one row per day, in ascending date order.

**Rules these functions must follow:**

- **Snapshot selection**: take the latest snapshot with `as_of_date <= p_day`, ordered by `as_of_date DESC, as_of DESC, recorded_at DESC`.
- **Posting scope**: exclude transactions with `status = 'void'`.
- **Account scope**: exclude accounts with `status = 'closed' AND closed_on <= p_day`.
- **Quantity per balance mode**:
  - `snapshots`: the snapshot's quantity; no snapshot means no row.
  - `transactions`: the sum of postings with `booking_date <= p_day`.
  - `hybrid`: with a snapshot, = the snapshot quantity + the sum of postings whose transactions have `occurred_at > snapshot.as_of` and `booking_date <= p_day`; without a snapshot, the same as `transactions`.
- **FX rate `v1_fx_rate`** (spec §9, "the latest rate on or before the day"):
  1. same currency on both sides → exactly 1;
  2. otherwise there are up to three candidate paths:
     - direct: the latest base→quote rate with `as_of_date <= p_day`; within the same day, prefer `source = 'manual'`, then order by `recorded_at DESC`;
     - inverse: the reciprocal of the latest quote→base rate, chosen the same way;
     - via EUR: base→EUR × EUR→quote, with only one intermediate hop; each leg is chosen by these same rules (its fresher of direct and inverse, direct on a tie). The path's effective date is the older of its two legs' dates;
  3. the path with the most recent effective date wins; ties go direct, then inverse, then via EUR;
  4. no path → NULL.
- **Valuation `v1_valuation_asof`**:
  - Includes only accounts whose `ledger_type` is `asset` or `liability`, and excludes hidden instruments and rows with a quantity of 0.
  - The reporting currency is the fiat instrument matching `finance.reporting_currency` in `core.settings`.
  - Fiat instruments: `unit_price = 1`, `price_currency_id` is the instrument itself, and the FX rate converts it to the reporting currency.
  - Other instruments: take the latest price, with the same rules as for FX rates (`as_of_date <= p_day`; within the same day, prefer manual, then `recorded_at DESC`), then convert the price's quote currency to the reporting currency.
  - `status`: no price gives `missing_price`; no FX rate gives `missing_fx`; otherwise `ok`. `value = quantity × unit_price × fx_rate`, filled in only when `ok` and NULL otherwise.
- **Net worth series `v1_net_worth_series`**: one row per day from `p_start` to `p_end`. `value` is the sum of the values of that day's `ok` rows (0 if there are none), and `missing_count` is the number of non-`ok` rows that day.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/finance/test_valuation.py
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from lararium.core.settings_store import set_setting
from lararium.modules.finance.instruments import create_instrument, get_currency, set_hidden
from lararium.modules.finance.accounts import close_account
from lararium.modules.finance.ledger import (
    PostingInput, TransactionInput, post_transaction, void_transaction,
)
from lararium.modules.finance.pricing import record_fx_rate, record_price
from lararium.modules.finance.snapshots import record_snapshot
from lararium.modules.finance.valuation import balances_asof, holdings_asof, net_worth_series

LON = ZoneInfo("Europe/London")


def at(y: int, m: int, d: int, hh: int = 12) -> datetime:
    return datetime(y, m, d, hh, tzinfo=LON)


async def snap(run, account, instrument, qty: str, when: datetime) -> None:
    await run(record_snapshot, account_id=account.id, instrument_id=instrument.id,
              quantity=Decimal(qty), as_of=when)


async def test_snapshot_mode_uses_latest_on_or_before_day(session, run, make_account, gbp) -> None:
    acc = await make_account()
    await snap(run, acc, gbp, "1000", at(2026, 11, 1))
    await snap(run, acc, gbp, "1500", at(2026, 11, 5))
    assert [r.quantity for r in await balances_asof(session, date(2026, 11, 4))] == [Decimal("1000")]
    assert [r.quantity for r in await balances_asof(session, date(2026, 11, 5))] == [Decimal("1500")]


async def test_same_day_snapshots_latest_time_wins(session, run, make_account, gbp) -> None:
    acc = await make_account()
    await snap(run, acc, gbp, "1500", at(2026, 11, 5, 9))
    await snap(run, acc, gbp, "1700", at(2026, 11, 5, 18))
    assert [r.quantity for r in await balances_asof(session, date(2026, 11, 5))] == [Decimal("1700")]


async def test_transactions_mode_excludes_void(session, run, make_account, gbp) -> None:
    acc = await make_account(balance_mode="transactions")
    other = await make_account(name="Other", ledger_type="equity", subtype="other")
    first = TransactionInput(occurred_at=at(2026, 11, 1), description="in", postings=[
        PostingInput(acc.id, gbp.id, Decimal("100")), PostingInput(other.id, gbp.id, Decimal("-100"))])
    second = TransactionInput(occurred_at=at(2026, 11, 3), description="in", postings=[
        PostingInput(acc.id, gbp.id, Decimal("50")), PostingInput(other.id, gbp.id, Decimal("-50"))])
    await run(post_transaction, first)
    tx2 = await run(post_transaction, second)
    await run(void_transaction, tx2.id)
    rows = {r.account_id: r.quantity for r in await balances_asof(session, date(2026, 11, 4))}
    assert rows[acc.id] == Decimal("100")


async def test_hybrid_adds_postings_after_snapshot(session, run, make_account, gbp) -> None:
    manual_bank = await make_account(name="Manual bank", balance_mode="hybrid")
    monzo = await make_account(name="Monzo", balance_mode="transactions")
    await snap(run, manual_bank, gbp, "2000", at(2026, 11, 1, 18))
    for when, qty in ((at(2026, 11, 1, 9), "100"), (at(2026, 11, 5, 10), "500")):
        await run(post_transaction, TransactionInput(occurred_at=when, description="t", postings=[
            PostingInput(monzo.id, gbp.id, Decimal(f"-{qty}")),
            PostingInput(manual_bank.id, gbp.id, Decimal(qty))]))
    rows = {r.account_id: r.quantity for r in await balances_asof(session, date(2026, 11, 5))}
    assert rows[manual_bank.id] == Decimal("2500")
    rows = {r.account_id: r.quantity for r in await balances_asof(session, date(2026, 11, 4))}
    assert rows[manual_bank.id] == Decimal("2000")


async def test_fiat_valued_direct_inverse_and_via_eur(session, run, make_account, gbp, usd, eur) -> None:
    cny = await get_currency(session, "CNY")
    usd_acc, eur_acc, cny_acc = [await make_account(name=n) for n in ("USD", "EUR", "CNY")]
    day = date(2026, 11, 10)
    await snap(run, usd_acc, usd, "100", at(2026, 11, 10))
    await snap(run, eur_acc, eur, "100", at(2026, 11, 10))
    await snap(run, cny_acc, cny, "1000", at(2026, 11, 10))
    await run(record_fx_rate, base_instrument_id=usd.id, quote_instrument_id=gbp.id,
              rate=Decimal("0.8"), as_of_date=day)
    await run(record_fx_rate, base_instrument_id=gbp.id, quote_instrument_id=eur.id,
              rate=Decimal("1.25"), as_of_date=day)
    await run(record_fx_rate, base_instrument_id=eur.id, quote_instrument_id=cny.id,
              rate=Decimal("8"), as_of_date=day)
    values = {h.symbol: h.value for h in await holdings_asof(session, day)}
    assert values["USD"] == Decimal("80")
    assert values["EUR"] == Decimal("80")
    assert values["CNY"] == Decimal("100")


async def test_missing_fx_is_flagged_not_zero(session, run, make_account, gbp) -> None:
    ngn = await run(create_instrument, kind="fiat", symbol="NGN", name="Naira", decimals=2,
                    quote_instrument_id=None, identifiers=[])
    bank, fx_wallet = await make_account(name="Bank"), await make_account(name="Multi-currency wallet")
    await snap(run, bank, gbp, "1000", at(2026, 11, 10))
    await snap(run, fx_wallet, ngn, "50000", at(2026, 11, 10))
    holdings = {h.symbol: h for h in await holdings_asof(session, date(2026, 11, 10))}
    assert (holdings["NGN"].status, holdings["NGN"].value) == ("missing_fx", None)
    [point] = await net_worth_series(session, date(2026, 11, 10), date(2026, 11, 10))
    assert (point.value, point.missing_count) == (Decimal("1000"), 1)


async def test_crypto_valued_with_price_then_fx(session, run, make_account, btc, usd, gbp) -> None:
    wallet = await make_account(name="Wallet", subtype="wallet")
    day = date(2026, 11, 10)
    await snap(run, wallet, btc, "0.5", at(2026, 11, 10))
    [h] = await holdings_asof(session, day)
    assert h.status == "missing_price"
    await run(record_price, instrument_id=btc.id, quote_instrument_id=usd.id,
              price=Decimal("60000"), as_of_date=day)
    await run(record_fx_rate, base_instrument_id=usd.id, quote_instrument_id=gbp.id,
              rate=Decimal("0.8"), as_of_date=day)
    [h] = await holdings_asof(session, day)
    assert (h.status, h.value) == ("ok", Decimal("24000"))


async def test_hidden_and_closed_and_non_balance_accounts_excluded(
    session, run, make_account, gbp, usd
) -> None:
    spam = await run(create_instrument, kind="crypto", symbol="SPAM", name="Airdrop", decimals=18,
                     quote_instrument_id=usd.id, identifiers=[])
    wallet = await make_account(name="Wallet", subtype="wallet")
    old = await make_account(name="Old bank")
    await snap(run, wallet, spam, "1000000", at(2026, 11, 1))
    await snap(run, old, gbp, "10", at(2026, 11, 1))
    await run(set_hidden, spam.id, True)
    await run(close_account, old.id, date(2026, 11, 5))
    assert [h.symbol for h in await holdings_asof(session, date(2026, 11, 4))] == ["GBP"]
    assert await holdings_asof(session, date(2026, 11, 5)) == []


async def test_liability_reduces_net_worth(session, run, make_account, gbp) -> None:
    bank = await make_account(name="Bank")
    card = await make_account(name="Card", ledger_type="liability", subtype="other")
    await snap(run, bank, gbp, "2000", at(2026, 11, 10))
    await snap(run, card, gbp, "-500", at(2026, 11, 10))
    [point] = await net_worth_series(session, date(2026, 11, 10), date(2026, 11, 10))
    assert point.value == Decimal("1500")


async def test_reporting_currency_change_revalues(session, run, make_account, gbp, usd) -> None:
    bank = await make_account(name="Bank")
    await snap(run, bank, gbp, "100", at(2026, 11, 10))
    await run(record_fx_rate, base_instrument_id=usd.id, quote_instrument_id=gbp.id,
              rate=Decimal("0.8"), as_of_date=date(2026, 11, 10))
    await run(set_setting, "finance.reporting_currency", "USD")
    [h] = await holdings_asof(session, date(2026, 11, 10))
    assert h.value == Decimal("125")


async def test_series_has_one_row_per_day(session) -> None:
    days = [p.day for p in await net_worth_series(session, date(2026, 11, 1), date(2026, 11, 3))]
    assert days == [date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 3)]
```

The three cases in `test_fiat_valued_direct_inverse_and_via_eur`: USD uses the direct rate 0.8; EUR uses the reciprocal of GBP→EUR 1.25, 100 / 1.25 = 80; CNY goes via EUR, 1000 × (1/8) × (1/1.25) = 100. In `test_reporting_currency_change_revalues`, 100 GBP with the reciprocal of USD→GBP 0.8 equals 125 USD. Plain `Decimal` equality is enough for the comparisons: all these numbers divide exactly under numeric arithmetic, so there are no rounding issues.

- [ ] **Step 2: Run them and confirm they fail**

Run: `cd backend && uv run pytest tests/finance/test_valuation.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'lararium.modules.finance.valuation'`

- [ ] **Step 3: Write migration `0006_finance_valuation_v1`, implementing the four SQL functions per the rules above (`LANGUAGE sql STABLE` or `plpgsql STABLE`); write the Python wrappers in `valuation.py`**

The wrappers use `text("select * from finance.v1_valuation_asof(:day)")` and map the results to the dataclasses.

- [ ] **Step 4: Run them and confirm they pass**

Run: `cd backend && uv run pytest tests/finance -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add backend/
git commit -m "feat: add versioned valuation functions and net worth series"
```

---

### Task 9: REST API

**Files:**
- Create: `backend/src/lararium/api/{app.py, deps.py, core_routes.py, export_openapi.py}`, `backend/src/lararium/modules/registry.py`, `backend/src/lararium/modules/finance/{schemas.py, api.py}`
- Modify: `backend/src/lararium/modules/finance/__init__.py` (the real `MODULE`)
- Test: `backend/tests/api/conftest.py`, `backend/tests/api/test_core_api.py`, `backend/tests/api/test_finance_api.py`

**Interfaces:**
- Consumes: the services from all previous tasks.
- Produces:
  - `@dataclass(frozen=True) class ModuleSpec: name: str; install: Callable[[FastAPI], None]`; `MODULES: tuple[ModuleSpec, ...] = (finance.MODULE,)`. finance's `install` mounts the routes and maps the `FinanceError` subclasses to HTTP errors: not found → 404; duplicate, already closed → 409; unknown currency, not a currency → 422; ledger imbalance → 422, with the response body `{"detail": {"error": "ledger_imbalance", "residuals": {"<instrument_id>": "<plain decimal>"}}}`.
  - `def create_app(settings: Settings | None = None) -> FastAPI`: `generate_unique_id_function=lambda route: route.name`, so each route's `name` is its operationId and the generated TypeScript function names are stable.
  - `async def get_session(request: Request) -> AsyncIterator[AsyncSession]`; `def get_actor(request: Request) -> Actor`:
    - with `auth_mode="dev"`, returns `Actor("user", "dev")`;
    - with `auth_mode="tailscale"`, the `Tailscale-User-Login` request header must equal `owner_login`, otherwise it returns 401; when it matches, returns `Actor("user", <login>)`.
    - `/api/v1/health` requires no identity.
  - `DecimalStr`: an Annotated type based on `Decimal`. It accepts only strings matching `^-?[0-9]+(\.[0-9]+)?$` (ASCII digits only); any other input (including JSON numbers) gets 422; output goes through `to_plain_str`; the JSON schema is `{"type": "string", "pattern": …}`. All amount, quantity, price and FX rate fields use it.
  - Routes (route name → method and path):
    - core: `health` GET `/api/v1/health` → `{"status": "ok"}`; `get_settings` GET `/api/v1/settings` → `{"timezone": …}`; `update_settings` PUT `/api/v1/settings` (the timezone must be a valid IANA name, otherwise 422).
    - finance (prefix `/api/v1/finance`):
      - `get_finance_settings` GET `/settings` → `{"reporting_currency": "GBP"}`; `update_finance_settings` PUT `/settings` (the currency must be an existing fiat).
      - `list_instruments` GET `/instruments?include_hidden=false`; `create_instrument` POST `/instruments` → 201; `update_instrument` PATCH `/instruments/{instrument_id}` (body: `{"hidden": bool}`).
      - `list_accounts` GET `/accounts`; `create_account` POST `/accounts` → 201 (`institution_name` in the body is turned into an entity id via `get_or_create_organization`); `rename_account` PATCH `/accounts/{account_id}`; `close_account` POST `/accounts/{account_id}/close` (body: `{"closed_on": "YYYY-MM-DD"}`).
      - `record_snapshot` POST `/accounts/{account_id}/snapshots` → 201.
      - `post_transaction` POST `/transactions` → 201 (at least two postings); `void_transaction` POST `/transactions/{transaction_id}/void`.
      - `record_price` POST `/prices` → 204 (body: `instrument_id`, `quote_currency` (ISO code), `price`, `as_of_date`); `record_fx_rate` POST `/fx-rates` → 204 (body: `base`, `quote` (ISO codes), `rate`, `as_of_date`).
      - `get_holdings` GET `/holdings?date=YYYY-MM-DD` (defaults to today in the reporting timezone); `get_net_worth` GET `/net-worth?start=&end=` (defaults to the 365 days up to today).
  - Every write runs inside `command_context(session, actor, "<route name>", <the body as JSON>)`.
  - `python -m lararium.api.export_openapi`: writes the OpenAPI document to stdout with `sort_keys=True` and `indent=2`.
  - Test fixtures (`tests/api/conftest.py`): `client` uses `dev` mode; `tailscale_client` uses `tailscale` mode with `owner_login="me@example.com"`. Both override `get_session` so that it returns the test `session`, and send requests through `httpx.ASGITransport`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/api/test_core_api.py
async def test_health(client) -> None:
    response = await client.get("/api/v1/health")
    assert response.json() == {"status": "ok"}


async def test_tailscale_mode_requires_owner_header(tailscale_client) -> None:
    assert (await tailscale_client.get("/api/v1/finance/accounts")).status_code == 401
    ok = await tailscale_client.get(
        "/api/v1/finance/accounts", headers={"Tailscale-User-Login": "me@example.com"}
    )
    assert ok.status_code == 200


async def test_writes_record_actor_in_audit_log(tailscale_client, session) -> None:
    from sqlalchemy import text
    await tailscale_client.post(
        "/api/v1/finance/accounts",
        headers={"Tailscale-User-Login": "me@example.com"},
        json={"name": "Manual bank", "ledger_type": "asset", "subtype": "bank", "balance_mode": "hybrid"},
    )
    row = (await session.execute(
        text("select actor_type, actor_id, command from core.audit_log")
    )).one()
    assert tuple(row) == ("user", "me@example.com", "create_account")
```

```python
# backend/tests/api/test_finance_api.py
import json

from lararium.api.export_openapi import render_openapi


async def _gbp_id(client) -> str:
    instruments = (await client.get("/api/v1/finance/instruments")).json()
    return next(i["id"] for i in instruments if i["symbol"] == "GBP")


async def _account(client, **overrides) -> str:
    body = {"name": "Bank", "ledger_type": "asset", "subtype": "bank", "balance_mode": "snapshots"}
    response = await client.post("/api/v1/finance/accounts", json=body | overrides)
    assert response.status_code == 201
    return response.json()["id"]


async def test_create_and_list_accounts(client) -> None:
    await _account(client, name="Current account", institution_name="Example Bank")
    names = [a["name"] for a in (await client.get("/api/v1/finance/accounts")).json()]
    assert "Current account" in names


async def test_amounts_must_be_strings(client) -> None:
    account_id, gbp_id = await _account(client), await _gbp_id(client)
    response = await client.post(
        f"/api/v1/finance/accounts/{account_id}/snapshots",
        json={"instrument_id": gbp_id, "quantity": 0.1, "as_of": "2026-11-10T12:00:00Z"},
    )
    assert response.status_code == 422


async def test_extreme_precision_round_trip(client) -> None:
    account_id = await _account(client, subtype="wallet")
    token = (await client.post("/api/v1/finance/instruments", json={
        "kind": "crypto", "symbol": "TKN", "name": "Token", "decimals": 18,
        "quote_currency": "GBP", "identifiers": [],
    })).json()
    await client.post("/api/v1/finance/prices", json={
        "instrument_id": token["id"], "quote_currency": "GBP", "price": "1",
        "as_of_date": "2026-11-10",
    })
    await client.post(f"/api/v1/finance/accounts/{account_id}/snapshots", json={
        "instrument_id": token["id"], "quantity": "123456789012.123456789012345678",
        "as_of": "2026-11-10T12:00:00Z",
    })
    holdings = (await client.get("/api/v1/finance/holdings", params={"date": "2026-11-10"})).json()
    [row] = [h for h in holdings if h["symbol"] == "TKN"]
    assert row["quantity"] == "123456789012.123456789012345678"
    assert row["value"] == "123456789012.123456789012345678"


async def test_unbalanced_transaction_returns_residuals(client) -> None:
    a, b, gbp_id = await _account(client, name="A"), await _account(client, name="B"), await _gbp_id(client)
    response = await client.post("/api/v1/finance/transactions", json={
        "occurred_at": "2026-11-10T12:00:00Z", "description": "bad",
        "postings": [
            {"account_id": a, "instrument_id": gbp_id, "quantity": "-100.00"},
            {"account_id": b, "instrument_id": gbp_id, "quantity": "99.99"},
        ],
    })
    assert response.status_code == 422
    assert response.json()["detail"] == {"error": "ledger_imbalance", "residuals": {gbp_id: "-0.01"}}


def test_openapi_export_is_deterministic() -> None:
    first, second = render_openapi(), render_openapi()
    assert first == second
    assert "/api/v1/finance/net-worth" in json.loads(first)["paths"]
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `cd backend && uv run pytest tests/api -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'lararium.api.app'`

- [ ] **Step 3: Implement `registry.py`, `schemas.py`, `finance/api.py`, `core_routes.py`, `deps.py`, `app.py` and `export_openapi.py`**

`export_openapi.py` provides `render_openapi() -> str`: it builds the app with `Settings(database_url="postgresql+psycopg://openapi@localhost/unused", auth_mode="dev")` and only generates the schema, without connecting to the database (the engine is created lazily); `main()` prints that string.

- [ ] **Step 4: Run them and confirm they pass**

Run: `make backend-check`
Expected: all pass (including `test_no_float_in_source`)

- [ ] **Step 5: Commit**

```bash
git add backend/
git commit -m "feat: add versioned REST API with audit-aware commands"
```

---

### Task 10: Web UI

**Files:**
- Create: under `web/`: `package.json`, `vite.config.ts`, `tsconfig.json`, `index.html`, `openapi-ts.config.ts`, `openapi.json` (generated by command), `src/main.tsx`, `src/router.tsx`, `src/api/` (generated by command), `src/lib/format.ts`, `src/components/NetWorthChart.tsx`, `src/pages/NetWorthPage.tsx`, `src/pages/AccountsPage.tsx`, `src/pages/InstrumentsPage.tsx`; shadcn components (button, card, input, label, select, table, checkbox)
- Modify: `Makefile` (add `openapi`, `openapi-check`, `web-check`; `check` depends on `backend-check openapi-check web-check`)
- Test: `web/src/lib/format.test.ts`, `web/src/pages/AccountsPage.test.tsx`, `web/src/pages/NetWorthPage.test.tsx`

**Interfaces:**
- Consumes: the functions generated from OpenAPI, named by converting Task 9's route names to camelCase: `listAccounts`, `createAccount`, `recordSnapshot`, `listInstruments`, `createInstrument`, `updateInstrument`, `getHoldings`, `getNetWorth`, `getFinanceSettings`, and so on.
- Produces:
  - `formatMoney(value: string, currency: string, locale = "en-GB"): string`: formats with `Intl.NumberFormat`, passing the string straight in without going through Number.
  - `formatQuantity(value: string, maxDecimals: number, locale = "en-GB"): string`: `minimumFractionDigits: 0`.
  - `negateDecimalString(value: string): string`: pure string negation; `"0"` stays `"0"`.
  - Pages: `/` net worth, `/accounts` accounts, `/instruments` instruments. The top navigation labels are, in order, "Net worth", "Accounts", "Instruments".
  - The frontend converts strings to numbers only for charting (inside `NetWorthChart`); nowhere else does it do any arithmetic on amounts.
  - The dev server proxies `/api` to `http://127.0.0.1:8000`.

- [ ] **Step 1: Initialize the frontend and generate the API client**

Create a Vite + React + TypeScript project with pnpm; install TanStack Router, TanStack Query, echarts and Tailwind v4 (`@tailwindcss/vite`), and add the components listed above with the shadcn CLI; install vitest, jsdom, @testing-library/react, @testing-library/user-event and @hey-api/openapi-ts as dev dependencies. `openapi-ts.config.ts`: input `openapi.json`, output `src/api`, using the fetch client plugin. Scripts in `package.json`: `typecheck` (`tsc --noEmit`), `test` (`vitest`), `openapi-ts`.

Run: `make openapi`
Expected: `web/openapi.json` and `web/src/api/` are generated, and `export const getNetWorth` can be found in them

- [ ] **Step 2: Write the failing tests**

```ts
// web/src/lib/format.test.ts
import { describe, expect, it } from "vitest";
import { formatMoney, formatQuantity, negateDecimalString } from "./format";

describe("formatMoney", () => {
  it("formats GBP from a string", () => {
    expect(formatMoney("1234567.891", "GBP")).toBe("£1,234,567.89");
  });
});

describe("formatQuantity", () => {
  it("keeps 18 decimals", () => {
    expect(formatQuantity("0.000000000000000001", 18)).toBe("0.000000000000000001");
  });
});

describe("negateDecimalString", () => {
  it.each([["500", "-500"], ["-12.30", "12.30"], ["0", "0"]])("%s -> %s", (input, expected) => {
    expect(negateDecimalString(input)).toBe(expected);
  });
});
```

```tsx
// web/src/pages/AccountsPage.test.tsx
// Replace the generated client with vi.mock("../api"):
//   listAccounts returns one account, "Credit card", whose ledger_type is "liability";
//   listInstruments returns GBP; recordSnapshot is vi.fn().
// Render AccountsPage; in the snapshot form on the "Credit card" row, type "500" into the "Amount owed" input and click "Save snapshot".
it("sends liability balances as negative quantities", async () => {
  // …render and interact…
  expect(recordSnapshot).toHaveBeenCalledWith(
    expect.objectContaining({ body: expect.objectContaining({ quantity: "-500" }) }),
  );
});
```

```tsx
// web/src/pages/NetWorthPage.test.tsx
// vi.mock("../api"): getNetWorth returns { value: "1500.00", missing_count: 2 } for the last day;
// getHoldings returns an empty array; getFinanceSettings returns { reporting_currency: "GBP" }.
// Return values are wrapped in { data: … }, matching the generated client's format (the same applies to the AccountsPage test).
// vi.mock("../components/NetWorthChart") and replace it with an empty component, to avoid running ECharts in jsdom.
it("shows the total and warns about missing prices", async () => {
  // …render…
  expect(await screen.findByText("£1,500.00")).toBeInTheDocument();
  expect(screen.getByText("2 items are missing a price or FX rate and are excluded from net worth")).toBeInTheDocument();
});
```

In the snapshot form for asset accounts, the quantity input is labelled "Quantity"; for liability accounts it is labelled "Amount owed", and the value is negated with `negateDecimalString` before submitting.

- [ ] **Step 3: Run them and confirm they fail**

Run: `cd web && pnpm test --run`
Expected: FAIL, reporting that `./format` and the two page modules can't be found

- [ ] **Step 4: Implement `format.ts`, the three pages, `NetWorthChart` (ECharts line chart), routing and navigation**

- Net worth page: the total in the reporting currency (the last day's value from `getNetWorth`); the banner above when `missing_count > 0` (for exactly 1 item it reads "1 item is missing a price or FX rate and is excluded from net worth"); a line chart of the last 365 days; a holdings table grouped by account (Account, Instrument, Quantity, Value, Status).
- Accounts page: the account list; a "New account" form (Name, Ledger type, Subtype, Balance mode, Institution name, Tax wrapper); a snapshot form for each account (Instrument dropdown, Quantity or Amount owed, As of (defaults to now), "Save snapshot" button).
- Instruments page: the instrument list, with a "Hide" checkbox on each row; a "New instrument" form (Kind, Symbol, Name, Decimals, Quote currency, CoinGecko id (optional)).

- [ ] **Step 5: Run them and confirm they pass**

Run: `make check`
Expected: backend checks, the OpenAPI consistency check (`git diff --exit-code` prints nothing), and the frontend typecheck and tests all pass

- [ ] **Step 6: Commit**

```bash
git add Makefile web/
git commit -m "feat: add web UI for net worth, accounts and instruments"
```

---

### Task 11: Deployment, backups and CI

**Files:**
- Create: `infra/compose.yaml`, `infra/env.example`, `infra/caddy/Caddyfile`, `infra/backup/backup.sh`, `infra/backup/restore.sh`, `backend/Dockerfile`, `web/Dockerfile`, `.github/workflows/ci.yml`, `README.md`, `README.zh-CN.md`
- Modify: `Makefile` (add `up`, `down`, `backup`)

**Interfaces:**
- Consumes: `roles.sql` and `00-roles.sh` from Task 2, `create_app` from Task 9, the frontend build output from Task 10.
- Produces:
  - Compose services: `db` (`postgres:18`), `migrate` (runs `alembic upgrade head` once, as `lararium_owner`), `api` (uvicorn, as `lararium_backend`, `LARARIUM_AUTH_MODE=tailscale`), `web` (Caddy serving the SPA and forwarding `/api/*` to api), bound only to `127.0.0.1:8080`.
  - Access: run `tailscale serve --bg 8080` on the host; Tailscale adds the `Tailscale-User-Login` header to requests.
  - Environment variables (`infra/env.example`): `POSTGRES_PASSWORD`, `LARARIUM_OWNER_PASSWORD`, `LARARIUM_BACKEND_PASSWORD`, `LARARIUM_OWNER_LOGIN`, `BACKUP_DIR`, plus the optional `RESTIC_REPOSITORY` and `RESTIC_PASSWORD`. `infra/.env` is not tracked by git; the README explains how to encrypt it with SOPS + age, and how to decrypt it again.

- [ ] **Step 1: Write `compose.yaml`, the two Dockerfiles and the `Caddyfile`**

```yaml
# infra/compose.yaml
name: lararium
services:
  db:
    image: postgres:18
    environment:
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      LARARIUM_OWNER_PASSWORD: ${LARARIUM_OWNER_PASSWORD}
      LARARIUM_BACKEND_PASSWORD: ${LARARIUM_BACKEND_PASSWORD}
    volumes:
      - pgdata:/var/lib/postgresql
      - ./postgres:/lararium:ro
      - ./postgres/init:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d lararium"]
      interval: 5s
      retries: 30
  migrate:
    build: ../backend
    command: ["alembic", "upgrade", "head"]
    environment:
      LARARIUM_DATABASE_URL: postgresql+psycopg://lararium_backend:${LARARIUM_BACKEND_PASSWORD}@db:5432/lararium
      LARARIUM_MIGRATION_DATABASE_URL: postgresql+psycopg://lararium_owner:${LARARIUM_OWNER_PASSWORD}@db:5432/lararium
    depends_on:
      db: { condition: service_healthy }
  api:
    build: ../backend
    command: ["uvicorn", "lararium.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
    environment:
      LARARIUM_DATABASE_URL: postgresql+psycopg://lararium_backend:${LARARIUM_BACKEND_PASSWORD}@db:5432/lararium
      LARARIUM_AUTH_MODE: tailscale
      LARARIUM_OWNER_LOGIN: ${LARARIUM_OWNER_LOGIN}
    depends_on:
      migrate: { condition: service_completed_successfully }
  web:
    build:
      context: ..
      dockerfile: web/Dockerfile
    ports: ["127.0.0.1:8080:80"]
    depends_on: [api]
volumes:
  pgdata: {}
```

```
# infra/caddy/Caddyfile
:80 {
	handle /api/* {
		reverse_proxy api:8000
	}
	handle {
		root * /srv
		try_files {path} /index.html
		file_server
	}
}
```

`backend/Dockerfile`: based on `python:3.13-slim`; installs with uv from `uv.lock` (without dev dependencies) and copies `migrations/` and `alembic.ini` into the image as well. `web/Dockerfile` (the build context is the repository root): a `node:24` stage first runs `pnpm install --frozen-lockfile && pnpm build` in `web/`, then `web/dist/` is copied into `/srv` in a `caddy:2` image, and `infra/caddy/Caddyfile` is copied to `/etc/caddy/Caddyfile`.

- [ ] **Step 2: Start the stack and smoke-test it**

Run:
```bash
cp infra/env.example infra/.env   # fill in passwords for local testing; set LARARIUM_OWNER_LOGIN to me@example.com
docker compose -f infra/compose.yaml --env-file infra/.env up -d --build
curl -s http://127.0.0.1:8080/api/v1/health
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/api/v1/finance/accounts
curl -s -H "Tailscale-User-Login: me@example.com" http://127.0.0.1:8080/api/v1/finance/accounts
```
Expected: outputs `{"status":"ok"}`, `401`, `[]` in that order; opening `http://127.0.0.1:8080/` in a browser shows the net worth page (the API returning 401 without the header is expected)

- [ ] **Step 3: Write `backup.sh` and `restore.sh`, and verify one restore**

Both scripts find the stack with `docker compose -f "$(dirname "$0")/../compose.yaml" --env-file "$(dirname "$0")/../.env"`.

- `backup.sh`: `… exec -T db pg_dump -U postgres -d lararium --format=custom`, written to `$BACKUP_DIR/lararium-<UTC timestamp>.dump`. When `RESTIC_REPOSITORY` is set, it then also runs `restic backup "$BACKUP_DIR"`.
- `restore.sh <dump file> <target database name>`: creates a new target database owned by `lararium_owner`, then restores into it with `pg_restore --no-owner --role=lararium_owner`.

Run:
```bash
infra/backup/backup.sh
infra/backup/restore.sh "$(ls -t backups/lararium-*.dump | head -1)" lararium_restore_check
docker compose -f infra/compose.yaml exec -T db psql -U postgres -d lararium_restore_check -tAc "select count(*) from finance.instruments"
```
Expected: the last command prints `10` (the number of seeded currencies)

- [ ] **Step 4: Write the CI and the README**

`.github/workflows/ci.yml`: on `ubuntu-latest`, install uv, pnpm 11 and Node 24, then run `make check` (testcontainers uses the runner's built-in Docker). `README.md` (English) covers three things clearly: how to start local development (`make check`, `uv run uvicorn … --factory --reload`, `pnpm dev`); server deployment (`make up`, `tailscale serve --bg 8080`); and the commands for SOPS-encrypting `.env` and for backup and restore. `README.zh-CN.md` is its Chinese translation. Each of the two files links to the other at the top.

Run: `make check`
Expected: all pass

- [ ] **Step 5: Confirm the image builds for amd64**

Run: `docker buildx build --platform linux/amd64 backend`
Expected: the build succeeds (the local machine is arm64, which Step 2 already covered)

- [ ] **Step 6: Commit**

```bash
git add infra/ backend/Dockerfile web/Dockerfile .github/ README.md README.zh-CN.md Makefile
git commit -m "chore: add docker compose deployment, backups and CI"
```
