#!/usr/bin/env bash
# The certified calls scorecard (fran-ops S12.4) on the rig, end to end, with its negative test.
#
# Usage: rig/scorecard-check.sh [<sources_bi_dir>]   (KEEP=1 leaves the rig up afterwards)
# The default folder is ../franops-data-dbt/sources/bi, the bi/ of franops_sources beside this
# repository. Needs Docker, python3, and franops-bi (pip install -e tools/franops_bi).
#
# 1. AC1: an empty rig. One franops-bi deploy, with `callrail` from a FranOps stub, creates the
#    brand fixture and the full certified set of bi/callrail/ (rig/scorecard_deploy_check.py).
# 2. AC2: rls-plan embeds each dashboard that holds a certified chart, and the init task applies
#    the roles. A guest token for franchisee A (its crigloca locations, and loc-001 of the
#    brand fixture) gets rows of A only from each certified chart, the scorecard's included.
# 3. The seeded fault: the validator is off and the token has no rls rule. The check must exit 3
#    (a row of franchisee B). Any other exit fails this script.
set -euo pipefail
cd "$(dirname "$0")/.."
SOURCES="${1:-../franops-data-dbt/sources/bi}"
URL="http://127.0.0.1:${RIG_SUPERSET_PORT:-18388}"
A_KEYS=(loc-001 crigloca00000000000000001 crigloca00000000000000002)
CLAUSE="location_key IN ('loc-001','crigloca00000000000000001','crigloca00000000000000002')"
PLAN="$(mktemp)"
cleanup() {
  status=$?
  rm -f "$PLAN"
  if [ "${KEEP:-0}" != "1" ]; then rig/rig.sh down >/dev/null 2>&1 || true; fi
  exit "$status"
}
trap cleanup EXIT
[ -d "$SOURCES/callrail" ] || { echo "FAIL: no callrail folder in $SOURCES" >&2; exit 1; }
allow=()
for k in "${A_KEYS[@]}"; do allow+=(--allow "$k"); done
check() { python3 scripts/rls_check.py --url "$URL" --plan "$PLAN" "${allow[@]}" "$@"; }

rig/rig.sh up
echo "== AC1: one deploy on an empty BI"
python3 rig/scorecard_deploy_check.py "$SOURCES"

rig/rig.sh rls-plan 2>/dev/null > "$PLAN"
docker compose -f rig/compose.yml exec -T rig-superset /app/franops-init.sh 2>&1 | grep "^role\|^roles:" \
  || { echo "FAIL: the init task did not apply the roles" >&2; exit 1; }
python3 - "$PLAN" <<'PY'
import json, sys
plan = json.load(open(sys.argv[1]))
names = sorted(c.get("chartName") or "" for c in plan["charts"])
print("plan:", len(names), "certified charts:", ", ".join(names))
missing = {"Calls by location", "Calls per day by location"} - set(names)
if missing:
    sys.exit(f"FAIL: the plan does not hold the scorecard charts {sorted(missing)}")
PY

echo "== AC2: franchisee A's guest token"
check --clause "$CLAUSE"

echo "== Negative test: validator off, a guest token with no rls rule"
RIG_GUEST_TOKEN_REQUIRE_RLS=false rig/rig.sh restart
set +e
check --no-rls
fault=$?
set -e
if [ "$fault" != "3" ]; then
  echo "FAIL: the seeded fault gave exit $fault; the check must exit 3 (a leak)" >&2
  exit 1
fi
echo "PASS: one deploy creates the certified scorecard, and A's token gets no row of B"
