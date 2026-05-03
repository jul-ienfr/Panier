import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import panier.cli as cli
from panier.brands import BrandPreferences, load_brand_preferences
from panier.cart import (
    CART_STATUS_EVAL_JS,
    CartLine,
    cart_items_param,
    cart_lines_from_recommendation,
    cart_run_path,
    cart_sync_diff,
    load_cart_run,
    new_cart_run_id,
    store_cart_url,
    store_search_url,
)
from panier.cli import app, managed_browser_profile_for_drive
from panier.drive import (
    BrandType,
    DriveProduct,
    best_offer_for_item,
    build_drive_search_plan,
    build_drive_search_query,
    collect_drive_offers,
    drive_search_url,
    open_drive_searches,
)
from panier.managed_browser import ManagedBrowserClient, ManagedBrowserError
from panier.models import FoodProfile, Pantry, PriceMode, Recipe, ShoppingItem, StoreOffer
from panier.nutrition import score_recipe_balance
from panier.planner import (
    compare_basket_options,
    consolidate_ingredients,
    recommend_basket,
    select_meals,
    subtract_pantry,
)


def test_select_meals_excludes_disliked_ingredients() -> None:
    recipes = [
        Recipe(name="ok", ingredients=[{"name": "riz"}]),
        Recipe(name="blocked", ingredients=[{"name": "oignons"}]),
    ]
    profile = FoodProfile(dislikes={"oignons"})

    assert [recipe.name for recipe in select_meals(recipes, profile, meals=3)] == ["ok"]


def test_select_meals_scores_recipes_deterministically_before_slicing() -> None:
    recipes = [
        Recipe(
            name="Pâtes fromage",
            prep_minutes=5,
            cost_level="budget",
            tags=["rapide", "budget"],
            ingredients=[
                {"name": "pâtes", "quantity": 250, "unit": "g"},
                {"name": "fromage", "quantity": 150, "unit": "g"},
            ],
        ),
        Recipe(
            name="Bowl poulet",
            prep_minutes=30,
            cost_level="budget",
            tags=["rapide", "budget"],
            ingredients=[
                {"name": "riz", "quantity": 150, "unit": "g"},
                {"name": "poulet", "quantity": 150, "unit": "g"},
                {"name": "brocoli", "quantity": 1, "unit": "pièce"},
            ],
        ),
    ]

    selected = select_meals(
        recipes,
        FoodProfile(),
        meals=1,
        include_tags={"rapide"},
        min_balance_score=0,
    )

    assert [recipe.name for recipe in selected] == ["Bowl poulet"]


def test_select_meals_tie_breaks_by_prep_minutes_then_name() -> None:
    recipes = [
        Recipe(name="Zeta", prep_minutes=20, tags=["rapide"], ingredients=[{"name": "riz"}]),
        Recipe(name="Alpha", prep_minutes=20, tags=["rapide"], ingredients=[{"name": "riz"}]),
        Recipe(name="Charlie", prep_minutes=10, tags=["rapide"], ingredients=[{"name": "riz"}]),
    ]

    selected = select_meals(recipes, FoodProfile(), meals=3, include_tags={"rapide"})

    assert [recipe.name for recipe in selected] == ["Charlie", "Alpha", "Zeta"]


def test_select_meals_tie_breaks_are_stable_across_input_order() -> None:
    recipes = [
        Recipe(name="Gamma", prep_minutes=15, tags=["rapide"], ingredients=[{"name": "riz"}]),
        Recipe(name="Beta", prep_minutes=15, tags=["rapide"], ingredients=[{"name": "riz"}]),
    ]

    forward = select_meals(recipes, FoodProfile(), meals=2, include_tags={"rapide"})
    backward = select_meals(
        list(reversed(recipes)), FoodProfile(), meals=2, include_tags={"rapide"}
    )

    assert [recipe.name for recipe in forward] == ["Beta", "Gamma"]
    assert [recipe.name for recipe in backward] == ["Beta", "Gamma"]


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


def test_compare_basket_options_reports_single_store_missing_and_hybrid_total() -> None:
    items = [ShoppingItem(name="riz"), ShoppingItem(name="carottes")]
    offers = [
        StoreOffer(store="auchan", item="riz", product="Riz", price=2.0),
        StoreOffer(store="auchan", item="carottes", product="Carottes", price=3.0),
        StoreOffer(store="leclerc", item="riz", product="Riz", price=1.0),
    ]

    options = compare_basket_options(items, offers, max_stores=2)

    by_store = {option.stores: option for option in options}
    assert by_store[("auchan",)].total == 5.0
    assert by_store[("auchan",)].missing_items == []
    assert by_store[("leclerc",)].total is None
    assert by_store[("leclerc",)].missing_items == ["carottes"]
    assert by_store[("auchan", "leclerc")].total == 4.0


def test_recommend_basket_can_compare_by_unit_price() -> None:
    items = [ShoppingItem(name="huile", quantity=1, unit="l")]
    offers = [
        StoreOffer(
            store="leclerc",
            item="huile",
            product="Huile 50cl",
            price=2.10,
            unit_price=4.20,
        ),
        StoreOffer(
            store="auchan",
            item="huile",
            product="Huile 1L",
            price=3.00,
            unit_price=3.00,
        ),
    ]

    recommendation = recommend_basket(
        items, offers, PriceMode.SIMPLE, max_stores=1, compare_by="unit_price"
    )

    assert recommendation.stores == ("auchan",)
    assert recommendation.total == 3.0
    assert recommendation.by_item["huile"].product == "Huile 1L"


def test_brand_preference_prefers_brand_within_price_guardrail() -> None:
    items = [ShoppingItem(name="yaourt")]
    offers = [
        StoreOffer(store="leclerc", item="yaourt", product="Yaourt Eco", price=2.00),
        StoreOffer(store="leclerc", item="yaourt", product="Yaourt Maison A", price=2.20),
    ]
    preferences = BrandPreferences(prefer={"maison a"})

    recommendation = recommend_basket(
        items,
        offers,
        PriceMode.SIMPLE,
        max_stores=1,
        brand_preferences=preferences,
    )

    assert recommendation.by_item["yaourt"].product == "Yaourt Maison A"
    assert recommendation.total == 2.20


def test_brand_preference_price_guardrail_keeps_much_cheaper_offer() -> None:
    items = [ShoppingItem(name="yaourt")]
    offers = [
        StoreOffer(store="leclerc", item="yaourt", product="Yaourt Eco", price=2.00),
        StoreOffer(store="leclerc", item="yaourt", product="Yaourt Maison A", price=3.50),
    ]
    preferences = BrandPreferences(prefer={"maison a"})

    recommendation = recommend_basket(
        items,
        offers,
        PriceMode.SIMPLE,
        max_stores=1,
        brand_preferences=preferences,
    )

    assert recommendation.by_item["yaourt"].product == "Yaourt Eco"


def test_brand_preference_avoid_keeps_avoided_brand_only_for_big_savings() -> None:
    items = [ShoppingItem(name="pates")]
    preferences = BrandPreferences(avoid={"discounto"})

    small_gap = recommend_basket(
        items,
        [
            StoreOffer(store="leclerc", item="pates", product="Discounto Pâtes", price=1.00),
            StoreOffer(store="leclerc", item="pates", product="Pâtes neutres", price=1.30),
        ],
        PriceMode.SIMPLE,
        max_stores=1,
        brand_preferences=preferences,
    )
    big_gap = recommend_basket(
        items,
        [
            StoreOffer(store="leclerc", item="pates", product="Discounto Pâtes", price=1.00),
            StoreOffer(store="leclerc", item="pates", product="Pâtes neutres", price=4.00),
        ],
        PriceMode.SIMPLE,
        max_stores=1,
        brand_preferences=preferences,
    )

    assert small_gap.by_item["pates"].product == "Pâtes neutres"
    assert big_gap.by_item["pates"].product == "Discounto Pâtes"


def test_brand_preference_block_excludes_brand() -> None:
    items = [ShoppingItem(name="cafe")]
    offers = [
        StoreOffer(store="leclerc", item="cafe", product="Café Bloque", price=1.00),
        StoreOffer(store="leclerc", item="cafe", product="Café OK", price=2.00),
    ]
    preferences = BrandPreferences(block={"bloque"})

    recommendation = recommend_basket(
        items,
        offers,
        PriceMode.SIMPLE,
        max_stores=1,
        brand_preferences=preferences,
    )

    assert recommendation.by_item["cafe"].product == "Café OK"


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
    assert "À acheter:" in result.output
    assert "- riz 150 g" in result.output
    assert "Comparatif paniers:" in result.output
    assert "- Tout leclerc: 11.55 €" in result.output
    assert "- Tout intermarche: 11.40 €" in result.output
    assert "Recommandation achat:" in result.output
    assert "Total:" in result.output
    assert "Détail achat:" in result.output


