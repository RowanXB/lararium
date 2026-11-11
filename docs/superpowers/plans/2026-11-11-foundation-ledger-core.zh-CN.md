# 基础与账本核心 Implementation Plan

**语言：** [English](./2026-11-11-foundation-ledger-core.md) | 简体中文

本文是英文版的中文译文；两者如有出入，以英文版为准。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搭好 Lararium 的骨架和账本核心：能手动建账户、录入余额快照和持仓数量、记复式交易、录入价格和汇率，并在网页上看到任意一天的净资产。

**Architecture:** Python 模块化单体：`lararium.core` 是公共层，`lararium.modules.finance` 是财务模块，`lararium.api` 把各模块挂到 FastAPI 上。所有数据存 PostgreSQL 18。估值逻辑写成带版本号的 SQL 函数，界面、以后的 BI 和 MCP 共用同一套。前端是 React + Vite 的 SPA，API 客户端从 OpenAPI 生成。

**Tech Stack:** Python 3.13、uv、FastAPI、Pydantic v2、SQLAlchemy 2（async，psycopg 3）、Alembic、pytest + pytest-asyncio + testcontainers；PostgreSQL 18；React、Vite、TanStack Router / Query、shadcn/ui、ECharts、@hey-api/openapi-ts、Vitest + Testing Library；Docker Compose、Caddy。

**Spec:** `docs/superpowers/specs/2026-11-11-financial-gate-phase1-design.md`（本计划覆盖其中的 M0 和 M1）

**计划系列：** 第一阶段按里程碑拆成几份计划，每份做完都是能运行、能测试的软件。后一份等前一份落地后再写，这样能基于真实代码里的接口：

1. **基础与账本核心**（本计划：M0、M1）
2. 手动导入（M2）
3. connector 框架与 Monzo（M3）
4. 转账匹配（M4）
5. 投资与加密数据源（M5–M8：IBKR、Binance、自托管钱包、券商）
6. 运维收尾（M9）

规格里的公共对象，本计划只建账本核心用得上的：实体、设置、事件、审计日志。文档、关联、连接、原始记录、webhook 收件箱、凭证，留到用得上它们的计划（2、3）里再建；`webhook_in` 和 `mcp_ro` 两个数据库角色也一样。

## Global Constraints

- Python 3.13+；用 uv 管理依赖；ruff 做检查和格式化；pyright 以 strict 模式检查 `src/`。
- PostgreSQL 18+，用到 `uuidv7()`、`NUMERIC`、`JSONB`、`LISTEN/NOTIFY`。
- 金额和数量：数据库里用不限精度的 `NUMERIC`，Python 里用 `Decimal`，JSON 里用字符串。整条链路禁止浮点数：源码里不出现 `float`，数据库里不出现 `real` 或 `double precision` 列。`test_no_float_in_source` 连注释和文档字符串也扫描，所以 `src/` 里连这个单词都不要写。
- 所有 ID 用 UUIDv7（数据库默认值 `uuidv7()`）。
- 机器时间戳存 UTC 的 `timestamptz`；日期用 `date` 单独存；`core.timezone` 默认 `Europe/London`。
- 报告币种默认 GBP（设置项 `finance.reporting_currency`）。
- 依赖方向：`lararium.core` 不能 import `lararium.modules` 或 `lararium.api`；`lararium.modules` 不能 import `lararium.api`。由 import-linter 检查。第二个模块出现时，再加一条“模块之间互相独立”的 independence 合约。
- 系统只读：不写任何下单、转账、提现功能。
- 本计划不开任何公网入口；只通过 Tailscale 访问。
- 不用 Next.js、SQLite、Redis、Kafka。
- Docker 镜像要能在 amd64 和 arm64 上构建。
- 分支名 `feat/claude-…`；提交信息格式 `<type>: <summary>`，type 取 feat、fix、chore、refactor、docs、style、perf、test 之一。提交时用 `local-signed-git-commit` skill 处理签名。
- 开发环境要求：Docker（带 buildx，daemon 在运行）、uv、Node 24、pnpm 11、Python 3.13、git。
- 开源：许可证 AGPL-3.0-or-later（`pyproject.toml` 和 `web/package.json` 都写上）。仓库里不放任何真实个人数据：测试数据全部手写合成，代码里不内置任何凭证；个人笔记放在不进 git 的 `private/`。新增依赖要确认许可证和 AGPL-3.0 兼容。
- 文档双语：英文为主，中文译文以 `*.zh-CN.md` 放在对应英文文件旁边。界面文案用英文。

## Review Focus

这五类输入或情况，规格里隐含了要求，但没有哪个任务的主线测试会碰到，最容易让使用者踩坑。每一条都已经把测试加到了负责它的任务里：

- **英国夏令时边界**：UTC 时间 2026-03-31 23:30 记录的快照，按伦敦时间属于 4 月 1 日（任务 7：`test_snapshot_as_of_date_uses_reporting_timezone`）。
- **同一天多次快照**：同一天录了两次余额，取当天最晚的那次（任务 8：`test_same_day_snapshots_latest_time_wins`）。
- **缺汇率的币种**（比如 NGN）：标记为缺汇率，不当成 0 计入净值，并在净值里报告缺失条数（任务 8：`test_missing_fx_is_flagged_not_zero`）。
- **客户端用 JSON 数字传金额**：返回 422，而不是悄悄经过浮点数（任务 9：`test_amounts_must_be_strings`）。
- **极端精度**：12 位整数加 18 位小数的数量，经过数据库和 API 往返后一字不差（任务 9：`test_extreme_precision_round_trip`）。

---

## 文件结构

```
lararium/
├── Makefile                      # check / backend-check / openapi / web-check / up
├── README.md                     # 怎么在本机和服务器上跑起来
├── README.zh-CN.md               # README.md 的中文译文
├── .gitignore
├── .github/workflows/ci.yml
├── infra/
│   ├── compose.yaml              # db、migrate、api、web
│   ├── env.example
│   ├── caddy/Caddyfile
│   ├── postgres/roles.sql        # 角色和数据库，测试和部署共用
│   ├── postgres/init/00-roles.sh
│   └── backup/backup.sh, restore.sh
├── backend/
│   ├── pyproject.toml, uv.lock, alembic.ini, Dockerfile
│   ├── migrations/env.py
│   ├── migrations/versions/      # 0001_core_baseline … 0006_finance_valuation_v1
│   ├── src/lararium/
│   │   ├── __init__.py           # __version__
│   │   ├── config.py             # Settings（LARARIUM_ 开头的环境变量）
│   │   ├── db.py                 # engine 和 session 工厂
│   │   ├── core/                 # 公共层，不认识任何模块
│   │   │   ├── actors.py         # Actor
│   │   │   ├── json_types.py     # JSONValue（不含 float）
│   │   │   ├── errors.py
│   │   │   ├── timeutil.py       # local_date
│   │   │   ├── models.py         # core 表的 ORM 映射
│   │   │   ├── commands.py       # 命令上下文：审计日志 + 事件
│   │   │   ├── settings_store.py
│   │   │   └── entities.py
│   │   ├── modules/
│   │   │   ├── registry.py       # ModuleSpec、MODULES
│   │   │   └── finance/
│   │   │       ├── __init__.py   # MODULE
│   │   │       ├── errors.py, money.py, models.py
│   │   │       ├── instruments.py, accounts.py, ledger.py
│   │   │       ├── snapshots.py, pricing.py, valuation.py
│   │   │       ├── schemas.py    # API 用的 Pydantic 模型
│   │   │       └── api.py        # /api/v1/finance 路由
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
        ├── api/                  # 生成的客户端，不手改
        ├── lib/format.ts (+ format.test.ts)
        ├── components/ui/…       # shadcn 组件
        ├── components/NetWorthChart.tsx
        └── pages/NetWorthPage.tsx, AccountsPage.tsx, InstrumentsPage.tsx (+ 各自的 .test.tsx)
```

