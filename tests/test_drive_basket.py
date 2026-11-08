from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from panier.cli import app
from panier.drive_basket import (
    DriveBasketError,
    compare_from_history,
    parse_items,
)
from panier.history_store import record_offers
from panier.household import (
    HouseholdProfile,
    save_household,
    set_active_household,
)
from panier.models import StoreOffer

RUNNER = CliRunner()


def _offer(item: str, store: str, product: str, price: float, **extra) -> StoreOffer:
    return StoreOffer(store=store, item=item, product=product, price=price, **extra)


def _seed(tmp_path: Path) -> None:
    record_offers(
        tmp_path,
        "leclerc",
        [
            _offer("riz", "leclerc", "Riz long étuvé Eco+ 4x125g", 0.85),
            _offer("thon", "leclerc", "Miettes de thon à la tomate Eco+ 160g", 0.77),
        ],
    )
    record_offers(
        tmp_path,
        "auchan",
        [
            _offer("riz", "auchan", "POUCE Riz long étuvé 500g", 0.89),
            _offer("courgettes", "auchan", "AUCHAN Courgettes en rondelles 1kg", 1.25),
        ],
    )


def _household(tmp_path: Path, stores: dict[str, bool]) -> None:
    household = HouseholdProfile(name="foyer", stores=stores)
    save_household(tmp_path, household)
    set_active_household(tmp_path, household.name)


def test_parse_items_dedupes_and_rejects_empty() -> None:
    items = parse_items(["riz, thon", "Riz"])
    assert [item.name for item in items] == ["riz", "thon"]
    with pytest.raises(DriveBasketError):
        parse_items([])


def test_compare_uses_household_active_stores(tmp_path: Path) -> None:
    _seed(tmp_path)
    _household(tmp_path, {"leclerc": True, "auchan": False})
    payload = compare_from_history(tmp_path, parse_items(["riz", "thon", "courgettes"]))
    assert payload["stores_used"] == ["leclerc"]
    stores = {entry["store"]: entry for entry in payload["stores"]}
    assert stores["leclerc"]["items_covered"] == 2
    assert stores["leclerc"]["enabled"] is True
    assert "courgettes" in stores["leclerc"]["items_missing"]
    assert payload["missing_items"] == ["courgettes"]


def test_compare_picks_single_store_when_cheapest(tmp_path: Path) -> None:
    _seed(tmp_path)
    _household(tmp_path, {"leclerc": True, "auchan": True})
    payload = compare_from_history(tmp_path, parse_items(["riz", "thon"]), max_stores=1)
    recommendation = payload["recommendation"]
    assert recommendation is not None
    assert recommendation["stores"] == ["leclerc"]
    assert recommendation["total"] == pytest.approx(1.62)


def test_compare_explicit_store_overrides_household(tmp_path: Path) -> None:
    _seed(tmp_path)
    _household(tmp_path, {"leclerc": True, "auchan": False})
    payload = compare_from_history(
        tmp_path, parse_items(["riz"]), stores=["auchan"], include_disabled=True
    )
    assert payload["stores_used"] == ["auchan"]
    assert payload["offers"][0]["store"] == "auchan"


def test_compare_rejects_when_all_stores_disabled(tmp_path: Path) -> None:
    _seed(tmp_path)
    _household(tmp_path, {"leclerc": False})
    with pytest.raises(DriveBasketError, match="aucune enseigne active"):
        compare_from_history(tmp_path, parse_items(["riz"]))


def test_compare_reports_missing_when_history_empty(tmp_path: Path) -> None:
    _household(tmp_path, {"leclerc": True})
    with pytest.raises(DriveBasketError, match="aucune offre connue"):
        compare_from_history(tmp_path, parse_items(["riz"]))


def test_compare_skips_off_topic_products(tmp_path: Path) -> None:
    record_offers(
        tmp_path,
        "leclerc",
        [
            _offer("courgettes", "leclerc", "Petits pots bébé NATURNES courgettes", 0.99),
            _offer("courgettes", "leclerc", "Courgettes Notre Jardin Rondelles 1kg", 1.20),
        ],
    )
    _household(tmp_path, {"leclerc": True})
    payload = compare_from_history(tmp_path, parse_items(["courgettes"]))
    products = [offer["product"] for offer in payload["offers"]]
    assert "Petits pots bébé NATURNES courgettes" not in products
    assert "Courgettes Notre Jardin Rondelles 1kg" in products