def test_brand_cli_persists_preferences_deterministically(tmp_path: Path) -> None:
    runner = CliRunner()

    prefer = runner.invoke(app, ["brand", "prefer", "Maison A", "--data-dir", str(tmp_path)])
    avoid = runner.invoke(app, ["brand", "avoid", "Discounto", "--data-dir", str(tmp_path)])
    block = runner.invoke(app, ["brand", "block", "Bloqué", "--data-dir", str(tmp_path)])
    listed = runner.invoke(app, ["brand", "list", "--data-dir", str(tmp_path)])

    assert prefer.exit_code == 0
    assert avoid.exit_code == 0
    assert block.exit_code == 0
    assert listed.exit_code == 0
    assert "prefer: maison a" in listed.output
    assert "avoid: discounto" in listed.output
    assert "block: bloqué" in listed.output
    preferences = load_brand_preferences(tmp_path)
    assert preferences.prefer == {"maison a"}
    assert preferences.avoid == {"discounto"}
    assert preferences.block == {"bloqué"}


def test_compare_cli_applies_brand_preferences(tmp_path: Path) -> None:
    runner = CliRunner()
    shopping_list = tmp_path / "list.yaml"
    prices = tmp_path / "prices.yaml"
    shopping_list.write_text("items:\n  - name: yaourt\n", encoding="utf-8")
    prices.write_text(
        """
offers:
  - store: leclerc
    item: yaourt
    product: Yaourt Eco
    price: 2.00
  - store: leclerc
    item: yaourt
    product: Yaourt Maison A
    price: 2.20
""",
        encoding="utf-8",
    )
    runner.invoke(app, ["brand", "prefer", "Maison A", "--data-dir", str(tmp_path)])

    result = runner.invoke(
        app,
        [
            "compare",
            str(shopping_list),
            "--prices",
            str(prices),
            "--data-dir",
            str(tmp_path),
            "--mode",
            "simple",
            "--max-stores",
            "1",
        ],
    )

    assert result.exit_code == 0
    assert "Yaourt Maison A" in result.output
    assert "2.20 €" in result.output


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


def test_subtract_pantry_converts_kg_to_g() -> None:
    items = [ShoppingItem(name="riz", quantity=750, unit="g")]
    pantry = Pantry(items=[ShoppingItem(name="riz", quantity=0.5, unit="kg")])

    assert subtract_pantry(items, pantry) == [ShoppingItem(name="riz", quantity=250, unit="g")]


def test_pantry_need_consume_and_low_stock_cli(tmp_path: Path) -> None:
    runner = CliRunner()
    recipe = tmp_path / "recette.yaml"
    recipe.write_text(
        """
name: Chili test
ingredients:
  - name: riz
    quantity: 300
    unit: g
  - name: tomates
    quantity: 2
    unit: boîte
""",
        encoding="utf-8",
    )

    runner.invoke(app, ["pantry", "init", "--data-dir", str(tmp_path)])
    runner.invoke(
        app,
        [
            "pantry",
            "add",
            "riz",
            "--quantity",
            "500",
            "--unit",
            "g",
            "--min",
            "300g",
            "--data-dir",
            str(tmp_path),
        ],
    )
    runner.invoke(
        app,
        [
            "pantry",
            "add",
            "tomates",
            "--quantity",
            "1",
            "--unit",
            "boîte",
            "--data-dir",
            str(tmp_path),
        ],
    )

    need_result = runner.invoke(app, ["pantry", "need", str(recipe), "--data-dir", str(tmp_path)])
    consume_result = runner.invoke(
        app, ["pantry", "consume", str(recipe), "--data-dir", str(tmp_path)]
    )
    list_result = runner.invoke(app, ["pantry", "list", "--data-dir", str(tmp_path)])

    assert need_result.exit_code == 0
    assert "Manquant:" in need_result.output
    assert "- tomates 1 boîte" in need_result.output
    assert consume_result.exit_code == 0
    assert "Consommé:" in consume_result.output
    assert "Manquant:" in consume_result.output
    assert "Alerte réachat:" in consume_result.output
    assert "- riz 100 g" in consume_result.output
    assert "- riz 200 g" in list_result.output


def test_shopping_from_recipe_outputs_missing_items(tmp_path: Path) -> None:
    runner = CliRunner()
    recipe = tmp_path / "recette.yaml"
    recipe.write_text(
        """
name: Pâtes test
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
  - name: pâtes
    quantity: 100
    unit: g
""",
        encoding="utf-8",
    )

    result = runner.invoke(
        app, ["shopping", "from-recipe", str(recipe), "--data-dir", str(tmp_path)]
    )

    assert result.exit_code == 0
    assert "Liste à acheter:" in result.output
    assert "- pâtes 150 g" in result.output
    assert "- thon 1 boîte" in result.output


def test_recipe_cli_crud_and_shopping_consolidates_quantities(tmp_path: Path) -> None:
    runner = CliRunner()

    add_gratin = runner.invoke(
        app,
        [
            "recipe",
            "add",
            "Gratin test",
            "--ingredient",
            "emmental râpé:100:g",
            "--ingredient",
            "pâtes:200:g",
            "--tag",
            "test",
            "--data-dir",
            str(tmp_path),
        ],
    )
    add_croque = runner.invoke(
        app,
        [
            "recipe",
            "add",
            "Croque test",
            "--ingredient",
            "emmental râpé:300:g",
            "--ingredient",
            "pain:2:pièce",
            "--data-dir",
            str(tmp_path),
        ],
    )
    list_result = runner.invoke(app, ["recipe", "list", "--data-dir", str(tmp_path)])
    show_result = runner.invoke(app, ["recipe", "show", "gratin test", "--data-dir", str(tmp_path)])
    shopping_result = runner.invoke(
        app,
        [
            "recipe",
            "shopping",
            "gratin test",
            "croque test",
            "--data-dir",
            str(tmp_path),
        ],
    )
    remove_result = runner.invoke(
        app, ["recipe", "remove", "croque test", "--data-dir", str(tmp_path)]
    )
    list_after_remove = runner.invoke(app, ["recipe", "list", "--data-dir", str(tmp_path)])

    assert add_gratin.exit_code == 0
    assert add_croque.exit_code == 0
    assert "- Gratin test" in list_result.output
    assert "- Croque test" in list_result.output
    assert "emmental râpé 100 g" in show_result.output
    assert shopping_result.exit_code == 0
    assert "Liste à acheter:" in shopping_result.output
    assert "- emmental râpé 400 g" in shopping_result.output
    assert "- pâtes 200 g" in shopping_result.output
    assert "- pain 2 pièce" in shopping_result.output
    assert remove_result.exit_code == 0
    assert "Croque test" not in list_after_remove.output


def test_recipe_add_imports_yaml_file_and_filters_recipe_list(tmp_path: Path) -> None:
    recipe_file = tmp_path / "gratin.yaml"
    recipe_file.write_text(
        """
name: Gratin pâtes emmental
servings: 2
prep_minutes: 20
cost_level: budget
tags: [rapide, four, budget]
ingredients:
  - name: pâtes
    quantity: 250
    unit: g
  - name: emmental râpé
    quantity: 150
    unit: g
""",
        encoding="utf-8",
    )
    runner = CliRunner()

    add_result = runner.invoke(
        app, ["recipe", "add", str(recipe_file), "--data-dir", str(tmp_path)]
    )
    list_budget = runner.invoke(
        app, ["recipe", "list", "--tag", "budget", "--data-dir", str(tmp_path)]
    )
    list_no_four = runner.invoke(
        app, ["recipe", "list", "--exclude-tags", "four", "--data-dir", str(tmp_path)]
    )
    show_result = runner.invoke(
        app, ["recipe", "show", "gratin pâtes emmental", "--data-dir", str(tmp_path)]
    )

    assert add_result.exit_code == 0
    assert "Recette ajoutée : Gratin pâtes emmental" in add_result.output
    assert "Gratin pâtes emmental [rapide, four, budget] (20 min) {budget}" in list_budget.output
    assert "Aucune recette" in list_no_four.output
    assert "Préparation: 20 min" in show_result.output
    assert "Coût: budget" in show_result.output


def test_recipe_shopping_subtracts_pantry_after_multi_recipe_consolidation(tmp_path: Path) -> None:
    runner = CliRunner()
    (tmp_path / "recipes.yaml").write_text(
        """
- name: Gratin
  ingredients:
    - name: emmental râpé
      quantity: 100
      unit: g
- name: Croque
  ingredients:
    - name: emmental râpé
      quantity: 300
      unit: g
""",
        encoding="utf-8",
    )
    (tmp_path / "pantry.yaml").write_text(
        """
items:
  - name: emmental râpé
    quantity: 150
    unit: g
""",
        encoding="utf-8",
    )

    result = runner.invoke(
        app, ["recipe", "shopping", "gratin", "croque", "--data-dir", str(tmp_path)]
    )

    assert result.exit_code == 0
    assert "Recettes:" in result.output
    assert "- Gratin" in result.output
    assert "- Croque" in result.output
    assert "- emmental râpé 250 g" in result.output


