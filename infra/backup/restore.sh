#!/usr/bin/env bash
# Usage: restore.sh <dump file> <target database name>
# Creates a NEW database owned by lararium_owner and restores the dump into it. It refuses to
# touch an existing database: inspect the copy, then switch over deliberately.
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "usage: $0 <dump file> <target database name>" >&2
  exit 2
fi
dump="$1"
target="$2"
if ! [[ "$target" =~ ^[a-z_][a-z0-9_]*$ ]]; then
  echo "target database name must match ^[a-z_][a-z0-9_]*\$" >&2
  exit 2
fi
if [ ! -f "$dump" ]; then
  echo "no such dump file: $dump" >&2
  exit 2
fi

# LARARIUM_DB_CONTAINER names a container to run in (plain `docker exec`) instead of the
# compose stack's db service: the restore rehearsal's throwaway server.
if [ -n "${LARARIUM_DB_CONTAINER:-}" ]; then
  db=(docker exec -i "$LARARIUM_DB_CONTAINER")
else
  infra="$(cd "$(dirname "$0")/.." && pwd)"
  db=(docker compose -f "$infra/compose.yaml" --env-file "$infra/.env" exec -T db)
fi

"${db[@]}" psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -c "CREATE DATABASE $target OWNER lararium_owner"
"${db[@]}" pg_restore -U postgres -d "$target" \
  --no-owner --role=lararium_owner --exit-on-error < "$dump"
echo "restored $dump into $target"
