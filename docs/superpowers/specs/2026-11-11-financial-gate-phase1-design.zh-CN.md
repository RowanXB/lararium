# Lararium 第一阶段设计：个人财务基础设施

**语言：** [English](./2026-11-11-financial-gate-phase1-design.md) | 简体中文

- 日期：2026-11-11
- 状态：已确认，开始按计划实现
- 许可证：AGPL-3.0-or-later（开源项目）
- 范围：第一阶段（财务核心，接入全部财务数据源）。同时规定为了以后扩展成“个人 OS”，第一阶段必须提前留好的接口。
- 权威版本：以英文版为准；本文是中文译文，与英文版不一致时以英文版为准。

## 0. 怎么读这份文档

第 15 节的决策记录表给每个决定标了状态：

- **需求**：项目必须满足的要求。
- **已确认**：讨论后定下来的设计。
- **默认决定**：设计者选的默认做法，有充分理由时可以改。

第 16 节列出留给实现者自己决定的部分，第 17 节列出还没确认、但不阻塞开工的问题。

## 1. 意图

Lararium 是一套**自托管的个人财务基础设施**。第一个目标是把一个人的所有资产统计、追踪在一个地方：券商、交易所、银行、自托管加密钱包，以及没有 API、只能手动导入流水或存款信息的银行。

它是开源软件：每个使用者在自己的机器上部署一套，用自己的凭证连接自己的账户。它不是托管服务（见第 3 节“开源与合规”）。

这套系统以后会扩展更多能力，最终可能成为一个“个人 OS”。计划中的第一批非财务模块是：待办和日程、健康数据、日记。所以现阶段最看重的是**前向兼容**：今天的设计不能挡住以后的模块。

为什么自建，而不用现成的开源项目：Firefly III 偏记账和预算，投资功能弱；Ghostfolio、Wealthfolio 偏投资组合，银行流水和转账推断不是它们的重点；Beancount + Fava 的数据模型最好，但纯文本内核不适合持续的 API 同步，也不适合以后接很多模块。所以核心自建，数据模型借鉴 Beancount，同时支持导出成 Beancount 格式。

## 2. 目标、非目标、反目标

### 2.1 第一阶段目标

1. **可信的净资产视图**：任意一天（包括过去的日期）每个账户、每种资产、每种币种的余额和估值，并按报告币种汇总。历史能追溯多远，以各数据源实际能提供的为准，不设固定起点。
2. **接入全部财务数据源**：
   - 自动接入：Monzo、IBKR（Flex Web Service）、Binance、SnapTrade Personal（其他券商）、eToro（7.6 节）、自托管钱包（只读）。
   - 手动导入：能导出 CSV 或 OFX 的银行。
   - 手动余额快照：只能导出 PDF、或者只能在 App 里看对账单的账户；还有定期存款、养老金、房产等没有流水的资产。
3. **Monzo 自转账规则**：Monzo 转账到使用者自己的其他账户，或者从自己的其他账户转入时，对方账户的余额自动相应增减；对方账户有自己的数据源时，不能重复计算。详见第 8 节。
4. **原始数据全部保留**，账本可以从原始数据重新生成（6.6 节）。
5. **加新数据源只需要加 connector**：比如以后接入一个开放银行（Open Banking）聚合服务，自动同步各家银行，核心不用改。
6. **为个人 OS 留好接口**（第 12 节）。
7. **能放心地长期运行**：连接失效、同步停止、对账不平时会提醒；数据有备份，并且做过恢复演练。

### 2.2 第一阶段不做的事

- 任何交易、下单、转账、提现。系统只读。
- 托管服务：不替别人运营实例，也不在代码里内置任何凭证。
- 预算、消费分类报表、税务计算（比如英国资本利得税 CGT）。但税务需要的数据要记全：每笔交易的成本、费用、成交时的汇率，以及账户的税务包装类型（ISA / SIPP / LISA / GIA）。
- 只读 MCP（让 Claude 等 Agent 查询数据）。接口已经预留，以后需要时再加（10.3 节）。
- DeFi 仓位（借贷、流动性池、质押合约里的资产）。钱包第一阶段只算地址上的币和代币余额。
- PDF 账单解析和 LLM 抽取（以后作为一种导入解析器加进来）。
- 盘中实时行情。日终估值就够了。
- 个人 OS 的模块（待办、日程、健康、日记），只留接口。
- 多用户、手机原生 App、公网访问（webhook 接收器除外）。

### 2.3 反目标：即使功能都能跑，出现这些也算失败

- **数字不可信**：净值算错；同一笔钱在两个账户里都算了一遍；同步停了却没人知道。
- **精度丢失**：金额经过了浮点数。
- **数据被锁死**：不能导出、不能重新生成，或者某个数据源的历史没留住。
- **泄露或越权**：凭证或财务数据暴露到公网；系统里出现私钥、助记词，或者能交易、能提现的凭证；仓库里出现任何真实的个人财务数据。
- **财务的假设混进公共层**：以后加个人 OS 模块时，必须改动公共表的结构。
- **复杂度失控**：为一个人维护的系统引入微服务、Kafka、Kubernetes 这类东西。

## 3. 约束

- **单用户、自托管**：部署在一台常开的 Linux 主机上，用 Docker Compose。可以是家用服务器、NAS 或 VPS，但不能是会休眠的笔记本。镜像要同时支持 amd64 和 arm64。
- **只读**：所有外部凭证只开只读权限；加密钱包只存地址和 xpub，私钥和助记词永远不进系统。某个数据源如果拿不到只读凭证，就换一条能只读的接入路径（比如 7.6 节的 eToro）。
- **网络**：只有 webhook 接收器通过 Cloudflare Tunnel 对公网开放，而且只放行指定路径；其他服务只能通过 Tailscale 私网访问。
- **默认值**：报告币种默认 GBP，时区默认 `Europe/London`，因为第一批接入的机构以英国为主；两者都可以配置。
- **开源与合规**：
  - 许可证 AGPL-3.0-or-later：任何人都可以自己部署和修改；把它改成托管服务对外提供的人，必须公开修改后的源码。
  - 自带凭证：每个使用者自己申请 Monzo OAuth client、SnapTrade Personal key、IBKR Flex token、Binance key 等。SnapTrade 的条款明确要求个人 key 不能放进分发的应用，也不能给别人用。
  - 替别人汇总银行账户信息，在英国属于需要 FCA 注册的账户信息服务（AISP）。本项目只面向“自己部署、只连自己的账户”的使用方式。
  - 仓库里不放任何真实个人数据：测试数据全部手写合成，不用“脱敏后的真实响应”；个人笔记放在不进 git 的 `private/` 目录。
  - 文档可以写“支持 Monzo、IBKR”等，但项目名称和图标不用任何机构的商标。
