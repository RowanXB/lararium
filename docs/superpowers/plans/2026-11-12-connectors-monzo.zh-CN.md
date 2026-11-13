# connector 框架与 Monzo Implementation Plan

**语言：** [English](./2026-11-12-connectors-monzo.md) | 简体中文

本文是英文版的中文译文；两者如有出入，以英文版为准。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 通过官方 API 接入使用者的 Monzo 账户，让账户、pot、余额和交易自动同步进账本：既按计划定时同步，也在收到 Monzo webhook 后一分钟内同步。连接出问题时，在飞书上通知使用者。

**Architecture:**
- **通用核心。** `core` 里放一个不带财务色彩的 connector 框架：连接、加密的凭证、不可修改的原始记录、webhook 收件箱、通知器。
- **Worker。** 由一个 Procrastinate worker 进程执行同步。
- **Monzo 模块。** `lararium.modules.finance.monzo` 包里放 API 客户端、OAuth 流程、同步（API → 原始记录）和带版本号的 normalizer（原始记录 → 账本）。
- **Webhook。** webhook 经 Cloudflare Tunnel 到达一个独立的极小接收器。接收器只能往收件箱里插入数据；worker 把每次投递都只当作“马上同步”的信号，从不信任其内容。

**Tech Stack:** 同计划 1，另加：
- `procrastinate`（MIT）：任务和定时计划都存在 PostgreSQL 里；
- `cryptography`（Apache-2.0/BSD）：AES-256-GCM；
- `httpx`（BSD），从开发依赖移到运行时依赖；
- `starlette`（BSD），本来就是 FastAPI 的依赖；
- `cloudflare/cloudflared`（Apache-2.0），以容器镜像的形式使用。

**Spec:** `docs/superpowers/specs/2026-11-11-financial-gate-phase1-design.md`（本计划覆盖 M3：§7.1、§7.2、§7.9、§11.1–11.2，以及 §11.5 里通知的部分）

**基于：** 已合并进 `main` 的计划 1（`aef64ee`，迁移到 `0007` 为止）。暂缓的 M2 计划（`2026-11-12-manual-import.md`）**不是**前置条件。

## 开工前：使用者要先准备好的东西

这些都在代码之外，任务 10 写的 README 会逐项讲怎么做。实现和测试都用不到它们，只有最后的真实账户检查（任务 10 第 5 步）需要。

1. **Tailscale 和 `tailscale serve`** 在部署环境里能正常工作。OAuth 回调会在使用者的浏览器里打开 Lararium 的地址，而这个地址只能经 tailnet 访问。
2. **一个 Monzo OAuth client**，在 developers.monzo.com 上创建，类型选 **Confidential**。重定向地址是 `<LARARIUM_PUBLIC_BASE_URL>/api/v1/connections/oauth/monzo/callback`。
3. **一个 Cloudflare Tunnel**，配一个公网主机名（比如 `hooks.<domain>`）指向 `http://webhook-in:8000`，再加一条 WAF 自定义规则：除了对 `/monzo/*` 的 `POST`，其他一律拦截。
4. **一个飞书自定义机器人**，放在某个群里，开启签名校验：得到一个 webhook URL 和一个签名密钥。

## 决策

规格里留给实现者的选择，在这里先定下来，任务里就不用再纠结：

1. **本计划依赖的 Monzo 文档事实**（docs.monzo.com，2026-11-12 核对）：
   - **OAuth。** 在 `https://auth.monzo.com/` 授权；用 `POST https://api.monzo.com/oauth2/token` 换取和刷新 token。只有 confidential client 能拿到 refresh token。refresh token 只能用一次：每次刷新都返回一对新 token，旧的随即失效。access token 有效期约 6 小时（`expires_in`）。
   - **确认。** 新 token 要等使用者在 Monzo App 里确认（SCA）之后才能用。确认后的 **5 分钟内**可以拉取全部历史；过了这个窗口，就只能拉最近 90 天。
   - **接口。** `GET /accounts`、`GET /balance?account_id=`、`GET /pots?current_account_id=`，以及 `GET /transactions?account_id=&since=&before=&limit=`（`limit` 最大 100；`since` 可以是时间戳，也可以是交易 id；`expand[]=merchant`）。webhook：`POST`、`GET` 和 `DELETE /webhooks`。
   - **金额。** 以最小单位的整数表示；负数表示钱流出。`settled == ""` 表示尚未结算。被拒的交易带有 `decline_reason`。
   - **Webhook。** 只有 `transaction.created` 一种。投递不带签名，失败最多重试 5 次。
   - **文档里没写、要在任务 10 用真实账户核实的：** 对方信息的字段；pot 转移在返回里是什么样子（预计有 `metadata.pot_id`）；`local_amount`/`local_currency`；被撤销的 pending 付款会怎样。
2. **Worker。** Procrastinate 用它自己的 schema `procrastinate`，由一个 Alembic 迁移根据 Procrastinate 自带的 schema SQL 创建。任务有：
   - `sync_connection(connection_id)`，带 `queueing_lock=f"sync:{id}"` 和 `lock=f"sync:{id}"`，所以每个连接最多只有一个同步在排队、一个在运行；
   - `schedule_syncs`，每 30 分钟定时运行一次，为每个状态是 `healthy` 的连接安排一次同步；
   - `process_webhook_inbox`，每分钟定时运行一次；
   - `await_approval(connection_id)`，在 OAuth 回调之后运行（决策 7）。

   所以 webhook 触发的同步大约一分钟内就会开始。
3. **凭证**（规格 §11.2）。
   - **加密。** AES-256-GCM，32 字节的主密钥来自 `LARARIUM_MASTER_KEY`（base64），只在 `core/secrets.py` 里使用。关联数据（associated data）是 `f"{connection_id}:{purpose}"`，这样密文没法挪到别的连接上用。每一行记录 `key_version`（1），以便以后轮换密钥。
   - **明文的处理。** 解密出来的值包在 `Secret` 里传递，它的 `repr` 和 `str` 都是 `"Secret(***)"`。代码只在发起 HTTP 调用的地方才调用 `.reveal()`。
   - **绝不外露。** 凭证不出现在日志、事件、审计参数、API 响应或通知里。
