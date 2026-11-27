"""S4.11: the deploy reads the source keys from FranOps and imports `bi/<sourceKey>/` of
`franops_sources`, merged with the brand's warehouse database, before the brand `bi/`."""

from __future__ import annotations

import http.server
import json
import pathlib
import shutil
import threading

import pytest
import yaml

from franops_bi import bundle as b
from franops_bi import cli

ROOT = pathlib.Path(__file__).resolve().parents[3]
BRAND = ROOT / "tests" / "fixtures" / "bi"
SOURCES = ROOT / "tests" / "fixtures" / "sources" / "bi"
KEY = "fran_sources_test_key"


class _FranOps(http.server.BaseHTTPRequestHandler):
    keys: list[str] = []
    status = 200

    def do_GET(self):  # noqa: N802
        ok = self.path == cli.SOURCES_PATH and self.headers.get("Authorization") == f"Bearer {KEY}"
        status = self.status if ok else 401
        body = json.dumps({"organizationId": "org_x", "sourceKeys": self.keys}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def franops():
    server = http.server.HTTPServer(("127.0.0.1", 0), _FranOps)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _FranOps.keys, _FranOps.status = [], 200
    yield f"http://127.0.0.1:{server.server_port}", _FranOps
    server.shutdown()


def args(*extra: str):
    return cli.build_parser().parse_args(["deploy", "--dir", str(BRAND), *extra])


def test_the_source_fixture_names_the_warehouse_uuid():
    source = b.load(SOURCES / "callrail", source=True)
    assert source.counts() == {"databases": 0, "datasets": 1, "charts": 1, "dashboards": 1}
    assert b.load(BRAND).assets("databases")["databases/warehouse.yaml"]["uuid"] == b.WAREHOUSE_UUID


def test_a_source_folder_holds_no_database(tmp_path):
    folder = tmp_path / "callrail"
    shutil.copytree(SOURCES / "callrail", folder)
    shutil.copytree(BRAND / "databases", folder / "databases")
    with pytest.raises(b.BundleError, match="holds no database"):
        b.load(folder, source=True)


def test_a_source_dataset_must_name_the_warehouse_uuid(tmp_path):
    folder = tmp_path / "callrail"
    shutil.copytree(SOURCES / "callrail", folder)
    path = folder / "datasets" / "warehouse" / "callrail_calls_1.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["database_uuid"] = "00000000-0000-0000-0000-000000000000"
    path.write_text(yaml.safe_dump(doc))
    with pytest.raises(b.BundleError, match="is not the warehouse UUID"):
        b.load(folder, source=True)


def test_merge_carries_the_brand_metadata_and_warehouse(tmp_path):
    merged = b.merge(b.load(SOURCES / "callrail", source=True), b.load(BRAND))
    assert merged.counts() == {"databases": 1, "datasets": 1, "charts": 1, "dashboards": 1}
    files = b.rewrite(merged)
    assert files["metadata.yaml"] == (BRAND / "metadata.yaml").read_text()
    assert yaml.safe_load(files["charts/CallRail_calls_per_location_1.yaml"])["tags"] == [b.CERTIFIED]


def test_merge_refuses_a_brand_without_the_warehouse_uuid(tmp_path):
    brand = tmp_path / "bi"
    shutil.copytree(BRAND, brand)
    path = brand / "databases" / "warehouse.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["uuid"] = "11111111-1111-1111-1111-111111111111"
    path.write_text(yaml.safe_dump(doc))
    for ds in (brand / "datasets").rglob("*.yaml"):
        d = yaml.safe_load(ds.read_text())
        d["database_uuid"] = doc["uuid"]
        ds.write_text(yaml.safe_dump(d))
    with pytest.raises(b.BundleError, match="must carry it"):
        b.merge(b.load(SOURCES / "callrail", source=True), b.load(brand))


def test_merge_refuses_a_source_uuid_that_the_brand_uses():
    with pytest.raises(b.BundleError, match="is also in"):
        b.merge(b.load(BRAND, source=False), b.load(BRAND))


def test_the_default_sources_dir_is_beside_the_brand_bi(tmp_path):
    assert cli.default_sources_dir(str(tmp_path / "bi")) == (
        tmp_path / "dbt_packages" / "franops_sources" / "bi"
    )


def test_deploy_needs_franops_or_no_sources(monkeypatch):
    monkeypatch.delenv("FRANOPS_URL", raising=False)
    monkeypatch.delenv("FRANOPS_BI_SOURCES_KEY", raising=False)
    with pytest.raises(SystemExit, match="--no-sources"):
        cli.source_bundles(args(), b.load(BRAND))
    assert cli.source_bundles(args("--no-sources"), b.load(BRAND)) == []


def test_no_source_configured_imports_nothing_before_the_brand(franops):
    url, stub = franops
    a = args("--franops-url", url, "--franops-api-key", KEY, "--sources-dir", "/nonexistent")
    assert cli.source_bundles(a, b.load(BRAND)) == []


def test_each_key_with_a_folder_imports_and_a_key_without_one_is_skipped(franops, capsys):
    url, stub = franops
    stub.keys = ["callrail", "reference_csv"]
    a = args("--franops-url", url, "--franops-api-key", KEY, "--sources-dir", str(SOURCES))
    out = cli.source_bundles(a, b.load(BRAND))
    assert [k for k, _ in out] == ["callrail"]
    assert "reference_csv has no bi/ folder" in capsys.readouterr().out


def test_a_missing_sources_dir_says_run_dbt_deps(franops, tmp_path):
    url, stub = franops
    stub.keys = ["callrail"]
    a = args("--franops-url", url, "--franops-api-key", KEY, "--sources-dir", str(tmp_path / "x"))
    with pytest.raises(SystemExit, match="dbt deps"):
        cli.source_bundles(a, b.load(BRAND))


def test_a_refused_key_fails_the_deploy(franops):
    url, stub = franops
    a = args("--franops-url", url, "--franops-api-key", "wrong", "--sources-dir", str(SOURCES))
    with pytest.raises(SystemExit, match="HTTP 401"):
        cli.source_bundles(a, b.load(BRAND))


def test_a_key_that_is_not_a_slug_is_refused(franops):
    url, stub = franops
    stub.keys = ["../../etc"]
    a = args("--franops-url", url, "--franops-api-key", KEY, "--sources-dir", str(SOURCES))
    with pytest.raises(SystemExit, match="not a slug"):
        cli.source_bundles(a, b.load(BRAND))


def test_validate_source_checks_a_source_folder():
    assert cli.main(["validate", "--dir", str(SOURCES / "callrail"), "--source"]) == 0
    assert cli.main(["validate", "--dir", str(SOURCES / "callrail")]) == 1
