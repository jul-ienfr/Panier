from __future__ import annotations

from panier.drive import _is_strict_equivalent, _offer_from_browser_item, score_offer
from panier.models import ShoppingItem, fold_name


def _offer(item: str, product: str, *, price: float = 1.0):
    offer = _offer_from_browser_item(
        ShoppingItem(name=item), "leclerc", {"title": product, "price": price}
    )
    assert offer is not None
    return offer


def test_fold_name_strips_accents_but_normalize_name_keeps_them() -> None:
    assert fold_name("Pâtes") == "pates"
    assert fold_name("Crème fraîche") == "creme fraiche"
    assert fold_name("Œufs") == "oeufs"


def test_accented_product_matches_unaccented_item() -> None:
    item = ShoppingItem(name="pates")
    offer = _offer("pates", "Pâtes Eco+ Spaghetti 1kg")
    assert _is_strict_equivalent(item, offer, score_offer(item, offer).score)
    assert offer.confidence == "exact"


def test_baby_food_is_never_a_vegetable_substitute() -> None:
    offer = _offer("courgettes", "Petits pots bébé NATURNES Courgettes 4M+ 2x130g")
    assert offer.confidence == "low"


def test_prepared_dishes_are_excluded() -> None:
    for product in (
        "Soupe liquide brique Liebig Courgettes",
        "Purée de tomates Turini 500g",
        "Sauce pizza Turini aux tomates et origan",
        "Croquettes chat Tous mes amis Saumon thon",
        "Riz provençale Bonduelle Thon 320g",
        "Taboulé Cassegrain Courgettes et poivrons",
        "Risotto de courgettes Good Goût",
    ):
        assert _offer("courgettes", product).confidence == "low", product


def test_keyword_in_tail_is_not_the_main_product() -> None:
    offer = _offer("tomates", "Sachet cuisson Rustica Poulet paprika et tomates")
    assert offer.confidence == "low"


def test_plain_product_stays_exact() -> None:
    offer = _offer("tomates", "Tomates pelées Notre Jardin Entières 238g")
    assert offer.confidence == "exact"