---

### Task 1: 仓库与后端工具链

**Files:**
- Create: `backend/pyproject.toml`, `backend/src/lararium/__init__.py`, `backend/src/lararium/core/__init__.py`, `backend/src/lararium/modules/__init__.py`, `backend/src/lararium/api/__init__.py`, `Makefile`
- Test: `backend/tests/test_package.py`, `backend/tests/test_no_floats.py`

**Interfaces:**
- Produces: 包 `lararium`（`__version__ = "0.1.0"`）；命令 `make backend-check`，依次运行 `ruff check`、`ruff format --check`、`pyright`、`lint-imports`、`pytest`。

- [ ] **Step 1: 建分支，并提交设计文档和本计划**

```bash
git switch -c feat/claude-foundation-ledger-core
git add docs/ LICENSE .gitignore
git commit -m "docs: add license, phase-1 design and foundation plan"
```

- [ ] **Step 2: 建 `backend/pyproject.toml` 和四个空包**

`pyproject.toml` 的内容：
- `[project]`：name `lararium`，version `0.1.0`，`requires-python = ">=3.13"`，`license = "AGPL-3.0-or-later"`；依赖 `fastapi`、`uvicorn[standard]`、`pydantic>=2`、`pydantic-settings`、`sqlalchemy[asyncio]>=2.0`、`psycopg[binary]>=3.2`、`alembic`、`tzdata`。
- `[dependency-groups] dev`：`pytest`、`pytest-asyncio`、`httpx`、`testcontainers[postgres]`、`ruff`、`pyright`、`import-linter`。
- 构建后端用 hatchling，包目录是 `src/lararium`。
- `[tool.pytest.ini_options]`：`asyncio_mode = "auto"`、`asyncio_default_fixture_loop_scope = "session"`、`asyncio_default_test_loop_scope = "session"`、`testpaths = ["tests"]`。
- `[tool.ruff]`：`line-length = 100`、`target-version = "py313"`；lint 规则选 `E, F, I, UP, B, ASYNC, DTZ, RUF`（DTZ 禁止不带时区的 datetime，RUF032 禁止用浮点字面量构造 Decimal）。
- `[tool.pyright]`：`typeCheckingMode = "strict"`、`include = ["src"]`、`pythonVersion = "3.13"`。
- `[tool.importlinter]`：`root_package = "lararium"`，两条 forbidden 合约，内容见 Global Constraints 里的依赖方向。

`.gitignore` 和 `LICENSE`（AGPL-3.0 全文）已经在仓库根目录，不用再建；`.gitignore` 已包含 `private/`、`backups/`、`infra/.env`、`.venv/`、`__pycache__/`、`node_modules/`、`web/dist/`。

- [ ] **Step 3: 写失败的测试**

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

- [ ] **Step 4: 运行，确认失败**

Run: `cd backend && uv sync && uv run pytest -q`
Expected: FAIL，`AttributeError: module 'lararium' has no attribute '__version__'`

- [ ] **Step 5: 在 `src/lararium/__init__.py` 里写 `__version__ = "0.1.0"`；在根目录 `Makefile` 里加 `backend-check` 目标**

`backend-check` 在 `backend/` 目录下依次运行：`uv run ruff check .`、`uv run ruff format --check .`、`uv run pyright`、`uv run lint-imports`、`uv run pytest -q`。`check` 目标暂时只依赖 `backend-check`。

- [ ] **Step 6: 运行全部检查**

Run: `make backend-check`
Expected: ruff、pyright 无错误；lint-imports 显示 2 条合约都 KEPT；pytest 显示 `2 passed`

- [ ] **Step 7: 提交**

```bash
git add Makefile backend/
git commit -m "chore: scaffold backend package and tooling"
```

---

### Task 2: 测试数据库与迁移基线

**Files:**
- Create: `infra/postgres/roles.sql`, `infra/postgres/init/00-roles.sh`
- Create: `backend/src/lararium/config.py`, `backend/src/lararium/db.py`
- Create: `backend/alembic.ini`, `backend/migrations/env.py`, `backend/migrations/versions/0001_core_baseline.py`
- Test: `backend/tests/conftest.py`, `backend/tests/core/test_baseline.py`

**Interfaces:**
- Produces:
  - `class Settings(BaseSettings)`（`env_prefix="LARARIUM_"`）：`database_url: str`、`migration_database_url: str | None = None`、`auth_mode: Literal["dev", "tailscale"] = "tailscale"`、`owner_login: str | None = None`（tailscale 模式下没有 `owner_login` 时校验报错；测试和 OpenAPI 导出显式传 `auth_mode="dev"`）；`def get_settings() -> Settings`（带 `lru_cache`）。
  - `def create_engine(url: str) -> AsyncEngine`；`def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]`（`expire_on_commit=False`）。
  - 数据库角色：`lararium_owner`（拥有 schema，只用来跑迁移）、`lararium_backend`（应用使用，只有增删改查权限）。数据库名 `lararium`。
  - 测试 fixture：`owner_url`、`backend_url`（session 级，`postgresql+psycopg://…` 字符串）；`session`（每个测试一个 `AsyncSession`，以 `lararium_backend` 连接，外层事务在测试结束时回滚）。

- [ ] **Step 1: 写 `infra/postgres/roles.sql`**（测试和部署共用这一份）

```sql
CREATE ROLE lararium_owner LOGIN PASSWORD :'owner_pw';
CREATE ROLE lararium_backend LOGIN PASSWORD :'backend_pw';
CREATE DATABASE lararium OWNER lararium_owner;
\connect lararium
REVOKE ALL ON SCHEMA public FROM PUBLIC;
```

`00-roles.sh` 用 `psql -v ON_ERROR_STOP=1 -v owner_pw="$LARARIUM_OWNER_PASSWORD" -v backend_pw="$LARARIUM_BACKEND_PASSWORD" -U "$POSTGRES_USER" -f /lararium/roles.sql` 执行它。

- [ ] **Step 2: 写 `tests/conftest.py`**

- session 级 fixture：启动 `PostgresContainer("postgres:18")`，把 `infra/postgres/` 只读挂载到容器的 `/lararium`，在容器里执行 `psql -v ON_ERROR_STOP=1 -v owner_pw=owner -v backend_pw=backend -U <容器超级用户> -f /lararium/roles.sql`，断言退出码为 0。
- 由此得出 `owner_url`（`lararium_owner:owner`）和 `backend_url`（`lararium_backend:backend`），都指向 `lararium` 库。
- session 级 fixture 用 `owner_url` 跑 `alembic upgrade head`（通过 `alembic.config.Config` 设置 `sqlalchemy.url`）。
- `session` fixture：用 `backend_url` 建 async engine，开一个连接并 `begin()` 外层事务，`AsyncSession(bind=conn, join_transaction_mode="create_savepoint")`，测试结束后回滚外层事务。这样被测代码里的 `commit()` 只释放 savepoint，不会污染其他测试。

