# Connector Framework and Monzo Implementation Plan

**Languages:** English | [简体中文](./2026-11-12-connectors-monzo.zh-CN.md)

The English version is authoritative; if the two versions differ, follow this one.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect the owner's Monzo account through the official API so that accounts, pots, balances and transactions sync into the ledger automatically: on a schedule, and within a minute of a Monzo webhook. Notify the owner on Feishu when a connection breaks.

**Architecture:**
- **Generic core.** A finance-agnostic connector framework in `core`: connections, encrypted credentials, immutable raw records, a webhook inbox, a notifier.
- **Worker.** A Procrastinate worker process runs syncs.
- **Monzo module.** A `lararium.modules.finance.monzo` package holds the API client, the OAuth flow, the sync (API → raw records) and a versioned normalizer (raw records → ledger).
- **Webhooks.** They reach a tiny separate receiver through a Cloudflare Tunnel. The receiver can only insert into the inbox; the worker treats each delivery as a "sync now" signal and never trusts its content.

**Tech Stack:** as plan 1, plus:
- `procrastinate` (MIT): jobs and schedules in PostgreSQL;
- `cryptography` (Apache-2.0/BSD): AES-256-GCM;
- `httpx` (BSD), moved from dev to runtime dependencies;
- `starlette` (BSD), already a FastAPI dependency;
- `cloudflare/cloudflared` (Apache-2.0) as a container image.

**Spec:** `docs/superpowers/specs/2026-11-11-financial-gate-phase1-design.md` (this plan covers M3: §7.1, §7.2, §7.9, §11.1–11.2, and the notification part of §11.5)

**Builds on:** plan 1 as merged in `main` (`aef64ee`, migrations up to `0007`). The deferred M2 plan (`2026-11-12-manual-import.md`) is **not** a prerequisite.

## Before you start: what the owner must set up

These are outside the code; the README written in Task 10 walks through each one. The implementation and its tests do not need them; only the final real-account check (Task 10, Step 5) does.

1. **Tailscale and `tailscale serve`** working for the deployment. The OAuth callback lands in the owner's browser at the Lararium URL, which is only reachable over the tailnet.
2. **A Monzo OAuth client** at developers.monzo.com, type **Confidential**. Its redirect URL is `<LARARIUM_PUBLIC_BASE_URL>/api/v1/connections/oauth/monzo/callback`.
3. **A Cloudflare Tunnel** with a public hostname (e.g. `hooks.<domain>`) pointing at `http://webhook-in:8000`, plus a WAF custom rule blocking everything except `POST` to `/monzo/*`.
4. **A Feishu custom bot** in a group, with signature verification turned on: a webhook URL plus a signing secret.

## Decisions

Choices the spec leaves open, made here so the tasks don't have to:

1. **Facts from the Monzo docs this plan relies on** (docs.monzo.com, checked 2026-11-12):
   - **OAuth.** Authorise at `https://auth.monzo.com/`; exchange and refresh at `POST https://api.monzo.com/oauth2/token`. Only confidential clients get refresh tokens. A refresh token is single-use: each refresh returns a new pair and invalidates the old one. Access tokens last about 6 hours (`expires_in`).
   - **Approval.** A new token does nothing until the owner approves it in the Monzo app (SCA). For **5 minutes after that**, all history can be fetched; afterwards only the last 90 days.
   - **Endpoints.** `GET /accounts`, `GET /balance?account_id=`, `GET /pots?current_account_id=`, and `GET /transactions?account_id=&since=&before=&limit=` (`limit` at most 100; `since` takes a timestamp or a transaction id; `expand[]=merchant`). Webhooks: `POST`, `GET` and `DELETE /webhooks`.
   - **Amounts.** Integers in minor units; negative is money out. `settled == ""` means not yet settled. `decline_reason` is present on declined transactions.
   - **Webhooks.** Only `transaction.created` exists. Deliveries are unsigned and retried up to 5 times.
   - **Not documented, so verified against the real account in Task 10:** counterparty fields, how pot moves appear (`metadata.pot_id` is expected), `local_amount`/`local_currency`, what happens to a pending payment that is reversed.
2. **Worker.** Procrastinate in its own schema `procrastinate`, created by an Alembic migration from Procrastinate's bundled schema SQL. Jobs:
   - `sync_connection(connection_id)`, with `queueing_lock=f"sync:{id}"` and `lock=f"sync:{id}"`, so at most one sync per connection is queued and one runs;
   - `schedule_syncs`, periodic every 30 minutes, which defers a sync for every connection in `healthy` status;
   - `process_webhook_inbox`, periodic every minute;
   - `await_approval(connection_id)`, after the OAuth callback (Decision 7).

   Webhook-triggered syncs therefore start within about a minute.
3. **Credentials** (spec §11.2).
   - **Encryption.** AES-256-GCM with a 32-byte master key from `LARARIUM_MASTER_KEY` (base64), used only in `core/secrets.py`. The associated data is `f"{connection_id}:{purpose}"`, so a ciphertext can't be moved to another connection. Each row records `key_version` (1) so the key can be rotated later.
   - **Handling plaintext.** Decrypted values travel in a `Secret` wrapper whose `repr` and `str` are `"Secret(***)"`. Code calls `.reveal()` only at the HTTP call site.
   - **Never shown.** Credentials never appear in logs, events, audit args, API responses or notifications.
4. **Token rotation is crash-safe.** Because refresh tokens are single-use, the new token pair is encrypted and committed in its **own short transaction** before it is used. If anything later fails, the next run still holds a valid refresh token. A refresh that Monzo rejects (`invalid_grant`, or 401 on the token endpoint) moves the connection to `needs_reauth` and notifies the owner.
5. **Ledger mapping.**
   - **Accounts.** Each Monzo account becomes a finance account: `asset`/`bank`, `source_kind='connection'`, balance mode `transactions`, named from `description` (`uk_retail` → "Monzo", `uk_retail_joint` → "Monzo Joint"). Each pot becomes a child account: `asset`/`savings_pot`, balance mode `snapshots`, fed by an institution-reported snapshot of `pots[].balance` on every sync. A deleted pot is closed.
   - **Transactions.** Each non-declined transaction becomes one ledger transaction, keyed by `(connection_id, external_id = transaction id)`:
     - `occurred_at = created`, `booking_date` = the local date of `created`, `value_date` = the local date of `settled` (or `None`);
     - status `pending` while `settled == ""`, else `booked`;
     - description: the merchant name if there is a merchant, else the counterparty name, else `description`; `memo = notes`.
   - **Postings.** The Monzo account `amount/10^decimals` against one of these:
     - for a **pot move** (`metadata.pot_id` present): the pot's account;
     - otherwise: the system account **"Unassigned"**, which transfer matching (M4) will later re-point.
   - **Declined transactions** are not posted; their raw record is kept.
   - **Merchants** become `organization` entities via `get_or_create_organization`, with `external_ids["monzo_merchant"]` set, and are used as the counterparty.
