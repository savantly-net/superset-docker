"""The S5.9 probe, inside rig-superset after a deploy: each role is exactly the design table.

Run by rig/roles-check.sh. Prints one PASS or FAIL line, and a JSON line with the admin's API
key when ROLES_PROBE_MAKE_KEY=1 (the rig's key; it lives as long as the rig).
"""
import json
import os
import sys

from superset.app import create_app

app = create_app()
failures = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": {detail}"))
    if not ok:
        failures.append(name)


with app.app_context():
    from superset import db, security_manager as sm
    from superset.connectors.sqla.models import SqlaTable
    from superset.tags.models import ObjectType, Tag, TaggedObject

    from franops_bi.roles import BASE_GRANTS

    ids = [r.object_id for r in db.session.query(TaggedObject).join(Tag, Tag.id == TaggedObject.tag_id)
           .filter(Tag.name == "certified", TaggedObject.object_type == ObjectType.dataset)]
    perms = {ds.get_perm() for ds in db.session.query(SqlaTable).filter(SqlaTable.id.in_(ids))}
    check("the deploy certified datasets", perms, ids)
    for name, base in BASE_GRANTS.items():
        role = sm.find_role(name)
        have = set() if role is None else {(p.permission.name, p.view_menu.name) for p in role.permissions}
        want = set(base) | {("datasource_access", p) for p in perms}
        check(f"role {name} is exactly the design table", have == want,
              {"extra": sorted(have - want), "missing": sorted(want - have)})
    user = sm.find_user(username=os.environ.get("BI_SERVICE_USERNAME", "franops_guest_issuer"))
    check("the service account has GuestIssuer only",
          user is not None and [r.name for r in user.roles] == ["GuestIssuer"],
          None if user is None else [r.name for r in user.roles])
    def holders(view):
        return sorted({f"{r.name}:{p.permission.name}" for r in sm.get_all_roles()
                       for p in r.permissions if p.view_menu.name == view})

    found = holders("FranopsRoles")
    check("only Admin holds FranopsRoles (the roles endpoint)", found == ["Admin:can_write"], found)
    found = holders("ApiKey")
    print(f"info ApiKey holders: {found}")
    check("no FranOps role and not Public holds ApiKey",
          not [h for h in found if h.split(":")[0] in ("HQ", "Embed", "GuestIssuer", "Public")], found)

    if os.environ.get("ROLES_PROBE_MAKE_KEY") == "1":
        admin = sm.find_user(username="admin")
        gamma = sm.find_user(username="rig_gamma") or sm.add_user(
            "rig_gamma", "Rig", "Gamma", "gamma@rig.invalid", [sm.find_role("Gamma")], password="g-rig-pass")
        keys = {"admin": sm.create_api_key(admin, "rig-roles-check")["key"],
                "gamma": sm.create_api_key(gamma, "rig-roles-check")["key"]}
        print("RIGJSON " + json.dumps(keys))

print("PASS" if not failures else "FAIL: " + "; ".join(failures))
sys.exit(1 if failures else 0)
