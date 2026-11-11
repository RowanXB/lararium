#!/usr/bin/env bash
set -euo pipefail

: "${LARARIUM_OWNER_PASSWORD:?LARARIUM_OWNER_PASSWORD must be set}"
: "${LARARIUM_BACKEND_PASSWORD:?LARARIUM_BACKEND_PASSWORD must be set}"

# The passwords stay off the command line (visible in `ps`): psql reads them from the
# environment via \getenv, and the commands arrive on stdin. roles.sql stays the single
# source of truth; the test harness feeds it the same variables with `-v`.
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<'SQL'
\getenv owner_pw LARARIUM_OWNER_PASSWORD
\getenv backend_pw LARARIUM_BACKEND_PASSWORD
\i /lararium/roles.sql
SQL
