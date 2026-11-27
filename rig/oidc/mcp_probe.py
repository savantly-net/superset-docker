"""The S5.3 MCP check: MCP calls with tokens from a fake OIDC provider (rig only).

Runs inside rig-superset (rig/mcp-auth-check.sh), after the script starts `superset mcp run` on
MCP_URL. The fake provider mints JWT access tokens in the shape that spike T-5 check 3 measured
(POST /_mint), and the fake FranOps answers the BI role of each sub. Prints one PASS or FAIL line
for each check, and exits 0 when each check passes.

| Case | Expected |
|---|---|
| the shipped MCP settings | auth on; no API key, guest token or fixed user; the ten tools of B-2 step 8, each a tool of this Superset |
| no token, a token that is not a JWT | 401 |
| no `aud`, a wrong `aud`, a wrong `iss`, a key not in the JWKS, an expired token | 401 |
| a token without `superset:read` | refused (401 or 403) |
| an HQ token, no Superset user yet | the call runs as `oidc:<sub>`, made with HQ; the action log row names that user |
| the HQ token: tools/list | none of the ten tools |
| the HQ token: execute_sql, create_virtual_dataset, create_dataset | each call fails as an unknown tool, also through `call_tool` |
| an HQ user without the HQ role | the next call gives the role back |
| a franchisee token (FranOps: NONE) | the call fails; no user `oidc:<sub>` |
| an HQ token while FranOps answers 502 | the call fails |
| HQ, then FranOps answers NONE | the call fails after MCP_ROLE_CACHE_SECONDS |
| HQ, then a web login makes the row Admin | the next call fails, inside MCP_ROLE_CACHE_SECONDS |
| a client registration request, the OAuth metadata documents | 4xx |
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from rig_users import USERS

MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:5008/mcp")
MCP_BASE = MCP_URL.rsplit("/", 1)[0]
ISSUER = os.environ.get("OAUTH_ISSUER", "").rstrip("/")
FRANOPS = os.environ.get("FRANOPS_URL", "").rstrip("/")
AUDIENCE = os.environ.get("MCP_JWT_AUDIENCE", "")
CACHE_SECONDS = float(os.environ.get("MCP_ROLE_CACHE_SECONDS", "60"))
# The MCP service's own log (rig/mcp-auth-check.sh writes it). A removed tool logs
# `Unknown tool: '<name>'`; an RBAC refusal or a validation error does not.
MCP_LOG = Path(os.environ.get("MCP_LOG", "/tmp/mcp.log"))


def unknown_tool_lines(tool):
    try:
        return MCP_LOG.read_text(errors="replace").count(f"Unknown tool: '{tool}'")
    except OSError:
        return -1

# Spike B-2 step 8. The check reads the shipped setting against this list, so a change to the
# setting fails here until the spike's list changes too.
B2_DISABLED = {
    "find_users", "execute_sql", "get_chart_sql", "open_sql_lab_with_context", "list_rls_filters",
    "get_rls_filter_info", "list_users", "get_user_info", "create_virtual_dataset", "create_dataset",
}
D8_TOOLS = ("execute_sql", "create_virtual_dataset", "create_dataset")


def http(url, body=None, token=None, method=None):
    headers = {"Accept": "application/json, text/event-stream"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        response = urllib.request.urlopen(request, timeout=30)
    except urllib.error.HTTPError as err:
        response = err
    return response.status, response.read().decode()


def rpc(method, params=None, token=None):
    """One JSON-RPC request on the stateless streamable-HTTP transport. Returns (status, message)."""
    status, raw = http(MCP_URL, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, token)
    message = None
    for line in raw.splitlines():
        if line.startswith("data:"):
            message = json.loads(line[5:])
    if message is None and raw.strip().startswith("{"):
        message = json.loads(raw)
    return status, message


def call(tool, token, arguments=None, limit=200):
    """tools/call. Returns (status, ok, text): ok is True when the result is not an error.

    The `call_tool` proxy of the tool search answers a failed inner call with isError false and
    a text that starts with `Error`, so that text is an error too.
    """
    status, message = rpc("tools/call", {"name": tool, "arguments": arguments or {}}, token)
    if not message or "result" not in message:
        return status, False, json.dumps(message)[:limit]
    result = message["result"]
    text = " ".join(c.get("text", "") for c in result.get("content", []))
    return status, not result.get("isError", False) and not text.startswith("Error"), text[:limit]


def mint(user, **claims):
    body = json.dumps({"user": user, "aud": AUDIENCE, **claims}).encode()
    request = urllib.request.Request(f"{ISSUER}/_mint", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())["access_token"]


def set_franops_role(name, role):
    urllib.request.urlopen(urllib.request.Request(
        f"{FRANOPS}/_role?sub={USERS[name]['sub']}&biRole={role}", method="POST"), timeout=10)


def main():
    from superset.app import create_app

    app = create_app()
    results = []

    def check(name, ok, detail):
        results.append(bool(ok))
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}", flush=True)

    with app.app_context():
        from superset.extensions import db
        from superset.models.core import Log

        sm = app.appbuilder.sm
        cfg = app.config

        # 0. The shipped settings.
        check("MCP auth is on, with the verifier on the provider's JWKS and the BI audience",
              cfg["MCP_AUTH_ENABLED"] is True and cfg["MCP_JWKS_URI"] and cfg["MCP_JWT_AUDIENCE"] == AUDIENCE
              and cfg["MCP_JWT_ISSUER"] == ISSUER and cfg["MCP_JWT_ALGORITHM"] == "RS256",
              {k: bool(cfg.get(k)) for k in ("MCP_JWKS_URI", "MCP_JWT_AUDIENCE", "MCP_JWT_ISSUER")})
        check("the required scope is superset:read", cfg["MCP_REQUIRED_SCOPES"] == ["superset:read"],
              cfg["MCP_REQUIRED_SCOPES"])
        check("no API key, no guest token, no MCP_DEV_USERNAME",
              cfg["MCP_API_KEY_ENABLED"] is False and cfg["MCP_EMBEDDED_GUEST_AUTH_ENABLED"] is False
              and not cfg.get("MCP_DEV_USERNAME"),
              [cfg["MCP_API_KEY_ENABLED"], cfg["MCP_EMBEDDED_GUEST_AUTH_ENABLED"], cfg.get("MCP_DEV_USERNAME")])
        check("MCP_USER_RESOLVER is the image's resolver",
              getattr(cfg.get("MCP_USER_RESOLVER"), "__name__", "") == "mcp_user_resolver",
              getattr(cfg.get("MCP_USER_RESOLVER"), "__name__", None))
        check("MCP_DISABLED_TOOLS is the list of spike B-2 step 8", set(cfg["MCP_DISABLED_TOOLS"]) == B2_DISABLED,
              sorted(set(cfg["MCP_DISABLED_TOOLS"]) ^ B2_DISABLED))
        # A name that is not a tool is removed with a warning only, so a renamed tool would come
        # back. Each name must be a tool function of this Superset.
        import superset.mcp_service as mcp_pkg

        defined = set()
        for path in Path(mcp_pkg.__file__).parent.glob("*/tool/*.py"):
            defined |= set(re.findall(r"^(?:async )?def ([a-z_]+)\(", path.read_text(), re.M))
        check("each disabled name is a tool of this Superset", B2_DISABLED <= defined,
              sorted(B2_DISABLED - defined))

        # The HQ role comes from `franops-bi roles apply` on a deployed BI. Here it may read
        # dashboards, so an HQ call has a tool that it may run.
        hq = sm.find_role("HQ") or sm.add_role("HQ")
        perm = sm.find_permission_view_menu("can_read", "Dashboard")
        if perm not in hq.permissions:
            sm.add_permission_role(hq, perm)
        db.session.commit()

        def user_of(name):
            db.session.expire_all()
            return sm.find_user(username=f"oidc:{USERS[name]['sub']}")

        # 1. No token, and tokens that the verifier refuses.
        status, _ = rpc("tools/list")
        check("no token: tools/list is 401", status == 401, status)
        status, _, _ = call("list_dashboards", None)
        check("no token: tools/call is 401", status == 401, status)
        status, _, _ = call("list_dashboards", "not-a-jwt")
        check("a token that is not a JWT: 401", status == 401, status)
        for label, claims in (("no aud", {"aud": None}), ("a wrong aud", {"aud": "https://other.invalid"}),
                              ("a wrong iss", {"iss": "https://issuer.invalid"}),
                              ("a key not in the JWKS", {"foreign_key": True}),
                              ("an expired token", {"exp_in": -60})):
            status, _, _ = call("list_dashboards", mint("hq", **claims))
            check(f"{label}: 401", status == 401, status)
        status, _, _ = call("list_dashboards", mint("hq", scope="openid email profile"))
        check("no superset:read scope: refused", status in (401, 403), status)
        # Item 53 (NFR-6): the provider gives the BI audience to each of its clients.
        for label, client in (("a client not in MCP_ALLOWED_CLIENT_IDS", "other-clerk-client"),
                              ("a token with no client_id", None)):
            status, ok, text = call("list_dashboards", mint("hq", client_id=client))
            check(f"{label}: refused", status in (401, 403) or not ok, f"{status} {text[:80]}")
        check("no refused token made a user", user_of("hq") is None, None)

        # 2. An HQ token, with no Superset user yet: the resolver makes it.
        token = mint("hq")
        before = db.session.query(Log).filter(Log.action == "mcp_tool_call").count()
        status, ok, text = call("list_dashboards", token)
        check("HQ: list_dashboards runs", status == 200 and ok, f"{status} {text[:80]}")
        user = user_of("hq")
        check("HQ: the resolver made the user oidc:<sub> with the HQ role",
              user is not None and [r.name for r in user.roles] == ["HQ"],
              None if user is None else [r.name for r in user.roles])
        check("HQ: the user's email comes from FranOps (the access token has none)",
              user is not None and user.email == USERS["hq"]["email"], "email read")
        db.session.expire_all()
        rows = (db.session.query(Log).filter(Log.action == "mcp_tool_call")
                .order_by(Log.id.desc()).limit(5).all())
        names = [sm.get_user_by_id(r.user_id).username if r.user_id else None for r in rows]
        tools = [json.loads(r.json or "{}").get("tool") for r in rows]
        check("HQ: the action log has a new mcp_tool_call row, by username oidc:<sub>",
              db.session.query(Log).filter(Log.action == "mcp_tool_call").count() > before
              and names and names[0] == f"oidc:{USERS['hq']['sub']}" and tools[0] == "list_dashboards",
              list(zip(names, tools))[:2])

        # 3. The disabled tools.
        # The tool search is on (Superset's default): tools/list pins a few tools, and
        # search_tools with no query lists each tool that the caller may see.
        status, message = rpc("tools/list", token=token)
        listed = {t["name"] for t in (message or {}).get("result", {}).get("tools", [])}
        _, _, text = call("search_tools", token, limit=None)
        try:
            found = {t["name"] for t in json.loads(text)}
        except (ValueError, TypeError, KeyError):
            found = set()
        check("HQ: tools/list and search_tools list tools, and none of the ten",
              "list_dashboards" in found and not (listed | found) & B2_DISABLED,
              f"{len(listed)} listed, {len(found)} found; {sorted((listed | found) & B2_DISABLED)}")
        args = {"database_id": 1, "sql": "select 1", "table_name": "x"}
        # A failure alone proves nothing: the rig's HQ role cannot run these tools, and the
        # arguments do not validate. Each call must fail because the tool is not there.
        for tool in D8_TOOLS:
            for label, name, arguments in (("", tool, args),
                                           (" through the call_tool proxy", "call_tool",
                                            {"name": tool, "arguments": args})):
                seen = unknown_tool_lines(tool)
                status, ok, text = call(name, token, arguments)
                time.sleep(0.3)
                after = unknown_tool_lines(tool)
                check(f"HQ: {tool}{label} fails, as a tool that is not there",
                      status == 200 and not ok and seen >= 0 and after > seen,
                      f"{status} {text[:50]}; unknown-tool lines {seen} -> {after}")

        # 4. The role comes back when the user lost it.
        if user is not None:
            user.roles = [sm.find_role("Public")]
            db.session.commit()
        time.sleep(CACHE_SECONDS + 0.5)
        status, ok, _ = call("list_dashboards", token)
        user = user_of("hq")
        check("HQ without the HQ role: the call runs and the role is back",
              ok and user is not None and "HQ" in [r.name for r in user.roles],
              None if user is None else [r.name for r in user.roles])

        # 5. Users that FranOps does not answer HQ for.
        status, ok, text = call("list_dashboards", mint("franchisee"))
        check("franchisee: the token passes, and the call fails", status == 200 and not ok, f"{status} {text[:70]}")
        check("franchisee: no user oidc:<sub>", user_of("franchisee") is None, None)
        status, ok, _ = call("list_dashboards", mint("franops_down"))
        check("FranOps 502: the call fails, and no user", not ok and user_of("franops_down") is None, status)

        # 5a. An org admin (FranOps ADMIN) is refused: MCP is for HQ only (S7.1, item 68).
        status, ok, text = call("list_dashboards", mint("org_admin"))
        check("ADMIN: the token passes, and the call fails", status == 200 and not ok, f"{status} {text[:70]}")
        check("ADMIN: no user oidc:<sub>", user_of("org_admin") is None, None)

        # 6. Demotion: HQ at the first call, then NONE.
        token = mint("mcp_demoted")
        _, ok_first, _ = call("list_dashboards", token)
        set_franops_role("mcp_demoted", "NONE")
        time.sleep(CACHE_SECONDS + 0.5)
        _, ok_after, text = call("list_dashboards", token)
        check("demoted: HQ runs, and after the cache the same valid token fails",
              ok_first and not ok_after, f"first {ok_first}, after {ok_after}: {text[:60]}")

        # 6a. Promotion inside the cache window: HQ at the first call, then a web login syncs the
        # row to Admin. The next call, still inside MCP_ROLE_CACHE_SECONDS, must not run as Admin.
        token = mint("hq")
        _, ok_first, _ = call("list_dashboards", token)
        user = user_of("hq")
        admin = sm.find_role("Admin")
        if user is not None and admin is not None:
            user.roles = [admin]
            db.session.commit()
        set_franops_role("hq", "ADMIN")
        _, ok_after, text = call("list_dashboards", token)
        check("promoted inside the cache window: HQ runs, then the call fails at once",
              ok_first and admin is not None and not ok_after,
              f"first {ok_first}, after {ok_after}: {text[:60]}")
        set_franops_role("hq", "HQ")

        # 7. Dynamic client registration is off (NFR-6).
        for path, method, body in (("/register", "POST", {"redirect_uris": ["http://127.0.0.1/cb"]}),
                                   ("/.well-known/oauth-authorization-server", "GET", None)):
            status, _ = http(f"{MCP_BASE}{path}", body, method=method)
            check(f"{method} {path}: 4xx", 400 <= status < 500, status)

    print(f"mcp-probe: {sum(results)} of {len(results)} checks pass", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
