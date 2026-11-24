"""`franops-bi certify`: promote one draft chart or dashboard (S7.5, FR-7.4, decision D-6).

The order of the steps is the safety of the command:

1. Check the brand repository: a git work tree with no change, a `bi/` folder that loads, and
   no branch of the planned name. Nothing writes yet.
2. Export the asset from BI. Its YAML must refer only to assets that `bi/` already holds: the
   dataset of a chart, and each chart of a dashboard. Else the command stops, and certifies
   nothing. A dashboard with a draft chart needs that chart certified first.
3. Swap the tags as the reviewer: remove `draft`, then add `certified`. BI refuses the swap for
   a user who is not a reviewer (`403`), so a user who is not a reviewer stops here, before the
   repository changes. `draft` goes first: a half swap leaves neither tag, never both.
4. Read the tags back: `certified`, and no `draft`.
5. Write the YAML into `bi/` with `tags: [certified]`, on a new branch, and commit it. The
   reviewer pushes the branch and opens the pull request. The command never pushes.

A reviewer is an account that BI lets write tags on another user's asset: an `Admin`, which in
the MVP is a Savantly operator (D-6). `HQ` has `can_read` on Tag only.
⚠️ The login is the `db` provider, as for `deploy`. A BI instance in OAuth mode refuses it for
each account but the guest-token service account (S5.8).
"""

from __future__ import annotations

import io
import pathlib
import re
import subprocess
import zipfile
from dataclasses import dataclass
from typing import Any, Protocol

import yaml

from . import bundle as b
from .client import SupersetError

DRAFT = "draft"

# asset type -> (the REST resource, the tag API object type, the folder of bi/)
TYPES = {"chart": ("chart", 2, "charts"), "dashboard": ("dashboard", 3, "dashboards")}


class CertifyError(RuntimeError):
    """The command stopped. `changed_bi` says whether the tags in BI changed before the stop."""

    def __init__(self, message: str, changed_bi: bool = False):
        self.changed_bi = changed_bi
        super().__init__(message)


class Client(Protocol):
    def request(self, method: str, path: str, body: Any = None, **kw: Any) -> tuple[int, Any]: ...
    def tagged_objects(self, tag: str) -> list[dict[str, Any]]: ...
    def export(self, resource: str, ids: list[int]) -> bytes: ...


@dataclass
class Result:
    branch: str
    path: str
    swapped: bool


def branch_name(asset_type: str, asset_id: int) -> str:
    return f"certify/{asset_type}-{asset_id}"


