"""Make the certified CallRail scorecard of franops_sources (fran-ops S12.4) from a live rig.

    rig/rig.sh up
    FRANOPS_BI_DB_PASSWORDS='{"databases/warehouse.yaml":"warehouse-rig-pass"}' \\
      python3 -m franops_bi deploy --dir tests/fixtures/bi --no-sources \\
      --url http://127.0.0.1:18388 --username admin --password admin-rig-pass
    python3 rig/make_source_assets.py ../franops-data-dbt/sources/bi/callrail

The deploy first creates the brand's warehouse database with the fixed WAREHOUSE_UUID, so the
exported dataset names that UUID. Then this script creates the assets through the REST API, as an
analyst would in the UI: a dataset on marts.fct_calls_location_day with its metrics, two charts,
and a published dashboard. It exports the dashboard and writes charts/, dashboards/ and datasets/
into the folder. It drops databases/ and metadata.yaml: a source folder holds no database
(design/bi-image.md), and the deploy takes both from the brand's bi/.

Each chart groups by location_key. The guest token clause filters on location_key, and the RLS
check (scripts/rls_check.py) reads whose row it is from that column. The mart stores counts only
(FR-4.6), so each ratio is a metric over sums, and baseline_calls is never summed over days.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import sys
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools/franops_bi/src"))

from franops_bi import bundle as b  # noqa: E402
from franops_bi.client import Superset  # noqa: E402

URL = os.environ.get("SUPERSET_URL", "http://127.0.0.1:18388")
KEEP = ("charts/", "dashboards/", "datasets/")

METRICS = [
    ("calls", "Calls", "SUM(calls)", ",d", "All calls that are not excluded."),
    ("answered", "Answered", "SUM(answered)", ",d", "Inbound calls that a person answered."),
    ("answer_rate", "Answer rate", "SUM(answered) * 1.0 / NULLIF(SUM(answerable), 0)", ".0%",
     "Answered calls over inbound calls, computed from the sums at each grain."),
    ("first_time_calls", "First-time calls", "SUM(first_time_calls)", ",d",
     "Calls from a number that did not call before."),
    ("missed", "Missed", "SUM(missed)", ",d",
     "Inbound calls that no person answered, voicemail included."),
    ("baseline_calls", "Baseline calls", "MAX(baseline_calls)", ",d",
     "The location's number from the reference CSV. It repeats on each day, so it is never "
     "summed; read it by location."),
]


def _warehouse_id(s: Superset) -> int:
    for db in s.list_all("database", ["id", "uuid"]):
        if str(db.get("uuid")) == b.WAREHOUSE_UUID:
            return int(db["id"])
    raise SystemExit(f"no database with uuid {b.WAREHOUSE_UUID}: deploy the brand fixture first")


def _chart(s: Superset, name: str, viz: str, ds_id: int, dash_id: int, params: dict) -> dict:
    params = {"datasource": f"{ds_id}__table", "viz_type": viz, **params}
    _, chart = s.request("POST", "/api/v1/chart/", {
        "slice_name": name, "viz_type": viz, "datasource_id": ds_id, "datasource_type": "table",
        "params": json.dumps(params), "dashboards": [dash_id],
    }, expect=201)
    uuid = s.get(f"/api/v1/chart/{chart['id']}")["result"].get("uuid")
    return {"id": chart["id"], "uuid": uuid, "name": name}


def main(out_dir: str) -> None:
    out = pathlib.Path(out_dir)
    if out.exists() and any(p.name != ".gitkeep" for p in out.iterdir()):
        raise SystemExit(f"{out} is not empty")
    s = Superset(URL, "admin", os.environ.get("RIG_ADMIN_PASSWORD", "admin-rig-pass"))

    _, ds = s.request("POST", "/api/v1/dataset/", {
        "database": _warehouse_id(s), "schema": "marts", "table_name": "fct_calls_location_day",
    }, expect=201)
    s.request("PUT", f"/api/v1/dataset/{ds['id']}", {
        "description": "CallRail calls of each location and day (franops_sources). Counts "
                       "only: each ratio is a metric over sums.",
        "main_dttm_col": "date",
        "metrics": [
            {"metric_name": n, "verbose_name": v, "expression": e, "d3format": f,
             "description": d}
            for n, v, e, f, d in METRICS
        ],
    })
    _, dash = s.request("POST", "/api/v1/dashboard/", {
        "dashboard_title": "Calls scorecard", "published": True,
    }, expect=201)
    table = _chart(s, "Calls by location", "table", ds["id"], dash["id"], {
        "query_mode": "aggregate", "groupby": ["location_key"],
        "metrics": [m[0] for m in METRICS], "row_limit": 1000,
        "order_desc": True, "timeseries_limit_metric": "calls",
    })
    line = _chart(s, "Calls per day by location", "echarts_timeseries_line", ds["id"],
                  dash["id"], {
        "x_axis": "date", "time_grain_sqla": "P1D", "groupby": ["location_key"],
        "metrics": ["calls"], "row_limit": 10000, "truncate_metric": True,
    })

    position: dict = {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
        "GRID_ID": {"type": "GRID", "id": "GRID_ID", "children": [], "parents": ["ROOT_ID"]},
    }
    for i, chart in enumerate((table, line), start=1):
        row, cell = f"ROW-{i}", f"CHART-{i}"
        position["GRID_ID"]["children"].append(row)
        position[row] = {"type": "ROW", "id": row, "children": [cell],
                         "parents": ["ROOT_ID", "GRID_ID"],
                         "meta": {"background": "BACKGROUND_TRANSPARENT"}}
        position[cell] = {"type": "CHART", "id": cell, "children": [],
                          "parents": ["ROOT_ID", "GRID_ID", row],
                          "meta": {"chartId": chart["id"], "uuid": chart["uuid"], "width": 12,
                                   "height": 50, "sliceName": chart["name"]}}
    s.request("PUT", f"/api/v1/dashboard/{dash['id']}", {"position_json": json.dumps(position)})

    blob = s.export("dashboard", [dash["id"]])
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for name in z.namelist():
            rel = name.split("/", 1)[1]  # drop the timestamped root folder
            if not rel.startswith(KEEP):
                continue
            target = out / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(name))
            print(rel)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: make_source_assets.py <out_dir>")
    main(sys.argv[1])
