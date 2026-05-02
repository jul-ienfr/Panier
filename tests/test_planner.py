import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from panier.cli import app
from panier.drive import (
    BrandType,
    DriveProduct,
    best_offer_for_item,
    build_drive_search_plan,
    build_drive_search_query,
    drive_search_url,
    open_drive_searches,
)
from panier.managed_browser import ManagedBrowserClient, ManagedBrowserError
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
        "https://www.e.leclerc/recherche?text=tomates+concass%C3%A9es"
    )


def test_drive_search_url_encodes_intermarche_and_auchan_queries() -> None:
    assert drive_search_url("intermarche", "tomates concassées") == (
        "https://www.intermarche.com/recherche/tomates+concass%C3%A9es"
    )
    assert drive_search_url("auchan", "coca cola") == (
        "https://www.auchan.fr/recherche?text=coca+cola"
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


def test_managed_browser_client_falls_back_to_camofox_http() -> None:
    def fake_runner(
        args: list[str], *, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args,
            returncode=1,
            stdout="",
            stderr="navigate failed (HTTP 500): Internal server error",
        )

    calls: list[tuple[str, str, dict | None]] = []

    def fake_http(method: str, path: str, payload: dict | None = None) -> dict:
        calls.append((method, path, payload))
        return {"tabId": "fallback-tab", "url": payload["url"] if payload else None}

    client = ManagedBrowserClient(
        command="managed-browser",
        profile="panier",
        site="leclerc",
        runner=fake_runner,
        http=fake_http,
    )

    result = client.navigate("https://www.e.leclerc/recherche?text=riz")

    assert result.action == "open"
    assert result.data["tabId"] == "fallback-tab"
    assert "wrapper_error" in result.data
    assert calls == [
        (
            "POST",
            "/tabs",
            {
                "userId": "panier",
                "sessionKey": "leclerc",
                "url": "https://www.e.leclerc/recherche?text=riz",
            },
        )
    ]


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
    assert calls[0][3] == "https://www.e.leclerc/recherche?text=riz"
    assert calls[1][3] == "https://www.e.leclerc/recherche?text=tomates"


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
  - store: leclerc
    item: tomates concassées
    product: Tomates fraîches 1kg
    price: 1.10
""",
        encoding="utf-8",
    )

    plan_result = runner.invoke(app, ["drive", "plan", str(shopping), "--drive", "leclerc"])
    pick_result = runner.invoke(app, ["drive", "pick", str(shopping), str(prices)])

    assert plan_result.exit_code == 0
    assert "Recherches à lancer:" in plan_result.output
    assert "- riz 150 g -> riz (low)" in plan_result.output
    assert pick_result.exit_code == 0
    assert "Meilleurs produits:" in pick_result.output
    assert "Tomates concassées 400g" in pick_result.output


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
