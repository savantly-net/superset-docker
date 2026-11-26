"""`franops-bi certify` against a fake BI and a real git repository (S7.5).

The rig test (rig/hq-access-check.sh) runs the same command against BI itself.
"""

from __future__ import annotations

import io
import pathlib
import shutil
import subprocess
import zipfile
from typing import Any

import pytest
import yaml

from franops_bi import bundle as b
from franops_bi.certify import CertifyError, certify

FIXTURE = pathlib.Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "bi"
DATASET_UUID = "40f30f6d-2d79-411e-be3d-da1f69d94170"
FIXTURE_CHART_UUID = "9ef54bbb-5731-4a70-95f8-c16fb5cf69bd"
NEW_UUID = "11111111-2222-4333-8444-555555555555"


def git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "brand"
    shutil.copytree(FIXTURE, root / "bi")
    git(root, "init", "--quiet", "-b", "main")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "test")
    git(root, "add", ".")
    git(root, "commit", "--quiet", "-m", "fixture")
    return root


def chart_doc(dataset_uuid: str = DATASET_UUID, uid: str = NEW_UUID) -> dict[str, Any]:
    doc = yaml.safe_load(next((FIXTURE / "charts").glob("*.yaml")).read_text())
    doc.update(slice_name="HQ draft chart", uuid=uid, dataset_uuid=dataset_uuid, tags=["draft"])
    return doc


def dashboard_doc(chart_uuid: str) -> dict[str, Any]:
    doc = yaml.safe_load(next((FIXTURE / "dashboards").glob("*.yaml")).read_text())
    doc.update(dashboard_title="HQ draft dashboard", uuid=NEW_UUID, tags=["draft"])
    doc["position"]["CHART-1"]["meta"]["uuid"] = chart_uuid
    return doc


def export_zip(folder: str, doc: dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("export/metadata.yaml", "version: 1.0.0\ntype: Slice\n")
        z.writestr(f"export/{folder}/asset_7.yaml", yaml.safe_dump(doc))
        # The export also carries the dataset and the database; certify writes neither.
        z.writestr("export/datasets/warehouse/calls.yaml", "table_name: calls\n")
    return buf.getvalue()


class FakeBI:
    """The tag API of one asset (id 7). `refuse` maps a method to the status that BI answers."""

    def __init__(self, folder: str, doc: dict[str, Any], tags: set[str], refuse: dict[str, int] | None = None):
        self.blob = export_zip(folder, doc)
        self.resource = "chart" if folder == "charts" else "dashboard"
        self.tags = set(tags)
        self.refuse = refuse or {}
        self.calls: list[str] = []

    def export(self, resource: str, ids: list[int]) -> bytes:
        assert (resource, ids) == (self.resource, [7])
        self.calls.append("export")
        return self.blob

    def tagged_objects(self, tag: str) -> list[dict[str, Any]]:
        rows = [{"type": self.resource, "id": 7}] if tag in self.tags else []
        return rows + [{"type": "dataset", "id": 7}]

    def request(self, method: str, path: str, body: Any = None, **kw: Any) -> tuple[int, Any]:
        self.calls.append(f"{method} {path}")
        if method in self.refuse:
            return self.refuse[method], {"message": "Forbidden"}
        if method == "DELETE":
            self.tags.discard(path.rstrip("/").rsplit("/", 1)[1])
        else:
            self.tags.update(body["properties"]["tags"])
        return (200 if method == "DELETE" else 201), {}


def test_a_draft_chart_is_swapped_exported_and_committed_on_a_new_branch(repo):
    bi = FakeBI("charts", chart_doc(), {"draft"})
    result = certify(bi, "chart", 7, repo)
    assert bi.tags == {"certified"}
    # draft goes first, so a half swap never leaves both tags
    assert bi.calls == ["export", "DELETE /api/v1/tag/2/7/draft/", "POST /api/v1/tag/2/7/"]
    assert result.branch == "certify/chart-7" and result.swapped
    assert git(repo, "branch", "--show-current").strip() == "certify/chart-7"
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "diff", "--name-only", "main", "HEAD").split() == [result.path]
    written = yaml.safe_load((repo / result.path).read_text())
    assert written["tags"] == ["certified"] and written["uuid"] == NEW_UUID
    # the folder still loads, and deploy's rewrite keeps the tag
    rewritten = b.rewrite(b.load(repo / "bi"))
    assert yaml.safe_load(rewritten[result.path.removeprefix("bi/")])["tags"] == ["certified"]