- [ ] **Step 3: 写失败的测试**

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

- [ ] **Step 4: 运行，确认失败**

Run: `cd backend && uv run pytest tests/core/test_baseline.py -q`
Expected: FAIL（alembic 找不到配置或迁移；如果 schema 已建，则 `test_schemas_exist` 断言失败）

- [ ] **Step 5: 写 `config.py`、`db.py`、Alembic 配置和迁移 `0001_core_baseline`**

`migrations/env.py` 用同步 engine，从 `Settings.migration_database_url` 或 alembic 配置里的 `sqlalchemy.url` 读连接串。迁移一律用 `op.execute` 写原生 SQL。`0001_core_baseline` 的内容：

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

另外三件事：
- 插入 self 实体（`kind='person'`、`name='Me'`、`is_self=true`），再用第二条语句把它的 `owner_entity_id` 设成它自己的 id。
- 建触发器函数 `core.notify_event()`：`AFTER INSERT ON core.events`，执行 `pg_notify('core_events', NEW.id::text)`。
- 写好 downgrade：删掉两个 schema。

- [ ] **Step 6: 运行，确认通过**

Run: `cd backend && uv run pytest tests/core/test_baseline.py -q`
Expected: `5 passed`

- [ ] **Step 7: 提交**

```bash
git add infra/postgres backend/
git commit -m "feat: add database roles, test harness and core baseline migration"
```

---

### Task 3: 公共层——命令上下文、事件、审计、设置、实体

**Files:**
- Create: `backend/src/lararium/core/{actors.py, json_types.py, errors.py, timeutil.py, models.py, commands.py, settings_store.py, entities.py}`
- Modify: `backend/tests/conftest.py`（加 `actor` 和 `run` 两个 fixture）
- Test: `backend/tests/core/test_commands.py`, `backend/tests/core/test_settings_store.py`, `backend/tests/core/test_entities.py`, `backend/tests/core/test_timeutil.py`

**Interfaces:**
- Consumes: 任务 2 的 `session`、`backend_url` fixture。
- Produces:
  - `ActorType = Literal["user", "rule", "connector", "agent", "system"]`；`@dataclass(frozen=True, slots=True) class Actor: type: ActorType; id: str`。
  - `JSONValue = None | bool | int | str | list["JSONValue"] | dict[str, "JSONValue"]`。这里故意不含 float：Decimal 要先转成字符串再放进去。
  - `class CommandContext`：属性 `session: AsyncSession`、`actor: Actor`、`correlation_id: UUID`；方法 `emit(event_type: str, subject_id: UUID | None, data: Mapping[str, JSONValue], version: int = 1) -> None`，事件的 `source` 取 `event_type` 第一个点之前的部分；方法 `record_change(key: str, value: JSONValue) -> None`。
  - `command_context(session: AsyncSession, actor: Actor, command: str, args: Mapping[str, JSONValue], correlation_id: UUID | None = None) -> AsyncContextManager[CommandContext]`：正常退出时写一行 `core.audit_log`，写入所有已 emit 的 `core.events`，然后 `session.commit()`；出现异常时 `session.rollback()`，什么都不留下，再把异常抛出去。
  - `class SettingNotFoundError(Exception)`（在 `core/errors.py`）。
  - `async def get_setting(session: AsyncSession, key: str) -> JSONValue`；`async def set_setting(ctx: CommandContext, key: str, value: JSONValue) -> None`（upsert）；`async def get_timezone(session: AsyncSession) -> ZoneInfo`（读 `core.timezone`）。
  - `def local_date(ts: datetime, tz: ZoneInfo) -> date`：`ts` 不带时区时抛 `ValueError`。
  - `async def get_self_entity_id(session: AsyncSession) -> UUID`；`async def get_or_create_organization(ctx: CommandContext, name: str) -> UUID`：名称或别名不区分大小写匹配，匹配到就返回已有实体，否则新建 organization，owner 设为 self。
  - 测试 fixture：`actor` 返回 `Actor("user", "test")`；`run(fn, *args, **kwargs)` 在 `command_context(session, actor, "test", {})` 里执行 `await fn(ctx, *args, **kwargs)` 并返回结果。

- [ ] **Step 1: 写失败的测试**

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

- [ ] **Step 2: 运行，确认失败**

Run: `cd backend && uv run pytest tests/core -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'lararium.core.commands'`

- [ ] **Step 3: 实现 `core/` 下这几个文件，并在 `conftest.py` 里加 `actor` 和 `run` fixture**

`models.py` 用 SQLAlchemy 2 的 typed declarative，把 core 的四张表映射出来；`id` 用 `server_default=text("uuidv7()")`。`command_context` 用 `@asynccontextmanager` 实现。

- [ ] **Step 4: 运行，确认通过**

Run: `cd backend && uv run pytest tests/core -q && uv run pyright && uv run lint-imports`
Expected: core 测试全部通过；pyright 0 个错误；import 合约全部 KEPT

- [ ] **Step 5: 提交**

```bash
git add backend/
git commit -m "feat: add command context with audit log, outbox events and settings"
```

---

### Task 4: 资产（instrument）与金额工具

**Files:**
- Create: `backend/src/lararium/modules/finance/{__init__.py, errors.py, money.py, models.py, instruments.py}`, `backend/migrations/versions/0002_finance_instruments.py`
- Test: `backend/tests/finance/conftest.py`, `backend/tests/finance/test_money.py`, `backend/tests/finance/test_instruments.py`

