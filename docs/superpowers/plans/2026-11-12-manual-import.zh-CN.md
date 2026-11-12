# 手动导入 Implementation Plan

**语言：** [English](./2026-11-12-manual-import.md) | 简体中文

本文是英文版的中文译文；两者如有出入，以英文版为准。

> **状态：暂缓（2026-11-12）。** 使用者主要依靠 Monzo API，只是偶尔手动导入，所以先做 M3（Monzo）。恢复这份计划时：
> - **任务 1（文档）**仍然需要做：M3 的计划把 Monzo 返回的小 JSON 直接存成原始记录，不建文档。恢复前先确认是不是已有更早的计划建好了它。
> - **任务 4（OFX）**以及其他任务里和 OFX 有关的部分都是可选的。使用者的银行目前都不提供 OFX；除非哪家开始提供，否则跳过。这些部分是 `OfxProfileConfig`、决策 13 里的 `LEDGERBAL`、任务 5 和 7 里的 OFX 测试，以及 OFX 示例文件。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把银行对账单导入账本：CSV 按各机构的解析配置解析，另外支持 OFX。流程是上传文件、审阅暂存行（新增、重复、跳过、错误）、提交入账生成交易，以及把整个批次回滚。

**Architecture:** 上传的文件在 `core` 里存成按内容寻址的**文档**（存储是本地目录，放在 `DocumentStore` 接口后面）。新建 `lararium.modules.finance.importing` 包，里面有：解析配置（以数据形式保存）、两个纯函数解析器（CSV、OFX）、暂存服务（解析 → 去重 → 写入 `finance.import_rows`），以及提交和回滚服务——它通过现有的 `ledger.post_transaction` 把每一行变成一笔有两条分录的交易。解析在 API 请求里同步完成；文件上限 5 MiB。

**Tech Stack:** 同计划 1，另加 `python-multipart` 处理上传。不用 OFX 库：OFX 由一个手写的小型分词器解析（见“决策”）。

**Spec:** `docs/superpowers/specs/2026-11-11-financial-gate-phase1-design.md`（本计划覆盖 M2：7.8 节，外加 6.2 节里的“文档”对象）

**基于：** 计划 1（`docs/superpowers/plans/2026-11-11-foundation-ledger-core.md`）合并进 `main` 后的代码（`aef64ee`），迁移已到 `0007`。

## 决策

规格里没定下来的选择，在这里先定好，任务里就不用再纠结：

1. **导入行的另一条分录。** 银行流水的一行是单式记账，账本是复式记账。每一行变成一笔交易，有两条分录：一条记在被导入的账户上，另一条记在一个系统账户 **"Unassigned (imports)"** 上（`ledger_type` 为 `equity`，subtype 为 `other`，余额模式为 `transactions`）。这个账户第一次用到时创建，id 记在设置项 `finance.import_contra_account_id` 里。净值只统计资产和负债账户，所以这个账户永远不影响净值。以后转账匹配（M4）会把这些分录改指到正确的账户。
2. **正负号约定。** 解析配置用**流入**（账户余额增加）和**流出**来描述。对信用卡（负债）来说，消费是流出（余额更负），还款是流入。解析配置要么映射一个带符号的金额列，并用 `sign: "inflow_positive" | "outflow_positive"` 说明方向；要么映射两个不带符号的列 `inflow` 和 `outflow`（数量 = inflow − outflow）。
3. **哪些账户能导入。** 只有未关闭的手动账户，而且余额模式是 `transactions` 或 `hybrid`。估值时会忽略 `snapshots` 账户上的交易，导入进去也看不见效果，所以直接拒绝。
4. **错误阻止提交；金额为 0 的行和被过滤掉的行跳过。** 解析失败的行，或者打断了余额链（决策 12）的行，以 `error` 状态保存，并附上错误信息；批次里只要有一行出错就不能提交（改好解析配置或文件后重新上传）。金额为 0 的行，或者被解析配置的 `include_only` 过滤掉的行（比如一笔 `PENDING` 的刷卡消费），状态为 `skipped`，原因写在 `note` 里，不阻止提交。重复的行永远不会提交。M2 不支持逐行手动覆盖。
5. **去重键**（规格 6.4 节），按账户计算：
   - 行里有外部 ID（OFX 的 `FITID`，或映射的 CSV 列）：`sha256("ext", account, external_id)`；
   - 否则，行里有流水余额：`sha256("bal", account, date, quantity, currency, description, balance)`；
   - 否则：`sha256("seq", account, date, quantity, currency, description, n)`，其中 `n` 是**同一个文件**里此前日期、数量、币种、描述都相同的行数。两笔真实的相同消费分别得到 `n = 0` 和 `n = 1`；重新导出的文件里如果两笔都在，两个键都会原样复现。

   数量和余额用 `to_plain_str(value.normalize())` 规范化；描述转成小写（case-fold）并合并空白。同一账户上已经有**已提交**的行用了同一个键，这一行就算重复。数据库里对已提交行建一个部分唯一索引，作为最后一道防线。
6. **日期。** `booking_date` 永远是文件里写的那个日期，绝不因为换算 UTC 而偏移。
   - 只有日期的值（CSV，或者没有时间的 OFX `DTPOSTED`）：`occurred_at` 取这一天的本地零点，`occurred_tz` 设为该时区名。
   - 带时间的 CSV 值（`date_format` 里含 `%H`、`%I`、`%M`、`%S` 或 `%p`，例如 `15 Oct, 2026 09:05`）：按解析配置的 `timezone` 解读这个本地时间，`occurred_tz` 设为该时区。
   - 带时间的 OFX `DTPOSTED` 按其中的偏移量解析（文件没写偏移量时按 UTC，这是 OFX 规范的规定），记账日期仍然取文件里写的 `YYYYMMDD`。
   - “本地”指：解析配置设置了 `timezone` 时用它，否则用 `core.timezone`。
7. **回滚是作废，从不删除。** 回滚一个已提交的批次时，把它的交易作废（已经作废的不动），把它的行标为 `rolled_back`，这样同一个文件可以再次导入。丢弃一个暂存中的批次只是把它标为 `discarded`。文档永远不删除。
8. **不用库解析 OFX。** `ofxtools` 是 GPL-3.0（和 AGPL 兼容），但它会把所有时间戳转成 UTC，丢掉文件里写的日期。对账单交易只需要几个叶子字段，所以写一个同时支持 OFX 1.x SGML（叶子标签不闭合）和 2.x XML 的小分词器，更简单，结果也确定。
9. **解析配置在界面上以 JSON 编辑**，由服务端校验。表单式的编辑器以后再做。
10. **导入的交易保持 `origin = 'manual'`**；它的来源通过 `finance.import_rows.transaction_id` 关联追溯。这样就不用改 `transactions.origin` 上的 CHECK。
11. **手续费。** 解析配置可以映射一个 `fee` 列。手续费是在金额之外另收的，所以账户余额的变化是 `quantity − fee`。手续费不为 0 的行变成三条分录：账户上 `+quantity`；账户上 `−fee`，带 `is_fee = true`；对方账户上 `−(quantity − fee)`。
12. **检查余额链。** 行里带流水余额时，暂存按时间顺序（解析配置的 `row_order`）逐行检查，每种资产分开算。它维护一个预期余额：每遇到一行可导入的行，就加上它的 `quantity − fee`；某一行写明了余额时，这个余额必须等于预期余额。随后写明的余额成为新的预期余额。
    - 对不上时，这一行变成 `error`："balance doesn't follow from the previous row: expected X, the file says Y"。`sign` 设反了、被跳过的行其实动了钱、某一列读错了，这些都会在这里被发现，而不会悄悄扭曲净值。
    - 跳过的行不计入。重复的行要计入，因为它们是对账单里真实存在的行。解析出错的行会让余额链重新开始。
13. **自动对齐期初余额。** 对账单很少从账户开立的第一天开始，所以对账单各行之和并不是账户的余额。
    - **推算。** 暂存按资产推算对账单的期初余额：取按时间顺序第一个带余额的行（它的余额减去从第一行到它本身这些行的净额）；没有这样的行时，取 OFX 的 `LEDGERBAL`（期末余额减去所有可导入行的净额）。
    - **比较。** 把它和账本比较：账户在这种资产上、记账日期早于对账单第一天的所有未作废分录之和。
    - **调整。** 两者不同时，暂存为每种资产加一行：`kind = 'opening_adjustment'`，`position` 为负数，描述为 "Opening balance adjustment"，数量是差额，记账日期是对账单第一天的前一天。它的另一条分录记在第二个系统账户 **"Opening balances"** 上（`equity`，设置项 `finance.opening_balance_account_id`）。
    - **含义。** 第一次导入时，这一行就是期初余额。以后再出现不为 0 的调整，通常说明缺了一段时期的对账单：审阅页面会提示这一点，使用者可以丢弃这个批次，先把缺的那段导入。
    - **不适用的情况。** `hybrid` 账户不做调整，因为它们的余额由快照锚定；余额链检查照常进行。文件里既没有余额也没有 `LEDGERBAL` 时，批次带上警告 "This file has no balances, so its opening balance can't be checked"。
    - **提交时。** 重新计算调整额。如果暂存之后账本有变化（又提交了别的批次），提交被拒绝，抛 `ImportBatchStaleError`，使用者重新上传文件。

