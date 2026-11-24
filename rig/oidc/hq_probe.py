"""The S7.1 and S7.3 checks: what HQ reaches over MCP, and the draft tag (rig only).

Runs inside rig-superset (rig/hq-access-check.sh), after `franops-bi deploy` imported the
certified fixture (tests/fixtures/bi) and the script started `superset mcp run` on MCP_URL. Prints
one PASS or FAIL line for each check, and exits 0 when each check passes.

The rig gives the HQ role the grants of design/bi-image.md. Nothing in the image makes the role
yet (pointer item 49). The rig's tokens carry `superset:write`, which a write tool needs; whether
the MVP provider issues that scope is pointer item 56.

| Case | Expected |
|---|---|
| the deployed fixture | each certified chart, dashboard and dataset has `certified` and no `draft` |
| HQ: generate_chart, saved, on a certified dataset (S7.1 AC1) | a chart on that dataset, made by oidc:<sub> |
| that chart (S7.3 AC1) | the tag `draft` at once, and not `certified` |
| HQ: generate_dashboard with that chart | a dashboard with the tag `draft` |
| HQ: create_dataset, create_virtual_dataset on the raw table (S7.1 AC2) | each fails as a tool that is not there |
| HQ: generate_chart on a dataset of the raw table, by ID and by UUID (S7.1 AC2) | fails, and no chart on that dataset |
| control: the same call after a grant on the raw dataset | a chart: the refusal above came from the grant |
| franops-bi deploy again, over the MCP drafts (S7.3 AC2) | the certified assets keep `certified` and get no `draft` |
"""

import json
import os
import sys
import time

from mcp_probe import call, mint, unknown_tool_lines
from rig_users import USERS

WRITE_SCOPE = "openid email profile superset:read superset:write"
RAW_SCHEMA, RAW_TABLE = "raw_callrail", "calls"


def parse(text):
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def table_config(column):
    return {"chart_type": "table", "columns": [{"name": column}]}