**Interfaces:**
- Consumes: `CommandContext`、`get_setting`、`get_self_entity_id`。
- Produces:
  - `def tolerance(decimals: int) -> Decimal`：返回最后一位小数的一半，即 `Decimal(1).scaleb(-decimals) / 2`。
  - `def to_plain_str(value: Decimal) -> str`：定点表示，不用科学计数法，保留原有的末尾 0。
  - `InstrumentKind = Literal["fiat", "equity", "etf", "fund", "bond", "crypto", "option", "cfd", "other"]`。
  - `IdentifierScheme = Literal["iso4217", "isin", "figi", "ticker_mic", "ibkr_conid", "etoro_id", "chain_contract", "coingecko"]`。
  - `@dataclass(frozen=True) class InstrumentIdentifier: scheme: IdentifierScheme; value: str`。
  - `@dataclass(frozen=True) class InstrumentRecord`：`id: UUID`、`kind: InstrumentKind`、`symbol: str`、`name: str`、`decimals: int`、`quote_instrument_id: UUID | None`、`hidden: bool`、`identifiers: tuple[InstrumentIdentifier, ...]`（按 scheme 排序）。
  - `def normalize_identifier(scheme: IdentifierScheme, value: str) -> str`：先去掉首尾空白；`iso4217`、`isin`、`figi` 转大写；`coingecko`、`chain_contract` 转小写；其余原样。
  - `async def create_instrument(ctx: CommandContext, *, kind: InstrumentKind, symbol: str, name: str, decimals: int, quote_instrument_id: UUID | None, identifiers: Sequence[InstrumentIdentifier]) -> InstrumentRecord`：emit `finance.instrument.created`。
  - `async def find_by_identifier(session: AsyncSession, scheme: IdentifierScheme, value: str) -> InstrumentRecord | None`
  - `async def get_currency(session: AsyncSession, iso_code: str) -> InstrumentRecord`：找不到时抛 `UnknownCurrencyError`。
  - `async def get_instrument(session: AsyncSession, instrument_id: UUID) -> InstrumentRecord`：找不到时抛 `InstrumentNotFoundError`。
  - `async def set_hidden(ctx: CommandContext, instrument_id: UUID, hidden: bool) -> InstrumentRecord`：emit `finance.instrument.updated`。
  - `async def list_instruments(session: AsyncSession, *, include_hidden: bool) -> list[InstrumentRecord]`：按 symbol 排序。
  - `finance/errors.py`：`FinanceError(Exception)` 作为基类，子类有 `UnknownCurrencyError`、`NotACurrencyError`、`InstrumentNotFoundError`、`DuplicateIdentifierError`、`AccountNotFoundError`、`AccountAlreadyClosedError`、`TransactionNotFoundError`，以及 `LedgerImbalanceError`（带属性 `residuals: dict[UUID, Decimal]`）。
  - 设置键常量 `REPORTING_CURRENCY_KEY = "finance.reporting_currency"`。
  - 测试 fixture（`tests/finance/conftest.py`）：`gbp`、`usd`、`eur`，分别是 `await get_currency(session, "GBP")` 等的结果。

- [ ] **Step 1: 写失败的测试**

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

- [ ] **Step 2: 运行，确认失败**

Run: `cd backend && uv run pytest tests/finance -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'lararium.modules.finance'`

- [ ] **Step 3: 写迁移 `0002_finance_instruments`**

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

种子数据：GBP、USD、EUR、CNY、HKD、CHF、CAD、AUD、SGD（小数位都是 2），JPY（小数位 0）。这些都是 `kind='fiat'`，`name` 用英文全称，owner 是 self 实体，每个都带一条 `iso4217` 标识，值等于 symbol。

- [ ] **Step 4: 实现 `money.py`、`errors.py`、`models.py`（instrument 相关部分）、`instruments.py`，在 `finance/__init__.py` 里先放一个空的 `MODULE` 占位**

`create_instrument` 在插入标识前先规范化；遇到主键冲突，捕获 `IntegrityError` 并转成 `DuplicateIdentifierError`。

- [ ] **Step 5: 运行，确认通过**

Run: `cd backend && uv run pytest tests/finance -q`
Expected: `7 passed`

- [ ] **Step 6: 提交**

```bash
git add backend/
git commit -m "feat: add instruments with identifiers and decimal helpers"
```

---

### Task 5: 账户

**Files:**
- Create: `backend/src/lararium/modules/finance/accounts.py`, `backend/migrations/versions/0003_finance_accounts.py`
- Modify: `backend/src/lararium/modules/finance/models.py`
- Test: `backend/tests/finance/test_accounts.py`；在 `backend/tests/finance/conftest.py` 里加 `make_account` fixture

**Interfaces:**
- Consumes: `get_or_create_organization`、`get_self_entity_id`、`CommandContext`。
- Produces:
  - `LedgerType = Literal["asset", "liability", "income", "expense", "equity"]`
  - `AccountSubtype = Literal["bank", "savings_pot", "broker", "exchange", "wallet", "in_transit", "manual_asset", "other"]`
  - `TaxWrapper = Literal["none", "isa", "sipp", "lisa", "gia"]`
  - `BalanceMode = Literal["transactions", "snapshots", "hybrid"]`
  - `@dataclass(frozen=True) class AccountRecord`：`id`、`name`、`ledger_type`、`subtype`、`institution_entity_id: UUID | None`、`owner_entity_id: UUID`、`tax_wrapper`、`parent_account_id: UUID | None`、`balance_mode`、`source_kind: Literal["manual", "connection"]`、`source_connection_id: UUID | None`、`status: Literal["open", "closed"]`、`opened_on: date | None`、`closed_on: date | None`。
  - `async def create_account(ctx, *, name: str, ledger_type: LedgerType, subtype: AccountSubtype, balance_mode: BalanceMode, institution_entity_id: UUID | None = None, tax_wrapper: TaxWrapper = "none", parent_account_id: UUID | None = None, opened_on: date | None = None) -> AccountRecord`：父账户不存在时抛 `AccountNotFoundError`；emit `finance.account.created`，`data={"account_id": str(id)}`。
  - `async def rename_account(ctx, account_id: UUID, name: str) -> AccountRecord`：emit `finance.account.updated`。
  - `async def close_account(ctx, account_id: UUID, closed_on: date) -> AccountRecord`：已经关闭的账户再关闭时抛 `AccountAlreadyClosedError`；emit `finance.account.closed`。
  - `async def get_account(session, account_id: UUID) -> AccountRecord`：找不到时抛 `AccountNotFoundError`。
  - `async def list_accounts(session) -> list[AccountRecord]`：按名称排序。
  - 测试 fixture `make_account(**overrides) -> AccountRecord`。默认值：`name="Test"`、`ledger_type="asset"`、`subtype="bank"`、`balance_mode="snapshots"`。

- [ ] **Step 1: 写失败的测试**

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

- [ ] **Step 2: 运行，确认失败**

Run: `cd backend && uv run pytest tests/finance/test_accounts.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'lararium.modules.finance.accounts'`