**本计划不做：** 暂存行和推断分录的配对（依赖 M4）；快照和账本对不上时的提醒（M9）；从文档重新生成账本（6.6 节；本计划保存了文档、解析配置的快照和解析器版本，以后可以做）；PDF 解析；后台 worker。

## Global Constraints

- 计划 1 的 Global Constraints 全部继续有效：Python 3.13；uv 并强制使用 lockfile；ruff；pyright 以 strict 模式检查 `src/`；import-linter 合约；PostgreSQL 18；`NUMERIC`/`Decimal`/字符串，`src/` 里不出现 `float` 这个单词；UUIDv7；UTC 的 `timestamptz` 加单独的 `date`；界面文案用英文；文档双语（英文为主，`*.zh-CN.md`）；只用和 AGPL-3.0 兼容的依赖。
- 分支 `feat/claude-manual-import`；提交信息 `<type>: <summary>`；提交时用 `local-signed-git-commit` skill 处理签名。
- 所有写操作都经过 `command_context`（每个请求一条审计日志，事件写入发件箱）。新的写路由沿用 `ActorDep`，所以上传也要过跨站检查。
- **仓库里任何地方都不放真实的对账单。** 示例文件和测试文件全部手写合成，机构用虚构的 "Example Bank"。使用者自己的真实导出文件放在 `private/`。
- 金额范围：导入的数量和余额都要经过 `money.require_amount`（小数点两边各最多 40 位）；不通过的值变成行错误，绝不返回 500。
- 上传上限 `MAX_IMPORT_BYTES = 5 * 1024 * 1024`；行数上限 `MAX_IMPORT_ROWS = 20_000`。
- `core` 继续不带财务色彩：`core.documents` 对导入一无所知，它记录的 `source` 是自由字符串（`"finance.import"`）。

## Review Focus

这几类输入规格里隐含了要求，但主线测试碰不到，按踩坑可能性从高到低排列。每一条都在负责它的任务里有测试。前两条是核对使用者真实的导出文件时发现的（LemFi 和 Revolut 的 CSV；测试只用仿照其结构的合成文件）：

- **从历史中途开始的对账单**：导入后账户余额等于对账单的期末余额，而不是各行之和（任务 5：`test_opening_adjustment_from_first_balance`；任务 7：`test_example_csv_end_to_end`）。
- **还没完成就被导出的待处理行**：它被 `include_only` 过滤器跳过，所以以后导入已完成的版本时不会重复计算（任务 3：`test_include_only_skips_before_parsing`；任务 6：`test_pending_then_completed_counted_once`）。

- **同一份对账单导出两次，日期范围有重叠**：只有还没提交过的行是新增（任务 6：`test_overlapping_export_commits_only_new_rows`）。
- **同一天两笔真实的相同消费**：两笔都导入；同一个文件再导入一次，两笔都标为重复（任务 5：`test_identical_rows_in_one_file_are_both_new`；任务 6：`test_reimporting_same_file_marks_all_duplicate`）。
- **信用卡对账单**：消费让负债更负，还款让它向 0 靠近（任务 6：`test_liability_import_signs`）。
- **跨英国夏令时切换的纯日期行**：`booking_date` 保持文件里写的日期，不会因为换算 UTC 而偏移（任务 5：`test_date_only_rows_keep_booking_date_across_dst`）。
- **Excel 风格的 CSV**：表头前的 UTF-8 BOM 和 cp1252 编码的 `£` 都能正确解码；文件不符合解析配置指定的编码时，给出明确的文件级错误，绝不产生乱码（任务 3：`test_utf8_bom_header`、`test_cp1252_pound_sign`、`test_wrong_encoding_is_file_error`）。

---

## 文件结构

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

### Task 1: 文档（core）

**Files:**
- Create: `backend/migrations/versions/0008_core_documents.py`, `backend/src/lararium/core/documents.py`, `backend/tests/core/test_documents.py`
- Modify: `backend/src/lararium/core/models.py`, `backend/src/lararium/core/errors.py`, `backend/src/lararium/config.py`

**Interfaces:**
- Consumes: `CommandContext`、`command_context`，以及 `session`/`run` 两个 fixture。
- Produces:
  - `class DocumentStore(Protocol)`：`def put(self, sha256: str, content: bytes) -> None`；`def get(self, sha256: str) -> bytes`。
  - `class LocalDocumentStore(DocumentStore)`：`__init__(self, root: Path)`。文件存在 `root / sha[:2] / sha[2:4] / sha`；先在同一目录写临时文件，再用 `os.replace` 换到目标位置；`put` 一个已存在的哈希时什么都不做。
  - `@dataclass(frozen=True) class DocumentRecord`：`id: UUID, sha256: str, size_bytes: int, media_type: str, original_filename: str, source: str, created_at: datetime`。
  - `async def store_document(ctx: CommandContext, store: DocumentStore, content: bytes, *, filename: str, media_type: str, source: str) -> DocumentRecord`：先写内容，再用 `ON CONFLICT (sha256) DO NOTHING` 插入；冲突时返回已有的那一行（保留第一次的文件名）。只有新插入时才 emit `core.document.stored`，并调用 `ctx.record_change("document", {...})`。
  - `async def get_document(session: AsyncSession, document_id: UUID) -> DocumentRecord`：找不到时抛 `DocumentNotFoundError`。
  - `async def read_document(session: AsyncSession, store: DocumentStore, document_id: UUID) -> bytes`：重新计算哈希，对不上或文件不存在时抛 `DocumentIntegrityError`。
  - `Settings.document_dir: Path = Path("/var/lib/lararium/documents")`（环境变量 `LARARIUM_DOCUMENT_DIR`）。

迁移 `0008`：`core.documents(id uuid PK DEFAULT uuidv7(), sha256 text NOT NULL UNIQUE CHECK (sha256 ~ '^[0-9a-f]{64}$'), size_bytes bigint NOT NULL CHECK (size_bytes >= 0), media_type text NOT NULL, original_filename text NOT NULL, source text NOT NULL, created_at timestamptz NOT NULL DEFAULT now())`，然后 `REVOKE UPDATE, DELETE ON core.documents FROM lararium_backend`（文档和审计日志一样，是不可修改的原始数据）。

- [ ] **Step 1: 写失败测试**，放在 `tests/core/test_documents.py`，用一个 `store` fixture（`LocalDocumentStore(tmp_path)`）：
  - `test_store_and_read_round_trip`：以文件名 `a.csv` 存入 `b"date,amount\n"`，返回的记录 `size_bytes == 12`，`sha256` 是 64 位十六进制；`read_document` 读回相同的字节。
  - `test_same_content_is_stored_once`：同样的字节先以 `a.csv`、再以 `b.csv` 存入，返回同一个 `id`；`original_filename == "a.csv"`；它对应的 `core.document.stored` 事件恰好一条。
  - `test_tampered_file_is_an_integrity_error`：改写已存文件的字节后，`read_document` 抛 `DocumentIntegrityError`；把文件删掉，同样抛 `DocumentIntegrityError`。
  - `test_unknown_document_is_not_found`：`get_document(session, 一个类似 uuid7 的随机 UUID)` 抛 `DocumentNotFoundError`。
  - `test_backend_role_cannot_update_or_delete_documents`：用 backend 的 session 执行 `UPDATE core.documents SET source = 'x'` 和 `DELETE FROM core.documents`，各自抛 `ProgrammingError`（权限不足）。每条语句各用一个 savepoint。
- [ ] **Step 2: 运行** `cd backend && uv run --locked pytest tests/core/test_documents.py -q`；预期：FAIL（import 报错）。
- [ ] **Step 3: 实现** 迁移、`Document` ORM 模型、错误类型，以及按上面描述实现 `core/documents.py`。
- [ ] **Step 4: 运行** 同一条命令；预期：5 passed。再跑 `make backend-check`；预期：全部通过。
- [ ] **Step 5: 提交** `feat: add content-addressed documents to core`。

