"""Read, check, rewrite, and zip a `bi/` folder in Superset's export format.

A `bi/` folder is the exploded ZIP of `GET /api/v1/assets/export/`, without its root folder:

    bi/metadata.yaml               version, type: assets
    bi/tags.yaml                   optional; the tag descriptions
    bi/databases/<name>.yaml
    bi/datasets/<database>/<name>.yaml
    bi/charts/<name>.yaml
    bi/dashboards/<name>.yaml

`rewrite` marks each asset as certified before the import. The source folder never changes.
"""

from __future__ import annotations

import io
import pathlib
import zipfile
from dataclasses import dataclass, field
from typing import Any

import yaml

ASSET_TYPES = ("databases", "datasets", "charts", "dashboards")

# Superset 7.0.0rc2 writes the `tags` list of an import only for charts and dashboards
# (commands/importers/v1/assets.py calls import_tag for those two). The dataset and database
# import schemas have no `tags` field, and they refuse an unknown field.
TAGGED_TYPES = ("charts", "dashboards")

# Each asset import schema accepts `is_managed_externally`.
MANAGED_TYPES = ASSET_TYPES

CERTIFIED = "certified"

# The UUID of the warehouse database of every brand (S4.11, Jeremy 2026-11-27). Each brand's
# `bi/databases/warehouse.yaml` carries it, and each dataset in `bi/<sourceKey>/` of
# `franops_sources` names it as `database_uuid`. A source folder holds no database, because the
# connection string is the brand's. An export from any brand BI then names the right database.
WAREHOUSE_UUID = "ef85f59c-c056-4cb4-b44f-94b720da9310"

# The keys that each asset type needs, from Superset's import schemas (required=True).
REQUIRED_KEYS = {
    "databases": ("database_name", "sqlalchemy_uri", "uuid", "version"),
    "datasets": ("table_name", "uuid", "database_uuid", "version"),
    "charts": ("slice_name", "viz_type", "uuid", "dataset_uuid", "version"),
    "dashboards": ("dashboard_title", "uuid", "version"),
}


@dataclass
class Bundle:
    root: pathlib.Path
    # Relative POSIX path -> parsed YAML document, for each YAML file of the folder.
    documents: dict[str, Any] = field(default_factory=dict)
    # Relative path -> file text, for a non-asset file that is not under `root` (`merge`).
    texts: dict[str, str] = field(default_factory=dict)

    def assets(self, asset_type: str) -> dict[str, dict[str, Any]]:
        prefix = asset_type + "/"
        return {p: d for p, d in self.documents.items() if p.startswith(prefix)}

    def counts(self) -> dict[str, int]:
        return {t: len(self.assets(t)) for t in ASSET_TYPES}


