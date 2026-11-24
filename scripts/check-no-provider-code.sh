#!/usr/bin/env bash
# The image holds no code for one identity provider (S5.2, FR-5.2, decision D-3): the provider
# is read from its discovery document, and its name is configuration. This check fails when a
# file that goes into the image names Clerk, the provider of the MVP.
#
# Usage: scripts/check-no-provider-code.sh [path ...]   (default: config/ and the Dockerfile)
# scripts/test-no-provider-code.sh proves that it fails on a hit.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "$#" -eq 0 ]; then set -- config Dockerfile; fi
PATTERN="${PROVIDER_PATTERN:-clerk}"

if hits="$(grep -rniI --exclude-dir=__pycache__ -e "$PATTERN" "$@")"; then
  echo "check-no-provider-code: FAIL: these lines name the provider '$PATTERN':" >&2
  echo "$hits" >&2
  exit 1
fi
echo "check-no-provider-code: OK: no line of $* names '$PATTERN'"