- **数据源的硬约束**（2026-11-11 的调研结果，链接见第 18 节；实现前要再核实一遍）：
  - **Monzo**：用户在 App 里完成 SCA 授权后的 5 分钟内，才能拉到较早的历史；之后只能拉到最近约 90 天。超出范围的请求可能返回成功、但结果被悄悄截断，所以要检查返回数据的日期范围。webhook 只有 `transaction.created` 一种事件，社区反馈会重复推送、偶尔漏发。这个 API 只供开发者本人（或少数白名单用户）使用。
  - **IBKR**：Flex Web Service 的 token 在 Client Portal 里生成，有效期 6 小时到 1 年可选，可以绑定 IP。数据以日终为准。个人账户使用的 Web API / TWS 需要网关登录并定期重新认证（含 2FA），不适合无人值守运行。
  - **Binance**：只读 API key，建议绑定 IP 白名单（需要固定的出口 IP）。成交历史只能按交易对逐个拉取。
  - **SnapTrade Personal**：免费，最多 20 个券商连接，但定价页写的是“限时免费”。连接券商时选只读（`connectionType=read`）。不同券商能提供的数据不一样，比如有的券商只有持仓、没有交易历史。
  - **eToro**：有自己的公开 API，用 Public API Key 加 User Key 认证，能读取持仓、余额、盈亏和交易历史。还没查到这个 key 能不能设成只读。SnapTrade 也支持 eToro，而且是只读接入。
  - **链上数据**：第三方接口会变。比如 Etherscan 在 2025 年底把 Base、BNB、OP 等链移出了免费档。
  - **手动导入**：各家机构能导出的格式差别很大：有的能导出 CSV、OFX、QIF，有的只有 PDF 对账单，有的金融科技 App 只能在 App 里下载对账单。导入设计要按机构配置，不能假设某一种格式。
  - **以后的自动同步**：GoCardless Bank Account Data 已经停止接受新注册。Enable Banking 有一个“受限模式”：只能读取自己关联的账户，条款写明仅限评估或个人使用，关联账户的数量可能受限，每次授权最长 90 天；官方页面没有写这个模式的价格，“免费”的说法来自第三方文档。它官方写的覆盖范围是欧洲经济区（EEA）的 30 个国家，英国不在其中，是否支持英国的银行没有确认。

## 4. 架构

### 4.1 数据怎么流动

```
Sources      Monzo · IBKR Flex · Binance · SnapTrade · eToro · on-chain
   │         CSV/OFX upload · manual snapshots
   │         scheduled pull │ webhook = "sync now" │ file → staging → review
   ▼
[raw]        immutable: every response / file as received, with hash
   │         versioned normalizers (replayable)
   ▼
[ledger]     accounts · instruments · transactions · postings
   │         balance snapshots · transfer matching · reconciliation
   ▼
[valuation]  prices · FX → net worth · holdings · allocation
   │
   ├─▶ REST / OpenAPI ──▶ web UI (React SPA)
   ├─▶ curated views ───▶ BI · exports (Beancount, Parquet) · MCP (later)
   └─▶ events (outbox) ─▶ notifications · future modules
```

### 4.2 进程划分

| 进程 | 技术 | 权限 | 网络 |
|---|---|---|---|
| web | React + Vite 构建出的静态文件，由 Caddy 托管 | 不访问数据库，只调用 api | 仅 Tailscale |
| api | FastAPI | 读写账本和配置；处理 OAuth 回调和凭证录入，所以能写凭证 | 仅 Tailscale |
| worker | Python + Procrastinate | 运行 connector、normalizer、转账匹配、估值、通知；能读凭证 | 不对外 |
| webhook 接收器 | 极小的 Python ASGI 应用 | 只能往 webhook 收件箱表插入数据 | 通过 Cloudflare Tunnel 对公网开放，只放行指定路径 |
| mcp（以后） | 官方 MCP Python SDK | 只能 `SELECT` 几个整理好的视图 | 本机或 Tailscale |
| postgres | PostgreSQL 18+ | 角色划分见 11.1 节 | 只在容器内部网络 |

api 和 worker 是同一套 Python 代码跑成的两个进程。整个后端是一个**模块化单体**，不拆微服务。

### 4.3 代码结构的规则

- 一个 Python 包分成两层：`core`（公共层）和 `modules/finance`（财务模块）。以后每个个人 OS 模块都是 `modules/` 下的一个新目录。
- **依赖只能单向**：模块可以依赖 core；core 不能依赖任何模块；模块之间不能直接引用。模块之间要交互，只能通过事件，或者 core 里的关联表。这条规则在 CI 里用 import-linter 强制检查。
- 每个模块用同一种方式注册自己：数据库迁移、API 路由、MCP 工具、事件处理器、前端页面。

## 5. 技术栈

具体的库是默认选择；实现者有充分理由时可以换成同类的库，但要写明理由。

| 层 | 选型 |
|---|---|
| 后端语言 | Python 3.13+；uv 管理依赖，ruff 做检查和格式化，pyright 开 strict |
| API | FastAPI + Pydantic v2，自动生成 OpenAPI 文档 |
| 数据库访问 | SQLAlchemy 2（带类型标注）+ Alembic 迁移 |
| 后台任务 | Procrastinate（用 Postgres 做队列，支持定时任务、重试和锁） |
| 数据库 | PostgreSQL 18+（用到 `uuidv7()`、`NUMERIC`、`JSONB`、`LISTEN/NOTIFY`） |
| 前端 | React + Vite + TanStack Router / Query / Table + shadcn/ui + ECharts；TypeScript 客户端从 OpenAPI 生成 |
| 数据源 SDK | Binance：CCXT 加少量原生 SAPI 调用；SnapTrade：官方 Python SDK；eToro、Monzo、IBKR Flex：直接调 HTTP 接口 |
| 链上数据 | BTC：descriptor 派生地址（比如 bdkpython 或 embit），余额查询走 Esplora 兼容的接口或自建 Electrum 服务；EVM 链：Alchemy、Blockscout 一类的数据服务；都放在可替换的接口后面 |
| 加密资产价格 | CoinGecko（免费档即可），Binance 的公开行情作为补充 |
| 部署 | Docker Compose：postgres、api、worker、webhook-in、web（Caddy）、cloudflared |
| 网络 | Tailscale（私网访问）；Cloudflare Tunnel（只给 webhook 接收器用） |
| 密钥 | SOPS + age 加密环境配置；凭证在数据库里用应用层加密存放 |
| 通知 | Apprise（具体渠道由使用者配置） |
| 监控 | healthchecks（定时同步的心跳）；结构化日志 |
| 备份 | `pg_dump` + WAL 归档（可按时间点恢复）+ restic 加密异地备份 |
| 以后：MCP | 官方 MCP Python SDK |