6. **Upstream changes update the same ledger transaction.** A transaction that changes upstream (it settles, its amount changes, its description is enriched) has its fields and postings **replaced in place**: same transaction id, new posting rows. The update emits `finance.transaction.updated`.
   - This is allowed only for `origin='connector'` transactions; manual ones are never touched.
   - A pending transaction that comes back with `amount == 0` or a `decline_reason` is voided.

   Replacing rather than void-and-repost keeps one ledger row per Monzo transaction, which is what "single source of truth" means here.
7. **OAuth flow.**
   - `authorize_connection` stores `sha256(state)` and `expires_at = now + 10 min` in the connection's config and returns the Monzo URL.
   - The callback route exchanges the code, stores the tokens, sets status `authorising`, defers `await_approval`, and redirects (303) to `/connections`.
   - `await_approval` polls `GET /accounts` every 10 s for up to 4 minutes. On the first success it immediately runs a **full backfill** (no `since`) inside the 5-minute window, then sets `healthy`. On timeout it sets `needs_reauth` with the message "Approve the Monzo request in the app within 4 minutes, then authorise again".
   - **The callback is a cross-site navigation** coming from auth.monzo.com, which plan 1's cross-site rule refuses. This one route is exempt when the request is a top-level navigation (`Sec-Fetch-Mode: navigate` and `Sec-Fetch-Dest: document`). It still requires the owner header and a matching, unexpired, single-use `state`.
8. **Sync window and truncation checks.**
   - **Incremental sync** fetches transactions from `last_seen_created − 14 days` (the overlap catches pending ones that settle or change), paging with `since=<last id>` and `limit=100`.
   - **Outside the approval window**, a `since` older than 89 days is clamped to 89 days, and the clamp is recorded as a connection warning.
   - **After the full backfill**, if the earliest transaction is more than 7 days after the account's `created`, the connection gets the warning "history may be truncated".
9. **Opening balance and reconciliation.**
   - **Opening balance, set once.** After the first full backfill of an account, the ledger balance (including pending, excluding void) is compared with `GET /balance` `balance`. Any difference is posted once as an "Opening balance adjustment" transaction (`external_id = f"opening:{monzo_account_id}"`), booked the day before the earliest transaction, against the system account **"Opening balances"**. It is never silently changed after that.
   - **Reconciliation on every later sync.** A difference larger than the currency's tolerance is a **reconciliation mismatch**: it is recorded as a connection warning and sent to Feishu once per new value, never auto-corrected.
   - **Snapshots.** The balance is also stored as an institution-reported snapshot, for the M9 reconciliation views.
10. **System accounts** are generic: `get_or_create_system_account(ctx, key)` with key `"unassigned"` → "Unassigned" or `"opening_balances"` → "Opening balances". Both are `equity`/`other`/`transactions`, remembered in the settings `finance.system_account.<key>`. They never affect net worth. When the deferred M2 plan is resumed, it reuses these instead of its own "Unassigned (imports)".
11. **Webhook receiver.** It is a separate Starlette app, `lararium.webhook_in`, running from the same image with its own database role `lararium_webhook_in`. That role has only `USAGE` on schema `core` and `INSERT` on `core.webhook_inbox` (no `SELECT`, so no `RETURNING`).
    - **Its one route.** `POST /monzo/{secret}` compares `secret` with `LARARIUM_WEBHOOK_SECRET` using `hmac.compare_digest`. A wrong secret gets 404. A body over 64 KiB gets 413, and a body that is not valid JSON gets 400. Otherwise it inserts `(source='monzo', body)` and returns 200 with no body.
    - **Deployment.** The receiver and `cloudflared` run under the compose profile `webhook`, so a deployment without a domain simply leaves them out and relies on the 30-minute schedule.
12. **Webhook registration** happens on every sync when `LARARIUM_WEBHOOK_BASE_URL` is set. For each Monzo account, the sync lists the webhooks, deletes those that start with our base URL but don't match the current one, and registers `f"{base}/monzo/{secret}"` if it is missing.
13. **Notifications** go through a `Notifier` interface; the only implementation is Feishu.
    - **Format.** A custom-bot text message: `{"msg_type": "text", "content": {"text": ...}}`. With a secret, it also carries `timestamp` and `sign = base64(HMAC-SHA256(key=f"{timestamp}\n{secret}", msg=b""))`.
    - **When.** A connection enters `needs_reauth`; a sync fails 3 times in a row; a connection recovers from failing; a reconciliation mismatch appears; the approval window expires.
    - **Content.** Messages name the connection and the problem. They may include balance differences but never credentials.
    - **Failure.** Sending failures are logged and never fail the job.
    - **Configuration.** `LARARIUM_FEISHU_WEBHOOK_URL` and `LARARIUM_FEISHU_SECRET` live in `infra/.env`. Neither set means a no-op notifier.
14. **Connection lifecycle** (spec §7.1): `created` → `authorising` → `healthy` ⇄ `failing` → `needs_reauth` → (re-authorise) `authorising`; also `disabled`. `failing` is a status this plan adds between `healthy` and `needs_reauth`, for errors that aren't about credentials. Each connection records `last_success_at`, `last_error`, `consecutive_failures`, `credential_expires_at`, and a list of `warnings`.

**Not in this plan:** transfer matching and inference (M4); pairing pot moves beyond Monzo's own pots; healthchecks heartbeat and Apprise (Feishu only); regenerating the ledger from raw records (the normalizer is deterministic and versioned so it can be done later); other connectors.

## Global Constraints

