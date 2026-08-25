from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

import panier.cli as cli
from panier.cart import store_cart_url, store_search_url
from panier.cli import app
from panier.collector import managed_browser_profile_for_drive
from panier.drive import collect_drive_offers
from panier.managed_browser import ManagedBrowserClient
from panier.models import ShoppingItem

RUNNER = CliRunner()


def test_carrefour_uses_dedicated_managed_browser_profile() -> None:
    assert managed_browser_profile_for_drive("courses", "carrefour") == "courses-carrefour"
    assert managed_browser_profile_for_drive("courses", "leclerc") == "courses"
    assert managed_browser_profile_for_drive("courses", "auchan") == "courses-auchan"


def test_carrefour_search_and_cart_urls() -> None:
    assert (
        store_search_url("carrefour", "emmental rape")
        == "https://www.carrefour.fr/s?q=emmental+rape"
    )
    assert store_cart_url("carrefour") == "https://www.carrefour.fr/cart"


def test_drive_plan_accepts_carrefour(tmp_path: Path) -> None:
    shopping = tmp_path / "list.yaml"
    shopping.write_text(
        yaml_dump({"items": [{"name": "riz"}]}), encoding="utf-8"
    )

    result = RUNNER.invoke(
        app,
        [
            "drive",
            "plan",
            str(shopping),
            "--drive",
            "carrefour",
            "--data-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "-> riz" in result.output


def test_collect_drive_offers_works_for_carrefour_via_generic_extraction() -> None:
    def fake_runner(args: list[str], *, input_text: str | None = None):
        if "navigate" in args:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=json.dumps({"result": {"value": {"tabId": "tab-carrefour"}}}),
            )
        if "console" in args:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=json.dumps(
                    {
                        "result": {
                            "value": {
                                "items": [
                                    {
                                        "title": "Emmental râpé Carrefour 200g",
                                        "price": "2,10 €",
                                        "unitPrice": "10,50 €/kg",
                                        "url": "/p/emmental-200g/12345",
                                    }
                                ]
                            }
                        }
                    }
                ),
            )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="{}")

    client = ManagedBrowserClient(command="managed-browser", runner=fake_runner)
    offers = collect_drive_offers(
        [ShoppingItem(name="emmental râpé")], "carrefour", client, max_results=1
    )

    assert len(offers) == 1
    offer = offers[0]
    assert offer.store == "carrefour"
    assert offer.price == 2.10
    assert offer.unit_price == 10.50
    assert offer.url == "https://www.carrefour.fr/p/emmental-200g/12345"


def test_run_cart_flow_live_uses_carrefour_specific_expression(monkeypatch) -> None:
    expressions: list[str] = []
    calls: list[list[str]] = []

    def fake_runner(args: list[str], *, input_text: str | None = None):
        calls.append(args)
        if "storage" in args and "checkpoint" in args:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="{}")
        if "navigate" in args:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="{}")
        if "console" in args:
            expression = args[args.index("--expression") + 1]
            expressions.append(expression)
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=json.dumps(
                    {
                        "result": {
                            "value": {
                                "catalog_found": True,
                                "addable": True,
                                "inserted": True,
                                "url": "https://www.carrefour.fr/s?q=riz",
                                "button_label": "Ajouter au panier",
                            }
                        }
                    }
                ),
            )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="{}")

    def fake_init(self, **kwargs):
        self.command = kwargs.get("command")
        self.profile = kwargs.get("profile", "courses-carrefour")
        self.site = kwargs.get("site", "carrefour")
        self.runner = fake_runner

    monkeypatch.setattr(cli.ManagedBrowserClient, "__init__", fake_init)
    result = cli.run_cart_flow_for_store(
        "carrefour",
        [
            cli.CartLine(
                store="carrefour",
                item="riz",
                product="Riz basmati Carrefour",
                search_url="https://www.carrefour.fr/s?q=riz",
            )
        ],
        profile="courses",
        browser_command="managed-browser",
        dry_run=False,
    )

    value = cli.cart_flow_value(result)
    assert len(value["inserted"]) == 1
    assert expressions
    assert "product-card" in expressions[0]
    assert "Supprimer" not in expressions[0]
    assert "Riz basmati Carrefour" in expressions[0]
    assert "before-live-cart-carrefour" in calls[0]


def test_run_cart_remove_flow_live_uses_carrefour_expression(monkeypatch) -> None:
    expressions: list[str] = []
    calls: list[list[str]] = []

    def fake_runner(args: list[str], *, input_text: str | None = None):
        calls.append(args)
        if "storage" in args and "checkpoint" in args:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="{}")
        if "navigate" in args:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="{}")
        if "console" in args:
            expression = args[args.index("--expression") + 1]
            expressions.append(expression)
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=json.dumps(
                    {
                        "result": {
                            "value": {
                                "catalog_found": True,
                                "removable": True,
                                "removed": True,
                                "url": "https://www.carrefour.fr/cart",
                                "button_label": "Supprimer",
                            }
                        }
                    }
                ),
            )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="{}")

    def fake_init(self, **kwargs):
        self.command = kwargs.get("command")
        self.profile = kwargs.get("profile", "courses-carrefour")
        self.site = kwargs.get("site", "carrefour")
        self.runner = fake_runner

    monkeypatch.setattr(cli.ManagedBrowserClient, "__init__", fake_init)
    result = cli.run_cart_remove_flow_for_store(
        "carrefour",
        [
            cli.CartLine(
                store="carrefour",
                item="riz",
                product="Riz basmati Carrefour",
                url="https://www.carrefour.fr/cart",
            )
        ],
        profile="courses",
        browser_command="managed-browser",
        dry_run=False,
    )

    value = cli.cart_flow_value(result)
    assert len(value["removed"]) == 1
    assert expressions
    assert "cart-item" in expressions[0]
    assert "Ajouter" not in expressions[0]
    assert "before-live-cart-carrefour-remove" in calls[0]


def test_intermarche_still_unsupported_for_cart_flows() -> None:
    result = RUNNER.invoke(app, ["cart", "add", "--drive-check", "intermarche"])
    assert result.exit_code != 0 or "intermarche" not in result.output.lower()


def yaml_dump(data: dict) -> str:
    import yaml as _yaml

    return _yaml.safe_dump(data, allow_unicode=True)
