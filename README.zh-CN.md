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

Lararium 只能通过 Tailscale 访问，入口有两种，由 `infra/.env` 里的 `LARARIUM_INGRESS` 选择：

- `host`（默认）：主机上的 `tailscale serve` 转发到 `127.0.0.1:8080` 上的 `web`。需要主机装有
  Tailscale，并且 Docker Engine 是 28 或更新版本。
- `tailscale`：一个边车容器作为独立节点 `lararium` 加入你的 tailnet，由它提供 `web`；主机上不发布
  任何端口。适用于 Docker 较旧（比如群晖的 Docker 24）或没装 Tailscale 的主机。见
  [通过 Tailscale 边车访问](#通过-tailscale-边车访问)。

两种方式用的是同一套镜像和代码，传递身份的方式也一样；`make up` 会加上所选方式的覆盖文件
（`infra/compose.host.yaml` 或 `infra/compose.tailscale.yaml`）。如果不用 `make`、自己运行
`docker compose`，要在 `infra/compose.yaml` 后面传入这个文件：单独的基础文件不发布任何端口。

使用 `host` 时，在装有 Docker Engine 28 或更新版本以及 Tailscale 的服务器上：

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

### 通过 Tailscale 边车访问

1. 在 Tailscale 管理后台的 DNS 页面打开 MagicDNS 和 HTTPS 证书：边车用自己名字的证书提供 HTTPS。
2. 在 Settings > Keys 里生成一个 auth key（reusable 和 ephemeral 都不勾）。在 `infra/.env` 里把
   `TS_AUTHKEY` 设为它，并设置 `LARARIUM_INGRESS=tailscale`。
3. 运行 `make up`。在没有 `make` 的主机上（群晖），在仓库目录下运行：

   ```bash
   sudo docker compose -f infra/compose.yaml -f infra/compose.tailscale.yaml \
     --env-file infra/.env up -d --build
   ```

4. 打开 `https://lararium.<你的tailnet>.ts.net`，并把 `LARARIUM_PUBLIC_BASE_URL` 设为这个地址。
5. 在管理后台的 Machines 页面给 `lararium` 关闭 key expiry，否则它的密钥过期时（默认 180 天）会
   登出，Lararium 在它重新登录之前都无法访问。

边车只在第一次登录时用这个 key；登录状态保存在 `lararium_tailscale-state` 卷里，所以之后可以把
key 清空。删掉这个卷，它就会成为一个新节点，需要新的 key。

要在 `host` 和 `tailscale` 之间切换：先运行 `make down`，改 `LARARIUM_INGRESS`，再运行
`make up`。从 `host` 切到边车时，还要用 `tailscale serve reset` 撤掉主机上的 serve。

**升级连接器出现之前的部署。** compose 现在要求设置 `LARARIUM_MASTER_KEY` 和
`LARARIUM_WEBHOOK_IN_PASSWORD`，所以在下一次 `make up` 之前先把两者填进 `infra/.env`（主密钥用
`openssl rand -base64 32` 生成，密码用 `openssl rand -hex 32` 生成）。然后按
[通过 Cloudflare Tunnel 接收 Webhook](#通过-cloudflare-tunnel-接收-webhook) 一节里**已有的数据库**
的说明创建 webhook 接收器的角色。

**使用 `host` 时，如果主机的 443 端口已被占用**，换一个 HTTPS 端口：

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
- 使用 `LARARIUM_INGRESS=host` 时，需要 Docker Engine 28 或更新版本。更旧的引擎可能让局域网里的
  其他机器访问到发布在 `127.0.0.1` 上的端口，从而暴露下面的 `web` 端口。边车不发布任何端口，
  所以不依赖引擎版本。
- webhook 接收器的数据库角色（`lararium_webhook_in`）是只写的：它只能往 webhook 收件箱里插入行，
  不能读取，也碰不了其他任何表。它拿不到主密钥。Cloudflare 负责终结 TLS，所以它能看到 webhook
  的明文内容；这些内容只被当作「立即同步」的信号，真正的数据始终由 worker 向 Monzo 拉取。

### 访问方式与请求头信任

API 依据 `Tailscale-User-Login` 请求头认证请求，只放行 `LARARIUM_OWNER_LOGIN`。这之所以安全，
完全取决于服务的暴露方式：

- `tailscale serve` 是唯一入口，在主机上或在边车里。它接受来自你 tailnet 的连接，把
  `Tailscale-User-Login` 设为调用者的身份，再转发到 `web`。
- 使用 `host` 时，`web` 只绑定 `127.0.0.1:8080`。使用边车时它不发布任何端口，除了本套服务
  自己的容器，只有边车和它在同一个网络里。`api` 服务不发布任何端口：只有 compose 网络里的
  Caddy 能访问它。
- 除此之外，任何东西都不能访问 `web`：不能有别的反向代理、端口转发、`tailscale funnel`，
  主机上也不能有不受信任的本地用户或容器。谁能访问到它，谁就能自己伪造这个请求头、冒充你。

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
