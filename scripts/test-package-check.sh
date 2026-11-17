#!/usr/bin/env bash
# Negative test for scripts/check-package-unmodified.sh.
# Builds tests/fixtures/writes-into-package (a Dockerfile that writes into the superset
# package) and asserts that the check fails on it with exit code 1 and names the file.
#
# Usage: scripts/test-package-check.sh <base-tag>
set -euo pipefail

BASE_TAG="${1:?usage: $0 <base-tag>}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIXTURE_IMAGE="superset-package-check-fixture:local"
OUTPUT="$(mktemp)"
trap 'rm -f "$OUTPUT"; docker image rm -f "$FIXTURE_IMAGE" >/dev/null 2>&1 || true' EXIT

docker build --load --quiet --build-arg "BASE_TAG=$BASE_TAG" -t "$FIXTURE_IMAGE" \
  "$ROOT_DIR/tests/fixtures/writes-into-package" >/dev/null

set +e
"$ROOT_DIR/scripts/check-package-unmodified.sh" "apache/superset:$BASE_TAG" "$FIXTURE_IMAGE" >"$OUTPUT" 2>&1
STATUS=$?
set -e
cat "$OUTPUT"

if [ "$STATUS" -ne 1 ]; then
  echo "test-package-check: FAIL: expected exit code 1 from the check, got $STATUS" >&2
  exit 1
fi
if ! grep -q "FAIL: .* writes into the superset package" "$OUTPUT" \
  || ! grep -q "runtime: added    fixture_runtime_patch.py" "$OUTPUT" \
  || ! grep -q "installed: added    fixture_installed_patch.py" "$OUTPUT"; then
  echo "test-package-check: FAIL: the check did not report the fixture write" >&2
  exit 1
fi
echo "test-package-check: OK: the check fails on a Dockerfile that writes into the package"
