"""franops-bi: deploy and check the certified assets of a brand repository's `bi/` folder.

    franops-bi validate --dir bi/
    franops-bi deploy   --dir bi/ --url https://bi.example.com --franops-url https://franops.io
    franops-bi certify  chart 42 --repo ../brand --url https://bi.example.com
    franops-bi roles apply                    (in process, inside the BI image)

Credentials come from flags or the environment:
    SUPERSET_URL, SUPERSET_API_KEY            (an API key of an Admin; under OAuth, the only way)
    SUPERSET_USERNAME, SUPERSET_PASSWORD      (the `db` login; OAuth off only)
    FRANOPS_URL, FRANOPS_BI_SOURCES_KEY       (deploy: the source keys; a data_bi:sources key)
    BI_SERVICE_PASSWORD                       (roles apply: the service account's password)
    FRANOPS_BI_DB_PASSWORDS   JSON map: {"databases/<file>.yaml": "<password>"}
The export masks each database password, so the first import into an empty instance needs it.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request
from typing import Sequence

from . import bundle as b
from .certify import TYPES, CertifyError, certify
from .client import Superset, SupersetError


def _db_passwords(args: argparse.Namespace) -> dict[str, str]:
    out: dict[str, str] = {}
    env = os.environ.get("FRANOPS_BI_DB_PASSWORDS")
    if env:
        out.update(json.loads(env))
    for item in args.db_password or []:
        path, sep, env_name = item.partition("=")
        if not sep:
            raise SystemExit(f"--db-password {item!r}: use databases/<file>.yaml=ENV_VAR")
        if env_name not in os.environ:
            raise SystemExit(f"--db-password {item!r}: ${env_name} is not set")
        out[path] = os.environ[env_name]
    return out


def connect(args: argparse.Namespace, command: str) -> Superset:
    """The session of deploy and certify: the API key when one is set, else the `db` login."""
    if not args.url:
        raise SystemExit(f"{command}: set --url or SUPERSET_URL")
    if args.api_key:
        return Superset(args.url, api_key=args.api_key)
    if not args.password:
        raise SystemExit(f"{command}: set SUPERSET_API_KEY, or --password or SUPERSET_PASSWORD")
    return Superset(args.url, args.username, args.password)


ROLES_PATH = "/api/v1/franops/roles/apply/"


def apply_roles_remote(client: Superset) -> int:
    """POST the roles endpoint of the image (S5.9). Prints its summary. Returns the exit code."""
    status, body = client.request("POST", ROLES_PATH, {}, expect=None)
    if status != 200 or not isinstance(body, dict):
        hint = " (is the image older than S5.9?)" if status == 404 else ""
        print(f"error: {ROLES_PATH} returned HTTP {status}{hint}: {json.dumps(body, default=str)}",
              file=sys.stderr)
        return 1
    from .roles import Result, RoleChange

    result = Result(
        roles=[RoleChange(n, r.get("added", []), r.get("removed", []))
               for n, r in (body.get("roles") or {}).items()],
        service_account=str(body.get("serviceAccount")),
        datasets=int(body.get("datasets", 0)),
        scorecard=int(body.get("scorecardDatasets", 0)),
        uncertified=[str(x) for x in body.get("uncertifiedEmbedded") or []],
    )
    for line in result.lines():
        print(line)
    return 0


def cmd_roles_apply(args: argparse.Namespace) -> int:
    try:
        from superset.app import create_app
    except ImportError:
        print("error: roles apply runs inside the BI image, in process "
              "(from outside, deploy calls the image's roles endpoint)", file=sys.stderr)
        return 1
    from . import roles

    app = create_app()
    with app.app_context():
        try:
            result = roles.apply(roles.service_password_from_env(args.service_password_env))
        except roles.RolesError as err:
            print(f"error: {err}", file=sys.stderr)
            return 1
    for line in result.lines():
        print(line)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        bundle = b.load(args.dir)
    except b.BundleError as err:
        for problem in err.problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1
    counts = bundle.counts()
    print(
        f"ok: {len(bundle.documents)} YAML files; "
        + ", ".join(f"{n} {t}" for t, n in counts.items())
    )
    return 0


# The object types of Superset's tag API (ObjectType), and the list resource of each.
OBJECT_TYPES = {"charts": ("chart", 2), "dashboards": ("dashboard", 3), "datasets": ("dataset", 4)}


def ids_by_uuid(client: Superset, asset_type: str, bundle: b.Bundle) -> list[int]:
    """The ids, in the BI instance, of the assets of one type of the bundle."""
    wanted = {str(d["uuid"]) for d in bundle.assets(asset_type).values()}
    resource = OBJECT_TYPES[asset_type][0]
    return [
        row["id"]
        for row in client.list_all(resource, ["id", "uuid"])
        if str(row.get("uuid")) in wanted
    ]


def tag_datasets(client: Superset, bundle: b.Bundle) -> list[int]:
    """Tag each dataset of the bundle `certified` through the tag API. Returns the dataset ids.

    The dataset import refuses `tags`, so the tag cannot travel in the YAML. A dataset import
    does not touch tag rows, so the tag stays through the next overwrite import.
    ⚠️ A deploy never removes a dataset tag: a dataset that leaves bi/ keeps `certified`.
    """
    ids = ids_by_uuid(client, "datasets", bundle)
    for dataset_id in ids:
        client.request(
            "POST",
            f"/api/v1/tag/{OBJECT_TYPES['datasets'][1]}/{dataset_id}/",
            {"properties": {"tags": [b.CERTIFIED]}},
            expect=(200, 201),
        )
    return ids


def untagged(client: Superset, bundle: b.Bundle, types: Sequence[str]) -> list[str]:
    """Each asset of `types` in the bundle that the BI instance does not tag `certified`.

    Reads the tag API back, so a deploy against an instance with TAGGING_SYSTEM off fails
    instead of printing success with no tag written.
    ⚠️ Charts and dashboards only: on 7.0.0rc2 `get_objects` never lists a dataset, and no
    dataset endpoint returns its tags. For a dataset, the check is the 201 of the tag call.
    """
    tagged = {(str(o.get("type")), int(o.get("id"))) for o in client.tagged_objects(b.CERTIFIED)}
    missing: list[str] = []
    for asset_type in types:
        name = OBJECT_TYPES[asset_type][0]
        expected = ids_by_uuid(client, asset_type, bundle)
        if len(expected) != len(bundle.assets(asset_type)):
            missing.append(f"{asset_type}: {len(expected)} of {len(bundle.assets(asset_type))} found")
        missing += [f"{asset_type} id={i}" for i in expected if (name, int(i)) not in tagged]
    return missing


SOURCES_PATH = "/api/data/bi/sources"


def default_sources_dir(brand_dir: str) -> pathlib.Path:
    """`dbt_packages/franops_sources/bi` beside the brand's `bi/`, after `dbt deps` (S4.11)."""
    return pathlib.Path(brand_dir).resolve().parent / "dbt_packages" / "franops_sources" / "bi"


