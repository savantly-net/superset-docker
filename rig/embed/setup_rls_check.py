"""Prepare a rig for the RLS check (S5.6), in process. Prints the check's plan.

Usage: rig/rig.sh rls-plan      (runs this file inside rig-superset, after `franops-bi deploy`)
Prints one line 'RIGJSON {...}': each chart tagged `certified`, with its dataset, the
dashboards that hold it, and the embedded UUID of each of those dashboards.

Rig only. Since S5.9 franops-bi writes the roles and the service account too; the embedded
config is still rig only:
- an embedded config on each dashboard that holds a certified chart;
- the role `GuestIssuer`: `can_grant_guest_token`, and `datasource_access` on each dataset of
  a certified chart (spike T-4: the minting account needs both);
- the role `Embed`: `datasource_access` on the same datasets, and `can_read` on Dashboard and
  on Chart (spike T-4: without `can_read` on Chart, chart data is `403`);
- the service account `franops_guest_issuer`, with the role `GuestIssuer` only.
The roles are set to exactly these permissions. `setup_embed.py` sets them for its own
fixture, so run the two on separate rigs.
"""
import json
import os

from superset.app import create_app

SERVICE_USER = os.environ.get("BI_SERVICE_USERNAME", "franops_guest_issuer")
SERVICE_PASSWORD = os.environ.get("RIG_SERVICE_PASSWORD", "svc-rig-pass")
CERTIFIED = "certified"

app = create_app()

with app.app_context():
    from flask_appbuilder.security.sqla.models import PermissionView
    from superset import db, security_manager as sm
    from superset.models.embedded_dashboard import EmbeddedDashboard
    from superset.models.slice import Slice
    from superset.tags.models import ObjectType, Tag, TaggedObject

    chart_ids = [
        row.object_id
        for row in db.session.query(TaggedObject)
        .join(Tag, Tag.id == TaggedObject.tag_id)
        .filter(Tag.name == CERTIFIED, TaggedObject.object_type == ObjectType.chart)
        .all()
    ]
    charts = db.session.query(Slice).filter(Slice.id.in_(chart_ids)).order_by(Slice.id).all()

    datasets = {}
    plan = []
    for slc in charts:
        ds = slc.datasource
        if ds is None:
            raise SystemExit(f"certified chart {slc.id} has no dataset")
        datasets[ds.id] = ds
        dashboards = []
        for dash in slc.dashboards:
            if not dash.embedded:
                db.session.add(EmbeddedDashboard(dashboard_id=dash.id))
                db.session.commit()
            dashboards.append({"id": dash.id, "embeddedUuid": str(dash.embedded[0].uuid)})
        plan.append({
            "chartId": slc.id,
            "chartName": slc.slice_name,
            "datasetId": ds.id,
            "params": json.loads(slc.params or "{}"),
            "queryContext": json.loads(slc.query_context) if slc.query_context else None,
            "dashboards": dashboards,
        })

    def pv(perm, view):
        found = sm.find_permission_view_menu(perm, view)
        if found is None:
            raise SystemExit(f"missing permission {perm} on {view}")
        return found

    access = []
    for ds in datasets.values():
        sm.add_permission_view_menu("datasource_access", ds.get_perm())
        db.session.commit()
        access.append(pv("datasource_access", ds.get_perm()))

    grants = (db.session.query(PermissionView).join(PermissionView.permission)
              .filter_by(name="can_grant_guest_token").all())
    if not grants:
        raise SystemExit("missing permission can_grant_guest_token")
    issuer = sm.find_role("GuestIssuer") or sm.add_role("GuestIssuer")
    issuer.permissions = [*grants, *access]
    embed = sm.find_role("Embed") or sm.add_role("Embed")
    embed.permissions = [*access, pv("can_read", "Dashboard"), pv("can_read", "Chart")]
    db.session.commit()

    user = sm.find_user(username=SERVICE_USER)
    if not user:
        sm.add_user(username=SERVICE_USER, first_name="franops", last_name="issuer",
                    email=f"{SERVICE_USER}@rig.invalid", role=[issuer], password=SERVICE_PASSWORD)
    else:
        user.roles = [issuer]
    db.session.commit()

    print("RIGJSON " + json.dumps({"serviceUsername": SERVICE_USER, "charts": plan}))