def test_recipe_balance_score_rewards_complete_simple_meals() -> None:
    balanced = Recipe(
        name="Bowl",
        ingredients=[
            {"name": "quinoa", "quantity": 100, "unit": "g"},
            {"name": "thon", "quantity": 1, "unit": "boîte"},
            {"name": "tomates", "quantity": 2, "unit": "pièce"},
        ],
    )
    rich = Recipe(
        name="Gratin",
        ingredients=[
            {"name": "pâtes", "quantity": 250, "unit": "g"},
            {"name": "emmental râpé", "quantity": 150, "unit": "g"},
            {"name": "crème", "quantity": 20, "unit": "cl"},
        ],
    )

    balanced_score = score_recipe_balance(balanced)
    rich_score = score_recipe_balance(rich)

    assert balanced_score.score >= 70
    assert balanced_score.verdict == "équilibré"
    assert "légumes" in balanced_score.positives
    assert rich_score.score < 70
    assert any("riche" in penalty for penalty in rich_score.penalties)


def test_select_meals_can_filter_by_balance_score() -> None:
    recipes = [
        Recipe(
            name="Bowl",
            ingredients=[
                {"name": "riz", "quantity": 150, "unit": "g"},
                {"name": "poulet", "quantity": 150, "unit": "g"},
                {"name": "brocoli", "quantity": 1, "unit": "pièce"},
            ],
        ),
        Recipe(
            name="Pâtes fromage",
            ingredients=[
                {"name": "pâtes", "quantity": 250, "unit": "g"},
                {"name": "fromage", "quantity": 150, "unit": "g"},
            ],
        ),
    ]

    selected = select_meals(recipes, FoodProfile(), meals=2, min_balance_score=70)

    assert [recipe.name for recipe in selected] == ["Bowl"]


def test_recipe_score_cli_explains_balance(tmp_path: Path) -> None:
    (tmp_path / "recipes.yaml").write_text(
        """
- name: Bowl test
  ingredients:
    - name: riz
      quantity: 150
      unit: g
    - name: poulet
      quantity: 150
      unit: g
    - name: brocoli
      quantity: 1
      unit: pièce
""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["recipe", "score", "bowl test", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert "Équilibre: 80/100 (équilibré)" in result.output
    assert "+ légumes, protéine, féculent" in result.output


def test_plan_balanced_filters_and_displays_scores(tmp_path: Path) -> None:
    (tmp_path / "recipes.yaml").write_text(
        """
- name: Bowl équilibré
  ingredients:
    - name: riz
      quantity: 150
      unit: g
    - name: poulet
      quantity: 150
      unit: g
    - name: brocoli
      quantity: 1
      unit: pièce
- name: Gratin riche
  ingredients:
    - name: pâtes
      quantity: 250
      unit: g
    - name: emmental râpé
      quantity: 150
      unit: g
    - name: crème
      quantity: 20
      unit: cl
""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app, ["plan", "--meals", "2", "--balanced", "--data-dir", str(tmp_path)]
    )

    assert result.exit_code == 0
    assert "- Bowl équilibré — équilibre 80/100 (équilibré)" in result.output
    assert "Gratin riche" not in result.output


def test_plan_filters_recipes_and_can_disable_pantry(tmp_path: Path) -> None:
    (tmp_path / "recipes.yaml").write_text(
        """
- name: Rapide budget
  prep_minutes: 15
  tags: [rapide, budget]
  ingredients:
    - name: emmental râpé
      quantity: 100
      unit: g
- name: Four budget
  prep_minutes: 45
  tags: [budget, four]
  ingredients:
    - name: emmental râpé
      quantity: 300
      unit: g
""",
        encoding="utf-8",
    )
    (tmp_path / "pantry.yaml").write_text(
        """
items:
  - name: emmental râpé
    quantity: 50
    unit: g
""",
        encoding="utf-8",
    )
    runner = CliRunner()

    with_pantry = runner.invoke(
        app,
        [
            "plan",
            "--meals",
            "2",
            "--include-tags",
            "budget",
            "--exclude-tags",
            "four",
            "--max-prep-minutes",
            "20",
            "--data-dir",
            str(tmp_path),
        ],
    )
    no_pantry = runner.invoke(
        app,
        [
            "plan",
            "--meals",
            "2",
            "--include-tags",
            "budget",
            "--exclude-tags",
            "four",
            "--max-prep-minutes",
            "20",
            "--no-pantry",
            "--data-dir",
            str(tmp_path),
        ],
    )

    assert with_pantry.exit_code == 0
    assert "- Rapide budget" in with_pantry.output
    assert "Four budget" not in with_pantry.output
    assert "- emmental râpé 50 g" in with_pantry.output
    assert no_pantry.exit_code == 0
    assert "- emmental râpé 100 g" in no_pantry.output