- [ ] **Step 3: 写迁移 `0003_finance_accounts`**

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
  source_connection_id uuid,  -- 指向 core.connections 的外键在计划 3 里加
  status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
  opened_on date,
  closed_on date,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((source_kind = 'manual') = (source_connection_id IS NULL)),
  CHECK ((status = 'closed') = (closed_on IS NOT NULL))
);
```

- [ ] **Step 4: 实现 `accounts.py`，补上 `models.py`，在 `tests/finance/conftest.py` 里加 `make_account`**

- [ ] **Step 5: 运行，确认通过**

Run: `cd backend && uv run pytest tests/finance -q`
Expected: 新增的 6 个测试和之前的测试全部通过

- [ ] **Step 6: 提交**

```bash
git add backend/
git commit -m "feat: add accounts with tax wrappers, parents and closing"
```

---

### Task 6: 复式账本

**Files:**
- Create: `backend/src/lararium/modules/finance/ledger.py`, `backend/migrations/versions/0004_finance_ledger.py`
- Modify: `backend/src/lararium/modules/finance/models.py`
- Test: `backend/tests/finance/test_ledger_residuals.py`, `backend/tests/finance/test_ledger.py`

**Interfaces:**
- Consumes: `tolerance`、`get_instrument`、`get_timezone`、`local_date`、`get_self_entity_id`、`make_account`、`gbp`、`usd`。
- Produces:
  - `TransactionStatus = Literal["pending", "booked", "inferred", "confirmed", "void"]`；`TransactionOrigin = Literal["connector", "manual", "inference", "rule"]`。
  - `@dataclass(frozen=True) class PostingInput`：`account_id: UUID`、`instrument_id: UUID`、`quantity: Decimal`、`cost_per_unit: Decimal | None = None`、`cost_instrument_id: UUID | None = None`、`price_per_unit: Decimal | None = None`、`price_instrument_id: UUID | None = None`、`is_fee: bool = False`、`memo: str = ""`。
  - `@dataclass(frozen=True) class TransactionInput`：`occurred_at: datetime`（必须带时区）、`description: str`、`postings: Sequence[PostingInput]`、`booking_date: date | None = None`、`status: TransactionStatus = "booked"`、`origin: TransactionOrigin = "manual"`、`occurred_tz: str | None = None`、`value_date: date | None = None`、`counterparty_entity_id: UUID | None = None`。
  - `PostingRecord`：`PostingInput` 的全部字段，加上 `id: UUID`、`position: int`。`TransactionRecord`：`id`、`occurred_at`、`booking_date`、`description`、`status`、`origin`，以及按 position 排序的 `postings: tuple[PostingRecord, ...]`。
  - `def compute_residuals(postings: Sequence[PostingInput]) -> dict[UUID, Decimal]`：纯函数。每条分录的“权重”这样算：有成本时是 `(cost_instrument_id, quantity × cost_per_unit)`；否则有价格时是 `(price_instrument_id, quantity × price_per_unit)`；都没有时是 `(instrument_id, quantity)`。按资产求和，只返回和不为 0 的项。
  - `async def post_transaction(ctx, tx: TransactionInput) -> TransactionRecord`：每项残差的绝对值都要 ≤ `tolerance(该资产的小数位)`，否则抛 `LedgerImbalanceError(residuals)`，而且什么都不写入。`booking_date` 为空时，用 `local_date(occurred_at, 报告时区)` 推出来。emit `finance.transaction.posted`。
  - `async def void_transaction(ctx, transaction_id: UUID) -> TransactionRecord`：把状态改成 `void`，emit `finance.transaction.voided`；找不到时抛 `TransactionNotFoundError`。
  - `async def find_unbalanced_transactions(session) -> list[UUID]`：在数据库里按同样的规则重新计算，跳过 void 的交易，返回不平衡的交易 id。以后的定时完整性任务会调用它。

- [ ] **Step 1: 写纯函数的失败测试**

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

- [ ] **Step 2: 运行，确认失败**

Run: `cd backend && uv run pytest tests/finance/test_ledger_residuals.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'lararium.modules.finance.ledger'`

- [ ] **Step 3: 在 `ledger.py` 里实现 `PostingInput` 和 `compute_residuals`**

- [ ] **Step 4: 运行，确认通过**

Run: `cd backend && uv run pytest tests/finance/test_ledger_residuals.py -q`
Expected: `4 passed`

- [ ] **Step 5: 写落库部分的失败测试**

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

- [ ] **Step 6: 运行，确认失败**

Run: `cd backend && uv run pytest tests/finance/test_ledger.py -q`
Expected: FAIL，`ImportError: cannot import name 'TransactionInput'`

- [ ] **Step 7: 写迁移 `0004_finance_ledger`**

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

- [ ] **Step 8: 实现 `post_transaction`、`void_transaction`、`find_unbalanced_transactions`**

- [ ] **Step 9: 运行，确认通过**

Run: `cd backend && uv run pytest tests/finance -q`
Expected: 全部通过

- [ ] **Step 10: 提交**

```bash
git add backend/
git commit -m "feat: add double-entry ledger with balance checks"
```

---

### Task 7: 余额快照、价格、汇率

**Files:**
- Create: `backend/src/lararium/modules/finance/snapshots.py`, `backend/src/lararium/modules/finance/pricing.py`, `backend/migrations/versions/0005_finance_snapshots_prices.py`
- Modify: `backend/src/lararium/modules/finance/models.py`
- Test: `backend/tests/finance/test_snapshots.py`, `backend/tests/finance/test_pricing.py`；在 `tests/finance/conftest.py` 里加 `btc` fixture（crypto，小数位 8，计价币种 USD）

**Interfaces:**
- Consumes: `get_timezone`、`local_date`、`get_account`、`get_instrument`、`get_self_entity_id`。
- Produces:
  - `SnapshotKind = Literal["reported", "manual"]`。
  - `@dataclass(frozen=True) class SnapshotRecord`：`id`、`account_id`、`instrument_id`、`quantity: Decimal`、`as_of: datetime`、`as_of_date: date`、`kind: SnapshotKind`。
  - `async def record_snapshot(ctx, *, account_id: UUID, instrument_id: UUID, quantity: Decimal, as_of: datetime, kind: SnapshotKind = "manual") -> SnapshotRecord`：`as_of` 不带时区时抛 `ValueError`；`as_of_date = local_date(as_of, 报告时区)`；emit `finance.snapshot.recorded`。数量带符号：负债为负。
  - `async def record_price(ctx, *, instrument_id: UUID, quote_instrument_id: UUID, price: Decimal, as_of_date: date, source: str = "manual") -> None`：计价币种必须是 fiat，否则抛 `NotACurrencyError`；同一个（资产，计价币种，日期，来源）只保留最新一次（upsert）；emit `finance.price.recorded`。
  - `async def record_fx_rate(ctx, *, base_instrument_id: UUID, quote_instrument_id: UUID, rate: Decimal, as_of_date: date, source: str = "manual") -> None`：两边都必须是 fiat，否则抛 `NotACurrencyError`；`rate <= 0` 时抛 `ValueError`；upsert 规则同上；emit `finance.fx_rate.recorded`。

- [ ] **Step 1: 写失败的测试**

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

- [ ] **Step 2: 运行，确认失败**

Run: `cd backend && uv run pytest tests/finance/test_snapshots.py tests/finance/test_pricing.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'lararium.modules.finance.snapshots'`

- [ ] **Step 3: 写迁移 `0005_finance_snapshots_prices`**

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

- [ ] **Step 4: 实现 `snapshots.py`、`pricing.py`，加 `btc` fixture**

upsert 用 `INSERT … ON CONFLICT (…) DO UPDATE SET price = EXCLUDED.price, recorded_at = now()`，汇率表同理。

- [ ] **Step 5: 运行，确认通过**

Run: `cd backend && uv run pytest tests/finance -q`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add backend/
git commit -m "feat: add balance snapshots, prices and fx rates"
```

---

### Task 8: 估值函数（v1）

**Files:**
- Create: `backend/migrations/versions/0006_finance_valuation_v1.py`, `backend/src/lararium/modules/finance/valuation.py`
- Test: `backend/tests/finance/test_valuation.py`

**Interfaces:**
- Consumes: 任务 4–7 的全部服务和 fixture。
- Produces（SQL 函数，以后 BI 和 MCP 也用这一套）：
  - `finance.v1_fx_rate(p_base uuid, p_quote uuid, p_day date) RETURNS numeric`
  - `finance.v1_balances_asof(p_day date) RETURNS TABLE (account_id uuid, instrument_id uuid, quantity numeric, snapshot_quantity numeric, snapshot_date date)`
  - `finance.v1_valuation_asof(p_day date) RETURNS TABLE (account_id uuid, account_name text, instrument_id uuid, symbol text, quantity numeric, unit_price numeric, price_currency_id uuid, fx_rate numeric, value numeric, status text)`
  - `finance.v1_net_worth_series(p_start date, p_end date) RETURNS TABLE (day date, value numeric, missing_count int)`
