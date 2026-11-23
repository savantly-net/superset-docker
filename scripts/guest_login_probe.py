"""Probe the service login and the embed settings of a running Superset (S5.8).

Runs inside the app container, which guest-login-check.sh starts with OAuth and embedding on:
`python guest_login_probe.py`. It seeds four users, then sends the broker's own login body
(`provider: db`, `refresh: false`) for each one. Spike T-4 measured the table that it pins:

| User | Expected |
|---|---|
| the service account, role GuestIssuer only | 200 |
| another user with a password | 401 |
| an OAuth user (empty password hash) | not 200 |
| another user with GuestIssuer and Admin | 401 |
| the service account after it also gets Admin | 401 |

It also reads the embed settings of bi-image.md section 3 from the app and from one response.
Exit 0 when each check passes. Prints one PASS or FAIL line for each check.
"""

import json
import os
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8088"
SERVICE = os.environ.get("BI_SERVICE_USERNAME", "franops_guest_issuer")
ISSUER_ROLE = os.environ.get("GUEST_ISSUER_ROLE", "GuestIssuer")


def login(username: str, password: str, provider: str = "db") -> int:
    body = {"username": username, "password": password, "provider": provider, "refresh": False}
    req = urllib.request.Request(
        BASE + "/api/v1/security/login",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status
    except urllib.error.HTTPError as err:
        return err.code


def headers(path: str) -> dict:
    try:
        with urllib.request.urlopen(BASE + path, timeout=15) as resp:
            return dict(resp.headers)
    except urllib.error.HTTPError as err:
        return dict(err.headers)


def main() -> int:
    from superset.app import create_app

    app = create_app()
    results = []

    def check(name: str, ok: bool, detail: object) -> None:
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")

    with app.app_context():
        from superset.extensions import db

        sm = app.appbuilder.sm
        issuer = sm.find_role(ISSUER_ROLE) or sm.add_role(ISSUER_ROLE)
        admin, gamma = sm.find_role("Admin"), sm.find_role("Gamma")

        def add(username: str, roles: list, password: str):
            user = sm.find_user(username=username) or sm.add_user(
                username, username, "probe", f"{username}@probe.invalid", roles, password=password
            )
            return user

        add(SERVICE, [issuer], "service-pass")
        add("probe_password_user", [gamma], "other-pass")
        oauth_user = add("probe_oauth_user", [gamma], "unused")
        oauth_user.password = ""  # FAB stores no password for an OAuth user.
        add("probe_issuer_admin", [issuer, admin], "issuer-admin-pass")
        db.session.commit()

        c = app.config
        check("AUTH_TYPE is OAuth", c["AUTH_TYPE"] == 4, c["AUTH_TYPE"])
        check("EMBEDDED_SUPERSET is on", c["FEATURE_FLAGS"].get("EMBEDDED_SUPERSET") is True,
              c["FEATURE_FLAGS"].get("EMBEDDED_SUPERSET"))
        check("GUEST_ROLE_NAME is Embed", c["GUEST_ROLE_NAME"] == "Embed", c["GUEST_ROLE_NAME"])
        check("GUEST_TOKEN_JWT_EXP_SECONDS is 300", c["GUEST_TOKEN_JWT_EXP_SECONDS"] == 300,
              c["GUEST_TOKEN_JWT_EXP_SECONDS"])
        check("GUEST_TOKEN_JWT_SECRET comes from the environment",
              c["GUEST_TOKEN_JWT_SECRET"] == os.environ.get("GUEST_TOKEN_JWT_SECRET"), "compared")
        origins = [os.environ["FRANOPS_ORIGIN"], *os.environ.get("EMBED_ORIGINS", "").split()]
        cors = (c.get("CORS_OPTIONS") or {}).get("origins", [])
        check("CORS allows each embed origin", all(o in cors for o in origins), cors)

    expected = [
        (f"{SERVICE} (service account) logs in", SERVICE, "service-pass", {200}),
        ("a password user is refused", "probe_password_user", "other-pass", {401}),
        ("an OAuth user is refused", "probe_oauth_user", "", None),
        ("a GuestIssuer user with Admin is refused", "probe_issuer_admin", "issuer-admin-pass", {401}),
    ]
    for name, username, password, codes in expected:
        code = login(username, password)
        ok = code != 200 if codes is None else code in codes
        check(name, ok, code)
    # The login schema also admits `ldap`. The image configures no LDAP server, so it must refuse.
    code = login("probe_password_user", "other-pass", "ldap")
    check("a password user is refused with the ldap provider", code != 200, code)

    with app.app_context():
        from superset.extensions import db

        sm = app.appbuilder.sm
        service = sm.find_user(username=SERVICE)
        service.roles = [sm.find_role(ISSUER_ROLE), sm.find_role("Admin")]
        db.session.commit()
    code = login(SERVICE, "service-pass")
    check(f"{SERVICE} with a second role (Admin) is refused", code == 401, code)

    page = headers("/login/")
    csp = page.get("Content-Security-Policy", "")
    ancestors = [a for d in csp.split(";") if d.strip().startswith("frame-ancestors")
                 for a in d.split()[1:]]
    check("frame-ancestors holds each embed origin", all(o in ancestors for o in origins), ancestors)
    check("no X-Frame-Options header", "X-Frame-Options" not in page, page.get("X-Frame-Options"))

    print(json.dumps({"checks": len(results), "failed": results.count(False)}))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