### Task 2: 解析配置

**Files:**
- Create: `backend/migrations/versions/0009_finance_import_profiles.py`, `backend/src/lararium/modules/finance/importing/__init__.py`, `.../importing/profiles.py`, `backend/tests/finance/importing/test_profiles.py`
- Modify: `backend/src/lararium/modules/finance/models.py`, `.../finance/errors.py`

**Interfaces:**
- Produces（在 `importing/profiles.py` 里）。所有配置模型都是 Pydantic 模型，带 `ConfigDict(extra="forbid", frozen=True)`：
  - `class AmountFormat`：`decimal_separator: Literal[".", ","] = "."`；`thousands_separator: Literal["", ",", ".", " ", "'"] = ""`。校验：两者必须不同。
  - `class CsvColumns`：`date: str`；`description: list[str]`（至少 1 个；用一个空格连接）；`amount: str | None = None`；`inflow: str | None = None`；`outflow: str | None = None`；`balance: str | None = None`；`currency: str | None = None`；`external_id: str | None = None`；`fee: str | None = None`。校验：要么只给 `amount`，要么同时给 `inflow` 和 `outflow`，二者必居其一。
  - `class CsvProfileConfig`：
    - `format: Literal["csv"]`
    - `encoding: Literal["utf-8-sig", "utf-8", "cp1252", "latin-1"] = "utf-8-sig"`
    - `delimiter: str = ","`（恰好一个字符）
    - `skip_rows: int = 0`（0–50）
    - `columns: CsvColumns`
    - `date_format: str`。校验方法：用它格式化 `datetime(2026, 12, 31, 23, 59, 58)`，再把结果解析回来，解析出的日期必须是 2026-12-31。含 `%H`、`%I`、`%M`、`%S` 或 `%p` 的格式带时间（属性 `has_time: bool`）。
    - `timezone: str | None = None`，IANA 时区名，用 `ZoneInfo` 校验
    - `include_only: dict[str, list[str]] = {}`，列 → 允许的值（去掉首尾空白后精确匹配）；每个列表至少有一个值
    - `row_order: Literal["oldest_first", "newest_first"] = "oldest_first"`
    - `sign: Literal["inflow_positive", "outflow_positive"] = "inflow_positive"`
    - `amount_format: AmountFormat = AmountFormat()`
  - `class OfxProfileConfig`：`format: Literal["ofx"]`；`sign: Literal["inflow_positive", "outflow_positive"] = "inflow_positive"`；`timezone: str | None = None`。
  - `type ProfileConfig = Annotated[CsvProfileConfig | OfxProfileConfig, Field(discriminator="format")]`。
  - `@dataclass(frozen=True) class ImportProfileRecord`：`id: UUID, name: str, config: CsvProfileConfig | OfxProfileConfig, version: int, updated_at: datetime`。
  - `async def create_profile(ctx, *, name: str, config: CsvProfileConfig | OfxProfileConfig) -> ImportProfileRecord`
  - `async def update_profile(ctx, profile_id: UUID, *, name: str, config: CsvProfileConfig | OfxProfileConfig) -> ImportProfileRecord`：`version` 加 1。
  - `async def get_profile(session, profile_id: UUID) -> ImportProfileRecord`；`async def list_profiles(session) -> list[ImportProfileRecord]`（按名称排序）。
  - `finance/errors.py` 里的错误：`ImportProfileNotFoundError`、`DuplicateProfileNameError`。

迁移 `0009`：`finance.import_profiles(id uuid PK DEFAULT uuidv7(), name text NOT NULL, format text NOT NULL CHECK (format IN ('csv','ofx')), config jsonb NOT NULL, version integer NOT NULL DEFAULT 1 CHECK (version >= 1), owner_entity_id uuid NOT NULL REFERENCES core.entities, created_at/updated_at timestamptz NOT NULL DEFAULT now(), CHECK (config->>'format' = format))`，外加一个建在 `lower(btrim(name))` 上的唯一索引。写入时去掉名称首尾空白，名称为空时抛 `FinanceValidationError`。

- [ ] **Step 1: 写失败测试：**
  - `test_amount_xor_inflow_outflow`：`CsvColumns(date="D", description=["X"])` 报错；同时给 `amount` 和 `inflow`/`outflow` 报错；只给 `inflow` 不给 `outflow` 也报错。
  - `test_separators_must_differ`：`AmountFormat(decimal_separator=",", thousands_separator=",")` 报错。
  - `test_unknown_key_rejected`：校验 `{"format": "csv", ..., "colour": "red"}` 报错。
  - `test_date_format_must_round_trip`：接受 `"%d/%m/%Y"`、`"%b %d, %Y"`、`"%d %b, %Y %H:%M"` 和 `"%Y-%m-%d %H:%M:%S"`（后两个 `has_time` 为真）；拒绝 `"%d/%m"` 和 `"not a date"`。
  - `test_timezone_and_filters_validated`：拒绝 `timezone="Europe/Nowhere"`；拒绝 `include_only={"State": []}`。
  - `test_update_bumps_version`：创建后 `version == 1`；更新后 `version == 2`，配置是新的。
  - `test_duplicate_name_case_insensitive`：先创建 `"Example Bank"`，再创建 `" example bank "`，抛 `DuplicateProfileNameError`。
- [ ] **Step 2: 运行** `uv run --locked pytest tests/finance/importing/test_profiles.py -q`；预期：FAIL。
- [ ] **Step 3: 实现** 按上面的描述。保存时存 `config.model_dump(mode="json")`；读回时用 `TypeAdapter(ProfileConfig)` 解析。重名靠唯一索引发现：把这个索引触发的 sqlstate `23505` 转成 `DuplicateProfileNameError`。
- [ ] **Step 4: 运行**；预期：7 passed；`make backend-check` 全部通过。
- [ ] **Step 5: 提交** `feat: add import parse profiles stored as data`。

### Task 3: CSV 解析器

**Files:**
- Create: `.../importing/parsed.py`, `.../importing/csv_parser.py`, `backend/tests/finance/importing/test_csv_parser.py`
- Modify: `.../finance/errors.py`（+ `FileFormatError(FinanceError)`）

**Interfaces:**
- Consumes: `CsvProfileConfig`、`AmountFormat`（任务 2）；`money.require_amount`。
- Produces:
  - 在 `parsed.py` 里：
    - `@dataclass(frozen=True) class ParsedRow`：`position: int, booking_date: date, occurred_at: datetime | None, description: str, quantity: Decimal, fee: Decimal, currency: str | None, balance: Decimal | None, external_id: str | None, raw: dict[str, str]`。
      - `quantity` 已经是账本的符号方向（流入为正）。
      - 没有映射手续费列或单元格为空时，`fee` 为 `Decimal(0)`；余额的变化是 `quantity - fee`。
      - 来源给了时间时，`occurred_at` 是带时区的时间，否则为 `None`。
    - `@dataclass(frozen=True) class RowError`：`position: int, message: str, raw: dict[str, str]`。
    - `@dataclass(frozen=True) class SkippedRow`：`position: int, reason: str, raw: dict[str, str]`。用于被 `include_only` 过滤掉的行。
    - `@dataclass(frozen=True) class ParsedStatement`：`rows: list[ParsedRow | RowError | SkippedRow]`、`closing_balance: Decimal | None = None`、`closing_balance_date: date | None = None`。只有 OFX 会设置期末余额这两个字段。
    - `MAX_IMPORT_ROWS = 20_000`。
    - `def parse_amount(text: str, fmt: AmountFormat) -> Decimal`：去掉首尾空白；接受开头一个 `+`/`-`，或者用括号括起来表示负数；符号之后可以有一个货币符号（`£ $ €` 之一），去掉它；去掉千位分隔符；把小数分隔符换成 `.`；然后要求匹配 `^[0-9]+(\.[0-9]+)?$`，而且只能是 ASCII 数字，再调用 `require_amount`。其他情况一律抛 `ValueError`，错误信息里写出原文。
    - `def clean_description(parts: list[str]) -> str`：用空格连接各部分，合并空白，去掉首尾空白，截断到 1000 个字符。
  - 在 `csv_parser.py` 里：`PARSER_VERSION = "csv-1"`；`def parse_csv(content: bytes, config: CsvProfileConfig, local_tz: ZoneInfo) -> ParsedStatement`。解析配置设置了 `timezone` 时，`local_tz` 就是它；由调用方确定。

