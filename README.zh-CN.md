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
依赖许可证检查、OpenAPI/客户端漂移检查、前端类型检查与测试）。它严格按 `backend/uv.lock` 和
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
与 pot 之间的转账。已删除的 pot（包括连接之前就已删除的）会作为已关闭的账户保留，钱放在里面的那段
时间仍计入净值。

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
也能据此识别。只印出 sort code 的行是较弱的证据：只有当你登记的账户里恰好有一个是这个 sort code、
那个账户上的对应行没有写明任何银行账户、且两者互为对方唯一的候选时，才会配对；它自己永远不会单独
形成一笔转账。

### 手动账户：选 hybrid，并录入期初快照

Bank A 没有连接，报告不了它那一侧的转账。Lararium 一看到 Monzo 那一侧，就在 Bank A 上记一笔
*推断*（inferred）交易来代替，除非 Bank A 上有一行同金额的记录可能就是那一侧（见下文 *Needs attention*）。要让它改变 Bank A 的余额：

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

如果一个储蓄账户本身没有可登记的银行账户（比如你从账单导入的 Revolut 或 LemFi 储蓄账户），可以给它设一个**主账户**：在
Accounts 页面用它的 **Main account** 下拉框选择（只限手动账户；连接账户的主账户由它的连接设置）。
主账户必须是你自己的、未关闭的账户（不能是账户自己，也不能是账本自己维护的账户），且本身没有主账户；
和能登记标识的账户一样，它必须是资产或负债账户，且不能只记快照（snapshots），这样转账在它那一侧才算数。
一个主账户可以有多个储蓄账户，而已经是别的账户的主账户的账户，自己不能再设主账户。此后，这个储蓄账户里没有
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
    自己那笔到账前被一起认进来，等你那笔到了，它们会转到这里让你处理。手动账户上不会被自动采用的行
    （见 *Not listed in full*）和其他候选一样是候选，所以其中有这样一行的完全相同的几笔也会留在这里。
    在手动账户上，候选旁边不会记推断交易：万一其中一行就是真正的那一行，推断交易会让这笔钱算两次；
    在你配对之前，净资产会把这笔金额计在 In transit 账户里。
  - *Amount differs*（金额不同）：两条记录互相指向对方的账户，但金额不同。页面上没有 **Pair**：
    两条都是资金记录时，通过 API 配对（`POST /api/v1/finance/transfers/{id}/pair`，请求体
    `{"transaction_id": "<另一条>"}`；见下文“没有写明对方账户的记录”），否则如果它不是转账，就 Reject。
    手动账户上按 *Not listed in full* 那样不会被自动采用的同额行也会列出，带 **Pair**，旁边不会记推断交易。
  - *Not listed in full*（未完整列出）：这笔转账唯一的候选是手动账户上的一行，它落在转账窗口里、早于这个账户的
    上传完整列出的第一天，所以不会被自动采用（见下文“导入与转账”）。它旁边不会记推断交易：万一这行
    就是真正的那一行，推断交易会让这笔钱算两次。这个账户的余额是对的；在这项审核配对之前，净资产会把这笔金额计在
    In transit 账户里。上传一份列出那几天的账单，如果两条互为对方唯一的候选，
    就会自动配对（如果另一笔同金额的转账也在争这一行，两者都进入 *Several candidates*）；也可以自己配对：那一行会作为候选列出，带 **Pair**。已在 before history 结清的转账不会因此进入审核。等待手动账户的转账永远不会 *Overdue*：它等的是一次
    上传，而不是某个连接。
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
  点 Pair 即可撤销。只凭 sort code 配上的一对没有 Unpair，只有 **Reject**（**Undo** 可以恢复配对）：
  它的两条记录离开对方都不算转账，一旦取消配对，两边都不会再把对方列为候选。如果只是配错了对象
  （其中一条记录应该和另一条配对），就通过 API 取消配对（`POST /api/v1/finance/transfers/{id}/unpair`），
  再按下面“没有写明对方账户的记录”所说，把它和正确的那条配对：Reject 会把两条都标成不是转账，而被拒绝的
  记录不能配对。
- **Pair**（在金额相同的候选上，或 *Unpaired* 的候选上；金额或币种不同的候选在页面上没有这个按钮，
  改用 API 配对）：这两条记录是同一笔转账。你的选择优先于其他任何候选。候选按它自己的币种显示。

**没有写明对方账户的记录。** 两条记录都没有写明对方账户时（比如银行流水只印着你的名字和一个
参考号），不会自动生成转账。先在手动账户上手动录入另一侧，再通过 API 把这两条记录配对：
`GET /api/v1/finance/transactions/{id}/pair-candidates` 列出可能是它另一侧的记录：先是另一个账户上
金额相反且相等的；对资金记录，再是 7 天内币种或金额不同的资金记录，标为 `differs`（`currency` 或
`amount`）；每部分日期最近的在前。`POST /api/v1/finance/transactions/{id}/pair`，请求体
`{"transaction_id": "<另一条>"}`，把它们配成一笔 matched 转账。手动录入的那一侧必须是恰好两条分录的交易（该账户和 Unassigned），
否则配对会被拒绝。点 Unpair 即可撤销；撤销后，两条没有写明对方账户的记录不再有转账，要恢复只能
再通过同一个接口把它们重新配对（另一条记录仍会出现在候选列表里）。

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

## Overview（概览）

Overview 显示你选的日期范围（预设，或今天及以前的任意两天）：净值及其变化，收入、支出和结余，
按日、周或月的收支柱状图，按分类的支出，钱在哪些账户里，钱花到了哪里（按收款方），以及最后一天的
持仓。全部以报告货币计。

**什么算收入和支出。** 进出你账户的钱：交易记到收入、支出或权益账户（包括 Unassigned）上的
部分，期初余额（Opening balances）除外。自己账户之间的转账不算，买卖证券不算，凭空到账的证券
（公司行动）也不算。银行标记为不计入支出的（Monzo 的 `include_in_spending`）不算支出。分类就是
来源给出的分类；没有分类的记为 Uncategorised。当天没有汇率的外币付款不计入，页面会提示有几笔。

**隐藏账户。** 在 Accounts 页面，资产或负债账户上的眼睛图标可以把它从 Overview 中隐藏；账本和
Accounts 页面都不受影响。隐藏的账户被当作别人的账户：它的余额不计入净值和持仓，In transit 里
正在转入或转出它的钱也不计入。它和仍显示的账户之间转移的钱（一笔转账，或 Monzo 储蓄罐划转），
转出时算支出、转入时算收入，日期按显示一侧动账的那天，所以净值的变化和"收入 − 支出"能对上。
Overview 会按隐藏账户单独列出这些转移，不混入分类和收款方。