4. **token 轮换要扛得住崩溃。** 因为 refresh token 只能用一次，新的一对 token 要先加密，并在**单独的一个短事务**里提交，然后才使用。之后不管哪里出错，下次运行手里仍然有有效的 refresh token。刷新被 Monzo 拒绝（`invalid_grant`，或 token 接口返回 401）时，连接转为 `needs_reauth`，并通知使用者。
5. **账本映射。**
   - **账户。** 每个 Monzo 账户对应一个财务账户：`asset`/`bank`，`source_kind='connection'`，余额模式 `transactions`，名称按 `description` 取（`uk_retail` → "Monzo"，`uk_retail_joint` → "Monzo Joint"）。每个 pot 对应一个子账户：`asset`/`savings_pot`，余额模式 `hybrid`：第一条快照之前的历史来自 pot 转账，之后来自 pot 快照（类型 `reported`），每次同步都用 `pots[].balance` 记一条。pot 被删掉时，对应账户关闭。
   - **交易。** 每笔没被拒的交易对应一笔账本交易，以 `(connection_id, external_id = transaction id)` 为键：
     - `occurred_at = created`，`booking_date` = `created` 的本地日期，`value_date` = `settled` 的本地日期（没有就是 `None`）；
     - `settled == ""` 时状态是 `pending`，否则是 `booked`；
     - 描述：有商户就用商户名，没有就用对方名称，再没有就用 `description`；`memo = notes`。
   - **分录。** Monzo 账户一侧是 `amount/10^decimals`，另一侧是下面之一：
     - **pot 转移**（有 `metadata.pot_id`）：对应 pot 的账户；
     - 其他情况：系统账户 **"Unassigned"**，以后由转账匹配（M4）改指向正确的账户。
   - **被拒的交易**不记账，但保留它的原始记录。
   - **商户**通过 `get_or_create_organization` 变成 `organization` 实体，并设置 `external_ids["monzo_merchant"]`，用作交易的对方。
6. **上游变化时更新同一笔账本交易。** 交易在上游发生变化（结算了、金额变了、描述被补全了）时，它的字段和分录**就地替换**：交易 id 不变，换上新的分录行。这次更新会发出 `finance.transaction.updated`。
   - 只允许对 `origin='connector'` 的交易这样做；手动录入的交易绝不动。
   - 一笔 pending 交易再次返回时如果 `amount == 0` 或带有 `decline_reason`，就把它作废（void）。

   选择就地替换，而不是先作废再重记，是为了让每笔 Monzo 交易在账本里只有一行，这就是这里“唯一权威来源”的含义。
7. **OAuth 流程。**
   - `authorize_connection` 把 `sha256(state)` 和 `expires_at = now + 10 min` 存进连接的 config，并返回 Monzo 的授权地址。
   - 回调路由用 code 换 token，保存 token，把状态设为 `authorising`，安排 `await_approval`，然后重定向（303）到 `/connections`。
   - `await_approval` 每 10 秒轮询一次 `GET /accounts`，最多 4 分钟。第一次成功后，立刻在 5 分钟窗口内做一次**全量回填**（不带 `since`），然后设为 `healthy`。超时则设为 `needs_reauth`，消息是 "Approve the Monzo request in the app within 4 minutes, then authorise again"。
   - **回调是一次跨站导航**，从 auth.monzo.com 过来，而计划 1 的跨站规则会拒绝它。只有这一个路由在请求是顶层导航（`Sec-Fetch-Mode: navigate` 且 `Sec-Fetch-Dest: document`）时豁免。它仍然要求带使用者请求头，并且 `state` 必须匹配、未过期、只用一次。
8. **同步窗口和截断检查。**
   - **增量同步**从 `last_seen_created − 14 days` 开始拉交易（这段重叠用来抓住后来结算或有变化的 pending 交易），用 `since=<last id>` 和 `limit=100` 翻页。
   - **在确认窗口之外**，早于 89 天的 `since` 会被截到 89 天，并把这次截断记为连接的一条警告。
   - **全量回填之后**，如果最早的一笔交易比账户的 `created` 晚 7 天以上，连接会得到一条警告 "history may be truncated"。
9. **期初余额和对账。**
   - **期初余额只设一次。** 某个账户第一次全量回填之后，把账本余额（包括 pending，不包括 void）和 `GET /balance` 的 `balance` 比较。差额只记一次，记成一笔 "Opening balance adjustment" 交易（`external_id = f"opening:{monzo_account_id}"`），记账日期是最早那笔交易的前一天，另一侧是系统账户 **"Opening balances"**。之后绝不悄悄修改它。
   - **之后每次同步都对账。** 差额超过该币种容差时，就是一次**对账不符**：记为连接的一条警告，每出现一个新的差额值就往飞书发一次通知，绝不自动修正。
   - **快照。** 余额也存成一条类型为 `reported` 的快照，供 M9 的对账视图使用。
10. **系统账户**是通用的：`get_or_create_system_account(ctx, key)`，key 为 `"unassigned"` → "Unassigned"，`"opening_balances"` → "Opening balances"。两者都是 `equity`/`other`/`transactions`，记在设置项 `finance.system_account.<key>` 里。它们从不影响净值。暂缓的 M2 计划恢复时，直接复用这两个账户，不再用它自己的 "Unassigned (imports)"。
11. **webhook 接收器。** 它是一个独立的 Starlette 应用 `lararium.webhook_in`，和其他服务用同一个镜像运行，但有自己的数据库角色 `lararium_webhook_in`。这个角色只有 schema `core` 的 `USAGE` 和 `core.webhook_inbox` 的 `INSERT` 权限（没有 `SELECT`，所以也不能用 `RETURNING`）。
    - **唯一的路由。** `POST /monzo/{secret}` 用 `hmac.compare_digest` 比较 `secret` 和 `LARARIUM_WEBHOOK_SECRET`。密钥不对返回 404。请求体超过 64 KiB 返回 413，不是合法 JSON 返回 400。否则插入 `(source='monzo', body)`，返回不带内容的 200。
    - **部署。** 接收器和 `cloudflared` 放在 compose profile `webhook` 下，没有域名的部署直接不启用它们，只靠每 30 分钟的定时同步。
12. **webhook 注册**在设置了 `LARARIUM_WEBHOOK_BASE_URL` 时，每次同步都会做。对每个 Monzo 账户，同步时先列出已有的 webhook，删掉以我们的 base URL 开头但和当前地址不一致的那些，如果 `f"{base}/monzo/{secret}"` 还没注册就注册它。
13. **通知**走 `Notifier` 接口；唯一的实现是飞书。
    - **格式。** 自定义机器人的文本消息：`{"msg_type": "text", "content": {"text": ...}}`。配了密钥时，还要带上 `timestamp` 和 `sign = base64(HMAC-SHA256(key=f"{timestamp}\n{secret}", msg=b""))`。
    - **什么时候发。** 连接进入 `needs_reauth`；同步连续失败 3 次；连接从失败中恢复；出现对账不符；确认窗口过期。
    - **内容。** 消息写明是哪个连接、出了什么问题。可以包含余额差额，但绝不包含凭证。
    - **发送失败。** 发送失败只记日志，绝不让任务失败。
    - **配置。** `LARARIUM_FEISHU_WEBHOOK_URL` 和 `LARARIUM_FEISHU_SECRET` 放在 `infra/.env` 里。两个都没设置时，用一个什么都不做的通知器。
14. **连接的生命周期**（规格 §7.1）：`created` → `authorising` → `healthy` ⇄ `failing` → `needs_reauth` →（重新授权）`authorising`；另外还有 `disabled`。`failing` 是本计划在 `healthy` 和 `needs_reauth` 之间新加的状态，用于和凭证无关的错误。每个连接记录 `last_success_at`、`last_error`、`consecutive_failures`、`credential_expires_at`，以及一个 `warnings` 列表。