依赖的许可证都要和 AGPL-3.0 兼容；目前选的库都是 MIT、Apache-2.0、BSD、PostgreSQL 这类宽松许可。正式发布前跑一次依赖许可证扫描。

不选的东西和理由：

- **不用 Next.js**：单人私用的应用用不上 SSR / RSC 的好处，反而扩大了服务端攻击面（2025 年 12 月的 React2Shell 漏洞无需登录即可远程执行代码，CVSS 10）；大版本之间行为变化多；Server Actions 也不构成一套能给其他客户端复用的 API。
- **不用 SQLite**：没有真正的小数类型，并发写入能力弱。
- **不用 Redis、Kafka**：Postgres 足以承担队列和事件。

## 6. 数据模型

这里只规定概念和不变量。具体的表结构、命名和索引留给实现者。

### 6.1 Schema 划分

- `core`：公共对象（6.2 节）、原始数据、connector 状态、webhook 收件箱、凭证、事件、审计日志。
- `finance`：账户、资产、交易、分录、余额快照、价格、汇率、转账匹配、导入批次。
- Procrastinate 使用它自己的 schema。
- 以后每个模块一个 schema。

### 6.2 公共对象（core）

名字和结构要通用，但第一阶段只建财务现在就用得上的：

- **实体**：人、机构、地点。银行、券商、交易所、Monzo 的商户、转账对象都是实体，而不是财务专属的表。实体可以有别名和外部 ID（比如 Monzo 的 merchant id）。
- **文档**：导入的账单文件、IBKR 的 Flex XML、体积大的原始响应，以后还有收据。按内容哈希（sha256）存储，记录媒体类型、大小、原始文件名、来源。第一阶段的存储后端是本地目录，但接口要能换成 S3 兼容的存储。
- **关联**：任意两个对象之间的一条带类型的连线，比如“交易 ↔ 收据”。
- **连接**：每个已配置的数据源实例（7.1 节）。
- **原始记录**、**webhook 收件箱**、**凭证**：见第 7、11 节。
- **事件**：用 outbox 表实现。信封字段固定为：类型、来源、对象 ID、时间、数据、版本，不带任何财务专属字段。
- **审计日志**：每次写操作都记录：执行者类型（用户 / 规则 / connector / Agent / 系统）、执行者 ID、命令、参数、时间、变更内容、关联 ID。

所有对象都带 `owner` 字段，默认指向代表使用者本人的实体，为以后加入家庭成员留好位置。联名账户第一阶段按 100% 计入净值。

### 6.3 财务对象（finance）

- **资产（instrument）**：类型（法币、股票、ETF、基金、债券、加密资产、期权、差价合约、其他）、小数位数、计价币种（有价格的资产才有）。一个资产可以有多个标识符：ISIN、FIGI、ticker@交易所、IBKR conid、eToro instrument id、链 + 合约地址、CoinGecko id。同一个资产无论来自 IBKR、eToro、SnapTrade 还是 Binance，都要能合并成一条记录。资产可以被标记为“隐藏”，比如钱包里收到的垃圾空投代币，隐藏后不计入净值。
- **账户**：
  - 记账类型：资产、负债、收入、支出、权益。
  - 子类型：银行、储蓄 pot、券商、交易所、钱包、在途过渡账户、手动资产。
  - 所属机构（实体）、所有者、税务包装（ISA / SIPP / LISA / GIA / 无）、状态，以及开户和销户日期。
  - 父账户：比如 Monzo 的 pot 是 Monzo 主账户的子账户；Binance 的现货、资金、理财可以是一个 Binance 账户下的子账户。
  - **余额模式**：按交易推导、按快照，或者两者都有并互相对账。
  - **唯一权威来源**：一个连接，或者“手动”。同一个账户不能同时从两个来源入账。
- **账户识别信息（“我的账户”登记表）**：识别信息的类型（sort code + 账号、IBAN、卡尾号、收款人名称别名、入金参考号、钱包地址、xpub、Monzo user id、Monzo pot id、“通道”类机构）加上规范化后的值，指向一个账户。转账推断靠它（第 8 节）。一个 xpub 钱包是一个账户，它派生出的所有地址都是这个账户的识别信息。
- **交易和分录**：
  - 交易记录：发生时刻（`timestamptz`）、记账日期和起息日（`date`）、描述、对方实体、状态（pending / booked / inferred / confirmed / void）、来源（connector / 手动 / 推断 / 规则），并且能追溯到原始记录。链上交易额外记录链和交易哈希。
  - 分录记录：账户、资产、带符号的数量；需要时附上成本或价格（交易、换汇）。手续费（包括链上的 gas 费）单独成一条分录。
- **余额快照**：账户、资产、数量、时间点、来源。分为“机构报告的”和“手动录入的”两类。
- **价格和汇率**：资产（或币种对）、价格、时间点、来源。
- **转账匹配**：见第 8 节。
- **导入批次和暂存行**：见 7.8 节。

### 6.4 不变量

- **金额精度**：所有金额和数量，在数据库里用不限精度的 `NUMERIC`，在 Python 里用 `Decimal`，在 JSON 里用字符串传输。整条链路上禁止出现浮点数，要有 lint 或类型检查兜底。不用“整数存最小单位”：ETH 的最小单位 wei 有 18 位小数，int64 只装得下约 9.2 个 ETH。每个资产自己记录小数位数，用于显示和舍入。
- **复式平衡**：每笔交易的分录必须平衡。同一资产之间的转账按数量平衡；买卖和换汇按成本或价格换算后平衡，容差可配置。写入时由服务层校验，另外有定期的完整性任务复查全库。
- **每个数量都带资产**：不存在“没有币种的金额”。
- **防重复**：来自数据源的记录按“（连接，外部 ID）”唯一。没有外部 ID 的记录（比如 CSV 行），按规范化内容的哈希去重，而且要能区分真正的重复：同一天、同金额、同描述的两笔消费是两条记录，可以加上文件内的序号或流水里的余额来区分。
- **ID**：一律用 UUIDv7。

