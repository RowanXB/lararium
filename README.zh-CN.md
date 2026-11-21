# Lararium

[English](README.md) | 简体中文

Lararium（家神龛）是古罗马家庭中供奉家宅守护神 Lares 的神龛。这个项目就是你家的 lararium：
存放你的财务数据，并逐步扩展到生活中的其他数据，由你自托管、由你保管。它的 AI 助手将是它的
lares。

一个自托管、只读的个人财务系统：复式记账账本、余额快照、价格与汇率，以及净资产视图。
后端为 Python 3.13（FastAPI）+ PostgreSQL 18，前端为 React SPA；用 Docker Compose 部署，
只能通过 Tailscale 访问。许可证为 AGPL-3.0-or-later。

## 本地开发

环境要求：Docker（含 buildx，守护进程已启动）、uv、Python 3.13、Node 24、pnpm 11、git。

运行全部检查（lint、格式、类型、导入约束、在一次性 PostgreSQL 容器上跑的后端测试、
OpenAPI/客户端漂移检查、前端类型检查与测试）。它严格按 `backend/uv.lock` 和
`web/pnpm-lock.yaml` 安装依赖，任一锁文件过期都会失败：

```bash
make check
```

测试容器也可以跑在一台通过 ssh 访问的 Docker 主机上（你在那台主机上的用户需要能使用 Docker，
守护进程的 socket 需要在 /var/run/docker.sock，且 sshd 允许 TCP 和 Unix socket（stream-local）
转发，这是 OpenSSH 的默认设置）。Docker 守护进程的 socket 和数据库端口都经 ssh 转发，端口只发布
在那台主机的回环地址上：

```bash
DOCKER_HOST=ssh://<host> make check
```

如果一次运行没来得及清理就结束了（SIGKILL、SIGTERM、关掉终端、内存耗尽、连接断开），它的容器
会留在那台主机上，本机上它的两个 ssh 转发也可能还在运行。用下面的命令列出容器，再用
`docker rm -f` 删除；确认没有别的测试在跑时，用 `pkill` 停掉转发：

```bash
docker -H ssh://<host> ps -a --filter label=org.testcontainers=true
```

```bash
pkill -f 'ssh -N -o ControlMaster=no -o ControlPath=none'
```

要运行应用，先启动一个开发数据库（角色由部署时用的同一个初始化脚本创建），执行迁移，
再以自动重载方式启动 API：

```bash
docker run -d --name lararium-dev-db -p 127.0.0.1:5432:5432 \
  -e POSTGRES_PASSWORD=dev \
  -e LARARIUM_OWNER_PASSWORD=owner -e LARARIUM_BACKEND_PASSWORD=backend \
  -e LARARIUM_WEBHOOK_IN_PASSWORD=webhook_in \
  -v "$PWD/infra/postgres:/lararium:ro" \
  -v "$PWD/infra/postgres/init:/docker-entrypoint-initdb.d:ro" \
  postgres:18

cd backend
LARARIUM_MIGRATION_DATABASE_URL=postgresql+psycopg://lararium_owner:owner@127.0.0.1:5432/lararium \
  uv run alembic upgrade head
LARARIUM_DATABASE_URL=postgresql+psycopg://lararium_backend:backend@127.0.0.1:5432/lararium \
LARARIUM_AUTH_MODE=dev \
  uv run uvicorn lararium.api.app:create_app --factory --reload
```

`LARARIUM_AUTH_MODE=dev` 会跳过 Tailscale 请求头校验，切勿在部署中使用。在另一个终端启动前端
开发服务器（它把 `/api` 代理到 `127.0.0.1:8000`）：

```bash
cd web
pnpm install --frozen-lockfile
pnpm dev
```

修改 API 之后，用 `make openapi` 重新生成 `web/openapi.json` 和类型化客户端，并一起提交。

## 服务器部署

在装有 Tailscale 和 Docker Engine 28 或更新版本的服务器上（局域网可信时，旧版本的引擎也可以；
见下面的安全模型）：

```bash
cp infra/env.example infra/.env
chmod 600 infra/.env
# 填写 infra/.env：每个密码都用 `openssl rand -hex 32` 生成，主密钥用
# `openssl rand -base64 32` 生成，并把 LARARIUM_OWNER_LOGIN 设为你的 Tailscale 登录名
# （例如 you@example.com）。
make up
tailscale serve --bg 8080
```

`make up` 会构建镜像并启动 PostgreSQL（`db`），以 `lararium_owner` 身份执行一次迁移
（`migrate`），然后以 `lararium_backend` 身份启动 API（`api`）、后台 worker（`worker`：连接同步和
定时任务），以及提供 SPA 并转发 `/api/*` 的 Caddy（`web`）。`make down` 停止整套服务；数据保留在 `lararium_pgdata` 卷中，账单文件（上传的，或从 IBKR 获取的）保留在 `lararium_documents` 卷中。

在 tailnet 里的任意设备上打开 `https://<主机名>.<你的tailnet>.ts.net`。

