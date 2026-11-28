"""S12.4 AC1 (fran-ops): one deploy creates the full certified set of the configured sources on
an empty BI. rig/scorecard-check.sh runs it on an empty rig.

    python3 rig/scorecard_deploy_check.py <sources_bi_dir>

A stub on 127.0.0.1 answers `GET /api/data/bi/sources` as FranOps does, with the source key
`callrail`. One `franops-bi deploy` then runs with the brand fixture and the real `bi/` folder of
`franops_sources` (franops-data-dbt `sources/bi/`). The check reads the metadata database:
before the deploy it holds no asset; after it, it holds each asset of the brand fixture and of
`bi/callrail/`, each source asset carries the `certified` tag and is managed externally, and the
one database is the warehouse with WAREHOUSE_UUID. Exit 0 on a pass, 1 on a failure.
"""

from __future__ import annotations

import http.server
import json
import os
import pathlib
import subprocess
import sys
import threading

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools/franops_bi/src"))

from franops_bi import bundle as b  # noqa: E402
from franops_bi.cli import main as franops_bi  # noqa: E402

BRAND = REPO / "tests" / "fixtures" / "bi"
COMPOSE = REPO / "rig" / "compose.yml"
URL = os.environ.get("SUPERSET_URL", "http://127.0.0.1:18388")
ADMIN_PASSWORD = os.environ.get("RIG_ADMIN_PASSWORD", "admin-rig-pass")
KEY = "fran_rig_scorecard_key"
SOURCE_KEYS = ["callrail"]
TABLES = {"databases": "dbs", "datasets": "tables", "charts": "slices", "dashboards": "dashboards"}
TAG_TYPE = {"datasets": "dataset", "charts": "chart", "dashboards": "dashboard"}


class _FranOps(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path != "/api/data/bi/sources" or self.headers.get("Authorization") != f"Bearer {KEY}":
            self.send_response(401)
            self.end_headers()
            return
        body = json.dumps({"organizationId": "org_rig", "sourceKeys": SOURCE_KEYS}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


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
        f"AND o.object_type = '{TAG_TYPE[asset_type]}' JOIN tag t ON t.id = o.tag_id "
        f"WHERE a.uuid = '{uuid}' AND t.name = '{b.CERTIFIED}' AND a.is_managed_externally"
    ))


def expected(folder: pathlib.Path, source: bool) -> dict[str, set[str]]:
    bundle = b.load(folder, source=source)
    return {t: {str(d["uuid"]) for d in bundle.assets(t).values()} for t in TABLES}


def run(sources_dir: pathlib.Path) -> list[str]:
    problems: list[str] = []
    for t in TABLES:
        if uuids(t):
            return [f"the BI is not empty before the deploy: {t}"]
    server = http.server.HTTPServer(("127.0.0.1", 0), _FranOps)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        os.environ["FRANOPS_BI_DB_PASSWORDS"] = json.dumps(
            {"databases/warehouse.yaml": "warehouse-rig-pass"})
        status = franops_bi([
            "deploy", "--dir", str(BRAND), "--url", URL, "--username", "admin",
            "--password", ADMIN_PASSWORD, "--franops-url", f"http://127.0.0.1:{server.server_port}",
            "--franops-api-key", KEY, "--sources-dir", str(sources_dir)])
    finally:
        server.shutdown()
    if status != 0:
        return [f"franops-bi deploy exited {status}"]
    brand = expected(BRAND, False)
    want = {t: set(brand[t]) for t in TABLES}
    sources: dict[str, set[str]] = {t: set() for t in TABLES}
    for key in SOURCE_KEYS:
        for t, ids in expected(sources_dir / key, True).items():
            want[t] |= ids
            sources[t] |= ids
    for t in TABLES:
        got = uuids(t)
        if got != want[t]:
            problems.append(f"{t}: missing {sorted(want[t] - got)}, extra {sorted(got - want[t])}")
        for uuid in sources[t] if t in TAG_TYPE else ():
            if not certified(t, uuid):
                problems.append(f"{t} {uuid} is not certified and managed externally")
        print(f"ok   {t}: {len(got)} in the BI, {len(sources[t])} from the sources")
    if uuids("databases") != {b.WAREHOUSE_UUID}:
        problems.append(f"databases: {sorted(uuids('databases'))}, not the one warehouse")
    if not sources["dashboards"]:
        problems.append("the sources hold no dashboard; the check judged nothing")
    return problems


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: scorecard_deploy_check.py <sources_bi_dir>")
    found = run(pathlib.Path(sys.argv[1]).resolve())
    for p in found:
        print(f"FAIL: {p}")
    if not found:
        print("PASS: one deploy created the full certified set of the configured sources")
    sys.exit(1 if found else 0)