def test_plan_collects_multiple_drives_and_recommends(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "catalog.yaml").write_text("products: []\n", encoding="utf-8")
    (tmp_path / "recipes.yaml").write_text(
        """
- name: Gratin
  tags: [budget]
  ingredients:
    - name: emmental râpé
      quantity: 400
      unit: g
""",
        encoding="utf-8",
    )
    calls: list[tuple[str, list[ShoppingItem], int]] = []

    def fake_collect(
        items: list[ShoppingItem], drive: str, browser: object, max_results: int
    ) -> list[StoreOffer]:
        calls.append((drive, items, max_results))
        unit_price = 6.99 if drive == "leclerc" else 8.36
        return [
            StoreOffer(
                store=drive,
                item="emmental râpé",
                product=f"Emmental {drive}",
                price=unit_price,
                unit_price=unit_price,
                confidence="exact",
            )
        ]

    monkeypatch.setattr(cli, "collect_drive_offers", fake_collect)
    output = tmp_path / "offers.yaml"

    result = CliRunner().invoke(
        app,
        [
            "plan",
            "--meals",
            "1",
            "--collect",
            "leclerc,auchan",
            "--compare-by",
            "unit-price",
            "--collect-output",
            str(output),
            "--data-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert [call[0] for call in calls] == ["leclerc", "auchan"]
    assert calls[0][1] == [ShoppingItem(name="emmental râpé", quantity=400, unit="g")]
    assert output.exists()
    assert "Collecte leclerc: 1 offres" in result.output
    assert "Emmental leclerc" in result.output
    assert "6.99 €/kg" in result.output


def test_plan_collect_continues_when_one_drive_fails(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "catalog.yaml").write_text("products: []\n", encoding="utf-8")
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
    assert "Collecte leclerc: 1 offres" in result.output
    assert "Avertissement Managed Browser auchan:" in result.output
    assert output.exists()
    assert "Riz Leclerc" in output.read_text(encoding="utf-8")


def test_managed_browser_error_detail_prefers_json_error_fields() -> None:
    completed = subprocess.CompletedProcess(
        args=["managed-browser"],
        returncode=1,
        stdout=json.dumps(
            {
                "error": "Internal server error",
                "operation": "navigate",
                "profile": "courses-auchan",
            }
        ),
        stderr="",
    )

    assert ManagedBrowserClient._error_detail(completed) == (
        "Internal server error — navigate — courses-auchan"
    )


def test_build_drive_search_query_handles_store_brands() -> None:
    item = ShoppingItem(name="cola")
    product = DriveProduct(
        name="cola",
        brand="marque repère",
        brand_type=BrandType.STORE_BRAND,
        store_brand_affinity="leclerc",
    )

    assert build_drive_search_query(item, "leclerc", product) == "cola marque repère"
    assert build_drive_search_query(item, "carrefour", product) == "cola"


def test_build_drive_search_plan_marks_catalog_matches_high_confidence() -> None:
    items = [ShoppingItem(name="Riz"), ShoppingItem(name="tomates")]
    products = {"riz": DriveProduct(name="riz basmati", brand_type=BrandType.GENERIC)}

    plan = build_drive_search_plan(items, "leclerc", products)

    assert [(item.query, item.confidence) for item in plan] == [
        ("riz basmati", "high"),
        ("tomates", "low"),
    ]


def test_best_offer_for_item_prefers_relevance_then_unit_price() -> None:
    item = ShoppingItem(name="tomates concassées", quantity=1, unit="boîte")
    offers = [
        StoreOffer(
            store="leclerc",
            item="tomates concassées",
            product="Tomates rondes fraîches 1kg",
            price=2.20,
            unit_price=2.20,
            confidence="medium",
        ),
        StoreOffer(
            store="leclerc",
            item="tomates concassées",
            product="Tomates concassées boîte 400g",
            price=1.30,
            unit_price=3.25,
            confidence="exact",
        ),
    ]

    chosen = best_offer_for_item(item, offers)

    assert chosen is not None
    assert chosen.offer.product == "Tomates concassées boîte 400g"
    assert chosen.score > 0.5


def test_drive_search_url_encodes_leclerc_query() -> None:
    assert drive_search_url("leclerc", "tomates concassées") == (
        "https://fd2-courses.leclercdrive.fr/magasin-027419-027419-Viuz-en-Sallaz/"
        "recherche.aspx?TexteRecherche=tomates+concass%C3%A9es&tri=1"
    )


def test_drive_open_defaults_to_courses_managed_profile(tmp_path: Path) -> None:
    shopping = tmp_path / "shopping.yaml"
    shopping.write_text("items:\n  - name: riz\n", encoding="utf-8")
    recorder = tmp_path / "recorder.py"
    recorder.write_text(
        """
import json
import sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]), encoding='utf-8')
print(json.dumps({'tabId': 'default-profile-ok'}))
""".strip(),
        encoding="utf-8",
    )
    calls = tmp_path / "calls.json"

    result = CliRunner().invoke(
        app,
        [
            "drive",
            "open",
            str(shopping),
            "--browser-command",
            f"python {recorder} {calls}",
        ],
    )

    assert result.exit_code == 0
    argv = json.loads(calls.read_text(encoding="utf-8"))
    assert "--profile" in argv
    assert argv[argv.index("--profile") + 1] == "courses"
    assert argv[argv.index("--site") + 1] == "leclerc"


def test_auchan_uses_dedicated_managed_browser_profile() -> None:
    assert managed_browser_profile_for_drive("courses", "auchan") == "courses-auchan"
    assert managed_browser_profile_for_drive("courses", "leclerc") == "courses"
    assert managed_browser_profile_for_drive("custom", "auchan") == "custom"


def test_drive_collect_auchan_uses_dedicated_profile(tmp_path: Path, monkeypatch) -> None:
    shopping = tmp_path / "shopping.yaml"
    shopping.write_text("items:\n  - name: riz\n", encoding="utf-8")
    seen: dict[str, str] = {}

    def fake_collect(items, drive, browser, products=None, max_results=5):
        seen["profile"] = browser.profile
        seen["site"] = browser.site
        return []

    monkeypatch.setattr(cli, "collect_drive_offers", fake_collect)

    result = CliRunner().invoke(app, ["drive", "collect", str(shopping), "--drive", "auchan"])

    assert result.exit_code == 0
    assert seen == {"profile": "courses-auchan", "site": "auchan"}


def test_plan_collect_auchan_uses_dedicated_profile(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "catalog.yaml").write_text("products: []\n", encoding="utf-8")
    (tmp_path / "recipes.yaml").write_text(
        """
- name: Riz rapide
  tags: [budget, rapide, equilibre]
  ingredients:
    - name: riz
      quantity: 100
      unit: g
""".strip(),
        encoding="utf-8",
    )
    seen: dict[str, str] = {}

    def fake_collect(items, drive, browser, products=None, max_results=5):
        seen["profile"] = browser.profile
        seen["site"] = browser.site
        return [
            StoreOffer(
                store=drive,
                item="riz",
                product="Riz basmati 1kg",
                price=2.5,
                unit_price=2.5,
            )
        ]

    monkeypatch.setattr(cli, "collect_drive_offers", fake_collect)

    result = CliRunner().invoke(
        app,
        [
            "plan",
            "--data-dir",
            str(tmp_path),
            "--meals",
            "1",
            "--include-tags",
            "budget,rapide",
            "--collect",
            "auchan",
            "--no-pantry",
        ],
    )

    assert result.exit_code == 0
    assert seen == {"profile": "courses-auchan", "site": "auchan"}


def test_managed_browser_client_builds_wrapper_command() -> None:
    calls: list[list[str]] = []

    def fake_runner(
        args: list[str], *, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=json.dumps({"tabId": "abc"})
        )

    client = ManagedBrowserClient(
        command="managed-browser",
        profile="courses",
        site="leclerc",
        runner=fake_runner,
    )

    result = client.navigate("https://example.test/search")

    assert result.data == {"tabId": "abc"}
    assert calls == [
        [
            "managed-browser",
            "navigate",
            "--url",
            "https://example.test/search",
            "--profile",
            "courses",
            "--site",
            "leclerc",
            "--json",
        ]
    ]


def test_managed_browser_client_reports_failures() -> None:
    def fake_runner(
        args: list[str], *, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=args, returncode=7, stdout="", stderr="boom")

    client = ManagedBrowserClient(command="managed-browser", runner=fake_runner)

    try:
        client.status()
    except ManagedBrowserError as exc:
        assert "boom" in str(exc)
    else:
        raise AssertionError("ManagedBrowserError attendu")


def test_open_drive_searches_uses_managed_browser_client() -> None:
    calls: list[list[str]] = []

    def fake_runner(
        args: list[str], *, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=json.dumps({"tabId": len(calls)})
        )

    client = ManagedBrowserClient(
        command="managed-browser", profile="panier", site="leclerc", runner=fake_runner
    )
    results = open_drive_searches(
        [ShoppingItem(name="riz"), ShoppingItem(name="tomates")], "leclerc", client
    )

    assert [result.browser_result.data["tabId"] for result in results] == [1, 2]
    assert calls[0][:3] == ["managed-browser", "navigate", "--url"]
    assert calls[0][3] == (
        "https://fd2-courses.leclercdrive.fr/magasin-027419-027419-Viuz-en-Sallaz/"
        "recherche.aspx?TexteRecherche=riz&tri=1"
    )
    assert calls[1][3] == (
        "https://fd2-courses.leclercdrive.fr/magasin-027419-027419-Viuz-en-Sallaz/"
        "recherche.aspx?TexteRecherche=tomates&tri=1"
    )


def test_drive_cli_plan_and_pick(tmp_path: Path) -> None:
    runner = CliRunner()
    shopping = tmp_path / "shopping.yaml"
    shopping.write_text(
        """
items:
  - name: riz
    quantity: 150
    unit: g
  - name: tomates concassées
    quantity: 1
    unit: boîte
""",
        encoding="utf-8",
    )
    prices = tmp_path / "prices.yaml"
    prices.write_text(
        """
offers:
  - store: leclerc
    item: riz
    product: Riz basmati 1kg
    price: 1.80
  - store: leclerc
    item: tomates concassées
    product: Tomates concassées 400g
    price: 1.20
    unit_price: 3.25
  - store: leclerc
    item: tomates concassées
    product: Tomates fraîches 1kg
    price: 1.10
""",
        encoding="utf-8",
    )

    plan_result = runner.invoke(app, ["drive", "plan", str(shopping), "--drive", "leclerc"])
    pick_result = runner.invoke(
        app, ["drive", "pick", str(shopping), str(prices), "--compare-by", "unit-price"]
    )

    assert plan_result.exit_code == 0
    assert "Recherches à lancer:" in plan_result.output
    assert "- riz 150 g -> riz (low)" in plan_result.output
    assert pick_result.exit_code == 0
    assert "Meilleurs produits:" in pick_result.output
    assert "Tomates concassées 400g" in pick_result.output
    assert "3.25 €/unité" in pick_result.output


def test_drive_cli_open_reports_managed_browser_error(tmp_path: Path) -> None:
    shopping = tmp_path / "shopping.yaml"
    shopping.write_text("items:\n  - name: riz\n", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "drive",
            "open",
            str(shopping),
            "--browser-command",
            "python -c 'import sys; sys.exit(3)'",
        ],
    )

    assert result.exit_code == 1
    assert "Erreur Managed Browser:" in result.output


def test_drive_search_url_supports_auchan() -> None:
    assert drive_search_url("auchan", "lait demi écrémé") == (
        "https://www.auchan.fr/recherche?text=lait+demi+%C3%A9cr%C3%A9m%C3%A9"
    )


def test_managed_browser_client_passes_tab_id_to_console_eval() -> None:
    calls: list[list[str]] = []

    def fake_runner(
        args: list[str], *, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=json.dumps({"items": []})
        )

    client = ManagedBrowserClient(
        command="managed-browser", profile="courses", site="auchan", runner=fake_runner
    )

    result = client.console_eval("1 + 1", tab_id="tab-42")

    assert result.action == "console"
    assert calls == [
        [
            "managed-browser",
            "console",
            "eval",
            "--expression",
            "1 + 1",
            "--tab-id",
            "tab-42",
            "--profile",
            "courses",
            "--site",
            "auchan",
            "--json",
        ]
    ]


def test_collect_drive_offers_extracts_normalized_prices_from_browser_page() -> None:
    calls: list[list[str]] = []
    payloads = [
        {"tabId": "tab-riz"},
        {
            "items": [
                {
                    "title": "Riz basmati 1kg",
                    "price": "2,49 €",
                    "unitPrice": "2,49 €/kg",
                    "url": "/p/riz-basmati",
                },
                {
                    "title": "Céréales riz soufflé",
                    "price": "1,20 €",
                    "unitPrice": "6,00 €/kg",
                    "url": "https://example.test/cereales",
                },
            ]
        },
    ]

    def fake_runner(
        args: list[str], *, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=json.dumps(payloads.pop(0))
        )

    client = ManagedBrowserClient(
        command="managed-browser", profile="courses", site="auchan", runner=fake_runner
    )

    offers = collect_drive_offers([ShoppingItem(name="riz")], "auchan", client, max_results=1)

    assert [offer.product for offer in offers] == ["Riz basmati 1kg"]
    assert offers[0].store == "auchan"
    assert offers[0].item == "riz"
    assert offers[0].price == 2.49
    assert offers[0].unit_price == 2.49
    assert offers[0].confidence == "exact"
    assert offers[0].url == "https://www.auchan.fr/p/riz-basmati"
    assert calls[0][:3] == ["managed-browser", "navigate", "--url"]
    assert calls[1][:4] == ["managed-browser", "console", "eval", "--expression"]
    assert "--tab-id" in calls[1]
    assert calls[1][calls[1].index("--tab-id") + 1] == "tab-riz"


def test_collect_drive_offers_sorts_leclerc_by_unit_price_after_equivalence_filter() -> None:
    calls: list[list[str]] = []
    payloads = [
        {"tabId": "tab-emmental"},
        {
            "items": [
                {
                    "title": "Emmental Président 400g",
                    "price": "4,41 €",
                    "unitPrice": "11,03 € / kg",
                    "url": "#",
                },
                {
                    "title": "Emmental fromage rapé Les Croisés - 1kg",
                    "price": "7,89 €",
                    "unitPrice": "7,89 € / kg",
                    "url": "#",
                },
                {
                    "title": "Fromage Gruyère Suisse 250g",
                    "price": "9,95 €",
                    "unitPrice": "39,80 € / kg",
                    "url": "#",
                },
            ]
        },
    ]

    def fake_runner(
        args: list[str], *, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=json.dumps(payloads.pop(0))
        )

    client = ManagedBrowserClient(
        command="managed-browser", profile="courses", site="leclerc", runner=fake_runner
    )

    offers = collect_drive_offers([ShoppingItem(name="emmental")], "leclerc", client, max_results=2)

    assert [offer.product for offer in offers] == [
        "Emmental fromage rapé Les Croisés - 1kg",
        "Emmental Président 400g",
    ]
    assert calls[0][calls[0].index("--url") + 1].endswith("TexteRecherche=emmental&tri=2")


def test_collect_drive_offers_uses_leclerc_unit_price_sort_for_weighted_items() -> None:
    calls: list[list[str]] = []
    payloads = [
        {"tabId": "tab-carottes"},
        {
            "items": [
                {
                    "title": "Carottes filière Panier du Primeur - 1kg",
                    "price": "1,59 €",
                    "unitPrice": "1,59 € / kg",
                    "url": "#",
                }
            ]
        },
    ]

    def fake_runner(
        args: list[str], *, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=json.dumps(payloads.pop(0))
        )

    client = ManagedBrowserClient(
        command="managed-browser", profile="courses", site="leclerc", runner=fake_runner
    )

    offers = collect_drive_offers(
        [ShoppingItem(name="carottes", quantity=1, unit="kg")],
        "leclerc",
        client,
        max_results=1,
    )

    assert [offer.product for offer in offers] == ["Carottes filière Panier du Primeur - 1kg"]
    assert calls[0][calls[0].index("--url") + 1].endswith("TexteRecherche=carottes&tri=4")


def test_collect_drive_offers_rejects_non_strict_leclerc_substitutions() -> None:
    payload_by_query = {
        "quinoa": [
            {
                "title": "Duo céréales Comptoir du Grain Quinoa et Boulgour - 400g",
                "price": "2,05 €",
                "unitPrice": "5,13 € / kg",
                "url": "#",
            },
            {
                "title": "Quinoa Céréal Bio Bio repas express 220g",
                "price": "1,85 €",
                "unitPrice": "8,41 € / kg",
                "url": "#",
            },
        ],
        "thon+nature": [
            {
                "title": "Thon entier MSC Pêche Océan A l'huile - 104g",
                "price": "1,20 €",
                "unitPrice": "11,54 € / kg",
                "url": "#",
            },
            {
                "title": "Thon entier Pêche Océan Naturel - 93g",
                "price": "1,39 €",
                "unitPrice": "14,95 € / kg",
                "url": "#",
            },
        ],
    }
    current_query = ""

    def fake_runner(
        args: list[str], *, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        nonlocal current_query
        if args[:2] == ["managed-browser", "navigate"]:
            url = args[args.index("--url") + 1]
            current_query = url.split("TexteRecherche=", 1)[1].split("&", 1)[0]
            current_query = "thon+nature" if current_query == "thon" else current_query
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=json.dumps({"tabId": current_query})
            )
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps({"items": payload_by_query[current_query]}),
        )

    client = ManagedBrowserClient(
        command="managed-browser", profile="courses", site="leclerc", runner=fake_runner
    )

    offers = collect_drive_offers(
        [ShoppingItem(name="quinoa"), ShoppingItem(name="thon nature")],
        "leclerc",
        client,
        max_results=1,
        products={"thon nature": DriveProduct(name="thon nature")},
        catalog=None,
    )

    assert [offer.product for offer in offers] == [
        "Quinoa Céréal Bio Bio repas express 220g",
        "Thon entier Pêche Océan Naturel - 93g",
    ]