### 6.5 时间模型

- 机器时间戳一律存 UTC（`timestamptz`）。
- 有当地意义的时间额外存 IANA 时区（如 `Europe/London`），比如交易发生时使用者所在的时区。时区必须存下来，不能取查看者的：浏览器只知道查看者现在在哪，不知道事情发生时在哪。要存时区名，不能只存 `+08:00` 这样的偏移量：偏移量能显示过去的时刻，但做不了跨夏令时的日历计算。
- 存法按时间的种类区分：

  | 种类 | 例子 | 存法 |
  |---|---|---|
  | 已经发生的 | 交易、睡眠片段、日记的写作时刻 | UTC 时刻（`timestamptz`）+ IANA 时区 |
  | 计划中或重复的 | 将来的日程、“每周一 09:00” | 当地钟面时间 + IANA 时区；UTC 时刻由它推出，重复规则或 tzdata 变化时重算 |
  | 浮动的（跟着人走） | “不管在哪，早上 8 点吃药” | 当地钟面时间，不带时区 |
  | 全天的 | 生日、全天事件、记账日期 | `date` |

  计划中的时间保留钟面时间，因为那才是使用者的本意：“每周一伦敦 09:00”冬天是 UTC 09:00，夏天是 UTC 08:00；而且如果事件发生前当地改了时区规则，固定的 UTC 时刻也会跟着漂移。
- “日期”和“时刻”分开存：记账日期、起息日，以及以后的全天事件，都是 `date`。
- 每条记录都有两个时间：发生时间（业务上什么时候发生）和入库时间（系统什么时候知道的）。这样既能回放“某天当时看到的净值”，也能事后修正。
- 来源给的时间戳要按来源自己的时区规则解析。比如 IBKR Flex 的时间字段用的是哪个时区，实现者需要核实。

### 6.6 重新生成，以及保留使用者的决定

- 原始记录不可修改。账本里来自数据源的行由 normalizer 生成，随时可以删掉，再从原始数据重新生成。
- **使用者做的决定单独保存**：确认或拒绝某个转账匹配、手动修改描述或对方、隐藏某个资产、手动录入的交易和快照。这些决定用稳定的外部引用（来源 + 外部 ID）定位，账本重新生成后自动重新应用。这样升级 normalizer 不会冲掉使用者的工作。
- normalizer 带版本号。重新生成必须是确定性的：同样的原始数据加同样的决定，得到同样的账本。

## 7. 数据摄取

### 7.1 connector 契约（不带财务色彩）

- 基础接口只做一件事：**同步**。输入是连接和游标，输出是一批原始记录和新的游标。
- 每个 connector 声明自己支持哪些能力：余额、持仓、交易、webhook、设备推送、对外操作（写回外部系统）。第一阶段没有 connector 用到“设备推送”和“对外操作”，但契约里要有这两项：以后的待办和健康模块会用到。
- 有“对外操作”能力时，需要一张外部 ID 映射表：本地 ID ↔ 外部 ID ↔ 外部版本号（etag 之类）。规则是“每类对象只认一个权威来源”。
- **连接的生命周期**：创建 → 授权中 → 正常 → 需要重新授权 → 停用。每个连接记录最近一次成功同步的时间、最近的错误，以及凭证的过期时间（比如 IBKR Flex token）。
- **调度**：每个连接按计划定时同步；webhook 只触发“马上同步一次”；另外有回填任务。同一个连接同一时间只能有一个同步在运行，用锁保证。
- **原始记录**包含：连接、记录类型或接口名、请求参数、获取时间、内容（JSONB；大的 XML、CSV 存成文档再引用）、内容哈希、connector 版本。
- **normalizer**：把原始记录变成账本命令，按“（来源，记录类型）”分别版本化，是可以重放的纯函数。

### 7.2 Monzo

- **授权**：使用者在 developers.monzo.com 为自己的账户建一个 OAuth client（confidential 类型，能拿到 refresh token）。授权后，还要在 Monzo App 里确认 SCA。
- **授权完成后立刻全量回填**，而且必须在 5 分钟窗口内完成。之后增量同步：至少每天一次，再加上 webhook 触发。增量同步时，和上次的游标保留一段重叠时间。每次都要检查返回数据的日期范围，防止结果被悄悄截断。
- **映射**：
  - Monzo 账户 → 银行账户。
  - pot → 子账户。主账户和 pot 之间的转移是内部转账，两边都已知，直接配对。
  - 余额接口的结果 → 机构报告的快照，用来对账。
  - 商户 → 实体。
  - 交易里的对方信息（sort code、账号、名称、Monzo user id 等，以实际返回为准）→ 转账推断的输入。
- **webhook**：注册 `transaction.created`，地址指向 webhook 接收器，路径里带一个随机密钥。收到后只把它当作“马上同步”的信号，不信任 payload 本身。
- **重新授权**：refresh token 失效时发通知提醒使用者，界面上提供重新授权的入口。
- **实现者要核实**：对方信息和 pot 转移在 API 返回里的实际字段；授权窗口内到底能拉到多久以前的历史（不同来源说法不一）。

### 7.3 IBKR（Flex Web Service）

- **凭证**：Flex token 和 query ID，按凭证存储。
- **两类 Flex Query**：
  - Activity：持仓、各币种现金、成交、现金流水（入金出金、股息、预扣税、利息、费用）、公司行动、汇率。
  - Trade Confirmation：当天的成交。
- **流程**：先调用 SendRequest，再调用 GetStatement，分两步。每天在 IBKR 生成报表之后运行。XML 原文存成文档，解析结果存成原始记录。
- **映射**：
  - 每个 IBKR 账户 → 券商账户，多币种现金。
  - 持仓 → 机构报告的快照，附上 IBKR 报告的成本基础和当日价格。
  - 成交 → 带成本、佣金、成交时汇率的交易。
  - 入金和出金 → 转账匹配的另一侧，和 Monzo 的转出配对。
- **历史**：Flex 单次查询的时间范围有上限，需要核实。更早的历史可以分段拉取，或者从 Client Portal 导出后走手动导入。
- **token 过期**：记录过期日期，提前两周提醒。

### 7.4 Binance

