"""The role plan of S5.9 and the credentials of S5.10, without a BI instance."""

from __future__ import annotations

import pytest

from franops_bi import cli
from franops_bi.client import Superset
from franops_bi.roles import (BASE_GRANTS, FORBIDDEN, Result, RoleChange, RolesError, desired, diff,
                               role_names, split_embedded)

A, B = "[warehouse].[a](id:1)", "[warehouse].[b](id:2)"


def test_each_role_gets_exactly_the_design_table() -> None:
    wanted = desired([A, B], [A])
    assert wanted["HQ"] == {
        ("can_read", "Chart"), ("can_write", "Chart"), ("can_read", "Dashboard"),
        ("can_write", "Dashboard"), ("can_read", "Tag"), ("can_list", "Tags"),
    } | {("datasource_access", A), ("datasource_access", B)}
    assert wanted["Embed"] == {("can_read", "Dashboard"), ("can_read", "Chart"), ("datasource_access", A)}
    assert wanted["GuestIssuer"] == {("can_grant_guest_token", "SecurityRestApi"), ("datasource_access", A)}


def test_no_embedded_dashboard_gives_the_guest_roles_no_dataset() -> None:
    wanted = desired([A, B], [])
    assert wanted["Embed"] == set(BASE_GRANTS["Embed"])
    assert wanted["GuestIssuer"] == set(BASE_GRANTS["GuestIssuer"])
    assert ("datasource_access", A) in wanted["HQ"]


def test_no_certified_dataset_gives_the_fixed_grants_only() -> None:
    assert desired([], []) == {k: set(v) for k, v in BASE_GRANTS.items()}


def test_a_scorecard_dataset_that_is_not_certified_is_refused() -> None:
    with pytest.raises(RolesError, match="not certified"):
        desired([A], [B])


def test_split_embedded_keeps_certified_and_names_the_rest() -> None:
    assert split_embedded([1, 2], [2, 3]) == ({2}, {3})
    assert split_embedded([1], []) == (set(), set())


def test_the_result_names_an_uncertified_embedded_dataset() -> None:
    result = Result([RoleChange("Embed")], "unchanged", datasets=2, scorecard=1, uncertified=["raw (id 3)"])
    assert result.lines()[0] == "roles: 2 certified datasets, 1 on embedded dashboards"
    assert "raw (id 3)" in result.lines()[1]
    assert result.as_dict()["uncertifiedEmbedded"] == ["raw (id 3)"]


def test_no_role_holds_a_forbidden_permission() -> None:
    for grants in desired(["[w].[t](id:9)"], ["[w].[t](id:9)"]).values():
        assert not {p for p, _ in grants} & FORBIDDEN


def test_diff_names_what_changes_and_an_equal_set_is_unchanged() -> None:
    change = diff("HQ", {("can_read", "Chart"), ("can_sqllab", "Superset")}, {("can_read", "Chart"), ("can_read", "Tag")})
    assert change.added == ["can_read on Tag"]
    assert change.removed == ["can_sqllab on Superset"]
    assert diff("HQ", {("can_read", "Chart")}, {("can_read", "Chart")}).changed is False


def test_role_names_follow_the_image_configuration() -> None:
    assert role_names({}) == {"HQ": "HQ", "Embed": "Embed", "GuestIssuer": "GuestIssuer"}
    names = role_names({"FRANOPS_BI_ROLES": {"HQ": "Head"}, "GUEST_ROLE_NAME": "G", "GUEST_ISSUER_ROLE": "I"})
    assert names == {"HQ": "Head", "Embed": "G", "GuestIssuer": "I"}


class _Recorder(Superset):
    """Records each request instead of sending it; answers the CSRF call."""

    def __init__(self, *args, **kwargs):
        self.sent: list[tuple[str, str, str | None]] = []
        super().__init__(*args, **kwargs)

    def request(self, method, path, body=None, **kwargs):
        self.sent.append((method, path, self._token))
        if path == "/api/v1/security/csrf_token/":
            return 200, {"result": "csrf"}
        if path == "/api/v1/security/login":
            return 200, {"access_token": "jwt"}
        return 200, {}


def test_an_api_key_is_the_bearer_and_nothing_logs_in() -> None:
    client = _Recorder("https://bi.example", api_key="sst_abc")
    assert [p for _, p, _ in client.sent] == ["/api/v1/security/csrf_token/"]
    assert client._token == "sst_abc"


def test_without_a_key_the_db_login_runs() -> None:
    client = _Recorder("https://bi.example", "admin", "pw")
    assert [p for _, p, _ in client.sent] == ["/api/v1/security/login", "/api/v1/security/csrf_token/"]
    assert client._token == "jwt"


def test_no_key_and_no_password_is_refused() -> None:
    with pytest.raises(ValueError):
        Superset("https://bi.example", "admin", None)
    args = cli.build_parser().parse_args(["deploy", "--dir", "x", "--url", "https://bi.example"])
    args.api_key, args.password = None, None
    with pytest.raises(SystemExit, match="SUPERSET_API_KEY"):
        cli.connect(args, "deploy")


def test_the_cli_reads_the_key_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("SUPERSET_API_KEY", "sst_env")
    args = cli.build_parser().parse_args(["certify", "chart", "1", "--repo", "r", "--url", "u"])
    assert args.api_key == "sst_env"


def test_roles_apply_outside_the_image_says_where_it_runs(capsys) -> None:
    try:
        import superset  # noqa: F401
    except ImportError:
        assert cli.main(["roles", "apply"]) == 1
        assert "inside the BI image" in capsys.readouterr().err
    else:  # pragma: no cover - only inside the image
        pytest.skip("Superset is installed")


def test_deploy_reports_a_missing_roles_endpoint(capsys) -> None:
    class Old:
        def request(self, method, path, body=None, **kwargs):
            assert (method, path) == ("POST", cli.ROLES_PATH)
            return 404, {"message": "Not found"}

    assert cli.apply_roles_remote(Old()) == 1
    assert "older than S5.9" in capsys.readouterr().err
