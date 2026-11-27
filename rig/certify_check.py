"""The S7.4 and S7.5 checks on the rig: the certified view, and `franops-bi certify`.

rig/hq-access-check.sh runs it on the host after the HQ probe, with OAuth off, because
`certify` and `deploy` log in with the `db` provider (S5.8 refuses it in OAuth mode). At that
point BI holds the certified fixture, and the HQ drafts of the probe: a chart on a certified
dataset and a dashboard of that chart. The script makes one more draft, as HQ over REST.

`rig_hq_db` is an `HQ` user with a password, so that the checks run through the HQ grants of the
real permission path. The admin is the reviewer (D-6: Savantly operators).

| Case | Expected |
|---|---|
| HQ: the certified list, `GET /api/v1/tag/get_objects/?tags=certified` (S7.4 AC1) | no asset with `draft` |
| HQ: the certified page, `/all_entities/?id=<certified>` | 200 |
| HQ: the chart and dashboard list APIs (S7.4 AC2) | the label `draft` on each draft |
| HQ: `certify chart`, and the tag API calls of the swap (S7.5 AC3) | exit 1, and `403`; nothing changes |
| admin: `certify chart`, then `certify dashboard` (S7.5 AC1) | `certified` alone; one commit each |
| `validate` and `deploy` of the branch, then an export (S7.5 AC2) | the same YAML; the hook tags nothing |
| the certified list after the certify (S7.4 AC1) | still no asset with `draft` |
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

import yaml

from franops_bi.client import Superset

ROOT = pathlib.Path(__file__).resolve().parents[1]
URL = f"http://127.0.0.1:{os.environ.get('RIG_SUPERSET_PORT', '18388')}"
ADMIN = ("admin", os.environ.get("RIG_ADMIN_PASSWORD", "admin-rig-pass"))
HQ_DB = ("rig_hq_db", "hq-db-rig-pass")
DB_PASSWORDS = '{"databases/warehouse.yaml":"warehouse-rig-pass"}'

results: list[bool] = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}", flush=True)


def tagged(client, tag):
    return {(o["type"], int(o["id"])) for o in client.tagged_objects(tag) if o["type"] in ("chart", "dashboard")}


def labels(client, resource, title):
    body = client.get(f"/api/v1/{resource}/", columns=["id", title, "tags.name"], page_size=100)
    return {int(r["id"]): sorted(t["name"] for t in r.get("tags") or []) for r in body["result"]}


def find(client, resource, title, value):
    body = client.get(f"/api/v1/{resource}/", columns=["id", title], page_size=100)
    ids = [int(r["id"]) for r in body["result"] if r[title] == value]
    return ids[0] if len(ids) == 1 else None


def hook_tag_lines():
    logs = subprocess.run(
        ["docker", "compose", "-f", "rig/compose.yml", "logs", "--no-log-prefix", "rig-superset"],
        cwd=ROOT, capture_output=True, text=True,
    )
    return (logs.stdout + logs.stderr).count("draft hook: tagged")


def franops_bi(*args, user=ADMIN):
    env = dict(os.environ, SUPERSET_URL=URL, SUPERSET_USERNAME=user[0], SUPERSET_PASSWORD=user[1],
               FRANOPS_BI_DB_PASSWORDS=DB_PASSWORDS)
    done = subprocess.run([sys.executable, "-m", "franops_bi", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True)
    return done.returncode, (done.stdout + done.stderr).strip()


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout


def exported(client, resource, asset_id, folder):
    blob = client.export(resource, [asset_id])
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        docs = [yaml.safe_load(z.read(n)) for n in z.namelist() if n.split("/")[1:2] == [folder]]
    return docs[0] if len(docs) == 1 else None


# What the import itself fills or drops, with no change to the asset: the chart's `datasource`,
# which the import builds from `dataset_uuid`, and an empty `annotation_layers` or
# `filter_scopes`. Any other difference fails the round trip.
IMPORT_DEFAULTS = {"params": {"datasource"}, "metadata": set()}
EMPTY = (None, [], {}, "")


def normalized(doc):
    out = dict(doc or {})
    for key, ignored in IMPORT_DEFAULTS.items():
        if isinstance(out.get(key), dict):
            out[key] = {k: v for k, v in out[key].items() if k not in ignored and v not in EMPTY}
    return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def page_status(client, path):
    """The status of a page for a client, with no redirect followed. The client follows redirects,
    so on Superset 7.0.0rc2 a denied page came back as the login page, a 200: S7.4 passed with no
    grant until 7.0.0rc3 answered 403. A redirect here is its own 3xx status."""
    cookies = next(h for h in client._opener.handlers if isinstance(h, urllib.request.HTTPCookieProcessor))
    opener = urllib.request.build_opener(cookies, _NoRedirect())
    headers = {"Authorization": f"Bearer {client._token}"} if client._token else {}
    try:
        with opener.open(urllib.request.Request(client.url + path, headers=headers), timeout=client.timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as err:
        return err.code


def certified_view_clean(hq, admin, label):
    certified, drafts = tagged(hq, "certified"), tagged(admin, "draft")
    check(f"S7.4 AC1 {label}: HQ's certified list holds no asset with draft",
          certified and not certified & drafts, f"certified={sorted(certified)} drafts={sorted(drafts)}")


def main():
    admin = Superset(URL, *ADMIN)
    hq = Superset(URL, *HQ_DB)

    mcp_chart = find(admin, "chart", "slice_name", "HQ draft on a certified dataset")
    mcp_board = find(admin, "dashboard", "dashboard_title", "HQ draft dashboard")
    check("the HQ probe left one MCP draft chart and one MCP draft dashboard",
          mcp_chart and mcp_board, [mcp_chart, mcp_board])
    if not (mcp_chart and mcp_board):
        return 1

    # One more draft, made by HQ over REST, on the dataset of the MCP chart.
    rows = admin.get("/api/v1/chart/", columns=["id", "datasource_id"], page_size=100)["result"]
    dataset_id = next(int(r["datasource_id"]) for r in rows if int(r["id"]) == mcp_chart)
    status, body = hq.request("POST", "/api/v1/chart/", {
        "slice_name": "HQ REST draft", "viz_type": "table", "datasource_id": dataset_id,
        "datasource_type": "table", "params": json.dumps({"viz_type": "table", "query_mode": "raw",
                                                          "all_columns": ["location_key"]})}, expect=None)
    rest_chart = body.get("id") if isinstance(body, dict) else None
    check("HQ makes a chart over REST on a certified dataset", status == 201 and rest_chart, status)

    # S7.4: the certified view, as HQ.
    certified_view_clean(hq, admin, "before the certify")
    tag_rows = admin.get("/api/v1/tag/", filters=[{"col": "name", "opr": "eq", "value": "certified"}])
    tag_id = tag_rows["result"][0]["id"] if tag_rows.get("result") else None
    status = page_status(hq, f"/all_entities/?id={tag_id}")
    check("S7.4: HQ opens the certified page /all_entities/?id=<certified>, with no redirect", status == 200, [tag_id, status])

    drafts = tagged(admin, "draft")
    shown = {("chart", i): v for i, v in labels(hq, "chart", "slice_name").items()}
    shown |= {("dashboard", i): v for i, v in labels(hq, "dashboard", "dashboard_title").items()}
    check("S7.4 AC2: the drafts are a chart over MCP, a chart over REST, and a dashboard",
          {("chart", mcp_chart), ("chart", rest_chart), ("dashboard", mcp_board)} <= drafts, sorted(drafts))
    visible = [d for d in drafts if d in shown]
    check("S7.4 AC2: HQ's list APIs return the label draft for each draft that HQ can list",
          visible and all("draft" in shown[d] for d in visible),
          {f"{t}:{i}": shown.get((t, i)) for t, i in drafts})
    # Superset lists an unpublished dashboard to its editors only, so another HQ user sees no
    # MCP dashboard of the probe's user. The admin lists each one.
    every = {("chart", i): v for i, v in labels(admin, "chart", "slice_name").items()}
    every |= {("dashboard", i): v for i, v in labels(admin, "dashboard", "dashboard_title").items()}
    check("S7.4 AC2: the list APIs return the label draft for each draft (admin)",
          all("draft" in every.get(d, []) for d in drafts), {f"{t}:{i}": every.get((t, i)) for t, i in drafts})

    # S7.5: a brand repository that holds the fixture.
    work = pathlib.Path(tempfile.mkdtemp(prefix="certify-rig-"))
    repo = work / "brand"
    shutil.copytree(ROOT / "tests" / "fixtures" / "bi", repo / "bi")
    git(repo, "init", "--quiet", "-b", "main")
    git(repo, "config", "user.email", "reviewer@rig.invalid")
    git(repo, "config", "user.name", "rig reviewer")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "the fixture")

    # AC3 first: HQ is not a reviewer.
    code, out = franops_bi("certify", "chart", str(mcp_chart), "--repo", str(repo), user=HQ_DB)
    check("S7.5 AC3: certify as HQ exits 1: BI refuses the export, before any swap",
          code == 1 and "refused" in out and "did not change" in out, out.splitlines()[-2:] if out else code)
    # The swap itself, with no tool in the way: BI refuses it to HQ (HQ has can_read on Tag and can_list on Tags, no write).
    removed, _ = hq.request("DELETE", f"/api/v1/tag/2/{mcp_chart}/draft/", expect=None)
    added, _ = hq.request("POST", f"/api/v1/tag/2/{mcp_chart}/", {"properties": {"tags": ["certified"]}},
                          expect=None)
    check("S7.5 AC3: HQ's own call to remove draft or to add certified is refused",
          removed == 403 and added == 403, [removed, added])
    check("S7.5 AC3: the chart keeps draft and gets no certified",
          ("chart", mcp_chart) in tagged(admin, "draft") and ("chart", mcp_chart) not in tagged(admin, "certified"))
    check("S7.5 AC3: the repository has no new branch and no change",
          git(repo, "branch", "--list").split() == ["*", "main"] and not git(repo, "status", "--porcelain"))

    # AC1: the reviewer.
    for kind, asset_id in (("chart", mcp_chart), ("dashboard", mcp_board)):
        code, out = franops_bi("certify", kind, str(asset_id), "--repo", str(repo))
        draft, cert = tagged(admin, "draft"), tagged(admin, "certified")
        check(f"S7.5 AC1: certify {kind} as the reviewer swaps draft for certified",
              code == 0 and (kind, asset_id) in cert and (kind, asset_id) not in draft, out.splitlines()[-2:])
    log = git(repo, "log", "--format=%s", "main..HEAD").splitlines()
    check("S7.5 AC1: one commit for each certify, on the new branches", len(log) == 2, log)
    check("S7.5 AC1: the work tree is clean", not git(repo, "status", "--porcelain"))
    certified_view_clean(hq, admin, "after the certify")
    check("S7.4 AC2 after the certify: the REST draft keeps its label",
          "draft" in labels(hq, "chart", "slice_name").get(rest_chart, []))

    # AC2: the round trip through S5.5.
    code, out = franops_bi("validate", "--dir", str(repo / "bi"))
    check("S7.5 AC2: franops-bi validate accepts the branch's bi/", code == 0, out[-120:])
    files = {"chart": next(repo.glob("bi/charts/HQ_draft_on_a_certified_dataset_*.yaml")),
             "dashboard": next(repo.glob("bi/dashboards/HQ_draft_dashboard_*.yaml"))}
    before = hook_tag_lines()
    code, out = franops_bi("deploy", "--dir", str(repo / "bi"), "--no-sources")
    check("S7.5 AC2: franops-bi deploy imports the branch's bi/", code == 0, out.splitlines()[-1:])
    check("S7.5 AC2: the hook tags no asset during the deploy", hook_tag_lines() == before,
          [before, hook_tag_lines()])
    for kind, asset_id, folder in (("chart", mcp_chart, "charts"), ("dashboard", mcp_board, "dashboards")):
        written = normalized(yaml.safe_load(files[kind].read_text()))
        again = exported(admin, kind, asset_id, folder)
        again = None if again is None else normalized(again)
        same_id = find(admin, kind, "slice_name" if kind == "chart" else "dashboard_title",
                       written.get("slice_name") or written.get("dashboard_title")) == asset_id
        diff = sorted(k for k in set(written) | set(again or {}) if written.get(k) != (again or {}).get(k))
        for k in diff:
            a, b2 = written.get(k), (again or {}).get(k)
            if isinstance(a, dict) and isinstance(b2, dict):
                print(f"  {kind}.{k}:", {j: (a.get(j), b2.get(j)) for j in set(a) | set(b2) if a.get(j) != b2.get(j)})
        check(f"S7.5 AC2: the deployed {kind} is the same object, and its export equals the file",
              same_id and again is not None and not diff, diff)
        check(f"S7.5 AC2: after the deploy, the {kind} is certified and not draft",
              (kind, asset_id) in tagged(admin, "certified") and (kind, asset_id) not in tagged(admin, "draft"))
    certified_view_clean(hq, admin, "after the deploy")

    shutil.rmtree(work, ignore_errors=True)
    print(f"certify-check: {sum(results)} of {len(results)} checks pass", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
