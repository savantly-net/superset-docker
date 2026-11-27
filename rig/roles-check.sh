#!/usr/bin/env bash
# The S5.9 and S5.10 check on the rig: the BI roles from code, and deploy and certify under OAuth
# with an API key.
#
# Usage: rig/roles-check.sh            (KEEP=1 leaves the rig up afterwards)
# Needs Docker, git, python3, and franops-bi (pip install -e tools/franops_bi).
# RIG_SKIP_BUILD=1 checks the image already tagged franops-bi-rig/superset:local.
#
# 1. An empty rig, OAuth off. franops-bi deploys tests/fixtures/bi with the admin login; the
#    deploy calls the roles endpoint, which creates the roles and the service account.
# 2. /app/franops-init.sh, the init task: it changes nothing. rig/oidc/roles_probe.py: each role
#    is exactly the design table, and only Admin holds FranopsRoles and ApiKey. The probe makes
#    an API key for the admin and for a Gamma user.
# 3. rig-superset restarts in OAuth mode on the same metadata database. The admin's `db` login
#    is refused (S5.8). The roles endpoint refuses no credentials, a wrong key, and Gamma's key.
# 4. franops-bi deploy with the admin's key: the import, the tags, and the roles call pass.
# 5. An admin draft chart over REST with the key, then franops-bi certify with the key, into a
#    scratch brand repository: one commit on certify/chart-<id>, and the chart is certified.
set -euo pipefail
cd "$(dirname "$0")/.."

URL="http://127.0.0.1:${RIG_SUPERSET_PORT:-18388}"
ADMIN_PASSWORD="${RIG_ADMIN_PASSWORD:-admin-rig-pass}"
export FRANOPS_BI_DB_PASSWORDS='{"databases/warehouse.yaml":"warehouse-rig-pass"}'
base() { docker compose -f rig/compose.yml "$@"; }
oauth() { docker compose -f rig/compose.yml -f rig/compose.oauth.yml "$@"; }
repo="$(mktemp -d)"
out="$(mktemp)"

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    cat "$out" >&2 2>/dev/null || true
    oauth logs --tail 40 rig-superset >&2 2>&1 || true
  fi
  rm -rf "$repo" "$out"
  if [ "${KEEP:-0}" != "1" ]; then oauth down --volumes --remove-orphans >/dev/null 2>&1 || true; fi
  exit "$status"
}
trap cleanup EXIT

pass() { echo "ok   $1"; }
fail() { echo "FAIL: $1" >&2; exit 1; }
expect_line() { grep -qx -- "$2" "$out" || fail "$1: no line '$2'"; pass "$1"; }

oauth down --volumes --remove-orphans >/dev/null 2>&1 || true
rig/rig.sh up

echo "== 1. deploy with OAuth off: the roles endpoint makes the roles"
python3 -m franops_bi deploy --dir tests/fixtures/bi --url "$URL" \
  --username admin --password "$ADMIN_PASSWORD" | tee "$out"
grep -q "^role HQ: +" "$out" || fail "the first deploy did not add the HQ grants"
expect_line "the first deploy created the service account" "service account: created"

echo "== 2. the init task changes nothing; each role is the design table"
base exec -T rig-superset /app/franops-init.sh > "$out" 2>&1 || fail "franops-init.sh failed"
for role in HQ Embed GuestIssuer; do expect_line "init: $role unchanged" "role $role: unchanged"; done
expect_line "init: the service account unchanged" "service account: unchanged"
keys="$(base exec -T -e ROLES_PROBE_MAKE_KEY=1 rig-superset python - < rig/oidc/roles_probe.py | tee /dev/stderr | sed -n 's/^RIGJSON //p')"
[ -n "$keys" ] || fail "the roles probe failed"
ADMIN_KEY="$(printf '%s' "$keys" | python3 -c 'import json,sys; print(json.load(sys.stdin)["admin"])')"
GAMMA_KEY="$(printf '%s' "$keys" | python3 -c 'import json,sys; print(json.load(sys.stdin)["gamma"])')"

