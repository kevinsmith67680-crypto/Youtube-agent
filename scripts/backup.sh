#!/usr/bin/env sh
# Nightly backup with retention (host cron or a scheduled job). Uses the application's own
# backup command (pg_dump custom format + SHA-256 manifest with per-table row counts) and
# keeps the newest $KEEP dumps. Verify backups with `trend-agent system restore-drill`.
#
#   BACKUP_DIR=/var/backups/trend-agent KEEP=14 scripts/backup.sh
#
# The database URL comes from TREND_AGENT_DATABASE_URL (never from the command line).
set -eu

BACKUP_DIR="${BACKUP_DIR:-./backups}"
KEEP="${KEEP:-14}"

case "$KEEP" in
  ''|*[!0-9]*) echo "KEEP must be a positive integer" >&2; exit 2 ;;
esac
[ "$KEEP" -ge 1 ] || { echo "KEEP must be >= 1" >&2; exit 2; }

mkdir -p "$BACKUP_DIR"
trend-agent system backup --out "$BACKUP_DIR" --json

# Retention: only *.dump files written by this script's naming scheme, newest first.
count=0
for f in $(ls -1t "$BACKUP_DIR"/*.dump 2>/dev/null); do
  count=$((count + 1))
  if [ "$count" -gt "$KEEP" ]; then
    rm -f -- "$f" "${f%.dump}.manifest.json"
  fi
done