def test_collect_drive_offers_drops_strictly_excluded_candidates_when_no_fallback() -> None:
    payload_by_query = {
        "thon+nature": [
            {
                "title": "Thon entier MSC Pêche Océan A l'huile - 104g",
                "price": "1,20 €",
                "unitPrice": "11,54 € / kg",
                "url": "#",
            }
        ],
    }
    current_query = ""

    def fake_runner(
        args: list[str], *, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        nonlocal current_query
        if args[:2] == ["managed-browser", "navigate"]:
            url = args[args.index("--url") + 1]
            current_query = url.split("TexteRecherche=", 1)[1].split("&", 1)[0]
            current_query = "thon+nature" if current_query == "thon" else current_query
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=json.dumps({"tabId": current_query})
            )
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps({"items": payload_by_query[current_query]}),
        )

    client = ManagedBrowserClient(
        command="managed-browser", profile="courses", site="leclerc", runner=fake_runner
    )

    offers = collect_drive_offers(
        [ShoppingItem(name="thon nature")],
        "leclerc",
        client,
        max_results=1,
        products={"thon nature": DriveProduct(name="thon nature")},
        catalog=None,
    )

    assert offers == []


def test_collect_drive_offers_uses_nested_tab_id_from_managed_browser_navigation() -> None:
    calls: list[list[str]] = []
    payloads = [
        {"result": {"value": {"tabId": "tab-riz"}}},
        {
            "items": [
                {
                    "title": "Riz basmati 1kg",
                    "price": "2,49 €",
                    "unitPrice": "2,49 € / kg",
                    "url": "#",
                }
            ]
        },
        {"result": {"value": {"tabId": "tab-carottes"}}},
        {
            "items": [
                {
                    "title": "Carottes 1kg",
                    "price": "1,29 €",
                    "unitPrice": "1,29 € / kg",
                    "url": "#",
                }
            ]
        },
    ]

    def fake_runner(
        args: list[str], *, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=json.dumps(payloads.pop(0))
        )

    client = ManagedBrowserClient(
        command="managed-browser", profile="courses", site="leclerc", runner=fake_runner
    )
    offers = collect_drive_offers(
        [ShoppingItem(name="riz"), ShoppingItem(name="carottes")],
        "leclerc",
        client,
        max_results=1,
    )

    assert [offer.product for offer in offers] == ["Riz basmati 1kg", "Carottes 1kg"]
    console_calls = [call for call in calls if call[:3] == ["managed-browser", "console", "eval"]]
    assert [call[call.index("--tab-id") + 1] for call in console_calls] == [
        "tab-riz",
        "tab-carottes",
    ]


def test_collect_drive_offers_handles_managed_browser_result_value_wrapper() -> None:
    payloads = [
        {"result": {"value": {"tabId": "tab-emmental"}}},
        {
            "result": {
                "value": {
                    "items": [
                        {
                            "title": "Emmental fromage rapé Les Croisés - 500g",
                            "price": "3,99 €",
                            "unitPrice": "7,98 € / kg",
                            "url": "",
                        }
                    ]
                }
            }
        },
    ]

    def fake_runner(
        args: list[str], *, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=json.dumps(payloads.pop(0))
        )

    client = ManagedBrowserClient(
        command="managed-browser", profile="courses", site="leclerc", runner=fake_runner
    )

    offers = collect_drive_offers([ShoppingItem(name="emmental")], "leclerc", client, max_results=1)

    assert [offer.product for offer in offers] == ["Emmental fromage rapé Les Croisés - 500g"]
    assert offers[0].price == 3.99
    assert offers[0].unit_price == 7.98
    assert offers[0].url is None