**本计划不做：** 转账匹配和推断（M4）；Monzo 自己的 pot 之外的 pot 转移配对；healthchecks 心跳和 Apprise（只做飞书）；从原始记录重新生成账本（normalizer 是确定性的、带版本号的，所以以后可以做）；其他 connector。

## Global Constraints

- 计划 1 的 Global Constraints 全部继续适用：
  - Python 3.13；uv，并强制使用 lockfile；ruff；pyright 以 strict 模式检查 `src/`；import-linter；
  - PostgreSQL 18；
  - `NUMERIC`/`Decimal`/字符串，`src/` 里任何地方都不出现 `float` 这个单词；
  - UUIDv7；UTC 的 `timestamptz`，日期用 `date` 单独存；
  - 界面文案用英文；文档双语；只用和 AGPL-3.0 兼容的依赖。
- 分支 `feat/claude-connectors-monzo`；提交信息 `<type>: <summary>`；提交时用 `local-signed-git-commit` skill 签名。
- **分层。** `core` 仍然不带财务色彩。连接、凭证、原始记录、收件箱和通知器放在 `core`；所有 Monzo 专属的东西放在 `lararium.modules.finance.monzo`。worker 和 webhook 接收器是顶层包（`lararium.worker`、`lararium.webhook_in`）。加几条 import-linter 合约：`lararium.webhook_in` 在本项目内只能 import `lararium.config` 和 `lararium.db`，`core` 仍然不 import 任何模块。
- **只用合成数据。** 仓库里不放任何真实的 Monzo 数据：测试 fixture 按文档里的数据结构手写。任务 10 期间从使用者账户抓下来的响应只放在 `private/monzo-samples/`。
- **所有写操作都要审计。** 每次写账本都经过 `command_context`。worker 的任务用 `Actor("connector", "monzo")`；API 用使用者的 actor。
- **网络。** 测试里不发任何对外 HTTP 请求：调用 Monzo 和飞书都通过可以注入 transport 的 `httpx.AsyncClient`（测试里用 `httpx.MockTransport`）。
- **密钥。** `LARARIUM_MASTER_KEY`、`LARARIUM_WEBHOOK_SECRET` 和飞书的两个值都是密钥。保存它们的设置字段用 pydantic 的 `SecretStr`。

## Review Focus

这些输入规格里隐含了要求，但主线测试碰不到，按出问题的可能性从高到低排列。每一条都在负责它的任务里有测试：

- **结算金额和授权金额不同的刷卡消费**（酒店、加油）：更新的是同一笔账本交易，不会出现第二笔（任务 6：`test_pending_settles_with_new_amount`）。
- **刚刷新完 token 就崩溃：** 新的 refresh token 已经提交，所以下次运行照样能用（任务 4：`test_refreshed_tokens_committed_before_use`）。
- **使用者在 App 里确认得太晚：** 连接最终处于 `needs_reauth`，消息清楚明白，也不会留下同步了一半却没有期初余额的数据（任务 7：`test_approval_window_expired`）。
- **pot 转移：** 主账户减少，pot 账户增加；净值不变，pot 的余额来自它的快照（任务 6：`test_pot_move_pairs_directly`）。
- **伪造、超大或重复的 webhook：**
  - 密钥不对返回 404，什么都不写；
  - 超过 64 KiB 返回 413；
  - 同一笔交易的两次投递最多只排队一次同步。

  （任务 8：`test_wrong_secret_404`、`test_body_too_large_413`、`test_duplicate_deliveries_one_sync`。）

---

## 文件结构

```
backend/
  migrations/versions/
    0008_core_connectors.py          # 任务 1：connections、credentials、raw_records、webhook_inbox
    0009_procrastinate.py            # 任务 2
    0010_finance_connector_refs.py   # 任务 3：transactions.connection_id/external_id，accounts 外键
  src/lararium/
    config.py                        # + master_key、public_base_url、webhook_*、feishu_*（SecretStr）
    core/
      secrets.py                     # 任务 1：AES-GCM、Secret
      connections.py                 # 任务 1：连接的增删改查和生命周期，凭证存储
      raw_records.py                 # 任务 1
      connectors.py                  # 任务 1：Connector 协议、SyncContext、注册表
      notify.py                      # 任务 2：Notifier、FeishuNotifier、NullNotifier
    worker/
      __init__.py, app.py, jobs.py   # 任务 2（Monzo 相关的任务在任务 7、8 接入）
      __main__.py                    # `python -m lararium.worker`
    webhook_in/
      __init__.py, app.py            # 任务 8
    modules/finance/
      ledger.py                      # + upsert_connector_transaction（任务 3）
      system_accounts.py             # 任务 3
      monzo/
        __init__.py
        client.py                    # 任务 4：API 客户端 + OAuth token 调用
        auth.py                      # 任务 4/7：token 存储、刷新、授权地址、回调
        sync.py                      # 任务 5：connector（API → 原始记录）
        normalize.py                 # 任务 6：原始记录 → 账本
        webhooks.py                  # 任务 8：注册，收件箱 → 同步
      connections_api.py             # 任务 7：/api/v1/connections 路由
  tests/
    core/test_secrets.py, test_connections.py, test_notify.py
    worker/test_runner.py
    finance/monzo/fixtures.py, test_client.py, test_sync.py, test_normalize.py, test_webhooks.py
    finance/test_connector_ledger.py
    api/test_connections_api.py
    webhook_in/test_receiver.py
web/src/pages/ConnectionsPage.tsx (+ .test.tsx), router.tsx
infra/compose.yaml, env.example, postgres/roles.sql, postgres/init/00-roles.sh, README*.md     # 任务 10
```

---

### Task 1: core 里的 connector 框架

**Files:**
- Create: `0008_core_connectors.py`, `core/secrets.py`, `core/connections.py`, `core/raw_records.py`, `core/connectors.py`, `core/notify.py`（只放 `Notifier` 协议和 `NullNotifier`；飞书在任务 2 加）, `tests/core/test_secrets.py`, `tests/core/test_connections.py`
- Modify: `config.py`, `core/models.py`, `core/errors.py`, `pyproject.toml`/`uv.lock`（加 `cryptography`；把 `httpx` 从开发依赖移到运行时依赖）

