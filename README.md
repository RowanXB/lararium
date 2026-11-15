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

On a server with Tailscale and Docker Engine 28 or later (or an older engine on a trusted
LAN; see the security model below):

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
`lararium_pgdata` volume, and uploaded statement files in `lararium_documents`.

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
- Docker Engine 28 or later is recommended. On Linux, older engines (Synology ships 24) let
  another machine on the LAN that adds a route reach the containers directly, `web` and
  `api` included, and send the identity header itself. That is acceptable only on a trusted
  LAN: no devices you don't control, guest Wi-Fi isolated. Otherwise upgrade Docker, or
  firewall Lararium's Docker bridges from the LAN. Docker Desktop is not affected.
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
   (once a connection exists, the Connections page shows the exact value). Note its client ID
   and client secret.
3. In Lararium open Connections, add a Monzo connection with the client ID and secret, and
   choose Authorise. Monzo opens in your browser; sign in with the emailed link.
4. **Approve within 4 minutes.** Monzo asks you to approve the new access in the Monzo app.
   Only for the first 5 minutes after you approve does Monzo serve your full history, so
   Lararium polls for your approval for up to 4 minutes and then backfills everything at once.
   If you don't approve in time, or the backfill doesn't finish, the connection says so;
   authorise again to retry (you only get the last 90 days outside that window).
5. After that the connection syncs by itself. The worker runs up to 4 jobs at once, so one
   connection's approval wait doesn't hold up another's; one connection's jobs never overlap.

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
lost, every stored credential is unreadable, and a connection can't be repaired in place: add
each one again, with its client secret, and authorise it. Then disable the old connection and
close its accounts and pots, so they stop counting towards net worth. The web app can't close
accounts yet, so use the API from a device on your tailnet: list them with
`GET /api/v1/finance/accounts`, and close each with `POST /api/v1/finance/accounts/<id>/close`
and the body `{"closed_on": "<today>"}`. Generate the key once with `openssl rand -base64 32`;
don't rotate it by editing `.env`.

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