def _git(repo: pathlib.Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if done.returncode != 0:
        raise CertifyError(f"git {' '.join(args)}: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout


def check_repo(repo: pathlib.Path, branch: str) -> b.Bundle:
    """Step 1. Raise CertifyError on a repository that the command must not change."""
    if not (repo / ".git").exists():
        raise CertifyError(f"{repo}: not the root of a git work tree")
    if _git(repo, "status", "--porcelain").strip():
        raise CertifyError(f"{repo}: the work tree has changes; commit or stash them first")
    exists = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        capture_output=True,
    )
    if exists.returncode == 0:
        raise CertifyError(f"{repo}: the branch {branch} exists already")
    try:
        return b.load(repo / "bi")
    except b.BundleError as err:
        raise CertifyError("bi/ does not load: " + "; ".join(err.problems)) from None


def exported_yaml(blob: bytes, folder: str) -> dict[str, Any]:
    """The one YAML of `folder` in an export ZIP. The ZIP has one root folder."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        docs = [
            yaml.safe_load(z.read(name))
            for name in z.namelist()
            if name.split("/")[1:2] == [folder] and name.endswith((".yaml", ".yml"))
        ]
    if len(docs) != 1:
        raise CertifyError(f"the export holds {len(docs)} {folder} YAML files, not 1")
    return docs[0]


def check_references(asset_type: str, doc: dict[str, Any], bi: b.Bundle) -> None:
    """Step 2. Each asset that `doc` refers to must be in bi/ already."""
    if asset_type == "chart":
        datasets = {str(d.get("uuid")) for d in bi.assets("datasets").values()}
        if str(doc.get("dataset_uuid")) not in datasets:
            raise CertifyError(
                f"the dataset {doc.get('dataset_uuid')} of the chart is not in bi/: a certified "
                "chart is on a certified dataset only"
            )
        return
    charts = {str(d.get("uuid")) for d in bi.assets("charts").values()}
    missing = [u for u in b._dashboard_chart_uuids(doc) if u not in charts]
    if missing:
        raise CertifyError(
            "these charts of the dashboard are not in bi/; certify each one first: "
            + ", ".join(missing)
        )


def target_path(asset_type: str, doc: dict[str, Any], bi: b.Bundle) -> str:
    """The path in bi/: the file that holds the same UUID, else a new file named for the asset."""
    folder = TYPES[asset_type][2]
    uid = str(doc.get("uuid"))
    for rel, existing in bi.assets(folder).items():
        if str(existing.get("uuid")) == uid:
            return rel
    title = doc.get("slice_name") if asset_type == "chart" else doc.get("dashboard_title")
    stem = re.sub(r"[^A-Za-z0-9]+", "_", str(title or asset_type)).strip("_") or asset_type
    rel = f"{folder}/{stem}_{uid[:8]}.yaml"
    if rel in bi.documents:
        raise CertifyError(f"bi/{rel} exists and holds another asset")
    return rel


def tagged_ids(client: Client, tag: str, object_type: str) -> set[int]:
    return {int(o["id"]) for o in client.tagged_objects(tag) if str(o.get("type")) == object_type}


def swap_tags(client: Client, asset_type: str, asset_id: int) -> bool:
    """Steps 3 and 4. True when the tags changed; False when the asset was certified already."""
    resource, object_type, _ = TYPES[asset_type]
    is_draft = asset_id in tagged_ids(client, DRAFT, resource)
    is_certified = asset_id in tagged_ids(client, b.CERTIFIED, resource)
    if is_certified and not is_draft:
        return False
    if is_draft:
        status, body = client.request(
            "DELETE", f"/api/v1/tag/{object_type}/{asset_id}/{DRAFT}/", expect=None
        )
        if status != 200:
            raise CertifyError(
                f"BI refused to remove {DRAFT} (HTTP {status}: {body}); is this account a reviewer?"
            )
    if not is_certified:
        status, body = client.request(
            "POST",
            f"/api/v1/tag/{object_type}/{asset_id}/",
            {"properties": {"tags": [b.CERTIFIED]}},
            expect=None,
        )
        if status not in (200, 201):
            raise CertifyError(
                f"BI refused to add {b.CERTIFIED} (HTTP {status}: {body}) after it removed "
                f"{DRAFT}; the asset has neither tag. Run the command again as a reviewer.",
                changed_bi=is_draft,
            )
    if asset_id in tagged_ids(client, DRAFT, resource) or asset_id not in tagged_ids(
        client, b.CERTIFIED, resource
    ):
        raise CertifyError("the tags read back are not `certified` alone", changed_bi=True)
    return True


def certify(client: Client, asset_type: str, asset_id: int, repo: str | pathlib.Path) -> Result:
    if asset_type not in TYPES:
        raise CertifyError(f"certify: the type is chart or dashboard, not {asset_type}")
    repo = pathlib.Path(repo)
    branch = branch_name(asset_type, asset_id)
    bi = check_repo(repo, branch)
    resource, _, folder = TYPES[asset_type]
    try:
        blob = client.export(resource, [asset_id])
    except SupersetError as err:
        raise CertifyError(f"BI refused the export of the {asset_type} (HTTP {err.status}); "
                           "is this account a reviewer?") from None
    doc = exported_yaml(blob, folder)
    check_references(asset_type, doc, bi)
    rel = target_path(asset_type, doc, bi)
    swapped = swap_tags(client, asset_type, asset_id)

    out = dict(doc)
    out["tags"] = [b.CERTIFIED]
    try:
        _git(repo, "switch", "--quiet", "-c", branch)
        path = repo / "bi" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(out, sort_keys=False, allow_unicode=True), encoding="utf-8")
        b.load(repo / "bi")  # the new file must keep bi/ valid for deploy
        _git(repo, "add", "--", f"bi/{rel}")
        title = doc.get("slice_name") or doc.get("dashboard_title") or asset_id
        _git(repo, "commit", "--quiet", "-m", f"bi: certify {asset_type} {title}")
    except (CertifyError, b.BundleError, OSError) as err:
        raise CertifyError(
            f"BI tags the {asset_type} {b.CERTIFIED}, but the export into {repo} failed: {err}",
            changed_bi=True,
        ) from None
    return Result(branch=branch, path=f"bi/{rel}", swapped=swapped)
