#!/usr/bin/env bash
# Usage: rehearse-restore.sh <docker context> <dump file> <documents tar>
#
# Rehearses the restore in the README ("Backup and restore") on a throwaway PostgreSQL in the
# given Docker context, which must not be the production host, then checks the copy:
#   1. restore.sh restores the dump into a new database, and the names are swapped as the
#      README does it;
#   2. every table holds as many rows as the dump has for it (counted from the dump itself);
#   3. the documents tar is unpacked into a throwaway volume as the README does it, and every
#      document the database refers to is there, readable by the api, with the content its
#      hash names;
#   4. `alembic upgrade head` runs as the stack's migrate service does, then
#      lararium.restore_check checks the revision is the code's head and the ledger invariants
#      hold (every transaction balances, the transfer invariant).
# The database gets no published port: it sits on an internal network (no route out) that
# only the check container joins. Everything the run creates is removed when it ends, however
# it ends, apart from the cached images. Output is table names, counts, hashes and ids only.
set -euo pipefail
umask 077

if [ $# -ne 3 ]; then
  echo "usage: $0 <docker context> <dump file> <documents tar>" >&2
  exit 2
fi
context="$1"
dump="$2"
documents="$3"
for file in "$dump" "$documents"; do
  if [ ! -f "$file" ]; then
    echo "no such file: $file" >&2
    exit 2
  fi
done

infra="$(cd "$(dirname "$0")/.." && pwd)"
root="$(dirname "$infra")"
# Every docker command below goes to $context, restore.sh's included.
unset DOCKER_HOST
export DOCKER_CONTEXT="$context"
docker info --format '{{.Name}}' >/dev/null

name="lararium-rehearsal-$(date -u +%Y%m%dT%H%M%SZ)-$(openssl rand -hex 3)"
db="$name-db"
volume="$name-documents"
network="$name"
image="lararium-rehearsal"
work="$(mktemp -d)"
cleanup() {
  docker rm -f -v "$db" >/dev/null 2>&1 || true
  docker volume rm "$volume" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

password() { openssl rand -hex 16; }
owner_password="$(password)"
failed=0
fail() {
  echo "FAIL $*"
  failed=1
}
sql() { docker exec -i "$db" psql -U postgres -d lararium -v ON_ERROR_STOP=1 -tAX "$@"; }

echo "== starting a throwaway PostgreSQL ($db) on $context"
docker network create --internal --label lararium.rehearsal "$network" >/dev/null
docker create --name "$db" --network "$network" --label lararium.rehearsal \
  -e POSTGRES_PASSWORD="$(password)" \
  -e LARARIUM_OWNER_PASSWORD="$owner_password" \
  -e LARARIUM_BACKEND_PASSWORD="$(password)" \
  -e LARARIUM_WEBHOOK_IN_PASSWORD="$(password)" \
  postgres:18 >/dev/null
# Copied, not bind-mounted: the remote daemon can't see this machine's paths. The same files
# the compose db service mounts: the roles and their init script.
docker cp -q "$infra/postgres/." "$db:/lararium"
docker cp -q "$infra/postgres/init/." "$db:/docker-entrypoint-initdb.d"
docker start "$db" >/dev/null
# As compose's healthcheck: on TCP only once the init scripts have run.
ready() { docker exec "$db" pg_isready -q -h 127.0.0.1 -U postgres -d lararium; }
for _ in $(seq 90); do
  if ready; then break; fi
  sleep 1
done
if ! ready; then
  echo "the throwaway PostgreSQL did not become ready" >&2
  exit 1
fi

echo "== 1. restoring $(basename "$dump") and swapping it in"
LARARIUM_DB_CONTAINER="$db" "$infra/backup/restore.sh" "$dump" lararium_restore
docker exec "$db" psql -U postgres -v ON_ERROR_STOP=1 \
  -c "ALTER DATABASE lararium RENAME TO lararium_old" \
  -c "ALTER DATABASE lararium_restore RENAME TO lararium" >/dev/null
echo "restored revision: $(sql -c 'SELECT version_num FROM alembic_version')"

echo "== 2. row counts against the dump"
# The dump's own count: one line per row between a table's COPY line and its \. line.
docker exec -i "$db" pg_restore --data-only -f - <"$dump" | awk '
  /^COPY / { table = $2; rows = 0; copying = 1; next }
  copying && $0 == "\\." { print table, rows; copying = 0; next }
  copying { rows++ }
' | sort >"$work/dump-counts"
sql -F ' ' >"$work/db-counts" <<'SQL'
SELECT format('SELECT %L, count(*) FROM %I.%I', format('%I.%I', n.nspname, c.relname),
              n.nspname, c.relname)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r' AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\_toast%'
\gexec
SQL
sort -o "$work/db-counts" "$work/db-counts"
if diff -u --label dump --label restored "$work/dump-counts" "$work/db-counts"; then
  awk '{ rows += $2 } END { printf "ok: %d tables, %d rows, as in the dump\n", NR, rows }' \
    "$work/db-counts"
else
  fail "row counts differ from the dump (above)"
fi

# Run by its id: the tag is shared, and a rehearsal of another commit could move it meanwhile.
image_id="$(docker build -q -t "$image" "$root/backend")"

echo "== 3. documents from $(basename "$documents")"
docker volume create --label lararium.rehearsal "$volume" >/dev/null
docker run --rm -v "$volume:/d" -i alpine tar -C /d -xf - <"$documents"
# Each document the database refers to, read as the api reads it: its image and user, its
# volume path, through LocalDocumentStore; one it can't open, or whose content has another
# hash, is printed by its hash.
sql -c 'SELECT sha256 FROM core.documents' >"$work/referenced"
docker run --rm -i -v "$volume:/var/lib/lararium/documents:ro" "$image_id" python -c '
import hashlib, sys
from pathlib import Path
from lararium.core.documents import LocalDocumentStore
store = LocalDocumentStore(Path("/var/lib/lararium/documents"))
for sha in sys.stdin.read().split():
    try:
        intact = hashlib.sha256(store.get(sha)).hexdigest() == sha
    except OSError:
        intact = False
    if not intact:
        print(sha)
' <"$work/referenced" >"$work/bad"
if [ -s "$work/bad" ]; then
  fail "$(wc -l <"$work/bad" | tr -d ' ') referenced documents missing, unreadable by the api or not intact:"
  cat "$work/bad"
else
  echo "ok: all $(wc -l <"$work/referenced" | tr -d ' ') referenced documents are there and intact"
fi

echo "== 4. migrating to head and checking the ledger"
url="postgresql+psycopg://lararium_owner:$owner_password@$db:5432/lararium"
if ! docker run --rm --network "$network" -e LARARIUM_MIGRATION_DATABASE_URL="$url" "$image_id" \
  sh -c 'alembic upgrade head && python -m lararium.restore_check'; then
  fail "migration or ledger check (above)"
fi

if [ "$failed" -ne 0 ]; then
  echo "== restore rehearsal FAILED"
  exit 1
fi
echo "== restore rehearsal passed"
