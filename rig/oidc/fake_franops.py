"""A fake FranOps `userinfo` endpoint for the S5.2 login check (rig only).

GET /api/data/bi/userinfo?sub=<sub>, with `Authorization: Bearer <FAKE_FRANOPS_KEY>`, answers as
fran-ops `app/api/data/bi/userinfo/route.ts` does:
  200 {"biRole": "HQ", "organizationId", "userId", "email"}   an HQ user of rig_users.py
  200 {"biRole": "ADMIN", ...}                                an ADMIN user of rig_users.py
  200 {"biRole": "NONE", "organizationId"}                   each other sub
  401 a missing or wrong key · 422 a missing sub · a user's `franopsStatus` (502) when it has one,
after a user's `franopsDelay` seconds when it has one
GET /_calls lists each call: the sub and whether the key was right, never the key itself.
POST /_role?sub=<sub>&biRole=<role> changes the answer for one sub (the S5.3 demotion case).
"""

import json
import os
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from rig_users import USERS

KEY = os.environ.get("FAKE_FRANOPS_KEY", "fran_rig-bi-userinfo-key")
PORT = int(os.environ.get("FAKE_FRANOPS_PORT", "8081"))
ORG = "org_rig0001"
BY_SUB = {user["sub"]: user for user in USERS.values()}
CALLS = []


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/health":
            return self.send_json(200, {"ok": True})
        if url.path == "/_calls":
            return self.send_json(200, {"calls": CALLS})
        if url.path != "/api/data/bi/userinfo":
            return self.send_json(404, {"error": "not_found"})
        sub = urllib.parse.parse_qs(url.query).get("sub", [""])[0]
        key_ok = self.headers.get("Authorization", "") == f"Bearer {KEY}"
        CALLS.append({"sub": sub, "key_ok": key_ok})
        if not key_ok:
            return self.send_json(401, {"error": "Unauthorized"})
        if not sub:
            return self.send_json(422, {"error": "sub is required"})
        user = BY_SUB.get(sub)
        if user and user.get("franopsDelay"):
            time.sleep(user["franopsDelay"])
        if user and user.get("franopsStatus"):
            return self.send_json(user["franopsStatus"], {"error": "identity provider unavailable"})
        if user and user["biRole"] in ("HQ", "ADMIN"):
            return self.send_json(200, {"biRole": user["biRole"], "organizationId": ORG, "userId": sub,
                                        "email": user.get("franopsEmail") or user.get("email")})
        return self.send_json(200, {"biRole": "NONE", "organizationId": ORG})

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if url.path != "/_role" or query.get("sub") not in BY_SUB:
            return self.send_json(404, {"error": "not_found"})
        BY_SUB[query["sub"]]["biRole"] = query.get("biRole", "NONE")
        return self.send_json(200, {"ok": True})

    def log_message(self, fmt, *args):
        print(f"fake-franops: {self.command} {urllib.parse.urlparse(self.path).path}", flush=True)


if __name__ == "__main__":
    print(f"fake-franops: on port {PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