`parse_csv` 的规则：
- 用 `config.encoding` 解码。遇到 `UnicodeDecodeError` 时抛 `FileFormatError("the file is not valid <encoding>")`。
- 先丢掉 `skip_rows` 个物理行，再用 `csv.DictReader(delimiter=config.delimiter)` 读取。
- 表头缺少任何一个映射的列，或者任何一个 `include_only` 用到的列时，抛 `FileFormatError`，信息里写出缺了哪些列。文件没有数据行时抛 `FileFormatError("no rows")`。数据行超过 `MAX_IMPORT_ROWS` 时抛 `FileFormatError`。
- 所有单元格都为空的行忽略。`position` 是它在剩下的数据行里从 0 开始的序号。
- `raw` 是这一行里被映射的列和 `include_only` 用到的列，保持原文。
- **先过滤。** 某一行的 `include_only` 列的值不在允许列表里时，在解析其他任何字段**之前**，就把它变成 `SkippedRow(reason="State is PENDING")`（写出列名和值）。待处理的行通常完成日期和余额都是空的，不能让它变成错误。
- 其余每一行遇到下面任何一种情况，都给出带具体信息的 `RowError`，绝不抛异常：
  - 日期不符合 `date_format`；
  - 金额、余额或手续费通不过 `parse_amount`；
  - 金额单元格为空：用 `amount` 时，该单元格为空；用 `inflow`/`outflow` 时，两个都为空（只有一个为空时按 0 算）；
  - 币种单元格不是三个 ASCII 字母（先转大写、去掉首尾空白再判断）。
- **可选单元格为空。** 余额单元格为空时 `balance = None`；手续费单元格为空时 `fee = 0`。
- **日期和时间。** `config.has_time` 为真时，用 `local_tz` 把解析出的本地时间变成带时区的时间（`occurred_at`），`booking_date` 取文件里写的那个日期。否则 `occurred_at = None`。
- **符号。** 用 `amount` 时，`inflow_positive` 下数量 = 金额，`outflow_positive` 下数量 = −金额；用 `inflow`/`outflow` 时，数量 = inflow − outflow，忽略 `sign`。手续费按原样取；手续费为正表示收费。

- [ ] **Step 1: 写失败测试**（配置在测试里直接构造；文件内容用 bytes 字面量）：
  - `test_signed_amount_column`：`Date,Description,Amount\n10/11/2026,Coffee,-3.50\n11/11/2026,Salary,2000.00\n` 配 `%d/%m/%Y`，得到数量 `Decimal("-3.50")` 和 `Decimal("2000.00")`，记账日期 `2026-11-10` 和 `2026-11-11`。
  - `test_outflow_positive_sign`：同一个文件配 `sign="outflow_positive"`，得到 `3.50` 和 `-2000.00`。
  - `test_inflow_outflow_columns`：`Paid in` 为空、`Paid out` 为 `"1,234.56"`，配 `thousands_separator=","`，得到 `-1234.56`。
  - `test_parentheses_and_currency_symbol`：`"(£12.00)"` 得到 `-12.00`；`"€1.234,50"` 配小数分隔符 `,`、千位分隔符 `.`，得到 `1234.50`。
  - `test_utf8_bom_header`：`b"\xef\xbb\xbfDate,Description,Amount\n..."` 配编码 `utf-8-sig`，能找到 `Date` 列。
  - `test_cp1252_pound_sign`：描述字节 `b"Caf\xe9 \xa3"` 配 `cp1252`，得到 `"Café £"`。
  - `test_wrong_encoding_is_file_error`：cp1252 字节 `b"\xa3"` 按 `utf-8` 解码，抛 `FileFormatError`。
  - `test_missing_column_is_file_error`：错误信息里包含缺失的列名。
  - `test_bad_row_is_row_error_not_exception`：日期为 `31/02/2026`、金额为 `"12.3.4"`、金额为 `"١٢"`、币种为 `"POUND"` 的行，各自变成 `RowError`，position 正确，前后的正常行照常解析。
  - `test_blank_lines_ignored_and_positions_contiguous`。
  - `test_datetime_dates_with_comma_format`：表头为 `Date,Description,Details,Money out (GBP),Money in (GBP),Balance (GBP)`，单元格带引号（`"15 Oct, 2026 09:05"` 和 `"1,000.00"`），配 `date_format="%d %b, %Y %H:%M"` 和 `local_tz=Europe/London`，这一行得到 `booking_date == date(2026, 10, 15)` 和 `occurred_at == datetime(2026, 10, 15, 9, 5, tzinfo=ZoneInfo("Europe/London"))`，描述由 `Description` 和 `Details` 连接而成。
  - `test_include_only_skips_before_parsing`：表头为 `Type,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance`，配 `include_only={"State": ["COMPLETED"]}` 和 `columns.date="Completed Date"`，一行 `PENDING`、`Completed Date` 和 `Balance` 都为空的行变成 `SkippedRow`，原因里写出 `State` 和 `PENDING`，而不是 `RowError`。
  - `test_fee_column`：金额 `-10.00`、手续费 `0.50`，得到 `quantity == -10.00` 和 `fee == 0.50`；手续费单元格为空时得到 `fee == 0`。
  - `test_currency_symbol_in_quoted_amounts`：`"£12,345.67"` 配 `thousands_separator=","`，得到 `12345.67`。
  - `test_too_many_rows`：`MAX_IMPORT_ROWS + 1` 行抛 `FileFormatError`（用 monkeypatch 把上限改成 3，测试保持小巧）。
- [ ] **Step 2: 运行** `uv run --locked pytest tests/finance/importing/test_csv_parser.py -q`；预期：FAIL。
- [ ] **Step 3: 实现** `parsed.py` 和 `csv_parser.py`。
- [ ] **Step 4: 运行**；预期：全部通过；`make backend-check` 全部通过。
- [ ] **Step 5: 提交** `feat: parse CSV statements through profiles`。

### Task 4: OFX 解析器

**Files:**
- Create: `.../importing/ofx_parser.py`, `backend/tests/finance/importing/test_ofx_parser.py`

**Interfaces:**
- Consumes: `ParsedRow`、`RowError`、`ParsedStatement`、`parse_amount`、`clean_description`、`MAX_IMPORT_ROWS`、`FileFormatError`（任务 3）；`OfxProfileConfig`（任务 2）。
- Produces: `PARSER_VERSION = "ofx-1"`；`def parse_ofx(content: bytes, config: OfxProfileConfig) -> ParsedStatement`。

规则：
- **解码。** 文件头写了 `ENCODING:UTF-8`，或者 XML 声明里写了 `encoding="UTF-8"` 时，按 UTF-8 解码；否则按 cp1252。无法解码的字节抛 `FileFormatError`。
- **分词。** 文件头之后的全部内容用 `<(/?)([A-Za-z0-9.]+)>([^<]*)` 分词。叶子节点的值是开标签后面的文本，去掉首尾空白，再做 `html.unescape`。聚合节点（`STMTTRN`、`STMTRS`、`CCSTMTRS`、`BANKTRANLIST`）在 OFX 1.x 和 2.x 里都是闭合的。
- **每个文件一份对账单。** `STMTRS` 或 `CCSTMTRS` 必须恰好有一个；没有或有多个都抛 `FileFormatError`（"export one account per file"）。
- **币种。** 每一行的币种取对账单的 `CURDEF`；交易里有 `CURRENCY/CURSYM` 或 `ORIGCURRENCY/CURSYM` 时，用它覆盖。
- **每个 `STMTTRN` 的字段。** `DTPOSTED` 必填。`TRNAMT` 必填，用 `parse_amount` 配 `AmountFormat()` 解析。`FITID` 作为 `external_id`（没有时为 `None`）。描述是 `clean_description([NAME, MEMO])`，缺的部分跳过。`raw` 保存这笔交易的所有叶子节点。
- **`DTPOSTED`。** 格式是 `YYYYMMDD[HHMMSS[.XXX]][[±H[H][.MM]][:TZ]]`。`booking_date` 取前 8 个字符。没有时间部分时 `occurred_at` 为 `None`；有时间部分时，它是带时区的时间，用方括号里的偏移量，没有偏移量时按 UTC。格式不对时是 `RowError`。
- **符号。** `TRNAMT` 本来就是从账户持有人的角度记的，所以 `inflow_positive` 原样使用，`outflow_positive` 取相反数。
- **手续费和余额。** 各行的 `fee = 0`，`balance = None`。
- **期末余额。** `LEDGERBAL/BALAMT` 作为 `closing_balance`（符号规则相同），`LEDGERBAL/DTASOF` 的日期作为 `closing_balance_date`。没有 `LEDGERBAL` 时两者都为 `None`。