**升级连接器出现之前的部署。** compose 现在要求设置 `LARARIUM_MASTER_KEY` 和
`LARARIUM_WEBHOOK_IN_PASSWORD`，所以在下一次 `make up` 之前先把两者填进 `infra/.env`（主密钥用
`openssl rand -base64 32` 生成，密码用 `openssl rand -hex 32` 生成）。然后按
[通过 Cloudflare Tunnel 接收 Webhook](#通过-cloudflare-tunnel-接收-webhook) 一节里**已有的数据库**
的说明创建 webhook 接收器的角色。

**如果主机的 443 端口已被占用**，换一个 HTTPS 端口：

```bash
tailscale serve --bg --https=8443 8080
```

然后打开 `https://<主机名>.<你的tailnet>.ts.net:8443`。身份请求头仍由 `tailscale serve`
提供，其他都不用改。不要改成把 Lararium 挂在别的反向代理后面（比如已经占着 443 的
nginx）：不经过 `tailscale serve` 的流量可以伪造 `Tailscale-User-Login`。

### 安全模型

- 系统是只读的：从不下单、转账或提现。
- 所有第三方凭据都是只读的：连接器需要 API key 或 token 时，只创建只读权限的那种。
- 从不保存私钥和助记词：钱包只按公开地址跟踪。
- 建议使用 Docker Engine 28 或更新版本。在 Linux 上，更旧的引擎（群晖自带 24）会让局域网里加了
  路由的其他机器直接访问到容器，包括 `web` 和 `api`，并自己发送身份请求头。只有在局域网可信时
  才可以接受：没有你管不了的设备，访客 Wi-Fi 已隔离。否则请升级 Docker，或者用防火墙挡住局域网
  到 Lararium 的 Docker 网桥的访问。Docker Desktop 不受影响。
- webhook 接收器的数据库角色（`lararium_webhook_in`）是只写的：它只能往 webhook 收件箱里插入行，
  不能读取，也碰不了其他任何表。它拿不到主密钥。Cloudflare 负责终结 TLS，所以它能看到 webhook
  的明文内容；这些内容只被当作「立即同步」的信号，真正的数据始终由 worker 向 Monzo 拉取。

### 访问方式与请求头信任

API 依据 `Tailscale-User-Login` 请求头认证请求，只放行 `LARARIUM_OWNER_LOGIN`。这之所以安全，
完全取决于服务的暴露方式：

- `tailscale serve` 是唯一入口。它接受来自你 tailnet 的连接，把 `Tailscale-User-Login`
  设为调用者的身份，再转发到 `127.0.0.1:8080`。
- `web` 服务只绑定 `127.0.0.1:8080`，`api` 服务不发布任何端口：只有 compose 网络里的
  Caddy 能访问它。
- 除此之外，任何东西都不能访问 8080 端口：不能有别的反向代理、端口转发、
  `tailscale funnel`，主机上也不能有不受信任的本地用户或容器。谁能访问到这个端口，
  谁就能自己伪造这个请求头、冒充你。

`tailscale serve` 会给你浏览器发出的每个请求都附上你的身份，包括别的网站页面触发的请求，
所以只要 `Sec-Fetch-Site` 或 `Origin` 显示请求来自别的网站，API 就拒绝它，读请求也一样。
Caddy 还禁止别的页面用 iframe 嵌入本应用（`frame-ancestors 'none'`），所以诱骗来的点击
无法通过被嵌入的页面执行操作。

## 连接 Monzo

Monzo 是一个连接器：`worker` 服务每 30 分钟（以及点击「立即同步」或收到 webhook 时）把它同步
进账本。它只读取数据。

1. 在 `infra/.env` 里把 `LARARIUM_PUBLIC_BASE_URL` 设为你打开 Lararium 用的地址（即
   `tailscale serve` 的地址，末尾不带斜杠），然后用 `make up` 重启。没设置它时，API 会拒绝
   创建连接。
2. 在 [developers.monzo.com](https://developers.monzo.com) 创建一个 OAuth 客户端：类型选
   confidential，重定向 URI 填
   `<LARARIUM_PUBLIC_BASE_URL>/api/v1/connections/oauth/monzo/callback`（添加过连接后，连接页面会显示确切的值）。
   记下它的 client ID 和 client secret。
3. 在 Lararium 打开「连接」页面，用 client ID 和 secret 添加一个 Monzo 连接，然后点「授权」。
   浏览器会打开 Monzo，用邮件里的登录链接登录。
4. **在 4 分钟内批准。** Monzo 会要求你在 Monzo 应用里批准这次新的访问。只有批准后的头 5 分钟，
   Monzo 才会返回你的全部历史，所以 Lararium 最多会轮询 4 分钟等你批准，然后一次性回填全部历史。
   如果没有及时批准，或回填没有完成，连接上会提示；重新授权即可重试（在那个时间窗口之外只能拿到
   最近 90 天）。
5. 之后连接会自动同步。worker 最多同时跑 4 个任务，所以一个连接的批准等待不会拖住其他连接；
   同一个连接的任务不会重叠。

会同步的内容：你的账户（联名账户是单独的账户）、作为所属账户子账户的每个 pot、交易（被拒绝的会
跳过；待处理的交易会在结算或被撤销时更新）以及余额快照。刷卡付款会匹配到商户；pot 转账变成账户
与 pot 之间的转账。

账本里会出现两个系统账户：

- **Unassigned（未分配）**：已同步交易里你还没指定分类的那一侧（复式账本里每笔 Monzo 付款都有
  两侧）。
- **Opening balances（期初余额）**：期初调整的另一侧，它让账户余额从 Monzo 报告的数字开始，这样
  同步窗口之前的历史也能对得上。

凭据（client secret 和 OAuth token）在存储前用 `LARARIUM_MASTER_KEY` 加密。**主密钥要和数据库
转储分开备份**（转储加密钥才能完整恢复；只有转储读不出凭据）。密钥一旦丢失，所有已存的凭据都无法
读取，连接也没法原地修复：要把每个连接带着它的 client secret 重新添加一遍，再授权。然后停用旧连接，
并关闭它的账户和余池，让它们不再计入净资产。网页端暂时不能关闭账户，所以要在 tailnet 里的设备上调用
API：用 `GET /api/v1/finance/accounts` 列出账户，再对每个账户调用
`POST /api/v1/finance/accounts/<id>/close`，请求体是 `{"closed_on": "<今天>"}`。主密钥用
`openssl rand -base64 32` 生成一次；不要通过修改 `.env` 来轮换它。

### 用真实 Monzo 账户验证

测试用的是按 Monzo 文档写出的合成响应。在依赖这个连接器之前，请用你自己的账户核对（这里的任何
操作都不会改动 Monzo 里的东西）：

1. 创建并授权连接，在 4 分钟内批准。
2. 确认账户、pot 和期初调整都出现了，并且 Lararium 里 Monzo 账户的余额等于 Monzo 应用里的余额。
3. 刷一笔小额卡付款，确认它约一分钟内到达（需要先按下文配置 webhook；否则 30 分钟内，或点
   「立即同步」）。
4. 每个接口保存一份响应（accounts、balance、pots、带 `expand[]=merchant` 的 transactions、
   webhook 注册）到 `private/monzo-samples/`，该目录被 git 忽略。如果要分享，请先去掉姓名和账号。
5. 与 `backend/tests/finance/monzo/` 里的夹具对比：
   - 转账和他人付款上的 **counterparty（对手方）** 字段；
   - 往 pot 里转入或转出的交易上的 **`metadata.pot_id`**；
   - **401 响应体的形态**：带的是 `code` 还是 `error`（客户端两种都读）；
   - **被撤销的待处理付款** 长什么样（同一个交易 id 带上 `decline_reason`、`amount` 或 `settled`
     发生变化，还是交易完全不再出现）；
   - Monzo 在你于应用里批准之前是否确实返回 **403**，客户端是按这个假设写的。
6. 与现实不符的地方，用合成数据修改夹具和 normalizer，绝不要用真实响应。

已知缺口：待处理的付款会一直被重新获取，直到它结算或作废；但如果 Monzo 完全不再列出它，它就会一直
保持待处理，连接上的对账不符警告就是信号。另外，同步需要的历史如果早于 Monzo 在批准窗口之外提供的
89 天（比如长时间停机之后），就拿不到了：连接上会显示"同步窗口受限"的警告。要回填它，先停用连接，再重新授权，并在 4 分钟内批准（正常状态的
连接不能直接授权）。

## 自己账户之间的转账

钱在你自己的两个账户之间转动时，Lararium 会同时改变两边的余额，净资产既不会下跌，也不会把这笔钱
算两遍，钱在路上的时候也一样。它靠对方的 sort code 和账号识别转账（名字的写法变化太多，不能用来
识别），所以它需要知道哪些银行账户是你的。下面的例子用一个 Monzo 连接和一个叫「Bank A」的手动
账户。导入的行如果只印出对方的账号（没有 sort code），只要你登记的账户里恰好有一个是这个账号，
也能据此识别。

### 手动账户：选 hybrid，并录入期初快照

Bank A 没有连接，报告不了它那一侧的转账。Lararium 一看到 Monzo 那一侧，就在 Bank A 上记一笔
*推断*（inferred）交易来代替。要让它改变 Bank A 的余额：

1. 在 Accounts 页面创建账户时选 **Balance mode: Hybrid**（ledger type 选 Asset，subtype 选
   Bank）。
2. 保存一个期初快照：按 Bank A 的对账单，填某个日期的 GBP 余额。

hybrid 账户的余额是最近一次快照加上快照之后记入的所有分录。快照日期之前的转账已经包含在快照里，
不会算两遍。日期在该账户这种货币的第一个快照当时或之前的转账，根本不会生成推断交易，而是对 Opening
balances 结清，所以快照之前的历史也不会变。之后再录入一个更早的快照，它之后的转账会回到这个账户上。手动账户不要用 Transactions 模式，除非你会导入它的账单（见下文“导入账单”）：没有账单，它的余额就只是 Lararium 看到的那些转账。
Snapshots 账户忽略分录，所以不能登记标识；它的 Identifiers 区域会改为提供 **Switch to hybrid**，
切换后估值和原来完全一样（最近一次快照），再加上之后记入的分录。

### 登记标识

- **手动登记**：在 Accounts 页面展开账户的 **Identifiers**，填入它的 sort code 和账号（可以带短横线
  和空格）。Lararium 随后会检查全部历史：凡是提到这个账户的过往交易都变成转账，Bank A 也会得到相应
  的推断交易。**Remove** 会撤销这些。
- **从 Suggestions 登记**：Transfers 页面列出你的交易里出现过、但还没登记的每个银行账户，附上它出现
  时用过的名字、转出和转入的笔数，以及最近日期。选出它是你的哪个账户，点 **Register**（和手动登记
  一样）；不是你的就点 **Dismiss**。Snapshots 模式的账户不会出现在选项里；这一栏会列出它们的名字，
  在 Accounts 页面把它切换成 hybrid 后就会出现。
- **Monzo 会登记它自己的账户**：每次同步都会，所以两个 Monzo 连接之间的转账（比如联名账户）不需要你
  做任何事。它从不拿走你登记在别的账户上的标识；如果你把它自己账户的标识 dismiss 了，它会收回。

不带任何银行账户信息的记录（比如券商的入金记录）也可以按金额和日期成为转账的另一侧，但前提是它的
数据源把它标为银行转账：同样金额的刷卡退款永远不会被配对。转账都是 GBP；Lararium 不记汇率或手续费
差额。

### 给储蓄账户设置主账户

如果一个储蓄账户本身没有可登记的银行账户（比如 Monzo 的 pot），可以给它设一个**主账户**：在
Accounts 页面用它的 **Main account** 下拉框选择（只限手动账户；连接账户的主账户由它的连接设置）。
主账户必须是你自己的、未关闭、本身没有主账户、也还不是别的账户的主账户。此后，这个储蓄账户里没有
写明对方银行账户的银行转账，就视为写明了它的主账户，于是按金额和日期与主账户那一侧配对，Lararium
会立即检查全部历史。选 **None** 即可撤销，历史会重新匹配。

### Transfers 页面

每笔转账一行，比如「Monzo → Bank A · 100.00 GBP」。

- **Needs attention**（需要处理）：Lararium 不会自行决定的转账。只有当两条记录互为对方唯一的候选时，
  它才会把它们配对，所以结果从不取决于哪个数据源先同步。
  - *Several candidates*（多个候选）：不止一条记录可能是另一侧（比如相隔几天、金额相同的两笔转账）。
    页面列出这些候选；对正确的那条点 **Pair**。完全相同的会自动配对：一笔转账拆成金额相同的几笔
    （同一天、同样两个账户之间、金额相同的两笔转出和两笔转入），按固定顺序配上，怎么配余额都一样。
    如果一侧的记录没有写明对方账户，就和单笔一样按金额和日期认：同一天别人转来的同额款项，可能在你
    自己那笔到账前被一起认进来，等你那笔到了，它们会转到这里让你处理。
  - *Amount differs*（金额不同）：两条记录互相指向对方的账户，但金额不同。它们不能配对；如果它不是
    转账，就 Reject。
  - *Overdue*（逾期）：另一个账户有自己的连接，但 7 天内还没报告它那一侧。

  在你决定之前什么都不会变：余额和净资产保持原样。
- **In transit**（在途）：转入或转出有自己连接的账户、正等待那一侧报告的转账，附上已经过去的天数。
  展开某一笔（或 Recent 里 inferred、before history 的一笔）的 **Candidates**（候选），可以看到哪些
  记录可能是它的另一侧。
- **Suggestions**（建议）：还没登记的银行账户（见上文）。
- **Recent**（最近，默认折叠）：已经处理好的转账，各带状态：
  - *matched*：两个账户各自的记录都在；
  - *inferred*：一条记录，加上手动账户上的推断交易；
  - *before history*：另一个账户自己的历史已经包含这笔转账，所以不会在它上面记任何分录：它的连接开始于
    这笔转账之后，或者转账在它的期初快照当时或之前，或者转账落在它已导入的账单完整列出、或由期初调整
    代表的日子里；
  - *rejected*：已拒绝。

各个按钮的作用（每个决定在重新同步后都会保留）：

- **Reject**：这不是你自己账户之间的转账。它的记录回到 Unassigned，推断交易被删除，之后的同步也会
  保持拒绝。
- **Undo**（在已拒绝的转账上）：撤回拒绝，记录重新参与匹配。
- **Unpair**（在已匹配的转账上）：这两条记录不是同一笔转账，但各自仍可能是转账。它们以后不会再被
  自动配对，各自单独重新匹配。如果点错了，两边的转账都会把对方列为 *Unpaired*（已取消配对）的候选；
  点 Pair 即可撤销。
- **Pair**（在金额相同的候选上，或 *Unpaired* 的候选上；金额不同的候选没有这个按钮）：这两条记录是
  同一笔转账。你的选择优先于其他任何候选。

### In transit 计入净资产

**In transit**（在途）是一个资产类的系统账户，所以计入净资产。转账的转出一侧记到它上面，转入一侧
（它自己的记录，或推断交易）再把钱转出去。钱在路上时就停在 In transit，所以净资产不会下跌；没有
在途的钱时，In transit 是 0。如果不是 0，Transfers 页面的 In transit 区域会列出在等什么。

### 逾期提醒

配置了[飞书通知](#飞书通知)后，worker 每 30 分钟检查一次，对每笔需要你处理、或在途超过 7 天的转账
发一条消息；同一次检查超过 3 笔时，改为只发一条消息说明有几笔。每笔转账只提醒一次。默认值（转入一侧
的记账日期可以在转出一侧之前 1 个自然日到之后 5 个自然日之间，按记账日期计算，所以周五的对账单记录能
和周三任何时刻的 Monzo 付款配上；7 天后算逾期）在设置 `finance.transfers` 里；目前还没有
页面可以修改它们。每一项都是 1 到 3650 之间的整天数，前后两个窗口加起来必须短于 7 天，这样每周
一次的转账不会和下一周的配上。

## 连接 Interactive Brokers

Interactive Brokers（IBKR）是一个连接器：`worker` 通过 IBKR 的 Flex Web Service 同步它，
该服务返回你事先定义好的一份 Flex query 报表。它只读取数据。拉取到的报表会作为文档保存在
`documents` 卷中，`worker` 和 `api` 一样挂载这个卷；升级后运行 `make up`，worker 才会挂上它。

### 1. 创建 Activity Flex Query

在 Client Portal 打开 Performance & Reports，再进入 Flex Queries，创建一个 **Activity Flex
Query**：

- 选择**所有账户**（只选一个账户的 query 会漏掉其他账户）。
- 格式选 **XML**，日期格式 `yyyyMMdd`，时间格式 `HH:mm:ss TimeZone`，分隔符 `;`。
- 把 **Include Currency Rates**、**Include Audit Trail Fields**、**Include Offsetting
  Trade/Cancel Pairs** 和 **Breakout by Day** 都设为 Yes。
- **勾选每一个 section，以及每个字段和选项。** Lararium 会把 IBKR 发来的每个字段都按类型存下来。
  个人信息字段（姓名、地址、邮箱、出生日期、受益人、客户经理）是可选的：勾选后同样会存储，但绝不
  出现在日志、警告或通知里。
- 时间段无关紧要：Lararium 会自己发送日期。

保存后记下 **query ID**。

### 2. 创建 token

在 Flex Web Service Configuration 中启用该服务并生成 token。有效期最长一年。IP 限制是可选的；
家里是动态 IP 时请留空。**记下到期日**：IBKR 没有查询到期日的 API，所以由你告诉 Lararium，
它会在到期前 14 天提醒你。生成新 token 会让旧 token 失效。

### 3. 添加连接

在 Connections 页面选择 **Add Interactive Brokers**，填写显示名称、token、到期日和 query ID。
每个 IBKR 连接都会显示 token 到期日（14 天内高亮），并有 **Update token**（填新 token 和到期日；
query ID 留空表示沿用当前的）和 **Upload a Flex statement**（见下文）。

worker 每天伦敦时间 06:00 之后同步，点击 **Sync now** 时也会同步。首次同步最多拉取一年，再往前
拉取到账户开立为止（以 IBKR 提供的为限）；**Sync now** 遵循同样的规则。每个 IBKR 账户在
Lararium 中对应一个账户，税务账户类型（ISA 或 GIA）按 IBKR 的账户类型自动设置。

### 与 Monzo 之间的转账

在 IBKR 账户上登记 IBKR 的收款银行账户（Monzo 显示的 sort code 和账号），做法见
[登记标识](#登记标识)，这样 Monzo 的付款就能与 IBKR 的入金配对。如果 IBKR 从另一个账户付款给你，
它会出现在 Suggestions 里。你自己的 IBKR 账户之间的转账会自动配对。

### 对账警告

每次同步或上传后，Lararium 会按每个账户 IBKR 最近一次报告的那一天，把自己的记录与 IBKR 报告的
持仓和现金对比。

- **“IBKR ···1234 SYMBOL differs from IBKR's report on DATE by AMOUNT”** 表示 Lararium 的记录
  与 IBKR 报告的数字对不上：可能是某种记录类型还没处理，或是 IBKR 做了更正。Lararium 绝不会
  悄悄自动修正。某次同步里更早一天的差异只会发通知。
- **同步失败** 时，连接上会显示原因，比如需要在 Flex query 里勾选的某个字段。
- **“IBKR flagged: …”** 指出了 Lararium 还没有给类型的某个 section 或字段。

### 已关闭的账户

Flex Web Service 无法选择已关闭的账户（比如关掉的 ISA）。请手动获取它的报表：

1. 在 Client Portal 打开账户选择器的筛选，选 Account Status，再选 Closed，并选中所有账户。
2. 运行 Flex query：Run，格式选 XML，时间段覆盖该已关闭账户的存续期。IBKR 可能在后台生成
   （Batch Flex Queries）；生成后下载。
3. 等该连接的首次同步完成后，在 Connections 页面的 IBKR 连接上选择 **Upload a Flex statement**，
   上传这个 XML（最大 64 MiB）。请在最近一次同步之后再运行导出：旧文件里某行若已被 IBKR 更正，上传会把更正覆盖回去。

## 通过 Cloudflare Tunnel 接收 Webhook

不做这一步，每 30 分钟轮询一次也能用。想在大约一分钟内看到付款，就需要给 Monzo 一个它能调用的公网
URL。只有一个很小的接收器通过 Cloudflare Tunnel 暴露出去，服务器上不需要开放任何端口：

1. 在 Cloudflare Zero Trust 创建一个 tunnel，把它的 token 填进 `CLOUDFLARE_TUNNEL_TOKEN`。添加
   一个公共主机名（例如 `hooks.example.com`），服务填 `http://webhook-in:8000`，并把
   `LARARIUM_WEBHOOK_BASE_URL` 设为 `https://hooks.example.com`。
2. 生成路径密钥填进 `LARARIUM_WEBHOOK_SECRET`：至少 32 个字符，只能用字母、数字和 `. _ ~ -`
   （用 `openssl rand -hex 32` 生成的十六进制最简单）。不符合要求的，接收器会拒绝启动。Monzo 的
   推送没有签名，所以 URL 里的这个密钥就是区分你的推送和别人请求的依据。
3. 在该域名的 WAF 里添加一条自定义规则，动作选 Block，表达式如下，这样除了 Monzo 的
   `POST /monzo/...` 之外，什么都进不了 tunnel：

   ```
   not (http.request.method eq "POST" and starts_with(http.request.uri.path, "/monzo/"))
   ```

4. 在 `infra/.env` 里设置 `COMPOSE_PROFILES=webhook`，然后运行 `make up`。有了这个 profile，接收器
   和 tunnel 就成了整套服务的一部分：`make up` 会和其他服务一起构建、启动它们（`worker` 和 `api`
   也需要这三个变量）。要关掉 webhook：清空这三个变量和 `COMPOSE_PROFILES`，然后先运行
   `make down`，再运行 `make up`；只运行 `make up` 的话，接收器和 tunnel 会继续运行。

   之后每次同步都会把 `https://hooks.example.com/monzo/<密钥>` 注册给 Monzo。接收器存下每次推送并
   立即应答；worker 在一分钟内把它变成一次同步。密钥错误会得到 404，和其他任何路径一样。

接收器运行 uvicorn 时不开访问日志（密钥在 URL 路径里）；请保持这样，也不要把密钥放进 Cloudflare
的日志或仪表盘。

**已有的数据库。** `infra/postgres/roles.sql` 只在全新的卷上执行，所以在 webhook 接收器出现之前
创建的数据库没有 `lararium_webhook_in` 角色，迁移 0011 也跳过了它的授权。在设置
`COMPOSE_PROFILES=webhook` 之前执行一次：在 `infra/.env` 里设置 `LARARIUM_WEBHOOK_IN_PASSWORD`，运行
`make up`（这会带着该变量重建 `db`，数据保留），然后：

```bash
docker compose -f infra/compose.yaml --env-file infra/.env exec -T db \
  psql -v ON_ERROR_STOP=1 -U postgres -d lararium <<'SQL'
\getenv webhook_in_pw LARARIUM_WEBHOOK_IN_PASSWORD
CREATE ROLE lararium_webhook_in LOGIN PASSWORD :'webhook_in_pw';
GRANT USAGE ON SCHEMA core TO lararium_webhook_in;
GRANT INSERT ON core.webhook_inbox TO lararium_webhook_in;
SQL
```

## 飞书通知

连接需要重新授权、或同步持续失败时，Lararium 可以给你发消息。在飞书群里添加一个自定义机器人，
开启签名校验，把 webhook 地址和签名密钥分别填进 `infra/.env` 的 `LARARIUM_FEISHU_WEBHOOK_URL`
和 `LARARIUM_FEISHU_SECRET`（`make up` 会把它们传给 worker 和 api）。两者都不设置就不发通知。消息里
不会出现任何凭据。

## 导入账单

没有连接器同步的账户，可以上传银行导出的账单文件。在你审阅之前，什么都不会进入账本。

1. **上传。** 在 Imports 页面选择账户、一个 profile（怎样读这家银行的文件，见下文）和文件；文件
   本身不写明币种时，还可以选一个币种。文件最大 5 MiB、20,000 行。文件会作为文档保存（在
   `documents` 卷中），永不删除。
2. **审阅。** 批次页面把每一行标为 **new**（新的）、**duplicate**（账本里已有）、**skipped**
   （金额为零，或被 profile 的 `include_only` 排除；会显示原因）或 **error**（无法读取，或与
   滚动余额对不上）。只要有错误行就不能提交：修改 profile 或文件，丢弃这个批次后重新上传。行列表
   上方的警告会提示文件没有余额，或提交它会改变另一次导入的期初调整。
3. **提交。** 每个新行变成一笔交易；重复行和跳过的行永远不会。不想要一个暂存批次时，选择丢弃。
4. **回滚。** 回滚一个已提交的批次会作废它的交易（不删除任何东西），之后同一个文件可以再次导入。
   如果另一个已提交批次因为“已导入过”而跳过了这个批次里的行，就必须先回滚那个批次：错误信息会
   写明是哪个。

**哪些账户可以导入。** 只有开着的、手动的（不由连接同步）、余额模式为 `transactions` 或
`hybrid` 的账户。`snapshots` 账户的余额不看交易，因此导入会被拒绝，而不是悄悄不计入。

**每一行的另一侧。** 银行的一行只有一侧，账本却是复式的。另一条分录记到 **Unassigned（未分配）**
系统账户，和 Monzo 用的是同一个，它从不改变净资产。账单很少从账户开立的第一天开始，所以在
`transactions` 账户上，导入还会在账单第一行的前一天加一笔 **期初调整**（对方是
**Opening balances（期初余额）**），让导入后的余额等于账单上写的数字。如果之后才来一份更早期间的
账单，提交和回滚时会重新对齐这个账户所有导入的期初调整，期初余额永远不会被算两次；审阅页面会先
提醒你。后来的导入还需要调整，通常说明缺了某一期账单：丢弃它，先导入缺的那期。`hybrid` 账户不加
调整，因为它的快照已经锚定了余额。

**文件里的每个字段都会保存。** 每一行保存日期和起息日、文件给出的时间及其精度（`time_unit`：文件只给日期时为
`day`，这一行记在那天的零点；否则为 `minute` 或 `second`）、收款方和备注、支票号、银行的交易类型代码、
对方的名称、sort code、账号和付款参考，以及账单上印出时的更多字段（付款类型、银行参考号、用途代码、
汇款附言、对方银行、被遮住的对方账号的末几位；Revolut 还有付款的发起时间、所用的卡，以及外币付款的
原币金额、银行汇率和 ECB 汇率），各占一列，另外还保存整行原始数据。每份账单保存
账单期间、期初和期末余额、收入和支出合计、单独汇总时的已付利息、笔数、账单编号和日期、印出的账户
信息（类型、名称、sort code、账号、IBAN、BIC、客户号、分行、开户银行）、约定透支额度以及利率
（印出时还有利率适用的日期和付息频率）。文件写明的账户必须与之前导入到同一个账本账户的账户一致，
否则上传会被拒绝（提示信息只显示账号的最后四位）。

**导入与转账。** 提交账单时，账单写明的 sort code 和账号会登记到这个账户（与“登记标识”相同），
每一行也会经过转账匹配：如果某行是来自已连接账户的一笔转账的另一侧，它会取代这笔转账的占位交易，
而不会被算两次，Transfers 页面也像对待其他记录一样对待它。写明对方账户的行通过登记表匹配。没有写明
对方账户、也不是刷卡类型的行，会仅凭金额和日期被采用，除非这笔转账的时间窗口早于这个账户的导入
完整列出的第一天（没有写明账单期间的导出文件，第一天和最后一天可能不完整）：那之前，真正的那一行
可能在账户的期初里，永远不会被导入，不能让一笔金额相同但无关的款项顶替它。已导入的最后一天之后则会
采用：万一采用的不是真正那一行，下一份账单会带来真正的那一行，这笔转账就会带着两个候选进入
“需要处理”。在 Transactions 账户上，如果时间窗口完全落在账单完整列出、或由期初调整
代表的日子里，这笔转账会直接结清（*before history*），账户余额保持和账单一致。到账太晚、没能配对的
收款也是这样：那一行留在 Unassigned，这笔转账出现在 Recent 里，而不是“需要处理”。两次导入之间不要
漏掉账单：漏掉那个月里的转账，要等你补导那个月才会配对。
回滚批次时会先解除它各行的关联，占位交易随之恢复；这个账户对该银行账号的登记也会撤销，除非这个账户的
其他导入仍然写明了它（下次导入写明它时会重新登记）。

### 格式

在 Import profiles 页面，**Add from catalog** 会为下表每种银行格式创建一个现成的 profile；每家
银行请用推荐的格式，它包含的信息最多。

| 银行 | 格式 | 推荐 |
|---|---|---|
| HSBC UK | PDF 账单 | 是 |
| HSBC UK | midata CSV、OFX、CSV（交易列表，无表头行）、QIF | |
| Lloyds | CSV | 是 |
| Lloyds | PDF 账单、QIF | |
| BOC UK | PDF 账单 | 是（唯一格式） |
| Tembo | PDF 账单 | 是（唯一格式） |
| LemFi | PDF 账单（GBP 账户和储蓄账户） | 是 |
| LemFi | CSV（GBP 账户和储蓄账户） | |
| Revolut Current | PDF 账单 | 是 |
| Revolut Current | CSV | |
| Revolut Savings | PDF 账单 | 是 |
| Revolut Savings | CSV | |

LemFi 的 PDF 包含其 CSV 的全部内容（相同的行，以及每行的时间和付款详情），还多出：账单期间、期初和
期末余额、收入和支出合计、储蓄账户的利息、账户的 IBAN、sort code 和开户银行，以及储蓄利率。没有任何
交易的期间的文件（LemFi 会为空月份导出这样的文件）会作为空文件被拒绝：没有可导入的内容。

Revolut 的两个账户导出格式不同，所以各有自己的条目。活期账户的 CSV 比 PDF 多的只有交易类型、发起和
完成时间以及手续费；PDF 多出账户信息（sort code、账号、IBAN、BIC）、余额汇总，以及每笔付款的参考、
对方、所用的卡和外币付款的原币金额与汇率。储蓄账户的 PDF 包含 CSV 的全部行，另有账户信息、产品名和
每类交易的合计。

PDF 账单由为该银行编写的版式读取，它会自我校验：汇总（期初和期末余额、收入和支出、笔数）必须等于
各行，每个印出的余额都必须能由金额推出，每一行都必须恰好有一个金额、一个落在账单期间内的日期和一个交易类型代码（银行印出类型代码时；LemFi
不印），表格里的每个词都必须属于某一行。Revolut 活期账户的账单不印交易类型代码：刷卡付款靠所用的卡来识别；
汇总不计入的待处理（pending）和已撤销（reverted）付款作为跳过的行保留。一份 Revolut 账单只能有一个
币种有金额，两个币种都有的会被拒绝。在账单期间最后一天当天或之前生成的账单（Revolut 账单的期间截止到
你下载的那天时就是这样），和导出文件一样只部分列出那一天，它的期末余额是生成那一刻的余额，保存时不带日期；要完整
列出，Revolut 账单请截止到已经过完的一天。Tembo 不印期初余额和收支合计：它的 Plan value 必须等于各行最后的余额，每笔金额的方向由交易类型决定，页码和期间（第一行到最后一行）不能有遗漏。Tembo 账单是按需生成的，所以和导出文件一样，只有首末两天之间的日子算作完整列出。LemFi 的收入和支出合计可能各比各行之和少一便士（LemFi 把它们
向下截到便士）；利息和期末余额仍必须由各行精确推出。LemFi 账单还必须为所有页编号（"Page n of N"），
缺页的账单会被拒绝。对不上的账单会被整体拒绝，
并写明哪项校验失败，而不是带着错误的数字导入。同一个账户可以用不同格式导入重叠的日期，例如过去用
PDF 账单、最近几周用 OFX 导出，先后顺序不限：落在之前导入已覆盖日期上的行，会与那次导入的行核对。

### Profile

Profile 是 JSON，保存时由服务器校验。`format` 为 `csv`、`ofx`、`qif` 或 `pdf`；每种格式都可以带
可选的 `timezone`（IANA 时区名，例如 `Europe/London`；默认是 Lararium 的时区），用它把日期换算成
时间，但不会改动文件里写的日期；以及 `card_types`：银行表示刷卡、取现和支票的交易类型代码（目录里的
profile 已经设好，例如 HSBC 的 `VIS` 和 `)))`、Lloyds 的 `DEB`；OFX 默认是 `POS`、`ATM` 和
`CHECK`）。这些类型的行永远不会仅凭金额和日期被当作一笔转账的另一侧（见下文）。

- **`csv`**：`columns`（见下）、`date_format`（`strptime` 格式，例如 `%d/%m/%Y`；带时间的格式如
  `%d %b, %Y %H:%M` 会保留当天的时间）、`encoding`（默认 `utf-8-sig`，或 `utf-8`、`cp1252`、
  `latin-1`）、`delimiter`（`,`）、`skip_rows`（表头前的行数，0–50）、`header`（无表头行的文件
  按顺序给出的列名）、`skip_footer_rows`（0–20）和 `overdraft_footer_label`（写有约定透支额度的
  页脚行的第一格）、`include_only`（`{"列名": ["值", ...]}`：其他值的行会被跳过，例如待入账的
  卡付款）、`row_order`（`oldest_first` 或 `newest_first`）、`sign`（见下）、`amount_format`
  （`decimal_separator`，`.` 或 `,`；`thousands_separator`，默认无，或 `,` `.` 空格 `'`）和
  `currency`（文件只写一次而不逐行写币种时，每一行的币种，例如 LemFi 的 `Money in (GBP)`；否则用上传时
  选的币种，或映射的 `currency` 列）。
  重复表头的行不算一行交易。
- **`columns`** 给出文件里的列名：`date`、`description`（列表；多列用一个空格连接），然后是
  `amount`，或同时给出 `inflow` 和 `outflow`；可选的有 `balance`、`currency`、`external_id`、
  `fee`（在金额之外另收的费用）、`type`、`value_date`、`payee`、`memo`、`check_number`、
  `counterparty_name`、`counterparty_sort_code`、`counterparty_account_number`、`reference`
  （付款参考）、`started_at`（付款的发起时间，按 `date_format` 解析，这时它必须带时间；`date` 是
  完成时间），以及账单自身账户的 `account_sort_code`、`account_number` 和 `account_type`（产品，
  例如 Revolut 的 `Product`；每一行都必须相同）。
- **`extract`** 从一列里读出多个类型化字段：`[{"column": "Details", "patterns": ["<正则>", ...]}]`。
  第一个完整匹配非空单元格的正则，用其命名分组填写同名字段（`payee`、`memo`、`check_number`、
  `counterparty_name`、`counterparty_sort_code`、`counterparty_account_number`、
  `counterparty_account_ending`、`counterparty_bank`、`reference`）；没有任何正则匹配的单元格会让
  该行成为错误行。一个字段只能有一个来源：映射的列或某一个 extract。
- **`ofx`**：`sign`。
- **`qif`**：`date_format`、`encoding`、`amount_format`（默认以 `,` 作千位分隔符）和 `sign`。
- **`pdf`**：`layout`：`hsbc_uk_statement`、`lloyds_statement`、`boc_uk_statement`、
  `revolut_current_statement`、`revolut_savings_statement`、`tembo_statement` 或 `lemfi_statement`。

**正负号。** Profile 用 **inflow**（流入：账户余额增加）和 **outflow**（流出）来表述。只有一个
`amount` 列时，`sign` 说明它的方向：`inflow_positive`（默认）或 `outflow_positive`；有 `inflow`
和 `outflow` 两列时，金额是流入减流出。在信用卡（负债）上，一笔消费是流出，让余额更负；向卡还款
是流入，让余额回到零附近。所以把消费列成正数的信用卡导出需要 `"sign": "outflow_positive"`。

**重复识别。** 当同一账户里一条已提交的行有相同的键时，这一行就是重复的：有外部 ID（OFX 的
`FITID`，或映射的 `external_id` 列）时用它；否则用日期、金额、币种、描述和滚动余额（只有这些都相同的几行，
在文件给出时间时再加上时间：一天里两次转入又转出，余额可能相同）；再否则用不含余额的这些字段。后两种还会
加上同一文件里排在它前面的完全相同行的数量，这样同一天两笔真实的相同消费都会导入，文件再次导入时两笔都算
重复；带余额的行只在不含时间的文件里这样计数（文件给出时间时，连时间都相同的行是错误）。滚动余额也可能重复：
同一天余额回到同一个数（同一笔转账转入两次，中间有一笔付款）。这种计数在不同文件之间是一致的：银行的文件不会从
一天的中途开始，只可能在一天的中途结束（当天导出的文件）。每份文件都从那天的第一行列起，所以第 n 个这样的行在
每份文件里都是同一行。
不同格式的文件键不相同，所以在之前导入已覆盖的日期上，改为按日期和金额
与那次导入的行配对：配上的是重复行，其余的是错误行。如果文件缺少之前导入在这些日期上的某一行，
上传会被拒绝；若新文件是对的，先回滚之前那次导入。

**示例文件。** `backend/src/lararium/modules/finance/importing/examples/` 里有虚构的 Example Bank
文件（`example-bank.csv`、`.ofx`、`.qif`），以及前两者的 profile（`example-bank-csv.json`、
`example-bank-ofx.json`）。把 profile 的 JSON 粘贴进一个新 profile，上传时选 GBP，导入到一个手动的
`transactions` 账户，就能试一遍整个流程。

### 用真实账单验证

测试用的是合成文件。要确认你自己的账单仍能正确导入（例如改了导入代码之后），把每家银行的文件
各放进一个文件夹，文件夹名以 `HSBC`、`Lloy`（Lloyds）、`BOC`、`Revolut`、`Tembo` 或 `LemF`（LemFi）开头，
每个文件夹一个账户（LemFi 的 GBP 账户和储蓄账户各一个；Revolut 可以叫 `Revolut current` 和
`Revolut savings`：文件本身会说明文件夹里是哪个账户），都放在同一个目录下，然后运行：

```bash
make real-check REAL_ROOT="$HOME/Downloads"
```

它把每个文件导入 `make check` 所用的同一个临时 PostgreSQL 容器，按两种顺序各一遍：先导账单，再先导
导出文件。每一遍里，每个文件都必须入账且没有错误行，每份账单的期末余额都必须在它自己的日期上对得上；
再从新到旧全部回滚，必须都被允许，并且余额回到零。它只打印计数和通过与否：错误信息里的数字和文件名
都会打码。导入时保存的副本放在一个临时目录里，结束时删除，容器也随之删除。Docker 在哪里运行（比如
远程 Docker 主机），文件内容就会在运行期间发送到那里。`make check` 和 CI 都会跳过它。

## 密钥：用 SOPS + age 加密 `infra/.env`

`infra/.env` 保存所有凭据，已被 git 忽略。如需保存一份加密副本（例如和备份放在一起），
可使用 [SOPS](https://github.com/getsops/sops) 和 [age](https://github.com/FiloSottile/age)：

```bash
# 仅一次：生成密钥对。keys.txt 要妥善保管，且与加密文件分开存放。
mkdir -p ~/.config/sops/age
age-keygen -o ~/.config/sops/age/keys.txt   # 会打印公钥：age1...

# 加密
sops encrypt --age age1... --input-type dotenv --output-type dotenv \
  infra/.env > infra/.env.sops

# 解密（例如在新服务器上）
SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt \
  sops decrypt --input-type dotenv --output-type dotenv infra/.env.sops > infra/.env
chmod 600 infra/.env
```

`infra/.env.sops` 同样被 git 忽略；不要强行把它加进公开仓库。

## 备份与恢复

```bash
make backup   # 或：infra/backup/backup.sh
```

它会写出 `$BACKUP_DIR/lararium-<UTC 时间戳>.dump`（`pg_dump` 自定义格式；`BACKUP_DIR` 为相对
路径时相对于仓库根目录），并以同一时间戳写出 `documents-<UTC 时间戳>.tar`：经由 `api` 容器读出的
`lararium_documents` 卷里的账单文件（上传的，或从 IBKR 获取的）。整套服务必须在运行：没有 `api` 容器时备份会失败，而不是留下
一个缺少文档的转储。如果 `infra/.env` 里设置了 `RESTIC_REPOSITORY`，随后还会用
`RESTIC_PASSWORD` 执行 `restic backup "$BACKUP_DIR"`；主机上需要装有 `restic`。
如需每晚执行，可添加一条 cron，例如 `0 3 * * * cd /path/to/lararium && make backup`。

把某个备份恢复到一个新数据库（脚本拒绝覆盖已存在的数据库）：

```bash
infra/backup/restore.sh backups/lararium-20260101T030000Z.dump lararium_restore
docker compose -f infra/compose.yaml --env-file infra/.env exec -T db \
  psql -U postgres -d lararium_restore -tAc "select count(*) from finance.instruments"
```

要让恢复出的副本生效：停止所有连接数据库的服务（除 `db` 以外的全部，包括所有 profile 里的，
这样在已关掉的 profile 下启动过的接收器也会停下），交换数据库名，再重新启动
整套服务。有人连着数据库时，PostgreSQL 不允许给它改名。

```bash
docker compose -f infra/compose.yaml --env-file infra/.env --profile '*' stop
docker compose -f infra/compose.yaml --env-file infra/.env start db
docker compose -f infra/compose.yaml --env-file infra/.env exec -T db psql -U postgres \
  -c "ALTER DATABASE lararium RENAME TO lararium_old" \
  -c "ALTER DATABASE lararium_restore RENAME TO lararium"
make up
```

把文档恢复到 `lararium_documents` 卷（转储里的行按哈希引用它们；卷里已有的文件会被相同的副本覆盖）：

```bash
docker run --rm -v lararium_documents:/d -i alpine tar -C /d -xf - < backups/documents-20260101T030000Z.tar
```

在全新服务器上，先把 `infra/.env` 里的 `LARARIUM_MASTER_KEY` 设成转储来源那台服务器的主密钥（从你单独
备份的密钥或 `infra/.env.sops` 里取），不要新生成：转储里存的凭据是用它加密的，换了密钥，每个连接都得
重新添加。然后运行 `make up`（它会创建角色和一个已迁移的空数据库），再按上面的步骤恢复并交换。

## 许可证

[AGPL-3.0-or-later](LICENSE)。
