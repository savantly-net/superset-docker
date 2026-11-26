"""A fake OpenID Connect provider for the S5.2 login check (rig only, never in the image).

It serves what Superset's generic OAuth provider reads: the discovery document, an authorize
endpoint, a token endpoint, a userinfo endpoint, and a JWKS. Its ID tokens have the claims that
spike T-5 measured on the MVP provider (sub, email, name, given_name, family_name, org_id) and
no role and no preferred_username, signed RS256 with a key made at start. A user with
`noIdToken` gets an access token only, so BI must read the userinfo endpoint.

The authorize endpoint shows no sign-in page. The test driver adds `rig_user=<name>` to the
authorize URL, and the endpoint redirects to the callback with a code for that user.
GET /_issued lists the tokens that it issued, so the check can look for them in the logs.
POST /_mint makes a JWT access token in the shape that spike T-5 check 3 measured on the MVP
provider (`out/email-resource.json`): RS256, `iss`, `sub`, `aud` (the `resource` of RFC 8707),
`scope` as one string, `client_id`, `org_id`, and no email. The S5.3 MCP check sends a JSON body
`{"user", "aud", "scope", "iss", "exp_in", "foreign_key"}`; each key but `user` is optional, and
`aud: null` leaves the claim out. `foreign_key: true` signs with a key that is not in the JWKS.

Env: FAKE_OIDC_ISSUER (http://rig-fake-oidc:8080), FAKE_OIDC_CLIENT_ID, FAKE_OIDC_CLIENT_SECRET,
FAKE_OIDC_PORT (8080).
"""

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from rig_users import USERS

ISSUER = os.environ.get("FAKE_OIDC_ISSUER", "http://rig-fake-oidc:8080").rstrip("/")
CLIENT_ID = os.environ.get("FAKE_OIDC_CLIENT_ID", "rig-bi-client")
CLIENT_SECRET = os.environ.get("FAKE_OIDC_CLIENT_SECRET", "rig-bi-client-secret")
PORT = int(os.environ.get("FAKE_OIDC_PORT", "8080"))
KID = "rig-oidc-key-1"

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
FOREIGN_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_JWK = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key())), "kid": KID, "use": "sig", "alg": "RS256"}

CODES = {}  # code -> {user, nonce, redirect_uri, client_id}
ACCESS = {}  # access token -> user name
ISSUED = []  # each access token and ID token string

DISCOVERY = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/authorize",
    "token_endpoint": f"{ISSUER}/token",
    "userinfo_endpoint": f"{ISSUER}/userinfo",
    "jwks_uri": f"{ISSUER}/jwks.json",
    "response_types_supported": ["code"],
    "subject_types_supported": ["public"],
    "id_token_signing_alg_values_supported": ["RS256"],
    "scopes_supported": ["openid", "email", "profile"],
    "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
    "claims_supported": ["sub", "email", "name", "given_name", "family_name", "org_id"],
}


def claims_of(name):
    user = USERS[name]
    claims = {"sub": user["sub"], "org_id": "org_rig0001"}
    for key in ("email", "name", "given_name", "family_name"):
        if user.get(key):
            claims[key] = user[key]
    return claims


def id_token(name, nonce, access_token):
    now = int(time.time())
    # at_hash: the left half of the SHA-256 of the access token (OIDC Core 3.1.3.6).
    digest = hashlib.sha256(access_token.encode()).digest()
    at_hash = base64.urlsafe_b64encode(digest[:16]).rstrip(b"=").decode()
    payload = {**claims_of(name), "iss": ISSUER, "aud": CLIENT_ID, "iat": now, "exp": now + 300,
               "nonce": nonce, "at_hash": at_hash}
    return jwt.encode(payload, KEY, algorithm="RS256", headers={"kid": KID})


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, body, headers=None):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if url.path == "/.well-known/openid-configuration":
            return self.send_json(200, DISCOVERY)
        if url.path == "/jwks.json":
            return self.send_json(200, {"keys": [PUBLIC_JWK]})
        if url.path == "/health":
            return self.send_json(200, {"ok": True})
        if url.path == "/_issued":
            return self.send_json(200, {"tokens": ISSUED})
        if url.path == "/authorize":
            name = query.get("rig_user")
            if query.get("client_id") != CLIENT_ID or name not in USERS or "redirect_uri" not in query:
                return self.send_json(400, {"error": "invalid_request"})
            code = secrets.token_urlsafe(16)
            CODES[code] = {"user": name, "nonce": query.get("nonce", ""),
                           "redirect_uri": query["redirect_uri"]}
            target = query["redirect_uri"] + "?" + urllib.parse.urlencode(
                {"code": code, "state": query.get("state", "")})
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
            return None
        if url.path == "/userinfo":
            token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if token not in ACCESS:
                return self.send_json(401, {"error": "invalid_token"})
            return self.send_json(200, claims_of(ACCESS[token]))
        return self.send_json(404, {"error": "not_found"})

    def mint(self, body):
        name = body.get("user")
        if name not in USERS:
            return self.send_json(400, {"error": "unknown user"})
        now = int(time.time())
        payload = {"iss": body.get("iss", ISSUER), "sub": USERS[name]["sub"], "client_id": "rig-mcp-client",
                   "org_id": "org_rig0001", "iat": now, "nbf": now - 5,
                   "exp": now + int(body.get("exp_in", 300)), "jti": "oat_" + secrets.token_urlsafe(8),
                   "scope": body.get("scope", "openid email profile superset:read")}
        if body.get("aud", "absent") is not None:
            payload["aud"] = body.get("aud", ISSUER)
        key = FOREIGN_KEY if body.get("foreign_key") else KEY
        token = jwt.encode(payload, key, algorithm="RS256", headers={"kid": KID})
        ISSUED.append(token)
        return self.send_json(200, {"access_token": token})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        if path == "/_mint":
            return self.mint(json.loads(self.rfile.read(length) or b"{}"))
        if path != "/token":
            return self.send_json(404, {"error": "not_found"})
        form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(length).decode()).items()}
        client_id, client_secret = form.get("client_id"), form.get("client_secret")
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            client_id, _, client_secret = base64.b64decode(auth[6:]).decode().partition(":")
            client_id, client_secret = urllib.parse.unquote(client_id), urllib.parse.unquote(client_secret)
        if client_id != CLIENT_ID or client_secret != CLIENT_SECRET:
            return self.send_json(401, {"error": "invalid_client"})
        grant = CODES.pop(form.get("code", ""), None)
        if form.get("grant_type") != "authorization_code" or grant is None \
                or grant["redirect_uri"] != form.get("redirect_uri"):
            return self.send_json(400, {"error": "invalid_grant"})
        access_token = "rig-access-" + secrets.token_urlsafe(24)
        ACCESS[access_token] = grant["user"]
        body = {"access_token": access_token, "token_type": "Bearer", "expires_in": 300,
                "scope": "openid email profile"}
        ISSUED.append(access_token)
        if not USERS[grant["user"]].get("noIdToken"):
            body["id_token"] = id_token(grant["user"], grant["nonce"], access_token)
            ISSUED.append(body["id_token"])
        return self.send_json(200, body, {"Cache-Control": "no-store"})

    def log_message(self, fmt, *args):
        # The path holds codes and states; log the method and the path only, with no query.
        print(f"fake-oidc: {self.command} {urllib.parse.urlparse(self.path).path}", flush=True)


if __name__ == "__main__":
    print(f"fake-oidc: issuer {ISSUER} on port {PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
