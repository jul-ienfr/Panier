from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

import panier.cli as cli
from panier.cli import app
from panier.drive import BrandType, DriveProduct, build_drive_search_plan, build_drive_search_query
from panier.models import PriceMode, ShoppingItem, StoreOffer
from panier.planner import recommend_basket


def _write_deterministic_plan_fixture(data_dir: Path) -> Path:
    (data_dir / "recipes.yaml").write_text(
        """
- name: Chili doux
  tags: [budget, rapide]
  ingredients:
    - name: Riz
      quantity: 300
      unit: g
    - name: Tomates concassées
      quantity: 1
      unit: boîte
- name: Pâtes thon
  tags: [budget]
  ingredients:
    - name: Pâtes
      quantity: 250
      unit: g
    - name: Thon
      quantity: 1
      unit: boîte
""",
        encoding="utf-8",
    )
    (data_dir / "pantry.yaml").write_text(
        """
items:
  - name: riz
    quantity: 100
    unit: g
""",
        encoding="utf-8",
    )
    prices = data_dir / "prices.yaml"
    prices.write_text(
        """
offers:
  - store: leclerc
    item: riz
    product: Riz rond 1kg
    price: 2.00
    unit_price: 2.00
    confidence: exact
  - store: leclerc
    item: tomates concassées
    product: Tomates concassées 400g
    price: 1.20
    unit_price: 3.00
    confidence: exact
  - store: leclerc
    item: pâtes
    product: Pâtes 1kg
    price: 1.40
    unit_price: 1.40
    confidence: exact
  - store: leclerc
    item: thon
    product: Thon x3
    price: 4.50
    unit_price: 15.00
    confidence: exact
  - store: auchan
    item: riz
    product: Riz basmati 1kg
    price: 2.10
    unit_price: 2.10
    confidence: exact
  - store: auchan
    item: tomates concassées
    product: Tomates concassées Auchan
    price: 1.30
    unit_price: 3.25
    confidence: exact
  - store: auchan
    item: pâtes
    product: Pâtes Auchan 1kg
    price: 1.50
    unit_price: 1.50
    confidence: exact
  - store: auchan
    item: thon
    product: Thon Auchan x3
    price: 4.60
    unit_price: 15.33
    confidence: exact
""",
        encoding="utf-8",
    )
    return prices


def test_plan_is_repeatable_without_network_or_llm(monkeypatch, tmp_path: Path) -> None:
    """A local plan with fixture prices is a pure deterministic transform.

    The command must not attempt browser/network/LLM escalation unless --collect (or a future
    explicit escalation flag) is requested, and identical inputs must produce identical output.
    """
    prices = _write_deterministic_plan_fixture(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-read-for-local-plan")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-be-read-for-local-plan")

    def fail_collect(*args: object, **kwargs: object) -> None:
        raise AssertionError("plan without --collect must not collect from drives")

    monkeypatch.setattr(cli, "collect_offers_for_drives", fail_collect)
    runner = CliRunner()
    args = [
        "plan",
        "--meals",
        "2",
        "--include-tags",
        "budget",
        "--prices",
        str(prices),
        "--data-dir",
        str(tmp_path),
    ]

    first = runner.invoke(app, args)
    second = runner.invoke(app, args)

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert first.output == second.output
    assert first.output == (
        "Recettes retenues:\n"
        "- Chili doux\n"
        "- Pâtes thon\n"
        "\n"
        "À acheter:\n"
        "- pâtes 250 g\n"
        "- riz 200 g\n"
        "- thon 1 boîte\n"
        "- tomates concassées 1 boîte\n"
        "\n"
        "Comparatif paniers:\n"
        "- Tout auchan: 9.50 €\n"
        "- Tout leclerc: 9.10 €\n"
        "- Hybride auchan + leclerc: 9.10 €\n"
        "\n"
        "Recommandation achat:\n"
        "Type: panier simple — commander dans un seul drive\n"
        "Mode: hybrid\n"
        "Drives: leclerc\n"
        "Total: 9.10 €\n"
        "Raison: Meilleur panier selon les contraintes demandées.\n"
        "\n"
        "Détail achat:\n"
        "- pâtes 250 g: Pâtes 1kg — leclerc — 1.40 € (exact)\n"
        "- riz 200 g: Riz rond 1kg — leclerc — 2.00 € (exact)\n"
        "- thon 1 boîte: Thon x3 — leclerc — 4.50 € (exact)\n"
        "- tomates concassées 1 boîte: Tomates concassées 400g — leclerc — 1.20 € (exact)\n"
    )


def test_drive_search_plan_snapshot_is_stable_across_repeated_calls() -> None:
    items = [
        ShoppingItem(name="Lait demi-écrémé", quantity=1, unit="L"),
        ShoppingItem(name="Cola", quantity=6, unit="canette"),
        ShoppingItem(name="Tomates concassées", quantity=1, unit="boîte"),
    ]
    products = {
        "lait demi-écrémé": DriveProduct(
            name="lait demi-écrémé", brand="Candia", brand_type=BrandType.COMMON
        ),
        "cola": DriveProduct(
            name="cola", brand="Marque Repère", brand_type=BrandType.STORE_BRAND,
            store_brand_affinity="Leclerc"
        ),
    }

    first = build_drive_search_plan(items, "Leclerc", products)
    second = build_drive_search_plan(items, "LECLERC", products)

    assert [(entry.item.name, entry.query, entry.confidence) for entry in first] == [
        ("lait demi-écrémé", "lait demi-écrémé candia", "high"),
        ("cola", "cola marque repère", "high"),
        ("tomates concassées", "tomates concassées", "low"),
    ]
    assert [(entry.query, entry.confidence) for entry in second] == [
        (entry.query, entry.confidence) for entry in first
    ]
    assert build_drive_search_query(items[1], "carrefour", products["cola"]) == "cola"


def test_basket_recommendation_tie_breaks_to_stable_single_store() -> None:
    items = [ShoppingItem(name="riz"), ShoppingItem(name="pâtes")]
    offers = [
        StoreOffer(store="zdrive", item="riz", product="Riz Z", price=1.0),
        StoreOffer(store="adrive", item="riz", product="Riz A", price=1.0),
        StoreOffer(store="zdrive", item="pâtes", product="Pâtes Z", price=2.0),
        StoreOffer(store="adrive", item="pâtes", product="Pâtes A", price=2.0),
    ]

    recommendation = recommend_basket(items, offers, PriceMode.SIMPLE, max_stores=1)

    assert recommendation.stores == ("adrive",)
    assert recommendation.total == 3.0
    assert [recommendation.by_item[item.name].product for item in items] == ["Riz A", "Pâtes A"]


def test_economic_tie_break_prefers_fewer_then_sorted_stores() -> None:
    items = [ShoppingItem(name="riz"), ShoppingItem(name="pâtes")]
    offers = [
        StoreOffer(store="adrive", item="riz", product="Riz A", price=1.0),
        StoreOffer(store="adrive", item="pâtes", product="Pâtes A", price=2.0),
        StoreOffer(store="bdrive", item="riz", product="Riz B", price=1.0),
        StoreOffer(store="cdrive", item="pâtes", product="Pâtes C", price=2.0),
    ]

    recommendation = recommend_basket(items, offers, PriceMode.ECONOMIC, max_stores=2)

    assert recommendation.stores == ("adrive",)
    assert recommendation.total == 3.0
    assert recommendation.reason == "Meilleur panier selon les contraintes demandées."
