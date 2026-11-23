#!/usr/bin/env bash
# Service login and embed check (S5.8): under OAuth, the API password login is open to the
# service account only, and the embed settings of design/bi-image.md section 3 are on.
#
# Usage: scripts/guest-login-check.sh <image> [timeout-seconds] [override.py]
#
# It starts the image with scripts/deployed-shape.env (OAuth and embedding on, placeholder
# values), runs `superset init`, and runs scripts/guest_login_probe.py inside the app
# container. override.py, when given, is mounted as superset_config_docker.py;
# scripts/test-guest-login-check.sh uses it to break the check and expects a failure.
# The check publishes no port and pushes nothing.
set -euo pipefail

IMAGE="${1:?usage: $0 <image> [timeout-seconds]}"
TIMEOUT="${2:-240}"
OVERRIDE="${3:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$HERE/deployed-shape.env"
RUN_ID="superset-guest-login-$$"
NETWORK="$RUN_ID-net"
DB="$RUN_ID-db"
APP="$RUN_ID-app"
POSTGRES_IMAGE="postgres:17"

# shellcheck disable=SC2329  # invoked by the EXIT trap
cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    echo "guest-login-check: FAIL. Last app log lines:" >&2
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

echo "guest-login-check: waiting for Postgres"
for _ in $(seq 1 60); do
  docker exec "$DB" pg_isready -U superset -d superset >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$DB" pg_isready -U superset -d superset >/dev/null

echo "guest-login-check: applying migrations and superset init"
docker run --rm --network "$NETWORK" "${ENV_ARGS[@]}" "$IMAGE" \
  sh -c 'superset db upgrade && superset init' >/dev/null 2>&1 \
  || { echo "guest-login-check: superset db upgrade or init failed" >&2;
       docker run --rm --network "$NETWORK" "${ENV_ARGS[@]}" "$IMAGE" \
         sh -c 'superset db upgrade && superset init' 2>&1 | tail -40 >&2;
       exit 1; }

echo "guest-login-check: starting $IMAGE with its default CMD"
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
    echo "guest-login-check: the app container exited" >&2
    exit 1
  fi
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "guest-login-check: /health did not answer 200 within ${TIMEOUT}s" >&2
    exit 1
  fi
  sleep 3
done

docker cp "$HERE/guest_login_probe.py" "$APP:/tmp/guest_login_probe.py"
echo "guest-login-check: probing the service login and the embed settings"
# Not `python -I`: the probe must load the same configuration as the server, and -I drops
# PYTHONPATH, where superset_config_docker.py lives.
docker exec -w /tmp "$APP" python /tmp/guest_login_probe.py
echo "guest-login-check: OK"
