#!/usr/bin/env python3
"""Tests of scripts/base_image_watch.py (S5.7). Standard library only.

Run: python3 scripts/test_base_image_watch.py

The end-to-end tests copy the pinned files into a scratch git repository with a bare
remote, put a stub `gh` first on PATH, and feed a mocked tag list. The stub records each
call, so a test asserts the pull request and the check-only dispatch.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import base_image_watch as watch  # noqa: E402

UPSTREAM = [
    "7.0.0rc3", "7.0.0rc3-dev", "7.0.0rc3-lean", "7.0.0rc3-py311", "7.0.0rc3-py312",
    "7.0.0rc2", "7.0.0rc2-dev", "7.0.0rc1", "latest", "master",
]

GH_STUB = """#!/bin/sh
printf '%s\\n<<end>>\\n' "$*" >> "$GH_LOG"
"""


class Pick(unittest.TestCase):
    def test_rc_follows_its_own_line(self):
        self.assertEqual(watch.pick("7.0.0rc2", UPSTREAM), "7.0.0rc3")

    def test_final_beats_its_candidates(self):
        self.assertEqual(watch.pick("7.0.0rc2", ["7.0.0rc3", "7.0.0"]), "7.0.0")

    def test_rc_of_a_later_version_is_ignored(self):
        self.assertEqual(watch.pick("7.0.0rc3", ["7.0.0", "7.0.1rc1"]), "7.0.0")
        self.assertIsNone(watch.pick("7.0.0", ["7.0.1rc1", "7.1.0rc2"]))

    def test_final_patch_and_minor(self):
        self.assertEqual(watch.pick("7.0.0", ["7.0.1", "7.0.2", "7.1.0"]), "7.1.0")

    def test_variants_older_and_other_majors_are_ignored(self):
        self.assertIsNone(watch.pick("7.0.0rc3", UPSTREAM))
        self.assertIsNone(watch.pick("7.0.1", ["7.0.1-dev", "7.0.1-py312", "7.0.0", "8.0.0"]))

    def test_the_pin_must_be_a_release_tag(self):
        with self.assertRaises(SystemExit):
            watch.pick("latest", UPSTREAM)


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo, self.remote = self.tmp / "repo", self.tmp / "remote.git"
        for path, _ in watch.PINS:
            (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / path, self.repo / path)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text(GH_STUB)
        (bin_dir / "gh").chmod(0o755)
        self.log = self.tmp / "gh.log"
        self.env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    "GH_LOG": str(self.log), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        self.git("init", "-q", "--bare", str(self.remote), cwd=self.tmp)
        self.git("init", "-q", "-b", "main")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "init")
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-q", "origin", "main")
        self.pinned = watch.read_pin(self.repo)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env, check=True,
                              text=True, capture_output=True).stdout

    def watch(self, tags):
        tags_file = self.tmp / "tags.json"
        tags_file.write_text(json.dumps(tags))
        return subprocess.run(
            [sys.executable, str(HERE / "base_image_watch.py"), "--root", str(self.repo),
             "--tags-file", str(tags_file)],
            env=self.env, text=True, capture_output=True)

    def gh_calls(self):
        if not self.log.exists():
            return []
        return [c.strip("\n") for c in self.log.read_text().split("<<end>>") if c.strip()]

    def test_a_newer_tag_opens_a_pull_request_and_dispatches_the_check(self):
        new = "7.99.0"
        res = self.watch([new, f"{new}-dev", self.pinned])
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        branch = f"base-image/{new}"
        self.assertIn(branch, self.git("ls-remote", "--heads", "origin", branch))
        for path, _ in watch.PINS:
            text = self.git("--git-dir", str(self.remote), "show", f"{branch}:{path}")
            self.assertIn(new, text, path)
            self.assertNotIn(self.pinned, text, path)
        calls = self.gh_calls()
        self.assertEqual(len(calls), 2, calls)
        self.assertTrue(calls[0].startswith(f"pr create --base main --head {branch} "), calls[0])
        self.assertEqual(calls[1], f"workflow run docker-publish.yml --ref {branch} "
                                   f"-f base_tag={new} -f check_only=true")

    def test_up_to_date_does_nothing(self):
        res = self.watch([self.pinned, "7.0.0rc1", "6.0.0"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("up to date", res.stdout)
        self.assertEqual(self.gh_calls(), [])

    def test_an_existing_branch_is_skipped(self):
        self.git("push", "-q", "origin", "main:base-image/7.99.0")
        res = self.watch(["7.99.0"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("skipping", res.stdout)
        self.assertEqual(self.gh_calls(), [])


if __name__ == "__main__":
    unittest.main()
