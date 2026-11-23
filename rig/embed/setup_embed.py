"""Seed the embed fixture of the fran-ops guest-token rig tests (S6.2), in process.

Usage: rig/rig.sh embed      (runs this file inside rig-superset)
Prints one line 'RIGJSON {...}' with what the fran-ops rig test needs.

It creates, from spike T-4 (`spike/t4-guest-token-rls`, `spikes/t4/scripts/setup.py`):
- the database `rig_embed_warehouse` and the dataset `embed.scorecard_fixture`;
- a table chart with a stored query_context, on a published dashboard, with an embedded config;
- the roles of `design/bi-image.md`: `GuestIssuer` (`can_grant_guest_token` and `datasource_access`
  on the dataset) and `Embed` (`datasource_access`, `can_read` on Dashboard and on Chart);
- the service account `franops_guest_issuer`, with the role `GuestIssuer` only.

The S5.5 rig tests need an empty instance, so run this on a rig of its own, after them.
"""
import json
import os

from superset.app import create_app

SERVICE_USER = "franops_guest_issuer"
SERVICE_PASSWORD = os.environ.get("RIG_SERVICE_PASSWORD", "svc-rig-pass")
WAREHOUSE_URI = "postgresql://warehouse:warehouse-rig-pass@rig-warehouse:5432/warehouse"
COLUMNS = ["franchisee_label", "location_key", "calls"]

app = create_app()

with app.app_context():
    from flask_appbuilder.security.sqla.models import PermissionView
    from superset import db, security_manager as sm
    from superset.connectors.sqla.models import SqlaTable
    from superset.models.core import Database
    from superset.models.dashboard import Dashboard
    from superset.models.embedded_dashboard import EmbeddedDashboard
    from superset.models.slice import Slice

    database = db.session.query(Database).filter_by(database_name="rig_embed_warehouse").one_or_none()
    if not database:
        database = Database(database_name="rig_embed_warehouse", sqlalchemy_uri=WAREHOUSE_URI)
        db.session.add(database)
        db.session.flush()
    ds = (
        db.session.query(SqlaTable)
        .filter_by(table_name="scorecard_fixture", schema="embed", database_id=database.id)
        .one_or_none()
    )
    if not ds:
        ds = SqlaTable(table_name="scorecard_fixture", schema="embed", database=database)
        db.session.add(ds)
        db.session.flush()
        ds.fetch_metadata()
    db.session.commit()
    sm.add_permission_view_menu("datasource_access", ds.get_perm())
    db.session.commit()

    slc = db.session.query(Slice).filter_by(slice_name="rig embed scorecard").one_or_none()
    if not slc:
        slc = Slice(slice_name="rig embed scorecard", viz_type="table", datasource_type="table",
                    datasource_id=ds.id)
        db.session.add(slc)
        db.session.flush()
    form_data = {"slice_id": slc.id, "datasource": f"{ds.id}__table", "viz_type": "table",
                 "query_mode": "raw", "all_columns": COLUMNS, "row_limit": 1000}
    query_context = {
        "datasource": {"id": ds.id, "type": "table"}, "force": True,
        "queries": [{"columns": COLUMNS, "metrics": [], "orderby": [], "row_limit": 1000,
                     "filters": [], "extras": {"having": "", "where": ""}}],
        "form_data": form_data, "result_format": "json", "result_type": "full",
    }
    slc.params = json.dumps(form_data)
    slc.query_context = json.dumps(query_context)
    db.session.commit()

    dash = db.session.query(Dashboard).filter_by(slug="rig-embed").one_or_none()
    if not dash:
        dash = Dashboard(dashboard_title="rig embed", slug="rig-embed", published=True)
        db.session.add(dash)
        db.session.flush()
    dash.slices = [slc]
    dash.position_json = json.dumps({
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
        "GRID_ID": {"type": "GRID", "id": "GRID_ID", "children": ["ROW-1"], "parents": ["ROOT_ID"]},
        "ROW-1": {"type": "ROW", "id": "ROW-1", "children": ["CHART-1"], "parents": ["ROOT_ID", "GRID_ID"],
                  "meta": {"background": "BACKGROUND_TRANSPARENT"}},
        "CHART-1": {"type": "CHART", "id": "CHART-1", "children": [],
                    "parents": ["ROOT_ID", "GRID_ID", "ROW-1"],
                    "meta": {"chartId": slc.id, "width": 4, "height": 50}},
    })
    db.session.commit()
    if not dash.embedded:
        db.session.add(EmbeddedDashboard(dashboard_id=dash.id))
        db.session.commit()
    embedded = dash.embedded[0]

    def pv(perm, view):
        found = sm.find_permission_view_menu(perm, view)
        if found is None:
            raise SystemExit(f"missing permission {perm} on {view}")
        return found

    grants = (db.session.query(PermissionView).join(PermissionView.permission)
              .filter_by(name="can_grant_guest_token").all())
    if not grants:
        raise SystemExit("missing permission can_grant_guest_token")
    issuer = sm.find_role("GuestIssuer") or sm.add_role("GuestIssuer")
    issuer.permissions = [*grants, pv("datasource_access", ds.get_perm())]
    embed = sm.find_role("Embed") or sm.add_role("Embed")
    embed.permissions = [pv("datasource_access", ds.get_perm()), pv("can_read", "Dashboard"),
                         pv("can_read", "Chart")]
    db.session.commit()

    user = sm.find_user(username=SERVICE_USER)
    if not user:
        sm.add_user(username=SERVICE_USER, first_name="franops", last_name="issuer",
                    email="franops_guest_issuer@rig.invalid", role=[issuer], password=SERVICE_PASSWORD)
    else:
        user.roles = [issuer]
    db.session.commit()

    print("RIGJSON " + json.dumps({
        "serviceUsername": SERVICE_USER,
        "servicePassword": SERVICE_PASSWORD,
        "dashboardId": dash.id,
        "embeddedUuid": str(embedded.uuid),
        "queryContext": query_context,
    }))