### 已知限制

- 收入和支出由不止一次查询读出。如果同步恰好在两次查询之间配对了一笔转账，那一次加载可能把这笔
  金额算两遍；重新加载页面即恢复正确。存储的数据不受影响。

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

## 连接 Trading 212

Trading 212 是一个连接器：`worker` 通过 Trading 212 的公开 API 每 30 分钟同步一次（点击
**Sync now** 时也会同步）。它只读取数据：只发送 GET 请求，而且只发给 Trading 212。一个连接对应一个
账户，所以 Invest 账户和 Stocks ISA 是两个连接，各用各的 key。

### 1. 创建 key

在 Trading 212 app 里打开 Settings，再进入 API (Beta)，为你要连接的那一个账户创建 key。

- 勾选读取权限。Lararium 需要 `account`、`portfolio`、`metadata`、`history:orders`、
  `history:dividends` 和 `history:transactions`；`orders:read`（挂单）和 `pies:read`（pies）
  勾选了就会用到，没勾选时跳过这部分，并在一条 “Trading 212 flagged: …” 警告里说明一次。
- **不要勾选 `orders:execute` 和 `pies:write`。** Lararium 只做读取，没有这两项的 key 即使泄露，
  也无法下单或修改 pie。
- 记下 API key 和 API secret。Trading 212 在创建 key 时就定死了它的权限，所以缺了必需的权限时，
  连接会要求换新 key：新建一个勾选了该权限的 key。

### 2. 添加连接

在 Accounts → Add account 里选择 **Trading 212 Invest** 或 **Trading 212 Stocks ISA**，填写显示
名称，粘贴 API key 和 API secret，点 **Add Trading 212**。Lararium 会先向 Trading 212
核验这个 key；如果它对应的账户已经连接过，会被拒绝。

这个连接会创建一个账户，名为 `Trading 212 Invest ···1234` 或 `Trading 212 ISA ···1234`（Trading 212
账户 ID 的后四位；可以改名），税务账户类型为 GIA 或 ISA。你的订单（每笔成交及其税费）、分红、入金、
出金、费用和利息都记进这个账户，每次同步还会记下所持每个标的的价格。首次同步读取全部历史；之后只读
新的部分，每周再完整读一遍。很长的历史分几次同步读完，每次同步每类历史最多 50 页。如果之后
Trading 212 拒绝了 key，连接上会提示：用 **Update keys** 换一对新的。已停用的连接也这样恢复：
**Update keys** 像添加时一样用 Trading 212 核验新的一对（必须是同一个账户），再把连接恢复为正常。

### CSV 导出（可选）

没有它一切照常。Trading 212 的 CSV 导出（在 app 的 History 下）有少数 API 不返回的细节，比如卡入金
写在 Notes 里的交易号。想补上这些，就在首次同步完成后，在该连接上选择 **Upload Trading 212 export
(CSV, optional)** 上传（最大 16 MiB）。文件必须是这个连接所对应账户的。

