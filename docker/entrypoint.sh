#!/bin/sh
# Role dispatcher for the backend image. Signals reach the process (exec, under the
# container init), so SIGTERM triggers the graceful shutdown each role implements.
set -eu

role="${1:-api}"
[ "$#" -gt 0 ] && shift

case "$role" in
  api)
    # Readiness (/health/ready) fails until the schema is current; the API never migrates.
    exec trend-agent dashboard serve --host 0.0.0.0 --port "${PORT:-8000}" "$@"
    ;;
  scheduler)
    exec trend-agent system scheduler "$@"
    ;;
  worker)
    exec trend-agent system worker "$@"
    ;;
  migrate)
    # Forward-only and additive by policy; downgrades are never run automatically.
    # Refuses to start if the database is at an unknown revision.
    exec alembic -c /app/alembic.ini upgrade head
    ;;
  heartbeat-check)
    exec trend-agent system heartbeat-check "$@"
    ;;
  *)
    exec trend-agent "$role" "$@"
    ;;
esac
