from __future__ import annotations

import json

import pytest

from panier import managed_browser_shim as shim


def _capture(monkeypatch, status: int, payload: object):
    calls: list[tuple[str, str, dict, str]] = []

    def fake_request(base_url, route, payload_body, *, method="POST"):
        calls.append((base_url, route, payload_body, method))
        return status, payload

    monkeypatch.setattr(shim, "_request", fake_request)
    return calls


def test_parse_args_extracts_subcommand_options_profile_site() -> None:
    command, options, profile, site = shim.parse_args(
        [
            "console",
            "eval",
            "--expression",
            "1+1",
            "--tab-id",
            "t1",
            "--profile",
            "courses",
            "--site",
            "leclerc",
            "--json",
        ]
    )

    assert command == ("console", "eval")
    assert options["expression"] == "1+1"
    assert options["tab_id"] == "t1"
    assert options["--json"] is True
    assert profile == "courses"
    assert site == "leclerc"


def test_parse_args_flow_run_collects_params_and_name() -> None:
    command, options, _, _ = shim.parse_args(
        [
            "flow",
            "run",
            "add-cart-leclerc",
            "--param",
            "itemsB64=abc",
            "--param",
            "dryRunB64=dHJ1ZQ==",
            "--max-side-effect-level",
            "read_only",
            "--profile",
            "courses",
            "--site",
            "leclerc",
            "--json",
        ]
    )

    assert command == ("flow", "run")
    assert options["_flow"] == "add-cart-leclerc"
    assert options["_params"] == {"itemsB64": "abc", "dryRunB64": "dHJ1ZQ=="}
    assert options["_max_side_effect_level"] == "read_only"


@pytest.mark.parametrize(
    "argv,route,method,body_key",
    [
        (["navigate", "--url", "https://x"], "/managed/cli/open", "POST", "url"),
        (["console", "eval", "--expression", "1"], "/console/eval", "POST", "expression"),
        (["snapshot"], "/managed/cli/snapshot", "POST", None),
        (
            ["storage", "checkpoint", "--reason", "avant"],
            "/storage/checkpoint",
            "POST",
            "reason",
        ),
        (["flow", "run", "mon-flow"], "/flow/run", "POST", None),
    ],
)
def test_build_payload_routes(monkeypatch, argv, route, method, body_key) -> None:
    calls = _capture(monkeypatch, 200, {"ok": True, "result": {}})
    code = shim.main([*argv, "--profile", "courses-auchan", "--site", "auchan", "--json"])

    assert code == 0
    base, called_route, payload, called_method = calls[0]
    assert called_route == route
    assert called_method == method
    assert payload["profile"] == "courses-auchan"
    assert payload["site"] == "auchan"
    if body_key:
        assert body_key in payload


def test_profile_status_uses_get(monkeypatch) -> None:
    calls = _capture(monkeypatch, 200, {"ok": True})

    code = shim.main(
        ["profile", "status", "--profile", "courses", "--site", "leclerc", "--json"]
    )

    assert code == 0
    _, route, _, method = calls[0]
    assert route == "/managed/profiles/courses/status"
    assert method == "GET"


def test_http_409_tab_exists_is_treated_as_success(monkeypatch, capsys) -> None:
    _capture(
        monkeypatch,
        409,
        {"detail": {"success": False, "tabId": "t42", "reason": "Tab already exists"}},
    )

    code = shim.main(
        ["navigate", "--url", "https://x", "--profile", "courses", "--site", "leclerc"]
    )

    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["detail"]["tabId"] == "t42"


def test_http_error_prints_detail_and_exits_nonzero(monkeypatch, capsys) -> None:
    _capture(monkeypatch, 404, {"detail": {"error": "profile_unknown"}})

    code = shim.main(
        ["navigate", "--url", "https://x", "--profile", "ghost", "--site", "auchan"]
    )

    assert code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["detail"]["error"] == "profile_unknown"


def test_unknown_subcommand_fails_fast() -> None:
    with pytest.raises(SystemExit):
        shim.main(["teleport", "--profile", "courses", "--site", "leclerc"])


def test_default_transport_is_sdk() -> None:
    """Plan P4.19 : le défaut est le SDK in-process, plus le shim subprocess."""
    from panier.managed_browser import ManagedBrowserClient

    client = ManagedBrowserClient(runner=lambda args, *, input_text=None: None)  # type: ignore[arg-type,return-value]

    assert client.command is None
    assert client._sdk is not None


def test_env_command_still_targets_the_shim(monkeypatch) -> None:
    """Échappatoire explicite conservée : PANIER_MANAGED_BROWSER_COMMAND."""
    from panier.managed_browser import ManagedBrowserClient

    monkeypatch.setenv("PANIER_MANAGED_BROWSER_COMMAND", "panier-managed-browser")
    client = ManagedBrowserClient(runner=lambda args, *, input_text=None: None)  # type: ignore[arg-type,return-value]

    assert client.command == "panier-managed-browser"
    assert client._sdk is None
