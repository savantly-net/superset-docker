"""The S5.2 login check: OIDC sign-in through a fake provider and a fake FranOps (rig only).

Runs inside rig-superset (rig/oidc-login-check.sh): `python /rig-oidc/login_probe.py`. It plays
the browser with plain HTTP: GET /login/<provider>, then the provider's authorize URL with
`rig_user=<name>`, then Superset's callback, with Superset's session cookie. Prints one PASS or
FAIL line for each check, and exits 0 when each check passes.

| Case | Expected |
|---|---|
| an HQ user (FranOps: HQ) | signed in; the user `oidc:<sub>` has HQ, and Public (no permission) only beside it |
| the same HQ user, a second time | signed in; the same user, the role synced |
| an HQ user with no email or names in the claims | signed in; the email comes from FranOps |
| a franchisee (FranOps: NONE) | refused; no user `oidc:<sub>` |
| an HQ user while FranOps answers 502, or after the timeout | refused; no user `oidc:<sub>` |
| an HQ user with no ID token | signed in from the provider's userinfo endpoint |
| an HQ user with the email of another Superset user | refused; no user `oidc:<sub>` |
| the database login form (POST /login/ with the admin password) | 405, and no session |
| the admin with the API `db` login | 401 |
| the service account with the API `db` login | 200 and an access token (S5.8) |
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie

from rig_users import USERS

BASE = "http://127.0.0.1:8088"
PROVIDER = os.environ.get("OAUTH_NAME", "oidc")
ISSUER = os.environ.get("OAUTH_ISSUER", "").rstrip("/")
FRANOPS = os.environ.get("FRANOPS_URL", "").rstrip("/")
ADMIN_PASSWORD = os.environ.get("RIG_ADMIN_PASSWORD", "admin-rig-pass")
SERVICE = os.environ.get("BI_SERVICE_USERNAME", "franops_guest_issuer")
SERVICE_PASSWORD = "rig-service-pass"
ISSUER_ROLE = os.environ.get("GUEST_ISSUER_ROLE", "GuestIssuer")
# Roles that FAB may give an OAuth user next to HQ. `Public` is the registration role; it holds
# no permission (the lockdown check reads it).
ALLOWED_EXTRA_ROLES = {"Public"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


class Browser:
    """One browser: Superset's cookies, kept by hand (cookiejar drops some IP-host cookies)."""

    def __init__(self):
        self.cookies = {}

    def request(self, url, data=None, headers=None, method=None):
        headers = dict(headers or {})
        if url.startswith(BASE) and self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            resp = OPENER.open(req, timeout=20)
        except urllib.error.HTTPError as err:
            resp = err
        if url.startswith(BASE):
            for raw in resp.headers.get_all("Set-Cookie") or []:
                for name, morsel in SimpleCookie(raw).items():
                    self.cookies[name] = morsel.value
        body = resp.read()
        return resp.status, resp.headers, body

    def sign_in(self, user):
        """The OAuth round trip. Returns (callback status, callback Location)."""
        status, headers, _ = self.request(f"{BASE}/login/{PROVIDER}")
        authorize = headers.get("Location", "")
        if status != 302 or not authorize.startswith(f"{ISSUER}/authorize"):
            return status, f"/login/{PROVIDER} did not redirect to the provider: {authorize[:80]}"
        status, headers, _ = self.request(authorize + "&" + urllib.parse.urlencode({"rig_user": user}))
        callback = headers.get("Location", "")
        if status != 302 or "/oauth-authorized/" not in callback:
            return status, f"the provider did not redirect to the callback ({status})"
        status, headers, _ = self.request(callback)
        return status, headers.get("Location", "")

    def me(self):
        status, _, body = self.request(f"{BASE}/api/v1/me/")
        try:
            return status, json.loads(body).get("result", {})
        except ValueError:
            return status, {}