def main():
    from superset.app import create_app

    app = create_app()
    results = []

    def check(name, ok, detail):
        results.append(bool(ok))
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}", flush=True)

    with app.app_context():
        from superset.connectors.sqla.models import SqlaTable
        from superset.extensions import db
        from superset.models.dashboard import Dashboard
        from superset.models.slice import Slice
        from superset.tags.models import ObjectType, Tag, TaggedObject

        sm = app.appbuilder.sm

        def tags_of(object_type, object_id):
            db.session.expire_all()
            rows = (db.session.query(Tag.name).join(TaggedObject, TaggedObject.tag_id == Tag.id)
                    .filter(TaggedObject.object_type == object_type, TaggedObject.object_id == object_id)
                    .all())
            return sorted(name for (name,) in rows if not name.startswith(("type:", "owner:", "editor:")))

        def certified(object_type, model):
            db.session.expire_all()
            ids = [r.object_id for r in db.session.query(TaggedObject).join(Tag, TaggedObject.tag_id == Tag.id)
                   .filter(Tag.name == "certified", TaggedObject.object_type == object_type).all()]
            return db.session.query(model).filter(model.id.in_(ids)).all() if ids else []

        # 0. The deployed fixture.
        charts, dashboards, datasets = (certified(ObjectType.chart, Slice),
                                        certified(ObjectType.dashboard, Dashboard),
                                        certified(ObjectType.dataset, SqlaTable))
        check("the deploy made certified charts, dashboards and datasets",
              charts and dashboards and datasets, [len(charts), len(dashboards), len(datasets)])

        def certified_assets_clean(label):
            assets = ([(ObjectType.chart, c.id) for c in charts] + [(ObjectType.dashboard, d.id) for d in dashboards]
                      + [(ObjectType.dataset, d.id) for d in datasets])
            found = {f"{t.name}:{i}": tags_of(t, i) for t, i in assets}
            check(f"{label}: each certified asset has certified and no draft",
                  found and all("certified" in v and "draft" not in v for v in found.values()), found)

        certified_assets_clean("after the deploy")

        # The HQ grants of design/bi-image.md (rig only, item 49).
        hq = sm.find_role("HQ") or sm.add_role("HQ")
        grants = [("can_read", "Chart"), ("can_write", "Chart"), ("can_read", "Dashboard"),
                  ("can_write", "Dashboard"), ("can_read", "Tag")]
        for perm, view in grants:
            pv = sm.find_permission_view_menu(perm, view)
            if pv not in hq.permissions:
                sm.add_permission_role(hq, pv)
        for dataset in datasets:
            pv = sm.add_permission_view_menu("datasource_access", dataset.get_perm())
            if pv not in hq.permissions:
                sm.add_permission_role(hq, pv)
        db.session.commit()
        check("HQ has no database, schema, or all-datasource access",
              not [p for p in hq.permissions if p.permission.name in
                   ("database_access", "schema_access", "all_datasource_access", "all_database_access")],
              sorted(f"{p.permission.name}|{p.view_menu.name}" for p in hq.permissions))

        # A dataset of the raw table, as an operator could make it. HQ gets no grant on it.
        database = datasets[0].database
        raw = (db.session.query(SqlaTable)
               .filter_by(database_id=database.id, schema=RAW_SCHEMA, table_name=RAW_TABLE).one_or_none())
        if raw is None:
            raw = SqlaTable(table_name=RAW_TABLE, schema=RAW_SCHEMA, database=database)
            db.session.add(raw)
            db.session.flush()
            raw.fetch_metadata()
            db.session.commit()
        raw_id, raw_uuid, raw_perm = raw.id, str(raw.uuid), raw.get_perm()
        check("the raw dataset exists, with the column location_key",
              "location_key" in [c.column_name for c in raw.columns], [c.column_name for c in raw.columns])

        token = mint("hq", scope=WRITE_SCOPE)
        owner = f"oidc:{USERS['hq']['sub']}"

        # 1. S7.1 AC1: an MCP chart on a certified dataset.
        target = datasets[0]
        status, ok, text = call("generate_chart", token, {
            "request": {"dataset_id": target.id, "config": table_config("location_key"),
                        "chart_name": "HQ draft on a certified dataset", "save_chart": True,
                        "generate_preview": False}}, limit=None)
        db.session.expire_all()
        made = (db.session.query(Slice).filter_by(slice_name="HQ draft on a certified dataset")
                .order_by(Slice.id.desc()).first())
        hq_user = sm.find_user(username=owner)
        editors = [] if made is None else [e.user_id for e in made.editors]
        creator = None if made is None else made.created_by_fk
        check("S7.1 AC1: HQ generate_chart on a certified dataset makes a chart on it",
              status == 200 and ok and made is not None and made.datasource_id == target.id,
              f"{status} ok={ok} {text[:120] if not ok else ''}")
        check("S7.1 AC1: the HQ user made the chart and is its editor",
              hq_user is not None and creator == hq_user.id and hq_user.id in editors,
              [creator, editors, None if hq_user is None else hq_user.id])

        # 2. S7.3 AC1: the chart is a draft at once.
        if made is not None:
            check("S7.3 AC1: the MCP chart has draft and not certified, with no sweep",
                  tags_of(ObjectType.chart, made.id) == ["draft"], tags_of(ObjectType.chart, made.id))
            status, ok, text = call("generate_dashboard", token, {
                "request": {"chart_ids": [made.id], "dashboard_title": "HQ draft dashboard"}}, limit=None)
            db.session.expire_all()
            board = db.session.query(Dashboard).filter_by(dashboard_title="HQ draft dashboard").first()
            check("S7.3 AC1: HQ generate_dashboard makes a dashboard with draft",
                  ok and board is not None and tags_of(ObjectType.dashboard, board.id) == ["draft"],
                  f"ok={ok} {text[:100] if not ok else ''} "
                  f"{None if board is None else tags_of(ObjectType.dashboard, board.id)}")

        # 3. S7.1 AC2: no dataset of a raw table over MCP. The tools are not there (B-2, D-8).
        for tool, arguments in (("create_dataset", {"database_id": database.id, "schema": RAW_SCHEMA,
                                                    "table_name": RAW_TABLE}),
                                ("create_virtual_dataset", {"database_id": database.id,
                                                            "sql": f"select * from {RAW_SCHEMA}.{RAW_TABLE}",
                                                            "table_name": "raw_calls"})):
            seen = unknown_tool_lines(tool)
            status, ok, text = call(tool, token, {"request": arguments})
            time.sleep(0.3)
            after = unknown_tool_lines(tool)
            check(f"S7.1 AC2: HQ {tool} on the raw table fails, as a tool that is not there",
                  not ok and seen >= 0 and after > seen, f"{status}; unknown-tool lines {seen} -> {after}")
        db.session.expire_all()
        check("S7.1 AC2: no new dataset on the raw table",
              db.session.query(SqlaTable).filter_by(schema=RAW_SCHEMA).count() == 1,
              db.session.query(SqlaTable).filter_by(schema=RAW_SCHEMA).count())

        # 4. S7.1 AC2: no chart on the raw dataset. generate_chart checks access before it
        # makes the chart, and warns only after; so the check counts the charts.
        def charts_on_raw():
            db.session.expire_all()
            return db.session.query(Slice).filter_by(datasource_id=raw_id, datasource_type="table").count()

        for label, dataset_id in (("by ID", raw_id), ("by UUID", raw_uuid)):
            status, ok, text = call("generate_chart", token, {
                "request": {"dataset_id": dataset_id, "config": table_config("location_key"),
                            "chart_name": f"HQ chart on raw {label}", "save_chart": True,
                            "generate_preview": False}}, limit=None)
            # The tool answers a refused dataset as one that does not exist, with no chart.
            answer = parse(text)
            refused = (answer.get("chart") is None and answer.get("success") is not True
                       and "not found" in json.dumps(answer.get("error")).lower())
            check(f"S7.1 AC2: HQ generate_chart on the raw dataset {label} is refused, and makes no chart",
                  refused and charts_on_raw() == 0,
                  f"{status} error={json.dumps(answer.get('error'))[:80]}; charts on raw {charts_on_raw()}")

        # 5. The control: with a grant on the raw dataset, the same call makes a chart. The
        # refusal above came from the grant, not from the arguments.
        pv = sm.add_permission_view_menu("datasource_access", raw_perm)
        sm.add_permission_role(hq, pv)
        db.session.commit()
        status, ok, text = call("generate_chart", token, {
            "request": {"dataset_id": raw_id, "config": table_config("location_key"),
                        "chart_name": "control chart on raw", "save_chart": True, "generate_preview": False}})
        check("control: with a grant on the raw dataset, the same call makes a chart",
              ok and charts_on_raw() == 1, f"{status} {text[:90]}; charts on raw {charts_on_raw()}")
        hq = sm.find_role("HQ")
        sm.del_permission_role(hq, pv)
        for chart in db.session.query(Slice).filter_by(datasource_id=raw_id).all():
            db.session.delete(chart)
        db.session.commit()

    # 6. S7.3 AC2: a second deploy, over the MCP drafts. rig/hq-access-check.sh runs it, then
    # this probe again with HQ_PROBE_PHASE=redeploy.
    print(f"hq-probe: {sum(results)} of {len(results)} checks pass", flush=True)
    return 0 if all(results) else 1


