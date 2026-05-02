from panier.models import FoodProfile, PriceMode, Recipe, ShoppingItem, StoreOffer
from panier.planner import consolidate_ingredients, recommend_basket, select_meals


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
