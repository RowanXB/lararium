#!/usr/bin/env bash
# Dump the lararium database to $BACKUP_DIR/lararium-<UTC timestamp>.dump (pg_dump custom
# format) and the uploaded documents to $BACKUP_DIR/documents-<same timestamp>.tar, then,
# when RESTIC_REPOSITORY is set, back $BACKUP_DIR up with restic.
set -euo pipefail
# Dumps hold every balance and transaction, and the documents are bank statements:
# everything this script creates is owner-only.
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

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$BACKUP_DIR/lararium-$stamp.dump"
out_docs="$BACKUP_DIR/documents-$stamp.tar"
# Write to temporary names first so a failed dump never looks like a good backup. Both are
# moved into place only once both are written: without the api container (stopped, say) the
# exec fails, and set -e stops here rather than leave a dump without its documents. A failed
# run removes its partial files, so restic never backs one up; after the moves, rm finds
# nothing.
trap 'rm -f "$out.partial" "$out_docs.partial"' EXIT
"${compose[@]}" exec -T db pg_dump -U postgres -d lararium --format=custom > "$out.partial"
# .tmp-* files are uploads still being written; LocalDocumentStore renames them into place.
# GNU tar exits 1 when a directory changed while it was read: an upload landed meanwhile.
# The dump above was taken first, and documents are written once and never change, so the
# tar still holds every document the dump refers to; only ones uploaded since may be
# missing, and the next backup has them. The 1 is made 0 inside the container (with a
# warning), so a failed exec (the api stopped, say, which compose also reports as 1) still
# stops the backup.
"${compose[@]}" exec -T api sh -c \
  'tar -C /var/lib/lararium/documents --exclude=".tmp-*" -cf - .; s=$?
   if [ "$s" -eq 1 ]; then echo "warning: an upload landed during the documents tar" >&2; s=0; fi
   exit "$s"' \
  > "$out_docs.partial"
mv "$out.partial" "$out"
mv "$out_docs.partial" "$out_docs"
echo "wrote $out"
echo "wrote $out_docs"

if [ -n "${RESTIC_REPOSITORY:-}" ]; then
  restic backup "$BACKUP_DIR"
fi
