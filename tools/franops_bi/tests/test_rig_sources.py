"""Rig tests for S4.11: the deploy imports `bi/<sourceKey>/` of `franops_sources` for each source
key that FranOps returns, then the brand `bi/`.

    rig/rig.sh up
    FRANOPS_BI_RIG=1 python -m pytest -m rig -s tools/franops_bi/tests/test_rig_sources.py
    rig/rig.sh down

A stub on 127.0.0.1 answers `GET /api/data/bi/sources` as FranOps does (fran-ops
`tests/integration/data-embed/bi-sources.test.ts` tests the real route). The tests run in file
order on one empty rig: first with no source configured (AC2), then with `callrail` (AC1).
Each reads the metadata database through `docker compose exec rig-meta psql`.
"""

from __future__ import annotations

import http.server
import json
import os
import pathlib
import subprocess
import threading

import pytest

from franops_bi import bundle as b
from franops_bi.cli import main

pytestmark = [
    pytest.mark.rig,
    pytest.mark.skipif(os.environ.get("FRANOPS_BI_RIG") != "1", reason="set FRANOPS_BI_RIG=1"),
]

REPO = pathlib.Path(__file__).resolve().parents[3]
BRAND = REPO / "tests" / "fixtures" / "bi"
SOURCES = REPO / "tests" / "fixtures" / "sources" / "bi"
COMPOSE = REPO / "rig" / "compose.yml"
URL = os.environ.get("SUPERSET_URL", "http://127.0.0.1:18388")
ADMIN_PASSWORD = os.environ.get("RIG_ADMIN_PASSWORD", "admin-rig-pass")
DB_PASSWORDS = {"databases/warehouse.yaml": "warehouse-rig-pass"}
KEY = "fran_rig_sources_key"

TABLES = {"databases": "dbs", "datasets": "tables", "charts": "slices", "dashboards": "dashboards"}
TAG_OBJECT_TYPE = {"datasets": "dataset", "charts": "chart", "dashboards": "dashboard"}


class _FranOps(http.server.BaseHTTPRequestHandler):
    keys: list[str] = []

    def do_GET(self):  # noqa: N802
        if self.path != "/api/data/bi/sources" or self.headers.get("Authorization") != f"Bearer {KEY}":
            self.send_response(401)
            self.end_headers()
            return
        body = json.dumps({"organizationId": "org_rig", "sourceKeys": self.keys}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def franops():
    server = http.server.HTTPServer(("127.0.0.1", 0), _FranOps)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def sql(query: str) -> list[list[str]]:
    out = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "exec", "-T", "rig-meta",
         "psql", "-U", "superset", "-d", "superset", "-At", "-F", "\t", "-c", query],
        capture_output=True, text=True, check=True,
    ).stdout
    return [line.split("\t") for line in out.splitlines() if line]


def uuids(asset_type: str) -> set[str]:
    return {r[0] for r in sql(f"SELECT uuid FROM {TABLES[asset_type]}")}


def certified(asset_type: str, uuid: str) -> bool:
    return bool(sql(
        f"SELECT 1 FROM {TABLES[asset_type]} a JOIN tagged_object o ON o.object_id = a.id "
        f"AND o.object_type = '{TAG_OBJECT_TYPE[asset_type]}' JOIN tag t ON t.id = o.tag_id "
        f"WHERE a.uuid = '{uuid}' AND t.name = '{b.CERTIFIED}' AND a.is_managed_externally"
    ))


def expected(folder: pathlib.Path, source: bool) -> dict[str, set[str]]:
    bundle = b.load(folder, source=source)
    return {t: {str(d["uuid"]) for d in bundle.assets(t).values()} for t in TAG_OBJECT_TYPE}


def deploy(franops_url: str) -> int:
    os.environ["FRANOPS_BI_DB_PASSWORDS"] = json.dumps(DB_PASSWORDS)
    return main(["deploy", "--dir", str(BRAND), "--url", URL, "--username", "admin",
                 "--password", ADMIN_PASSWORD, "--franops-url", franops_url,
                 "--franops-api-key", KEY, "--sources-dir", str(SOURCES)])


def test_ac2_no_source_configured_imports_the_brand_bi_only(franops):
    for asset_type in TAG_OBJECT_TYPE:
        assert uuids(asset_type) == set(), f"the rig is not empty: {asset_type}"
    _FranOps.keys = []
    assert deploy(franops) == 0
    brand, source = expected(BRAND, False), expected(SOURCES / "callrail", True)
    for asset_type in TAG_OBJECT_TYPE:
        assert uuids(asset_type) == brand[asset_type], asset_type
        assert not uuids(asset_type) & source[asset_type], asset_type


def test_ac1_callrail_configured_creates_the_callrail_certified_set(franops):
    _FranOps.keys = ["callrail"]
    assert deploy(franops) == 0
    brand, source = expected(BRAND, False), expected(SOURCES / "callrail", True)
    for asset_type in TAG_OBJECT_TYPE:
        assert uuids(asset_type) == brand[asset_type] | source[asset_type], asset_type
        for uuid in source[asset_type]:
            assert certified(asset_type, uuid), f"{asset_type} {uuid} is not certified"
    # One warehouse database: the source datasets reuse the brand's (WAREHOUSE_UUID).
    assert uuids("databases") == {b.WAREHOUSE_UUID}
    table = sql("SELECT schema, table_name FROM tables WHERE uuid = "
                f"'{next(iter(source['datasets']))}'")
    assert table == [["source_fixture", "callrail_calls"]]
