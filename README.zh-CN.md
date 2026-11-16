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
定时任务），以及提供 SPA 并转发 `/api/*` 的 Caddy（`web`）。`make down` 停止整套服务；数据保留在 `lararium_pgdata` 卷中。

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
账户。

### 手动账户：选 hybrid，并录入期初快照

Bank A 没有连接，报告不了它那一侧的转账。Lararium 一看到 Monzo 那一侧，就在 Bank A 上记一笔
*推断*（inferred）交易来代替。要让它改变 Bank A 的余额：

1. 在 Accounts 页面创建账户时选 **Balance mode: Hybrid**（ledger type 选 Asset，subtype 选
   Bank）。
2. 保存一个期初快照：按 Bank A 的对账单，填某个日期的 GBP 余额。

hybrid 账户的余额是最近一次快照加上快照之后记入的所有分录。快照日期之前的转账已经包含在快照里，
不会算两遍。日期在该账户这种货币的第一个快照当时或之前的转账，根本不会生成推断交易，而是对 Opening
balances 结清，所以快照之前的历史也不会变。之后再录入一个更早的快照，它之后的转账会回到这个账户上。手动账户不要用 Transactions 模式：那样它的余额就只是 Lararium 看到的那些转账。
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

### Transfers 页面

每笔转账一行，比如「Monzo → Bank A · 100.00 GBP」。

- **Needs attention**（需要处理）：Lararium 不会自行决定的转账。只有当两条记录互为对方唯一的候选时，
  它才会把它们配对，所以结果从不取决于哪个数据源先同步。
  - *Several candidates*（多个候选）：不止一条记录可能是另一侧（比如相隔几天、金额相同的两笔转账）。
    页面列出这些候选；对正确的那条点 **Pair**。
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
  - *before history*：另一个账户的连接开始于这笔转账之后，所以它的期初余额已经包含了这笔钱；
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
和 `LARARIUM_FEISHU_SECRET`（`make up` 会把它们传给 worker）。两者都不设置就不发通知。消息里
不会出现任何凭据。

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
路径时相对于仓库根目录）。如果 `infra/.env` 里设置了 `RESTIC_REPOSITORY`，随后还会用
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

在全新服务器上，先把 `infra/.env` 里的 `LARARIUM_MASTER_KEY` 设成转储来源那台服务器的主密钥（从你单独
备份的密钥或 `infra/.env.sops` 里取），不要新生成：转储里存的凭据是用它加密的，换了密钥，每个连接都得
重新添加。然后运行 `make up`（它会创建角色和一个已迁移的空数据库），再按上面的步骤恢复并交换。

## 许可证

[AGPL-3.0-or-later](LICENSE)。
