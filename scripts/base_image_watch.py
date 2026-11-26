#!/usr/bin/env python3
"""S5.7: open a pull request when upstream publishes a newer apache/superset base tag.

NFR-7: the team applies each security patch of the BI image within 14 days of the upstream
release. `.github/workflows/base-image-watch.yml` runs this script each day.

The script:
1. reads the pinned base tag from the Dockerfile;
2. lists the apache/superset tags on Docker Hub (or reads `--tags-file`, for the test);
3. picks the newest candidate (see `pick`); with none, it ends with "up to date";
4. skips the tag when its branch `base-image/<tag>` already exists on the remote;
5. writes the tag into every pin (`PINS`), commits, pushes the branch, and opens a pull request;
6. dispatches `docker-publish.yml` on the branch with `check_only=true`.

Step 6 exists because a pull request that the workflow's GITHUB_TOKEN opens starts no
`pull_request` run. A `workflow_dispatch` is the one event that the token can start, and
`check_only` runs every check of the image, the S5.6 RLS check included, with no publish.
Its check runs attach to the branch head, so they show on the pull request.

Only the standard library: the workflow runs it with no install.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HUB_URL = "https://hub.docker.com/v2/repositories/apache/superset/tags?page_size=100&name={major}."

# Each file that pins the base tag, and the pattern of its pin. Group 1 is the tag.
PINS: list[tuple[str, str]] = [
    ("Dockerfile", r"^FROM apache/superset:\$\{BASE_TAG:-([^}]+)\}"),
    ("rig/compose.yml", r"BASE_TAG: \$\{BASE_TAG:-([^}]+)\}"),
    ("deploy.sh", r"^BASE_TAG=(\S+)$"),
    ("tests/fixtures/writes-into-package/Dockerfile", r"^ARG BASE_TAG=(\S+)$"),
]

# Release tags only: the -dev, -lean and -pyNNN variants of a release do not match.
TAG_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:rc(\d+))?$")


def parse(tag: str) -> tuple[int, int, int, float] | None:
    """A sortable key. A release candidate sorts before its final release."""
    m = TAG_RE.match(tag)
    if not m:
        return None
    rc = float(m.group(4)) if m.group(4) is not None else float("inf")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), rc)


def pick(pinned: str, tags: list[str]) -> str | None:
    """The newest tag to move to, or None.

    - The candidate has the major version of the pin.
    - A final release qualifies when it is newer than the pin.
    - A release candidate qualifies only while the pin is a release candidate of the same
      version: the pin then follows its own line to the final release, and never moves to
      the candidate of a later version.
    """
    pin = parse(pinned)
    if pin is None:
        raise SystemExit(f"base-image-watch: the pinned tag '{pinned}' is not a release tag")
    best: tuple[tuple[int, int, int, float], str] | None = None
    for tag in tags:
        key = parse(tag)
        if key is None or key[0] != pin[0] or key <= pin:
            continue
        if key[3] != float("inf") and (pin[3] == float("inf") or key[:3] != pin[:3]):
            continue
        if best is None or key > best[0]:
            best = (key, tag)
    return best[1] if best else None


def read_pin(root: Path) -> str:
    path, pattern = PINS[0]
    m = re.search(pattern, (root / path).read_text(), re.M)
    if not m:
        raise SystemExit(f"base-image-watch: no pin in {path}")
    return m.group(1)


def write_pins(root: Path, new: str) -> list[str]:
    changed = []
    for path, pattern in PINS:
        file = root / path
        text = file.read_text()
        out, n = re.subn(pattern, lambda m: m.group(0).replace(m.group(1), new), text, flags=re.M)
        if n != 1:
            raise SystemExit(f"base-image-watch: {n} pins in {path}, expected 1")
        if out != text:
            file.write_text(out)
            changed.append(path)
    return changed


def hub_tags(major: int) -> list[str]:
    tags, url = [], HUB_URL.format(major=major)
    while url:
        with urllib.request.urlopen(url, timeout=30) as resp:
            page = json.load(resp)
        tags += [t["name"] for t in page.get("results", [])]
        url = page.get("next")
    return tags


def run(*cmd: str, root: Path, capture: bool = False) -> str:
    print("+", " ".join(cmd), flush=True)
    res = subprocess.run(cmd, cwd=root, check=True, text=True,
                         stdout=subprocess.PIPE if capture else None)
    return res.stdout or ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--tags-file", type=Path, help="a JSON list of tags, in place of Docker Hub")
    ap.add_argument("--base", default="main", help="the branch the pull request targets")
    ap.add_argument("--remote", default="origin")
    args = ap.parse_args(argv)
    root = args.root

    pinned = read_pin(root)
    tags = json.loads(args.tags_file.read_text()) if args.tags_file else hub_tags(parse(pinned)[0])
    new = pick(pinned, tags)
    if new is None:
        print(f"base-image-watch: up to date on {pinned}")
        return 0

    branch = f"base-image/{new}"
    if run("git", "ls-remote", "--heads", args.remote, branch, root=root, capture=True).strip():
        print(f"base-image-watch: {branch} exists on {args.remote}; skipping {new}")
        return 0

    print(f"base-image-watch: {pinned} -> {new}")
    run("git", "checkout", "-b", branch, root=root)
    changed = write_pins(root, new)
    run("git", "add", "--", *changed, root=root)
    run("git", "commit", "-m", f"chore: base image apache/superset {pinned} -> {new} (S5.7)", root=root)
    run("git", "push", args.remote, branch, root=root)
    body = (
        f"Upstream published `apache/superset:{new}`. This pull request moves every pin from "
        f"`{pinned}` to `{new}`.\n\n"
        "NFR-7: apply each security patch of the BI image within 14 days of the upstream release.\n\n"
        "A pull request that GITHUB_TOKEN opens starts no `pull_request` run, so the watch "
        "dispatched `docker-publish.yml` on this branch with `check_only=true`. That run holds "
        "every image check, the S5.6 RLS check included, and it publishes nothing.\n\n"
        "Merging publishes nothing either: a `v*` git tag publishes."
    )
    run("gh", "pr", "create", "--base", args.base, "--head", branch,
        "--title", f"Base image apache/superset {new}", "--body", body, root=root)
    run("gh", "workflow", "run", "docker-publish.yml", "--ref", branch,
        "-f", f"base_tag={new}", "-f", "check_only=true", root=root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