- Everything in plan 1's Global Constraints still applies:
  - Python 3.13; uv with the lockfile enforced; ruff; pyright strict on `src/`; import-linter;
  - PostgreSQL 18;
  - `NUMERIC`/`Decimal`/strings, and the word `float` nowhere in `src/`;
  - UUIDv7; UTC `timestamptz` plus separate `date`;
  - English UI copy; bilingual docs; AGPL-3.0-compatible dependencies only.
- Branch `feat/claude-connectors-monzo`; commit messages `<type>: <summary>`; sign commits with the `local-signed-git-commit` skill.
- **Layering.** `core` stays finance-agnostic. Connections, credentials, raw records, the inbox and the notifier live in `core`; everything Monzo-specific lives in `lararium.modules.finance.monzo`. The worker and the webhook receiver are top-level packages (`lararium.worker`, `lararium.webhook_in`). Add import-linter contracts: `lararium.webhook_in` may import only `lararium.config` and `lararium.db` from the project, and `core` still imports no module.
- **Synthetic data only.** No real Monzo data in the repository: test fixtures are hand-written from the docs' shapes. Responses captured from the owner's account during Task 10 go to `private/monzo-samples/` only.
- **All writes are audited.** Every ledger write goes through `command_context`. Worker jobs use `Actor("connector", "monzo")`; the API uses the owner actor.
- **Network.** No outbound HTTP in tests: Monzo and Feishu calls go through `httpx.AsyncClient` with an injectable transport (`httpx.MockTransport` in tests).
- **Secrets.** `LARARIUM_MASTER_KEY`, `LARARIUM_WEBHOOK_SECRET` and the Feishu values are secrets. Settings fields holding them use pydantic `SecretStr`.

## Review Focus

Inputs the spec implies but the main-path tests would not hit, most likely first. Each has a test in its owning task:

- **A card payment that settles for a different amount** (a hotel, petrol): the same ledger transaction is updated; there is no second transaction (Task 6: `test_pending_settles_with_new_amount`).
- **A crash right after a token refresh:** the new refresh token is already committed, so the next run still works (Task 4: `test_refreshed_tokens_committed_before_use`).
- **The owner approves in the app too late:** the connection ends `needs_reauth` with a clear message, and nothing half-synced is left without its opening balance (Task 7: `test_approval_window_expired`).
- **A pot move:** the main account goes down and the pot account up; net worth is unchanged, and the pot's balance comes from its snapshot (Task 6: `test_pot_move_pairs_directly`).
- **Forged, oversized or duplicated webhooks:**
  - a wrong secret gives 404 and writes nothing;
  - more than 64 KiB gives 413;
  - two deliveries for one transaction cause at most one queued sync.

  (Task 8: `test_wrong_secret_404`, `test_body_too_large_413`, `test_duplicate_deliveries_one_sync`.)

---

## File Structure

```
backend/
  migrations/versions/
    0008_core_connectors.py          # Task 1: connections, credentials, raw_records, webhook_inbox
    0009_procrastinate.py            # Task 2
    0010_finance_connector_refs.py   # Task 3: transactions.connection_id/external_id, accounts FK
  src/lararium/
    config.py                        # + master_key, public_base_url, webhook_*, feishu_* (SecretStr)
    core/
      secrets.py                     # Task 1: AES-GCM, Secret
      connections.py                 # Task 1: connection CRUD + lifecycle, credential store
      raw_records.py                 # Task 1
      connectors.py                  # Task 1: Connector protocol, SyncContext, registry
      notify.py                      # Task 2: Notifier, FeishuNotifier, NullNotifier
    worker/
      __init__.py, app.py, jobs.py   # Task 2 (+ Monzo jobs wired in Tasks 7, 8)
      __main__.py                    # `python -m lararium.worker`
    webhook_in/
      __init__.py, app.py            # Task 8
    modules/finance/
      ledger.py                      # + upsert_connector_transaction (Task 3)
      system_accounts.py             # Task 3
      monzo/
        __init__.py
        client.py                    # Task 4: API client + OAuth token calls
        auth.py                      # Task 4/7: token storage, refresh, authorize URL, callback
        sync.py                      # Task 5: connector (API → raw records)
        normalize.py                 # Task 6: raw records → ledger
        webhooks.py                  # Task 8: registration, inbox → sync
      connections_api.py             # Task 7: /api/v1/connections routes
  tests/
    core/test_secrets.py, test_connections.py, test_notify.py
    worker/test_runner.py
    finance/monzo/fixtures.py, test_client.py, test_sync.py, test_normalize.py, test_webhooks.py
    finance/test_connector_ledger.py
    api/test_connections_api.py
    webhook_in/test_receiver.py
web/src/pages/ConnectionsPage.tsx (+ .test.tsx), router.tsx
infra/compose.yaml, env.example, postgres/roles.sql, postgres/init/00-roles.sh, README*.md     # Task 10
```

---

### Task 1: Core connector framework

**Files:**
- Create: `0008_core_connectors.py`, `core/secrets.py`, `core/connections.py`, `core/raw_records.py`, `core/connectors.py`, `core/notify.py` (the `Notifier` protocol and `NullNotifier` only; Task 2 adds Feishu), `tests/core/test_secrets.py`, `tests/core/test_connections.py`
- Modify: `config.py`, `core/models.py`, `core/errors.py`, `pyproject.toml`/`uv.lock` (add `cryptography`; move `httpx` from dev to runtime)

