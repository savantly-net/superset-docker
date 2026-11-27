"""A small Superset REST client: login, CSRF, JSON calls, and the assets import and export.

It uses the standard library only, so the tool installs with PyYAML as its one dependency.
"""

from __future__ import annotations

import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any


class SupersetError(RuntimeError):
    """A Superset call returned a status that the caller did not expect."""

    def __init__(self, method: str, path: str, status: int, body: Any):
        self.status = status
        self.body = body
        super().__init__(f"{method} {path}: HTTP {status}: {body}")


class Superset:
    """A session on one Superset instance.

    With `api_key`, each call sends the key as the Bearer token, and nothing logs in: this is the
    way in under OAuth (S5.10), where the `db` login accepts the service account only. Without
    it, the session logs in with the `db` provider, as the rig does with OAuth off.
    """

    def __init__(
        self,
        url: str,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 120.0,
        *,
        api_key: str | None = None,
    ):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        self._token: str | None = None
        self._csrf: str | None = None
        if api_key:
            self._token = api_key
        else:
            if not username or not password:
                raise ValueError("Superset needs an API key, or a username and a password")
            status, body = self.request(
                "POST",
                "/api/v1/security/login",
                {"username": username, "password": password, "provider": "db", "refresh": False},
                expect=None,
            )
            if status != 200 or not isinstance(body, dict) or "access_token" not in body:
                raise SupersetError("POST", "/api/v1/security/login", status, body)
            self._token = body["access_token"]
        # A write with a Bearer token still needs the CSRF token and the session cookie.
        _, body = self.request("GET", "/api/v1/security/csrf_token/")
        self._csrf = body["result"]

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        raw: bytes | None = None,
        content_type: str = "application/json",
        expect: int | tuple[int, ...] | None = 200,
    ) -> tuple[int, Any]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if self._csrf:
            headers["X-CSRFToken"] = self._csrf
            headers["Referer"] = self.url
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
        if data is not None:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                status, payload, ctype = resp.status, resp.read(), resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as err:
            status, payload, ctype = err.code, err.read(), err.headers.get("Content-Type", "")
        parsed: Any = payload
        if "json" in ctype:
            try:
                parsed = json.loads(payload)
            except ValueError:
                pass
        if expect is not None:
            allowed = (expect,) if isinstance(expect, int) else expect
            if status not in allowed:
                raise SupersetError(method, path, status, parsed)
        return status, parsed

    def get(self, path: str, **query: Any) -> Any:
        if query:
            path += "?" + urllib.parse.urlencode({"q": _rison(query)})
        return self.request("GET", path)[1]

    def tagged_objects(self, tag: str) -> list[dict[str, Any]]:
        """The objects with `tag`, from `GET /api/v1/tag/get_objects/`.

        ⚠️ That endpoint reads plain query arguments, not Rison. With `?q=(tags:x)` it ignores the
        filter and returns each tagged object.
        """
        path = "/api/v1/tag/get_objects/?" + urllib.parse.urlencode({"tags": tag})
        return self.request("GET", path)[1].get("result", [])

    def list_all(self, resource: str, columns: list[str]) -> list[dict[str, Any]]:
        """Every row of `/api/v1/<resource>/`, page by page."""
        rows: list[dict[str, Any]] = []
        page = 0
        while True:
            body = self.get(
                f"/api/v1/{resource}/", columns=columns, page=page, page_size=100
            )
            rows.extend(body["result"])
            if len(rows) >= body["count"] or not body["result"]:
                return rows
            page += 1

    def import_assets(
        self, bundle: bytes, *, passwords: dict[str, str] | None = None, overwrite: bool = True
    ) -> tuple[int, Any]:
        fields = {"overwrite": "true" if overwrite else "false"}
        if passwords:
            fields["passwords"] = json.dumps(passwords)
        raw, ctype = _multipart(fields, {"bundle": ("bundle.zip", bundle, "application/zip")})
        return self.request(
            "POST", "/api/v1/assets/import/", raw=raw, content_type=ctype, expect=None
        )

    def export(self, resource: str, ids: list[int]) -> bytes:
        """The export ZIP of the `resource` objects with these ids, for example charts."""
        path = f"/api/v1/{resource}/export/?" + urllib.parse.urlencode({"q": _rison(list(ids))})
        status, body = self.request("GET", path, expect=None)
        if status != 200 or not isinstance(body, bytes):
            raise SupersetError("GET", path, status, body)
        return body

    def export_assets(self) -> bytes:
        status, body = self.request("GET", "/api/v1/assets/export/", expect=None)
        if status != 200 or not isinstance(body, bytes):
            raise SupersetError("GET", "/api/v1/assets/export/", status, body)
        return body


def _multipart(
    fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]
) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    for name, (filename, content, ctype) in files.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'.encode()
            + content
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _rison(value: Any) -> str:
    """Encode the small subset of Rison that the list API needs (dicts, lists, str, int)."""
    if isinstance(value, dict):
        return "(" + ",".join(f"{k}:{_rison(v)}" for k, v in value.items()) + ")"
    if isinstance(value, (list, tuple)):
        return "!(" + ",".join(_rison(v) for v in value) + ")"
    if isinstance(value, bool):
        return "!t" if value else "!f"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if text and all(c.isalnum() or c in "_-./~" for c in text) and not text[0].isdigit():
        return text
    return "'" + text.replace("!", "!!").replace("'", "!'") + "'"
