#!/usr/bin/env bash
# Negative test of scripts/check-no-provider-code.sh: a config file that names the provider in
# any case must fail the check, and a clean one must pass.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$WORK/clean" "$WORK/dirty"
printf 'OAUTH_PROVIDERS = [{"name": "oidc"}]\n' > "$WORK/clean/superset_config.py"
printf 'if provider == "CLERK":\n    pass\n' > "$WORK/dirty/superset_config.py"

if ! "$HERE/check-no-provider-code.sh" "$WORK/clean" >/dev/null 2>&1; then
  echo "test-no-provider-code: FAIL: the check failed on a clean config" >&2
  exit 1
fi
if "$HERE/check-no-provider-code.sh" "$WORK/dirty" >/dev/null 2>&1; then
  echo "test-no-provider-code: FAIL: the check passed on a config that names the provider" >&2
  exit 1
fi
echo "test-no-provider-code: OK: the check passes a clean config and fails on a hit"
