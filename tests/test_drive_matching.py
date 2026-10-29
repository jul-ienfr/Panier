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


def test_synonym_at_head_is_high_confidence() -> None:
    """« tomates concassées » ≈ « Pulpe de tomate » : bon produit, pas exact."""
    offer = _offer("tomates concassées", "Pulpe de tomate Turini Nature 390g")
    assert offer.confidence == "high"


def test_synonym_in_tail_is_still_rejected() -> None:
    offer = _offer("tomates concassées", "Plat cuisiné poulet pulpe de tomate 400g")
    assert offer.confidence == "low"


def test_dedupe_ignores_duplicate_product_ids() -> None:
    """Auchan expose le même produit sous deux IDs : une seule offre."""
    from panier.drive import _dedupe_offers
    from panier.models import StoreOffer

    offers = [
        StoreOffer(
            store="auchan",
            item="riz",
            product="AUCHAN Riz long grain incollable 1kg",
            price=1.83,
            unit_price=1.83,
            url="https://www.auchan.fr/auchan-riz-long-grain-incollable/pr-C1240700",
        ),
        StoreOffer(
            store="auchan",
            item="riz",
            product="AUCHAN Riz long grain incollable 1kg",
            price=1.83,
            unit_price=1.83,
            url="https://www.auchan.fr/auchan-riz-long-grain-incollable/pr-C1224692",
        ),
    ]
    assert len(_dedupe_offers(offers)) == 1


def test_dedupe_keeps_promo_variant_when_duplicates_differ() -> None:
    """Deux cartes identiques, une avec badge promo : on garde le badge."""
    from panier.drive import _dedupe_offers
    from panier.models import StoreOffer

    plain = StoreOffer(store="carrefour", item="tomates", product="Tomates grappe", price=3.59)
    badged = StoreOffer(
        store="carrefour",
        item="tomates",
        product="Tomates grappe",
        price=3.59,
        promo=True,
        promo_label="-10% Club",
    )

    assert [offer.promo for offer in _dedupe_offers([plain, badged])] == [True]
    assert [offer.promo for offer in _dedupe_offers([badged, plain])] == [True]


def test_parse_euro_price_handles_every_observed_drive_format() -> None:
    """Verrou anti-régression : les prix Carrefour/Super U étaient tronqués.

    Carrefour sépare les centimes par des espaces (« 3  ,59  € ») et Super U
    annonce parfois le prix en toutes lettres ; l'ancien parseur ne prenait que
    le premier groupe de chiffres, ce qui ramenait 100 % des prix Carrefour à
    leur partie entière et faussait toutes les comparaisons.
    """
    from panier.drive import _parse_euro_price

    assert _parse_euro_price("1,99 €") == 1.99
    assert _parse_euro_price("3  ,59  €") == 3.59
    assert _parse_euro_price("0  ,99  €") == 0.99
    assert _parse_euro_price("3.59 € / KG") == 3.59
    assert _parse_euro_price("3 euros 84 centimes") == 3.84
    assert _parse_euro_price("3 euros 84 centimes\n12 euros 80 le kilogramme\n3,84 €") == 3.84
    assert _parse_euro_price("1€99") == 1.99
    assert _parse_euro_price("0€89") == 0.89
    assert _parse_euro_price("1 234,56 €") == 1234.56
    assert _parse_euro_price("12,5 €") == 12.5
    assert _parse_euro_price("12€") == 12.0
    assert _parse_euro_price(2.49) == 2.49
    assert _parse_euro_price("0,00 €") is None
    assert _parse_euro_price(None) is None
    assert _parse_euro_price("   ") is None


def test_promo_badge_from_drive_is_propagated() -> None:
    """Le badge promo du DOM est conservé sur l'offre (source fiable)."""
    offer = _offer_from_browser_item(
        ShoppingItem(name="tomates"),
        "carrefour",
        {
            "title": "Tomates rondes en grappe vrac",
            "price": "3  ,59  €",
            "promo": True,
            "promoLabel": "-10% Club",
        },
    )

    assert offer is not None
    assert offer.price == 3.59
    assert offer.promo is True
    assert offer.promo_label == "-10% Club"


def test_no_promo_badge_keeps_offer_clean() -> None:
    offer = _offer("riz", "Riz long grain 1kg")

    assert offer.promo is False
    assert offer.promo_label is None