def test_compare_falls_back_to_off_topic_when_only_option(tmp_path: Path) -> None:
    record_offers(
        tmp_path,
        "leclerc",
        [_offer("courgettes", "leclerc", "Petits pots bébé courgettes 130g", 0.99)],
    )
    _household(tmp_path, {"leclerc": True})
    payload = compare_from_history(tmp_path, parse_items(["courgettes"]))
    assert len(payload["offers"]) == 1


def test_compare_respects_since_window(tmp_path: Path) -> None:
    old = datetime.now(UTC) - timedelta(hours=48)
    record_offers(
        tmp_path,
        "leclerc",
        [_offer("riz", "leclerc", "Riz long étuvé Eco+ 4x125g", 0.85)],
        collected_at=old,
    )
    record_offers(
        tmp_path,
        "leclerc",
        [_offer("riz", "leclerc", "Riz long blanc prix mini 1kg", 1.15)],
    )
    _household(tmp_path, {"leclerc": True})
    windowed = compare_from_history(tmp_path, parse_items(["riz"]), since="24h")
    products = [offer["product"] for offer in windowed["offers"]]
    assert products == ["Riz long blanc prix mini 1kg"]
    full = compare_from_history(tmp_path, parse_items(["riz"]), since="7d")
    assert len(full["offers"]) == 2


def test_parse_items_rejects_too_many_items() -> None:
    with pytest.raises(DriveBasketError, match="trop d'articles"):
        parse_items([",".join(f"article{i}" for i in range(61))])


def test_parse_items_accepts_single_csv_option() -> None:
    items = parse_items([", riz ,,thon ,"])
    assert [item.name for item in items] == ["riz", "thon"]


def test_compare_exposes_options_and_savings(tmp_path: Path) -> None:
    _seed(tmp_path)
    _household(tmp_path, {"leclerc": True, "auchan": True})
    payload = compare_from_history(tmp_path, parse_items(["riz", "thon"]), max_stores=2)
    singles = {opt["stores"][0]: opt for opt in payload["options"] if len(opt["stores"]) == 1}
    assert singles["leclerc"]["total"] == pytest.approx(1.62)
    assert singles["leclerc"]["complete"] is True
    assert len(singles["leclerc"]["by_item"]) == 2
    recommendation = payload["recommendation"]
    assert "savings_vs_best_single" in recommendation
    assert recommendation["savings_vs_best_single"] >= 0


def test_compare_coverage_reports_history_volume(tmp_path: Path) -> None:
    _seed(tmp_path)
    _household(tmp_path, {"leclerc": True, "auchan": True})
    payload = compare_from_history(tmp_path, parse_items(["riz"]))
    stores = {entry["store"]: entry for entry in payload["stores"]}
    assert stores["leclerc"]["offers_count"] == 2
    assert stores["auchan"]["offers_count"] == 2
    assert stores["leclerc"]["last_collected_at"]
    assert stores["leclerc"]["collectable"] is True


def test_drive_basket_cli_text_output(tmp_path: Path) -> None:
    _seed(tmp_path)
    _household(tmp_path, {"leclerc": True, "auchan": True})
    result = RUNNER.invoke(
        app,
        ["drive", "basket", "--items", "riz,thon", "--data-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "Panier drive" in result.stdout
    assert "1.62 €" in result.stdout
    assert "leclerc" in result.stdout


def test_drive_basket_cli_json(tmp_path: Path) -> None:
    _seed(tmp_path)
    _household(tmp_path, {"leclerc": True, "auchan": True})
    result = RUNNER.invoke(
        app,
        [
            "drive",
            "basket",
            "--items",
            "riz,thon",
            "--format",
            "json",
            "--data-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["recommendation"]["total"] > 0
    assert payload["compare_by"] == "price"


def test_drive_basket_cli_reports_error_without_history(tmp_path: Path) -> None:
    _household(tmp_path, {"leclerc": True})
    result = RUNNER.invoke(
        app,
        [
            "drive",
            "basket",
            "--items",
            "riz",
            "--data-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 1
    assert "aucune offre connue" in result.output