- **凭证**：只读 API key，建议绑定 IP 白名单。
- **以余额快照为主**：现货、资金账户、理财（活期和定期）等各个钱包的余额，按子账户分别记快照。使用者实际用到合约或杠杆账户的话，也要覆盖。
- **转账相关的记录**：法币和加密资产的充值、提现历史。它们是转账匹配的另一侧，比如 Monzo 转入 Binance 的 GBP，或者从 Binance 提币到自托管钱包（用链上交易哈希配对）。
- **成交记录**：不是第一阶段的必需品，因为余额靠快照。能低成本拉到的（比如闪兑记录）可以存原始数据，留给以后算成本。
- **实现**：主要用 CCXT，CCXT 没覆盖的理财、资金账户接口直接调原生 SAPI。

### 7.5 SnapTrade Personal

- **用途**：接 IBKR 以外、没有更好接入方式的券商。具体接哪些券商，由使用者在 SnapTrade 的连接门户里自己连。
- **授权**：用使用者自己的 Personal 套餐 client ID 和 consumer key；连接券商时选只读（`connectionType=read`）。
- **数据**：持仓（带价格和平均成本）、余额、交易活动。券商不提供交易历史时，这个账户按快照模式处理。
- **webhook**：连接断开、持仓更新等事件进 webhook 接收器，同样只当作“马上同步”的信号。
- **防重复**：IBKR 已经直连，不能再通过 SnapTrade 接一次；同一个账户只能有一个权威来源。
- **可替换**：定价页写着“限时免费”，所以它只是一个可以换掉的 connector。

### 7.6 eToro

两条路，按“只读”约束选：

- **首选 eToro 自己的公开 API**：前提是能创建只读的 key。它用 Public API Key 加 User Key 认证，能读取持仓、余额、盈亏和交易历史，数据比经过第三方更全。
- **否则走 SnapTrade**：SnapTrade 对 eToro 是只读接入。

实现者要先核实 eToro 的 key 能不能限定为只读，再决定走哪条路。两条路不能同时入账。

### 7.7 自托管钱包

- **只读**：只存地址和 xpub，私钥和助记词永远不进系统。
- **BTC**：用 descriptor 在本地派生地址，按 gap limit 扫描，查询每个地址的 UTXO 余额和交易。数据来源是 Esplora 兼容的接口（比如 mempool.space）；在意隐私的话，接自建的 Electrum 服务。
- **EVM 链**：查询原生币和 ERC-20 代币的余额，以及转入转出记录。数据来源放在可替换的接口后面（比如 Alchemy、Blockscout）。代币的元数据和价格用合约地址关联到 CoinGecko。垃圾空投代币可以由使用者隐藏（6.3 节）。
- **其他链**（比如 Solana、Tron）：有需要时作为新的数据源实现加进来。
- **链的范围**：第一阶段默认支持 BTC 和 EVM 链（以太坊和主要的 L2）。
- **转账匹配**：链上交易哈希和地址是识别信息，用来配对 Binance 的提币、充值和钱包的转入转出。

### 7.8 手动导入

- **流程**：在界面上传文件 → 文件存成文档 → 用“解析配置”解析 → 进入暂存区 → 使用者审阅 → 提交入账。一个批次可以整体撤回。
- **解析配置**按机构区分：列映射、日期格式、正负号约定、编码。配置以数据形式保存，加一家机构不用改代码。第一阶段要提供一个通用的 CSV 映射器，外加 OFX 解析。仓库里附带的示例配置只配合成的示例文件。
- **按导出能力分三类处理**：
  - 能导出 CSV 或 OFX 的银行：走导入。
  - 只有 PDF 对账单的账户：按“只记余额快照”处理（见下文）。
  - 只能在 App 里下载对账单的金融科技账户：能导出 CSV 就走导入，否则按余额快照处理。多币种钱包的每个币种分别记余额；ISA、LISA 这类账户标上对应的税务包装。
- **暂存区**显示：哪些行是新的、哪些是重复的、哪些能和已有的推断分录配对（第 8 节）。
- **手动快照和手动资产**：没有流水的账户（定期存款、养老金、房产、车等）直接录入余额或估值。定期存款可以记录利率和到期日。
- **手动持仓按数量录入**：任何资产都可以按数量手动录入（比如 0.5 BTC、100 股某只 ETF），由价格来源估值（第 9 节）；没有价格来源的资产，可以手动录入价格，或者直接录入报告币种的估值。
- **负债同样适用**：信用卡、贷款等负债账户和银行账户走同一套导入和快照流程。
- **只能导出 PDF 的账户**：第一阶段把它当作“只记余额快照”的账户。转账规则照样在它上面生成推断分录，再用手动录入的余额快照核对，差异超过容差就提醒。PDF 解析以后再加。

### 7.9 webhook 接收器

- 一个极小的 ASGI 应用。第一阶段有 Monzo 和 SnapTrade 两个路由；以后可以加 Notion 等。
- 能验签就验签，否则校验路径里的密钥。然后把原始请求写进收件箱表，立即返回 200。
- 它使用的数据库角色只能往收件箱表插入数据。worker 读取收件箱，再安排对应的同步。
- 它是全系统唯一对公网开放的部分，所以要尽量小，而且没有任何读取能力。

## 8. 转账匹配（Monzo 自转账规则）

这是核心里一个**通用的引擎**，Monzo 只是第一个用到它的数据源。

1. **“我的账户”登记表**（6.3 节）记录使用者所有账户的识别信息。
2. 一个有数据源的账户 A 出现一笔转出或转入，而且对方信息命中了登记表里的账户 B 时：
   - **B 没有数据源（手动账户）**：直接在 B 上生成一条推断分录，状态为 `inferred`，B 的余额立刻变化。以后导入 B 的账单时，导入流程先和已有的推断分录配对（金额、币种、日期窗口、参考号）；配上了就把状态改成 `confirmed`，不再新建交易。B 只有余额快照时，用快照核对。
   - **B 有自己的数据源**（比如 IBKR、Binance）：A 的转出先记进“在途”过渡账户。等 B 的数据源报告了对应的入金，就把在途清零，标记为已配对。超过设定天数还没配上，就发提醒。
