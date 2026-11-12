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

在装有 Docker Engine 28 或更新版本以及 Tailscale 的服务器上：

```bash
cp infra/env.example infra/.env
chmod 600 infra/.env
# 填写 infra/.env：每个密码都用 `openssl rand -hex 32` 生成，
# 并把 LARARIUM_OWNER_LOGIN 设为你的 Tailscale 登录名（例如 you@example.com）。
make up
tailscale serve --bg 8080
```

`make up` 会构建镜像并启动 PostgreSQL（`db`），以 `lararium_owner` 身份执行一次迁移
（`migrate`），然后以 `lararium_backend` 身份启动 API（`api`），以及提供 SPA 并转发 `/api/*`
的 Caddy（`web`）。`make down` 停止整套服务；数据保留在 `lararium_pgdata` 卷中。

在 tailnet 里的任意设备上打开 `https://<主机名>.<你的tailnet>.ts.net`。

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
- 需要 Docker Engine 28 或更新版本。更旧的引擎可能让局域网里的其他机器访问到发布在
  `127.0.0.1` 上的端口，从而暴露下面的 `web` 端口。

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

要让恢复出的副本生效：停止 API，交换数据库名，再重新启动：

```bash
docker compose -f infra/compose.yaml --env-file infra/.env stop api
docker compose -f infra/compose.yaml --env-file infra/.env exec -T db psql -U postgres \
  -c "ALTER DATABASE lararium RENAME TO lararium_old" \
  -c "ALTER DATABASE lararium_restore RENAME TO lararium"
docker compose -f infra/compose.yaml --env-file infra/.env start api
```

在全新服务器上，先运行 `make up`（它会创建角色和一个已迁移的空数据库），再按上面的步骤
恢复并交换。

## 许可证

[AGPL-3.0-or-later](LICENSE)。
