# financial-gate

English | [简体中文](README.zh-CN.md)

A self-hosted, read-only personal finance system: a double-entry ledger, balance
snapshots, prices and FX rates, and a net worth view. Python 3.13 (FastAPI) on
PostgreSQL 18 with a React SPA, deployed with Docker Compose and reached only through
Tailscale. Licensed under AGPL-3.0-or-later.

## Local development

Requirements: Docker (with buildx, daemon running), uv, Python 3.13, Node 24, pnpm 11, git.

Run every check (lint, format, types, import contracts, backend tests against a throwaway
PostgreSQL container, the OpenAPI/client drift check, web typecheck and tests). It installs
strictly from `backend/uv.lock` and `web/pnpm-lock.yaml` and fails if either is stale:

```bash
make check
```

To run the app, start a development database (roles are created by the same init script
the deployment uses), migrate it, then start the API with auto-reload:

```bash
docker run -d --name fg-dev-db -p 127.0.0.1:5432:5432 \
  -e POSTGRES_PASSWORD=dev -e FG_OWNER_PASSWORD=owner -e FG_BACKEND_PASSWORD=backend \
  -v "$PWD/infra/postgres:/fg:ro" \
  -v "$PWD/infra/postgres/init:/docker-entrypoint-initdb.d:ro" \
  postgres:18

cd backend
FG_MIGRATION_DATABASE_URL=postgresql+psycopg://fg_owner:owner@127.0.0.1:5432/financial_gate \
  uv run alembic upgrade head
FG_DATABASE_URL=postgresql+psycopg://fg_backend:backend@127.0.0.1:5432/financial_gate \
FG_AUTH_MODE=dev \
  uv run uvicorn financial_gate.api.app:create_app --factory --reload
```

`FG_AUTH_MODE=dev` skips the Tailscale header check; never use it in a deployment. In a
second terminal, start the web dev server (it proxies `/api` to `127.0.0.1:8000`):

```bash
cd web
pnpm install --frozen-lockfile
pnpm dev
```

After changing the API, regenerate `web/openapi.json` and the typed client with
`make openapi` and commit both.

## Server deployment

On a server with Docker Engine 28 or later and Tailscale:

```bash
cp infra/env.example infra/.env
chmod 600 infra/.env
# Fill in infra/.env: generate each password with `openssl rand -hex 32`, and set
# FG_OWNER_LOGIN to your Tailscale login (e.g. you@example.com).
make up
tailscale serve --bg 8080
```

`make up` builds the images and starts PostgreSQL (`db`), runs the migrations once as
`fg_owner` (`migrate`), then starts the API as `fg_backend` (`api`) and Caddy serving the
SPA and forwarding `/api/*` (`web`). `make down` stops the stack; the data stays in the
`financial-gate_pgdata` volume.

Open `https://<host>.<your-tailnet>.ts.net` from any device in your tailnet.

**If port 443 is already taken on the host**, serve on another HTTPS port instead:

```bash
tailscale serve --bg --https=8443 8080
```

and open `https://<host>.<your-tailnet>.ts.net:8443`. `tailscale serve` still supplies the
identity header, so nothing else changes. Don't put financial-gate behind another reverse
proxy (e.g. an nginx already on 443) instead: traffic that bypasses `tailscale serve` can
forge `Tailscale-User-Login`.

### Security model

- The system is read-only: it never places orders, moves money or withdraws funds.
- All third-party credentials are read-only: when a connector needs an API key or token,
  create one that can only read.
- Private keys and seed phrases are never stored: wallets are tracked by public address
  only.
- Docker Engine 28 or later is required. Older engines could let other machines on the
  LAN reach ports published on `127.0.0.1`, which would expose the `web` port below.

### Access and header trust

The API authenticates requests by the `Tailscale-User-Login` header and admits only
`FG_OWNER_LOGIN`. That is only safe because of how the stack is exposed:

- `tailscale serve` is the only ingress. It accepts connections from your tailnet, sets
  `Tailscale-User-Login` to the caller's identity, and forwards to `127.0.0.1:8080`.
- The `web` service binds to `127.0.0.1:8080` only, and the `api` service publishes no
  ports at all: it is reachable only from Caddy on the compose network.
- Nothing else must be able to reach port 8080: no other reverse proxy, no port forward,
  no `tailscale funnel`, and no untrusted local users or containers on the host. Anyone
  who can reach it can send the header themselves and act as you.

## Secrets: encrypting `infra/.env` with SOPS + age

`infra/.env` holds every credential and is ignored by git. To keep an encrypted copy
(for example next to your backups), use [SOPS](https://github.com/getsops/sops) with
[age](https://github.com/FiloSottile/age):

```bash
# Once: create a key pair. Keep keys.txt safe and separate from the encrypted file.
mkdir -p ~/.config/sops/age
age-keygen -o ~/.config/sops/age/keys.txt   # prints the public key: age1...

# Encrypt
sops encrypt --age age1... --input-type dotenv --output-type dotenv \
  infra/.env > infra/.env.sops

# Decrypt (e.g. on a new server)
SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt \
  sops decrypt --input-type dotenv --output-type dotenv infra/.env.sops > infra/.env
chmod 600 infra/.env
```

`infra/.env.sops` is ignored by git too; don't force-add it to a public repository.

## Backup and restore

```bash
make backup   # or: infra/backup/backup.sh
```

This writes `$BACKUP_DIR/fg-<UTC timestamp>.dump` (`pg_dump` custom format; a relative
`BACKUP_DIR` is relative to the repository root). If `RESTIC_REPOSITORY` is set in
`infra/.env`, it then runs `restic backup "$BACKUP_DIR"`, using `RESTIC_PASSWORD`; the
`restic` binary must be installed on the host. To run it nightly, add a cron entry such
as `0 3 * * * cd /path/to/financial-gate && make backup`.

Restore a dump into a new database (the script refuses to overwrite an existing one):

```bash
infra/backup/restore.sh backups/fg-20260101T030000Z.dump financial_gate_restore
docker compose -f infra/compose.yaml --env-file infra/.env exec -T db \
  psql -U postgres -d financial_gate_restore -tAc "select count(*) from finance.instruments"
```

To make the restored copy live, stop the API, swap the database names, and start it again:

```bash
docker compose -f infra/compose.yaml --env-file infra/.env stop api
docker compose -f infra/compose.yaml --env-file infra/.env exec -T db psql -U postgres \
  -c "ALTER DATABASE financial_gate RENAME TO financial_gate_old" \
  -c "ALTER DATABASE financial_gate_restore RENAME TO financial_gate"
docker compose -f infra/compose.yaml --env-file infra/.env start api
```

On a fresh server, run `make up` first (it creates the roles and an empty, migrated
database), then restore and swap as above.

## License

[AGPL-3.0-or-later](LICENSE).
