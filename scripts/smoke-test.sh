#!/usr/bin/env bash
# Smoke test: starts the image with its own CMD, as its default user, and expects 200 from
# /health.
#
# Usage: scripts/smoke-test.sh <image> [timeout-seconds]
#
# superset_config.py needs a metadata database, so the test starts a throwaway Postgres on a
# private Docker network and applies the migrations with `superset db upgrade` first. Redis
# gets a placeholder host: /health does not reach it. The test publishes no port and pushes
# nothing; it sends GET /health over TCP to gunicorn from inside the app container.
set -euo pipefail

IMAGE="${1:?usage: $0 <image> [timeout-seconds]}"
TIMEOUT="${2:-180}"
RUN_ID="superset-smoke-$$"
NETWORK="$RUN_ID-net"
DB="$RUN_ID-db"
APP="$RUN_ID-app"
POSTGRES_IMAGE="postgres:17"

# shellcheck disable=SC2329  # invoked by the EXIT trap
cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    echo "smoke-test: FAIL. Last app log lines:" >&2
    docker logs --tail 80 "$APP" >&2 2>&1 || true
  fi
  docker rm -f "$APP" "$DB" >/dev/null 2>&1 || true
  docker network rm "$NETWORK" >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

ENV_ARGS=(
  -e SECRET_KEY=smoke-test-secret-key-not-for-production
  -e DB_DIALECT=postgresql+psycopg2
  -e DB_USER=superset
  -e DB_PASSWORD=superset
  -e DB_HOST="$DB"
  -e DB_PORT=5432
  -e DB_NAME=superset
  -e REDIS_HOST=redis.invalid
  -e REDIS_PORT=6379
)

docker network create "$NETWORK" >/dev/null
docker run -d --name "$DB" --network "$NETWORK" \
  -e POSTGRES_USER=superset -e POSTGRES_PASSWORD=superset -e POSTGRES_DB=superset \
  "$POSTGRES_IMAGE" >/dev/null

echo "smoke-test: waiting for Postgres"
for _ in $(seq 1 60); do
  if docker exec "$DB" pg_isready -h 127.0.0.1 -U superset -d superset >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "$DB" pg_isready -h 127.0.0.1 -U superset -d superset >/dev/null

echo "smoke-test: applying migrations"
docker run --rm --network "$NETWORK" "${ENV_ARGS[@]}" "$IMAGE" superset db upgrade >/dev/null 2>&1 \
  || { echo "smoke-test: superset db upgrade failed" >&2;
       docker run --rm --network "$NETWORK" "${ENV_ARGS[@]}" "$IMAGE" superset db upgrade 2>&1 | tail -40 >&2;
       exit 1; }

echo "smoke-test: starting $IMAGE with its default CMD"
docker run -d --name "$APP" --network "$NETWORK" "${ENV_ARGS[@]}" "$IMAGE" >/dev/null

deadline=$((SECONDS + TIMEOUT))
code="000"
while [ "$SECONDS" -lt "$deadline" ]; do
  if [ "$(docker inspect -f '{{.State.Running}}' "$APP")" != "true" ]; then
    echo "smoke-test: the app container exited" >&2
    exit 1
  fi
  code="$(docker exec "$APP" python -I -c '
import urllib.request
try:
    print(urllib.request.urlopen("http://127.0.0.1:8088/health", timeout=5).status)
except Exception:
    print("000")
' 2>/dev/null || echo 000)"
  if [ "$code" = "200" ]; then
    echo "smoke-test: OK: GET /health returned 200"
    exit 0
  fi
  sleep 3
done

echo "smoke-test: GET /health did not return 200 within ${TIMEOUT}s (last code: $code)" >&2
exit 1
