"""Rig tests for S5.5: deploy tests/fixtures/bi into an empty BI instance, twice.

    rig/rig.sh up
    FRANOPS_BI_RIG=1 python -m pytest -m rig -s tools/franops_bi/tests/test_rig.py
    rig/rig.sh down

The tests run in file order on one rig, and they need an empty instance (`rig.sh up` gives
one). Each reads the metadata database through `docker compose exec rig-meta psql`, so the
numbers come from the rows, not from the API's rendered fields.

"Changes nothing" (AC2) is measured as: the same id and uuid for each database, dataset,
chart, and dashboard; the same count of each; the same tag rows; and the same export
content (GET /api/v1/assets/export/, without the timestamped root folder and the
metadata.yaml timestamp). The test also asserts that `changed_on` of each row stays the
same, and that the SQLAlchemy-Continuum history tables (*_version) get no new row. A last
test changes one YAML and shows that the same deploy does update an asset, so the
second deploy is an overwrite that finds nothing to change, not a skipped import.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import subprocess
import zipfile

import pytest
import yaml

from franops_bi import bundle as b
from franops_bi.cli import main
from franops_bi.client import Superset

pytestmark = [
    pytest.mark.rig,
    pytest.mark.skipif(os.environ.get("FRANOPS_BI_RIG") != "1", reason="set FRANOPS_BI_RIG=1"),
]

REPO = pathlib.Path(__file__).resolve().parents[3]
FIXTURE = REPO / "tests" / "fixtures" / "bi"
COMPOSE = REPO / "rig" / "compose.yml"
URL = os.environ.get("SUPERSET_URL", "http://127.0.0.1:18388")
ADMIN_PASSWORD = os.environ.get("RIG_ADMIN_PASSWORD", "admin-rig-pass")
DB_PASSWORDS = {"databases/warehouse.yaml": "warehouse-rig-pass"}
# The tables of marts in rig/warehouse/seed.sql: the fixture table and the real CallRail mart (S12.4).
MARTS_TABLES = ["calls_daily", "fct_calls_location_day"]

TABLES = {"databases": "dbs", "datasets": "tables", "charts": "slices", "dashboards": "dashboards"}
TAG_OBJECT_TYPE = {"datasets": "dataset", "charts": "chart", "dashboards": "dashboard"}

STATE: dict[str, object] = {}


def sql(query: str) -> list[list[str]]:
    out = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "exec", "-T", "rig-meta",
         "psql", "-U", "superset", "-d", "superset", "-At", "-F", "\t", "-c", query],
        capture_output=True, text=True, check=True,
    ).stdout
    return [line.split("\t") for line in out.splitlines() if line]


def rows(asset_type: str) -> list[list[str]]:
    return sql(
        f"SELECT id, uuid, changed_on, is_managed_externally FROM {TABLES[asset_type]} ORDER BY id"
    )


def tag_rows() -> list[list[str]]:
    return sql(
        "SELECT o.id, o.object_type, o.object_id, t.name FROM tagged_object o "
        "JOIN tag t ON t.id = o.tag_id WHERE t.type = 'custom' ORDER BY o.id"
    )


def tags_of(asset_type: str, object_id: str) -> list[str]:
    return [
        r[0] for r in sql(
            "SELECT t.name FROM tagged_object o JOIN tag t ON t.id = o.tag_id "
            f"WHERE t.type = 'custom' AND o.object_type = '{TAG_OBJECT_TYPE[asset_type]}' "
            f"AND o.object_id = {int(object_id)} ORDER BY t.name"
        )
    ]


def version_counts() -> dict[str, int]:
    names = [r[0] for r in sql(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name LIKE '%\\_version' "
        "AND table_name <> 'alembic_version' ORDER BY 1"
    )]
    return {n: int(sql(f"SELECT count(*) FROM {n}")[0][0]) for n in names}


def snapshot() -> dict[str, object]:
    return {
        "assets": {t: rows(t) for t in TABLES},
        "tags": tag_rows(),
        "versions": version_counts(),
    }


def export_content(client: Superset) -> dict[str, object]:
    out: dict[str, object] = {}
    with zipfile.ZipFile(io.BytesIO(client.export_assets())) as z:
        for name in z.namelist():
            rel = name.split("/", 1)[1]
            doc = yaml.safe_load(z.read(name))
            if rel == "metadata.yaml":
                doc.pop("timestamp", None)
            out[rel] = doc
    return out


def warehouse_tables(client: Superset) -> list[str]:
    """Tables of `marts`, read live from the warehouse: needs the stored database password."""
    db_id = int(rows("databases")[0][0])
    body = client.get(f"/api/v1/database/{db_id}/tables/", schema_name="marts", force=True)
    return sorted(t["value"] for t in body["result"])


def deploy(*extra: str) -> int:
    os.environ["FRANOPS_BI_DB_PASSWORDS"] = json.dumps(DB_PASSWORDS)
    return main(["deploy", "--dir", str(FIXTURE), "--no-sources", "--url", URL,
                 "--username", "admin", "--password", ADMIN_PASSWORD, *extra])


@pytest.fixture(scope="module")
def client() -> Superset:
    return Superset(URL, "admin", ADMIN_PASSWORD)


@pytest.fixture(scope="module")
def folder_counts() -> dict[str, int]:
    return b.load(FIXTURE).counts()


def test_ac1_deploy_into_an_empty_instance_creates_each_asset(
    client: Superset, folder_counts: dict[str, int]
) -> None:
    before = {t: len(rows(t)) for t in TABLES}
    print(f"\nAC1 before deploy: {before}")
    assert before == {t: 0 for t in TABLES}, "the rig is not empty: run rig/rig.sh up"

    assert deploy() == 0

    after = {t: len(rows(t)) for t in TABLES}
    api = {t: client.get(f"/api/v1/{r}/", page_size=100)["count"]
           for t, r in (("databases", "database"), ("datasets", "dataset"),
                        ("charts", "chart"), ("dashboards", "dashboard"))}
    print(f"AC1 folder: {folder_counts}")
    print(f"AC1 created (metadata DB): {after}")
    print(f"AC1 created (REST count): {api}")
    assert after == folder_counts
    assert api == folder_counts
    # Each uuid of the folder is in the instance.
    for asset_type in TABLES:
        wanted = {str(d["uuid"]) for d in b.load(FIXTURE).assets(asset_type).values()}
        assert {r[1] for r in rows(asset_type)} == wanted
    # The chart sits on the dashboard: the import rewrote the exported chartId to the new id.
    links = sql("SELECT dashboard_id, slice_id FROM dashboard_slices ORDER BY 1, 2")
    print(f"AC1 dashboard_slices: {links}")
    assert links == [[rows("dashboards")[0][0], rows("charts")[0][0]]]
    # The database connects with the password from FRANOPS_BI_DB_PASSWORDS.
    tables = warehouse_tables(client)
    print(f"AC1 warehouse tables in marts (live): {tables}")
    assert tables == MARTS_TABLES
    STATE["after_first"] = snapshot()
    STATE["export_first"] = export_content(client)


def test_ac2_second_deploy_changes_nothing(client: Superset) -> None:
    first = STATE["after_first"]
    assert deploy() == 0
    second = snapshot()
    export_second = export_content(client)

    def ids(snap: dict) -> dict[str, list[tuple[str, str]]]:
        return {t: [(r[0], r[1]) for r in rs] for t, rs in snap["assets"].items()}

    def changed_on(snap: dict) -> dict[str, list[str]]:
        return {t: [r[2] for r in rs] for t, rs in snap["assets"].items()}

    version_delta = {
        n: second["versions"][n] - first["versions"].get(n, 0) for n in second["versions"]
    }
    report = {
        "ids_uuids_equal": ids(first) == ids(second),
        "tag_rows_equal": first["tags"] == second["tags"],
        "export_equal": STATE["export_first"] == export_second,
        "changed_on_first": changed_on(first),
        "changed_on_second": changed_on(second),
        "changed_on_equal": changed_on(first) == changed_on(second),
        "version_rows_after_first": first["versions"],
        "version_rows_added": {n: d for n, d in version_delta.items() if d},
    }
    print("\nAC2 " + json.dumps(report, indent=2))
    assert report["ids_uuids_equal"]
    assert report["tag_rows_equal"]
    assert report["export_equal"]
    assert report["changed_on_equal"]
    assert first["versions"], "no *_version table found: the history check measures nothing"
    assert report["version_rows_added"] == {}
    # The overwrite import kept the stored password: the warehouse still answers.
    assert warehouse_tables(client) == MARTS_TABLES


def test_ac3_each_chart_and_dashboard_has_the_certified_tag() -> None:
    for asset_type in ("charts", "dashboards"):
        for r in rows(asset_type):
            tags = tags_of(asset_type, r[0])
            print(f"AC3 {asset_type} id={r[0]} tags={tags} managed={r[3]}")
            assert tags == ["certified"]
            assert r[3] == "t"


def test_ac3_each_database_and_dataset_is_managed_externally() -> None:
    for asset_type in ("databases", "datasets"):
        for r in rows(asset_type):
            print(f"AC3 {asset_type} id={r[0]} managed={r[3]}")
            assert r[3] == "t"


def test_ac3_each_dataset_has_the_certified_tag_after_a_plain_deploy() -> None:
    # The import cannot tag a dataset; deploy tags it through the tag API (the default).
    for r in rows("datasets"):
        print(f"AC3 datasets id={r[0]} tags={tags_of('datasets', r[0])}")
        assert tags_of("datasets", r[0]) == ["certified"]


def test_without_tag_datasets_the_read_back_fails_until_the_tag_exists() -> None:
    """`--no-tag-datasets` skips the tag API; the tag of the earlier deploy stays (a deploy never
    removes a dataset tag), so the read-back still passes on this instance."""
    assert deploy("--no-tag-datasets") == 0
    for r in rows("datasets"):
        assert tags_of("datasets", r[0]) == ["certified"]


def test_control_a_changed_yaml_does_update_the_asset(tmp_path: pathlib.Path) -> None:
    """The control for AC2: the same deploy writes a change when the YAML holds one."""
    import shutil

    copy = tmp_path / "bi"
    shutil.copytree(FIXTURE, copy)
    chart = next((copy / "charts").glob("*.yaml"))
    doc = yaml.safe_load(chart.read_text())
    doc["slice_name"] = doc["slice_name"] + " (renamed)"
    chart.write_text(yaml.safe_dump(doc, sort_keys=False))
    before = rows("charts")
    os.environ["FRANOPS_BI_DB_PASSWORDS"] = json.dumps(DB_PASSWORDS)
    assert main(["deploy", "--dir", str(copy), "--no-sources", "--url", URL,
                 "--username", "admin", "--password", ADMIN_PASSWORD]) == 0
    after = rows("charts")
    name = sql("SELECT slice_name FROM slices")[0][0]
    print(f"\ncontrol: slice_name={name!r} changed_on {before[0][2]} -> {after[0][2]}")
    assert name.endswith("(renamed)")
    assert [(r[0], r[1]) for r in after] == [(r[0], r[1]) for r in before]
    assert after[0][2] != before[0][2]
