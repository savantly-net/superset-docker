#!/usr/bin/env bash
# Negative test of the public lockdown check: break the lockdown three ways and expect
# scripts/public-lockdown-check.sh to fail on each of them.
#
# Usage: scripts/test-lockdown-check.sh <image> [timeout-seconds]
#
# The faults, in one superset_config_docker.py:
# 1. PUBLIC_ROLE_LIKE = "Gamma": anonymous callers get Gamma's read permissions.
# 2. The default CSRF exemption list: the guest-token route answers 400, not 401.
# 3. No GUEST_TOKEN_VALIDATOR_HOOK.
set -euo pipefail

IMAGE="${1:?usage: $0 <image> [timeout-seconds]}"
TIMEOUT="${2:-240}"
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cat > "$WORK/superset_config_docker.py" <<'PY'
from superset.config import WTF_CSRF_EXEMPT_LIST  # noqa: F401  (the default list)
PUBLIC_ROLE_LIKE = "Gamma"
GUEST_TOKEN_VALIDATOR_HOOK = None
PY

if "$HERE/public-lockdown-check.sh" "$IMAGE" "$TIMEOUT" "$WORK/superset_config_docker.py" \
    > "$WORK/out.log" 2>&1; then
  echo "test-lockdown-check: FAIL: the check passed with the lockdown broken" >&2
  tail -40 "$WORK/out.log" >&2
  exit 1
fi

missing=0
expect() {
  if ! grep -qE "$1" "$WORK/out.log"; then
    echo "test-lockdown-check: FAIL: no line matches: $2" >&2
    missing=1
  fi
}
expect '^FAIL [a-z_]+: ' "a route group open to Gamma's permissions"
expect '^FAIL POST /api/v1/security/guest_token/ gave \[400\]' "the guest-token route without the CSRF exemption"
expect '^FAIL GUEST_TOKEN_VALIDATOR_HOOK is not set' "the missing validator hook"
if [ "$missing" -ne 0 ]; then
  grep -E '^(PASS|FAIL|\{)' "$WORK/out.log" >&2 || tail -40 "$WORK/out.log" >&2
  exit 1
fi
echo "test-lockdown-check: OK: the check fails on each of the three faults"
grep -E '^FAIL' "$WORK/out.log" | grep -v '^FAIL [a-z_]*: ' || true
grep -cE '^FAIL [a-z_]+: ' "$WORK/out.log" | sed 's/^/test-lockdown-check: open groups: /'
