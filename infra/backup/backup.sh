#!/usr/bin/env bash
# Dump the financial_gate database to $BACKUP_DIR/fg-<UTC timestamp>.dump (pg_dump custom
# format), then, when RESTIC_REPOSITORY is set, back $BACKUP_DIR up with restic.
set -euo pipefail
# Dumps hold every balance and transaction: everything this script creates is owner-only.
umask 077

infra="$(cd "$(dirname "$0")/.." && pwd)"
root="$(dirname "$infra")"
set -a
# shellcheck source=/dev/null
. "$infra/.env"
set +a
compose=(docker compose -f "$infra/compose.yaml" --env-file "$infra/.env")

: "${BACKUP_DIR:?BACKUP_DIR must be set in infra/.env}"
case "$BACKUP_DIR" in
  /*) ;;
  *) BACKUP_DIR="$root/$BACKUP_DIR" ;;
esac
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"  # mkdir -p leaves an existing directory's mode alone

out="$BACKUP_DIR/fg-$(date -u +%Y%m%dT%H%M%SZ).dump"
# Write to a temporary name first so a failed dump never looks like a good backup.
"${compose[@]}" exec -T db pg_dump -U postgres -d financial_gate --format=custom > "$out.partial"
mv "$out.partial" "$out"
echo "wrote $out"

if [ -n "${RESTIC_REPOSITORY:-}" ]; then
  restic backup "$BACKUP_DIR"
fi
