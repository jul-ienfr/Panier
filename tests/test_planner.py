from pathlib import Path

from typer.testing import CliRunner

from panier.cli import app
from panier.models import FoodProfile, Pantry, PriceMode, Recipe, ShoppingItem, StoreOffer
from panier.planner import consolidate_ingredients, recommend_basket, select_meals, subtract_pantry


def test_select_meals_excludes_disliked_ingredients() -> None:
    recipes = [
        Recipe(name="ok", ingredients=[{"name": "riz"}]),
        Recipe(name="blocked", ingredients=[{"name": "oignons"}]),
    ]
    profile = FoodProfile(dislikes={"oignons"})

    assert [recipe.name for recipe in select_meals(recipes, profile, meals=3)] == ["ok"]


def test_consolidate_ingredients_sums_same_unit() -> None:
    recipes = [
        Recipe(name="a", ingredients=[{"name": "Riz", "quantity": 100, "unit": "g"}]),
        Recipe(name="b", ingredients=[{"name": "riz", "quantity": 250, "unit": "g"}]),
    ]

    items = consolidate_ingredients(recipes)

    assert items == [ShoppingItem(name="riz", quantity=350, unit="g")]


def test_subtract_pantry_keeps_only_missing_quantities() -> None:
    items = [
        ShoppingItem(name="riz", quantity=500, unit="g"),
        ShoppingItem(name="tomates", quantity=2, unit="boîte"),
        ShoppingItem(name="sel"),
    ]
    pantry = Pantry(
        items=[
            ShoppingItem(name="riz", quantity=200, unit="g"),
            ShoppingItem(name="tomates", quantity=2, unit="boîte"),
            ShoppingItem(name="sel"),
        ]
    )

    assert subtract_pantry(items, pantry) == [ShoppingItem(name="riz", quantity=300, unit="g")]


def test_hybrid_keeps_single_store_when_split_savings_too_small() -> None:
    items = [ShoppingItem(name="riz"), ShoppingItem(name="tomates")]
    offers = [
        StoreOffer(store="leclerc", item="riz", product="Riz", price=2.0),
        StoreOffer(store="leclerc", item="tomates", product="Tomates", price=2.0),
        StoreOffer(store="intermarche", item="riz", product="Riz", price=3.0),
        StoreOffer(store="intermarche", item="tomates", product="Tomates", price=5.0),
    ]

    recommendation = recommend_basket(items, offers, PriceMode.HYBRID, max_stores=2)

    assert recommendation.stores == ("leclerc",)
    assert recommendation.total == 4.0


def test_economic_can_split_between_stores() -> None:
    items = [ShoppingItem(name="riz"), ShoppingItem(name="tomates")]
    offers = [
        StoreOffer(store="leclerc", item="riz", product="Riz", price=2.0),
        StoreOffer(store="leclerc", item="tomates", product="Tomates", price=5.0),
        StoreOffer(store="intermarche", item="riz", product="Riz", price=1.0),
        StoreOffer(store="intermarche", item="tomates", product="Tomates", price=4.0),
        StoreOffer(store="auchan", item="riz", product="Riz", price=1.5),
        StoreOffer(store="auchan", item="tomates", product="Tomates", price=1.0),
    ]

    recommendation = recommend_basket(items, offers, PriceMode.ECONOMIC, max_stores=2)

    assert recommendation.stores == ("auchan", "intermarche")
    assert recommendation.total == 2.0


def test_plan_with_prices_outputs_optimized_basket(tmp_path: Path) -> None:
    (tmp_path / "recipes.yaml").write_text(
        """
- name: Chili
  ingredients:
    - name: riz
      quantity: 300
      unit: g
    - name: haricots rouges
      quantity: 1
      unit: boîte
    - name: tomates concassées
      quantity: 1
      unit: boîte
- name: Pâtes thon
  ingredients:
    - name: pâtes
      quantity: 250
      unit: g
    - name: thon
      quantity: 1
      unit: boîte
""",
        encoding="utf-8",
    )
    (tmp_path / "pantry.yaml").write_text(
        """
items:
  - name: riz
    quantity: 150
    unit: g
""",
        encoding="utf-8",
    )
    prices = tmp_path / "prices.yaml"
    prices.write_text(
        """
offers:
  - store: leclerc
    item: riz
    product: Riz 1kg
    price: 1.80
  - store: leclerc
    item: haricots rouges
    product: Haricots rouges
    price: 2.20
  - store: leclerc
    item: tomates concassées
    product: Tomates concassées
    price: 1.70
  - store: leclerc
    item: pâtes
    product: Pâtes 1kg
    price: 1.35
  - store: leclerc
    item: thon
    product: Thon x3
    price: 4.50
  - store: intermarche
    item: riz
    product: Riz 1kg
    price: 1.65
  - store: intermarche
    item: haricots rouges
    product: Haricots rouges
    price: 1.90
  - store: intermarche
    item: tomates concassées
    product: Tomates concassées
    price: 2.10
  - store: intermarche
    item: pâtes
    product: Pâtes 1kg
    price: 1.55
  - store: intermarche
    item: thon
    product: Thon x3
    price: 4.20
""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["plan", "--data-dir", str(tmp_path), "--meals", "2", "--prices", str(prices)],
    )

    assert result.exit_code == 0
    assert "Liste à acheter:" in result.output
    assert "- riz 150 g" in result.output
    assert "Recommandation achat:" in result.output
    assert "Total:" in result.output
    assert "Détail achat:" in result.output


def test_pantry_cli_add_list_and_remove(tmp_path: Path) -> None:
    runner = CliRunner()

    init_result = runner.invoke(app, ["pantry", "init", "--data-dir", str(tmp_path)])
    add_result = runner.invoke(
        app,
        [
            "pantry",
            "add",
            "Riz",
            "--quantity",
            "200",
            "--unit",
            "g",
            "--data-dir",
            str(tmp_path),
        ],
    )
    add_more_result = runner.invoke(
        app,
        [
            "pantry",
            "add",
            "riz",
            "--quantity",
            "100",
            "--unit",
            "g",
            "--data-dir",
            str(tmp_path),
        ],
    )
    list_result = runner.invoke(app, ["pantry", "list", "--data-dir", str(tmp_path)])
    remove_result = runner.invoke(
        app,
        [
            "pantry",
            "remove",
            "riz",
            "--quantity",
            "150",
            "--unit",
            "g",
            "--data-dir",
            str(tmp_path),
        ],
    )
    list_after_remove = runner.invoke(app, ["pantry", "list", "--data-dir", str(tmp_path)])

    assert init_result.exit_code == 0
    assert add_result.exit_code == 0
    assert add_more_result.exit_code == 0
    assert "- riz 300 g" in list_result.output
    assert remove_result.exit_code == 0
    assert "- riz 150 g" in list_after_remove.output