- Produces（Python 封装，在 `valuation.py`）：
  - `@dataclass(frozen=True) class BalanceRow`：`account_id`、`instrument_id`、`quantity: Decimal`、`snapshot_quantity: Decimal | None`、`snapshot_date: date | None`
  - `HoldingStatus = Literal["ok", "missing_price", "missing_fx"]`
  - `@dataclass(frozen=True) class HoldingRow`：`account_id`、`account_name: str`、`instrument_id`、`symbol: str`、`quantity: Decimal`、`unit_price: Decimal | None`、`price_currency_id: UUID | None`、`fx_rate: Decimal | None`、`value: Decimal | None`、`status: HoldingStatus`
  - `@dataclass(frozen=True) class NetWorthPoint`：`day: date`、`value: Decimal`、`missing_count: int`
  - `async def balances_asof(session, day: date) -> list[BalanceRow]`
  - `async def holdings_asof(session, day: date) -> list[HoldingRow]`：按账户名、symbol 排序。
  - `async def net_worth_series(session, start: date, end: date) -> list[NetWorthPoint]`：每天一行，按日期升序。

**这些函数要遵守的规则：**

- **快照选择**：取 `as_of_date <= p_day` 的最新快照，排序依次按 `as_of_date DESC, as_of DESC, recorded_at DESC`。
- **分录范围**：排除 `status = 'void'` 的交易。
- **账户范围**：排除 `status = 'closed' AND closed_on <= p_day` 的账户。
- **各余额模式的数量**：
  - `snapshots`：取快照的数量；没有快照就没有这一行。
  - `transactions`：`booking_date <= p_day` 的分录求和。
  - `hybrid`：有快照时 = 快照数量 + 交易 `occurred_at > 快照.as_of` 且 `booking_date <= p_day` 的分录之和；没有快照时，和 `transactions` 一样。
- **汇率 `v1_fx_rate`**（设计文档 §9，"取当天或之前最新的汇率"）：
  1. 两边是同一币种 → 恰好为 1；
  2. 否则最多有三条候选路径：
     - 直接：取 `as_of_date <= p_day` 的最新一条 base→quote 汇率，同一天先取 `source = 'manual'`，再按 `recorded_at DESC`；
     - 反向：按同样规则取最新一条 quote→base 汇率，再取倒数；
     - 经 EUR 中转：base→EUR × EUR→quote，只中转一次；每一段都按这里的同一套规则取（直接和反向里取较新的，日期相同取直接）。这条路径的生效日期是两段中较早的那个日期；
  3. 生效日期最新的路径胜出；日期相同时依次优先直接、反向、经 EUR；
  4. 没有任何路径 → NULL。
- **估值 `v1_valuation_asof`**：
  - 只包含 `ledger_type` 为 `asset` 或 `liability` 的账户，排除隐藏的资产和数量为 0 的行。
  - 报告币种取 `core.settings` 里 `finance.reporting_currency` 对应的 fiat 资产。
  - fiat 资产：`unit_price = 1`，`price_currency_id` 是它自己，汇率换算到报告币种。
  - 其他资产：取最新价格，规则和汇率一样（`as_of_date <= p_day`，同一天先取 manual，再按 `recorded_at DESC`），再把价格的计价币种换算到报告币种。
  - `status`：没有价格是 `missing_price`；没有汇率是 `missing_fx`；否则是 `ok`。`value = quantity × unit_price × fx_rate`，只有 `ok` 时才有值，其他情况为 NULL。
- **净值序列 `v1_net_worth_series`**：从 `p_start` 到 `p_end` 每天一行。`value` 是当天 `ok` 行的 value 之和（没有就是 0），`missing_count` 是当天非 `ok` 的行数。

- [ ] **Step 1: 写失败的测试**

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

`test_fiat_valued_direct_inverse_and_via_eur` 里的三种情况：USD 用直接汇率 0.8；EUR 用 GBP→EUR 1.25 取倒数，100 / 1.25 = 80；CNY 经 EUR 中转，1000 × (1/8) × (1/1.25) = 100。`test_reporting_currency_change_revalues` 里 100 GBP 用 USD→GBP 0.8 取倒数，等于 125 USD。比较用 `Decimal` 相等即可：这几个数在 numeric 运算下都能整除，没有舍入问题。

- [ ] **Step 2: 运行，确认失败**

Run: `cd backend && uv run pytest tests/finance/test_valuation.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'lararium.modules.finance.valuation'`

- [ ] **Step 3: 写迁移 `0006_finance_valuation_v1`，按上面的规则实现四个 SQL 函数（`LANGUAGE sql STABLE` 或 `plpgsql STABLE`）；在 `valuation.py` 里写 Python 封装**

封装用 `text("select * from finance.v1_valuation_asof(:day)")`，把结果映射成 dataclass。

- [ ] **Step 4: 运行，确认通过**

Run: `cd backend && uv run pytest tests/finance -q`
Expected: 全部通过

- [ ] **Step 5: 提交**

```bash
git add backend/
git commit -m "feat: add versioned valuation functions and net worth series"
```

---

### Task 9: REST API

**Files:**
- Create: `backend/src/lararium/api/{app.py, deps.py, core_routes.py, export_openapi.py}`, `backend/src/lararium/modules/registry.py`, `backend/src/lararium/modules/finance/{schemas.py, api.py}`
- Modify: `backend/src/lararium/modules/finance/__init__.py`（真正的 `MODULE`）
- Test: `backend/tests/api/conftest.py`, `backend/tests/api/test_core_api.py`, `backend/tests/api/test_finance_api.py`