**Interfaces:**
- Produces:
  - **`core/secrets.py`：**
    - `class Secret`，有 `reveal() -> str`；`repr`/`str` 返回 `"Secret(***)"`。
    - `def encrypt(master_key: bytes, plaintext: str, aad: str) -> tuple[bytes, bytes]` 返回 `(nonce, ciphertext)`，nonce 是 12 字节随机数。
    - `def decrypt(master_key: bytes, nonce: bytes, ciphertext: bytes, aad: str) -> Secret`，任何失败都抛 `CredentialDecryptError`。
    - `def load_master_key(value: SecretStr) -> bytes`，要求是恰好 32 字节内容的 base64。
  - **`Settings` 新增：** `master_key: SecretStr | None = None`，在 `auth_mode == "tailscale"` 时必填（校验器写法和 `owner_login` 一样）；`public_base_url: str | None = None`。
  - **`core/connections.py`：**
    - `ConnectionStatus = Literal["created", "authorising", "healthy", "failing", "needs_reauth", "disabled"]`。
    - `@dataclass(frozen=True) class ConnectionRecord`：`id, connector: str, display_name: str, status, config: dict[str, JSONValue], cursor: JSONValue, last_success_at, last_error: str | None, consecutive_failures: int, credential_expires_at, warnings: list[str], created_at`。
    - `create_connection(ctx, *, connector, display_name, config) -> ConnectionRecord`
    - `get_connection(session, id)`、`list_connections(session)`
    - `set_status(ctx, id, status, *, error: str | None = None) -> ConnectionRecord`：状态有变化时发出 `core.connection.status_changed`，数据是 `{from, to}`。
    - `record_sync_success(ctx, id, cursor)`；`record_sync_failure(ctx, id, error) -> int`，返回 `consecutive_failures`。
    - `set_warnings(ctx, id, warnings)`、`update_config(ctx, id, config)`。
    - `store_credential(ctx, master_key, connection_id, purpose: str, value: str, expires_at: datetime | None)` 和 `load_credential(session, master_key, connection_id, purpose) -> Secret | None`。后面会用到的 purpose：`"client_secret"`、`"access_token"`、`"refresh_token"`。
  - **`core/raw_records.py`：** `store_raw_record(ctx, *, connection_id, record_type: str, request: dict[str, JSONValue], content: JSONValue, connector_version: str, sync_id: UUID) -> UUID`。它对规范化后的 JSON（`json.dumps(..., sort_keys=True, separators=(",", ":"))`）求哈希，如果 `(connection_id, record_type, content_hash)` 已经存在，就跳过插入，返回已有的 id。另有 `list_raw_records(session, sync_id) -> list[RawRecord]`。
  - **`core/notify.py`：** `class Notifier(Protocol)`，带 `async def send(self, text: str) -> None`；以及 `class NullNotifier`。
  - **`core/connectors.py`：**
    - `Capability = Literal["balances", "holdings", "transactions", "webhook", "device_push", "outbound_actions"]`。
    - `class Connector(Protocol)`：`name: str`、`version: str`、`capabilities: frozenset[Capability]`、`async def sync(self, ctx: SyncContext) -> JSONValue`，返回新的游标。
    - `@dataclass class SyncContext`：`command: CommandContext, connection: ConnectionRecord, sync_id: UUID, master_key: bytes, http: httpx.AsyncClient, notifier: Notifier`。
    - 注册表：`ModuleSpec` 新增 `connectors: Mapping[str, Connector] = {}`（保留默认值，现有模块不用改），以及 `registry.connector(name) -> Connector`。

迁移 `0008`：
- `core.connections`。列：`id`；`connector text NOT NULL`；`display_name text NOT NULL`；`status text NOT NULL CHECK IN (...)`；`config jsonb NOT NULL DEFAULT '{}'`；`cursor jsonb`；`last_success_at`；`last_error text`；`consecutive_failures int NOT NULL DEFAULT 0 CHECK (>= 0)`；`credential_expires_at`；`warnings jsonb NOT NULL DEFAULT '[]'`；`owner_entity_id`；`created_at`；`updated_at`。
- `core.credentials`。列：`connection_id` 外键 `ON DELETE CASCADE`；`purpose text`；`nonce bytea NOT NULL`；`ciphertext bytea NOT NULL`；`key_version int NOT NULL DEFAULT 1`；`expires_at`；`updated_at`；`PRIMARY KEY (connection_id, purpose)`。
- `core.raw_records`。列：`id`；`connection_id` 外键；`sync_id uuid NOT NULL`；`record_type text`；`request jsonb`；`fetched_at timestamptz DEFAULT now()`；`content jsonb NOT NULL`；`content_hash text NOT NULL`；`connector_version text NOT NULL`；`UNIQUE (connection_id, record_type, content_hash)`。然后执行 `REVOKE UPDATE, DELETE ON core.raw_records FROM lararium_backend`（原始数据不可修改）。
- `core.webhook_inbox`。列：`id uuid DEFAULT uuidv7()`；`source text NOT NULL`；`body jsonb NOT NULL`；`received_at DEFAULT now()`；`processed_at timestamptz`；`outcome text`。
- `ALTER TABLE finance.accounts ADD FOREIGN KEY (source_connection_id) REFERENCES core.connections`。这一条语句在 core 的迁移里改了 finance 的表；这样做可以接受，因为计划 1 的迁移里留了注释承诺会补上它，而且迁移不受 import-linter 约束。

- [ ] **Step 1: 写失败的测试：**
  - `test_encrypt_round_trip_and_aad_binding`：用不同的 `aad` 解密会抛 `CredentialDecryptError`。
  - `test_secret_never_reprs_plaintext`：`Secret` 的 `repr`、`str` 和 f-string 都不含原值。
  - `test_master_key_must_be_32_bytes`。
  - `test_credential_store_round_trip`：`ciphertext` 的字节里找不到明文；对已有的 purpose 再调用 `store_credential` 会替换原值。
  - `test_status_change_emits_event_once`：连续两次设置同一个状态，只发出一个事件。
  - `test_failure_counter_resets_on_success`。
  - `test_raw_record_dedup_by_content`：同样的内容存两次，返回同一个 id；backend 角色不能对 `core.raw_records` 执行 `UPDATE` 或 `DELETE`。
  - `test_settings_require_master_key_in_tailscale_mode`。
- [ ] **Step 2: 运行** `uv run --locked pytest tests/core -q`；预期：新测试失败。
- [ ] **Step 3: 实现。**
- [ ] **Step 4: 运行** `make backend-check`；预期：全部通过。
- [ ] **Step 5: 提交** `feat: add connections, encrypted credentials and raw records to core`。

### Task 2: Worker 与通知

**Files:**
- Create: `0009_procrastinate.py`, `worker/app.py`, `worker/jobs.py`, `worker/__main__.py`, `tests/worker/test_runner.py`, `tests/core/test_notify.py`
- Modify: `config.py`（+ `feishu_webhook_url: SecretStr | None`、`feishu_secret: SecretStr | None`）, `core/notify.py`（加飞书）, `pyproject.toml`/`uv.lock`（加 `procrastinate`）

