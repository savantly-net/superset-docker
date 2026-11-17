"""Probe each /api/v1 route of a running Superset with no credentials (S5.4, NFR-2).

Runs inside the app container: `python -I lockdown_probe.py [base-url]`. It reads the routes
from the app's own URL map, so a route that a new Superset version adds is probed with no
change here. Each method of each route gets one request with no cookie, no Authorization
header and no CSRF token. Path arguments get a placeholder value.

A route passes when it answers 401 or 403. A write that Flask-WTF refuses first (400, "The
CSRF token is missing") also keeps the anonymous caller out, but it is not the AC's answer, so
it is listed apart. Each other answer fails, unless ALLOWED names the route with a reason.

Exit 0 when every route group passes, 1 otherwise. Prints one line for each group.
"""

import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8088"

# (method, rule) -> (allowed codes, reason). Each entry is a route that answers an anonymous
# caller with something other than 401/403 on purpose. Keep the reason; a new entry needs one.
ALLOWED = {
    ("POST", "/api/v1/security/login"): (
        {400, 401},
        "the login itself: a request with no credentials gets no session",
    ),
    ("GET", "/api/v1/database/oauth2/"): (
        {400},
        "the OAuth2 callback for database connections: it refuses a request with no signed state",
    ),
}

# Routes that answer 404 while their feature flag is off. The flag check runs before the
# authentication check. When a deployment turns the flag on, the route must answer 401/403,
# and this check then fails until the entry is removed.
FLAG_OFF = {
    ("GET", "/api/v1/chart/<pk>/cache_screenshot/"): "THUMBNAILS",
    ("GET", "/api/v1/chart/<pk>/screenshot/<digest>/"): "THUMBNAILS",
    ("GET", "/api/v1/chart/<pk>/thumbnail/<digest>/"): "THUMBNAILS",
    ("POST", "/api/v1/dashboard/<pk>/cache_dashboard_screenshot/"): "THUMBNAILS",
    ("GET", "/api/v1/dashboard/<pk>/screenshot/<digest>/"): "THUMBNAILS",
    ("GET", "/api/v1/dashboard/<pk>/thumbnail/<digest>/"): "THUMBNAILS",
    ("GET", "/api/v1/embedded_dashboard/<uuid>"): "EMBEDDED_SUPERSET",
}

GUEST_TOKEN_RULE = "/api/v1/security/guest_token/"

PLACEHOLDER = {"int": "1", "float": "1.0", "uuid": "00000000-0000-0000-0000-000000000000"}


ARG = re.compile(r"<(?:(\w+)(?:\([^)]*\))?:)?\w+>")


def concrete(rule) -> str:
    """The rule's path with each argument replaced by a placeholder of its converter's type."""
    return ARG.sub(lambda m: PLACEHOLDER.get(m.group(1) or "", "1"), rule.rule)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D102
        return None


OPENER = urllib.request.build_opener(_NoRedirect)


def send(method: str, path: str):
    """One anonymous request. Redirects are not followed: a redirect is the answer."""
    data = b"{}" if method in ("POST", "PUT", "PATCH") else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for _ in range(5):
        time.sleep(0.03)  # Superset's global limit is 50 requests a second for each address.
        try:
            with OPENER.open(req, timeout=30) as resp:
                code, body, loc = resp.status, resp.read(300), resp.headers.get("Location", "")
        except urllib.error.HTTPError as err:
            code, body, loc = err.code, err.read(300), err.headers.get("Location", "")
        if code != 429:
            break
        time.sleep(1.5)
    return code, body.decode("utf-8", "replace"), loc


# (rls rules, expected verdict) for GUEST_TOKEN_VALIDATOR_HOOK. Spike T-4 measured each refused
# case as a token that returns every row.
VALIDATOR_CASES = [
    ([{"clause": "location_key IN ('cl1','cl2')"}], True),
    ([{"clause": "a = 'x' AND (b = 'y')"}, {"clause": "c = 'it''s'"}], True),
    ([], False),
    ([{"clause": "  "}], False),
    ([{"dataset": 7, "clause": "franchisee_id = 'A'"}], False),
    ([{"clause": "franchisee_id = 'A') OR (1=1"}], False),
    ([{"clause": "franchisee_id = 'A'; DROP TABLE x"}], False),
    ([{"clause": "franchisee_id = 'A' -- "}], False),
    ([{"clause": "franchisee_id = 'A"}], False),
]


def check_validator(app) -> list:
    hook = app.config.get("GUEST_TOKEN_VALIDATOR_HOOK")
    if not callable(hook):
        return ["GUEST_TOKEN_VALIDATOR_HOOK is not set"]
    errors = []
    for rls, expected in VALIDATOR_CASES:
        body = {"user": {"username": "probe"}, "resources": [], "rls": rls}
        if bool(hook(body)) is not expected:
            errors.append(f"validator gave {not expected} for rls={rls!r}")
    return errors


def main() -> int:
    from superset.app import create_app

    app = create_app()
    rules = [r for r in app.url_map.iter_rules() if r.rule.startswith("/api/v1/")]
    keys = ("auth", "csrf", "allowed", "flag_off")
    groups = defaultdict(lambda: {**{k: 0 for k in keys}, "fail": []})
    guest_token_codes = []
    for rule in sorted(rules, key=lambda r: r.rule):
        group = rule.rule.split("/")[3] or "(root)"
        path = concrete(rule)
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            code, body, loc = send(method, path)
            g = groups[group]
            allowed = ALLOWED.get((method, rule.rule))
            if rule.rule == GUEST_TOKEN_RULE:
                guest_token_codes.append(code)
            if code in (401, 403):
                g["auth"] += 1
            elif method != "GET" and (
                (code == 400 and "csrf" in body.lower())
                or (code == 302 and loc.startswith("/login/"))
            ):
                # Flask-WTF refuses the write before authentication runs. Superset answers a
                # missing CSRF token with 400 on most routes, and with a redirect to the login
                # page on some (error_handling.py, "Refresh CSRF token error").
                g["csrf"] += 1
            elif allowed and code in allowed[0]:
                g["allowed"] += 1
            elif code == 404 and (method, rule.rule) in FLAG_OFF:
                g["flag_off"] += 1
            else:
                g["fail"].append(f"{method} {path} -> {code} {loc or body[:100]!r}")

    failed = 0
    for group in sorted(groups):
        g = groups[group]
        ok = not g["fail"]
        failed += 0 if ok else 1
        print(
            f"{'PASS' if ok else 'FAIL'} {group}: {g['auth']} x 401/403, {g['csrf']} x CSRF,"
            f" {g['allowed']} allowed, {g['flag_off']} flag off"
        )
        for line in g["fail"]:
            print(f"    {line}")

    # The CSRF exemption: without it, the guest-token route answers 400 and a Bearer-only
    # broker cannot call it.
    extra = []
    if guest_token_codes != [401]:
        extra.append(f"POST {GUEST_TOKEN_RULE} gave {guest_token_codes}, not [401]")
    extra += check_validator(app)
    for line in extra:
        print(f"FAIL {line}")
    print(json.dumps({"routes": len(rules), "groups": len(groups), "failed_groups": failed,
                      "other_failures": len(extra), "validator_cases": len(VALIDATOR_CASES)}))
    return 1 if failed or extra else 0


if __name__ == "__main__":
    sys.exit(main())