- [ ] **Step 1: 写失败测试**，用两个直接写在测试模块里的合成文件（Example Bank，FITID 是假的）：
  - `test_sgml_v1_statement`：OFX 1.02 SGML，叶子标签不闭合，`CURDEF GBP`，两个 `STMTTRN`。得到两行，数量、外部 ID 正确，`currency == "GBP"`，`DTPOSTED 20261110` 对应 `occurred_at is None`。
  - `test_xml_v2_statement`：OFX 2.2 XML，带闭合标签。得到同样的行。
  - `test_dtposted_with_offset_keeps_written_date`：`20261110233000[-5:EST]` 得到 `booking_date == date(2026, 11, 10)` 和 `occurred_at == datetime(2026, 11, 11, 4, 30, tzinfo=UTC)`。
  - `test_entities_unescaped`：`<NAME>M&amp;S` 得到描述 `"M&S"`。
  - `test_credit_card_statement`：`CCSTMTRS` 里一笔消费 `-25.00`、一笔还款 `100.00`，符号保持不变。
  - `test_ledgerbal_is_closing_balance`：`<LEDGERBAL><BALAMT>1234.56<DTASOF>20261130` 得到 `closing_balance == Decimal("1234.56")` 和 `closing_balance_date == date(2026, 11, 30)`；没有它的文件得到 `None`。
  - `test_multiple_statements_rejected` 和 `test_no_statement_rejected`：都抛 `FileFormatError`。
  - `test_bad_trnamt_is_row_error`。
- [ ] **Step 2: 运行**；预期：FAIL。
- [ ] **Step 3: 实现** `ofx_parser.py`。
- [ ] **Step 4: 运行**；预期：全部通过；`make backend-check` 全部通过。
- [ ] **Step 5: 提交** `feat: parse OFX 1.x and 2.x statements`。

### Task 5: 暂存

**Files:**
- Create: `backend/migrations/versions/0010_finance_import_batches.py`, `.../importing/staging.py`, `backend/tests/finance/importing/test_staging.py`, `backend/tests/finance/importing/conftest.py`
- Modify: `.../finance/models.py`, `.../finance/errors.py`

**Interfaces:**
- Consumes:
  - 任务 1：`store_document`、`DocumentStore`；
  - 任务 2：`get_profile`；
  - 任务 3 和 4：`parse_csv`、`parse_ofx`，以及各自的 `PARSER_VERSION`；
  - 计划 1：`accounts.get_account`、`instruments.get_currency`、`instruments.get_instrument`、`get_timezone`、`to_plain_str`。
- Produces（在 `staging.py` 里）：
  - `BatchStatus = Literal["staged", "committed", "discarded", "rolled_back"]`。
  - `RowStatus = Literal["new", "duplicate", "skipped", "error", "committed", "rolled_back"]`。
  - `RowKind = Literal["statement", "opening_adjustment"]`。
  - `@dataclass(frozen=True) class ImportRowRecord`：`id, position, kind: RowKind, status: RowStatus, booking_date: date | None, occurred_at: datetime | None, description: str, quantity: Decimal | None, fee: Decimal | None, instrument_id: UUID | None, balance: Decimal | None, external_id: str | None, error: str | None, note: str | None, raw: dict[str, str], transaction_id: UUID | None`。
  - `@dataclass(frozen=True) class ImportBatchRecord`：`id, account_id, profile_id, profile_version: int, document_id, filename: str, status: BatchStatus, created_at, committed_at: datetime | None, rolled_back_at: datetime | None, counts: dict[RowStatus, int]`（每种状态都有，包括为 0 的），`warnings: list[str]`。
  - `OPENING_SETTING = "finance.opening_balance_account_id"`。`async def get_or_create_system_account(ctx, setting: str, name: str) -> UUID`：创建或复用一个 `equity`/`other`/`transactions` 账户，id 记在这个设置项里；设置项指向的账户不存在时，新建一个。任务 6 用它取两个系统账户（决策 1 和 13）。
  - `async def ledger_balance_before(session, account_id: UUID, instrument_id: UUID, day: date) -> Decimal`：账户在这种资产上的分录之和，只算所属交易未作废、记账日期早于 `day` 的。
  - `def opening_adjustments(rows: list[ParsedRow], instrument_of: Callable[[ParsedRow], UUID], closing: tuple[Decimal, date] | None, row_order: str) -> dict[UUID, tuple[Decimal, date]]`：纯函数，按资产返回对账单的期初余额和第一天（决策 13）。既没有余额也没有期末余额的资产不在结果里。
  - `def check_balance_chain(rows: list[ParsedRow], instrument_of: Callable[[ParsedRow], UUID], row_order: str) -> dict[int, str]`：纯函数；把余额链断开的地方映射成 position → 错误信息（决策 12）。调用方只传入可导入的行，解析出错的位置用 `None` 代替，让余额链重新开始。
  - `def dedupe_key(account_id: UUID, row: ParsedRow, currency: str, occurrence: int) -> str`：按决策 5 计算。
  - `async def stage_import(ctx, store: DocumentStore, *, account_id: UUID, profile_id: UUID, content: bytes, filename: str, media_type: str, default_instrument_id: UUID | None) -> ImportBatchRecord`
  - `async def get_batch(session, batch_id) -> ImportBatchRecord`；`async def list_batches(session) -> list[ImportBatchRecord]`（最新的在前）；`async def list_batch_rows(session, batch_id) -> list[ImportRowRecord]`（按 position 排序）。
  - 错误：`ImportNotAllowedError`（账户已关闭、不是手动账户，或余额模式是 `snapshots`）、`ImportBatchNotFoundError`。

迁移 `0010`：
- `finance.import_batches`。列：`id`；`account_id` FK；`profile_id` FK；`profile_version int`；`profile_config jsonb`（上传时解析配置的快照）；`parser_version text`；`document_id` FK `core.documents`；`filename text`；`default_instrument_id` FK NULL；`status text CHECK IN (...)`；`warnings jsonb NOT NULL DEFAULT '[]'`；`owner_entity_id`；`created_at`；`committed_at NULL`；`rolled_back_at NULL`。用 CHECK 把 `committed_at` 和 `committed`/`rolled_back` 两种状态绑定，把 `rolled_back_at` 和 `rolled_back` 绑定。
- `finance.import_rows`。列：`id`；`batch_id` FK `ON DELETE CASCADE`；`account_id` FK（为了建索引而冗余存放）；`position int`；`kind text NOT NULL DEFAULT 'statement' CHECK IN ('statement','opening_adjustment')`；`status text CHECK IN (...)`；`booking_date date`；`occurred_at timestamptz`；`occurred_tz text`；`description text NOT NULL DEFAULT ''`；`quantity numeric`；`fee numeric NOT NULL DEFAULT 0`；`instrument_id` FK；`balance numeric`；`external_id text`；`dedupe_key text`；`error text`；`note text`；`raw jsonb NOT NULL`；`transaction_id` FK `finance.transactions` NULL。
  - 约束：
    - `UNIQUE (batch_id, position)`；
    - `CHECK ((status = 'error') = (error IS NOT NULL))`；
    - 一条 CHECK 要求状态为 `new`、`duplicate`、`committed` 和 `rolled_back` 的行，`booking_date`、`occurred_at`、`quantity`、`instrument_id`、`dedupe_key` 都有值；跳过的行可以没有；
    - `CHECK ((kind = 'opening_adjustment') = (position < 0))`；
    - `CHECK ((status IN ('committed','rolled_back')) = (transaction_id IS NOT NULL))`；
    - `quantity`、`fee` 和 `balance` 上的 NaN/Infinity CHECK，写法同迁移 0005。
  - 索引：`CREATE UNIQUE INDEX import_rows_committed_once ON finance.import_rows (account_id, dedupe_key) WHERE status = 'committed'`。

`stage_import` 的规则：
1. **检查账户。** 账户必须未关闭、`source_kind == "manual"`，余额模式是 `transactions` 或 `hybrid`；否则抛 `ImportNotAllowedError`，信息里说明原因。
2. **默认币种。** 给了 `default_instrument_id` 时，它必须是 fiat 资产，否则抛 `NotACurrencyError`。
3. **存文档。** `store_document(ctx, store, content, filename=..., media_type=..., source="finance.import")`。
4. **解析**，按 `profile.config.format` 选解析器，`local_tz` 取 `ZoneInfo(profile timezone)` 或 `get_timezone(session)`。`FileFormatError` 直接往外抛，`command_context` 回滚全部写入（已经存下的文件可能还留着；它按内容寻址，留着也无害）。
5. **给每一行分类：**
   - `SkippedRow` 变成 `skipped`，原因写进 `note`；
   - `RowError` 变成 `error`；
   - 否则确定币种：有行内币种代码时用 `get_currency` 查，没有就用默认资产的代码。代码不认识，或者根本没有币种，都变成 `error`，信息里提示使用者在上传时选一个币种，或者在解析配置里映射币种列；
   - `quantity == 0` 且 `fee == 0` 变成 `skipped`，note 为 "zero amount"；
   - 否则计算去重键，出现次数按决策 5 计数；
   - 账户上已经有已提交的行用了这个键，或者本批次前面的行用了这个键，这一行就是 `duplicate`；否则是 `new`。