3. 换汇和手续费各自是独立的分录。比如转出的是 GBP，到账的是 USD；链上转账的 gas 费单独记。
4. **对方信息没有命中登记表**：不做推断。可以按名称模糊匹配给出建议；使用者确认后，把这条识别信息补进登记表，下次自动命中。
5. 使用者对匹配的确认和拒绝都属于“使用者的决定”，账本重新生成后仍然保留（6.6 节）。
6. 日期窗口、金额容差等参数可以配置，默认值由实现者定。
7. **要覆盖的边界情况**：一笔转出分几次到账；退款和冲正；两个 Monzo 账户之间的转账（比如联名账户）；pot 转移（内部转账）；定期转账；Binance 和自托管钱包之间的提币、充值（按交易哈希配对）；经过中间机构的转账（见下一条）。
8. **经过中间机构的转账**（比如经 Wise 这类汇款服务）：Monzo 看到的收款方是中间机构，不是最终的目的账户。
   - 如果使用者在中间机构有自己的账户（有余额），就把它当作普通的自有账户，两段转账分别匹配。
   - 如果中间机构只是汇款通道，就在登记表里把它登记成“通道”类识别信息：转出先记入在途，等目的账户的入账来配对；换汇差额和手续费单独记分录。
9. **界面**：转账审阅页，列出在途款项和已在途的天数、匹配建议、冲突，可以逐条确认或拒绝。

## 9. 估值和视图

- **某一天的账户余额**：
  - 按交易推导的账户：截至那天的分录求和。
  - 按快照的账户：那天或之前最近的一次快照。
  - 两者都有的账户：以最近的快照为基准，加上快照之后的分录；同时和后续快照对账，差异超过容差就提醒。
- **估值** = 数量 × 那天或之前最近的价格 × 那天或之前最近的汇率（从计价币种换算到报告币种）。
- **价格和汇率的来源**：
  - IBKR Flex 里的当日价格和汇率。
  - SnapTrade、eToro 报告的持仓价格。
  - 加密资产：CoinGecko（按资产 ID 或合约地址），Binance 的公开行情作为补充。
  - 欧洲央行的参考汇率（比如通过 Frankfurter 获取），用于常见币种。欧洲央行没有的币种（比如部分非洲、南美货币），需要另接一个汇率源，由实现者选。
  - 手动录入的价格和估值。

  找不到价格时要明确标记为“缺价格”，不能当成 0。
- **净值历史**：可以按天物化，也可以查询时现算，由实现者定。但改了价格或历史数据之后，必须能重算。
- **整理好的 SQL 视图**（带版本号）是界面、BI、导出，以及以后的 MCP 共用的契约：
  - 每日净值；
  - 持仓（可按账户、资产、资产类别、币种、税务包装、机构分组）；
  - 对账差异；
  - 过期的连接；
  - 未配对的转账；
  - 缺价格的资产。

## 10. 接口

### 10.1 REST API

- 路径带版本号，比如 `/api/v1`。写操作是“命令”：经过服务层、记审计日志、带执行者。读操作是“查询”。
- OpenAPI 文档是前端唯一的契约。前端的 TypeScript 客户端从它生成，CI 检查两边是否一致。
- 第一阶段的执行者只有“用户”和“系统 / connector”，但审计模型要已经支持“规则”和“Agent”。

### 10.2 网页界面（第一阶段）

- **净资产**：总额、按账户、资产类别、币种、税务包装的分布、历史曲线。
- **账户和持仓**。
- **连接**：状态、最近同步时间、连接和重新授权的入口，以及各数据源凭证的录入（Binance key、eToro key、钱包地址和 xpub 等）。
- **导入**：上传、暂存审阅、提交或撤回。
- **转账**：在途款项、匹配建议、确认或拒绝。
- **我的账户登记表**：维护各账户的识别信息。
- **资产**：合并重复的资产、隐藏垃圾代币、处理缺价格的资产。
- **设置**：报告币种、时区等。

界面要在手机浏览器上能用（通过 Tailscale 访问）。

### 10.3 以后：只读 MCP

第一阶段不做。它的用途是让 Claude 等 Agent 用自然语言查询数据，回答固定界面没有覆盖的临时问题，比如“这个月比上个月多花了多少”“美股在总资产里占多少”“上周转去另一家银行的钱配上了没有”；以后接入日程、健康、日记后，还能做跨领域的问题。

现在预留的部分：第 9 节整理好的视图、`mcp_ro` 数据库角色的设计（只能 `SELECT` 这些视图，读不到凭证和原始数据）、模块注册时的“MCP 工具”这一项。以后要加时：

- 只在使用者自己的设备上用（比如本机的 Claude Code），可以完全不对公网开放。
- 如果要在 claude.ai 网页版或手机 App 上用，它们是从 Anthropic 的云端连过来的，需要一个对公网开放、带 OAuth 登录的端点，到时要重新评估“只有 webhook 接收器对公网开放”这条约束。
- 要让 Agent 写入时，另做一个“操作型 MCP”，调用同一套命令层：敏感操作需要使用者确认，并且可以撤销。

### 10.4 事件

- 写入 outbox 表，通过 `LISTEN/NOTIFY` 唤醒消费者。
- 第一阶段的消费者只有通知：连接失效、同步过期、在途转账超时、对账不平、缺价格。
- 事件类型带模块前缀，比如 `finance.transaction.created`；数据格式带版本号。

## 11. 安全与运维

### 11.1 数据库角色

| 角色 | 使用者 | 权限 |
|---|---|---|
| owner（迁移） | 只在执行迁移时使用 | 建表、改表 |
| backend | api、worker | 读写业务表，读写凭证 |
| webhook_in | webhook 接收器 | 只能往收件箱表插入数据 |
| mcp_ro（以后） | mcp | 只能 `SELECT` 整理好的视图 |

### 11.2 凭证

- 在应用层加密后存进数据库。主密钥来自环境变量；环境配置用 SOPS + age 加密，git 里不出现明文。
- 凭证不能出现在日志、错误信息、事件或 API 响应里。
- 录入凭证时检查权限：能查到权限的数据源（比如 Binance 的 key），发现不是只读就拒绝保存。

### 11.3 身份

- 第一阶段只能从 Tailscale 内访问，由 Tailscale 确认身份，具体机制由实现者定。代码里通过一个“当前用户”抽象拿到身份，以后换成 passkey 或 OIDC 时只改这一处。
- README 必须写清楚安全前提：只经 Tailscale 访问、凭证只读、绝不存私钥。部署者未必了解这些，不能只靠默认值。

### 11.4 备份

- 每天 `pg_dump`，加上 WAL 归档（可以恢复到任意时间点）。
- 文档存储一起备份；用 restic 加密后存到异地。
- 写一份恢复步骤文档，并且实际演练一次恢复。

### 11.5 监控与通知

