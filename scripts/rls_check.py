#!/usr/bin/env python3
"""The automated RLS check (S5.6, NFR-8): a guest token for one franchisee sees no other row.

    python3 scripts/rls_check.py --url URL --plan plan.json \\
        --clause "location_key IN ('loc-001')" --allow loc-001

The check logs in as the service account with the `db` provider and a Bearer token only, the
same way the FranOps broker does. So it runs with OAuth on or off. For each certified chart of
the plan, and for each dashboard that holds the chart, it mints a guest token for that
dashboard with the clause, and calls `POST /api/v1/chart/data` with the token.

The plan comes from `rig/rig.sh rls-plan`. The service password comes from
RLS_CHECK_SERVICE_PASSWORD (default: the rig's).

Exit codes:
  0  each chart answers 200 with at least one row, and each row's key is in --allow
  3  a LEAK: a row with a key outside --allow (the seeded fault must give exactly this)
  1  the check could not judge: no certified chart, a login or mint refused, a chart that
     answers other than 200, a chart with no row, or a chart whose rows carry no key column
A chart that no dashboard holds is skipped with a line, because no guest token reaches it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

LEAK = 3
CANNOT_JUDGE = 1


class CheckError(Exception):
    pass


def call(url: str, path: str, body: Any, headers: dict[str, str]) -> tuple[int, Any]:
    req = urllib.request.Request(
        url + path,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as err:
        status, raw = err.code, err.read()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw[:300].decode(errors="replace")


def service_token(url: str, username: str, password: str) -> str:
    status, body = call(url, "/api/v1/security/login",
                        {"username": username, "password": password, "provider": "db",
                         "refresh": False}, {})
    if status != 200 or not isinstance(body, dict) or "access_token" not in body:
        raise CheckError(f"service login as {username}: HTTP {status}: {body}")
    return body["access_token"]


def guest_token(url: str, bearer: str, embedded_uuid: str, rls: list[dict[str, str]]) -> str:
    status, body = call(url, "/api/v1/security/guest_token/",
                        {"user": {"username": "rls-check", "first_name": "rls",
                                  "last_name": "check"},
                         "resources": [{"type": "dashboard", "id": embedded_uuid}],
                         "rls": rls},
                        {"Authorization": f"Bearer {bearer}"})
    if status != 200 or not isinstance(body, dict) or "token" not in body:
        raise CheckError(f"guest token for dashboard {embedded_uuid}: HTTP {status}: {body}")
    return body["token"]


def query_context(chart: dict[str, Any], dashboard_id: int) -> dict[str, Any]:
    """The chart's stored query context, or one built from its params.

    The build covers the table and aggregate shapes (groupby or all_columns, metrics). A chart
    type that needs more answers other than 200, and the check then fails on it.
    """
    stored = chart.get("queryContext")
    if stored:
        body = json.loads(json.dumps(stored))
        body["form_data"] = {**body.get("form_data", {}), "dashboardId": dashboard_id}
        body["force"] = True
        return body
    params = chart.get("params") or {}
    columns = params.get("all_columns") if params.get("query_mode") == "raw" else None
    columns = columns or params.get("groupby") or []
    return {
        "datasource": {"id": chart["datasetId"], "type": "table"},
        "force": True,
        "queries": [{
            "columns": columns,
            "metrics": params.get("metrics") or [],
            "orderby": [],
            "row_limit": params.get("row_limit") or 1000,
            "filters": [],
            "extras": {"having": "", "where": ""},
        }],
        "form_data": {**params, "slice_id": chart["chartId"], "dashboardId": dashboard_id},
        "result_format": "json",
        "result_type": "full",
    }


def chart_rows(url: str, token: str, chart: dict[str, Any], dashboard_id: int) -> list[dict]:
    status, body = call(url, "/api/v1/chart/data", query_context(chart, dashboard_id),
                        {"X-GuestToken": token})
    if status != 200 or not isinstance(body, dict):
        raise CheckError(f"chart {chart['chartId']} on dashboard {dashboard_id}: "
                         f"HTTP {status}: {body}")
    rows: list[dict] = []
    for result in body.get("result", []):
        rows.extend(result.get("data") or [])
    return rows


def run(args: argparse.Namespace) -> int:
    with open(args.plan) as fh:
        plan = json.load(fh)
    charts = plan.get("charts") or []
    if not charts:
        print("FAIL: the plan holds no certified chart; the check has nothing to judge")
        return CANNOT_JUDGE
    allowed = set(args.allow)
    rls = [] if args.no_rls else [{"clause": args.clause}]
    username = args.service_username or plan.get("serviceUsername") or "franops_guest_issuer"
    bearer = service_token(args.url, username, args.service_password)

    leaks: list[str] = []
    judged = 0
    for chart in charts:
        label = f"chart {chart['chartId']} ({chart.get('chartName')})"
        if not chart.get("dashboards"):
            print(f"SKIP {label}: no dashboard holds it, so no guest token reaches it")
            continue
        for dash in chart["dashboards"]:
            token = guest_token(args.url, bearer, dash["embeddedUuid"], rls)
            rows = chart_rows(args.url, token, chart, dash["id"])
            where = f"{label} on dashboard {dash['id']}"
            if not rows:
                raise CheckError(f"{where}: no row; an empty answer proves nothing")
            if any(args.key_column not in row for row in rows):
                raise CheckError(f"{where}: a row has no {args.key_column!r} column, so the "
                                 "check cannot read whose row it is")
            others = sorted({str(r[args.key_column]) for r in rows} - allowed)
            if others:
                leaks.append(f"{where}: {len(rows)} rows, keys outside the token's "
                             f"franchisee: {', '.join(others)}")
                print(f"LEAK {where}: {others}")
            else:
                print(f"ok   {where}: {len(rows)} rows, each key in the allowed set")
            judged += 1
    if not judged:
        print("FAIL: no certified chart sits on a dashboard; the check judged nothing")
        return CANNOT_JUDGE
    if leaks:
        print(f"FAIL: {len(leaks)} of {judged} chart answers hold a row of another franchisee")
        return LEAK
    print(f"PASS: {judged} chart answers, each holding rows of the token's franchisee only")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--url", required=True)
    p.add_argument("--plan", required=True, help="the JSON line of rig/rig.sh rls-plan")
    p.add_argument("--clause", help="the RLS clause of franchisee A's guest token")
    p.add_argument("--no-rls", action="store_true",
                   help="mint the token with no rls rule (the seeded fault)")
    p.add_argument("--allow", action="append", default=[], required=True,
                   help="a key of franchisee A; repeatable")
    p.add_argument("--key-column", default="location_key")
    p.add_argument("--service-username", default=os.environ.get("RLS_CHECK_SERVICE_USERNAME"))
    p.add_argument("--service-password",
                   default=os.environ.get("RLS_CHECK_SERVICE_PASSWORD", "svc-rig-pass"))
    args = p.parse_args(argv)
    if bool(args.clause) == args.no_rls:
        p.error("give exactly one of --clause and --no-rls")
    url = args.url.rstrip("/")
    args.url = url
    try:
        return run(args)
    except CheckError as err:
        print(f"FAIL (cannot judge): {err}")
        return CANNOT_JUDGE


if __name__ == "__main__":
    sys.exit(main())
