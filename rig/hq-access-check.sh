#!/usr/bin/env bash
# The S7.1, S7.3, S7.4 and S7.5 check on the rig: what HQ reaches over MCP, the draft tag, the
# certified view, and franops-bi certify.
#
# Usage: rig/hq-access-check.sh            (KEEP=1 leaves the rig up afterwards)
# Needs Docker, python3, and franops-bi (pip install -e tools/franops_bi).
# RIG_SKIP_BUILD=1 checks the image already tagged franops-bi-rig/superset:local.
#
# 1. An empty rig with OAuth off. franops-bi deploys tests/fixtures/bi with the admin login,
#    which OAuth mode refuses (S5.8). The web log must show the draft hook.
# 2. rig-superset restarts in OAuth mode with the fakes and the MCP settings, on the same
#    metadata database. `superset mcp run` starts, and its log must show the draft hook.
# 3. rig/oidc/hq_probe.py: an HQ chart over MCP on a certified dataset is made and is a draft;
#    a dataset or a chart on the raw table is refused; the certified assets are not drafts.
# 4. rig-superset restarts with OAuth off, and franops-bi deploys the fixture again. The probe's
#    second phase: the certified assets keep `certified` and get no `draft`; the drafts stay.
# 5. OAuth off once more. rig/certify_check.py: HQ's certified view and draft labels (S7.4), and
#    franops-bi certify as HQ and as the reviewer, then a deploy of its branch (S7.5).
set -euo pipefail
cd "$(dirname "$0")/.."

URL="http://127.0.0.1:${RIG_SUPERSET_PORT:-18388}"
base() { docker compose -f rig/compose.yml "$@"; }
full() { docker compose -f rig/compose.yml -f rig/compose.oauth.yml -f rig/compose.oidc.yml -f rig/compose.mcp.yml "$@"; }
mcp_log="$(mktemp)"

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    full logs --tail 40 rig-superset >&2 2>&1 || true
    echo "== MCP log (tail)" >&2
    tail -40 "$mcp_log" >&2 2>/dev/null || true
  fi
  rm -f "$mcp_log"
  if [ "${KEEP:-0}" != "1" ]; then full down --volumes --remove-orphans >/dev/null 2>&1 || true; fi
  exit "$status"
}
trap cleanup EXIT

deploy() {
  FRANOPS_BI_DB_PASSWORDS='{"databases/warehouse.yaml":"warehouse-rig-pass"}' \
    python3 -m franops_bi deploy --dir tests/fixtures/bi --url "$URL" \
    --username admin --password "${RIG_ADMIN_PASSWORD:-admin-rig-pass}"
}

hook_lines() { grep -c "draft hook installed" || true; }

# Only the deploy inserts charts and dashboards in this container, so the hook must tag none.
# The import replaces the tags afterwards, so the end state alone cannot show a transient draft.
no_draft_from_deploy() {
  tagged="$(base logs --no-log-prefix rig-superset 2>&1 | grep -c "draft hook: tagged" || true)"
  if [ "$tagged" != "0" ]; then
    echo "FAIL: $1: the hook tagged $tagged imported assets draft" >&2
    exit 1
  fi
  echo "ok   $1: the hook tagged no imported asset draft"
}

full down --volumes --remove-orphans >/dev/null 2>&1 || true
rig/rig.sh up
deploy
if [ "$(base logs --no-log-prefix rig-superset 2>&1 | hook_lines)" -lt 1 ]; then
  echo "FAIL: the web log holds no 'draft hook installed' line" >&2
  exit 1
fi
echo "ok   the web process installed the draft hook"
no_draft_from_deploy "the first deploy"

echo "== Restart in OAuth mode with the MCP settings"
full up --detach --wait --wait-timeout "${RIG_TIMEOUT:-420}" rig-fake-oidc rig-fake-franops
full up --detach --no-deps --force-recreate --wait --wait-timeout "${RIG_TIMEOUT:-420}" rig-superset
full exec -d rig-superset sh -c 'superset mcp run --host 127.0.0.1 --port 5008 > /tmp/mcp.log 2>&1'
ready=0
for _ in $(seq 1 60); do
  code="$(full exec -T rig-superset python -I -c '
import urllib.request, urllib.error
try:
    urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:5008/mcp", data=b"{}", method="POST"), timeout=3)
    print(200)
except urllib.error.HTTPError as err:
    print(err.code)
except Exception:
    print(0)
' 2>/dev/null || echo 0)"
  if [ "$code" = "401" ]; then ready=1; break; fi
  sleep 2
done
full exec -T rig-superset cat /tmp/mcp.log > "$mcp_log" 2>/dev/null || true
if [ "$ready" != "1" ]; then
  echo "FAIL: the MCP service did not answer 401 to a request with no token" >&2
  exit 1
fi
if [ "$(hook_lines < "$mcp_log")" -lt 1 ]; then
  echo "FAIL: the MCP log holds no 'draft hook installed' line" >&2
  exit 1
fi
echo "ok   the MCP process installed the draft hook"

echo "== HQ probe"
# Not `python -I`: the probe loads the server's configuration, and -I drops PYTHONPATH.
full exec -T -w /rig-oidc rig-superset python /rig-oidc/hq_probe.py

echo "== Deploy again, over the drafts"
# The admin login needs OAuth off. The probe needs the /rig-oidc mount of compose.oidc.yml, so
# rig-superset restarts once more in the full shape before the probe's second phase.
base up --detach --no-deps --force-recreate --wait --wait-timeout "${RIG_TIMEOUT:-420}" rig-superset
deploy
no_draft_from_deploy "the second deploy"
full up --detach --no-deps --force-recreate --wait --wait-timeout "${RIG_TIMEOUT:-420}" rig-superset
full exec -T -w /rig-oidc -e HQ_PROBE_PHASE=redeploy rig-superset python /rig-oidc/hq_probe.py

echo "== The certified view and franops-bi certify (S7.4, S7.5)"
# certify logs in with the db provider, as deploy does, so OAuth is off again. rig_hq_db is an
# HQ user with a password: the checks run through the HQ grants that the probe gave the role.
base up --detach --no-deps --force-recreate --wait --wait-timeout "${RIG_TIMEOUT:-420}" rig-superset
base exec -T rig-superset superset fab create-user --role HQ --username rig_hq_db \
  --firstname Rig --lastname HQ --email hq.db@rig.invalid --password hq-db-rig-pass >/dev/null
# A REST client reads the CSRF token from /api/v1/security/csrf_token/, which HQ cannot read: a
# browser gets it in the page. RigCsrf holds that one grant, so the HQ role stays as designed.
base exec -T rig-superset python -c '
from superset.app import create_app
app = create_app()
with app.app_context():
    from superset.extensions import db
    sm = app.appbuilder.sm
    role = sm.find_role("RigCsrf") or sm.add_role("RigCsrf")
    sm.add_permission_role(role, sm.find_permission_view_menu("can_read", "SecurityRestApi"))
    user = sm.find_user(username="rig_hq_db")
    user.roles = [sm.find_role("HQ"), role]
    db.session.commit()
' >/dev/null 2>&1
python3 rig/certify_check.py
echo "PASS: the HQ access, draft, certified view and certify check"
