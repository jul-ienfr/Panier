import json
import subprocess
from pathlib import Path

import yaml
from typer.testing import CliRunner

import panier.cli as cli
from panier.cli import app
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
from panier.planner import consolidate_ingredients, recommend_basket, select_meals, subtract_pantry


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

    result = CliRunner().invoke(
        app, ["recipe", "score", "bowl test", "--data-dir", str(tmp_path)]
    )

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


def test_collect_drive_offers_keeps_leclerc_visible_order() -> None:
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
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=json.dumps(payloads.pop(0))
        )

    client = ManagedBrowserClient(
        command="managed-browser", profile="courses", site="leclerc", runner=fake_runner
    )

    offers = collect_drive_offers([ShoppingItem(name="emmental")], "leclerc", client, max_results=2)

    assert [offer.product for offer in offers] == [
        "Emmental Président 400g",
        "Emmental fromage rapé Les Croisés - 1kg",
    ]


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
