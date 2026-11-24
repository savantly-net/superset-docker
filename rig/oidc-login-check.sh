#!/usr/bin/env bash
# The S5.2 login check on the rig, end to end: OIDC sign-in through a fake provider, with the
# BI role from a fake FranOps `userinfo` endpoint.
#
# Usage: rig/oidc-login-check.sh            (KEEP=1 leaves the rig up afterwards)
# RIG_SKIP_BUILD=1 checks the image already tagged franops-bi-rig/superset:local.
# COMPOSE_PROJECT_NAME, RIG_SUPERSET_PORT, and RIG_META_PORT move the rig beside another one.
#
# 1. An empty rig in OAuth mode (compose.oauth.yml), with compose.oidc.yml: the fake provider,
#    the fake FranOps, and the issuer URL only (no endpoint variable).
# 2. rig/oidc/login_probe.py, inside rig-superset: an HQ user signs in and gets HQ; a franchisee
#    and a user whom FranOps cannot answer are refused, and no user is made for them; the
#    database login form gives no session; the service account still gets an API token (S5.8).
# 3. The logs of rig-superset hold no test email, no API key, and no token that the fake
#    provider issued.
set -euo pipefail
cd "$(dirname "$0")"

compose() { docker compose -f compose.yml -f compose.oauth.yml -f compose.oidc.yml "$@"; }
secrets_file="$(mktemp)"

cleanup() {
  status=$?
  rm -f "$secrets_file"
  if [ "$status" -ne 0 ]; then
    compose logs --tail 60 rig-superset rig-fake-oidc rig-fake-franops >&2 2>&1 || true
  fi
  if [ "${KEEP:-0}" != "1" ]; then compose down --volumes --remove-orphans >/dev/null 2>&1 || true; fi
  exit "$status"
}
trap cleanup EXIT

compose down --volumes --remove-orphans >/dev/null 2>&1 || true
if [ "${RIG_SKIP_BUILD:-0}" != "1" ]; then compose build rig-superset; fi
compose up --detach --wait --wait-timeout "${RIG_TIMEOUT:-420}" rig-superset

echo "== OIDC login probe"
# Not `python -I`: the probe loads the server's configuration, and -I drops PYTHONPATH.
compose exec -T -w /rig-oidc rig-superset python /rig-oidc/login_probe.py

echo "== The logs hold no email, no API key, and no token"
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
if [ "$count" -lt 6 ]; then
  echo "FAIL: only $count secrets to look for; the probe issued too few tokens" >&2
  exit 1
fi
# The log must hold the login lines, or the search below proves nothing.
if ! compose logs --no-log-prefix rig-superset 2>&1 | grep "oauth login refused: provider=oidc sub=user_rigFR0002" >/dev/null; then
  echo "FAIL: the rig-superset log holds no login line; the log search would prove nothing" >&2
  exit 1
fi
if compose logs --no-log-prefix rig-superset 2>&1 | grep -F -f "$secrets_file" >/dev/null; then
  echo "FAIL: the rig-superset log holds a test email, the API key, or a token" >&2
  compose logs --no-log-prefix rig-superset 2>&1 | grep -n -o -F -f "$secrets_file" | cut -c1-24 | sort -u >&2
  exit 1
fi
echo "PASS: no line of the rig-superset log holds one of $count emails, keys, and tokens"
echo "PASS: the OIDC login check"
