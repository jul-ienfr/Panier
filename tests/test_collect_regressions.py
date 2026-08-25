from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

import panier.cli as cli
from panier.cart import CartLine
from panier.cli import app, cart_flow_value
from panier.managed_browser import ManagedBrowserError
from panier.models import StoreOffer


def _write_single_recipe(tmp_path: Path) -> None:
    (tmp_path / "recipes.yaml").write_text(
        """
- name: Riz rapide
  tags: [budget]
  ingredients:
    - name: riz
      quantity: 100
      unit: g
""".strip(),
        encoding="utf-8",
    )


def test_plan_collect_all_drives_failed_reports_unavailable_instead_of_traceback(
    tmp_path: Path, monkeypatch
) -> None:
    _write_single_recipe(tmp_path)

    def fake_collect(items, drive, browser, products=None, max_results=5):
        raise ManagedBrowserError("Internal server error — navigate — courses")

    monkeypatch.setattr(cli, "collect_drive_offers", fake_collect)
    output = tmp_path / "offers.yaml"

    result = CliRunner().invoke(
        app,
        [
            "plan",
            "--data-dir",
            str(tmp_path),
            "--meals",
            "1",
            "--collect",
            "leclerc,auchan",
            "--collect-output",
            str(output),
            "--no-pantry",
        ],
    )

    assert result.exit_code == 0
    assert "Avertissement Managed Browser leclerc:" in result.output
    assert "Avertissement Managed Browser auchan:" in result.output
    assert "Recommandation indisponible: aucune offre collectée" in result.output
    assert "Traceback" not in result.output


def test_plan_collect_partial_failure_persists_offers_and_keeps_warning(
    tmp_path: Path, monkeypatch
) -> None:
    _write_single_recipe(tmp_path)

    def fake_collect(items, drive, browser, products=None, max_results=5):
        if drive == "auchan":
            raise ManagedBrowserError("Internal server error — navigate — courses-auchan")
        return [StoreOffer(store=drive, item="riz", product="Riz Leclerc", price=2.0)]

    monkeypatch.setattr(cli, "collect_drive_offers", fake_collect)
    output = tmp_path / "offers.yaml"

    result = CliRunner().invoke(
        app,
        [
            "plan",
            "--data-dir",
            str(tmp_path),
            "--meals",
            "1",
            "--collect",
            "leclerc,auchan",
            "--collect-output",
            str(output),
            "--no-pantry",
        ],
    )

    assert result.exit_code == 0
    assert "Avertissement Managed Browser auchan:" in result.output
    assert output.exists()
    assert "Riz Leclerc" in output.read_text(encoding="utf-8")


def test_plan_collect_recommendation_error_is_reported_not_raised(
    tmp_path: Path, monkeypatch
) -> None:
    """Un store collecte zéro offre exploitable -> ValueError du planner -> message clair."""
    _write_single_recipe(tmp_path)

    def fake_collect(items, drive, browser, products=None, max_results=5):
        return []

    monkeypatch.setattr(cli, "collect_drive_offers", fake_collect)

    result = CliRunner().invoke(
        app,
        [
            "plan",
            "--data-dir",
            str(tmp_path),
            "--meals",
            "1",
            "--collect",
            "leclerc,auchan",
            "--no-pantry",
        ],
    )

    assert result.exit_code == 0
    assert "Recommandation indisponible:" in result.output
    assert "Traceback" not in result.output


def test_cart_flow_value_unwraps_nested_console_eval_payload() -> None:
    class FakeResult:
        data = {
            "result": {
                "results": [
                    {"step": "navigate"},
                    {
                        "step": "console_eval",
                        "result": {
                            "value": {
                                "catalog_found": ["a"],
                                "addable": ["b"],
                                "inserted": ["c"],
                                "message": "ok",
                            }
                        },
                    },
                ]
            }
        }

    value = cart_flow_value(FakeResult())  # type: ignore[arg-type]

    assert value["message"] == "ok"
    assert len(value["inserted"]) == 1


def test_cart_flow_value_returns_flat_payload_unchanged() -> None:
    payload = {"ok": True, "store": "leclerc", "inserted": []}

    assert cart_flow_value(_Data(payload)) == payload  # type: ignore[arg-type]


class _Data:
    def __init__(self, data: dict) -> None:
        self.data = data


def test_run_cart_flow_live_unwraps_value_wrapper_and_falls_back_to_raw(
    monkeypatch,
) -> None:
    """Le piège documenté : console_eval renvoie {result:{value:...}}; sans wrapper aussi."""
    payloads = iter(
        [
            {"result": {"value": {"catalog_found": False, "addable": False, "inserted": False}}},
            {"catalog_found": True, "addable": True, "inserted": True, "url": "https://l/riz"},
        ]
    )

    def fake_runner(
        args: list[str], *, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        if "storage" in args and "checkpoint" in args:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="{}")
        if "navigate" in args:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="{}")
        if "console" in args:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=json.dumps(next(payloads), ensure_ascii=False),
            )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="{}")

    def fake_init(self, **kwargs):
        self.command = kwargs.get("command")
        self.profile = kwargs.get("profile", "courses")
        self.site = kwargs.get("site", "leclerc")
        self.runner = fake_runner

    monkeypatch.setattr(cli.ManagedBrowserClient, "__init__", fake_init)
    result = cli.run_cart_flow_for_store(
        "leclerc",
        [
            CartLine(
                store="leclerc",
                item="riz",
                product="Riz Leclerc",
                url="https://l/blocked",
                search_url="https://l/riz",
            )
        ],
        profile="courses",
        browser_command="managed-browser",
        dry_run=False,
    )

    assert len(result.data["inserted"]) == 1