echo "== 3. OAuth on: no db login for the admin; the roles endpoint refuses each non-Admin"
oauth up --detach --no-deps --force-recreate --wait --wait-timeout "${RIG_TIMEOUT:-420}" rig-superset
code="$(curl -s -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$ADMIN_PASSWORD\",\"provider\":\"db\"}" "$URL/api/v1/security/login")"
[ "$code" = "401" ] || fail "the admin's db login under OAuth answered $code, not 401"
pass "the admin's db login under OAuth is 401 (S5.8 holds)"
roles_post() { curl -s -o /dev/null -w '%{http_code}' -X POST "$@" "$URL/api/v1/franops/roles/apply/"; }
code="$(roles_post)"; [ "$code" = "401" ] || fail "the roles endpoint with no credentials answered $code"
pass "the roles endpoint with no credentials is 401"
code="$(roles_post -H 'Authorization: Bearer sst_not-a-key-0123456789')"
[ "$code" = "401" ] || fail "the roles endpoint with a wrong key answered $code"
pass "the roles endpoint with a wrong key is 401"
code="$(roles_post -H "Authorization: Bearer $GAMMA_KEY")"
[ "$code" = "403" ] || fail "the roles endpoint with Gamma's key answered $code"
pass "the roles endpoint with Gamma's key is 403"
if SUPERSET_API_KEY=sst_not-a-key-0123456789 python3 -m franops_bi deploy --dir tests/fixtures/bi \
  --url "$URL" > "$out" 2>&1; then fail "deploy with a wrong key passed"; fi
pass "deploy with a wrong key fails"

echo "== 4. deploy under OAuth with the admin's key"
SUPERSET_API_KEY="$ADMIN_KEY" python3 -m franops_bi deploy --dir tests/fixtures/bi --url "$URL" | tee "$out"
for role in HQ Embed GuestIssuer; do expect_line "deploy with the key: $role unchanged" "role $role: unchanged"; done

echo "== 5. certify under OAuth with the admin's key"
git -C "$repo" init -q -b main
cp -R tests/fixtures/bi "$repo/bi"
git -C "$repo" -c user.name=rig -c user.email=rig@rig.invalid add bi
git -C "$repo" -c user.name=rig -c user.email=rig@rig.invalid commit -q -m "the fixture"
chart_id="$(SUPERSET_API_KEY="$ADMIN_KEY" URL="$URL" python3 - <<'PY'
import os
from franops_bi.client import Superset
bi = Superset(os.environ["URL"], api_key=os.environ["SUPERSET_API_KEY"])
datasets = bi.list_all("dataset", ["id", "table_name"])
_, body = bi.request("POST", "/api/v1/chart/", {
    "slice_name": "rig roles-check draft", "viz_type": "table", "datasource_type": "table",
    "datasource_id": datasets[0]["id"], "params": "{}"}, expect=201)
print(body["id"])
PY
)"
pass "an admin draft chart over REST with the key: id $chart_id"
SUPERSET_API_KEY="$ADMIN_KEY" GIT_AUTHOR_NAME=rig GIT_AUTHOR_EMAIL=rig@rig.invalid \
  GIT_COMMITTER_NAME=rig GIT_COMMITTER_EMAIL=rig@rig.invalid \
  python3 -m franops_bi certify chart "$chart_id" --repo "$repo" --url "$URL" | tee "$out"
grep -q "swapped draft for certified" "$out" || fail "certify did not swap the tags"
n="$(git -C "$repo" rev-list --count "main..certify/chart-$chart_id")"
[ "$n" = "1" ] || fail "certify/chart-$chart_id holds $n commits, not 1"
pass "certify with the key: one commit on certify/chart-$chart_id"
echo "PASS: the roles and API key check"
