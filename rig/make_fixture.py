"""Make tests/fixtures/bi/ from a live rig. Run it once against an empty rig; commit the result.

    rig/rig.sh up
    python rig/make_fixture.py tests/fixtures/bi

It creates the assets through the REST API, as an analyst would in the UI: the warehouse
database, a dataset on marts.calls_daily, a table chart, and a dashboard that holds the chart.
Then it exports them with GET /api/v1/assets/export/ and explodes the ZIP into the folder,
without the export's root directory. The export masks the database password.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import sys
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools/franops_bi/src"))

from franops_bi.client import Superset  # noqa: E402

URL = os.environ.get("SUPERSET_URL", "http://127.0.0.1:18388")
WAREHOUSE_URI = "postgresql+psycopg2://warehouse:warehouse-rig-pass@rig-warehouse:5432/warehouse"


def main(out_dir: str) -> None:
    out = pathlib.Path(out_dir)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty")
    s = Superset(URL, "admin", os.environ.get("RIG_ADMIN_PASSWORD", "admin-rig-pass"))

    _, db = s.request("POST", "/api/v1/database/", {
        "database_name": "warehouse",
        "sqlalchemy_uri": WAREHOUSE_URI,
        "expose_in_sqllab": False,
    }, expect=201)
    _, ds = s.request("POST", "/api/v1/dataset/", {
        "database": db["id"], "schema": "marts", "table_name": "calls_daily",
    }, expect=201)
    s.request("PUT", f"/api/v1/dataset/{ds['id']}", {
        "description": "Calls per location per day (certified fixture).",
    })
    _, dash = s.request("POST", "/api/v1/dashboard/", {
        "dashboard_title": "Calls by location", "published": True,
    }, expect=201)
    params = {
        "datasource": f"{ds['id']}__table", "viz_type": "table", "query_mode": "aggregate",
        "groupby": ["location_key"], "metrics": [{
            "expressionType": "SIMPLE", "aggregate": "SUM", "label": "SUM(calls)",
            "column": {"column_name": "calls"},
        }], "row_limit": 1000,
    }
    _, chart = s.request("POST", "/api/v1/chart/", {
        "slice_name": "Calls per location", "viz_type": "table",
        "datasource_id": ds["id"], "datasource_type": "table",
        "params": json.dumps(params), "dashboards": [dash["id"]],
    }, expect=201)
    chart_uuid = s.get(f"/api/v1/chart/{chart['id']}")["result"].get("uuid")
    position = {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
        "GRID_ID": {"type": "GRID", "id": "GRID_ID", "children": ["ROW-1"],
                    "parents": ["ROOT_ID"]},
        "ROW-1": {"type": "ROW", "id": "ROW-1", "children": ["CHART-1"],
                  "parents": ["ROOT_ID", "GRID_ID"],
                  "meta": {"background": "BACKGROUND_TRANSPARENT"}},
        "CHART-1": {"type": "CHART", "id": "CHART-1", "children": [],
                    "parents": ["ROOT_ID", "GRID_ID", "ROW-1"],
                    "meta": {"chartId": chart["id"], "uuid": chart_uuid, "width": 6,
                             "height": 50, "sliceName": "Calls per location"}},
    }
    s.request("PUT", f"/api/v1/dashboard/{dash['id']}", {
        "position_json": json.dumps(position),
    })

    blob = s.export_assets()
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for name in z.namelist():
            rel = name.split("/", 1)[1]  # drop the timestamped root folder
            target = out / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(name))
            print(rel)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/bi")