def read_source_keys(franops_url: str, api_key: str) -> list[str]:
    """GET the organization's source keys from FranOps with a `data_bi:sources` key (S4.11)."""
    req = urllib.request.Request(
        franops_url.rstrip("/") + SOURCES_PATH,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raise SystemExit(f"deploy: FranOps {SOURCES_PATH} returned HTTP {err.code}") from err
    except (urllib.error.URLError, ValueError) as err:
        raise SystemExit(f"deploy: FranOps {SOURCES_PATH} did not answer: {err}") from err
    keys = body.get("sourceKeys") if isinstance(body, dict) else None
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        raise SystemExit(f"deploy: FranOps {SOURCES_PATH} returned no sourceKeys list")
    return keys


def source_bundles(args: argparse.Namespace, brand: b.Bundle) -> list[tuple[str, b.Bundle]]:
    """The import of each `bi/<sourceKey>/` of `franops_sources`, merged with the brand's
    warehouse database, in the order of the keys. A key with no folder imports nothing."""
    if args.no_sources:
        print("sources: skipped (--no-sources)")
        return []
    if not args.franops_url or not args.franops_api_key:
        raise SystemExit(
            "deploy: set --franops-url (FRANOPS_URL) and FRANOPS_BI_SOURCES_KEY, a FranOps API "
            "key with data_bi:sources, or pass --no-sources"
        )
    keys = read_source_keys(args.franops_url, args.franops_api_key)
    print("sources: " + (", ".join(keys) if keys else "none configured"))
    if not keys:
        return []
    root = pathlib.Path(args.sources_dir) if args.sources_dir else default_sources_dir(args.dir)
    if not root.is_dir():
        raise SystemExit(
            f"deploy: {root} does not exist; run `dbt deps` in the brand repository, "
            "or pass --sources-dir"
        )
    out = []
    for key in keys:
        folder = root / key
        if not folder.is_dir():
            print(f"sources: {key} has no bi/ folder in franops_sources; nothing to import")
            continue
        out.append((key, b.merge(b.load(folder, source=True), brand)))
    return out


def import_bundle(client: Superset, args: argparse.Namespace, label: str, bundle: b.Bundle) -> int:
    """Import one bundle, tag its datasets, and read the tags back. Returns the exit code."""
    blob = b.to_zip(b.rewrite(bundle))
    if args.zip_out:
        out = args.zip_out if label == "brand" else f"{args.zip_out}.{label}"
        with open(out, "wb") as fh:
            fh.write(blob)
    status, body = client.import_assets(blob, passwords=_db_passwords(args), overwrite=True)
    if status != 200:
        print(f"error: {label}: import returned HTTP {status}: {json.dumps(body, default=str)}",
              file=sys.stderr)
        return 1
    counts = bundle.counts()
    print(f"imported {label}: " + ", ".join(f"{n} {t}" for t, n in counts.items()))
    types = list(b.TAGGED_TYPES)
    if args.tag_datasets:
        ids = tag_datasets(client, bundle)
        print(f"tagged {len(ids)} datasets {b.CERTIFIED} through the tag API")
        if len(ids) != len(bundle.assets("datasets")):
            print(f"error: {label}: {len(ids)} of {len(bundle.assets('datasets'))} datasets found "
                  "after the import", file=sys.stderr)
            return 1
    missing = untagged(client, bundle, types)
    if missing:
        print(f"error: {label}: not tagged {b.CERTIFIED}: " + "; ".join(missing)
              + " (is TAGGING_SYSTEM on?)", file=sys.stderr)
        return 1
    print(f"checked {label}: each {', '.join(types)} asset is tagged {b.CERTIFIED}")
    return 0


def cmd_deploy(args: argparse.Namespace) -> int:
    """S4.11: import `bi/<sourceKey>/` of `franops_sources` for each source key of the
    organization, then the brand `bi/` (S5.5), then set the roles (S5.9)."""
    try:
        brand = b.load(args.dir)
        sources = source_bundles(args, brand)
    except b.BundleError as err:
        for problem in err.problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1
    client = connect(args, "deploy")
    for key, bundle in sources:
        if import_bundle(client, args, key, bundle):
            return 1
    if import_bundle(client, args, "brand", brand):
        return 1
    if args.apply_roles:
        return apply_roles_remote(client)
    return 0


def cmd_certify(args: argparse.Namespace) -> int:
    client = connect(args, "certify")
    try:
        result = certify(client, args.type, args.id, args.repo)
    except CertifyError as err:
        print(f"error: {err}", file=sys.stderr)
        if not err.changed_bi:
            print("BI and the repository did not change", file=sys.stderr)
        return 1
    done = "swapped draft for certified" if result.swapped else "was certified already"
    print(f"{args.type} {args.id}: {done}; wrote {result.path} on the branch {result.branch}")
    print("next: push the branch and open the pull request")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="franops-bi", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate", help="parse each YAML of a bi/ folder; exit 1 on a defect")
    v.add_argument("--dir", required=True)
    v.set_defaults(func=cmd_validate)

    d = sub.add_parser("deploy", help="import the certified assets of a bi/ folder")
    d.add_argument("--dir", required=True)
    _credentials(d)
    d.add_argument(
        "--db-password",
        action="append",
        metavar="databases/<file>.yaml=ENV_VAR",
        help="the password of one database, read from an environment variable; repeatable",
    )
    d.add_argument(
        "--tag-datasets",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="tag each dataset `certified` through the tag API, because the import cannot "
        "(default: on)",
    )
    d.add_argument(
        "--apply-roles",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="after the import, set the BI roles through the image's roles endpoint (S5.9) "
        "(default: on)",
    )
    d.add_argument("--zip-out", help="also write the rewritten ZIP to this path")
    d.add_argument(
        "--franops-url",
        default=os.environ.get("FRANOPS_URL"),
        help="FranOps, which lists the organization's source keys (S4.11)",
    )
    d.add_argument(
        "--franops-api-key",
        default=os.environ.get("FRANOPS_BI_SOURCES_KEY"),
        help="a FranOps API key whose only scope is data_bi:sources; prefer "
        "FRANOPS_BI_SOURCES_KEY, so the key stays out of the process list",
    )
    d.add_argument(
        "--sources-dir",
        help="the bi/ folder of franops_sources (default: dbt_packages/franops_sources/bi "
        "beside --dir, after `dbt deps`)",
    )
    d.add_argument(
        "--no-sources",
        action="store_true",
        help="import the brand bi/ only, and do not ask FranOps for the source keys",
    )
    d.set_defaults(func=cmd_deploy)

    c = sub.add_parser(
        "certify",
        help="as a reviewer, swap draft for certified and export the asset into bi/ on a branch",
    )
    c.add_argument("type", choices=sorted(TYPES))
    c.add_argument("id", type=int)
    c.add_argument("--repo", required=True, help="the brand repository; its bi/ folder gets the YAML")
    _credentials(c)
    c.set_defaults(func=cmd_certify)

    r = sub.add_parser("roles", help="the BI roles and the service account (S5.9)")
    rsub = r.add_subparsers(dest="roles_command", required=True)
    ra = rsub.add_parser(
        "apply", help="in process, inside the BI image: set HQ, Embed, GuestIssuer exactly"
    )
    ra.add_argument(
        "--service-password-env",
        default="BI_SERVICE_PASSWORD",
        metavar="ENV_VAR",
        help="the environment variable that holds the service account's password "
        "(default: BI_SERVICE_PASSWORD)",
    )
    ra.set_defaults(func=cmd_roles_apply)
    return p


def _credentials(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", default=os.environ.get("SUPERSET_URL"))
    parser.add_argument(
        "--api-key",
        default=os.environ.get("SUPERSET_API_KEY"),
        help="an API key (sst_...) of an Admin; prefer SUPERSET_API_KEY, so the key stays out "
        "of the process list",
    )
    parser.add_argument("--username", default=os.environ.get("SUPERSET_USERNAME", "admin"))
    parser.add_argument("--password", default=os.environ.get("SUPERSET_PASSWORD"))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SupersetError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