**Interfaces:**
- Consumes: 任务 1。
- Produces:
  - **`core/notify.py`：**
    - `Notifier` 和 `NullNotifier` 在任务 1 已经有了；本任务补上其余部分。
    - `class FeishuNotifier(url: SecretStr, secret: SecretStr | None, http: httpx.AsyncClient)`，按决策 13 签名。状态码不是 200，或返回体的 `code != 0` 时，以 warning 级别记日志（不带 URL），然后正常返回。
    - `def notifier_from_settings(settings, http) -> Notifier`。
  - **`worker/app.py`：** `procrastinate_app`（基于 `LARARIUM_DATABASE_URL` 的 `PsycopgConnector`，URL 转成 psycopg 的 DSN）。
  - **`worker/jobs.py`：**
    - `async def run_sync(session, connection_id, *, settings, http, notifier) -> None`，是 `sync_connection` 可测试的核心。它会：
      1. 以 `Actor("connector", <connector name>)` 打开一个 `command_context`；
      2. 调用 `connector.sync`；
      3. 记录成功和游标；
      4. 出现异常时：回滚，在一个新的 `command_context` 里记录失败，把状态设为 `failing`，失败次数达到 3 时发通知。如果是 `CredentialRejectedError`，则改为设成 `needs_reauth` 并立即通知；
      5. 从 `failing` 恢复成功时，设为 `healthy`，并发一条“已恢复”（"recovered"）消息。
    - 任务 `sync_connection(connection_id: str)`，带决策 2 里的锁。
    - 定时任务 `schedule_syncs`（`*/30 * * * *`）。
    - 定时任务 `process_webhook_inbox`（`* * * * *`），在任务 8 接上。
  - **`core/errors.py`：** `CredentialRejectedError`，数据源拒绝凭证时由 connector 抛出。
  - **`python -m lararium.worker`** 运行 Procrastinate worker，带上定时任务。

迁移 `0009`：在使用者创建的 schema `procrastinate` 里应用 `procrastinate.schema.SchemaManager.get_schema()`。给 `lararium_backend` 授予该 schema 的 usage，其中各表的 `SELECT`、`INSERT`、`UPDATE` 和 `DELETE`，以及其中函数的 `EXECUTE`。如果自带的 SQL 默认用的是 `public`，就在 `SET search_path TO procrastinate` 下执行，并在迁移的 docstring 里写明。

- [ ] **Step 1: 写失败的测试：**
  - `test_feishu_payload_and_signature`：固定 `timestamp=1700000000`、密钥 `s3cret`，发出的 JSON 里有 `msg_type`、`content.text`、`timestamp`，以及测试用同一公式算出的 `sign`。
  - `test_feishu_failure_does_not_raise`：返回 500，或返回体是 `{"code": 19021}`，都正常返回。
  - `test_run_sync_success_records_cursor`，为测试注册一个假的 connector。
  - `test_third_failure_notifies_once`：连续三次失败只发一条消息；第四次不再发；之后成功一次，发 "recovered"。
  - `test_credential_rejected_sets_needs_reauth`。
  - `test_procrastinate_schema_present`：迁移完成后，`procrastinate.procrastinate_jobs` 存在，backend 角色能插入任务。
- [ ] **Step 2: 运行**；预期：FAIL。
- [ ] **Step 3: 实现。**
- [ ] **Step 4: 运行** `make backend-check`；预期：全部通过。
- [ ] **Step 5: 提交** `feat: add the sync worker and Feishu notifications`。

### Task 3: 账本支持 connector 数据

**Files:**
- Create: `0010_finance_connector_refs.py`, `modules/finance/system_accounts.py`, `tests/finance/test_connector_ledger.py`
- Modify: `modules/finance/ledger.py`, `modules/finance/accounts.py`（允许 `source_kind='connection'` 并带 `source_connection_id`）

**Interfaces:**
- Consumes: 计划 1 的账本和账户；任务 1 的连接。
- Produces:
  - **迁移。** `finance.transactions` 新增 `connection_id uuid REFERENCES core.connections`、`external_id text`、`CHECK ((connection_id IS NULL) = (external_id IS NULL))`，以及 `(connection_id, external_id)` 上的唯一索引。
  - **`ledger.TransactionInput`** 新增 `connection_id: UUID | None = None` 和 `external_id: str | None = None`，两者都由 `post_transaction` 写入。
  - **`ledger.upsert_connector_transaction(ctx, tx: TransactionInput) -> tuple[TransactionRecord, Literal["created", "updated", "unchanged"]]`**，要求 `tx.origin == "connector"` 且两个引用都有值。
    - 还没有交易带这对引用时，直接记账。
    - 已有一笔，且字段和分录都相同时，返回 `"unchanged"`，什么都不写。
    - 否则按 `post_transaction` 的方式校验新的分录，然后更新这一行并替换它的分录：先删除，再插入。发出 `finance.transaction.updated`，并用修改前后的内容调用 `ctx.record_change`。
    - 已有那一行的 origin 不是 `connector` 时，拒绝（`FinanceValidationError`）。
  - **`accounts.create_account`** 新增 `source_connection_id: UUID | None = None`；传了这个参数时，设置 `source_kind='connection'`。
  - **`system_accounts.get_or_create_system_account(ctx, key: Literal["unassigned", "opening_balances"]) -> UUID`**，按决策 10。

- [ ] **Step 1: 写失败的测试：**
  - `test_upsert_creates_then_unchanged`。
  - `test_upsert_replaces_postings_in_place`：id 不变，旧的分录行没了，余额反映新的金额。
  - `test_upsert_refuses_manual_rows`。
  - `test_refs_must_be_paired`：数据库层面的 CHECK 生效。
  - `test_system_accounts_reused_and_excluded_from_net_worth`。
- [ ] **Step 2: 运行**；预期：FAIL。
- [ ] **Step 3: 实现。**
- [ ] **Step 4: 运行** `make backend-check`；预期：全部通过。
- [ ] **Step 5: 提交** `feat: let connectors upsert ledger transactions by external id`。

### Task 4: Monzo API 客户端与 token

**Files:**
- Create: `monzo/__init__.py`, `monzo/client.py`, `monzo/auth.py`, `tests/finance/monzo/fixtures.py`, `tests/finance/monzo/test_client.py`

**Interfaces:**
- Consumes: 任务 1（`store_credential`、`load_credential`、`CredentialRejectedError`）。
- Produces:
  - **`monzo/client.py`：**
    - `API = "https://api.monzo.com"`、`AUTH = "https://auth.monzo.com/"`。
    - `class MonzoClient(http: httpx.AsyncClient, access_token: Secret)`，方法有：
      - `whoami() -> dict`
      - `accounts() -> list[dict]`
      - `balance(account_id) -> dict`
      - `pots(account_id) -> list[dict]`
      - `transactions(account_id, *, since: str | None, before: str | None = None) -> AsyncIterator[list[dict]]`：带 `expand[]=merchant`，每页最多 100 条逐页返回；每页之后把 `since` 推进到该页最后一条的 id，遇到不足 100 条的页就停。
      - `webhooks(account_id) -> list[dict]`、`register_webhook(account_id, url) -> dict`、`delete_webhook(webhook_id)`
    - `MonzoApiError(status, code)`。401 且 `error` 是 `("invalid_token", "unauthorized")` 之一时抛 `AccessTokenExpiredError`。确认之前的 403 抛 `NotYetApprovedError`。429 抛 `MonzoRateLimitedError`。错误消息里绝不带 token。
  - **`monzo/auth.py`：**
    - `def authorize_url(client_id: str, redirect_uri: str, state: str) -> str`。
    - `async def exchange_code(http, client_id, client_secret: Secret, redirect_uri, code) -> TokenPair`。
    - `async def refresh(http, client_id, client_secret: Secret, refresh_token: Secret) -> TokenPair`。
    - `@dataclass TokenPair(access_token: Secret, refresh_token: Secret, expires_at: datetime)`。
    - `async def save_tokens(session_factory, master_key, connection_id, pair)`，在**它自己的 session 和事务**里提交（决策 4）。
    - `async def access_token_for(sync_ctx) -> Secret`，剩余有效期不足 5 分钟时刷新，并在返回前调用 `save_tokens`。`invalid_grant` 抛 `CredentialRejectedError`。

