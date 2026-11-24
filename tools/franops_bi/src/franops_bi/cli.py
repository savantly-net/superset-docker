"""franops-bi: deploy and check the certified assets of a brand repository's `bi/` folder.

    franops-bi validate --dir bi/
    franops-bi deploy   --dir bi/ --url https://bi.example.com
    franops-bi certify  chart 42 --repo ../brand --url https://bi.example.com

Credentials come from flags or the environment:
    SUPERSET_URL, SUPERSET_USERNAME, SUPERSET_PASSWORD
    FRANOPS_BI_DB_PASSWORDS   JSON map: {"databases/<file>.yaml": "<password>"}
The export masks each database password, so the first import into an empty instance needs it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
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


def cmd_deploy(args: argparse.Namespace) -> int:
    try:
        bundle = b.load(args.dir)
    except b.BundleError as err:
        for problem in err.problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1
    if not args.url:
        raise SystemExit("deploy: set --url or SUPERSET_URL")
    if not args.password:
        raise SystemExit("deploy: set --password or SUPERSET_PASSWORD")
    blob = b.to_zip(b.rewrite(bundle))
    if args.zip_out:
        with open(args.zip_out, "wb") as fh:
            fh.write(blob)
    client = Superset(args.url, args.username, args.password)
    status, body = client.import_assets(blob, passwords=_db_passwords(args), overwrite=True)
    if status != 200:
        print(f"error: import returned HTTP {status}: {json.dumps(body, default=str)}",
              file=sys.stderr)
        return 1
    counts = bundle.counts()
    print("imported: " + ", ".join(f"{n} {t}" for t, n in counts.items()))
    types = list(b.TAGGED_TYPES)
    if args.tag_datasets:
        ids = tag_datasets(client, bundle)
        print(f"tagged {len(ids)} datasets {b.CERTIFIED} through the tag API")
        if len(ids) != len(bundle.assets("datasets")):
            print(f"error: {len(ids)} of {len(bundle.assets('datasets'))} datasets found after "
                  "the import", file=sys.stderr)
            return 1
    missing = untagged(client, bundle, types)
    if missing:
        print(f"error: not tagged {b.CERTIFIED}: " + "; ".join(missing)
              + " (is TAGGING_SYSTEM on?)", file=sys.stderr)
        return 1
    print(f"checked: each {', '.join(types)} asset is tagged {b.CERTIFIED}")
    return 0


def cmd_certify(args: argparse.Namespace) -> int:
    if not args.url:
        raise SystemExit("certify: set --url or SUPERSET_URL")
    if not args.password:
        raise SystemExit("certify: set --password or SUPERSET_PASSWORD")
    client = Superset(args.url, args.username, args.password)
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
    d.add_argument("--url", default=os.environ.get("SUPERSET_URL"))
    d.add_argument("--username", default=os.environ.get("SUPERSET_USERNAME", "admin"))
    d.add_argument("--password", default=os.environ.get("SUPERSET_PASSWORD"))
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
    d.add_argument("--zip-out", help="also write the rewritten ZIP to this path")
    d.set_defaults(func=cmd_deploy)

    c = sub.add_parser(
        "certify",
        help="as a reviewer, swap draft for certified and export the asset into bi/ on a branch",
    )
    c.add_argument("type", choices=sorted(TYPES))
    c.add_argument("id", type=int)
    c.add_argument("--repo", required=True, help="the brand repository; its bi/ folder gets the YAML")
    c.add_argument("--url", default=os.environ.get("SUPERSET_URL"))
    c.add_argument("--username", default=os.environ.get("SUPERSET_USERNAME", "admin"))
    c.add_argument("--password", default=os.environ.get("SUPERSET_PASSWORD"))
    c.set_defaults(func=cmd_certify)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SupersetError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