**Interfaces:**
- Produces:
  - **`core/secrets.py`:**
    - `class Secret`, with `reveal() -> str`; `repr`/`str` give `"Secret(***)"`.
    - `def encrypt(master_key: bytes, plaintext: str, aad: str) -> tuple[bytes, bytes]` returns `(nonce, ciphertext)`, with a 12-byte random nonce.
    - `def decrypt(master_key: bytes, nonce: bytes, ciphertext: bytes, aad: str) -> Secret` raises `CredentialDecryptError` on any failure.
    - `def load_master_key(value: SecretStr) -> bytes` requires base64 of exactly 32 bytes.
  - **`Settings` additions:** `master_key: SecretStr | None = None`, required when `auth_mode == "tailscale"` (same validator style as `owner_login`); `public_base_url: str | None = None`.
  - **`core/connections.py`:**
    - `ConnectionStatus = Literal["created", "authorising", "healthy", "failing", "needs_reauth", "disabled"]`.
    - `@dataclass(frozen=True) class ConnectionRecord`: `id, connector: str, display_name: str, status, config: dict[str, JSONValue], cursor: JSONValue, last_success_at, last_error: str | None, consecutive_failures: int, credential_expires_at, warnings: list[str], created_at`.
    - `create_connection(ctx, *, connector, display_name, config) -> ConnectionRecord`
    - `get_connection(session, id)`, `list_connections(session)`
    - `set_status(ctx, id, status, *, error: str | None = None) -> ConnectionRecord`: a change emits `core.connection.status_changed` with `{from, to}`.
    - `record_sync_success(ctx, id, cursor)`, `record_sync_failure(ctx, id, error) -> int`, which returns `consecutive_failures`.
    - `set_warnings(ctx, id, warnings)`, `update_config(ctx, id, config)`.
    - `store_credential(ctx, master_key, connection_id, purpose: str, value: str, expires_at: datetime | None)` and `load_credential(session, master_key, connection_id, purpose) -> Secret | None`. Purposes used later: `"client_secret"`, `"access_token"`, `"refresh_token"`.
  - **`core/raw_records.py`:** `store_raw_record(ctx, *, connection_id, record_type: str, request: dict[str, JSONValue], content: JSONValue, connector_version: str, sync_id: UUID) -> UUID`. It hashes the canonical JSON (`json.dumps(..., sort_keys=True, separators=(",", ":"))`) and skips the insert when `(connection_id, record_type, content_hash)` already exists, returning the existing id. Also `list_raw_records(session, sync_id) -> list[RawRecord]`.
  - **`core/notify.py`:** `class Notifier(Protocol)` with `async def send(self, text: str) -> None`, and `class NullNotifier`.
  - **`core/connectors.py`:**
    - `Capability = Literal["balances", "holdings", "transactions", "webhook", "device_push", "outbound_actions"]`.
    - `class Connector(Protocol)`: `name: str`, `version: str`, `capabilities: frozenset[Capability]`, `async def sync(self, ctx: SyncContext) -> JSONValue`, which returns the new cursor.
    - `@dataclass class SyncContext`: `command: CommandContext, connection: ConnectionRecord, sync_id: UUID, master_key: bytes, http: httpx.AsyncClient, notifier: Notifier`.
    - The registry: `ModuleSpec` gains `connectors: Mapping[str, Connector] = {}` (keep the default so existing modules don't change), and `registry.connector(name) -> Connector`.

Migration `0008`:
- `core.connections`. Columns: `id`; `connector text NOT NULL`; `display_name text NOT NULL`; `status text NOT NULL CHECK IN (...)`; `config jsonb NOT NULL DEFAULT '{}'`; `cursor jsonb`; `last_success_at`; `last_error text`; `consecutive_failures int NOT NULL DEFAULT 0 CHECK (>= 0)`; `credential_expires_at`; `warnings jsonb NOT NULL DEFAULT '[]'`; `owner_entity_id`; `created_at`; `updated_at`.
- `core.credentials`. Columns: `connection_id` FK `ON DELETE CASCADE`; `purpose text`; `nonce bytea NOT NULL`; `ciphertext bytea NOT NULL`; `key_version int NOT NULL DEFAULT 1`; `expires_at`; `updated_at`; with `PRIMARY KEY (connection_id, purpose)`.
- `core.raw_records`. Columns: `id`; `connection_id` FK; `sync_id uuid NOT NULL`; `record_type text`; `request jsonb`; `fetched_at timestamptz DEFAULT now()`; `content jsonb NOT NULL`; `content_hash text NOT NULL`; `connector_version text NOT NULL`; with `UNIQUE (connection_id, record_type, content_hash)`. Then `REVOKE UPDATE, DELETE ON core.raw_records FROM lararium_backend` (raw data is immutable).
- `core.webhook_inbox`. Columns: `id uuid DEFAULT uuidv7()`; `source text NOT NULL`; `body jsonb NOT NULL`; `received_at DEFAULT now()`; `processed_at timestamptz`; `outcome text`.
- `ALTER TABLE finance.accounts ADD FOREIGN KEY (source_connection_id) REFERENCES core.connections`. This one statement touches a finance table from a core migration; it is acceptable because the plan 1 migration left a comment promising it, and migrations aren't subject to import-linter.

- [ ] **Step 1: Write the failing tests:**
  - `test_encrypt_round_trip_and_aad_binding`: decrypting with a different `aad` raises `CredentialDecryptError`.
  - `test_secret_never_reprs_plaintext`: `repr`, `str` and an f-string of a `Secret` don't contain the value.
  - `test_master_key_must_be_32_bytes`.
  - `test_credential_store_round_trip`: the plaintext is not present in the `ciphertext` bytes; `store_credential` for an existing purpose replaces it.
  - `test_status_change_emits_event_once`: setting the same status twice emits one event.
  - `test_failure_counter_resets_on_success`.
  - `test_raw_record_dedup_by_content`: storing the same content twice returns the same id; the backend role can't `UPDATE` or `DELETE` `core.raw_records`.
  - `test_settings_require_master_key_in_tailscale_mode`.
- [ ] **Step 2: Run** `uv run --locked pytest tests/core -q`; expected: the new tests fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make backend-check`; expected: green.
- [ ] **Step 5: Commit** `feat: add connections, encrypted credentials and raw records to core`.

### Task 2: Worker and notifications

**Files:**
- Create: `0009_procrastinate.py`, `worker/app.py`, `worker/jobs.py`, `worker/__main__.py`, `tests/worker/test_runner.py`, `tests/core/test_notify.py`
- Modify: `config.py` (+ `feishu_webhook_url: SecretStr | None`, `feishu_secret: SecretStr | None`), `core/notify.py` (add Feishu), `pyproject.toml`/`uv.lock` (add `procrastinate`)

**Interfaces:**
- Consumes: Task 1.
- Produces:
  - **`core/notify.py`:**
    - `Notifier` and `NullNotifier` exist from Task 1; this task adds the rest.
    - `class FeishuNotifier(url: SecretStr, secret: SecretStr | None, http: httpx.AsyncClient)` signs per Decision 13. A non-200 status, or a body whose `code != 0`, is logged at warning level without the URL, and the call returns normally.
    - `def notifier_from_settings(settings, http) -> Notifier`.
  - **`worker/app.py`:** `procrastinate_app` (a `PsycopgConnector` on `LARARIUM_DATABASE_URL`, converted to a psycopg DSN).
  - **`worker/jobs.py`:**
    - `async def run_sync(session, connection_id, *, settings, http, notifier) -> None`, the testable core of `sync_connection`. It:
      1. opens a `command_context` as `Actor("connector", <connector name>)`;
      2. calls `connector.sync`;
      3. records success and the cursor;
      4. on an exception: rolls back, records the failure in a fresh `command_context`, sets status `failing`, and notifies once the failure count reaches 3. A `CredentialRejectedError` instead sets `needs_reauth` and notifies right away;
      5. on success after `failing`, sets `healthy` and sends a "recovered" message.
    - The job `sync_connection(connection_id: str)` with the locks from Decision 2.
    - The periodic `schedule_syncs` (`*/30 * * * *`).
    - The periodic `process_webhook_inbox` (`* * * * *`), wired in Task 8.
  - **`core/errors.py`:** `CredentialRejectedError`, raised by connectors when the provider rejects the credentials.
  - **`python -m lararium.worker`** runs the Procrastinate worker with the periodic tasks.

Migration `0009`: apply `procrastinate.schema.SchemaManager.get_schema()` inside schema `procrastinate`, which the owner creates. Grant `lararium_backend` usage, and `SELECT`, `INSERT`, `UPDATE` and `DELETE` on its tables, plus `EXECUTE` on its functions. If the bundled SQL assumes `public`, run it with `SET search_path TO procrastinate` and say so in the migration docstring.

- [ ] **Step 1: Write the failing tests:**
  - `test_feishu_payload_and_signature`: with a fixed `timestamp=1700000000` and secret `s3cret`, the posted JSON has `msg_type`, `content.text`, `timestamp` and the `sign` computed by the test with the same formula.
  - `test_feishu_failure_does_not_raise`: a 500 response and a `{"code": 19021}` body each return normally.
  - `test_run_sync_success_records_cursor` with a fake connector registered for the test.
  - `test_third_failure_notifies_once`: three failing runs send exactly one message; a fourth sends none; a following success sends "recovered".
  - `test_credential_rejected_sets_needs_reauth`.
  - `test_procrastinate_schema_present`: after migrations, `procrastinate.procrastinate_jobs` exists and the backend role can insert a job.
- [ ] **Step 2: Run**; expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make backend-check`; expected: green.
- [ ] **Step 5: Commit** `feat: add the sync worker and Feishu notifications`.

### Task 3: Ledger support for connector data

**Files:**
- Create: `0010_finance_connector_refs.py`, `modules/finance/system_accounts.py`, `tests/finance/test_connector_ledger.py`
- Modify: `modules/finance/ledger.py`, `modules/finance/accounts.py` (allow `source_kind='connection'` with `source_connection_id`)

**Interfaces:**
- Consumes: plan 1 ledger and accounts; Task 1 connections.
- Produces:
  - **The migration.** `finance.transactions` gains `connection_id uuid REFERENCES core.connections`, `external_id text`, `CHECK ((connection_id IS NULL) = (external_id IS NULL))`, and a unique index on `(connection_id, external_id)`.
  - **`ledger.TransactionInput`** gains `connection_id: UUID | None = None` and `external_id: str | None = None`, both written by `post_transaction`.
  - **`ledger.upsert_connector_transaction(ctx, tx: TransactionInput) -> tuple[TransactionRecord, Literal["created", "updated", "unchanged"]]`** requires `tx.origin == "connector"` and both refs.
    - If no transaction has these refs, it posts.
    - If one exists with the same fields and postings, it returns `"unchanged"` and writes nothing.
    - Otherwise it validates the new postings as `post_transaction` does, then updates the row and replaces its postings: delete, then insert. It emits `finance.transaction.updated` and calls `ctx.record_change` with the before and after.
    - It refuses (`FinanceValidationError`) when the existing row's origin is not `connector`.
  - **`accounts.create_account`** gains `source_connection_id: UUID | None = None`; given one, it sets `source_kind='connection'`.
  - **`system_accounts.get_or_create_system_account(ctx, key: Literal["unassigned", "opening_balances"]) -> UUID`**, per Decision 10.

- [ ] **Step 1: Write the failing tests:**
  - `test_upsert_creates_then_unchanged`.
  - `test_upsert_replaces_postings_in_place`: the id is kept, the old posting rows are gone, and the balance reflects the new amount.
  - `test_upsert_refuses_manual_rows`.
  - `test_refs_must_be_paired`: the CHECK holds at the database level.
  - `test_system_accounts_reused_and_excluded_from_net_worth`.
- [ ] **Step 2: Run**; expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make backend-check`; expected: green.
- [ ] **Step 5: Commit** `feat: let connectors upsert ledger transactions by external id`.

### Task 4: Monzo API client and tokens

**Files:**
- Create: `monzo/__init__.py`, `monzo/client.py`, `monzo/auth.py`, `tests/finance/monzo/fixtures.py`, `tests/finance/monzo/test_client.py`

**Interfaces:**
- Consumes: Task 1 (`store_credential`, `load_credential`, `CredentialRejectedError`).
- Produces:
  - **`monzo/client.py`:**
    - `API = "https://api.monzo.com"`, `AUTH = "https://auth.monzo.com/"`.
    - `class MonzoClient(http: httpx.AsyncClient, access_token: Secret)` with:
      - `whoami() -> dict`
      - `accounts() -> list[dict]`
      - `balance(account_id) -> dict`
      - `pots(account_id) -> list[dict]`
      - `transactions(account_id, *, since: str | None, before: str | None = None) -> AsyncIterator[list[dict]]`: yields pages of up to 100 with `expand[]=merchant`, advancing `since` to the last id of each page, and stopping on a page shorter than 100.
      - `webhooks(account_id) -> list[dict]`, `register_webhook(account_id, url) -> dict`, `delete_webhook(webhook_id)`
    - `MonzoApiError(status, code)`. A 401 with `error` in `("invalid_token", "unauthorized")` raises `AccessTokenExpiredError`. 403 before approval raises `NotYetApprovedError`. 429 raises `MonzoRateLimitedError`. Messages never include tokens.
  - **`monzo/auth.py`:**
    - `def authorize_url(client_id: str, redirect_uri: str, state: str) -> str`.
    - `async def exchange_code(http, client_id, client_secret: Secret, redirect_uri, code) -> TokenPair`.
    - `async def refresh(http, client_id, client_secret: Secret, refresh_token: Secret) -> TokenPair`.
    - `@dataclass TokenPair(access_token: Secret, refresh_token: Secret, expires_at: datetime)`.
    - `async def save_tokens(session_factory, master_key, connection_id, pair)`, which commits in its **own session and transaction** (Decision 4).
    - `async def access_token_for(sync_ctx) -> Secret`, which refreshes when fewer than 5 minutes remain and calls `save_tokens` before returning. `invalid_grant` raises `CredentialRejectedError`.

- [ ] **Step 1: Write the failing tests** using `httpx.MockTransport` handlers and the fixture builders in `fixtures.py`. The builders are `make_account`, `make_transaction(**overrides)`, `make_pot` and `make_balance`, with synthetic ids like `acc_TEST1` and `tx_TEST…`.
  - `test_transactions_paginates_with_since_last_id`: a 250-transaction history gives pages of 100, 100 and 50, and the second request carries `since=<100th id>`.
  - `test_authorize_url_params`: `client_id`, `redirect_uri`, `response_type=code` and `state` are present and URL-encoded.
  - `test_error_mapping`.
  - `test_errors_never_contain_tokens`.
  - `test_refreshed_tokens_committed_before_use`: the refresh succeeds, then the next API call raises. A fresh session still reads the new refresh token.
  - `test_invalid_grant_is_credential_rejected`.
- [ ] **Step 2: Run**; expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make backend-check`; expected: green.
- [ ] **Step 5: Commit** `feat: add the Monzo API client and token handling`.

### Task 5: Monzo sync (API → raw records)

**Files:**
- Create: `monzo/sync.py`, `tests/finance/monzo/test_sync.py`
- Modify: `modules/finance/__init__.py` (register the connector in `MODULE.connectors`)

**Interfaces:**
- Consumes: Tasks 1, 2 and 4.
- Produces:
  - `class MonzoConnector` with `name="monzo"`, `version="monzo-sync-1"`, `capabilities={"balances", "transactions", "webhook"}`.
  - `async def sync(self, ctx: SyncContext) -> JSONValue` stores raw records of types `accounts`, `balance`, `pots` and `transactions_page`, each with its request parameters, and then calls `normalize(ctx)` (Task 6).
  - The cursor shape is `{"accounts": {<monzo_account_id>: {"last_created": str, "backfilled": bool}}}`.
  - `full_backfill: bool` comes from `ctx.connection.config.get("backfill_requested")`. Task 7 sets that flag and the sync clears it.

Rules: Decision 8 (window, overlap, clamp, truncation warning). Closed Monzo accounts (`closed: true` in the response) are still synced, but no webhook is registered for them.

- [ ] **Step 1: Write the failing tests:**
  - `test_first_sync_stores_raw_records_per_endpoint`.
  - `test_incremental_sync_uses_14_day_overlap`: the request's `since` equals `last_created − 14 days`.
  - `test_since_clamped_to_89_days_with_warning`.
  - `test_backfill_truncation_warning`.
  - `test_unchanged_responses_not_stored_twice`.
- [ ] **Step 2: Run**; expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make backend-check`; expected: green.
- [ ] **Step 5: Commit** `feat: sync Monzo accounts, pots, balances and transactions`.

### Task 6: Monzo normalizer (raw records → ledger)

**Files:**
- Create: `monzo/normalize.py`, `tests/finance/monzo/test_normalize.py`

**Interfaces:**
- Consumes: Tasks 3 and 5; `snapshots.record_snapshot` (kind `institution`); `get_or_create_organization`; `get_currency`.
- Produces:
  - `NORMALIZER_VERSION = "monzo-norm-1"`.
  - `async def normalize(ctx: SyncContext) -> NormalizeReport`, with `NormalizeReport(created: int, updated: int, unchanged: int, voided: int, skipped_declined: int, mismatches: list[str])`.
  - `def to_quantity(minor_units: int, decimals: int) -> Decimal`, which is `Decimal(minor_units).scaleb(-decimals)`.

Rules: Decisions 5, 6 and 9. Accounts and pots are found or created by the setting `config["account_map"]`: Monzo id → finance account id, kept in the connection's config.

- [ ] **Step 1: Write the failing tests** (each one stages raw records with the fixture builders, then normalizes):
  - `test_creates_accounts_and_pot_children`: a pot account has the main account as its `parent_account_id`, is in mode `snapshots`, and has a snapshot equal to the pot balance.
  - `test_card_payment_posts_against_unassigned`: `-1234` minor GBP gives `-12.34`; the counterparty is the merchant entity; the status is `booked` when settled.
  - `test_pending_settles_with_new_amount`: a pending `-5000`, then settled `-4250`: one transaction, `updated`, quantity `-42.50`, status `booked`.
  - `test_reversed_pending_is_voided`.
  - `test_declined_not_posted`.
  - `test_pot_move_pairs_directly`: `metadata.pot_id` gives main `-50.00` and pot `+50.00`, with no Unassigned posting.
  - `test_opening_adjustment_once`: after backfill, the history sums to 100.00 but the balance is 250.00, so one adjustment of 150.00 is posted. A second normalize with a 10.00 mismatch posts nothing, adds a warning and notifies once.
  - `test_normalize_is_idempotent`: running twice gives counts of `unchanged` only.
  - `test_booking_date_uses_local_date`: a `created` of `2026-03-29T23:30:00Z` gives booking date `2026-03-30` in `Europe/London` (BST).
- [ ] **Step 2: Run**; expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make backend-check`; expected: green.
- [ ] **Step 5: Commit** `feat: normalize Monzo data into the ledger`.

### Task 7: Connections API and the OAuth flow

**Files:**
- Create: `modules/finance/connections_api.py`, `tests/api/test_connections_api.py`
- Modify: `deps.py` (navigation exemption helper), `modules/finance/__init__.py` (install the router), `worker/jobs.py` (`await_approval`), `web/openapi.json`, `web/src/api/*`

**Interfaces:**
- Consumes: Tasks 1, 2, 4 and 5.
- Produces:
  - **Routes** under `/api/v1/connections`. The route name is the operationId; all routes use `ActorDep` except the callback (see below).
    - `GET ""` `list_connections` → `[ConnectionOut]`. `ConnectionOut` has `id`, `connector`, `display_name`, `status`, `last_success_at`, `last_error`, `consecutive_failures`, `credential_expires_at`, `warnings`, `redirect_uri` and `webhook_enabled: bool`. It never includes credentials.
    - `POST ""` `create_monzo_connection` with body `{display_name, client_id, client_secret}` → 201. The secret is stored encrypted, and audit args omit `client_secret`.
    - `POST "/{id}/authorize"` `authorize_connection` → `{url}`. Decision 7.
    - `GET "/oauth/monzo/callback"` `monzo_oauth_callback` with `?code&state` → a 303 to `/connections`. On an invalid or expired state, a 303 to `/connections?error=state`.
    - `POST "/{id}/sync"` `sync_connection_now` → 202. It defers `sync_connection`.
    - `POST "/{id}/disable"` `disable_connection`.
  - **`deps.get_actor_allowing_navigation`** behaves as `get_actor`, except that it accepts a cross-site request when `method == "GET"`, `Sec-Fetch-Mode == "navigate"` and `Sec-Fetch-Dest == "document"`. Only the callback uses it.
  - **`worker.jobs.await_approval(connection_id)`**, per Decision 7. Before the backfill it sets `config["backfill_requested"] = True`.

- [ ] **Step 1: Write the failing tests:**
  - `test_create_connection_hides_secret`: neither the response nor the audit args contain the secret.
  - `test_authorize_returns_monzo_url_and_stores_state_hash`.
  - `test_callback_navigation_allowed_but_fetch_refused`: with `Sec-Fetch-Site: cross-site`, a request with mode `navigate` and dest `document` gets 303, while one with mode `cors` gets 403.
  - `test_callback_state_single_use_and_expiring`.
  - `test_callback_exchanges_code_and_defers_approval`, with mocked HTTP and the Procrastinate in-memory connector.
  - `test_await_approval_backfills_on_first_success`.
  - `test_approval_window_expired`: no success within 4 minutes (a fake clock) gives `needs_reauth`, the message, one notification, and no ledger rows.
  - `test_connection_operation_ids`.
- [ ] **Step 2: Run**; expected: FAIL.
- [ ] **Step 3: Implement**, then `make openapi`.
- [ ] **Step 4: Run** `make check`; expected: green.
- [ ] **Step 5: Commit** `feat: add connection routes and the Monzo OAuth flow`.

### Task 8: Webhook receiver and inbox processing

**Files:**
- Create: `webhook_in/app.py`, `monzo/webhooks.py`, `tests/webhook_in/test_receiver.py`, `tests/finance/monzo/test_webhooks.py`
- Modify: `config.py` (+ `webhook_secret: SecretStr | None`, `webhook_base_url: str | None`), `worker/jobs.py`, `monzo/sync.py` (call registration), `pyproject.toml` (import-linter contract for `webhook_in`), `infra/postgres/roles.sql` and `infra/postgres/init/00-roles.sh` (role `lararium_webhook_in`), and a new migration `0011_webhook_in_grants.py` granting it `USAGE ON SCHEMA core` and `INSERT ON core.webhook_inbox`. The migration skips with a notice when the role doesn't exist, so the test container, which creates the role in `roles.sql`, and older databases both work.

**Interfaces:**
- Produces:
  - **`webhook_in/app.py`:** `def create_app(database_url: str, secret: SecretStr) -> Starlette` and a module-level `app` built from env. Behaviour per Decision 11. It uses a plain `psycopg` async connection pool (no SQLAlchemy models) for one parameterised `INSERT`.
  - **`monzo/webhooks.py`:**
    - `async def ensure_webhooks(client, account_ids, base_url, secret)`, per Decision 12.
    - `async def process_inbox(session, *, defer) -> int`. It claims unprocessed rows with `FOR UPDATE SKIP LOCKED`, maps `body["data"]["account_id"]` to a connection through `config["account_map"]`, calls `defer(connection_id)` (the `queueing_lock` drops duplicates), and sets `processed_at` and `outcome` (`queued`, `duplicate`, `unknown_account` or `bad_body`). The body is never written to the ledger.

- [ ] **Step 1: Write the failing tests:**
  - `test_valid_delivery_inserted_200`.
  - `test_wrong_secret_404`: nothing is inserted.
  - `test_body_too_large_413`.
  - `test_invalid_json_400`.
  - `test_get_is_405`.
  - `test_receiver_role_cannot_select`: connected as `lararium_webhook_in`, `SELECT` on `core.webhook_inbox` and on `finance.accounts` fails.
  - `test_duplicate_deliveries_one_sync`.
  - `test_unknown_account_marked`.
  - `test_ensure_webhooks_replaces_stale_and_registers_missing`.
- [ ] **Step 2: Run**; expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make backend-check`; expected: green.
- [ ] **Step 5: Commit** `feat: receive Monzo webhooks through a write-only inbox`.

### Task 9: Web UI — Connections page

**Files:**
- Create: `web/src/pages/ConnectionsPage.tsx`, `ConnectionsPage.test.tsx`
- Modify: `web/src/router.tsx` (route `/connections`, and the nav item "Connections" after "Prices")

Behaviour:
- **A list of connections:** name; status badge; last successful sync as a relative time with the exact time in a tooltip; last error; warnings.
- **Buttons per connection:**
  - "Sync now";
  - "Authorise" (status `created` or `needs_reauth`). It calls `authorizeConnection` and then sets `window.location.href` to the returned URL;
  - "Disable".
- **While `authorising`:** a prominent notice "Open the Monzo app and approve the request now. Lararium has 4 minutes to fetch your full history." The page polls `listConnections` every 5 s until the status changes.
- **"Add Monzo" form:** display name, client ID, client secret (a password field). The help text shows the exact `redirect_uri` from the server, to paste into developers.monzo.com.
- **`?error=state`** on the URL shows "The authorisation link expired or was already used. Click Authorise again."

- [ ] **Step 1: Write the failing tests:**
  - `test_add_form_sends_secret_and_clears_it`.
  - `test_authorise_redirects_to_returned_url`, with `window.location` stubbed.
  - `test_authorising_notice_and_polling`, with fake timers.
  - `test_state_error_banner`.
- [ ] **Step 2: Run** `cd web && pnpm test --run`; expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `make check`; expected: green.
- [ ] **Step 5: Commit** `feat: add the Connections page`.

### Task 10: Deployment, docs and the real-account check

**Files:**
- Modify: `infra/compose.yaml`, `infra/env.example`, `backend/tests/infra/test_compose.py`, `README.md`, `README.zh-CN.md`, `Makefile` (`make worker-logs` is optional; skip it if unneeded)

Changes:
- **Compose:**
  - `worker`: the backend image; `command: ["python", "-m", "lararium.worker"]`; the same database URL and settings as `api` plus the Feishu variables; `restart: unless-stopped`; depends on `migrate` having completed.
  - `webhook-in`, under `profiles: ["webhook"]`: the backend image; `command: ["uvicorn", "lararium.webhook_in.app:app", "--host", "0.0.0.0", "--port", "8000"]`; `LARARIUM_DATABASE_URL` uses the role `lararium_webhook_in`; `LARARIUM_WEBHOOK_SECRET`; no published ports.
  - `cloudflared`, under `profiles: ["webhook"]`: `cloudflare/cloudflared:<pinned version>`; `command: ["tunnel", "--no-autoupdate", "run"]`; `TUNNEL_TOKEN: ${CLOUDFLARE_TUNNEL_TOKEN:?…}`; no ports.
  - `api` gains `LARARIUM_MASTER_KEY`, `LARARIUM_PUBLIC_BASE_URL` and `LARARIUM_WEBHOOK_BASE_URL`; `worker` gains those too, plus the Feishu variables.
- **`env.example`:** every new variable with generation hints:
  - `LARARIUM_MASTER_KEY`: `openssl rand -base64 32`;
  - `LARARIUM_WEBHOOK_SECRET`: `openssl rand -hex 32`;
  - `LARARIUM_WEBHOOK_IN_PASSWORD`: `openssl rand -hex 32`;
  - plus `CLOUDFLARE_TUNNEL_TOKEN`, `LARARIUM_FEISHU_WEBHOOK_URL`, `LARARIUM_FEISHU_SECRET`, `LARARIUM_PUBLIC_BASE_URL` and `LARARIUM_WEBHOOK_BASE_URL`.

  It also warns that losing `LARARIUM_MASTER_KEY` means re-entering every credential and re-authorising.
- **README, both languages:** a section "Connecting Monzo":
  - creating the OAuth client, with the redirect URI;
  - the 4-minute approval rule;
  - what syncs, and what "Unassigned" and "Opening balances" are.

  A section "Webhooks through Cloudflare Tunnel":
  - creating the tunnel and the public hostname → `http://webhook-in:8000`;
  - the WAF rule expression `not (http.request.method eq "POST" and starts_with(http.request.uri.path, "/monzo/"))` → Block;
  - `docker compose --profile webhook up -d`.

  A section "Feishu notifications": a custom bot with signature verification. In the security section: the receiver role is write-only, and Cloudflare sees webhook payloads in plaintext but they are only used as a "sync now" signal. For an **existing database**, the one-time SQL to create `lararium_webhook_in`, since `roles.sql` runs only on a fresh volume.

- [ ] **Step 1: Write the failing tests:**
  - `test_compose.py`:
    - `worker` exists with the worker command;
    - `webhook-in` and `cloudflared` are only in the `webhook` profile and publish no ports;
    - `webhook-in` connects as `lararium_webhook_in`;
    - no service except `web` publishes a port.
- [ ] **Step 2: Run**; expected: FAIL.
- [ ] **Step 3: Implement**; write both READMEs.
- [ ] **Step 4: Verify.**
  - `make check` passes.
  - Compose smoke test: `make up` (without the profile) starts `worker`, and its logs show the periodic tasks registered.
  - `docker compose --profile webhook up -d webhook-in`. A `curl -X POST` from inside the compose network to `/monzo/<secret>` with `{}` gives 200, and the worker marks the row `bad_body` within a minute; a wrong secret gives 404.
- [ ] **Step 5: Real-account check (the owner, with the implementer guiding).** Once the prerequisites at the top are in place:
  1. Create the connection.
  2. Authorise it and approve in the app within 4 minutes.
  3. Confirm that the accounts, pots and the opening adjustment appear, and that the Monzo account's balance in Lararium equals the app's.
  4. Make a small card payment and confirm it arrives within about a minute through the webhook.
  5. Save one anonymised response per endpoint to `private/monzo-samples/` (gitignored).
  6. Compare the counterparty, `metadata.pot_id` and pending-reversal fields with the fixtures. If they differ, fix the fixtures and normalizer **with synthetic data** in a follow-up commit `fix: align Monzo normalizer with real API responses`.
  7. Record the findings in Decision 1.
- [ ] **Step 6: Commit** `feat: deploy the worker and webhook ingress`.

---

## Self-review notes

- **Spec coverage.**
  - §7.1: the connector contract (sync, cursor, capabilities including `device_push` and `outbound_actions`), the lifecycle, the per-connection lock, schedules and backfill. The webhook is only a "sync now" trigger.
  - §7.2: OAuth with a confidential client, backfill inside the window, incremental sync with overlap, the date-range checks, the mapping (accounts, pots as children, balance snapshots, merchants as entities; counterparty data kept in raw records for M4), webhook registration, the re-authorisation reminder and entry point, and the verification items (Task 10, Step 5).
  - §7.9: the receiver with an insert-only role and secret-in-path, returning immediately.
  - §11.1: the `webhook_in` role.
  - §11.2: application-layer encryption with a master key from the environment, nothing in logs, events or responses. (SOPS stays a deployment choice, as in plan 1.)
  - §11.5: notifications, through Feishu instead of Apprise (the owner's choice).
- **Types.**
  - `SyncContext` (Task 1) is used by Tasks 5, 6 and 8.
  - `upsert_connector_transaction` (Task 3) is used by Task 6.
  - `CredentialRejectedError` (Task 1) is raised in Task 4 and handled in Task 2.
  - `config["account_map"]` is written in Task 6 and read in Task 8.
  - `backfill_requested` is set in Task 7 and read in Task 5.