def test_a_chart_that_is_in_bi_already_is_written_over_its_own_file(repo):
    bi = FakeBI("charts", chart_doc(uid=FIXTURE_CHART_UUID), {"certified"})
    result = certify(bi, "chart", 7, repo)
    assert result.path == "bi/charts/Calls_per_location_1.yaml"
    assert not result.swapped and bi.calls == ["export"]


def test_a_user_who_is_not_a_reviewer_changes_nothing(repo):
    bi = FakeBI("charts", chart_doc(), {"draft"}, refuse={"DELETE": 403, "POST": 403})
    with pytest.raises(CertifyError, match="reviewer") as err:
        certify(bi, "chart", 7, repo)
    assert not err.value.changed_bi
    assert bi.tags == {"draft"}
    assert git(repo, "branch", "--list").split() == ["*", "main"]
    assert git(repo, "status", "--porcelain") == ""


def test_a_refused_add_after_the_remove_says_that_the_asset_has_neither_tag(repo):
    bi = FakeBI("charts", chart_doc(), {"draft"}, refuse={"POST": 403})
    with pytest.raises(CertifyError, match="neither tag") as err:
        certify(bi, "chart", 7, repo)
    assert err.value.changed_bi and bi.tags == set()
    assert git(repo, "branch", "--list").split() == ["*", "main"]


def test_a_chart_on_a_dataset_that_is_not_in_bi_is_refused_before_the_swap(repo):
    bi = FakeBI("charts", chart_doc(dataset_uuid="99999999-0000-4000-8000-000000000000"), {"draft"})
    with pytest.raises(CertifyError, match="not in bi/"):
        certify(bi, "chart", 7, repo)
    assert bi.calls == ["export"] and bi.tags == {"draft"}


def test_a_dashboard_with_a_chart_that_is_not_in_bi_is_refused_before_the_swap(repo):
    bi = FakeBI("dashboards", dashboard_doc(NEW_UUID), {"draft"})
    with pytest.raises(CertifyError, match="certify each one first"):
        certify(bi, "dashboard", 7, repo)
    assert bi.calls == ["export"] and bi.tags == {"draft"}


def test_a_dashboard_of_certified_charts_is_certified(repo):
    bi = FakeBI("dashboards", dashboard_doc(FIXTURE_CHART_UUID), {"draft"})
    result = certify(bi, "dashboard", 7, repo)
    assert bi.calls[1] == "DELETE /api/v1/tag/3/7/draft/" and bi.tags == {"certified"}
    assert result.path.startswith("bi/dashboards/HQ_draft_dashboard_")


def test_a_work_tree_with_changes_is_refused_before_the_export(repo):
    (repo / "bi" / "charts" / "Calls_per_location_1.yaml").write_text("changed: true\n")
    bi = FakeBI("charts", chart_doc(), {"draft"})
    with pytest.raises(CertifyError, match="changes"):
        certify(bi, "chart", 7, repo)
    assert bi.calls == []


def test_a_branch_that_exists_is_refused_before_the_export(repo):
    git(repo, "branch", "certify/chart-7")
    bi = FakeBI("charts", chart_doc(), {"draft"})
    with pytest.raises(CertifyError, match="exists already"):
        certify(bi, "chart", 7, repo)
    assert bi.calls == []


def test_a_refused_export_stops_before_the_swap(repo):
    bi = FakeBI("charts", chart_doc(), {"draft"})

    def refuse(resource: str, ids: list[int]) -> bytes:
        from franops_bi.client import SupersetError
        raise SupersetError("GET", "/api/v1/chart/export/", 403, {"message": "Forbidden"})

    bi.export = refuse  # type: ignore[method-assign]
    with pytest.raises(CertifyError, match="reviewer") as err:
        certify(bi, "chart", 7, repo)
    assert not err.value.changed_bi and bi.calls == [] and bi.tags == {"draft"}