6. **余额链。** 对仍是 `new` 或 `duplicate` 的行运行 `check_balance_chain`，解析出错的行作为重置点一并传入。报出来的每个 position 都变成 `error`，带上对应的信息（决策 12）。
7. **期初调整。** 只针对 `transactions` 账户，而且只在批次里没有 `error` 行时才做：按资产把 `opening_adjustments(...)` 和 `ledger_balance_before(...)` 比较。每有一处差额，就按决策 13 加一行：`kind='opening_adjustment'`，状态 `new`，`dedupe_key = sha256("open", account, date, quantity, currency)`。`position` 按资产 symbol 的顺序依次为 `-1, -2, …`。拿不到余额时，改为给批次加上决策 13 里的那条警告。
8. **时间。** `occurred_at` 取行里的值；没有时取 `booking_date` 在本地时区（第 4 步）的零点，`occurred_tz` 设为该时区名。
9. **事件。** emit `finance.import.staged`，带上批次 id 和各状态的行数。

在 `tests/finance/importing/conftest.py` 里加一个 `store` fixture（`LocalDocumentStore(tmp_path)`）和一个 `csv_profile` fixture（Example Bank 的 CSV 解析配置，列是 `Date,Description,Amount`，日期格式 `%d/%m/%Y`）。

- [ ] **Step 1: 写失败测试：**
  - `test_stage_counts_and_rows`：一个 3 行的文件（一行正常、一行日期错误、一行金额为 0），行数统计是 `new 1, error 1, skipped 1`；行按 position 排序；错误行的信息里提到日期。
  - `test_identical_rows_in_one_file_are_both_new`：两行完全相同的 `10/11/2026,Coffee,-3.50`，都是 `new`，`dedupe_key` 不同。
  - `test_external_id_duplicate_within_file`：一个 OFX 文件里同一个 FITID 出现两次，先是 `new`，后是 `duplicate`。
  - `test_snapshots_account_refused`、`test_closed_account_refused`：都抛 `ImportNotAllowedError`。
  - `test_file_format_error_persists_nothing`：表头有误时抛 `FileFormatError`，不留下任何批次或文档记录。
  - `test_currency_resolution`：没有币种列也没有默认币种时，每一行都是 `error`；默认币种为 GBP 时，各行是 `new`，资产 id 是 GBP 的；币种列里写 `XYZ` 的那一行是 `error`。
  - `test_date_only_rows_keep_booking_date_across_dst`：日期为 `29/03/2026` 和 `25/10/2026`（英国夏令时切换日）的行，在 `core.timezone` 为 `Europe/London` 时保持这两个记账日期，`occurred_at` 是本地零点（`2026-03-29T00:00Z` 和 `2026-10-24T23:00Z`）。
  - `test_batch_snapshots_profile`：暂存之后再修改解析配置，批次的 `profile_version` 和 `profile_config` 不变。
  - `test_balance_chain_break_is_error`：一个 3 行的文件，余额依次为 100.00、90.00、95.00，金额依次为 +100.00、−10.00、+10.00，第三行是 `error`，信息里包含 `expected 100.00` 和 `95.00`。
  - `test_balance_chain_catches_wrong_sign`：一个正确的文件配 `sign="outflow_positive"` 暂存，第一行之后的每一行都是 `error`。
  - `test_balance_chain_ignores_skipped_rows`：两行连续的带余额行中间夹一行被过滤掉的 `PENDING` 行，余额链不会断。
  - `test_newest_first_chain`：同样的行倒序排列，配 `row_order="newest_first"`，不报任何错误。
  - `test_opening_adjustment_from_first_balance`：第一次导入，第一行是 `-18.00`、余额 `1482.00`，加一行数量为 `1500.00` 的 `opening_adjustment`，日期是第一行的前一天。
  - `test_no_adjustment_when_ledger_matches`：提交期初那个批次之后，再暂存下个月的对账单（它从上个月的期末余额开始），不加调整行。
  - `test_gap_creates_adjustment`：因为跳过了一个月，暂存的对账单起始余额高于账本，加一行调整，数量是差额。
  - `test_hybrid_account_has_no_adjustment` 和 `test_no_balances_adds_warning`。
  - `test_opening_from_ofx_ledgerbal`：一个有 `LEDGERBAL`、没有流水余额的 OFX 文件，期初余额由它推算。
  - `test_multi_currency_chains_separately`：一个带币种列的文件，GBP 行和 EUR 行各自带着自己的余额，不报余额链错误，每个币种各加一行调整。
- [ ] **Step 2: 运行**；预期：FAIL。
- [ ] **Step 3: 实现** 迁移、ORM 模型和 `staging.py`。`check_balance_chain` 和 `opening_adjustments` 保持为纯函数，通过 `stage_import` 来测试它们。
- [ ] **Step 4: 运行**；预期：全部通过；`make backend-check` 全部通过。
- [ ] **Step 5: 提交** `feat: stage imported statements with deduplication`。

### Task 6: 提交入账、丢弃、回滚

**Files:**
- Create: `.../importing/commit.py`, `backend/tests/finance/importing/test_commit.py`
- Modify: `.../finance/errors.py`

**Interfaces:**
- Consumes:
  - 任务 5：全部；
  - 计划 1：`ledger.post_transaction`、`ledger.void_transaction`、`TransactionInput`、`PostingInput`、`accounts.create_account`、`get_setting`/`set_setting`、`valuation.balances_asof`。
- Produces:
  - `CONTRA_SETTING = "finance.import_contra_account_id"`。对方账户由 `get_or_create_system_account(ctx, CONTRA_SETTING, "Unassigned (imports)")` 取得（决策 1），期初账户由 `get_or_create_system_account(ctx, OPENING_SETTING, "Opening balances")` 取得（决策 13）。
  - `async def commit_batch(ctx, batch_id: UUID) -> ImportBatchRecord`
  - `async def discard_batch(ctx, batch_id: UUID) -> ImportBatchRecord`
  - `async def rollback_batch(ctx, batch_id: UUID) -> ImportBatchRecord`
  - 错误：`ImportBatchStateError`（批次状态不允许这个操作）、`ImportHasErrorsError`、`ImportBatchStaleError`（暂存之后账本有变化，决策 13）。

规则：
- 每个操作都先用 `SELECT … FOR UPDATE` 锁住批次这一行。
- **`commit_batch`：**
  - 状态必须是 `staged`。
  - 不能有 `error` 行。
  - 用和暂存时相同的规则重新检查账户，因为它的余额模式可能已经改了。
  - 对 `transactions` 账户，用 `opening_adjustments` 对批次的对账单行、加上 `ledger_balance_before`，重新算出每种资产预期的调整额。如果和暂存时的调整行不一致（包括多出或少了一行调整），抛 `ImportBatchStaleError("the account changed since this file was uploaded; discard it and upload again")`。
  - 然后按 position 处理每一个 `new` 行（调整行的 position 是负数，所以排在最前）：
    - 如果此时已经有同一个 `(account_id, dedupe_key)` 的已提交行，这一行变成 `duplicate`；
    - 否则调用 `post_transaction`，传入 `occurred_at`、`occurred_tz`、`booking_date` 和 `description`，所有分录都用这一行的资产：
      - 没有手续费的对账单行：账户上 `+quantity`，对方账户上 `−quantity`；
      - 有手续费的对账单行（决策 11）：账户上 `+quantity`，账户上 `−fee` 并带 `is_fee=True`，对方账户上 `−(quantity − fee)`；
      - 调整行：账户上 `+quantity`，期初账户上 `−quantity`。

      这一行变成 `committed`，记下 `transaction_id`。
  - 最后批次变成 `committed`，`committed_at = now()`，emit `finance.import.committed`，带上各状态的行数。
- **`discard_batch`：** `staged` → `discarded`。
- **`rollback_batch`：**
  - 状态必须是 `committed`。
  - 对每一个 `committed` 行，作废它的交易（已经作废的跳过）；这一行变成 `rolled_back`。
  - 批次变成 `rolled_back`，记下 `rolled_back_at`，emit `finance.import.rolled_back`。

