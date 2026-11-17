"""Compare two package manifests from package_manifest.py. Used by check-package-unmodified.sh.

Usage: python3 compare_manifests.py <base.json> <built.json> <base-image> <built-image>
Exit codes: 0 = unchanged, 1 = changed, 2 = the manifests are not usable.
"""
import json
import sys

LABELS = ("installed", "runtime")


def main():
    base_path, built_path, base_image, built_image = sys.argv[1:5]
    with open(base_path) as handle:
        base = json.load(handle)
    with open(built_path) as handle:
        built = json.load(handle)

    problems = []
    roots = []
    count = 0
    for label in LABELS:
        before_pkg = base.get("packages", {}).get(label)
        after_pkg = built.get("packages", {}).get(label)
        if not before_pkg or not after_pkg or not before_pkg["entries"] or not after_pkg["entries"]:
            print(f"check-package-unmodified: ERROR: empty {label} package manifest", file=sys.stderr)
            return 2
        roots.append(after_pkg["root"])
        count += len(after_pkg["entries"])
        if before_pkg["root"] != after_pkg["root"] or before_pkg["origin"] != after_pkg["origin"]:
            problems.append(
                f"{label}: package location changed: "
                f"{before_pkg['origin']} -> {after_pkg['origin']}"
            )
            continue
        before_entries = before_pkg["entries"]
        after_entries = after_pkg["entries"]
        for rel in sorted(set(before_entries) | set(after_entries)):
            before = before_entries.get(rel)
            after = after_entries.get(rel)
            if before == after:
                continue
            if before is None:
                problems.append(f"{label}: added    {rel}")
            elif after is None:
                problems.append(f"{label}: removed  {rel}")
            else:
                problems.append(f"{label}: changed  {rel}")

    where = " and ".join(sorted(set(roots)))
    if problems:
        print(
            f"check-package-unmodified: FAIL: {built_image} writes into the superset "
            f"package at {where} ({len(problems)} change(s) against {base_image}):"
        )
        for line in problems[:200]:
            print(f"  {line}")
        if len(problems) > 200:
            print(f"  ... and {len(problems) - 200} more")
        return 1

    print(
        f"check-package-unmodified: OK: {built_image} leaves the superset package at "
        f"{where} unchanged ({count} entries compared with {base_image})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