导出文件会完整保存，每一列都按类型存储，每一行挂到 API 里 ID 相同的记录上：它丰富那条记录，不会
再入账一次。API 还没列出的入金或出金会按导出文件入账。其他类型的行（交易、分红、利息）会保存并标记，
但不入账：这些由 API 入账，而它们的行与 API 记录如何对应还没见过实例。如果导出文件和 API 对金额、币种
或记账日期说法不一，Lararium 会开一个[待复核项](#待复核项)。

上传之前出现的现金与 Trading 212 报告不符的警告，会保留到下一次同步重新核对为止，即使这次上传已经
补上了缺的那条记录。

### 转账

- **从 Monzo 用卡或 Apple Pay 入金。** Monzo 把卡充值记为付给 Trading 212 商户的一笔刷卡消费，
  而不是银行转账。把这个商户登记到你的 Trading 212 账户上一次，此后每笔这样的付款都会与两分钟内
  金额相同的那笔 Trading 212 入金配对。目前只能通过 API 操作。Monzo 同步到一笔付给网站在
  `trading212.com` 上的商户的付款后，`GET /api/v1/finance/transfer-suggestions` 会列出它（kind 为
  `card_merchant`，带有它的 `merchant_id`，以及它能付入的 Trading 212 账户 `account_ids`）。用
  `POST /api/v1/finance/accounts/identifiers/card-merchants` 和
  `{"merchant_id": "<merchant_id>", "account_ids": ["<account id>"]}` 登记它，列出你用卡充值的每个
  Trading 212 账户（Invest 和 ISA 共用一个商户）。Lararium 随后会检查全部历史。如果它不是你的，用
  `POST /api/v1/finance/transfer-suggestions/card-merchants/dismiss` 和
  `{"merchant_id": "<merchant_id>"}` 把它去掉。
- **银行转账入金**在 Trading 212 一侧不带任何账户信息，所以当你银行那笔付款是唯一候选时，按金额和
  日期配对（见[登记标识](#登记标识)）。有两个候选都符合时（比如两个账户在相近的时间收到同样金额），
  这笔转账会出现在 Transfers 页面的 Needs attention 里：对正确的那个点 **Pair**。
- **多个账户共用的收款银行账户。** 如果你的银行用同一个银行账户（同一组 sort code 和账号）向
  Trading 212 Invest、Stocks ISA 和 Cash ISA 付款，这笔付款不会说明付给了哪一个：它的付款参考
  （reference）不固定，无法区分这几个账户。把这个银行账户登记一次，标明由它们共同持有，目前只能通过
  API 操作：`POST /api/v1/finance/accounts/identifiers/holders` 和 `{"kind": "uk_bank_account",
  "value": "<sort code> <account number>", "account_ids": ["<account id>", ...]}`（占位符；填你的
  银行 app 显示的内容，sort code 写成 11-22-33 或 112233 都行）。一次调用会替换它的全部持有账户，
  `account_ids` 为空列表则移除它们。一个银行账户要么登记到一个账户上（做法见[登记标识](#登记标识)），
  要么由多个账户共同持有，不能两者兼有；有持有账户时，它不会出现在建议里，也不能被忽略。Lararium
  随后会检查全部历史：付给该收款账户的银行转账，会与任一持有账户上、窗口内唯一一笔同金额、可能是
  银行转账的入金配对（标为银行转账的入金，或在 Trading 212 Invest、Stocks ISA 和 Cash ISA 上不指明
  任何人的入金；Trading 212 的分红、费用、利息和订单成交永远不算）。它绝不会与持有账户以外的账户上的记录配对，
  即使那条记录指明了你的银行账户。配对之前，这笔付款留在 Unassigned。无法配对时（两个持有账户上都有
  符合的入金，或唯一的那笔被覆盖检查拒绝：覆盖检查针对每个手动或导入的持有账户，因为真正的入金可能在
  其中任何一个上，藏在它的期初里），不会生成转账，
  也不会有任何提示：这笔付款只出现在 Unassigned，和未配对的刷卡付款一样；持有的收款账户也不会被当作
  历史之前已结清，不会生成占位记录。在 Unassigned 里找到这笔付款，手动配对正确的那一笔（先 `GET /api/v1/finance/transactions/{id}/pair-candidates`，再
  `POST /api/v1/finance/transactions/{id}/pair`，见上文“没有写明对方账户的记录”）。卡充值仍按上面的
  卡规则配对，且卡证据优先：已有刷卡付款候选的入金，不会按其账户的入金方式当作银行转账来配对。这样配上的
  一对在 Transfers 页面显示 **Reject**，没有 Unpair，和刷卡配对一样：两条记录少了对方都不是转账。有三个 Trading 212
  账户时，它们之间的转账不指明对手账户（见下文），所以转入的一侧可能被当作一笔付给共用收款账户、金额相同的付款的
  充值。真正的入金成为第二个合格候选后，Lararium 会自动拆开错配。原来配在一起的两条记录都会回到
  Unassigned（未分类），Transfers 页面不会生成审核项。请在 Unassigned 找到原付款，手动把它与真正的入金配对。
  如果真正的入金记在时间窗口之后，错误的配对会一直保留，直到你 Reject。
- **你的 Trading 212 账户之间。** 只有当你在 Lararium 里恰好有两个 Trading 212 系列的账户（不论
  状态；两个 Invest 账户也算两个）时，Trading 212 的 TRANSFER 才会指明另一个账户。有一个 Invest 和
  一个 Stocks ISA 时，两者互为对方的对手账户，转账自动配对。再添加了 Cash ISA 之后，转账不指明
  对手账户，需要手动配对：两侧都不指明任何账户，所以哪一侧都无法识别，不会自动配对。只有一个 Invest
  （或一个 Stocks ISA）加一个 Cash ISA 时，同样不指明对手账户：Cash ISA 没有可指明的
  Trading 212 id，这类转账也需要手动配对。
  Lararium 只统计添加到它里面的账户：你持有但没添加的 Cash ISA，
  仍按两个账户的规则处理，转到它的那笔会被当成转到另一个账户。新添加的同系列账户在每个
  Trading 212 连接的下一次同步时生效（届时会重放其转账）。

### Cash ISA（没有 API）

Trading 212 的 Cash ISA 没有 API。把它添加为一个单独的账户，再上传 app History 页面导出的 CSV：
在 Accounts → Add account 里选 **Trading 212 Cash ISA** 并在那里上传 History CSV（上传会自己使用现成的
profile）。请在表单的 **Opened on** 栏填上账户的开户日期：这样从那天开始的导出也能代表之前的
日子，开户当天的入金就能与银行那边的付款配对。每次导出必须从该账户的第一天开始，
或从之前某次上传已覆盖的某一天开始。导出文件请保留 Trading 212 给的文件名
（`from_<YYYY-MM-DD>_to_<YYYY-MM-DD>_….csv`）：它写明了这次导出的时段，时段内除最后一天外每天都
完整列出（导出可能在最后一天中途生成）。改过名的导出从首行那天起、到末行前一天为止每天都完整列出，上传时还会
警告文件名没有给出时段。利息行在其 `Action` 确定之前，按普通行导入。取款（Withdrawal）的 Notes 写明了
收款的银行账户（分行代码和账号），所以它会指明你在那里的账户，并与该账户的入账配对；存款（Deposit）的 Notes
写的是 Trading 212 的交易号，从另一个 Trading 212 账户转入的则 Notes 为空。其他形式的 Notes 会让该行报错，这样新的
形式不会被漏掉。

### 警告

- **“Trading 212 ISA ···1234 SYMBOL differs from Trading 212's report on DATE by AMOUNT”**：每次
  同步后，Lararium 都把每个标的的数量和现金与 Trading 212 报告的数字对比，绝不悄悄修正差异。
- **“Trading 212 flagged: …”** 指出 Lararium 还没处理的某个字段、取值或标的类型、某项没勾选的
  权限，或 Trading 212 不再提供的某一页历史。每条只通知一次。

## 连接 eToro

eToro 是一个连接器：`worker` 通过 eToro 的公开 API 每 30 分钟同步一次（点击 **Sync now** 时也会
同步），eToro 的账户报表则由你手动上传。它只读取数据：只发送 GET 请求，只发给 eToro，并拒绝不是
只读的 key。

### 1. 创建 key

在 eToro 打开 Settings，再进入 Trading，再进入 API Key Management，创建一个 Environment 为
**Real**、Permission 为 **Read** 的 key。记下 Public API Key 和 User Key。Lararium 在你添加它时以及
每次同步开始时都会读取 key 的 scopes：只要有一个不是读取 scope，这个 key 就会被拒绝（“This eToro
key can trade or move money. Create a key with Read permission.”），其他什么都不拉取。一个完全
没有 scope 的 key 也会被拒绝，提示是“This eToro key reports no permissions. Create a key with Read
permission.”。

### 2. 添加连接

在 Connections 页面的 **Add eToro** 里填写显示名称、Public API Key 和 User Key，点 **Add eToro**。
一个连接包含一个 eToro 用户的全部账户；同一用户的第二个连接会被拒绝。首次同步为每个账户分别创建
一个账户：

- **eToro Trading**（broker，GIA）：你的股票和 ETF、每次开仓和平仓，以及它们的价格。它的其他现金
  变动来自你的报表（第 3 步）。
- **eToro Money GBP**（bank；每个 eToro Money 币种账户一个）：它的刷卡消费、银行转账，以及与
  eToro Trading 之间的转账。
- **MoneyFarm ISA**（snapshots）：每次同步记下 eToro 报告的余额。eToro 的 API 不提供 MoneyFarm 的
  持仓或交易。

如果之后 eToro 拒绝了 key，连接上会提示：用 **Update keys** 换一对新的。已停用的连接也这样恢复：
**Update keys** 像添加时一样核验新的一对（只读、同一个 eToro 用户），再把连接恢复为正常。

### 3. 上传报表

eToro 的 API 提供交易，但不提供 Trading 账户上的其他现金变动：费用（隔夜费、SDRT、出金手续费）、
税、分红、利息、与 eToro Money 之间的转账，以及出金。这些只来自 eToro 的账户报表（account
statement）。没有报表，这些就缺失，Trading 的现金也对不上 eToro 的。

1. 在 eToro 网页版打开 Portfolio，再进入 History，再进入 Account Statement，选择时间段，下载为
   Excel（xlsx）。
2. 等该连接的首次同步完成后，在 eToro 连接上选择 **Upload eToro statement (xlsx)** 上传
   （最大 16 MiB，最多 100,000 个单元格：太大的报表会被拒绝，改为下载更短的时间段）。

- **第一份报表必须在你开户当天或更早开始**（报表里的 Date Created）。开始得更晚会被拒绝，并指明
  那一天，这样 API 和你的报表合起来才能覆盖账户的整个存续期。
- 报表必须属于这个连接：它的用户名和币种必须是该 eToro 用户的、该 Trading 账户的。
- 报表之间可以重叠：两份共有的行只写一次。在某一天中途结束的报表（在它的结束日当天下载的）不覆盖
  那天剩下的时间，所以下一份应从那天开始。
- 报表里的时间按 UTC 读取。

**“eToro Trading: upload an eToro statement covering YYYY-MM-DD onward”** 会显示在连接上并通知一次：
当 Trading 的现金与 eToro 的不一致，而原因很可能是只有报表才有的内容时，即最近一份报表之后的一笔
与 eToro Money 之间的转账，或某个持仓上的费用或分红。下载一份在那天或更早开始的报表并上传。

### 转账

- **eToro Money ↔ eToro Trading**（GBP ↔ USD）在两侧互相指明对方、且时间相差不超过两分钟时
  （eToro 用同一个时钟给两侧打时间戳）自动配对，并带上报表里的换汇手续费。在报表带来 Trading 一侧
  之前，eToro Money 一侧停在 In transit，净资产不会下跌。
- **Monzo ↔ eToro Money。** eToro 的 API 不提供 eToro Money 自己的 sort code 和账号。把它们（按
  eToro 显示的）登记到 eToro Money GBP 账户上，手动登记或在 Monzo 付过款后从 Suggestions 登记，做法
  见[登记标识](#登记标识)；之后 Monzo 的付款就会与 eToro Money 的配对。
- **币种或金额不同的记录**（比如从 eToro Trading 出金 USD，你的银行收到 GBP）永远不会自动配对。
  通过 API 配对，做法同 [Transfers 页面](#transfers-页面) 里“不带账户信息的记录”：
  `POST /api/v1/finance/transactions/{id}/pair` 加 `{"transaction_id": "<other>"}`，接受两笔方向
  相反、币种不同（差额是汇率）或同一币种下金额不同（差额记为手续费）的资金记录。
  `GET /api/v1/finance/transactions/{id}/pair-candidates` 会列出它们，标为 `differs`。两笔币种不同的
  记录，如果其连接器给出的手续费放不进这次换汇，就不能配对：唯一的选择是 Reject。

### 警告

- **“eToro Trading SYMBOL differs from eToro's report on DATE by AMOUNT”**：每次同步和上传后，
  Lararium 都把每个账户的持仓和现金与 eToro 自己的报告对比，绝不悄悄修正差异。
- **“eToro Trading could not be reconciled”**：Lararium 无法对比（比如 eToro 还没报告余额）；
  上一次检查的警告保留。
- **“eToro flagged: …”** 指出 Lararium 还没处理的某个工作表、列、字段、取值或类型。每条只通知一次。

### 待复核项

当 eToro 的 API 和报表，或 Trading 212 的 API 和导出文件，对同一件事说法不一时，Lararium 会开一个
待复核项（review item），两个值都保留，按 API 的值入账，并发一条通知，比如 “eToro: eToro's API and
statement disagree on close of position ···1234 (cash); they are resolved through the API (README:
Review items)”。发送失败的通知会在下一次同步时再发。对 eToro，
这件事是一次开仓或平仓的数量、现金、盈亏或记账日期；对 Trading 212，是一笔入金或出金的金额、币种或
记账日期。

目前还没有界面；请从 tailnet 里的设备调用 API。`GET /api/v1/connections/<id>/reviews` 列出该连接的
待复核项，未处理的在前，每项带两个值。`POST /api/v1/connections/<id>/reviews/<review id>/resolve`
加 `{"take": "api"}` 或 `{"take": "statement"}`（对 Trading 212，`statement` 指导出文件）处理一项：
你选的值立即入账，之后的同步也会保留。如果之后任一来源改了它的值，该项会重新打开；两个来源变得一致的
项会自行关闭。

### 已知限制

- 不支持 CFD、杠杆或跟单交易：这类持仓会被标记，不入账。
- 没有 MoneyFarm 的持仓或交易，只有它的余额。
- eToro 的费用、税和分红只来自报表。
- 只在连接之前持有过的 eToro 标的（开仓又平仓，从没在某次同步时持有）要靠报表提供币种：在有报表覆盖
  它之前，它的交易会被标记，不入账。
- 报表时间被视为 UTC。这只在冬季得到确认（那时 UTC 与英国时间一致）。如果夏季的报表其实用的是英国
  时间，只来自报表的事件会差一小时；只有当这一小时让某笔交易落到另一天时，才会出现待复核项。
- Trading 212 的正负号（出金、费用、转账、订单的税费和净额）以及导出文件里的交易行，还没有用真实交易
  核对过。
- 如果 Trading 212 改了某条记录、之后又改回去（一笔入金先是 10，再是 20，又变回 10），Lararium 按 10
  入账；但新版本的 Lararium 重建该连接的交易时，会按中间那个版本（20）入账。
- 卡入金只与连接器账户的记录配对，绝不与手动账户或导入账户的配对。
- 一笔刷卡付款会与商户所属账户里任何一条不指明其他账户、金额相同且在两分钟内的记录配对。所以如果
  付款金额恰好等于这段时间内的一笔利息入账，就会与它配对。
- 关闭一个 Trading 212 账户后，它仍登记在它的卡商户下，所以过去的卡入金保持配对；不会再有新的配进来。
  把它从该商户的账户里移除，才会取消这些配对。
- 很长的历史分几次同步读完：Trading 212 每次同步每类历史最多 50 页，eToro 每次同步最多查询 50 个订单
  （已平仓的交易会等它的订单查询，或等一份覆盖它的报表）。
- 在 Monzo 同步和 Trading 212 同步之间，一笔卡入金会显示为记在 Unassigned 上的支出，净资产会因此
  下跌，直到两侧都同步进来。

## 连接 Binance

Binance 是一个连接器：`worker` 通过 Binance 的 REST API 同步它，使用的密钥只能读取。它不会
交易、转账、申购或提现：客户端只发送查询。每个有余额的 Binance 钱包会成为 Lararium 里的一个
账户，其余额就是 Binance 报告的余额，而 Binance 列出的记录（充值、提现、交易等）用来解释余额
是怎么来的。

Lararium 从 Binance 读取的内容：

- **钱包**：每个有余额的钱包是一个名为“Binance <钱包>”的账户，逐个资产持有 Binance 报告的数量：
  - Spot（`spot-account`）、Funding（`funding-asset`）和 Earn（Binance 的钱包列表）；
  - Cross Margin 和 Isolated Margin（`margin-account`、`margin-isolated-account`）：每种资产的
    净数量，所以借入的资产是负持仓（Isolated Margin 按资产汇总它的各个交易对）；
  - USDⓈ-M 和 COIN-M Futures（`um-account`、`cm-account`）：每种资产的钱包余额加未实现盈亏，
    对账只用钱包余额；
  - Options（`opt-account`）：期权保证金账户中每种资产的权益（equity），对账只用保证金余额
    （margin balance）；
  - Crypto Loans（`loan-flexible-ongoing`、`vip-loan-ongoing`）：进行中借款的负债，是负持仓
    （负债类账户）；
  - 统一账户 Portfolio Margin（`pm-balance`，并保存 `pm-account`），在 Binance 的账户信息显示已
    开启时：每种资产的钱包余额减去借入额及其利息（借入的资产是负持仓），再加 USDⓈ-M 和 COIN-M 的
    未实现盈亏，对账时不含未实现盈亏。它的记录不入账。Portfolio Margin 取代了 Cross Margin 和两个
    合约钱包，所以开启期间不会请求它们，它们报告为空；看到它关闭的那次同步会重新请求它们，而
    Portfolio Margin 报告为空。同一资产绝不会在两边各算一次：切换会显示为对账差异。
  - Trading Bots 和 Copy Trading，来自 Binance 的钱包列表。Binance 没有为它们提供记录，所以它们的
    每次变化都会显示为对账差异。
  - Pending redemptions：Binance 稍后才到账的赎回所换得的资产，在到账 Spot 之前记在这里。

  账户没有的产品（Binance 说尚未开通的杠杆账户、没有期权的账户）会跳过。Binance 钱包列表中其他
  每个有余额的钱包都按该列表逐个资产计入，并按名称标记，仍会得到一个账户；余额不为零却没有列出
  任何资产的钱包也会按名称标记。
- **Binance 上的法币**（Binance 标记为法定货币的资产，例如 EUR）就是 Lararium 已有的那个币种。
  如果 Lararium 没有该 ISO 4217 代码的币种，且该代码还没有任何 instrument，该资产不计入：连接会被
  标记并写出该代码，其余额不计入，其记录保留但不入账。已有 instrument 的代码（在 Binance 标记它为法定
  货币之前按加密资产计入，或经由另一个连接建立）继续计入在那个 instrument 上。以它交易的另一方也不入账：在该账户最早的报告之后，它显示为对账差异；在那之前，
  它包含在该资产的期初调整中。要计入该币种，请在 Instruments 页面添加它（类型 Fiat，以 ISO 代码
  作为 symbol）：之后的同步会计入它的余额和新取得的记录。它的余额起初会显示为对账差异，因为此前的
  报告把它算作零；此前保留的记录只有在 Lararium 下次重放记录（新的规范化版本）或 Binance 修改这些
  记录时才会入账。如果添加该币种时，该代码已有一个加密资产 instrument，从此该资产就是该币种：在下一次
  报告中，加密资产 instrument 读数为 0，该币种读数为该金额，显示为两笔相等且相反的对账差异并保持未结；
  此前的入账留在加密资产 instrument 上，直到 Lararium 重放其记录。
- **记录**：Binance 只读 API 文档列出的每种记录都会拉取，按每个字段一个有类型的列保存，并在它改变
  余额的地方入账：
  - 充值和提现（`deposits`、`withdrawals`；提现手续费单独入账）、现货交易（`my-trades`）、闪兑
    （`convert`）和资产分红（`dividends`）。travel rule 的充值和提现历史（`travel-deposits`、
    `travel-withdrawals`）保存下来并关联到它们描述的那笔充值或提现，不会再入账一次。
  - 钱包之间的划转（`universal-transfer`）：每笔是两个钱包账户上的一笔交易，从不作为在途转账。
    与逐仓杠杆之间的划转按账户的每个逐仓交易对分别拉取。另一个历史也列出的划转（杠杆划转，或类型
    为 `TRANSFER` 的合约资金流水或期权账单）只入账一次。
  - 小额资产兑换成 BNB（`dust`），手续费单独记为费用；Cloud Mining 的付款和退款记在 Spot 上
    （`cloud-mining`）。
  - 活期和定期 Simple Earn 以及链上收益（`earn-flexible-subscriptions`、
    `earn-flexible-redemptions`、`earn-flexible-rewards`、`earn-locked-subscriptions`、
    `earn-locked-redemptions`、`earn-locked-rewards`、`onchain-subscriptions`、
    `onchain-redemptions`、`onchain-rewards`）：某个钱包与 Earn 之间的移动，收益记为收入。它们的
    持仓和账户（`earn-flexible-position`、`earn-locked-position`、`earn-account`、
    `onchain-locked-position`、`onchain-account`）以及活期质押记录（`earn-flexible-collateral`）
    只保存。Binance 以资产分红方式发放的每一笔奖励只入账一次，记为那笔分红。
  - BFUSD 和 RWUSD（`bfusd-subscriptions`、`bfusd-redemptions`、`bfusd-rewards`、
    `rwusd-subscriptions`、`rwusd-redemptions`、`rwusd-rewards`、`bfusd-account`、`rwusd-account`），
    ETH 和 SOL 质押（`eth-stakes`、`eth-redemptions`、`wbeth-wraps`、`wbeth-unwraps`、`sol-stakes`、
    `sol-redemptions`、`sol-boost-rewards`、`eth-staking-account`、`sol-staking-account`、
    `sol-unclaimed-rewards`）：在 Spot 上资产和它的代币之间的兑换。Binance 稍后才到账的赎回，在到账
    之前记在“Binance Pending redemptions”账户上。WBETH 和 BNSOL 的奖励（`wbeth-rewards`、
    `bnsol-rewards`）体现在代币的兑换比例里，只保存。
  - Soft Staking（`soft-staking-rewards`、`soft-staking-list`）：奖励记在 Spot 上。
  - Dual Investment 和 Discount Buy（`dci-positions`、`discount-positions`、`dci-accounts`、
    `discount-accounts`）：投入的资产转入 Earn，持仓结算时离开 Earn。Binance 没有说明持仓结算成什么，
    所以 Spot 上收到的部分会显示为一处对账差异。
  - 加密货币借贷，活期和 VIP（`loan-flexible-borrows`、`loan-flexible-repays`、
    `loan-flexible-ltv-adjustments`、`loan-flexible-liquidations`、`vip-loan-repays`、
    `vip-loan-interest`、`vip-loan-requests`、`vip-loan-collateral-account`）：借款把币转入 Spot，
    记为对应的负债；还款再转回；VIP 还款中的利息记为费用。Binance 不列出活期借款的利息，所以两次报告
    之间没有任何记录能解释的负债增长记为它的利息。强制平仓从 Earn 卖出抵押物。借款自身的记录从不移动
    抵押物：抵押物留在 Binance 报告它所在的钱包里。
  - 杠杆，全仓和逐仓（`margin-trades`、`margin-transfers`、`margin-borrow-repay`、
    `margin-interest`、`margin-capital-flow`、`margin-liquidations`、`margin-small-liability`、
    `margin-liquidation-loan`、`margin-liquidation-loan-repays`）：杠杆成交按现货成交记在它的钱包上；
    利息和强平手续费记为费用。借款和还款不改变净数量，只保存；资金流水、强制平仓、小额负债兑换和
    强平借款也只保存。
  - USDⓈ-M 和 COIN-M 合约（`um-income`、`um-trades`、`um-balance`、`um-positions`、
    `um-exchange-info`、`cm-income`、`cm-trades`、`cm-balance`、`cm-positions`、`cm-exchange-info`）：
    除划转外，每条资金流水都对 Unassigned 入账（支付的手续费、资金费和保险清算记为费用）。成交（按
    资金流水提到的交易对获取）和持仓只保存；合约永远不是一个 instrument。每个合约主机的请求限额从它
    自己的交易规则信息读取。
  - 期权（`opt-bill`、`opt-trades`、`opt-exercises`、`opt-positions`、`opt-exchange-info`）：除划转
    外，每条账单都对 Unassigned 入账（支付的手续费记为费用）。成交（按账单提到的交易对获取）、行权
    记录和持仓只保存；合约永远不是一个 instrument。期权主机的请求限额从它自己的交易规则信息读取。
  - 银行法币出入金、银行卡买卖、P2P、Pay 和返佣（`fiat-orders`、`fiat-payments`、`c2c`、`pay`、
    `rebate`）：法币充值或提现是你的银行与“Binance Spot”之间的转账（见[来自银行的法币充值](#来自银行的法币充值)）；
    银行卡购买是法币从卡里到账，再换成加密资产，手续费单独记账（用现金余额支付的只有兑换这一笔）；
    P2P 订单在“Binance Funding”上同样处理，按其单价。只有已完成的才入账。Pay 交易把付出或收到的金额
    记在它动用的每个钱包上（组合支付会涉及多个钱包），对 Unassigned 入账；返佣是 Spot 上的收入，若由
    资产分红发放则只入账一次。Binance 只提供 2020 年 6 月 10 日以来的返佣，这让首次同步多出大约 7 分钟。

已用真实账户核对：密钥检查，以及在 `api.binance.com`、`fapi.binance.com` 和 `dapi.binance.com` 上的
HMAC 签名；Spot 和 Earn 的余额；充值、提现、现货交易、闪兑、资产分红和活期 Earn，包括 Spot 的精确对账
和以分红发放的奖励。

依据 Binance 的文档实现、尚未在真实账户上见过（每种的第一条真实记录都会对照它的对账检查，规则有误时
届时更正）：

- Funding、Cross Margin、Isolated Margin、USDⓈ-M 和 COIN-M Futures、Options、Crypto Loans 和
  Portfolio Margin 的余额，以及钱包列表中 Trading Bots 和 Copy Trading 的明细；
- 钱包之间的划转、小额资产兑换、Cloud Mining 和 travel rule 历史；
- 定期 Simple Earn、链上收益、活期质押记录、BFUSD 和 RWUSD、ETH 和 SOL 质押（含 WBETH 和 BNSOL）、
  Soft Staking、Dual Investment 和 Discount Buy；
- 加密货币借贷，活期和 VIP；
- 每种杠杆、合约和期权记录，以及 Portfolio Margin 的切换；
- 银行法币出入金、银行卡买卖、P2P、Pay 和返佣；
- 期权和 Portfolio Margin 主机（`eapi.binance.com`、`papi.binance.com`）上的 HMAC 签名，以及 Ed25519
  签名；
- 这些记录各自落在哪个钱包。

### 1. 创建只读 API 密钥

在 Binance 创建一个 API 密钥，只勾选 **Enable Reading**。Lararium 在每次同步开始时检查密钥的
权限：只要开启了其他任何权限，无论已知还是未知，同步都会拒绝这个密钥；没有 **Enable Reading**
的密钥同样会被拒绝。IP 限制可选。两种密钥类型都支持：HMAC（“system generated”）和 Ed25519
（“self-generated”）。注意 Binance 只为 `api.binance.com` 提供 Ed25519 密钥的文档。如果合约或期权
主机不接受 Ed25519 密钥，连接不会被拒绝：该主机不再被读取（一周后再次询问，更新密钥后则立即询问），
它的钱包此前的余额在再次读取之前继续计入，并有一条警告指出主机名，建议改用 HMAC 密钥以包含它。

### 2. 添加连接

在 Accounts → Add account 里选择 **Binance**，填写显示名称、密钥类型、API key 及其 secret（Ed25519
密钥则填 PEM 格式的私钥，若有口令则一并填写）。密钥会加密保存，之后不再显示；**Update key**
可以替换它。同一个 Binance 账户只能连接一次。Binance 不会报告只读密钥的到期时间，因此没有到期
提醒；密钥被拒绝时，连接会显示需要新密钥，并通知你。

### 首次同步做什么

首次同步会把每个历史端点尽可能往回拉取到 Binance 提供的最早时间，然后对 Binance 列出的每个
交易对拉取成交记录，因为成交记录端点需要指定交易对。这大约是 3,750 个请求，按 Binance 的
`/api` 速率限制大约需要 15 分钟；Lararium 会自行控制节奏，保持在限制之下。首次同步可以续传：
中途中断后，下一次同步会从停下的地方继续。之后每 4 小时同步一次，**Sync now** 则总会运行。
它们从上一次同步前 7 天开始请求，所以之后才确认的充值也会被收进来。

Binance 并没有列出所有改变过余额的事件（例如很久以前的一笔 P2P 买入）。对每个资产，Binance
报告的最早余额与记录合计之间的差额，会作为一笔 **Opening balance adjustment** 记一次，日期是
该账户最早记录的前一天。它在每次同步时重新推导，所以一条早于该余额的迟到记录会让它随之变动。

### 对账差异

每次同步，对每个账户和资产，都会把两次余额报告之间的变化与这期间记录的合计做对比。如果两者
不一致，连接上会列出一条 **对账差异（reconciliation difference）** 并发送一次通知。余额仍然
是 Binance 报告的数字；这条差异会一直列着，直到有记录能解释它，而且不会自动修正。通知里不会
带数值、地址或哈希。

### 报告而不处理的情况

Lararium 无法处理的情况，会按名称标记一次。每一项都列在连接的警告里，一次同步新出现的所有标记
放在同一条通知的同一行里。每一项写出的是字段、记录类型、钱包、交易对或资产，绝不是数值：

- Lararium 不认识的密钥权限（开启时密钥也会被拒绝），以及 Binance 文档没有列出的钱包、字段、记录
  类型、状态或取值；
- Portfolio Margin 已开启：计入它的余额，不入账它的记录；
- Trading Bots 或 Copy Trading 持有资产：Binance 没有为它们提供记录，所以它们的每次变化都显示为
  对账差异；
- 你交易过、但 Binance 已不再列出的交易对：它的成交无法再获取，此前没有取到的成交会显示为对账差异；
- 任何来源都无法定价的资产，以及 Lararium 没有对应 instrument 的法币（不计入）。

### 提现到你自己的钱包

提现到 Lararium 不认识的链上地址，会进入 Unassigned，该地址出现在 Suggestions 里。要让它成为
转账，在 Accounts 页面打开某个账户的 **Identifiers**，把钱包地址添加为 **Chain address**，做法
见[登记标识](#登记标识)。此后提现到该地址会显示为在途转账，网络手续费单独记账。每笔充值和提现
还会保存交易哈希、网络和地址，以便以后按哈希配对钱包。

### 来自银行的法币充值

当你的银行流水写明 Binance 法币通道收款或付款的账户时，银行法币充值或提现就是你的银行账户与
“Binance Spot”之间的转账。把这个银行账户（按你的银行显示的样子）登记在“Binance Spot”账户上
（它的 **Identifiers**，做法见[登记标识](#登记标识)），就像为 IBKR 做的那样。把 Binance 登记为
“Binance Spot”的 **card merchant** 后，银行卡购买会与你的银行卡付款配对。P2P 订单的银行付款付给的是
别人的账户，所以不会自动配对：如果希望它们显示为一笔转账，可以手动把两者配对。不配对时，银行那一笔和
Binance 那一笔会在 Unassigned 上相互抵消，所以净资产在任何情况下都是对的。

### 价格

Binance 持仓按每个资产的每日价格以 EUR 估值：

- **Binance 的日 K 线**，通过 Binance 自己的 EUR 市场换算成 EUR（该资产的 EUR 交易对，或其
  USDT 交易对除以 `EURUSDT`）。稳定币也同样定价，从不假设等于一。
- **CoinGecko**，可选。在 `infra/.env` 中设置 `LARARIUM_COINGECKO_API_KEY`（Demo 套餐的密钥
  即可），然后运行 `make up`。对 Binance 没有定价到昨天的任何资产（在 Binance 上没有市场的
  资产，或交易对已停止交易的资产），它会填补最近 365 天里没有 Binance 价格的日子；它从不用于
  Binance 已有价格的日子。设置了密钥时，CoinGecko 对 Binance 市场的列表还会给每个 Binance
  资产它的 CoinGecko id，由此决定该资产对应哪个已有的 instrument（在别处持有的同一种币）。
  没有这个密钥时，Binance 无法定价的资产会显示为缺少价格。CoinGecko 的条款要求署名：
  Price data provided by [CoinGecko](https://www.coingecko.com)。
- **ECB 参考汇率**，每天通过 Frankfurter（`api.frankfurter.dev`，无需密钥）获取，每个安装都会
  运行，无论有没有 Binance 连接，使 EUR 价格可以显示为你的报告币种。它们只填补没有任何其他
  来源提供这两种币种之间汇率的日子。

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

1. **上传。** 目录里有的银行（见下文），在 Accounts → Add account 里选这家银行：上传文件后，
   Lararium 会认出它是这家银行的哪种格式，并为它新建或复用 profile。其他文件在 Data →
   Statement files 上传，并选一个 profile（怎样读这家银行的文件，见下文）。两种方式都要选择
   账户（或填一个新账户的名称）；文件本身不写明币种时，还可以选一个币种。文件最大 5 MiB、
   20,000 行。文件会作为文档保存（在 `documents` 卷中），永不删除。在账户类型出现之前添加的账户，可以用
`PATCH /api/v1/finance/accounts/{id}`、请求体 `{"type_key": "<key>"}` 为它设置类型；账单类型的 key 有
`hsbc_uk`、`lloyds`、`boc_uk`、`revolut.current`、`revolut.savings`、`tembo`、`lemfi` 和
`trading212.cash_isa`。该账户必须是手动的 transactions 账户，且形态与类型相符；设置类型后，它在
Accounts 上的卡片会出现 **Upload file**。
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
完整列出的第一天（导出文件的最后一天可能不完整；行带有 `started_at` 的导出，例如 Revolut 的 CSV，以及 `DTSTART` 带有午夜以外时刻的 OFX 文件，第一天也可能不完整）：那之前，真正的那一行
可能在账户的期初里，永远不会被导入，不能让一笔金额相同但无关的款项顶替它。账户的开户日期（上传时新建账户会询问）
让转账匹配把它第一次上传之前的日子视为空的：一次上传若从这一天或更早开始完整列出，也就代表了之前的所有时间。写明这个账户的记录是一笔付款时，
它的时间窗口从付款前一天开始，因为只给日期的账单可能把一笔付款记在钱转出之后。如果文件给出的时间精确到秒，
这一行不早于那笔付款前两分钟，而那笔付款是已连接账户的记录，这项检查就改从付款当天算起，按这一行的文件读取时用的时区（它的 profile 的时区，Trading 212 Cash ISA 是 UTC；
没有则用你的时区），这一行就是按这个时区记账的：
收款不会早于付款。（OFX 的行按它自己时间写的日期记账，不一定是这个时区；夏令时午夜刚过时发出的付款，这项检查仍可能晚一天开始。）导入的付款永远不算，即使时间精确到秒：它的时间可能是银行入账的时间（OFX 文件就是这样），
晚于钱转出的时间。时间窗口本身，以及候选，都不变。这项检查不采用的一行仍是候选，只是不会被自动采用：它的转账需要处理（*Not listed in full*，还有其他候选时是 *Several candidates*），以它为候选的任何其他转账也一样。已导入的最后一天之后则会采用：万一采用的不是真正那一行，下一份账单会带来真正的那一行，这笔转账就会带着两个候选进入
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
列出，Revolut 账单请截止到已经过完的一天。Tembo 不印期初余额和收支合计：它的 Plan value 必须等于各行最后的余额，每笔金额的方向由交易类型决定，页码和期间（第一行到最后一行）不能有遗漏。Tembo 账单列出账户开立以来直到生成当天的全部记录，所以从开户日（Account open date）到估值日（Valuation date）的前一天都算作完整列出。LemFi 的收入和支出合计可能各比各行之和少一便士（LemFi 把它们
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
  （`decimal_separator`，`.` 或 `,`；`thousands_separator`，默认无，或 `,` `.` 空格 `'`）、
  `currency`（文件只写一次而不逐行写币种时，每一行的币种，例如 LemFi 的 `Money in (GBP)`；否则用上传时
  选的币种，或映射的 `currency` 列）和 `interest_descriptions`（支付利息的行的描述，用于不印交易类型
  代码的银行，例如 LemFi 的 `Savings Interest Payout`：和刷卡付款一样，这样的行永远不会仅凭金额和日期
  被当作一笔转账的另一侧），以及 `period_from_filename`（在上传文件的文件名中搜索的正则表达式，带命名组
  `start` 和 `end`，各是一个 `YYYY-MM-DD` 日期，用于按时段给导出命名的银行，例如 Trading 212 的
  Cash ISA：匹配到的就是文件的时段，除最后一天外每天都完整列出；文件名给不出时段，或有行落在范围之外时，
  上传按原样读取，并附一条警告）。
  重复表头的行不算一行交易。
- **`columns`** 给出文件里的列名：`date`、`description`（列表；多列用一个空格连接），然后是
  `amount`，或同时给出 `inflow` 和 `outflow`；可选的有 `balance`、`currency`、`external_id`、
  `fee`（在金额之外另收的费用）、`type`、`value_date`、`payee`、`memo`、`check_number`、
  `counterparty_name`、`counterparty_sort_code`、`counterparty_account_number`、`reference`
  （付款参考）、`started_at`（付款的发起时间，按 `date_format` 解析，这时它必须带时间；`date` 是
  完成时间；按它读取的导出只部分列出第一天，因为其中的行可能按发起时间选出），以及账单自身账户的 `account_sort_code`、`account_number` 和 `account_type`（产品，
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

### 已知限制

- profile 按暂存文件那一刻的内容读取。如果你为一次上传选好 profile，点上传之前它在另一个
  标签页被改了，文件会按改过的 profile 读取。提交之前，批次页面会列出每一行的金额，所以按
  错误正负号读出的文件会在那里被发现：丢弃它，重新上传即可。

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

要用同样的方式检查一份 eToro 账户报表，把它作为 `ETORO_STATEMENT` 传入，可以单独传，也可以和
`REAL_ROOT` 一起传（两者至少要有一个）：

```bash
make real-check ETORO_STATEMENT="$HOME/Downloads/statement.xlsx"
```

它读取这份报表，报表必须能解析且没有任何标记；再检查报表自己的滚动余额：按秒计，该秒各行之后的余额
必须等于之前的余额加上这些行变动的现金。然后把它上传到同一容器里一个临时的 eToro 连接（首次同步用
替身代替，不用任何 eToro key）：不得有任何拒绝或标记，每一行都必须保存并按类型存储，每笔交易都必须
平衡。报表里从未平仓的持仓在那里等待 API 的数据，只计数，不算失败。它打印每个工作表读到多少行、生成了
多少条记录，以及通过与否：不含文件里的任何数值、ID、姓名或日期。

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

### 恢复演练

不时演练一次恢复，在一台不是生产环境的 Docker 主机上进行（任何有本仓库、并能通过 docker context
访问那台主机的机器都可以运行）：

```bash
make restore-rehearsal CONTEXT=<docker context> \
  DUMP=backups/lararium-20260101T030000Z.dump DOCUMENTS=backups/documents-20260101T030000Z.tar
```

`infra/backup/rehearse-restore.sh` 会在那台主机上启动一个一次性的 PostgreSQL 18，角色与正式服务相同，
然后按上面的步骤操作并逐步检查：

1. `restore.sh` 把转储恢复到一个新数据库，然后交换数据库名；
2. 每张表的行数与转储里该表的行数一致（行数直接从转储本身统计）；
3. 文档 tar 解压到一个一次性卷里，数据库引用的每个文档都在，API 能读取（同一镜像、用户和路径），
   且内容与其哈希一致；
4. 像正式服务的 `migrate` 一样运行 `alembic upgrade head`（较旧的备份会被升级），然后
   `python -m lararium.restore_check` 检查迁移版本是代码的 head、每笔交易都平衡、转账不变量成立。

它只输出表名、行数、文档哈希和交易 id，从不输出金额或描述；任一检查失败时以非零状态退出。数据库
不发布任何端口：它在一个内部 Docker 网络上，只有检查容器加入。后端镜像在那台主机上用你的工作区
构建。容器、卷和网络在运行结束时删除，无论以何种方式结束；`postgres:18`、`alpine` 和
`lararium-rehearsal` 镜像会保留缓存。运行期间那台主机上有真实的备份数据，所以请用你信得过的主机。

## 依赖许可证

`make check` 会运行 `make license-check`：每个生产依赖（后端 `uv.lock` 中除 dev 组以外的依赖，即镜像
所安装的；以及前端除 devDependencies 以外的依赖）都必须声明一个与 AGPL-3.0-or-later 兼容的 SPDX
许可证。允许的许可证，以及元数据不是清晰 SPDX 表达式的包的已审核例外，都在
[`tools/licenses.toml`](tools/licenses.toml) 中。未知或不兼容的许可证会让检查失败。要接受一个，先审核
该包的许可证文件，再添加一条 `[[exception]]`：写上检查输出的原样声明字符串、你的结论和理由。不再
匹配的例外（包已移除，或其声明的许可证变了）同样会让检查失败。

## 许可证

[AGPL-3.0-or-later](LICENSE)。