def after_redeploy():
    from superset.app import create_app

    app = create_app()
    results = []

    def check(name, ok, detail):
        results.append(bool(ok))
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}", flush=True)

    with app.app_context():
        from superset.extensions import db
        from superset.models.dashboard import Dashboard
        from superset.models.slice import Slice
        from superset.tags.models import ObjectType, Tag, TaggedObject

        def tags_by_object(object_type):
            db.session.expire_all()
            rows = (db.session.query(TaggedObject.object_id, Tag.name)
                    .join(Tag, TaggedObject.tag_id == Tag.id)
                    .filter(TaggedObject.object_type == object_type, Tag.name.in_(("draft", "certified"))).all())
            found = {}
            for object_id, name in rows:
                found.setdefault(object_id, set()).add(name)
            return found

        for object_type, model, label in ((ObjectType.chart, Slice, "chart"),
                                          (ObjectType.dashboard, Dashboard, "dashboard")):
            found = tags_by_object(object_type)
            managed = {o.id for o in db.session.query(model).filter(model.is_managed_externally.is_(True))}
            both = {i: sorted(v) for i, v in found.items() if v == {"draft", "certified"}}
            check(f"S7.3 AC2: after a second deploy, no {label} has both tags", not both, both)
            check(f"S7.3 AC2: after a second deploy, each managed {label} is certified and not draft",
                  managed and all(found.get(i) == {"certified"} for i in managed),
                  {i: sorted(found.get(i, ())) for i in managed})
            drafts = [i for i, v in found.items() if v == {"draft"}]
            check(f"after a second deploy, the HQ {label} draft keeps draft", drafts, drafts)

    print(f"hq-probe (redeploy): {sum(results)} of {len(results)} checks pass", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(after_redeploy() if os.environ.get("HQ_PROBE_PHASE") == "redeploy" else main())