- 每次定时同步完成后向 healthchecks 发心跳，没按时发就告警。
- 连接健康状态变化时，通过 Apprise 推送通知。
- 日志用结构化格式，凭证一律脱敏。

## 12. 个人 OS 的接口（第一阶段必须做对）

计划中的第一批模块，以及可能的接入方式（仅供参考，不在第一阶段实现）：

- **待办和日程**：比如 Notion（官方 API 和 webhook）。Notion Calendar 本身不存事件，它显示的是背后的 Google、iCloud 或 Outlook 日历里的事件，加上 Notion 数据库里带日期的条目，所以日程要直接接背后的日历。
- **健康数据**：比如 Apple 健康。它没有云端 API，可以首次手动全量导出，之后用 Health Auto Export 这类 App 定时推送 JSON。
- **日记**：可以是自带的日记模块，也可以读取外部笔记工具。比如 Capacities 的官方 API 还在 beta，没查到能列出全部对象的接口，靠完整导出最稳妥；Obsidian 这类纯 Markdown 工具可以直接读文件。

为了这些模块，第一阶段必须做到：

1. core 和 finance 分开，依赖只能单向（4.3 节）。
2. 公共对象的名字和结构不带财务假设（6.2 节）。
3. connector 契约包含“设备推送”和“对外操作”能力，以及外部 ID 映射（7.1 节）。
4. 时间模型按 6.5 节执行。以后的睡眠和日记存 UTC 时刻加 IANA 时区；计划中和重复的日程存当地钟面时间加 IANA 时区；全天事件存日期。
5. 每个模块有自己的 schema 和数据库角色；以后的 MCP 对每个模块默认关闭，由使用者逐个开启。健康和日记的数据，以后还要经过一个“AI 网关”，决定能不能发给外部模型。
6. 审计日志的执行者模型包含“Agent”。以后的 AI 助手模块将是 `modules/lares`（每个领域 agent 称为一个“lar”）；它的 agent 通过这个执行者类型和第 5 条的 AI 网关行事。
7. 以后的设备推送走 Tailscale 私网内的接收端点，每台设备一个 token。webhook 接收器和收件箱的设计要能复用到这里。

## 13. 里程碑（建议顺序）

- **M0 骨架**：代码仓库、Docker Compose、Postgres 及各角色、迁移、CI（ruff、pyright、测试、import-linter、OpenAPI 一致性检查）、备份脚本。
- **M1 账本核心**：core 和 finance 的 schema；带不变量校验的账本服务；手动账户、手动快照、按数量录入的手动持仓；每日净值视图；最小可用的界面。
- **M2 手动导入**：通用 CSV 映射器、OFX 解析、暂存审阅、批次撤回；至少一份示例解析配置（配合成数据）。
- **M3 Monzo**：OAuth、5 分钟窗口内的回填、增量同步、pot、webhook 接收器、重新授权提醒。
- **M4 转账匹配**：登记表、推断分录、在途过渡账户、中间机构、审阅界面。Monzo 自转账规则在这一步端到端跑通：仓库里的测试用合成账单，使用者另外用自己的真实账单在本地验证。
- **M5 IBKR Flex**：持仓、现金、成交、入金出金；价格和汇率；估值；和 Monzo 的转出配对。
- **M6 Binance**：各钱包余额快照、充值提现、CoinGecko 价格。
- **M7 自托管钱包**：BTC（xpub / descriptor）和 EVM 链；按交易哈希和 Binance 的提币、充值配对。
- **M8 券商**：SnapTrade（使用者连接的券商）和 eToro（按 7.6 节选路）。
- **M9 收尾**：对账提醒、Beancount 和 Parquet 导出、恢复演练、依赖许可证扫描。

之后的路线：

- **第二阶段**：找一个能覆盖英国银行、个人可用的开放银行聚合服务，自动同步各家银行（Enable Banking 是否支持英国还没确认，见第 17 节）；PDF 账单解析；用户自定义规则；按需要加只读 MCP。
- **再往后**：个人 OS 模块，顺序是日程和待办 → 健康 → 日记。

## 14. 验收标准

- 任选一天，系统算出的净值和人工核算一致，误差在容差内。
- 各数据源的余额和机构自己报告的一致：Monzo 必须完全一致；IBKR 和它报告的 NAV、Binance、SnapTrade、eToro 和各自报告的余额与持仓、钱包和区块浏览器上的余额，都在容差内一致。
- 从 Monzo 转一笔钱到一个手动导入的银行账户：两边余额都正确变化；之后导入那家银行的 CSV，不会重复入账。
- 从 Monzo 转一笔钱到 IBKR：钱先出现在在途账户里；IBKR 报告入金后自动配对。整个过程中，净值既没有凭空少一块，也没有多算一次。
- 从 Binance 提币到自己的钱包：两边按交易哈希自动配对，gas 费和手续费单独记录。
- 删掉账本里所有来自数据源的行，再从原始数据重新生成：结果和原来完全一致，使用者的决定都还在。
- 让 Monzo 的 token 失效：在设定的时间内收到提醒。
- 用备份恢复出一套环境，数据完整。
- 有测试证明：webhook_in 角色读不了任何表；保存非只读的 Binance key 会被拒绝。
- 金额链路上没有浮点数，由 lint 或类型检查保证。
- 仓库里没有真实个人数据，测试数据全部是合成的。

## 15. 决策记录