def api_login(username, password):
    status, _, body = Browser().request(
        f"{BASE}/api/v1/security/login",
        data=json.dumps({"username": username, "password": password, "provider": "db",
                         "refresh": False}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        token = json.loads(body).get("access_token")
    except ValueError:
        token = None
    return status, token


def get_json(url):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


def main():
    from superset.app import create_app

    app = create_app()
    results = []

    def check(name, ok, detail):
        results.append(bool(ok))
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}", flush=True)

    with app.app_context():
        from superset.extensions import db

        sm = app.appbuilder.sm
        from flask_appbuilder.const import AUTH_OAUTH

        check("AUTH_TYPE is AUTH_OAUTH", app.config["AUTH_TYPE"] == AUTH_OAUTH, app.config["AUTH_TYPE"])
        remote = app.config["OAUTH_PROVIDERS"][0]["remote_app"]
        check("the provider has no endpoint override, only the discovery document",
              "server_metadata_url" in remote and not {"authorize_url", "access_token_url"} & set(remote),
              sorted(remote))

        # The HQ role comes from `franops-bi roles apply` on a deployed BI; here it is empty.
        if sm.find_role("HQ") is None:
            sm.add_role("HQ")
        issuer = sm.find_role(ISSUER_ROLE) or sm.add_role(ISSUER_ROLE)
        service = sm.find_user(username=SERVICE)
        if service is None:
            sm.add_user(SERVICE, "Guest", "Issuer", "guest-issuer@rig-oidc.invalid", [issuer],
                        password=SERVICE_PASSWORD)
        else:
            service.roles = [issuer]
            sm.reset_password(service.id, SERVICE_PASSWORD)
        db.session.commit()

        def user_roles(name):
            db.session.expire_all()
            user = sm.find_user(username=f"oidc:{USERS[name]['sub']}")
            return None if user is None else sorted(role.name for role in user.roles), user

        def franops_calls(name):
            return [c for c in get_json(f"{FRANOPS}/_calls")["calls"] if c["sub"] == USERS[name]["sub"]]

        # 1. An HQ user signs in and gets HQ.
        browser = Browser()
        status, location = browser.sign_in("hq")
        check("HQ: the callback signs the user in", status == 302 and "/login" not in location,
              f"{status} -> {location}")
        # The rig's HQ role holds no permission, so /api/v1/me/ answers 403 to its session (an
        # anonymous request gets 401). With a permission, the answer names the user.
        status, me = browser.me()
        want = f"oidc:{USERS['hq']['sub']}"
        check("HQ: the browser holds a session (/api/v1/me/ is not 401)",
              status == 403 or (status == 200 and me.get("username") == want),
              f"{status} {me.get('username', '')}")
        roles, user = user_roles("hq")
        check("HQ: the Superset user is oidc:<sub>, with one login",
              user is not None and user.login_count == 1, None if user is None else user.login_count)
        check("HQ: the user has the HQ role, and no other role that grants anything",
              roles is not None and "HQ" in roles and set(roles) - {"HQ"} <= ALLOWED_EXTRA_ROLES, roles)
        check("HQ: names and email come from the ID token",
              user is not None and (user.email, user.first_name, user.last_name)
              == (USERS["hq"]["email"], USERS["hq"]["given_name"], USERS["hq"]["family_name"]),
              None if user is None else "email, first and last name read")
        calls = franops_calls("hq")
        check("HQ: BI asked FranOps with the API key and the verified sub",
              len(calls) == 1 and calls[0]["key_ok"], calls)

        # 2. The same user again: the same row, and the role is set again (AUTH_ROLES_SYNC_AT_LOGIN).
        if user is not None:
            user.roles = [sm.find_role("Public")]
            db.session.commit()
        status, location = Browser().sign_in("hq")
        roles2, user2 = user_roles("hq")
        check("HQ again: signed in, and the role synced back to HQ",
              status == 302 and "/login" not in location and roles2 is not None and "HQ" in roles2
              and user2 is not None and user is not None and user2.id == user.id, roles2)

        # 3. No email and no names in the claims: the email from FranOps.
        status, location = Browser().sign_in("hq_bare")
        roles3, user3 = user_roles("hq_bare")
        check("HQ with bare claims: signed in, with the FranOps email",
              status == 302 and "/login" not in location and roles3 is not None and "HQ" in roles3
              and user3 is not None and user3.email == USERS["hq_bare"]["franopsEmail"],
              f"{status} -> {location}; roles {roles3}")

        # 4. A franchisee is refused at login.
        browser = Browser()
        status, location = browser.sign_in("franchisee")
        check("franchisee: the callback sends the browser back to the login page",
              status == 302 and "/login" in location, f"{status} -> {location}")
        status, _ = browser.me()
        check("franchisee: no session (/api/v1/me/ is 401)", status == 401, status)
        roles, _ = user_roles("franchisee")
        check("franchisee: no Superset user oidc:<sub> exists", roles is None, roles)
        check("franchisee: FranOps was asked once", len(franops_calls("franchisee")) == 1,
              franops_calls("franchisee"))

        # 5. FranOps answers 502: the login is refused.
        browser = Browser()
        status, location = browser.sign_in("franops_down")
        check("FranOps 502: the login is refused", status == 302 and "/login" in location,
              f"{status} -> {location}")
        roles, _ = user_roles("franops_down")
        check("FranOps 502: no Superset user oidc:<sub> exists", roles is None, roles)

        # 5b. FranOps answers too late: refused.
        status, location = Browser().sign_in("franops_slow")
        roles, _ = user_roles("franops_slow")
        check("FranOps slower than FRANOPS_TIMEOUT_SECONDS: refused, and no user",
              status == 302 and "/login" in location and roles is None, f"{status} -> {location}; {roles}")

        # 5c. No ID token: the provider's userinfo endpoint gives the claims.
        status, location = Browser().sign_in("hq_no_id_token")
        roles, user = user_roles("hq_no_id_token")
        check("no ID token: signed in from the provider's userinfo, with HQ",
              status == 302 and "/login" not in location and roles is not None and "HQ" in roles
              and user.email == USERS["hq_no_id_token"]["email"], f"{status} -> {location}; {roles}")

        # 5d. The email of another Superset user: refused before FAB's insert fails.
        status, location = Browser().sign_in("hq_email_taken")
        roles, _ = user_roles("hq_email_taken")
        check("the email of another user: refused, and no user",
              status == 302 and "/login" in location and roles is None, f"{status} -> {location}; {roles}")

        # 6. The database login form is off.
        browser = Browser()
        status, _, page = browser.request(f"{BASE}/login/")
        check("/login/ answers", status == 200, status)
        status, _, _ = browser.request(
            f"{BASE}/login/", data=urllib.parse.urlencode(
                {"username": "admin", "password": ADMIN_PASSWORD}).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        me_status, _ = browser.me()
        # 405: no view takes a password at /login/. Under database auth the same POST reaches
        # the form view (and its CSRF check), so 405 is what shows that the form is off.
        check("the database login form is off: POST /login/ is 405 and gives no session",
              status == 405 and me_status == 401, f"POST {status}, then /api/v1/me/ {me_status}")
        status, token = api_login("admin", ADMIN_PASSWORD)
        check("the admin API db login is refused", status == 401, status)

        # 7. The service account still gets a token through the API db login (S5.8).
        status, token = api_login(SERVICE, SERVICE_PASSWORD)
        check("the service account API db login answers with a token", status == 200 and bool(token),
              status)

    print(f"login-probe: {sum(results)} of {len(results)} checks pass", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
