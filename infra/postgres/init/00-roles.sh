#!/usr/bin/env bash
set -euo pipefail

: "${FG_OWNER_PASSWORD:?FG_OWNER_PASSWORD must be set}"
: "${FG_BACKEND_PASSWORD:?FG_BACKEND_PASSWORD must be set}"

# The passwords stay off the command line (visible in `ps`): psql reads them from the
# environment via \getenv, and the commands arrive on stdin. roles.sql stays the single
# source of truth; the test harness feeds it the same variables with `-v`.
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<'SQL'
\getenv owner_pw FG_OWNER_PASSWORD
\getenv backend_pw FG_BACKEND_PASSWORD
\i /fg/roles.sql
SQL