- [ ] **Step 1: 写失败测试**（先用任务 5 的 fixture 通过 `stage_import` 暂存，再执行操作）：
  - `test_commit_posts_balanced_transactions`：提交一个 2 行的文件后，`balances_asof` 显示账户余额等于各行之和，对方账户余额是它的相反数，净值只变化了账户那部分的和。
  - `test_reimporting_same_file_marks_all_duplicate`：提交后，把相同的字节再暂存一次：所有行都是 `duplicate`，提交第二个批次不会记任何交易。
  - `test_overlapping_export_commits_only_new_rows`：先提交 11 月 1–10 日的行，再暂存一份 11 月 5–15 日的导出：重叠的行是 `duplicate`，其余是 `new`。
  - `test_commit_rechecks_duplicates`：同一个文件暂存成批次 A 和 B，先提交 A，再提交 B：B 的行最后都是 `duplicate`，不报错。
  - `test_errors_block_commit`：抛 `ImportHasErrorsError`，批次仍是 `staged`。
  - `test_commit_twice_refused`：抛 `ImportBatchStateError`。
  - `test_commit_refused_after_account_switched_to_snapshots`：抛 `ImportNotAllowedError`。
  - `test_rollback_voids_and_allows_reimport`：回滚后账户余额回到之前的值，各行是 `rolled_back`，再暂存同一个文件得到的是 `new` 行。
  - `test_rollback_skips_already_void_transactions`：先手动作废其中一笔交易；回滚成功，而且不会为它再 emit 一次作废事件。
  - `test_liability_import_signs`：在负债账户上，用 `inflow`/`outflow` 格式的文件导入一笔消费（outflow 25.00）和一笔还款（inflow 100.00），得到数量 `-25.00` 和 `100.00`。
  - `test_contra_account_reused`：两个已提交的批次用的是同一个对方账户 id。
  - `test_fee_posting`：金额 `-10.00`、手续费 `0.50` 的一行记成三条分录，手续费那条带 `is_fee`，账户余额变化 `-10.50`。
  - `test_opening_adjustment_committed_and_rolled_back`：提交第一次导入后，账户余额等于文件的期末余额；"Opening balances" 账户上是期初金额的相反数；净值等于期末余额。回滚时调整行也一并作废。
  - `test_stale_batch_refused`：先暂存第 2 个月（因为缺了第 1 个月，需要一行调整），再暂存并提交第 1 个月，然后提交第 2 个月。抛 `ImportBatchStaleError`；重新暂存第 2 个月，不再有调整行。
  - `test_pending_then_completed_counted_once`：一份导出里某笔付款是 `PENDING`（被跳过），提交；之后另一份导出里同一笔付款是 `COMPLETED`。第二个批次里它是 `new`，账户里它恰好只算一次。
  - `test_discard`：staged → discarded；之后再提交它，抛 `ImportBatchStateError`。
- [ ] **Step 2: 运行**；预期：FAIL。
- [ ] **Step 3: 实现** `commit.py`。
- [ ] **Step 4: 运行**；预期：全部通过；`make backend-check` 全部通过。
- [ ] **Step 5: 提交** `feat: commit, discard and roll back import batches`。

### Task 7: REST API 与示例文件

**Files:**
- Create:
  - 示例文件 `.../importing/examples/example-bank-csv.json`、`example-bank.csv`、`example-bank-ofx.json`、`example-bank.ofx`；
  - 测试 `backend/tests/api/test_import_api.py`。
- Modify: `backend/pyproject.toml` + `uv.lock`（加 `python-multipart`，Apache-2.0）, `backend/src/lararium/api/app.py`, `backend/src/lararium/deps.py`, `.../finance/schemas.py`, `.../finance/api.py`, `web/openapi.json`, `web/src/api/*`（重新生成）。

**Interfaces:**
- Consumes: 任务 1–6。
- Produces:
  - **接线。**
    - 在 `create_app` 里：`app.state.document_store = LocalDocumentStore(settings.document_dir)`。
    - 在 `deps.py` 里：`def get_document_store(request: Request) -> DocumentStore` 和 `DocumentStoreDep`。
    - 在 API 测试的 `conftest` 里：把 `LARARIUM_DOCUMENT_DIR`（或 `Settings` 对象）指向 `tmp_path`。
  - **Schema。** 金额用 `DecimalStr`。
    - `ImportProfileCreate { name: str (1–100 chars), config: ProfileConfig }`。
    - `ImportProfileOut { id, name, config, version, updated_at }`。
    - `ImportBatchOut { id, account_id, profile_id, profile_version, document_id, filename, status, created_at, committed_at, rolled_back_at, counts: dict[str, int], warnings: list[str] }`。
    - `ImportRowOut { id, position, kind, status, booking_date, description, quantity: DecimalStr | None, fee: DecimalStr | None, instrument_id, balance: DecimalStr | None, external_id, error, note, raw: dict[str, str], transaction_id }`。
    - `ImportBatchDetailOut(ImportBatchOut) { rows: list[ImportRowOut] }`。
  - **路由。** 路由名就是 operationId：

    | 方法和路径 | 名称 | 响应 |
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

    `stage_import` 接收 multipart 表单字段 `account_id`、`profile_id`、可选的 `instrument_id`，以及 `file`。它的审计参数是这几个 id 加上 `filename`、`size_bytes` 和 `sha256`，绝不记录文件内容。
  - **上传上限。** 路由按 64 KiB 一块读取上传内容，超过 `MAX_IMPORT_BYTES` 就停止，返回 413 `{"error": "file_too_large"}`。空文件返回 422 `file_format`。
  - **错误映射。**

    | 错误 | 状态码 | 错误码 |
    |---|---|---|
    | `ImportProfileNotFoundError` | 404 | `import_profile_not_found` |
    | `ImportBatchNotFoundError` | 404 | `import_batch_not_found` |
    | `DuplicateProfileNameError` | 409 | `duplicate_profile_name` |
    | `ImportNotAllowedError` | 422 | `import_not_allowed` |
    | `FileFormatError` | 422 | `file_format` |
    | `ImportBatchStateError` | 409 | `import_batch_state` |
    | `ImportHasErrorsError` | 409 | `import_has_errors` |
    | `ImportBatchStaleError` | 409 | `import_batch_stale` |

  - **示例文件。** 全部是合成的，机构是 "Example Bank"。
    - `example-bank.csv`：日期格式 `dd/mm/yyyy`，有 `Paid in`/`Paid out`/`Balance` 三列（流水余额从期初余额 1,000.00 开始，所以第一次导入会加一行期初调整），用了千位分隔符，包含一行金额为 0 的行和同一天两笔相同的消费，大约 12 行。
    - `example-bank-csv.json`：它的解析配置。
    - `example-bank.ofx`：OFX 1.x SGML，GBP，5 笔带 FITID 的交易。
    - `example-bank-ofx.json`：`{"format": "ofx"}`。

- [ ] **Step 1: 写失败测试：**
  - `test_example_csv_end_to_end`：用 `example-bank-csv.json` 创建解析配置，建一个手动的 `transactions` 账户，以 GBP 上传 `example-bank.csv`：返回 201，`counts.new` 等于金额非 0 的行数，`counts.skipped == 1`。然后提交：持仓显示账户余额等于文件的期末 `Balance`。再上传一次：所有行都是 `duplicate`。
  - `test_example_ofx_end_to_end`：用 OFX 示例走同样的流程。
  - `test_upload_too_large_is_413`：上传 `MAX_IMPORT_BYTES + 1` 字节。
  - `test_upload_to_snapshots_account_is_422`：错误码 `import_not_allowed`。
  - `test_profile_config_validation_is_422` 和 `test_duplicate_profile_name_is_409`。
  - `test_cross_site_upload_refused`：带 `Sec-Fetch-Site: cross-site` 的 multipart POST 返回 403。
  - `test_import_operation_ids`：上面九个名称都出现在 `render_openapi()` 里。
  - `test_rollback_via_api`：提交后回滚：状态是 `rolled_back`，持仓回到 0。
- [ ] **Step 2: 运行** `uv run --locked pytest tests/api/test_import_api.py -q`；预期：FAIL。
- [ ] **Step 3: 实现** 依赖（`uv add python-multipart`）、接线、schema、路由、错误映射和示例文件。然后运行 `make openapi`。
- [ ] **Step 4: 运行** `make check`；预期：全部通过，`web/openapi.json` 和 `web/src/api` 已重新生成并提交。
- [ ] **Step 5: 提交** `feat: add import REST API with Example Bank samples`。

### Task 8: 网页界面

**Files:**
- Create: `web/src/pages/ImportsPage.tsx`, `ImportBatchPage.tsx`, `ImportProfilesPage.tsx`，每个页面各配一个 `.test.tsx`。
- Modify: `web/src/router.tsx`。

