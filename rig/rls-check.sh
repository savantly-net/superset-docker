#!/usr/bin/env bash
# The automated RLS check (S5.6) on the rig, end to end, with its negative test.
#
# Usage: rig/rls-check.sh            (KEEP=1 leaves the rig up afterwards)
# Needs Docker, python3, and franops-bi (pip install -e tools/franops_bi).
# RIG_SKIP_BUILD=1 checks the image already tagged franops-bi-rig/superset:local.
#
# 1. An empty rig with OAuth off. franops-bi deploys tests/fixtures/bi, and rls-plan prepares the
#    guest roles, the service account, and the embedded dashboards (rig only, pointer item 49).
# 2. The check passes with OAuth off: franchisee A (loc-001) sees no row of loc-002.
# 3. rig-superset restarts in OAuth mode on the same metadata database. An admin API login now
#    gets 401, which proves the mode. The check passes again, through the service login.
# 4. The seeded fault: the validator is off and the token has no rls rule. The check must exit 3
#    (a row of another franchisee). Any other exit fails this script.
set -euo pipefail
cd "$(dirname "$0")/.."

URL="http://127.0.0.1:${RIG_SUPERSET_PORT:-18388}"
CLAUSE="location_key IN ('loc-001')"
PLAN="$(mktemp)"

cleanup() {
  status=$?
  rm -f "$PLAN"
  if [ "${KEEP:-0}" != "1" ]; then rig/rig.sh down >/dev/null 2>&1 || true; fi
  exit "$status"
}
trap cleanup EXIT

check() { python3 scripts/rls_check.py --url "$URL" --plan "$PLAN" --allow loc-001 "$@"; }

admin_login_status() {
  curl -s -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' \
    -d "{\"username\":\"admin\",\"password\":\"${RIG_ADMIN_PASSWORD:-admin-rig-pass}\",\"provider\":\"db\"}" \
    "$URL/api/v1/security/login"
}

rig/rig.sh up
FRANOPS_BI_DB_PASSWORDS='{"databases/warehouse.yaml":"warehouse-rig-pass"}' \
  python3 -m franops_bi deploy --dir tests/fixtures/bi --no-sources --url "$URL" \
  --username admin --password "${RIG_ADMIN_PASSWORD:-admin-rig-pass}"
rig/rig.sh rls-plan 2>/dev/null > "$PLAN"
# The product's roles apply (item 69) sets Embed and GuestIssuer after the embed, so each guest token
# below runs on the grants that the image sets, not on the plan's.
docker compose -f rig/compose.yml exec -T rig-superset /app/franops-init.sh 2>&1 | grep "^role\|^roles:" \
  || { echo "FAIL: the init task did not apply the roles" >&2; exit 1; }
echo "plan: $(python3 -c 'import json,sys; p=json.load(open(sys.argv[1])); print(len(p["charts"]), "certified charts")' "$PLAN")"

echo "== RLS check, OAuth off"
check --clause "$CLAUSE"

echo "== RLS check, OAuth on"
RIG_OAUTH=1 rig/rig.sh restart
status="$(admin_login_status)"
if [ "$status" != "401" ]; then
  echo "FAIL: OAuth mode is not on: the admin API login got $status, not 401" >&2
  exit 1
fi
echo "ok   OAuth mode: the admin API login gets 401"
check --clause "$CLAUSE"

echo "== Negative test: validator off, a guest token with no rls rule"
RIG_OAUTH=1 RIG_GUEST_TOKEN_REQUIRE_RLS=false rig/rig.sh restart
set +e
check --no-rls
fault=$?
set -e
if [ "$fault" != "3" ]; then
  echo "FAIL: the seeded fault gave exit $fault; the check must exit 3 (a leak)" >&2
  exit 1
fi
echo "PASS: the check passes with OAuth off and on, and fails on the seeded fault"
