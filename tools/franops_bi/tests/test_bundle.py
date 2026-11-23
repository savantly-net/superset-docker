"""Unit tests for the bi/ folder check and the certify rewrite. No Docker, no network."""

from __future__ import annotations

import io
import pathlib
import shutil
import zipfile

import pytest
import yaml

from franops_bi import bundle as b
from franops_bi.cli import main

FIXTURE = pathlib.Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "bi"


@pytest.fixture
def bi(tmp_path: pathlib.Path) -> pathlib.Path:
    dst = tmp_path / "bi"
    shutil.copytree(FIXTURE, dst)
    return dst


def _one(root: pathlib.Path, asset_type: str) -> pathlib.Path:
    return next((root / asset_type).rglob("*.yaml"))


def _edit(path: pathlib.Path, **changes: object) -> None:
    doc = yaml.safe_load(path.read_text())
    doc.update(changes)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def test_fixture_loads_with_one_of_each_type() -> None:
    assert b.load(FIXTURE).counts() == {
        "databases": 1, "datasets": 1, "charts": 1, "dashboards": 1,
    }


def test_certify_chart_replaces_the_tag_list_and_sets_the_flag() -> None:
    out = b.certify("charts", {"slice_name": "x", "tags": ["draft", "other"]})
    assert out["tags"] == ["certified"]
    assert out["is_managed_externally"] is True


def test_certify_dashboard_adds_tags_when_the_key_is_absent() -> None:
    out = b.certify("dashboards", {"dashboard_title": "x"})
    assert out["tags"] == ["certified"]
    assert out["is_managed_externally"] is True


@pytest.mark.parametrize("asset_type", ["databases", "datasets"])
def test_certify_database_and_dataset_get_the_flag_but_no_tags(asset_type: str) -> None:
    out = b.certify(asset_type, {"uuid": "u"})
    assert out["is_managed_externally"] is True
    assert "tags" not in out


def test_certify_does_not_mutate_its_input() -> None:
    doc = {"tags": ["draft"]}
    b.certify("charts", doc)
    assert doc == {"tags": ["draft"]}


def test_rewrite_leaves_metadata_and_tags_yaml_unchanged(bi: pathlib.Path) -> None:
    files = b.rewrite(b.load(bi))
    assert files["metadata.yaml"] == (bi / "metadata.yaml").read_text()
    assert files["tags.yaml"] == (bi / "tags.yaml").read_text()


def test_rewrite_marks_each_asset_and_keeps_its_other_keys(bi: pathlib.Path) -> None:
    before = b.load(bi)
    files = b.rewrite(before)
    for rel, doc in before.documents.items():
        if "/" not in rel:
            continue
        after = yaml.safe_load(files[rel])
        assert after["is_managed_externally"] is True
        asset_type = rel.split("/", 1)[0]
        if asset_type in ("charts", "dashboards"):
            assert after["tags"] == ["certified"]
        else:
            assert "tags" not in after
        rest = {k: v for k, v in after.items() if k not in ("tags", "is_managed_externally")}
        assert rest == {k: v for k, v in doc.items() if k not in ("tags", "is_managed_externally")}


def test_rewrite_never_writes_the_source_folder(bi: pathlib.Path) -> None:
    snapshot = {p: p.read_bytes() for p in bi.rglob("*") if p.is_file()}
    b.to_zip(b.rewrite(b.load(bi)))
    assert {p: p.read_bytes() for p in bi.rglob("*") if p.is_file()} == snapshot


def test_zip_puts_every_file_under_one_root(bi: pathlib.Path) -> None:
    blob = b.to_zip(b.rewrite(b.load(bi)))
    names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
    assert names and all(n.startswith("bi/") for n in names)
    assert "bi/metadata.yaml" in names


def test_load_skips_files_that_superset_skips(bi: pathlib.Path) -> None:
    (bi / "README.md").write_text("# not an asset\n")
    (bi / "charts" / ".hidden.yaml").write_text(": not yaml")
    assert b.load(bi).counts()["charts"] == 1


def test_broken_yaml_is_a_problem(bi: pathlib.Path) -> None:
    _one(bi, "charts").write_text("slice_name: [unclosed\n")
    with pytest.raises(b.BundleError) as err:
        b.load(bi)
    assert any("does not parse" in p for p in err.value.problems)


def test_missing_metadata_is_a_problem(bi: pathlib.Path) -> None:
    (bi / "metadata.yaml").unlink()
    with pytest.raises(b.BundleError, match="metadata.yaml: missing"):
        b.load(bi)


def test_wrong_metadata_type_is_a_problem(bi: pathlib.Path) -> None:
    (bi / "metadata.yaml").write_text("version: 1.0.0\ntype: Dashboard\n")
    with pytest.raises(b.BundleError, match="type: assets"):
        b.load(bi)


def test_missing_required_key_is_a_problem(bi: pathlib.Path) -> None:
    path = _one(bi, "charts")
    doc = yaml.safe_load(path.read_text())
    del doc["viz_type"]
    path.write_text(yaml.safe_dump(doc))
    with pytest.raises(b.BundleError, match="missing viz_type"):
        b.load(bi)


def test_dangling_dataset_reference_is_a_problem(bi: pathlib.Path) -> None:
    _edit(_one(bi, "charts"), dataset_uuid="00000000-0000-0000-0000-000000000000")
    with pytest.raises(b.BundleError, match="is not in datasets/"):
        b.load(bi)


def test_dangling_chart_on_a_dashboard_is_a_problem(bi: pathlib.Path) -> None:
    for chart in (bi / "charts").glob("*.yaml"):
        chart.unlink()
    with pytest.raises(b.BundleError, match="is not in charts/"):
        b.load(bi)


def test_duplicate_uuid_is_a_problem(bi: pathlib.Path) -> None:
    chart = _one(bi, "charts")
    shutil.copy(chart, chart.with_name("copy.yaml"))
    with pytest.raises(b.BundleError, match="is also in"):
        b.load(bi)


@pytest.mark.parametrize("asset_type", ["databases", "datasets"])
def test_tags_on_a_database_or_dataset_is_a_problem(bi: pathlib.Path, asset_type: str) -> None:
    # Superset 7.0.0rc2 fails the whole import on this key: {'tags': ['Unknown field.']}.
    _edit(_one(bi, asset_type), tags=["certified"])
    with pytest.raises(b.BundleError, match="refuses"):
        b.load(bi)


def test_validate_command_exit_codes(bi: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", "--dir", str(bi)]) == 0
    assert "1 databases, 1 datasets, 1 charts, 1 dashboards" in capsys.readouterr().out
    _one(bi, "dashboards").write_text("dashboard_title: {\n")
    assert main(["validate", "--dir", str(bi)]) == 1
    assert "does not parse" in capsys.readouterr().err


def test_validate_command_refuses_a_missing_folder(tmp_path: pathlib.Path) -> None:
    assert main(["validate", "--dir", str(tmp_path / "nope")]) == 1
