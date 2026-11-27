"""The BI roles and the service account (S5.9; design/bi-image.md, Data Model).

`apply()` sets the roles `HQ`, `Embed` and `GuestIssuer` to exactly the permissions of the role
table, and gives the service account the role `GuestIssuer` and nothing else. A second run
changes nothing. It runs in process, inside the BI image, in two places:

- `franops-bi roles apply`, which `/app/franops-init.sh` runs after `superset init`, in the
  same one-off task as the migrations;
- `POST /api/v1/franops/roles/apply/` (config/franops_api.py), which `franops-bi deploy` calls
  after each import, so that a newly certified dataset gets its grants at once.

The dataset grants are `datasource_access` on each dataset tagged `certified`. The design gives
`Embed` and `GuestIssuer` the datasets of the scorecard dashboards. BI has no scorecard mark, so
each certified dataset stands for them: a scorecard dataset is certified (S12.1).

This module imports Superset inside the functions only, so the CLI loads without Superset.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable

CERTIFIED = "certified"

# The fixed grants of each role key, as (permission, view menu). The dataset grants come on top.
BASE_GRANTS: dict[str, tuple[tuple[str, str], ...]] = {
    "HQ": (
        ("can_read", "Chart"),
        ("can_write", "Chart"),
        ("can_read", "Dashboard"),
        ("can_write", "Dashboard"),
        ("can_read", "Tag"),
        ("can_list", "Tags"),
    ),
    # Chart data is 403 without can_read on Chart (T-4).
    "Embed": (("can_read", "Dashboard"), ("can_read", "Chart")),
    "GuestIssuer": (("can_grant_guest_token", "SecurityRestApi"),),
}

# A role of this module never holds one of these (design/bi-image.md: HQ gets no SQL Lab and no
# database, schema, or all-datasource access). apply() fails before it commits such a role.
FORBIDDEN = frozenset(
    {"database_access", "schema_access", "all_datasource_access", "all_database_access",
     "all_query_access", "can_sqllab", "can_execute_sql_query"}
)


class RolesError(RuntimeError):
    """The roles cannot be applied. Nothing was committed."""


@dataclass
class RoleChange:
    name: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


@dataclass
class Result:
    roles: list[RoleChange]
    service_account: str  # "created", "updated", "unchanged", or "skipped: <reason>"
    datasets: int

    @property
    def changed(self) -> bool:
        return any(r.changed for r in self.roles) or self.service_account in ("created", "updated")

    def as_dict(self) -> dict:
        return {
            "datasets": self.datasets,
            "serviceAccount": self.service_account,
            "roles": {r.name: {"added": r.added, "removed": r.removed} for r in self.roles},
            "changed": self.changed,
        }

    def lines(self) -> list[str]:
        out = [f"roles: {self.datasets} certified datasets"]
        for r in self.roles:
            if r.changed:
                out.append(f"role {r.name}: +{len(r.added)} -{len(r.removed)}")
            else:
                out.append(f"role {r.name}: unchanged")
        out.append(f"service account: {self.service_account}")
        return out


def role_names(config: dict) -> dict[str, str]:
    """The Superset role name of each role key, from the image configuration."""
    return {
        "HQ": (config.get("FRANOPS_BI_ROLES") or {}).get("HQ", "HQ"),
        "Embed": config.get("GUEST_ROLE_NAME") or "Embed",
        "GuestIssuer": config.get("GUEST_ISSUER_ROLE") or "GuestIssuer",
    }


def desired(dataset_perms: Iterable[str]) -> dict[str, set[tuple[str, str]]]:
    """The exact permission set of each role key, for the certified datasets' perm strings."""
    access = {("datasource_access", perm) for perm in dataset_perms}
    return {key: set(grants) | access for key, grants in BASE_GRANTS.items()}


def diff(name: str, current: set[tuple[str, str]], wanted: set[tuple[str, str]]) -> RoleChange:
    def fmt(items: set[tuple[str, str]]) -> list[str]:
        return sorted(f"{p} on {v}" for p, v in items)

    return RoleChange(name, added=fmt(wanted - current), removed=fmt(current - wanted))


def apply(service_password: str | None = None) -> Result:
    """Set the roles and the service account. Needs an app context. Commits once, at the end.

    `service_password` sets the password of the service account. With no password, an existing
    account keeps its password, and a missing account is not created (the result says so).
    """
    from flask import current_app
    from superset import db, security_manager as sm
    from superset.connectors.sqla.models import SqlaTable
    from superset.tags.models import ObjectType, Tag, TaggedObject

    config = current_app.config
    names = role_names(config)
    try:
        ids = [
            row.object_id
            for row in db.session.query(TaggedObject)
            .join(Tag, Tag.id == TaggedObject.tag_id)
            .filter(Tag.name == CERTIFIED, TaggedObject.object_type == ObjectType.dataset)
            .all()
        ]
        datasets = db.session.query(SqlaTable).filter(SqlaTable.id.in_(ids)).all() if ids else []
        wanted = desired(ds.get_perm() for ds in datasets)

        def pv(perm: str, view: str):
            if perm == "datasource_access":
                found = sm.add_permission_view_menu(perm, view)
            else:
                found = sm.find_permission_view_menu(perm, view)
            if found is None:
                raise RolesError(f"the permission {perm} on {view} does not exist (run superset init)")
            return found

        changes = []
        roles = {}
        for key, grants in wanted.items():
            bad = sorted(p for p, _ in grants if p in FORBIDDEN)
            if bad:
                raise RolesError(f"role {names[key]} would get {bad}")
            role = sm.find_role(names[key]) or sm.add_role(names[key])
            current = {(p.permission.name, p.view_menu.name) for p in role.permissions}
            changes.append(diff(names[key], current, grants))
            role.permissions = [pv(p, v) for p, v in sorted(grants)]
            roles[key] = role

        issuer = roles["GuestIssuer"]
        username = config.get("BI_SERVICE_USERNAME") or "franops_guest_issuer"
        user = sm.find_user(username=username)
        if user is None:
            if not service_password:
                account = "skipped: no BI_SERVICE_PASSWORD, and the account does not exist"
            else:
                created = sm.add_user(username, "franops", "guest issuer",
                                      f"{username}@franops.invalid", [issuer],
                                      password=service_password)
                if not created:
                    raise RolesError(f"the service account {username} was not created")
                account = "created"
        else:
            account = "unchanged"
            if [r.name for r in user.roles] != [issuer.name]:
                user.roles = [issuer]
                account = "updated"
            if service_password:
                from werkzeug.security import check_password_hash, generate_password_hash

                if not user.password or not check_password_hash(user.password, service_password):
                    user.password = generate_password_hash(
                        service_password,
                        method=config.get("FAB_PASSWORD_HASH_METHOD", "scrypt"),
                        salt_length=config.get("FAB_PASSWORD_HASH_SALT_LENGTH", 16),
                    )
                    account = "updated"
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return Result(roles=changes, service_account=account, datasets=len(datasets))


def service_password_from_env(env_name: str = "BI_SERVICE_PASSWORD") -> str | None:
    value = os.environ.get(env_name, "")
    return value or None