def test_drive_cli_collect_writes_offers_yaml(tmp_path: Path) -> None:
    shopping = tmp_path / "shopping.yaml"
    shopping.write_text("items:\n  - name: riz\n", encoding="utf-8")
    output = tmp_path / "offers.yaml"
    recorder = tmp_path / "fake_browser.py"
    recorder.write_text(
        """
import json
import sys
if sys.argv[1:3] == ['navigate', '--url']:
    print(json.dumps({'tabId': 'tab-riz'}))
elif sys.argv[1:3] == ['console', 'eval']:
    print(json.dumps({
        'items': [{
            'title': 'Riz basmati 1kg',
            'price': '2,49 €',
            'unitPrice': '2,49 €/kg',
            'url': '/p/riz',
        }]
    }))
else:
    print(json.dumps({}))
""".strip(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "drive",
            "collect",
            str(shopping),
            "--drive",
            "auchan",
            "--browser-command",
            f"python {recorder}",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0
    assert "Offres collectées: 1" in result.output
    data = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert data == {
        "offers": [
            {
                "store": "auchan",
                "item": "riz",
                "product": "Riz basmati 1kg",
                "price": 2.49,
                "unit_price": 2.49,
                "confidence": "exact",
                "url": "https://www.auchan.fr/p/riz",
            }
        ]
    }


def test_profile_recipe_feedback_prioritizes_accepted_and_hides_rejected(tmp_path: Path) -> None:
    (tmp_path / "recipes.yaml").write_text(
        """
- name: Chili
  ingredients:
    - name: haricots rouges
- name: Pâtes
  ingredients:
    - name: pâtes
- name: Soupe
  ingredients:
    - name: carotte
""",
        encoding="utf-8",
    )
    runner = CliRunner()

    accept = runner.invoke(
        app, ["profile", "accept-recipe", "add", "Pâtes", "--data-dir", str(tmp_path)]
    )
    reject = runner.invoke(
        app, ["profile", "reject-recipe", "add", "Soupe", "--data-dir", str(tmp_path)]
    )
    result = runner.invoke(app, ["plan", "--meals", "3", "--data-dir", str(tmp_path)])

    assert accept.exit_code == 0
    assert reject.exit_code == 0
    assert result.exit_code == 0
    assert "- Pâtes" in result.output
    assert "- Chili" in result.output
    assert "Soupe" not in result.output
    assert result.output.index("- Pâtes") < result.output.index("- Chili")


def test_week_outputs_balanced_week_and_recommendation(tmp_path: Path) -> None:
    (tmp_path / "recipes.yaml").write_text(
        """
- name: Bowl riz poulet
  ingredients:
    - name: riz
      quantity: 100
      unit: g
    - name: poulet
      quantity: 120
      unit: g
    - name: brocoli
      quantity: 1
      unit: pièce
- name: Chili sin carne
  ingredients:
    - name: haricots rouges
      quantity: 400
      unit: g
    - name: riz
      quantity: 100
      unit: g
    - name: tomates concassées
      quantity: 1
      unit: boîte
- name: Gratin riche
  ingredients:
    - name: crème
      quantity: 20
      unit: cl
    - name: emmental râpé
      quantity: 200
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
    price: 2.00
    unit_price: 2.00
  - store: leclerc
    item: poulet
    product: Poulet 1kg
    price: 8.00
    unit_price: 8.00
  - store: leclerc
    item: brocoli
    product: Brocoli pièce
    price: 1.50
    unit_price: 1.50
  - store: leclerc
    item: haricots rouges
    product: Haricots rouges
    price: 1.20
    unit_price: 3.00
  - store: leclerc
    item: tomates concassées
    product: Tomates concassées
    price: 1.10
    unit_price: 2.75
""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app, ["week", "--meals", "2", "--prices", str(prices), "--data-dir", str(tmp_path)]
    )

    assert result.exit_code == 0
    assert "Semaine:" in result.output
    assert "Bowl riz poulet — équilibre" in result.output
    assert "Chili sin carne — équilibre" in result.output
    assert "Gratin riche" not in result.output
    assert "À acheter:" in result.output
    assert "Recommandation achat:" in result.output


def test_cart_lines_group_recommendation_by_store() -> None:
    grouped = cart_lines_from_recommendation(
        {
            "riz": StoreOffer(
                store="leclerc",
                item="riz",
                product="Riz 1kg",
                price=2.0,
                url="https://l/riz",
            ),
            "tomates": StoreOffer(store="auchan", item="tomates", product="Tomates", price=1.5),
        }
    )

    assert list(grouped) == ["auchan", "leclerc"]
    assert grouped["leclerc"] == [
        CartLine(
            store="leclerc",
            item="riz",
            product="Riz 1kg",
            quantity=1,
            url="https://l/riz",
            search_url=store_search_url("leclerc", "Riz 1kg"),
        )
    ]
    assert (
        cart_items_param(grouped["leclerc"]) == "riz|Riz 1kg|1|https://l/riz|"
        "https://fd2-courses.leclercdrive.fr/magasin-027419-027419-Viuz-en-Sallaz/"
        "recherche.aspx?TexteRecherche=Riz+1kg&tri=1|offer_collected"
    )


def test_managed_browser_client_runs_flow_with_params_and_side_effect_policy() -> None:
    calls: list[list[str]] = []

    def fake_runner(
        args: list[str], *, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=json.dumps({"ok": True}))

    client = ManagedBrowserClient(
        command="managed-browser", profile="courses", site="leclerc", runner=fake_runner
    )
    result = client.flow_run(
        "add-cart-leclerc",
        params={"itemsB64": "cml6fFJpenwxfA==", "dryRunB64": "dHJ1ZQ=="},
        max_side_effect_level="read_only",
    )

    assert result.data == {"ok": True}
    assert calls == [
        [
            "managed-browser",
            "flow",
            "run",
            "add-cart-leclerc",
            "--param",
            "itemsB64=cml6fFJpenwxfA==",
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
    ]


def test_cart_lines_use_live_leclerc_drive_search_url_even_when_offer_url_is_blocked() -> None:
    lines = cart_lines_from_recommendation(
        {
            "riz": StoreOffer(
                store="leclerc",
                item="riz",
                product="Riz long Comptoir du Grain",
                price=1.67,
                url="https://fd2-courses.leclercdrive.fr/magasin-027419-027419-Viuz-en-Sallaz/recherche.aspx?TexteRecherche=riz&tri=1#",
            )
        }
    )["leclerc"]

    assert lines[0].url.endswith("TexteRecherche=riz&tri=1#")
    assert lines[0].search_url == (
        "https://fd2-courses.leclercdrive.fr/magasin-027419-027419-Viuz-en-Sallaz/"
        "recherche.aspx?TexteRecherche=Riz+long+Comptoir+du+Grain&tri=1"
    )


def test_run_cart_flow_live_navigates_and_clicks_without_flow_replay(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_runner(
        args: list[str], *, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if "storage" in args and "checkpoint" in args:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=json.dumps({"result": {"value": {"checkpoint": "ok"}}}),
            )
        if "navigate" in args:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=json.dumps({"result": {"value": {"tabId": "tab-live"}}}),
            )
        if "console" in args:
            expression = args[args.index("--expression") + 1]
            assert 'dryRun": false' in expression
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
                                "url": "https://l/riz",
                                "button_label": "Ajouter au panier",
                            }
                        }
                    }
                ),
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
                url="https://l/riz",
                search_url="https://l/riz",
            )
        ],
        profile="courses",
        browser_command="managed-browser",
        dry_run=False,
    )

    value = result.data
    assert value["dryRun"] is False
    assert len(value["catalog_found"]) == 1
    assert len(value["addable"]) == 1
    assert len(value["inserted"]) == 1
    assert [call[1:3] for call in calls] == [
        ["storage", "checkpoint"],
        ["navigate", "--url"],
        ["console", "eval"],
    ]
    assert "before-live-cart-leclerc" in calls[0]
    assert all("flow" not in call for call in calls)


def test_run_cart_flow_live_uses_auchan_specific_add_expression(monkeypatch) -> None:
    expressions: list[str] = []

    def fake_runner(
        args: list[str], *, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        if "storage" in args and "checkpoint" in args:
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=json.dumps({"ok": True})
            )
        if "navigate" in args:
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=json.dumps({"ok": True})
            )
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
                                "url": "https://www.auchan.fr/recherche?text=tomates",
                                "button_label": (
                                    "Ajouter 1 quantité de Tomates rondes prix bas 1kg au panier"
                                ),
                            }
                        }
                    }
                ),
            )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="{}")

    def fake_init(self, **kwargs):
        self.command = kwargs.get("command")
        self.profile = kwargs.get("profile", "courses-auchan")
        self.site = kwargs.get("site", "auchan")
        self.runner = fake_runner

    monkeypatch.setattr(cli.ManagedBrowserClient, "__init__", fake_init)
    result = cli.run_cart_flow_for_store(
        "auchan",
        [
            CartLine(
                store="auchan",
                item="tomates",
                product="Tomates rondes prix bas",
                search_url="https://www.auchan.fr/recherche?text=tomates",
            )
        ],
        profile="courses",
        browser_command="managed-browser",
        dry_run=False,
    )

    assert len(result.data["inserted"]) == 1
    assert expressions
    assert ".product-thumbnail" in expressions[0]
    assert "Supprimer" not in expressions[0]
    assert "Tomates rondes prix bas" in expressions[0]


def test_run_cart_flow_live_falls_back_from_offer_url_to_search_url(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_runner(
        args: list[str], *, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if "storage" in args and "checkpoint" in args:
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=json.dumps({"ok": True})
            )
        if "navigate" in args:
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=json.dumps({"ok": True})
            )
        if "console" in args:
            navigate_count = sum(1 for call in calls if "navigate" in call)
            found = navigate_count == 2
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=json.dumps(
                    {
                        "result": {
                            "value": {
                                "catalog_found": found,
                                "addable": found,
                                "inserted": found,
                                "url": "https://leclerc/search"
                                if found
                                else "https://leclerc/blocked",
                                "button_label": "Ajouter au panier" if found else "",
                            }
                        }
                    }
                ),
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
                url="https://leclerc/blocked",
                search_url="https://leclerc/search",
            )
        ],
        profile="courses",
        browser_command="managed-browser",
        dry_run=False,
    )

    value = result.data
    assert len(value["catalog_found"]) == 1
    assert len(value["addable"]) == 1
    assert len(value["inserted"]) == 1
    navigate_urls = [call[call.index("--url") + 1] for call in calls if "navigate" in call]
    assert navigate_urls == ["https://leclerc/blocked", "https://leclerc/search"]
    assert all("flow" not in call for call in calls)


def test_run_cart_remove_flow_live_uses_auchan_specific_remove_expression(monkeypatch) -> None:
    expressions: list[str] = []
    calls: list[list[str]] = []

    def fake_runner(
        args: list[str], *, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if "storage" in args and "checkpoint" in args:
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=json.dumps({"ok": True})
            )
        if "navigate" in args:
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=json.dumps({"ok": True})
            )
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
                                "url": "https://www.auchan.fr/recherche?text=tomates",
                                "button_label": (
                                    "Supprimer 1 quantité de Tomates rondes prix bas 1kg du panier"
                                ),
                            }
                        }
                    }
                ),
            )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="{}")

    def fake_init(self, **kwargs):
        self.command = kwargs.get("command")
        self.profile = kwargs.get("profile", "courses-auchan")
        self.site = kwargs.get("site", "auchan")
        self.runner = fake_runner

    monkeypatch.setattr(cli.ManagedBrowserClient, "__init__", fake_init)
    result = cli.run_cart_remove_flow_for_store(
        "auchan",
        [
            CartLine(
                store="auchan",
                item="tomates",
                product="Tomates rondes prix bas",
                search_url="https://www.auchan.fr/recherche?text=tomates",
            )
        ],
        profile="courses",
        browser_command="managed-browser",
        dry_run=False,
    )

    assert len(result.data["removed"]) == 1
    assert expressions
    assert ".product-thumbnail" in expressions[0]
    assert "Ajouter" not in expressions[0]
    assert "Tomates rondes prix bas" in expressions[0]
    assert "before-live-cart-auchan-remove" in calls[0]
    assert all("flow" not in call for call in calls)


def test_plan_add_to_cart_runs_store_flows_in_dry_run(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "recipes.yaml").write_text(
        """
- name: test riz tomates
  ingredients:
    - name: riz
    - name: tomates
  tags: [budget]
""",
        encoding="utf-8",
    )
    (tmp_path / "profile.yaml").write_text("{}\n", encoding="utf-8")
    prices = tmp_path / "prices.yaml"
    prices.write_text(
        """
offers:
  - store: leclerc
    item: riz
    product: Riz Leclerc
    price: 2.0
  - store: auchan
    item: tomates
    product: Tomates Auchan
    price: 1.0
  - store: leclerc
    item: tomates
    product: Tomates Leclerc
    price: 2.0
  - store: auchan
    item: riz
    product: Riz Auchan
    price: 3.0
""",
        encoding="utf-8",
    )
    calls: list[tuple[str, list[CartLine], bool]] = []

    def fake_run_cart_flow_for_store(store, lines, *, profile, browser_command, dry_run):
        calls.append((store, lines, dry_run))
        return cli.BrowserCommandResult(
            action="flow",
            data={
                "result": {
                    "results": [
                        {
                            "result": {
                                "value": {
                                    "ok": True,
                                    "catalog_found": [line.product for line in lines],
                                    "addable": [],
                                    "inserted": [],
                                    "message": f"Dry-run {store}",
                                }
                            }
                        }
                    ]
                }
            },
        )

    monkeypatch.setattr(cli, "run_cart_flow_for_store", fake_run_cart_flow_for_store)

    result = CliRunner().invoke(
        app,
        [
            "plan",
            "--data-dir",
            str(tmp_path),
            "--prices",
            str(prices),
            "--no-pantry",
            "--add-to-cart",
            "--cart-dry-run",
            "--mode",
            "economic",
            "--max-stores",
            "2",
        ],
    )

    assert result.exit_code == 0
    assert "Paniers à préparer:" in result.output
    assert "Produits trouvés/catalogue: 1" in result.output
    assert "Produits ajoutables/disponibles: 0" in result.output
    assert "Produits effectivement insérés: 0" in result.output
    assert calls == [
        (
            "auchan",
            [
                CartLine(
                    store="auchan",
                    item="tomates",
                    product="Tomates Auchan",
                    quantity=1,
                    url=None,
                    search_url="https://www.auchan.fr/recherche?text=Tomates+Auchan",
                )
            ],
            True,
        ),
        (
            "leclerc",
            [
                CartLine(
                    store="leclerc",
                    item="riz",
                    product="Riz Leclerc",
                    quantity=1,
                    url=None,
                    search_url=(
                        "https://fd2-courses.leclercdrive.fr/magasin-027419-027419-Viuz-en-Sallaz/"
                        "recherche.aspx?TexteRecherche=Riz+Leclerc&tri=1"
                    ),
                )
            ],
            True,
        ),
    ]


def test_plan_remove_from_cart_runs_remove_flows_in_dry_run(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "recipes.yaml").write_text(
        """
- name: test tomates
  ingredients:
    - name: tomates
  tags: [budget]
""",
        encoding="utf-8",
    )
    (tmp_path / "profile.yaml").write_text("{}\n", encoding="utf-8")
    prices = tmp_path / "prices.yaml"
    prices.write_text(
        """
offers:
  - store: auchan
    item: tomates
    product: Tomates Auchan
    price: 1.0
""",
        encoding="utf-8",
    )
    calls: list[tuple[str, list[CartLine], bool]] = []

    def fake_run_cart_remove_flow_for_store(store, lines, *, profile, browser_command, dry_run):
        calls.append((store, lines, dry_run))
        return cli.BrowserCommandResult(
            action="flow",
            data={
                "ok": True,
                "catalog_found": [{"product": line.product} for line in lines],
                "removable": [],
                "removed": [],
                "message": f"Dry-run remove {store}",
            },
        )

    monkeypatch.setattr(cli, "run_cart_remove_flow_for_store", fake_run_cart_remove_flow_for_store)

    result = CliRunner().invoke(
        app,
        [
            "plan",
            "--data-dir",
            str(tmp_path),
            "--prices",
            str(prices),
            "--no-pantry",
            "--remove-from-cart",
            "--cart-dry-run",
            "--mode",
            "simple",
        ],
    )

    assert result.exit_code == 0
    assert "Paniers à retirer:" in result.output
    assert "Produits trouvés/catalogue: 1" in result.output
    assert "Produits retirables/disponibles: 0" in result.output
    assert "Produits effectivement retirés: 0" in result.output
    assert calls == [
        (
            "auchan",
            [
                CartLine(
                    store="auchan",
                    item="tomates",
                    product="Tomates Auchan",
                    quantity=1,
                    url=None,
                    search_url="https://www.auchan.fr/recherche?text=Tomates+Auchan",
                )
            ],
            True,
        ),
    ]


def test_cart_status_expression_is_read_only() -> None:
    forbidden = [".click(", ".submit(", "fetch(", "XMLHttpRequest", "localStorage.setItem"]
    for token in forbidden:
        assert token not in CART_STATUS_EVAL_JS
    assert "read_only_cart_status" in CART_STATUS_EVAL_JS


def test_cart_sync_diff_classifies_keep_add_remove_and_quantity() -> None:
    desired = [
        CartLine(store="auchan", item="riz", product="Riz basmati", quantity=1),
        CartLine(store="auchan", item="lait", product="Lait demi écrémé", quantity=2),
        CartLine(store="auchan", item="tomates", product="Tomates", quantity=1),
    ]
    status = {
        "url": "https://www.auchan.fr/panier",
        "actual_lines": [
            {"title": "Riz basmati", "quantity": 1},
            {"title": "Lait demi écrémé", "quantity": 1},
            {"title": "Chips", "quantity": 1},
        ],
        "expected_matches": [
            {
                "expected_index": 0,
                "matched": True,
                "confidence": "high",
                "actual_index": 0,
                "actual_quantity": 1,
            },
            {
                "expected_index": 1,
                "matched": True,
                "confidence": "high",
                "actual_index": 1,
                "actual_quantity": 1,
            },
            {"expected_index": 2, "matched": False, "confidence": "none", "actual_index": None},
        ],
    }
    diff = cart_sync_diff("auchan", desired, status)
    assert diff["summary"] == {
        "desired_count": 3,
        "actual_count": 3,
        "to_add_count": 1,
        "to_remove_count": 1,
        "to_update_quantity_count": 1,
        "unchanged_count": 1,
        "ambiguous_count": 0,
        "blocked": False,
    }
    assert [operation["op"] for operation in diff["operations"]] == [
        "keep",
        "update_quantity",
        "add",
        "remove",
    ]


def test_plan_add_to_cart_persists_cart_run(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "recipes.yaml").write_text(
        """
- name: test riz
  ingredients:
    - name: riz
  tags: [budget]
""",
        encoding="utf-8",
    )
    (tmp_path / "profile.yaml").write_text("{}\n", encoding="utf-8")
    prices = tmp_path / "prices.yaml"
    prices.write_text(
        """
offers:
  - store: auchan
    item: riz
    product: Riz Auchan
    price: 1.0
""",
        encoding="utf-8",
    )

    def fake_run_cart_flow_for_store(store, lines, *, profile, browser_command, dry_run):
        return cli.BrowserCommandResult(
            action="flow",
            data={
                "ok": True,
                "catalog_found": [{"product": line.product} for line in lines],
                "addable": [],
                "inserted": [],
                "message": f"Dry-run {store}",
            },
        )

    monkeypatch.setattr(cli, "run_cart_flow_for_store", fake_run_cart_flow_for_store)
    result = CliRunner().invoke(
        app,
        [
            "plan",
            "--data-dir",
            str(tmp_path),
            "--prices",
            str(prices),
            "--no-pantry",
            "--add-to-cart",
            "--cart-dry-run",
            "--mode",
            "simple",
        ],
    )
    assert result.exit_code == 0
    assert "Run panier sauvegardé:" in result.output
    latest = (tmp_path / "runs" / "latest.txt").read_text(encoding="utf-8").strip()
    payload = yaml.safe_load((tmp_path / "runs" / f"{latest}.yaml").read_text(encoding="utf-8"))
    assert payload["action"] == "add"
    assert payload["dry_run"] is True
    assert payload["grouped_lines"]["auchan"][0]["product"] == "Riz Auchan"
    assert payload["results"]["auchan"]["catalog_found"] == [{"product": "Riz Auchan"}]


def test_cart_status_reads_persisted_run(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "latest.txt").write_text("cart-test\n", encoding="utf-8")
    (runs / "cart-test.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "cart-test",
                "action": "remove",
                "dry_run": True,
                "created_at": "now",
                "grouped_lines": {
                    "auchan": [
                        {
                            "store": "auchan",
                            "item": "riz",
                            "product": "Riz Auchan",
                            "quantity": 1,
                            "status": "offer_collected",
                        }
                    ]
                },
                "results": {
                    "auchan": {
                        "catalog_found": [{"product": "Riz Auchan"}],
                        "removable": [],
                        "removed": [],
                    }
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["cart", "status", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "Dernier run panier: cart-test" in result.output
    assert "Action: remove" in result.output
    assert "Produits trouvés/catalogue: 1" in result.output
    assert "Produits effectivement retirés: 0" in result.output


def test_cart_sync_prints_read_only_diff(tmp_path: Path, monkeypatch) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "latest.txt").write_text("cart-sync\n", encoding="utf-8")
    (runs / "cart-sync.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "cart-sync",
                "action": "add",
                "dry_run": True,
                "created_at": "now",
                "grouped_lines": {
                    "auchan": [
                        {
                            "store": "auchan",
                            "item": "riz",
                            "product": "Riz Auchan",
                            "quantity": 1,
                            "status": "offer_collected",
                            "search_url": "https://www.auchan.fr/recherche?text=Riz+Auchan",
                        }
                    ]
                },
                "results": {},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    def fake_status(store, lines, *, profile, browser_command):
        assert store == "auchan"
        return {
            "url": "https://www.auchan.fr/panier",
            "counts": {"expected": 1, "actual_candidates": 0, "matched": 0},
            "actual_lines": [],
            "expected_matches": [{"expected_index": 0, "matched": False, "confidence": "none"}],
        }

    monkeypatch.setattr(cli, "run_cart_status_for_store", fake_status)
    result = CliRunner().invoke(app, ["cart", "sync", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "État panier auchan (read-only)." in result.output
    assert "Diff sync panier auchan (dry-run)." in result.output
    assert "À ajouter: 1" in result.output


def test_doctor_status_reports_json_summary(tmp_path: Path) -> None:
    (tmp_path / "profile.yaml").write_text("{}\n", encoding="utf-8")
    (tmp_path / "recipes.yaml").write_text(
        "- name: Riz test\n  ingredients:\n    - name: riz\n", encoding="utf-8"
    )
    (tmp_path / "pantry.yaml").write_text("items:\n  - name: riz\n", encoding="utf-8")

    result = CliRunner().invoke(
        app, ["doctor", "status", "--data-dir", str(tmp_path), "--format", "json"]
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["mode"] == "deterministic-first"
    assert payload["files"]["profile"]["present"] is True
    assert payload["files"]["recipes"]["count"] == 1
    assert payload["files"]["pantry"]["count"] == 1
    assert payload["next_actions"]


def test_init_creates_deterministic_starter_files_idempotently(tmp_path: Path) -> None:
    runner = CliRunner()

    first = runner.invoke(app, ["init", "--data-dir", str(tmp_path)])
    second = runner.invoke(app, ["init", "--data-dir", str(tmp_path)])

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert "Initialisation Panier" in first.output
    assert "déjà présent" in second.output
    assert (tmp_path / "profile.yaml").exists()
    assert (tmp_path / "recipes.yaml").exists()
    assert (tmp_path / "pantry.yaml").exists()
    assert "Bowl riz thon" in (tmp_path / "recipes.yaml").read_text(encoding="utf-8")


def test_cart_status_can_output_json(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "latest.txt").write_text("cart-json\n", encoding="utf-8")
    (runs / "cart-json.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "cart-json",
                "action": "add",
                "dry_run": True,
                "created_at": "now",
                "grouped_lines": {
                    "auchan": [
                        {
                            "store": "auchan",
                            "item": "riz",
                            "product": "Riz Auchan",
                            "quantity": 1,
                            "status": "offer_collected",
                        }
                    ]
                },
                "results": {
                    "auchan": {
                        "catalog_found": [{"product": "Riz Auchan"}],
                        "addable": [{"product": "Riz Auchan"}],
                        "inserted": [],
                    }
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app, ["cart", "status", "--data-dir", str(tmp_path), "--format", "json"]
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["id"] == "cart-json"
    assert payload["summary"] == {
        "stores": 1,
        "catalog_found": 1,
        "available": 1,
        "done": 0,
    }


def test_python_module_entrypoint_shows_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "panier", "--help"],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Planifie repas" in result.stdout


def test_cart_run_id_rejects_path_traversal(tmp_path: Path) -> None:
    try:
        cart_run_path(tmp_path, "../outside")
    except ValueError as exc:
        assert "invalide" in str(exc) or "hors dossier" in str(exc)
    else:
        raise AssertionError("path traversal run id should be rejected")

    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "latest.txt").write_text("../outside\n", encoding="utf-8")

    try:
        load_cart_run(tmp_path)
    except FileNotFoundError as exc:
        assert "run panier" in str(exc)
    else:
        raise AssertionError("latest path traversal run id should be rejected")


def test_new_cart_run_id_is_unique_and_safe() -> None:
    ids = {new_cart_run_id() for _ in range(20)}

    assert len(ids) == 20
    assert all("/" not in run_id and ".." not in run_id for run_id in ids)


def test_cart_status_uses_store_cart_url(monkeypatch) -> None:
    navigated: list[str] = []

    class FakeBrowser:
        def __init__(self, **kwargs: object) -> None:
            pass

        def navigate(self, url: str):
            navigated.append(url)

        def console_eval(self, expression: str):
            class Result:
                data = {"result": {"value": {"url": "cart", "counts": {}}}}

            return Result()

    monkeypatch.setattr(cli, "ManagedBrowserClient", FakeBrowser)

    status = cli.run_cart_status_for_store(
        "leclerc",
        [
            CartLine(
                store="leclerc",
                item="riz",
                product="Riz",
                search_url=store_search_url("leclerc", "riz"),
            )
        ],
        profile="courses",
        browser_command=None,
    )

    assert navigated == [store_cart_url("leclerc")]
    assert status["url"] == "cart"


def test_cart_run_path_rejects_path_traversal(tmp_path: Path) -> None:
    from panier.cart import cart_run_path, load_cart_run

    for bad in ["../secret", "subdir/run", "", "latest/../x"]:
        with pytest.raises(ValueError):
            cart_run_path(tmp_path, bad)

    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "latest.txt").write_text("../secret\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        load_cart_run(tmp_path, "latest")


def test_cart_add_remove_reject_opposite_run_action(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from panier.cart import CartRun, save_cart_run

    data_dir = tmp_path / "data"
    save_cart_run(
        data_dir,
        CartRun(
            id="cart-remove-run",
            action="remove",
            dry_run=True,
            grouped_lines={},
            results={},
            created_at="now",
        ),
    )
    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["cart", "add", "--data-dir", str(data_dir), "--run", "cart-remove-run"]
    )
    assert result.exit_code != 0
    assert "ce run est une suppression" in result.output

    save_cart_run(
        data_dir,
        CartRun(
            id="cart-add-run",
            action="add",
            dry_run=True,
            grouped_lines={},
            results={},
            created_at="now",
        ),
    )
    result = runner.invoke(
        cli.app, ["cart", "remove", "--data-dir", str(data_dir), "--run", "cart-add-run"]
    )
    assert result.exit_code != 0
    assert "ce run est un ajout" in result.output