**Interfaces:**
- Consumes: 前面所有任务的服务。
- Produces:
  - `@dataclass(frozen=True) class ModuleSpec: name: str; install: Callable[[FastAPI], None]`；`MODULES: tuple[ModuleSpec, ...] = (finance.MODULE,)`。finance 的 `install` 负责挂路由，并把 `FinanceError` 的各子类映射成 HTTP 错误：找不到 → 404；重复、已关闭 → 409；未知币种、不是币种 → 422；账本不平衡 → 422，响应体为 `{"detail": {"error": "ledger_imbalance", "residuals": {"<instrument_id>": "<plain decimal>"}}}`。
  - `def create_app(settings: Settings | None = None) -> FastAPI`：`generate_unique_id_function=lambda route: route.name`，这样每个路由的 `name` 就是 operationId，生成的 TypeScript 函数名稳定。
  - `async def get_session(request: Request) -> AsyncIterator[AsyncSession]`；`def get_actor(request: Request) -> Actor`：
    - `auth_mode="dev"` 时返回 `Actor("user", "dev")`；
    - `auth_mode="tailscale"` 时，请求头 `Tailscale-User-Login` 必须等于 `owner_login`，否则返回 401；通过时返回 `Actor("user", <登录名>)`。
    - `/api/v1/health` 不要求身份。
  - `DecimalStr`：基于 `Decimal` 的 Annotated 类型。只接受符合 `^-?[0-9]+(\.[0-9]+)?$` 的字符串（只允许 ASCII 数字），其他输入（包括 JSON 数字）一律 422；输出时用 `to_plain_str`；JSON schema 是 `{"type": "string", "pattern": …}`。所有金额、数量、价格、汇率字段都用它。
  - 路由（route name → 方法和路径）：
    - core：`health` GET `/api/v1/health` → `{"status": "ok"}`；`get_settings` GET `/api/v1/settings` → `{"timezone": …}`；`update_settings` PUT `/api/v1/settings`（时区必须是合法的 IANA 名称，否则 422）。
    - finance（前缀 `/api/v1/finance`）：
      - `get_finance_settings` GET `/settings` → `{"reporting_currency": "GBP"}`；`update_finance_settings` PUT `/settings`（币种必须是已有的 fiat）。
      - `list_instruments` GET `/instruments?include_hidden=false`；`create_instrument` POST `/instruments` → 201；`update_instrument` PATCH `/instruments/{instrument_id}`（body：`{"hidden": bool}`）。
      - `list_accounts` GET `/accounts`；`create_account` POST `/accounts` → 201（body 里的 `institution_name` 通过 `get_or_create_organization` 换成实体 id）；`rename_account` PATCH `/accounts/{account_id}`；`close_account` POST `/accounts/{account_id}/close`（body：`{"closed_on": "YYYY-MM-DD"}`）。
      - `record_snapshot` POST `/accounts/{account_id}/snapshots` → 201。
      - `post_transaction` POST `/transactions` → 201（至少两条分录）；`void_transaction` POST `/transactions/{transaction_id}/void`。
      - `record_price` POST `/prices` → 204（body：`instrument_id`、`quote_currency`（ISO 代码）、`price`、`as_of_date`）；`record_fx_rate` POST `/fx-rates` → 204（body：`base`、`quote`（ISO 代码）、`rate`、`as_of_date`）。
      - `get_holdings` GET `/holdings?date=YYYY-MM-DD`（默认是报告时区的今天）；`get_net_worth` GET `/net-worth?start=&end=`（默认是截至今天的 365 天）。
  - 每个写操作都在 `command_context(session, actor, "<route name>", <body 的 JSON 形式>)` 里执行。
  - `python -m lararium.api.export_openapi`：把 OpenAPI 以 `sort_keys=True`、`indent=2` 输出到 stdout。
  - 测试 fixture（`tests/api/conftest.py`）：`client` 用 `dev` 模式；`tailscale_client` 用 `tailscale` 模式，`owner_login="me@example.com"`。两者都覆盖 `get_session`，让它返回测试用的 `session`，并通过 `httpx.ASGITransport` 发请求。

- [ ] **Step 1: 写失败的测试**

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

- [ ] **Step 2: 运行，确认失败**

Run: `cd backend && uv run pytest tests/api -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'lararium.api.app'`

- [ ] **Step 3: 实现 `registry.py`、`schemas.py`、`finance/api.py`、`core_routes.py`、`deps.py`、`app.py`、`export_openapi.py`**

`export_openapi.py` 提供 `render_openapi() -> str`：用 `Settings(database_url="postgresql+psycopg://openapi@localhost/unused", auth_mode="dev")` 构建 app，只生成 schema，不连数据库（engine 是惰性创建的）；`main()` 打印这个字符串。

- [ ] **Step 4: 运行，确认通过**

Run: `make backend-check`
Expected: 全部通过（包括 `test_no_float_in_source`）

- [ ] **Step 5: 提交**

```bash
git add backend/
git commit -m "feat: add versioned REST API with audit-aware commands"
```

---

### Task 10: 网页界面

**Files:**
- Create: `web/` 下的 `package.json`、`vite.config.ts`、`tsconfig.json`、`index.html`、`openapi-ts.config.ts`、`openapi.json`（由命令生成）、`src/main.tsx`、`src/router.tsx`、`src/api/`（由命令生成）、`src/lib/format.ts`、`src/components/NetWorthChart.tsx`、`src/pages/NetWorthPage.tsx`、`src/pages/AccountsPage.tsx`、`src/pages/InstrumentsPage.tsx`；shadcn 组件（button、card、input、label、select、table、checkbox）
- Modify: `Makefile`（加 `openapi`、`openapi-check`、`web-check`；`check` 依赖 `backend-check openapi-check web-check`）
- Test: `web/src/lib/format.test.ts`, `web/src/pages/AccountsPage.test.tsx`, `web/src/pages/NetWorthPage.test.tsx`

**Interfaces:**
- Consumes: OpenAPI 生成的函数，名字由任务 9 的 route name 转成 camelCase：`listAccounts`、`createAccount`、`recordSnapshot`、`listInstruments`、`createInstrument`、`updateInstrument`、`getHoldings`、`getNetWorth`、`getFinanceSettings` 等。
- Produces:
  - `formatMoney(value: string, currency: string, locale = "en-GB"): string`：用 `Intl.NumberFormat` 格式化，直接传字符串，不经过 Number。
  - `formatQuantity(value: string, maxDecimals: number, locale = "en-GB"): string`：`minimumFractionDigits: 0`。
  - `negateDecimalString(value: string): string`：纯字符串取反，`"0"` 保持 `"0"`。
  - 页面：`/` 净资产，`/accounts` 账户，`/instruments` 资产。顶部导航文字依次是“Net worth”“Accounts”“Instruments”。
  - 前端只在画图时把字符串转成 number（`NetWorthChart` 内部），其他地方不对金额做任何计算。
  - 开发服务器把 `/api` 代理到 `http://127.0.0.1:8000`。

- [ ] **Step 1: 初始化前端，生成 API 客户端**

用 pnpm 建 Vite + React + TypeScript 项目，装上 TanStack Router、TanStack Query、echarts、Tailwind v4（`@tailwindcss/vite`），用 shadcn CLI 加上述组件；开发依赖装 vitest、jsdom、@testing-library/react、@testing-library/user-event、@hey-api/openapi-ts。`openapi-ts.config.ts`：输入 `openapi.json`，输出 `src/api`，用 fetch 客户端插件。`package.json` 里的脚本：`typecheck`（`tsc --noEmit`）、`test`（`vitest`）、`openapi-ts`。

Run: `make openapi`
Expected: 生成 `web/openapi.json` 和 `web/src/api/`，并且能找到 `export const getNetWorth`

