#!/usr/bin/env bash
# The franops-bi rig: the BI image, a metadata Postgres, and a warehouse Postgres.
#
# Usage: rig/rig.sh up       build the image, start an empty stack, wait for /health 200
#        rig/rig.sh down     stop the stack and delete its volumes
#        rig/rig.sh status
#        rig/rig.sh psql     psql on the metadata database
# Ports (loopback only): Superset RIG_SUPERSET_PORT (18388), metadata DB RIG_META_PORT (18432).
# The admin account is admin / RIG_ADMIN_PASSWORD (admin-rig-pass).
set -euo pipefail
cd "$(dirname "$0")"

compose() { docker compose -f compose.yml "$@"; }

case "${1:-}" in
  up)
    # An empty instance each time: drop the volumes of an earlier run first.
    compose down --volumes --remove-orphans >/dev/null 2>&1 || true
    compose build rig-superset
    if ! compose up --detach --wait --wait-timeout "${RIG_TIMEOUT:-420}" rig-superset; then
      compose logs --tail 80 rig-init rig-superset >&2 || true
      exit 1
    fi
    echo "rig: up at http://127.0.0.1:${RIG_SUPERSET_PORT:-18388}"
    ;;
  down)
    compose down --volumes --remove-orphans
    ;;
  status)
    compose ps
    ;;
  psql)
    compose exec rig-meta psql -U superset -d superset
    ;;
  *)
    echo "usage: $0 up|down|status|psql" >&2
    exit 2
    ;;
esac
