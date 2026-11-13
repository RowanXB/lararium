# Lararium

English | [简体中文](README.zh-CN.md)

A lararium was the shrine in a Roman home where the Lares, the household's guardian
spirits, lived. This project is your household's lararium: your finances and, over time,
the rest of your life's data, self-hosted and kept by you. Its AI assistants will be its
lares.

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

`LARARIUM_AUTH_MODE=dev` skips the Tailscale header check; never use it in a deployment. In a
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
# Fill in infra/.env: generate each password with `openssl rand -hex 32`, the master key
# with `openssl rand -base64 32`, and set LARARIUM_OWNER_LOGIN to your Tailscale login
# (e.g. you@example.com).
make up
tailscale serve --bg 8080
```

`make up` builds the images and starts PostgreSQL (`db`), runs the migrations once as
`lararium_owner` (`migrate`), then starts the API as `lararium_backend` (`api`), the
background worker (`worker`: connection syncs and periodic jobs) and Caddy
serving the SPA and forwarding `/api/*` (`web`). `make down` stops the stack; the data stays in the
`lararium_pgdata` volume.

Open `https://<host>.<your-tailnet>.ts.net` from any device in your tailnet.

**Upgrading a deployment from before connectors.** Compose now requires
`LARARIUM_MASTER_KEY` and `LARARIUM_WEBHOOK_IN_PASSWORD`, so set both in `infra/.env` before the
next `make up` (the key with `openssl rand -base64 32`, the password with `openssl rand -hex 32`).
Then create the webhook receiver's role as **Existing databases** under
[Webhooks through Cloudflare Tunnel](#webhooks-through-cloudflare-tunnel) describes.

**If port 443 is already taken on the host**, serve on another HTTPS port instead:

```bash
tailscale serve --bg --https=8443 8080
```

and open `https://<host>.<your-tailnet>.ts.net:8443`. `tailscale serve` still supplies the
identity header, so nothing else changes. Don't put Lararium behind another reverse
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
- The webhook receiver's database role (`lararium_webhook_in`) is write-only: it can insert rows
  into the webhook inbox, and cannot read them or touch any other table. It never gets the
  master key. Cloudflare terminates TLS, so it sees webhook payloads in plaintext; they are
  only used as a "sync now" signal, and the data itself is always fetched from Monzo by the
  worker.

### Access and header trust

The API authenticates requests by the `Tailscale-User-Login` header and admits only
`LARARIUM_OWNER_LOGIN`. That is only safe because of how the stack is exposed:

- `tailscale serve` is the only ingress. It accepts connections from your tailnet, sets
  `Tailscale-User-Login` to the caller's identity, and forwards to `127.0.0.1:8080`.
- The `web` service binds to `127.0.0.1:8080` only, and the `api` service publishes no
  ports at all: it is reachable only from Caddy on the compose network.
- Nothing else must be able to reach port 8080: no other reverse proxy, no port forward,
  no `tailscale funnel`, and no untrusted local users or containers on the host. Anyone
  who can reach it can send the header themselves and act as you.

Because `tailscale serve` attaches your identity to every request your browser sends,
including ones another site's page triggers, the API refuses any request whose
`Sec-Fetch-Site` or `Origin` shows it came from another site, reads included. Caddy also
forbids other pages from framing the app (`frame-ancestors 'none'`), so a lured click
can't act through a framed copy.

## Connecting Monzo

Monzo is a connector: the `worker` service syncs it every 30 minutes (and on "Sync now" or a
webhook) into the ledger. It only reads.

1. Set `LARARIUM_PUBLIC_BASE_URL` in `infra/.env` to the URL you open Lararium at (the
   `tailscale serve` URL, no trailing slash), and restart with `make up`. Without it the API
   refuses to create a connection.
2. At [developers.monzo.com](https://developers.monzo.com) create an OAuth client: confidential,
   with the redirect URI `<LARARIUM_PUBLIC_BASE_URL>/api/v1/connections/oauth/monzo/callback`
   (the Connections page shows the exact value). Note its client ID and client secret.
3. In Lararium open Connections, add a Monzo connection with the client ID and secret, and
   choose Authorise. Monzo opens in your browser; sign in with the emailed link.
4. **Approve within 4 minutes.** Monzo asks you to approve the new access in the Monzo app.
   Only for the first 5 minutes after you approve does Monzo serve your full history, so
   Lararium polls for your approval for up to 4 minutes and then backfills everything at once.
   If you don't approve in time, or the backfill doesn't finish, the connection says so;
   authorise again to retry (you only get the last 90 days outside that window).
5. After that the connection syncs by itself. The worker runs one job at a time (concurrency
   1), so the 4-minute approval wait delays other connections' syncs; that is fine with one
   Monzo connection.

What syncs: your accounts (a joint account is a separate account), each pot as a child account
of its account, transactions (declined ones are skipped; pending ones are updated when they
settle or are reversed), and balance snapshots. Card payments are matched to merchants; pot
moves become transfers between the account and the pot.

Two system accounts appear in the ledger:

- **Unassigned**: the other side of a synced transaction whose category you haven't assigned
  yet (every Monzo payment has two sides in a double-entry ledger).
- **Opening balances**: the other side of the opening adjustment that makes an account's
  balance start from what Monzo reports, so history that predates the sync window still adds up.

Credentials (the client secret and the OAuth tokens) are encrypted with `LARARIUM_MASTER_KEY`
before they are stored. **Back the master key up separately from your database dumps** (a
dump plus the key restores everything; a dump alone can't read the credentials). If the key is
lost, every stored credential is unreadable: you must re-enter each one and re-authorise each
connection. Generate it once with `openssl rand -base64 32`; don't rotate it by editing `.env`.

### Verifying against your real Monzo account

The tests use synthetic responses written from Monzo's documentation. Before relying on the
connector, check them against your own account (nothing here changes anything in Monzo):

1. Create and authorise the connection, approving within 4 minutes.
2. Confirm the accounts, pots and the opening adjustment appear, and that Lararium's balance
   for the Monzo account equals the balance in the Monzo app.
3. Make a small card payment and see it arrive within about a minute (needs the webhook set up
   below; otherwise within 30 minutes or on "Sync now").
4. Save one response per endpoint (accounts, balance, pots, transactions with `expand[]=merchant`,
   webhook registration) to `private/monzo-samples/`, which git ignores. Strip names and
   account numbers if you share them.
5. Compare them with the fixtures in `backend/tests/finance/monzo/`:
   - the **counterparty** fields on transfers and payments from people;
   - **`metadata.pot_id`** on a transaction that moves money into or out of a pot;
   - the **401 body shape**: whether it carries `code` or `error` (the client reads both);
   - what a **reversed pending payment** looks like (the same transaction id with a
     `decline_reason`, a changed `amount` or `settled`, or no longer listed at all);
   - that Monzo answers **403 before you approve** in the app, as the client assumes.
6. Where reality differs, fix the fixtures and the normalizer using synthetic data, never the
   real responses.

Known gap: syncs re-fetch only the last 14 days, so a pending payment that disappears or settles
more than 14 days after it was made stays pending until you notice it. The reconciliation
mismatch warning on the connection is the signal.

## Webhooks through Cloudflare Tunnel

Polling every 30 minutes works without this. To see payments within about a minute, Monzo
needs a public URL to call. Only a tiny receiver is exposed, through a Cloudflare Tunnel, so no
port on your server is opened:

1. In Cloudflare Zero Trust create a tunnel and copy its token into `CLOUDFLARE_TUNNEL_TOKEN`.
   Add a public hostname (for example `hooks.example.com`) with the service
   `http://webhook-in:8000`, and set `LARARIUM_WEBHOOK_BASE_URL` to `https://hooks.example.com`.
2. Generate the path secret into `LARARIUM_WEBHOOK_SECRET`: at least 32 characters, using only
   letters, digits and `. _ ~ -` (hex from `openssl rand -hex 32` is simplest). The receiver
   refuses anything else. Monzo's deliveries are unsigned, so this secret in the URL is what
   tells yours from anyone else's.
3. In the zone's WAF add a custom rule with the action Block and this expression, so that
   nothing but Monzo's `POST /monzo/...` reaches the tunnel:

   ```
   not (http.request.method eq "POST" and starts_with(http.request.uri.path, "/monzo/"))
   ```

4. Start the receiver and the tunnel (the `worker` and `api` also need the three variables;
   `make up` passes them):

   ```bash
   make up
   docker compose -f infra/compose.yaml --env-file infra/.env --profile webhook up -d
   ```

   Syncs then register `https://hooks.example.com/monzo/<secret>` with Monzo. The receiver
   stores each delivery and answers at once; the worker turns it into a sync within a minute.
   A wrong secret gets 404, like any other path.

The receiver runs uvicorn without an access log (the secret is in the URL path); keep it that
way, and don't put the secret in your Cloudflare logs or dashboards.

**Existing databases.** `infra/postgres/roles.sql` only runs on a fresh volume, so a database
created before the webhook receiver has no `lararium_webhook_in` role, and migration 0011 skipped
its grants. Once, before the first `--profile webhook up`: set `LARARIUM_WEBHOOK_IN_PASSWORD`
in `infra/.env`, run `make up` (this recreates `db` with the variable; the data stays), then:

```bash
docker compose -f infra/compose.yaml --env-file infra/.env exec -T db \
  psql -v ON_ERROR_STOP=1 -U postgres -d lararium <<'SQL'
\getenv webhook_in_pw LARARIUM_WEBHOOK_IN_PASSWORD
CREATE ROLE lararium_webhook_in LOGIN PASSWORD :'webhook_in_pw';
GRANT USAGE ON SCHEMA core TO lararium_webhook_in;
GRANT INSERT ON core.webhook_inbox TO lararium_webhook_in;
SQL
```

## Feishu notifications

Lararium can message you when a connection needs re-authorising or a sync keeps failing. In
a Feishu group add a custom bot, turn on signature verification, and copy the webhook URL and
the signing secret into `LARARIUM_FEISHU_WEBHOOK_URL` and `LARARIUM_FEISHU_SECRET` (both in
`infra/.env`; `make up` passes them to the worker). With neither set there are no notifications.
Messages never contain credentials.

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

This writes `$BACKUP_DIR/lararium-<UTC timestamp>.dump` (`pg_dump` custom format; a relative
`BACKUP_DIR` is relative to the repository root). If `RESTIC_REPOSITORY` is set in
`infra/.env`, it then runs `restic backup "$BACKUP_DIR"`, using `RESTIC_PASSWORD`; the
`restic` binary must be installed on the host. To run it nightly, add a cron entry such
as `0 3 * * * cd /path/to/lararium && make backup`.

Restore a dump into a new database (the script refuses to overwrite an existing one):

```bash
infra/backup/restore.sh backups/lararium-20260101T030000Z.dump lararium_restore
docker compose -f infra/compose.yaml --env-file infra/.env exec -T db \
  psql -U postgres -d lararium_restore -tAc "select count(*) from finance.instruments"
```

To make the restored copy live, stop the API, swap the database names, and start it again:

```bash
docker compose -f infra/compose.yaml --env-file infra/.env stop api
docker compose -f infra/compose.yaml --env-file infra/.env exec -T db psql -U postgres \
  -c "ALTER DATABASE lararium RENAME TO lararium_old" \
  -c "ALTER DATABASE lararium_restore RENAME TO lararium"
docker compose -f infra/compose.yaml --env-file infra/.env start api
```

On a fresh server, run `make up` first (it creates the roles and an empty, migrated
database), then restore and swap as above.

## License

[AGPL-3.0-or-later](LICENSE).