- [ ] **Step 1: 写失败的测试**，用 `httpx.MockTransport` 的 handler 和 `fixtures.py` 里的构造函数。构造函数有 `make_account`、`make_transaction(**overrides)`、`make_pot` 和 `make_balance`，id 是 `acc_TEST1`、`tx_TEST…` 这样的合成值。
  - `test_transactions_paginates_with_since_last_id`：250 笔交易的历史分成 100、100、50 三页，第二次请求带 `since=<100th id>`。
  - `test_authorize_url_params`：`client_id`、`redirect_uri`、`response_type=code` 和 `state` 都在，并且做了 URL 编码。
  - `test_error_mapping`。
  - `test_errors_never_contain_tokens`。
  - `test_refreshed_tokens_committed_before_use`：刷新成功，然后下一次 API 调用抛异常。新开一个 session 仍能读到新的 refresh token。
  - `test_invalid_grant_is_credential_rejected`。
- [ ] **Step 2: 运行**；预期：FAIL。
- [ ] **Step 3: 实现。**
- [ ] **Step 4: 运行** `make backend-check`；预期：全部通过。
- [ ] **Step 5: 提交** `feat: add the Monzo API client and token handling`。

### Task 5: Monzo 同步（API → 原始记录）

**Files:**
- Create: `monzo/sync.py`, `tests/finance/monzo/test_sync.py`
- Modify: `modules/finance/__init__.py`（在 `MODULE.connectors` 里注册这个 connector）

**Interfaces:**
- Consumes: 任务 1、2 和 4。
- Produces:
  - `class MonzoConnector`，`name="monzo"`、`version="monzo-sync-1"`、`capabilities={"balances", "transactions", "webhook"}`。
  - `async def sync(self, ctx: SyncContext) -> JSONValue` 存下类型为 `accounts`、`balance`、`pots` 和 `transactions_page` 的原始记录，每条都带上请求参数，然后调用 `normalize(ctx)`（任务 6）。
  - 游标的结构是 `{"accounts": {<monzo_account_id>: {"last_created": str, "backfilled": bool}}}`。
  - `full_backfill: bool` 取自 `ctx.connection.config.get("backfill_requested")`。任务 7 设置这个标记，同步完成后把它清掉。

规则：决策 8（窗口、重叠、截断、截断警告）。已关闭的 Monzo 账户（返回里 `closed: true`）照样同步，但不给它们注册 webhook。

- [ ] **Step 1: 写失败的测试：**
  - `test_first_sync_stores_raw_records_per_endpoint`。
  - `test_incremental_sync_uses_14_day_overlap`：请求里的 `since` 等于 `last_created − 14 days`。
  - `test_since_clamped_to_89_days_with_warning`。
  - `test_backfill_truncation_warning`。
  - `test_unchanged_responses_not_stored_twice`。
- [ ] **Step 2: 运行**；预期：FAIL。
- [ ] **Step 3: 实现。**
- [ ] **Step 4: 运行** `make backend-check`；预期：全部通过。
- [ ] **Step 5: 提交** `feat: sync Monzo accounts, pots, balances and transactions`。

### Task 6: Monzo normalizer（原始记录 → 账本）

**Files:**
- Create: `monzo/normalize.py`, `tests/finance/monzo/test_normalize.py`

**Interfaces:**
- Consumes: 任务 3 和 5；`snapshots.record_snapshot`（kind 为 `reported`）；`get_or_create_organization`；`get_currency`。
- Produces:
  - `NORMALIZER_VERSION = "monzo-norm-1"`。
  - `async def normalize(ctx: SyncContext, fetched: Sequence[FetchedRecord], *, backfilled: Collection[str] = (), first_backfills: Collection[str] = ()) -> NormalizeReport`，其中 `NormalizeReport(created: int, updated: int, unchanged: int, voided: int, skipped_declined: int, mismatches: list[str])`。
  - `def to_quantity(minor_units: int, decimals: int) -> Decimal`，即 `Decimal(minor_units).scaleb(-decimals)`。

规则：决策 5、6 和 9。账户和 pot 通过设置 `config["account_map"]` 查找或创建：这是 Monzo id → 财务账户 id 的映射，存在连接的 config 里。

- [ ] **Step 1: 写失败的测试**（每个测试先用 fixture 构造函数准备好原始记录，再执行 normalize）：
  - `test_creates_accounts_and_pot_children`：pot 账户的 `parent_account_id` 是主账户，模式是 `hybrid`，并有一条等于 pot 余额的快照。
  - `test_card_payment_posts_against_unassigned`：`-1234` 个最小单位的 GBP 得到 `-12.34`；对方是商户实体；已结算时状态是 `booked`。
  - `test_pending_settles_with_new_amount`：先是 pending 的 `-5000`，然后以 `-4250` 结算：只有一笔交易，结果 `updated`，数量 `-42.50`，状态 `booked`。
  - `test_reversed_pending_is_voided`。
  - `test_declined_not_posted`。
  - `test_pot_move_pairs_directly`：有 `metadata.pot_id` 时，主账户 `-50.00`，pot `+50.00`，没有 Unassigned 分录。
  - `test_opening_adjustment_once`：回填后，历史交易合计 100.00，但余额是 250.00，于是记一笔 150.00 的调整。再 normalize 一次，差额 10.00，这次什么都不记，加一条警告，只通知一次。
  - `test_normalize_is_idempotent`：运行两次，计数只有 `unchanged`。
  - `test_booking_date_uses_local_date`：`created` 为 `2026-03-29T23:30:00Z` 时，在 `Europe/London`（BST）下记账日期是 `2026-03-30`。
- [ ] **Step 2: 运行**；预期：FAIL。
- [ ] **Step 3: 实现。**
- [ ] **Step 4: 运行** `make backend-check`；预期：全部通过。
- [ ] **Step 5: 提交** `feat: normalize Monzo data into the ledger`。

### Task 7: 连接 API 与 OAuth 流程

**Files:**
- Create: `modules/finance/connections_api.py`, `tests/api/test_connections_api.py`
- Modify: `deps.py`（导航豁免的辅助函数）, `modules/finance/__init__.py`（挂上 router）, `worker/jobs.py`（`await_approval`）, `web/openapi.json`, `web/src/api/*`

