"""Print a manifest of the `superset` package. Runs inside an image with `python -I`.

The base image holds two copies of the package:
  - "installed": the copy in site-packages, which resolves with no working directory;
  - "runtime": the copy that resolves from the image's working directory (/app/superset in
    the base image). Gunicorn and the `superset` CLI start in that directory and import it.
The script resolves each copy with importlib and prints, for each one, the resolved
directory and one record for each entry under it (type, mode, and a sha256 for a file or
the target for a symlink). It never imports `superset`, so no Superset code and no config
runs.

Usage: python -I - <working-dir> < package_manifest.py
"""
import hashlib
import importlib
import importlib.util
import json
import os
import stat
import sys


def resolve(extra_path):
    saved = list(sys.path)
    try:
        if extra_path:
            sys.path.insert(0, extra_path)
        importlib.invalidate_caches()
        spec = importlib.util.find_spec("superset")
    finally:
        sys.path[:] = saved
        importlib.invalidate_caches()
    if spec is None or not spec.submodule_search_locations:
        sys.stderr.write(f"package_manifest: cannot resolve superset (path {extra_path!r})\n")
        sys.exit(3)
    return spec


def walk(root):
    entries = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            st = os.lstat(path)
            mode = stat.S_IMODE(st.st_mode)
            if stat.S_ISLNK(st.st_mode):
                entries[rel] = ["link", mode, os.readlink(path)]
            elif stat.S_ISDIR(st.st_mode):
                entries[rel] = ["dir", mode, ""]
            elif stat.S_ISREG(st.st_mode):
                digest = hashlib.sha256()
                with open(path, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b""):
                        digest.update(chunk)
                entries[rel] = ["file", mode, digest.hexdigest()]
            else:
                entries[rel] = ["other", mode, ""]
    return entries


working_dir = sys.argv[1] if len(sys.argv) > 1 else ""
packages = {}
for label, extra in (("installed", ""), ("runtime", working_dir)):
    spec = resolve(extra)
    root = os.path.realpath(list(spec.submodule_search_locations)[0])
    packages[label] = {"origin": spec.origin, "root": root, "entries": walk(root)}

json.dump({"packages": packages}, sys.stdout)