class BundleError(ValueError):
    """The folder is not a valid Superset assets export. `problems` lists each defect."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(problems))


def _asset_type(rel: str) -> str | None:
    top = rel.split("/", 1)[0]
    return top if top in ASSET_TYPES and "/" in rel else None


def load(root: str | pathlib.Path, *, source: bool = False) -> Bundle:
    """Parse each YAML file under `root`. Raise BundleError with every problem found.

    `source=True` reads `bi/<sourceKey>/` of `franops_sources` (S4.11): it holds no
    metadata.yaml and no database, and each dataset names WAREHOUSE_UUID. `merge` then joins
    it to the brand's databases for the import.
    """
    root = pathlib.Path(root)
    problems: list[str] = []
    bundle = Bundle(root=root)
    if not root.is_dir():
        raise BundleError([f"{root}: not a directory"])
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        # The same filter as Superset's is_valid_config: the importer reads no other file.
        if path.name.startswith((".", "_")) or path.suffix.lower() not in (".yaml", ".yml"):
            continue
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError) as err:
            problems.append(f"{rel}: does not parse: {err}")
            continue
        bundle.documents[rel] = doc

    if source:
        if bundle.assets("databases"):
            problems.append(
                "databases/: a source folder holds no database; its datasets name "
                f"database_uuid {WAREHOUSE_UUID}, the brand's warehouse"
            )
    elif not (root / "metadata.yaml").is_file():
        problems.append("metadata.yaml: missing")
    elif "metadata.yaml" in bundle.documents:  # absent here when it did not parse
        meta = bundle.documents["metadata.yaml"]
        if not isinstance(meta, dict) or meta.get("type") != "assets" or not meta.get("version"):
            problems.append("metadata.yaml: needs `version` and `type: assets`")
    if not any(_asset_type(p) for p in bundle.documents):
        problems.append("no asset YAML under databases/, datasets/, charts/, or dashboards/")

    uuids: dict[str, str] = {}
    by_type: dict[str, set[str]] = {t: set() for t in ASSET_TYPES}
    for rel, doc in bundle.documents.items():
        asset_type = _asset_type(rel)
        if asset_type is None:
            continue
        if not isinstance(doc, dict):
            problems.append(f"{rel}: is not a mapping")
            continue
        missing = [k for k in REQUIRED_KEYS[asset_type] if doc.get(k) in (None, "")]
        if missing:
            problems.append(f"{rel}: missing {', '.join(missing)}")
        uid = str(doc.get("uuid", ""))
        if uid:
            if uid in uuids:
                problems.append(f"{rel}: uuid {uid} is also in {uuids[uid]}")
            uuids[uid] = rel
            by_type[asset_type].add(uid)
        if "tags" in doc and asset_type not in TAGGED_TYPES:
            problems.append(
                f"{rel}: has `tags`, which the {asset_type} import of Superset refuses"
            )

    # Each reference must resolve inside the folder: an import of a dangling one fails.
    for rel, doc in bundle.assets("datasets").items():
        if not isinstance(doc, dict):
            continue
        if source:
            if str(doc.get("database_uuid")) != WAREHOUSE_UUID:
                problems.append(
                    f"{rel}: database_uuid {doc.get('database_uuid')} is not the warehouse "
                    f"UUID {WAREHOUSE_UUID}"
                )
        elif str(doc.get("database_uuid")) not in by_type["databases"]:
            problems.append(f"{rel}: database_uuid {doc.get('database_uuid')} is not in databases/")
    for rel, doc in bundle.assets("charts").items():
        if isinstance(doc, dict) and str(doc.get("dataset_uuid")) not in by_type["datasets"]:
            problems.append(f"{rel}: dataset_uuid {doc.get('dataset_uuid')} is not in datasets/")
    for rel, doc in bundle.assets("dashboards").items():
        if not isinstance(doc, dict):
            continue
        for chart_uuid in _dashboard_chart_uuids(doc):
            if chart_uuid not in by_type["charts"]:
                problems.append(f"{rel}: chart {chart_uuid} is not in charts/")

    if problems:
        raise BundleError(problems)
    return bundle


def merge(source: Bundle, brand: Bundle) -> Bundle:
    """The source assets with the brand's metadata.yaml and databases, as one import (S4.11).

    Superset resolves a dataset's `database_uuid` inside the ZIP only, so the warehouse
    database travels with each source import. Raise BundleError when the brand's `bi/` has no
    database with WAREHOUSE_UUID, or when a source asset reuses a brand UUID.
    """
    problems: list[str] = []
    brand_dbs = brand.assets("databases")
    if not any(str(d.get("uuid")) == WAREHOUSE_UUID for d in brand_dbs.values()):
        problems.append(
            f"{brand.root}: no database with uuid {WAREHOUSE_UUID}; the brand's "
            "bi/databases/warehouse.yaml must carry it"
        )
    brand_uuids = {str(d.get("uuid")) for d in brand.documents.values() if isinstance(d, dict)}
    for rel, doc in source.documents.items():
        if _asset_type(rel) and isinstance(doc, dict) and str(doc.get("uuid")) in brand_uuids:
            problems.append(f"{source.root}/{rel}: uuid {doc.get('uuid')} is also in {brand.root}")
    if problems:
        raise BundleError(problems)
    merged = Bundle(root=source.root)
    merged.documents["metadata.yaml"] = brand.documents["metadata.yaml"]
    merged.documents.update(brand_dbs)
    merged.documents.update(
        {rel: doc for rel, doc in source.documents.items() if rel != "metadata.yaml"}
    )
    merged.texts["metadata.yaml"] = (brand.root / "metadata.yaml").read_text(encoding="utf-8")
    return merged


def _dashboard_chart_uuids(doc: dict[str, Any]) -> list[str]:
    position = doc.get("position") or {}
    out = []
    if isinstance(position, dict):
        for node in position.values():
            if isinstance(node, dict) and node.get("type") == "CHART":
                uid = (node.get("meta") or {}).get("uuid")
                if uid:
                    out.append(str(uid))
    return out


def certify(asset_type: str, doc: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of `doc` marked as a certified, externally managed asset.

    Charts and dashboards get `tags: [certified]`, which replaces any tag list they hold: an
    import sets the object's tags to exactly this list (spike T-3). Each asset gets
    `is_managed_externally: true`.
    """
    out = dict(doc)
    if asset_type in MANAGED_TYPES:
        out["is_managed_externally"] = True
    if asset_type in TAGGED_TYPES:
        out["tags"] = [CERTIFIED]
    return out


def rewrite(bundle: Bundle) -> dict[str, str]:
    """The text of each file after `certify`, keyed by relative path. Pure: no file writes."""
    out: dict[str, str] = {}
    for rel, doc in bundle.documents.items():
        asset_type = _asset_type(rel)
        if asset_type is not None:
            doc = certify(asset_type, doc)
            out[rel] = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
        else:
            # metadata.yaml and tags.yaml go through unchanged.
            text = bundle.texts.get(rel)
            out[rel] = text if text is not None else (bundle.root / rel).read_text(encoding="utf-8")
    return out


def to_zip(files: dict[str, str], root_name: str = "bi") -> bytes:
    """ZIP the files under one root folder; Superset's importer strips the first path part."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in sorted(files):
            z.writestr(f"{root_name}/{rel}", files[rel])
    return buf.getvalue()
