#!/usr/bin/env bash
# The S5.3 MCP check on the rig, end to end: the image's MCP service with tokens from a fake OIDC
# provider, and the BI role from a fake FranOps `userinfo` endpoint.
#
# Usage: rig/mcp-auth-check.sh            (KEEP=1 leaves the rig up afterwards)
# RIG_SKIP_BUILD=1 checks the image already tagged franops-bi-rig/superset:local.
# COMPOSE_PROJECT_NAME, RIG_SUPERSET_PORT, and RIG_META_PORT move the rig beside another one.
#
# 1. An empty rig in OAuth mode with the fakes (compose.oauth.yml, compose.oidc.yml) and the MCP
#    settings (compose.mcp.yml).
# 2. `superset mcp run` inside rig-superset, on 127.0.0.1:5008. A deploy runs the same command
#    in its own container, with the same image and environment.
# 3. rig/oidc/mcp_probe.py, inside rig-superset: no token and each bad token get 401; an HQ
#    token runs as oidc:<sub>, by the action log; the D-8 tools fail; a franchisee and a demoted
#    user are refused; client registration gets 4xx.
# 4. The MCP log and the rig-superset log hold no test email, no API key, and no token.
set -euo pipefail
cd "$(dirname "$0")"

compose() { docker compose -f compose.yml -f compose.oauth.yml -f compose.oidc.yml -f compose.mcp.yml "$@"; }
secrets_file="$(mktemp)"
mcp_log="$(mktemp)"

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    compose logs --tail 40 rig-superset rig-fake-oidc rig-fake-franops >&2 2>&1 || true
    echo "== MCP log (tail)" >&2
    tail -40 "$mcp_log" >&2 2>/dev/null || true
  fi
  rm -f "$secrets_file" "$mcp_log"
  if [ "${KEEP:-0}" != "1" ]; then compose down --volumes --remove-orphans >/dev/null 2>&1 || true; fi
  exit "$status"
}
trap cleanup EXIT

compose down --volumes --remove-orphans >/dev/null 2>&1 || true
if [ "${RIG_SKIP_BUILD:-0}" != "1" ]; then compose build rig-superset; fi
compose up --detach --wait --wait-timeout "${RIG_TIMEOUT:-420}" rig-superset

echo "== Start the MCP service"
compose exec -d rig-superset sh -c 'superset mcp run --host 127.0.0.1 --port 5008 > /tmp/mcp.log 2>&1'
ready=0
for _ in $(seq 1 60); do
  # No token: 401 once the service answers.
  code="$(compose exec -T rig-superset python -I -c '
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
if [ "$ready" != "1" ]; then
  compose exec -T rig-superset cat /tmp/mcp.log > "$mcp_log" 2>/dev/null || true
  echo "FAIL: the MCP service did not answer 401 to a request with no token" >&2
  exit 1
fi

echo "== MCP probe"
# Not `python -I`: the probe loads the server's configuration, and -I drops PYTHONPATH.
compose exec -T -w /rig-oidc rig-superset python /rig-oidc/mcp_probe.py

echo "== The logs hold no email, no API key, and no token"
compose exec -T rig-superset cat /tmp/mcp.log > "$mcp_log"
compose exec -T -w /rig-oidc rig-superset python -c '
import json, urllib.request
from rig_users import USERS
for user in USERS.values():
    for key in ("email", "franopsEmail"):
        if user.get(key):
            print(user[key])
print("fran_rig-bi-userinfo-key")
for token in json.load(urllib.request.urlopen("http://rig-fake-oidc:8080/_issued", timeout=10))["tokens"]:
    print(token)
' > "$secrets_file"
count="$(wc -l < "$secrets_file" | tr -d ' ')"
if [ "$count" -lt 12 ]; then
  echo "FAIL: only $count secrets to look for; the probe minted too few tokens" >&2
  exit 1
fi
# The MCP log must hold the resolver's lines, or the search below proves nothing.
if ! grep "mcp user refused: sub=user_rigFR0002" "$mcp_log" >/dev/null; then
  echo "FAIL: the MCP log holds no resolver line; the log search would prove nothing" >&2
  exit 1
fi
if grep -F -f "$secrets_file" "$mcp_log" >/dev/null \
    || compose logs --no-log-prefix rig-superset 2>&1 | grep -F -f "$secrets_file" >/dev/null; then
  echo "FAIL: the MCP log or the rig-superset log holds a test email, the API key, or a token" >&2
  { cat "$mcp_log"; compose logs --no-log-prefix rig-superset 2>&1; } \
    | grep -n -o -F -f "$secrets_file" | cut -c1-24 | sort -u >&2
  exit 1
fi
echo "PASS: no line of the MCP and rig-superset logs holds one of $count emails, keys, and tokens"
echo "PASS: the MCP auth check"
