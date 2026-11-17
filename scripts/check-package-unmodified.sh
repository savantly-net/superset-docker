#!/usr/bin/env bash
# Fails when an image changes the `superset` package of its base image.
#
# Usage: scripts/check-package-unmodified.sh <base-image> <built-image>
#
# The check compares the built image with the base image, not the Dockerfile text.
# A Dockerfile lint cannot see what a RUN step (sed, pip, a script) does to the package,
# but the filesystem of the built image shows every net change, from any instruction.
#
# The base image holds two copies of the package: the installed copy in site-packages,
# and the copy that resolves from the working directory (/app/superset), which the server
# imports. The check resolves both inside each image with importlib (it hardcodes neither
# path), and it fails when:
#   - either copy resolves to a different location in the built image (for example, a
#     dependency install that shadows the package, or a changed WORKDIR);
#   - any file, directory, or symlink under either copy is added, removed, or changed
#     (content, mode, or link target).
# It fails closed: an empty manifest or a failure to resolve the package is an error.
#
# Exit codes: 0 = unchanged, 1 = the image writes into the package, 2 = usage or check error.
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <base-image> <built-image>" >&2
  exit 2
fi

BASE_IMAGE="$1"
BUILT_IMAGE="$2"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

manifest() {
  # -I: ignore PYTHON* variables and the current directory; the manifest script adds the
  # image's working directory itself. --network none: the check needs no network.
  local workdir
  workdir="$(docker image inspect --format '{{.Config.WorkingDir}}' "$1")" || return 1
  docker run --rm -i --network none --user 0 --workdir / --entrypoint python \
    "$1" -I - "${workdir:-/}" < "$SCRIPT_DIR/package_manifest.py" > "$2"
}

if ! manifest "$BASE_IMAGE" "$WORK_DIR/base.json"; then
  echo "check-package-unmodified: ERROR: cannot read the package manifest of $BASE_IMAGE" >&2
  exit 2
fi
if ! manifest "$BUILT_IMAGE" "$WORK_DIR/built.json"; then
  echo "check-package-unmodified: ERROR: cannot read the package manifest of $BUILT_IMAGE" >&2
  exit 2
fi

python3 "$SCRIPT_DIR/compare_manifests.py" \
  "$WORK_DIR/base.json" "$WORK_DIR/built.json" "$BASE_IMAGE" "$BUILT_IMAGE"