**Interfaces:**
- Consumes: 任务 1、2、4 和 5。
- Produces:
  - **路由**，都在 `/api/v1/connections` 下。route name 就是 operationId；除了回调（见下文），所有路由都用 `ActorDep`。
    - `GET ""` `list_connections` → `[ConnectionOut]`。`ConnectionOut` 包含 `id`、`connector`、`display_name`、`status`、`last_success_at`、`last_error`、`consecutive_failures`、`credential_expires_at`、`warnings`、`redirect_uri` 和 `webhook_enabled: bool`，绝不包含凭证。
    - `POST ""` `create_monzo_connection`，请求体 `{display_name, client_id, client_secret}` → 201。密钥加密保存，审计参数里不记 `client_secret`。
    - `POST "/{id}/authorize"` `authorize_connection` → `{url}`。见决策 7。
    - `GET "/oauth/monzo/callback"` `monzo_oauth_callback`，带 `?code&state` → 303 到 `/connections`。state 无效或过期时，303 到 `/connections?error=state`。
    - `POST "/{id}/sync"` `sync_connection_now` → 202。它会安排一次 `sync_connection`。
    - `POST "/{id}/disable"` `disable_connection`。
  - **`deps.get_actor_allowing_navigation`** 的行为和 `get_actor` 一样，只是在 `method == "GET"`、`Sec-Fetch-Mode == "navigate"` 且 `Sec-Fetch-Dest == "document"` 时接受跨站请求。只有回调用它。
  - **`worker.jobs.await_approval(connection_id)`**，按决策 7。在回填之前，它先设置 `config["backfill_requested"] = True`。

- [ ] **Step 1: 写失败的测试：**
  - `test_create_connection_hides_secret`：响应和审计参数里都不含密钥。
  - `test_authorize_returns_monzo_url_and_stores_state_hash`。
  - `test_callback_navigation_allowed_but_fetch_refused`：带 `Sec-Fetch-Site: cross-site` 时，mode 为 `navigate`、dest 为 `document` 的请求得到 303，mode 为 `cors` 的请求得到 403。
  - `test_callback_state_single_use_and_expiring`。
  - `test_callback_exchanges_code_and_defers_approval`，HTTP 用 mock，Procrastinate 用内存 connector。
  - `test_await_approval_backfills_on_first_success`。
  - `test_approval_window_expired`：4 分钟内（用假时钟）一直不成功，结果是 `needs_reauth`、那条消息、一条通知，并且账本里没有任何行。
  - `test_connection_operation_ids`。
- [ ] **Step 2: 运行**；预期：FAIL。
- [ ] **Step 3: 实现**，然后运行 `make openapi`。
- [ ] **Step 4: 运行** `make check`；预期：全部通过。
- [ ] **Step 5: 提交** `feat: add connection routes and the Monzo OAuth flow`。

### Task 8: webhook 接收器与收件箱处理

**Files:**
- Create: `webhook_in/app.py`, `monzo/webhooks.py`, `tests/webhook_in/test_receiver.py`, `tests/finance/monzo/test_webhooks.py`
- Modify: `config.py`（+ `webhook_secret: SecretStr | None`、`webhook_base_url: str | None`）, `worker/jobs.py`, `monzo/sync.py`（调用注册）, `pyproject.toml`（`webhook_in` 的 import-linter 合约）, `infra/postgres/roles.sql` 和 `infra/postgres/init/00-roles.sh`（角色 `lararium_webhook_in`），以及新迁移 `0011_webhook_in_grants.py`，给这个角色授予 `USAGE ON SCHEMA core` 和 `INSERT ON core.webhook_inbox`。角色不存在时，迁移输出一条 notice 并跳过，这样测试容器（在 `roles.sql` 里创建了这个角色）和较老的数据库都能用。

**Interfaces:**
- Produces:
  - **`webhook_in/app.py`：** `def create_app(database_url: str, secret: SecretStr) -> Starlette`，以及一个从环境变量构建的模块级 `app`。行为见决策 11。它用一个普通的 `psycopg` 异步连接池（不用 SQLAlchemy 模型）执行一条参数化的 `INSERT`。
  - **`monzo/webhooks.py`：**
    - `async def ensure_webhooks(client, account_ids, base_url, secret)`，按决策 12。
    - `async def process_inbox(session, *, defer) -> int`。它用 `FOR UPDATE SKIP LOCKED` 领取未处理的行，通过 `config["account_map"]` 把 `body["data"]["account_id"]` 对应到某个连接，调用 `defer(connection_id)`（`queueing_lock` 会丢掉重复的），然后设置 `processed_at` 和 `outcome`（`queued`、`duplicate`、`unknown_account` 或 `bad_body`）。请求体绝不写进账本。

- [ ] **Step 1: 写失败的测试：**
  - `test_valid_delivery_inserted_200`。
  - `test_wrong_secret_404`：什么都没插入。
  - `test_body_too_large_413`。
  - `test_invalid_json_400`。
  - `test_get_is_405`。
  - `test_receiver_role_cannot_select`：以 `lararium_webhook_in` 连接时，对 `core.webhook_inbox` 和 `finance.accounts` 执行 `SELECT` 都失败。
  - `test_duplicate_deliveries_one_sync`。
  - `test_unknown_account_marked`。
  - `test_ensure_webhooks_replaces_stale_and_registers_missing`。
- [ ] **Step 2: 运行**；预期：FAIL。
- [ ] **Step 3: 实现。**
- [ ] **Step 4: 运行** `make backend-check`；预期：全部通过。
- [ ] **Step 5: 提交** `feat: receive Monzo webhooks through a write-only inbox`。

### Task 9: 网页界面——Connections 页

**Files:**
- Create: `web/src/pages/ConnectionsPage.tsx`, `ConnectionsPage.test.tsx`
- Modify: `web/src/router.tsx`（路由 `/connections`，导航项 "Connections" 放在 "Prices" 之后）

行为：
- **连接列表：** 名称；状态徽标；最近一次成功同步，显示为相对时间，悬停提示里给出准确时间；最近的错误；警告。
- **每个连接的按钮：**
  - "Sync now"；
  - "Authorise"（状态为 `created` 或 `needs_reauth` 时）。它调用 `authorizeConnection`，然后把 `window.location.href` 设为返回的地址；
  - "Disable"。
- **处于 `authorising` 时：** 醒目地显示提示 "Open the Monzo app and approve the request now. Lararium has 4 minutes to fetch your full history."。页面每 5 秒轮询一次 `listConnections`，直到状态变化。
- **"Add Monzo" 表单：** 显示名称、client ID、client secret（密码输入框）。帮助文字里显示服务端给出的准确 `redirect_uri`，供使用者粘贴到 developers.monzo.com。
- **地址里带 `?error=state`** 时，显示 "The authorisation link expired or was already used. Click Authorise again."。

