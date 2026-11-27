#!/usr/bin/env bash
# The S5.5 rig tests end to end: an empty rig, the deploy tests, then the rig is torn down.
#
# Usage: rig/test-franops-bi.sh            (KEEP=1 leaves the rig up afterwards)
# Needs Docker and a Python with franops-bi installed:
#   pip install -e 'tools/franops_bi[test]'
set -euo pipefail
cd "$(dirname "$0")/.."

cleanup() {
  status=$?
  if [ "${KEEP:-0}" != "1" ]; then rig/rig.sh down >/dev/null 2>&1 || true; fi
  exit "$status"
}
trap cleanup EXIT

rig/rig.sh up
FRANOPS_BI_RIG=1 python -m pytest -m rig -s -rxX tools/franops_bi/tests/test_rig.py
# S4.11 needs an empty BI of its own.
rig/rig.sh up
FRANOPS_BI_RIG=1 python -m pytest -m rig -s -rxX tools/franops_bi/tests/test_rig_sources.py
