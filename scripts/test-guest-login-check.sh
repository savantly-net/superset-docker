#!/usr/bin/env bash
# Negative test of the service login and embed check: break it three ways and expect
# scripts/guest-login-check.sh to fail on each of them.
#
# Usage: scripts/test-guest-login-check.sh <image> [timeout-seconds]
#
# The faults, in one superset_config_docker.py:
# 1. Superset's own security manager: the API login is open to each user with a password.
# 2. GUEST_ROLE_NAME = "Public": a guest token gets no permission, and chart data is 403.
# 3. Superset's default Talisman settings: X-Frame-Options SAMEORIGIN, no frame-ancestors.
set -euo pipefail

IMAGE="${1:?usage: $0 <image> [timeout-seconds]}"
TIMEOUT="${2:-240}"
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cat > "$WORK/superset_config_docker.py" <<'PY'
from superset.config import TALISMAN_CONFIG  # noqa: F401  (the default settings)
CUSTOM_SECURITY_MANAGER = None
GUEST_ROLE_NAME = "Public"
PY

if "$HERE/guest-login-check.sh" "$IMAGE" "$TIMEOUT" "$WORK/superset_config_docker.py" \
    > "$WORK/out.log" 2>&1; then
  echo "test-guest-login-check: FAIL: the check passed with the faults in place" >&2
  tail -40 "$WORK/out.log" >&2
  exit 1
fi

missing=0
expect() {
  if ! grep -qE "$1" "$WORK/out.log"; then
    echo "test-guest-login-check: FAIL: no line matches: $2" >&2
    missing=1
  fi
}
expect '^FAIL a password user is refused: 200' "the API login open to another password user"
expect '^FAIL a GuestIssuer user with Admin is refused: 200' "the API login open to an Admin"
expect '^FAIL GUEST_ROLE_NAME is Embed: Public' "the Public guest role"
expect '^FAIL frame-ancestors holds each embed origin' "no frame-ancestors"
expect '^FAIL no X-Frame-Options header: SAMEORIGIN' "X-Frame-Options SAMEORIGIN"
if [ "$missing" -ne 0 ]; then
  grep -E '^(PASS|FAIL|\{)' "$WORK/out.log" >&2 || tail -40 "$WORK/out.log" >&2
  exit 1
fi
echo "test-guest-login-check: OK: the check fails on each of the three faults"
grep -E '^FAIL' "$WORK/out.log" || true
