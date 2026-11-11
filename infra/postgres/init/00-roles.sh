#!/usr/bin/env bash
set -euo pipefail

psql -v ON_ERROR_STOP=1 -v owner_pw="$FG_OWNER_PASSWORD" -v backend_pw="$FG_BACKEND_PASSWORD" -U "$POSTGRES_USER" -f /fg/roles.sql
