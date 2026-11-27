"""POST /api/v1/franops/roles/apply/ (S5.9; design/bi-image.md, Data Model).

`franops-bi deploy` calls this endpoint after each import, so that a newly certified dataset
gets its grants at once. It runs `franops_bi.roles.apply()` in the web process, the same code as
`franops-bi roles apply` in /app/franops-init.sh.

Only Admin reaches it: the view FranopsRoles is in ADMIN_ONLY_VIEW_MENUS, and the handler checks
the Admin role again. Under OAuth, the caller is an Admin with an API key (S5.10). The answer
names roles and permission counts only, never a password.

superset_config.py installs this module through FLASK_APP_MUTATOR.
"""

import logging

from flask import current_app, g
from flask_appbuilder.api import BaseApi, expose, safe
from flask_appbuilder.security.decorators import protect

logger = logging.getLogger("franops.roles_api")


class FranopsRolesApi(BaseApi):
    resource_name = "franops/roles"
    class_permission_name = "FranopsRoles"
    method_permission_name = {"apply": "write"}
    allow_browser_login = False
    openapi_spec_tag = "FranOps"

    @expose("/apply/", methods=("POST",))
    @protect()
    @safe
    def apply(self):
        admin = current_app.config.get("AUTH_ROLE_ADMIN", "Admin")
        if admin not in [r.name for r in getattr(g.user, "roles", None) or []]:
            return self.response_403()
        from franops_bi import roles

        try:
            result = roles.apply(roles.service_password_from_env())
        except roles.RolesError as err:
            return self.response_400(message=str(err))
        logger.info("roles applied by %s: %s", getattr(g.user, "username", "?"), result.lines())
        return self.response(200, **result.as_dict())


_installed = False


def install(app) -> None:
    global _installed
    if _installed:
        return
    from superset.extensions import appbuilder

    appbuilder.add_api(FranopsRolesApi)
    _installed = True