Known gaps: a pending payment is fetched again until it settles or is voided, but one that
Monzo stops listing altogether stays pending; the reconciliation mismatch warning on the
connection is the signal. And history a sync needs that is older than the 89 days Monzo serves
outside the approval window (after a long outage, say) can't be fetched: the connection then
shows a "sync window limited" warning. To backfill it, disable the connection, then authorise
it again and approve within 4 minutes (a healthy connection can't be authorised directly).

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

4. Set `COMPOSE_PROFILES=webhook` in `infra/.env`, then run `make up`. The profile makes the
   receiver and the tunnel part of the stack, so `make up` builds and starts them with
   everything else (the `worker` and `api` need the three variables too). To turn webhooks
   off, clear the three variables and `COMPOSE_PROFILES`, then run `make down` and `make up`:
   `make up` alone leaves the receiver and the tunnel running.

   Syncs then register `https://hooks.example.com/monzo/<secret>` with Monzo. The receiver
   stores each delivery and answers at once; the worker turns it into a sync within a minute.
   A wrong secret gets 404, like any other path.

The receiver runs uvicorn without an access log (the secret is in the URL path); keep it that
way, and don't put the secret in your Cloudflare logs or dashboards.

**Existing databases.** `infra/postgres/roles.sql` only runs on a fresh volume, so a database
created before the webhook receiver has no `lararium_webhook_in` role, and migration 0011 skipped
its grants. Once, before setting `COMPOSE_PROFILES=webhook`: set `LARARIUM_WEBHOOK_IN_PASSWORD`
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

## Importing statements

For accounts no connector syncs, upload the statement files your bank exports. Nothing reaches
the ledger until you have reviewed it.

1. **Upload.** On Imports, choose the account, a profile (how to read this bank's file; see
   below) and the file, and optionally a currency for files that don't state one. Files are at
   most 5 MiB and 20,000 rows. The file is kept as a document (in the `documents` volume) and
   never deleted.
2. **Review.** The batch page lists every row as **new**, **duplicate** (already in the
   ledger), **skipped** (a zero amount, or left out by the profile's `include_only`; the reason
   is shown) or **error** (couldn't be read, or breaks the running balance). Any error blocks
   the commit: fix the profile or the file, discard the batch and upload again. Warnings above
   the rows say when the file has no balances, or when committing it would change another
   import's opening adjustment.
3. **Commit.** Each new row becomes a transaction; duplicates and skipped rows never do.
   Discard instead to drop a staged batch.
4. **Roll back.** Rolling back a committed batch voids its transactions (nothing is deleted),
   so the same file can be imported again. A batch whose rows another committed batch skipped
   as already imported can't be rolled back until that other batch is: the error names it.

**Which accounts take imports.** Only open, manual accounts (not synced from a connection) whose
balance mode is `transactions` or `hybrid`. A `snapshots` account's balance ignores
transactions, so imports into it are refused rather than silently not counting.

**The other side of each row.** A bank row has one side; the ledger is double-entry. The other
leg goes to the **Unassigned** system account, the same one Monzo uses, which never changes net
worth. A statement rarely starts on the account's first day, so on a `transactions` account the
import also adds an **opening adjustment** (against **Opening balances**) the day before the
statement's first row, so the balance after it matches what the statement says. When a
statement for an earlier period arrives later, the adjustments of every import into that account
are re-aligned at commit and at rollback, so an opening balance is never counted twice; the
review page warns you first. A later import that needs an adjustment usually means a statement
period is missing: discard it and import the gap first. Hybrid accounts take no adjustment,
because their snapshots anchor the balance.

**Everything the file says is stored.** Each row keeps its date and value date, payee and memo,
cheque number, the bank's type code, counterparty name, sort code, account number and payment
reference, and further fields where a statement prints them (payment kind, bank reference,
purpose code, remittance text, counterparty bank), each in its own column, plus the whole source
row. Each statement keeps its period, opening and closing balance, money in and out, payment
counts, reference number and date, the account as printed (type, name, sort code, account
number, IBAN, BIC, customer number, branch), the arranged overdraft limit and its interest rate
table. The account a file names must agree with the earlier imports into the same ledger
account, or the upload is refused (the message shows only the last four digits).

### Formats

Under Import profiles, **Add from catalog** creates a ready profile for each bank format below;
for each bank use the recommended one, which carries the most information.

| Bank | Format | Recommended |
|---|---|---|
| HSBC UK | PDF statement | yes |
| HSBC UK | midata CSV, OFX, CSV (transaction list, no header row), QIF | |
| Lloyds | CSV | yes |
| Lloyds | PDF statement, QIF | |
| BOC UK | PDF statement | yes (the only format) |

A PDF statement is read by a layout written for that bank, which checks itself: the summary
(opening and closing balance, money in and out, payment counts) must equal the rows, every
printed balance must follow from the amounts, every row must have exactly one amount, a date
inside the statement period and a type code, and every word in the table must belong to a row.
A statement that doesn't add up is refused as a whole, naming the failed check, rather than
imported with wrong numbers. You can import different formats of the same account over
overlapping days, for example PDF statements for the past and the OFX export for the weeks
since, in either order: rows on days an earlier import already covers are matched against it.

### Profiles

A profile is JSON, checked by the server when you save it. `format` is `csv`, `ofx`, `qif` or
`pdf`; every format takes an optional `timezone` (an IANA name such as `Europe/London`; the
default is Lararium's time zone), which turns dates into times without moving the date written
in the file.

- **`csv`**: `columns` (below), `date_format` (a `strptime` format such as `%d/%m/%Y`; one with
  a time such as `%d %b, %Y %H:%M` keeps the time of day), `encoding` (`utf-8-sig`, the default,
  `utf-8`, `cp1252` or `latin-1`), `delimiter` (`,`), `skip_rows` (lines before the header,
  0–50), `header` (the column names in order, for a file without a header row),
  `skip_footer_rows` (0–20) and `overdraft_footer_label` (the first cell of the footer line
  with the arranged overdraft limit), `include_only` (`{"column": ["value", ...]}`: rows with
  another value are skipped, e.g. pending card payments), `row_order` (`oldest_first` or
  `newest_first`), `sign` (below) and `amount_format` (`decimal_separator`, `.` or `,`;
  `thousands_separator`, none by default, or `,` `.` space `'`).
- **`columns`** names the file's columns: `date`, `description` (a list; several are joined
  with a space), then either `amount` or both `inflow` and `outflow`, and optionally `balance`,
  `currency`, `external_id`, `fee` (charged on top of the amount), `type`, `value_date`,
  `payee`, `memo`, `check_number`, `counterparty_name`, `counterparty_sort_code`,
  `counterparty_account_number`, `reference` (the payment reference), and the statement's own
  `account_sort_code` and `account_number`.
- **`ofx`**: `sign`.
- **`qif`**: `date_format`, `encoding`, `amount_format` (`,` thousands by default) and `sign`.
- **`pdf`**: `layout`: `hsbc_uk_statement`, `lloyds_statement` or `boc_uk_statement`.

**Signs.** Profiles speak of **inflow** (the account's balance goes up) and **outflow**. With
one `amount` column, `sign` says which way it points: `inflow_positive` (the default) or
`outflow_positive`; with `inflow` and `outflow` columns the amount is inflow minus outflow. On
a credit card (a liability) a purchase is an outflow, making the balance more negative, and a
payment to the card is an inflow, bringing it back towards zero. So a card export that lists
purchases as positive numbers needs `"sign": "outflow_positive"`.

**Duplicates.** A row is a duplicate when a committed row of the same account has the same key:
its external ID (an OFX `FITID`, or a mapped `external_id` column) when it has one; otherwise its
date, amount, currency, description and running balance; otherwise those without the balance
plus how many identical rows come before it in the same file, so two real identical purchases on
one day are both imported, and both are duplicates when the file comes again. Files in different
formats don't share keys, so on days an earlier import already covers, rows are matched to that
import's rows by date and amount instead: the matched ones are duplicates and any other is an
error. A file that lacks a row the earlier import has on those days is refused at upload; if
the new file is right, roll the earlier import back first.

**Example files.** `backend/src/lararium/modules/finance/importing/examples/` holds synthetic
Example Bank files (`example-bank.csv`, `.ofx`, `.qif`) and the profiles for the first two
(`example-bank-csv.json`, `example-bank-ofx.json`). Paste a profile's JSON into a new profile,
pick GBP at upload, and import into a manual `transactions` account to try the flow.

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
`BACKUP_DIR` is relative to the repository root) and, with the same timestamp,
`documents-<UTC timestamp>.tar`: the uploaded statement files from the `lararium_documents`
volume, read through the `api` container. The stack must be running: without the `api`
container the backup fails rather than leave a dump without its documents. If
`RESTIC_REPOSITORY` is set in `infra/.env`, it then runs `restic backup "$BACKUP_DIR"`, using
`RESTIC_PASSWORD`; the `restic` binary must be installed on the host. To run it nightly, add a
cron entry such as `0 3 * * * cd /path/to/lararium && make backup`.

Restore a dump into a new database (the script refuses to overwrite an existing one):

```bash
infra/backup/restore.sh backups/lararium-20260101T030000Z.dump lararium_restore
docker compose -f infra/compose.yaml --env-file infra/.env exec -T db \
  psql -U postgres -d lararium_restore -tAc "select count(*) from finance.instruments"
```

To make the restored copy live, stop everything that connects to the database (all services
but `db`, in every profile, so a receiver started under one since turned off stops too), swap the database names, and start the stack again. PostgreSQL won't rename a
database while anyone is connected to it.

```bash
docker compose -f infra/compose.yaml --env-file infra/.env --profile '*' stop
docker compose -f infra/compose.yaml --env-file infra/.env start db
docker compose -f infra/compose.yaml --env-file infra/.env exec -T db psql -U postgres \
  -c "ALTER DATABASE lararium RENAME TO lararium_old" \
  -c "ALTER DATABASE lararium_restore RENAME TO lararium"
make up
```

Restore the documents into the `lararium_documents` volume (the dump's rows refer to them by
hash; files already there are overwritten with identical copies):

```bash
docker run --rm -v lararium_documents:/d -i alpine tar -C /d -xf - < backups/documents-20260101T030000Z.tar
```

On a fresh server, first set `LARARIUM_MASTER_KEY` in `infra/.env` to the key of the server
the dump came from (from your separate key backup, or `infra/.env.sops`), not a new one: the
stored credentials in the dump were encrypted with it, and under any other key every
connection would have to be added again. Then run `make up` (it creates the roles and an
empty, migrated database), and restore and swap as above.

## License

[AGPL-3.0-or-later](LICENSE).
