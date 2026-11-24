"""The read-back check of `deploy` (S5.5): a missing `certified` tag fails the deploy."""

from __future__ import annotations

import pathlib
from typing import Any

from franops_bi import bundle as b
from franops_bi.cli import untagged

FIXTURE = pathlib.Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "bi"


class FakeClient:
    """Lists each fixture asset as id 1, and returns `tagged` from the tag API."""

    def __init__(self, bundle: b.Bundle, tagged: list[dict[str, Any]]):
        self.uuids = {
            "chart": [d["uuid"] for d in bundle.assets("charts").values()],
            "dashboard": [d["uuid"] for d in bundle.assets("dashboards").values()],
            "dataset": [d["uuid"] for d in bundle.assets("datasets").values()],
        }
        self.tagged = tagged

    def list_all(self, resource: str, columns: list[str]) -> list[dict[str, Any]]:
        return [{"id": i + 1, "uuid": u} for i, u in enumerate(self.uuids[resource])]

    def tagged_objects(self, tag: str) -> list[dict[str, Any]]:
        assert tag == "certified"
        return self.tagged


ALL = [{"type": "chart", "id": 1}, {"type": "dashboard", "id": 1}, {"type": "dataset", "id": 1}]


def test_every_chart_and_dashboard_tagged_passes() -> None:
    bundle = b.load(FIXTURE)
    assert untagged(FakeClient(bundle, ALL), bundle, ["charts", "dashboards"]) == []


def test_an_untagged_dashboard_fails() -> None:
    bundle = b.load(FIXTURE)
    client = FakeClient(bundle, [ALL[0]])
    assert untagged(client, bundle, ["charts", "dashboards"]) == ["dashboards id=1"]


def test_no_tag_at_all_fails_each_type() -> None:
    # TAGGING_SYSTEM off: the import succeeds and writes no tag.
    bundle = b.load(FIXTURE)
    missing = untagged(FakeClient(bundle, []), bundle, ["charts", "dashboards"])
    assert missing == ["charts id=1", "dashboards id=1"]


def test_an_asset_missing_from_the_instance_fails() -> None:
    bundle = b.load(FIXTURE)
    client = FakeClient(bundle, ALL)
    client.uuids["chart"] = []
    assert untagged(client, bundle, ["charts"]) == ["charts: 0 of 1 found"]


def test_tagged_objects_sends_a_plain_query_argument_not_rison():
    """get_objects reads `?tags=`; with Rison it ignores the filter and lists each tagged object."""
    from franops_bi.client import Superset

    client = Superset.__new__(Superset)
    seen: list[str] = []
    client.request = lambda method, path, *a, **k: (seen.append(path), (200, {"result": []}))[1]
    assert client.tagged_objects("certified") == []
    assert seen == ["/api/v1/tag/get_objects/?tags=certified"]