| 决定 | 状态 |
|---|---|
| 统计和追踪所有资产；数据源包括 Monzo、IBKR、Binance、SnapTrade Personal、eToro、自托管钱包，以及银行的手动导入和余额快照 | 需求 |
| Monzo 自转账要自动增减对方账户的余额 | 需求 |
| 以后扩展成个人 OS；现阶段优先考虑前向兼容 | 需求 |
| 开源，许可证 AGPL-3.0-or-later；自带凭证、自己部署，不做托管服务 | 已确认 |
| Python 后端 + React SPA + PostgreSQL，不用 Next.js | 已确认 |
| worker 独立成进程；MCP 独立成只读服务（MCP 第一阶段不做） | 已确认 |
| 所有财务数据源都在第一阶段接入 | 已确认 |
| 数据层设计：原始数据层、复式账本、事件表、模块化单体 | 已确认 |
| eToro：能拿到只读 key 就直连它的 API，否则走 SnapTrade | 默认决定 |
| 只能导出 PDF 的账户第一阶段按余额快照处理 | 默认决定 |
| 钱包第一阶段默认支持 BTC 和 EVM 链；DeFi 仓位不做 | 默认决定 |
| 第一阶段加入加密资产价格源；任何资产都可以按数量手动录入 | 默认决定 |
| 经过中间机构的转账，按“自有账户”或“通道”两种情况处理 | 默认决定 |
| 报告币种默认 GBP、时区默认 Europe/London，可配置 | 默认决定 |
| 具体的库和工具（FastAPI、Procrastinate、CCXT、CoinGecko、Caddy、Apprise 等） | 默认决定 |
| 部署在一台常开的 Linux 主机上 | 默认决定 |
| 使用者的决定单独保存，账本重新生成后重新应用 | 默认决定 |
| 联名账户第一阶段按 100% 计入 | 默认决定 |
| 测试数据全部合成；个人笔记放在不进 git 的 `private/` | 已确认 |
| 文档双语：英文为主版本，中文译文以 `*.zh-CN.md` 放在旁边 | 已确认 |
| 里程碑顺序（M0–M9）和之后的路线 | 默认决定 |
| 项目名称：Lararium（发布前由 financial-gate 改名）；以后的 AI 助手模块命名为 `lares` | 已确认 |
| posting 的 cost 或 price 必须用另一种 instrument 计价，不能用 posting 自己的 instrument；否则它能把数量任意缩放，让任何交易都显得平衡。数据库也强制这一点 | 默认决定 |
| instrument 有 ISO 4217 代码，当且仅当它是法币 | 默认决定 |
| 数据库强制的每条规则，领域代码都在写入前先检查；请求仍然引发的数据库错误返回 4xx（`constraint_violation`、`invalid_value`）或 503（`query_timeout`），并附可读的说明，绝不返回 500 | 默认决定 |
| 金额小数点前后各最多 40 位数字 | 默认决定 |
| 净值序列最多覆盖十年（3,653 天），每条数据库语句 30 秒超时 | 默认决定 |
| API 拒绝跨站请求（包括读请求），本应用不能被别的页面嵌入 | 默认决定 |
| 每个 API 错误都带人能读懂的 `message`；Web 界面原样显示它 | 默认决定 |

## 16. 留给实现者

- 具体的表结构、命名、索引，以及包的目录结构。
- normalizer 的内部实现；匹配参数的默认值。
- 净值是物化还是现算。
- 界面的视觉设计和交互细节。
- 库的具体版本；必要时换成同类的库（要写明理由，并确认许可证和 AGPL-3.0 兼容）。
- 链上数据服务的具体选择（在可替换的接口后面）。
- eToro 走哪条路（按 7.6 节的规则，先核实 key 的权限）。
- 欧洲央行以外的第二个汇率源选哪家。
- 主密钥的具体存放方式。
- 测试策略的细节。connector 测试的样例数据按各接口文档手写合成；用基于性质的测试（property-based testing）覆盖账本不变量。
- 是否另外部署 Metabase 之类的 BI 工具。

## 17. 待确认的问题（不阻塞开工）

- **英国银行的自动同步方案**（第二阶段才需要）：Enable Banking 官方写的覆盖范围是 EEA 的 30 个国家，英国不在 EEA 内，是否支持英国的银行没有确认。到第二阶段时，可以注册后在它的控制台里按国家查看可选的银行；不支持的话，另找覆盖英国、个人可用的聚合服务。
- **eToro 的 key 能不能设成只读**：实现 M8 前核实。

## 18. 参考

- Monzo SCA 与历史窗口：[Monzo community: SCA changes for developer apps](https://community.monzo.com/t/strong-customer-authentication-upcoming-changes-to-developer-apps/78763) · [Open Banking: Monzo](https://openbanking.atlassian.net/wiki/spaces/AD/pages/1135542651/Monzo+Bank+Limited) · [TrueLayer: Monzo ~88–90 days](https://support.truelayer.com/hc/en-us/articles/360016450337-Why-am-I-seeing-only-88-days-of-transactions-on-Monzo-requests)
- Monzo webhook：[Monzo community: inconsistent webhook delivery](https://community.monzo.com/t/inconsistent-monzo-webhook-delivery-missing-transaction-data-on-my-website-integration/184837)
- IBKR：[Flex Web Service access token](https://www.interactivebrokers.com/docs/web-api/flex-web-service/client-portal-configuration/enable-and-create-access-token) · [IBKR Web API](https://www.interactivebrokers.com/campus/ibkr-api-page/webapi-doc/)
- SnapTrade：[SnapTrade Personal](https://snaptrade.com/personal) · [Personal vs Commercial](https://docs.snaptrade.com/docs/personal-vs-commercial) · [Pricing](https://snaptrade.com/pricing) · [Broker access guide](https://support.snaptrade.com/snaptrade-broker-access-guide)
- eToro：[eToro API: authentication](https://api-portal.etoro.com/core/getting-started/authentication) · [eToro API: portfolio breakdown](https://api-portal.etoro.com/api-reference/trading--real/get-portfolio-breakdown.md) · [eToro public API launch](https://www.etoro.com/news-and-analysis/press-releases/etoro-marks-15-years-of-social-investing-with-launch-of-public-apis-and-expansion-of-copytradertm-to-the-us/)
- 银行聚合：[GoCardless Bank Account Data announcement](https://gocardless.com/bank-account-data/announcement) · [Enable Banking terms](https://enablebanking.com/terms) · [Enable Banking FAQ](https://enablebanking.com/docs/faq) · [Enable Banking homepage](https://enablebanking.com/) · [Actual Budget: Enable Banking](https://actualbudget.org/docs/advanced/bank-sync/enable-banking)
- 链上：[Etherscan free tier changes](https://info.etherscan.com/whats-changing-in-the-free-api-tier-coverage-and-why)
- Next.js 风险：[Vercel: CVE-2025-55182](https://vercel.com/changelog/cve-2025-55182)
- 个人 OS 相关：[Notion Calendar: manage calendars & events](https://www.notion.com/help/manage-your-calendars-and-events) · [Notion API upgrade guide 2025-09-03](https://developers.notion.com/docs/upgrade-guide-2025-09-03) · [Health Auto Export](https://apps.apple.com/app/id1115567069) · [Capacities API overview](https://developers.capacities.io/api/overview) · [TimescaleDB 2.23.0（支持 PG18）](https://newreleases.io/project/github/timescale/timescaledb/release/2.23.0)
- 英国 CGT 参考实现：[cgt-calc](https://pypi.org/project/cgt-calc/)
- 许可证：[GNU AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.html)