- [ ] **Step 2: 写失败的测试**

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
// 用 vi.mock("../api") 替换生成的客户端：
//   listAccounts 返回一个 ledger_type 为 "liability" 的账户 "Credit card"；
//   listInstruments 返回 GBP；recordSnapshot 是 vi.fn()。
// 渲染 AccountsPage，在 "Credit card" 那一行的快照表单里，往 "Amount owed" 输入框填 "500"，点 "Save snapshot"。
it("sends liability balances as negative quantities", async () => {
  // …渲染和交互…
  expect(recordSnapshot).toHaveBeenCalledWith(
    expect.objectContaining({ body: expect.objectContaining({ quantity: "-500" }) }),
  );
});
```

```tsx
// web/src/pages/NetWorthPage.test.tsx
// vi.mock("../api")：getNetWorth 返回最后一天 { value: "1500.00", missing_count: 2 }；
// getHoldings 返回空数组；getFinanceSettings 返回 { reporting_currency: "GBP" }。
// 返回值按生成客户端的格式包在 { data: … } 里（AccountsPage 的测试同理）。
// vi.mock("../components/NetWorthChart")，换成空组件，避免在 jsdom 里跑 ECharts。
it("shows the total and warns about missing prices", async () => {
  // …渲染…
  expect(await screen.findByText("£1,500.00")).toBeInTheDocument();
  expect(screen.getByText("2 items are missing a price or FX rate and are excluded from net worth")).toBeInTheDocument();
});
```

资产账户的快照表单，数量输入框的标签是“Quantity”；负债账户的标签是“Amount owed”，提交前用 `negateDecimalString` 取反。

- [ ] **Step 3: 运行，确认失败**

Run: `cd web && pnpm test --run`
Expected: FAIL，提示找不到 `./format` 和两个页面模块

- [ ] **Step 4: 实现 `format.ts`、三个页面、`NetWorthChart`（ECharts 折线图）、路由和导航**

- 净资产页：报告币种的总额（`getNetWorth` 最后一天的值）；`missing_count > 0` 时显示上面那条提示（只有 1 项时文案是 "1 item is missing a price or FX rate and is excluded from net worth"）；近 365 天的折线图；按账户分组的持仓表（Account、Instrument、Quantity、Value、Status）。
- 账户页：账户列表；“New account”表单（Name、Ledger type、Subtype、Balance mode、Institution name、Tax wrapper）；每个账户一个快照表单（Instrument 下拉框、Quantity 或 Amount owed、As of（默认是现在）、“Save snapshot”按钮）。
- 资产页：资产列表，每行一个“Hide”复选框；“New instrument”表单（Kind、Symbol、Name、Decimals、Quote currency、CoinGecko id（可选））。

- [ ] **Step 5: 运行，确认通过**

Run: `make check`
Expected: 后端检查、OpenAPI 一致性检查（`git diff --exit-code` 无输出）、前端 typecheck 和测试全部通过

- [ ] **Step 6: 提交**

```bash
git add Makefile web/
git commit -m "feat: add web UI for net worth, accounts and instruments"
```

---

### Task 11: 部署、备份与 CI

**Files:**
- Create: `infra/compose.yaml`, `infra/env.example`, `infra/caddy/Caddyfile`, `infra/backup/backup.sh`, `infra/backup/restore.sh`, `backend/Dockerfile`, `web/Dockerfile`, `.github/workflows/ci.yml`, `README.md`, `README.zh-CN.md`
- Modify: `Makefile`（加 `up`、`down`、`backup`）

**Interfaces:**
- Consumes: 任务 2 的 `roles.sql` 和 `00-roles.sh`，任务 9 的 `create_app`，任务 10 的前端构建产物。
- Produces:
  - Compose 服务：`db`（`postgres:18`）、`migrate`（一次性运行 `alembic upgrade head`，用 `lararium_owner`）、`api`（uvicorn，用 `lararium_backend`，`LARARIUM_AUTH_MODE=tailscale`）、`web`（Caddy 托管 SPA，并把 `/api/*` 转给 api），只绑定 `127.0.0.1:8080`。
  - 访问方式：在主机上运行 `tailscale serve --bg 8080`，Tailscale 会给请求加上 `Tailscale-User-Login` 头。
  - 环境变量（`infra/env.example`）：`POSTGRES_PASSWORD`、`LARARIUM_OWNER_PASSWORD`、`LARARIUM_BACKEND_PASSWORD`、`LARARIUM_OWNER_LOGIN`、`BACKUP_DIR`，以及可选的 `RESTIC_REPOSITORY`、`RESTIC_PASSWORD`。`infra/.env` 不进 git；README 里说明怎么用 SOPS + age 加密它，以及怎么解密回来。

- [ ] **Step 1: 写 `compose.yaml`、两个 Dockerfile 和 `Caddyfile`**

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

`backend/Dockerfile`：基于 `python:3.13-slim`，用 uv 按 `uv.lock` 安装（不装 dev 依赖），把 `migrations/` 和 `alembic.ini` 一起拷进镜像。`web/Dockerfile`（构建上下文是仓库根目录）：先用 `node:24` 阶段在 `web/` 里跑 `pnpm install --frozen-lockfile && pnpm build`，再把 `web/dist/` 拷进 `caddy:2` 镜像的 `/srv`，把 `infra/caddy/Caddyfile` 拷到 `/etc/caddy/Caddyfile`。

- [ ] **Step 2: 启动并冒烟测试**

Run:
```bash
cp infra/env.example infra/.env   # 填入本地测试用的密码，LARARIUM_OWNER_LOGIN 填 me@example.com
docker compose -f infra/compose.yaml --env-file infra/.env up -d --build
curl -s http://127.0.0.1:8080/api/v1/health
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/api/v1/finance/accounts
curl -s -H "Tailscale-User-Login: me@example.com" http://127.0.0.1:8080/api/v1/finance/accounts
```
Expected: 依次输出 `{"status":"ok"}`、`401`、`[]`；浏览器打开 `http://127.0.0.1:8080/` 能看到净资产页（没有请求头时 API 返回 401，属于预期）

- [ ] **Step 3: 写 `backup.sh` 和 `restore.sh`，验证一次恢复**

两个脚本都用 `docker compose -f "$(dirname "$0")/../compose.yaml" --env-file "$(dirname "$0")/../.env"` 找到这套服务。

- `backup.sh`：`… exec -T db pg_dump -U postgres -d lararium --format=custom` 输出到 `$BACKUP_DIR/lararium-<UTC 时间戳>.dump`。设置了 `RESTIC_REPOSITORY` 时，再执行 `restic backup "$BACKUP_DIR"`。
- `restore.sh <dump 文件> <目标库名>`：新建一个 owner 为 `lararium_owner` 的目标库，然后 `pg_restore --no-owner --role=lararium_owner` 恢复进去。

Run:
```bash
infra/backup/backup.sh
infra/backup/restore.sh "$(ls -t backups/lararium-*.dump | head -1)" lararium_restore_check
docker compose -f infra/compose.yaml exec -T db psql -U postgres -d lararium_restore_check -tAc "select count(*) from finance.instruments"
```
Expected: 最后一条命令输出 `10`（种子币种的数量）

- [ ] **Step 4: 写 CI 和 README**

`.github/workflows/ci.yml`：在 `ubuntu-latest` 上装 uv、pnpm 11、Node 24，然后运行 `make check`（testcontainers 用的是 runner 自带的 Docker）。`README.md`（英文）写清楚三件事：本机开发怎么启动（`make check`、`uv run uvicorn … --factory --reload`、`pnpm dev`）；服务器部署（`make up`、`tailscale serve --bg 8080`）；SOPS 加密 `.env` 和备份、恢复的命令。`README.zh-CN.md` 是它的中文译文。两份文件顶部互相链接。

Run: `make check`
Expected: 全部通过

- [ ] **Step 5: 确认镜像能构建 amd64**

Run: `docker buildx build --platform linux/amd64 backend`
Expected: 构建成功（本机是 arm64，第 2 步已经覆盖了 arm64）

- [ ] **Step 6: 提交**

```bash
git add infra/ backend/Dockerfile web/Dockerfile .github/ README.md README.zh-CN.md Makefile
git commit -m "chore: add docker compose deployment, backups and CI"
```
