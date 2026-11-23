#!/usr/bin/env bash
# Public lockdown check (S5.4, NFR-2): each /api/v1 route answers a request with no
# credentials with 401 or 403.
#
# Usage: scripts/public-lockdown-check.sh <image> [timeout-seconds] [override.py] [env-file]
#
# override.py, when given, is mounted as superset_config_docker.py, the file that
# superset_config.py imports last. scripts/test-lockdown-check.sh uses it to break the lockdown
# and expects this check to fail. Pass "" to give an env-file with no override.
#
# env-file, when given, goes to `docker run --env-file`. CI passes
# scripts/deployed-shape.env, so the check also runs with OAuth and embedding on (S5.8).
#
# Like smoke-test.sh, it starts a throwaway Postgres on a private Docker network, applies the
# migrations, and starts the image with its own CMD. It also runs `superset init`, which
# creates the roles (Public among them) and their permissions, as a deployment does. Then
# scripts/lockdown_probe.py runs inside the app container: it reads the app's URL map and
# sends one anonymous request to each method of each /api/v1 route. The check publishes no
# port and pushes nothing.
set -euo pipefail

IMAGE="${1:?usage: $0 <image> [timeout-seconds]}"
TIMEOUT="${2:-240}"
OVERRIDE="${3:-}"
ENV_FILE="${4:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
RUN_ID="superset-lockdown-$$"
NETWORK="$RUN_ID-net"
DB="$RUN_ID-db"
APP="$RUN_ID-app"
POSTGRES_IMAGE="postgres:17"

# shellcheck disable=SC2329  # invoked by the EXIT trap
cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    echo "lockdown-check: FAIL. Last app log lines:" >&2
    docker logs --tail 40 "$APP" >&2 2>&1 || true
  fi
  docker rm -f "$APP" "$DB" >/dev/null 2>&1 || true
  docker network rm "$NETWORK" >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

ENV_ARGS=(
  -e SECRET_KEY=lockdown-check-secret-key-not-for-production
  -e DB_DIALECT=postgresql+psycopg2
  -e DB_USER=superset
  -e DB_PASSWORD=superset
  -e DB_HOST="$DB"
  -e DB_PORT=5432
  -e DB_NAME=superset
  -e REDIS_HOST=redis.invalid
  -e REDIS_PORT=6379
)
if [ -n "$ENV_FILE" ]; then
  ENV_ARGS+=(--env-file "$ENV_FILE")
fi
if [ -n "$OVERRIDE" ]; then
  ENV_ARGS+=(-v "$(cd "$(dirname "$OVERRIDE")" && pwd)/$(basename "$OVERRIDE"):/app/pythonpath/superset_config_docker.py:ro")
fi

docker network create "$NETWORK" >/dev/null
docker run -d --name "$DB" --network "$NETWORK" \
  -e POSTGRES_USER=superset -e POSTGRES_PASSWORD=superset -e POSTGRES_DB=superset \
  "$POSTGRES_IMAGE" >/dev/null

echo "lockdown-check: waiting for Postgres"
for _ in $(seq 1 60); do
  docker exec "$DB" pg_isready -U superset -d superset >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$DB" pg_isready -U superset -d superset >/dev/null

echo "lockdown-check: applying migrations and superset init"
docker run --rm --network "$NETWORK" "${ENV_ARGS[@]}" "$IMAGE" \
  sh -c 'superset db upgrade && superset init' >/dev/null 2>&1 \
  || { echo "lockdown-check: superset db upgrade or init failed" >&2;
       docker run --rm --network "$NETWORK" "${ENV_ARGS[@]}" "$IMAGE" \
         sh -c 'superset db upgrade && superset init' 2>&1 | tail -40 >&2;
       exit 1; }

echo "lockdown-check: starting $IMAGE with its default CMD"
docker run -d --name "$APP" --network "$NETWORK" "${ENV_ARGS[@]}" "$IMAGE" >/dev/null

deadline=$((SECONDS + TIMEOUT))
until [ "$(docker exec "$APP" python -I -c '
import urllib.request
try:
    print(urllib.request.urlopen("http://127.0.0.1:8088/health", timeout=5).status)
except Exception:
    print("000")
' 2>/dev/null || echo 000)" = "200" ]; do
  if [ "$(docker inspect -f '{{.State.Running}}' "$APP")" != "true" ]; then
    echo "lockdown-check: the app container exited" >&2
    exit 1
  fi
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "lockdown-check: /health did not answer 200 within ${TIMEOUT}s" >&2
    exit 1
  fi
  sleep 3
done

docker cp "$HERE/lockdown_probe.py" "$APP:/tmp/lockdown_probe.py"
echo "lockdown-check: probing each /api/v1 route with no credentials"
# Not `python -I`: the probe must load the same configuration as the server, and -I drops
# PYTHONPATH, where superset_config_docker.py lives.
docker exec -w /tmp "$APP" python /tmp/lockdown_probe.py
echo "lockdown-check: OK"