**Interfaces:**
- Consumes: 生成的客户端函数 `listImportProfiles`、`createImportProfile`、`updateImportProfile`、`stageImport`、`listImportBatches`、`getImportBatch`、`commitImport`、`discardImport`、`rollbackImport`、`listAccounts`、`listInstruments`、`getFinanceSettings`；`lib/format.ts` 里的 `formatMoney`/`formatQuantity`。
- Produces:
  - 路由 `/imports`、`/imports/$batchId`、`/import-profiles`；
  - 导航项 "Imports" 和 "Profiles"，放在 "Prices" 后面。

页面行为：
- **`/imports`（ImportsPage）。**
  - **上传表单：**
    - "Account"：只列出未关闭、余额模式为 `transactions` 或 `hybrid` 的手动账户；一个都没有时显示提示；
    - "Profile"；
    - "Currency"：fiat 资产，默认是报告币种；另有一个选项 "From the file"，给映射了币种列的解析配置或 OFX 用；
    - "File"。
  - **提交表单**时用表单数据调用 `stageImport`。成功后跳转到批次页；失败时显示服务端返回的 `message`。
  - **表单下方**是批次列表：文件名、账户、状态、各状态行数、创建时间。
- **`/imports/$batchId`（ImportBatchPage）。**
  - **页头：** 各状态的行数，以及批次的 `warnings`。
  - **调整行**排在最前面，标签是 "Opening balance adjustment"。如果账户上已经有导入过的历史，标签改为 "Balance gap: a statement period may be missing"。
  - **行表格：** position、日期、描述、金额（用 `formatMoney(quantity, code)`，`code` 是这一行资产的 ISO 代码）、状态标签；错误行显示错误信息，跳过的行显示 note。
  - **批次是 `staged` 时：**
    - `counts.error > 0` 时 "Commit" 不可点，并显示 "Fix the errors in the file or profile and upload again"；
    - 可以 "Discard"。
  - **批次是 `committed` 时：** "Roll back" 要先确认（"This voids N transactions"）。
  - **查询缓存：** 任何操作之后，让 `["imports"]`、`["import-batch", id]`、`["holdings"]` 和 `["net-worth"]` 失效。
- **`/import-profiles`（ImportProfilesPage）。** 解析配置列表，加一个新建/编辑表单，字段是 "Name" 和一个等宽字体的 "Config (JSON)" 文本框，占位内容是一份最简的 CSV 配置。发送前先对配置做 `JSON.parse`；解析失败时显示 "Config is not valid JSON"，不发送。服务端返回 422 时显示它的信息。
- **任何地方都不对金额用 `Number(...)` 或 `parseFloat`。**

- [ ] **Step 1: 写失败测试**（像现有页面测试那样 mock `../api`）：
  - `ImportsPage`：`test_upload_sends_form_fields`：选好账户、解析配置和文件后提交，调用 `stageImport`，`body` 里包含 `account_id`、`profile_id`、`instrument_id`（GBP）和那个 `File`。`test_only_importable_accounts_listed`：`snapshots` 账户和已关闭的账户不出现在选项里。
  - `ImportBatchPage`：`test_commit_disabled_with_errors`；`test_rollback_requires_confirmation`：点确认按钮之前不调用 `rollbackImport`；`test_amounts_rendered_from_strings`：数量 `"-1234.56"` 显示为 `-£1,234.56`。
  - `ImportProfilesPage`：`test_invalid_json_not_sent`；`test_server_validation_message_shown`。
- [ ] **Step 2: 运行** `cd web && pnpm test --run`；预期：FAIL。
- [ ] **Step 3: 实现** 三个页面和路由，复用 `LabelledSelect`、`TextField` 和 `ui/` 里的组件。
- [ ] **Step 4: 运行** `make check`；预期：全部通过。
- [ ] **Step 5: 提交** `feat: add import and profile pages to the web UI`。

### Task 9: 部署、备份与文档

**Files:**
- Modify: `backend/Dockerfile`, `infra/compose.yaml`, `infra/backup/backup.sh`, `backend/tests/infra/test_compose.py`, `backend/tests/infra/test_backup.py`, `README.md`, `README.zh-CN.md`。

**Interfaces:**
- Consumes: `Settings.document_dir`（任务 1）。
- Produces: 具名卷 `documents`，挂载到 `api` 的 `/var/lib/lararium/documents`；每个 `.dump` 旁边多一个备份文件 `documents-<UTC 时间戳>.tar`。

改动：
- **`backend/Dockerfile`。** 在 `USER lararium` 之前运行 `mkdir -p /var/lib/lararium/documents && chown lararium /var/lib/lararium/documents`。新的具名卷第一次挂载时会沿用这个属主。
- **`compose.yaml`。**
  - `api` 加上 `volumes: [documents:/var/lib/lararium/documents]` 和 `LARARIUM_DOCUMENT_DIR: /var/lib/lararium/documents`。
  - 在 `volumes` 下加 `documents: {}`。
  - `migrate` 不挂载它。
- **`backup.sh`。**
  - 导出数据库之后，执行 `"${compose[@]}" exec -T api tar -C /var/lib/lararium/documents -cf - . > "$out_docs.partial"`，再 `mv` 到最终位置，时间戳和 dump 用同一个。
  - restic 两个都备份。
  - `api` 容器不存在时，备份要明确失败，而不是只写一个不含文档的 dump。
- **README，中英两份：**
  - 加一节 "Importing statements"：
    - 流程（上传 → 审阅 → 提交入账，回滚）；
    - 哪些账户能导入（决策 3）；
    - 解析配置的格式，列出任务 2 里的每一个键；
    - 流入/流出的符号规则，附信用卡的例子；
    - 怎么识别重复（决策 5）；
    - "Unassigned (imports)" 账户（决策 1）；
    - Example Bank 示例文件在哪里。
  - 在备份一节里写明：文档以 tar 的形式和 dump 放在一起备份；恢复时把它解包到 `lararium_documents` 卷（给出一行命令 `docker run --rm -v lararium_documents:/d -i alpine tar -C /d -xf - < documents-….tar`）。

- [ ] **Step 1: 写失败测试：**
  - `test_compose.py`：`api` 把 `documents` 挂载到 `/var/lib/lararium/documents` 并设置了 `LARARIUM_DOCUMENT_DIR`；`migrate` 和 `web` 不挂载它。
  - `test_backup.py`：脚本里引用了 `/var/lib/lararium/documents`，先写 `.partial` 再 `mv`，并且在两个输出之前都保持 `umask 077`。
- [ ] **Step 2: 运行**；预期：FAIL。
- [ ] **Step 3: 实现**；写好两份 README（中文版从英文版翻译）。
- [ ] **Step 4: 验证。**
  - 运行 `make check`；预期：全部通过。
  - 然后跑一遍 compose 冒烟测试：
    1. 在一套全新的卷上 `make up`。
    2. 以所有者身份通过 `127.0.0.1:8080` 创建示例解析配置和一个手动的 `transactions` 账户。
    3. 上传 `example-bank.csv` 并提交入账。
    4. 运行 `make backup`。预期：`.dump` 和 `documents-….tar` 都存在，tar 里列出一个文件。
  - 最后 `make down`。
- [ ] **Step 5: 提交** `feat: persist and back up import documents`。

---

## 自查记录

- **规格覆盖：7.8 节。**
  - 文件存成文档：任务 1、任务 5。
  - 按机构区分、以数据形式保存的解析配置：任务 2。
  - 通用的 CSV 映射器：任务 3。
  - OFX：任务 4。
  - 暂存区显示新增、重复和错误的行：任务 5 和 8。
  - 提交入账：任务 6。
  - 批次回滚：任务 6、7 和 8。
  - 示例配置只配合成的文件：任务 7。
  - 负债走同一套流程：任务 6 的 `test_liability_import_signs`。
  - 金融科技账户的多币种导出：解析配置里的币种列，任务 3 和 5。
  - 和推断分录的配对推迟到 M4（上文已说明）。
- **规格覆盖：6.2 节的文档。** 内容哈希、媒体类型、大小、原始文件名和来源，存储是放在可替换接口后面的本地目录：任务 1。
- **规格覆盖：6.4 节的防重复。** 决策 5，以及任务 5 和 6。
- **类型。** `ParsedRow`、`RowError` 和 `ParseResult` 只在任务 3 定义一次，任务 4 和 5 使用。记录类型在任务 5 定义，任务 6 返回。API 名称和任务 8 用的客户端函数名对得上，因为 hey-api 会把 operationId 转成驼峰。