- [ ] **Step 1: 写失败的测试：**
  - `test_add_form_sends_secret_and_clears_it`。
  - `test_authorise_redirects_to_returned_url`，`window.location` 用 stub 替换。
  - `test_authorising_notice_and_polling`，用假计时器。
  - `test_state_error_banner`。
- [ ] **Step 2: 运行** `cd web && pnpm test --run`；预期：FAIL。
- [ ] **Step 3: 实现。**
- [ ] **Step 4: 运行** `make check`；预期：全部通过。
- [ ] **Step 5: 提交** `feat: add the Connections page`。

### Task 10: 部署、文档与真实账户检查

**Files:**
- Modify: `infra/compose.yaml`, `infra/env.example`, `backend/tests/infra/test_compose.py`, `README.md`, `README.zh-CN.md`, `Makefile`（`make worker-logs` 可选，用不上就不加）

改动：
- **Compose：**
  - `worker`：backend 镜像；`command: ["python", "-m", "lararium.worker"]`；数据库 URL 和设置与 `api` 相同，另加飞书的变量；`restart: unless-stopped`；依赖 `migrate` 执行完成。
  - `webhook-in`，放在 `profiles: ["webhook"]` 下：backend 镜像；`command: ["uvicorn", "lararium.webhook_in.app:app", "--host", "0.0.0.0", "--port", "8000"]`；`LARARIUM_DATABASE_URL` 用角色 `lararium_webhook_in`；`LARARIUM_WEBHOOK_SECRET`；不发布任何端口。
  - `cloudflared`，放在 `profiles: ["webhook"]` 下：`cloudflare/cloudflared:<pinned version>`；`command: ["tunnel", "--no-autoupdate", "run"]`；`TUNNEL_TOKEN: ${CLOUDFLARE_TUNNEL_TOKEN:?…}`；不发布端口。
  - `api` 新增 `LARARIUM_MASTER_KEY`、`LARARIUM_PUBLIC_BASE_URL` 和 `LARARIUM_WEBHOOK_BASE_URL`；`worker` 也加这几个，另加飞书的变量。
- **`env.example`：** 列出所有新变量，并附上生成方法：
  - `LARARIUM_MASTER_KEY`：`openssl rand -base64 32`；
  - `LARARIUM_WEBHOOK_SECRET`：`openssl rand -hex 32`；
  - `LARARIUM_WEBHOOK_IN_PASSWORD`：`openssl rand -hex 32`；
  - 以及 `CLOUDFLARE_TUNNEL_TOKEN`、`LARARIUM_FEISHU_WEBHOOK_URL`、`LARARIUM_FEISHU_SECRET`、`LARARIUM_PUBLIC_BASE_URL` 和 `LARARIUM_WEBHOOK_BASE_URL`。

  还要提醒：丢了 `LARARIUM_MASTER_KEY`，就得重新录入所有凭证并重新授权。
- **README（两种语言）：** 一节 "Connecting Monzo"：
  - 怎么创建 OAuth client，以及重定向 URI；
  - 4 分钟内确认的规则；
  - 哪些东西会同步，"Unassigned" 和 "Opening balances" 是什么。

  一节 "Webhooks through Cloudflare Tunnel"：
  - 创建 tunnel，以及公网主机名 → `http://webhook-in:8000`；
  - WAF 规则表达式 `not (http.request.method eq "POST" and starts_with(http.request.uri.path, "/monzo/"))` → Block；
  - `docker compose --profile webhook up -d`。

  一节 "Feishu notifications"：开启签名校验的自定义机器人。在安全一节里写明：接收器的角色只能写；Cloudflare 能看到 webhook payload 的明文，但它们只被当作“马上同步”的信号。对于**已有的数据库**，给出一次性创建 `lararium_webhook_in` 的 SQL，因为 `roles.sql` 只在全新的数据卷上执行。

- [ ] **Step 1: 写失败的测试：**
  - `test_compose.py`：
    - `worker` 存在，并使用 worker 的命令；
    - `webhook-in` 和 `cloudflared` 只在 `webhook` profile 里，并且不发布端口；
    - `webhook-in` 以 `lararium_webhook_in` 连接数据库；
    - 除了 `web`，没有任何服务发布端口。
- [ ] **Step 2: 运行**；预期：FAIL。
- [ ] **Step 3: 实现**；写好两份 README。
- [ ] **Step 4: 验证。**
  - `make check` 通过。
  - Compose 冒烟测试：`make up`（不带 profile）会启动 `worker`，它的日志显示定时任务已注册。
  - `docker compose --profile webhook up -d webhook-in`。在 compose 网络内部用 `curl -X POST` 向 `/monzo/<secret>` 发送 `{}`，得到 200，并且 worker 在一分钟内把这一行标为 `bad_body`；密钥不对时得到 404。
- [ ] **Step 5: 真实账户检查（由使用者操作，实现者在旁指导）。** 开头列的前置条件都就绪之后：
  1. 创建连接。
  2. 授权，并在 4 分钟内到 App 里确认。
  3. 确认账户、pot 和期初调整都出现了，并且 Lararium 里 Monzo 账户的余额和 App 里一致。
  4. 刷卡做一笔小额消费，确认它在大约一分钟内经 webhook 进来。
  5. 每个接口保存一份匿名化后的响应到 `private/monzo-samples/`（已被 gitignore）。
  6. 把对方信息、`metadata.pot_id` 和 pending 撤销相关的字段和 fixture 对比。如有不同，**用合成数据**修正 fixture 和 normalizer，放在后续提交 `fix: align Monzo normalizer with real API responses` 里。
  7. 把发现记进决策 1。
- [ ] **Step 6: 提交** `feat: deploy the worker and webhook ingress`。

---

## 自查记录

- **规格覆盖。**
  - §7.1：connector 契约（同步、游标、能力，包括 `device_push` 和 `outbound_actions`）、生命周期、每个连接一把锁、定时计划和回填。webhook 只作为“马上同步”的触发。
  - §7.2：confidential client 的 OAuth、窗口内回填、带重叠的增量同步、日期范围检查、映射（账户、作为子账户的 pot、余额快照、作为实体的商户；对方信息保留在原始记录里，留给 M4）、webhook 注册、重新授权的提醒和入口，以及要核实的事项（任务 10 第 5 步）。
  - §7.9：用只能插入的角色、路径里带密钥的接收器，收到后立即返回。
  - §11.1：`webhook_in` 角色。
  - §11.2：应用层加密，主密钥来自环境变量，日志、事件、响应里都不出现凭证。（SOPS 仍然是部署时的选择，和计划 1 一样。）
  - §11.5：通知，用飞书代替 Apprise（使用者的选择）。
- **类型。**
  - `SyncContext`（任务 1）被任务 5、6 和 8 使用。
  - `upsert_connector_transaction`（任务 3）被任务 6 使用。
  - `CredentialRejectedError`（任务 1）在任务 4 抛出，在任务 2 处理。
  - `config["account_map"]` 在任务 6 写入，在任务 8 读取。
  - `backfill_requested` 在任务 7 设置，在任务 5 读取。
