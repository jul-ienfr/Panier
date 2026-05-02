from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations

from panier.models import (
    FoodProfile,
    Pantry,
    PriceMode,
    Recipe,
    ShoppingItem,
    StoreOffer,
    normalize_name,
)


@dataclass(frozen=True)
class BasketRecommendation:
    mode: PriceMode
    stores: tuple[str, ...]
    total: float
    by_item: dict[str, StoreOffer]
    savings_vs_best_single: float
    reason: str


def compatible_recipes(recipes: list[Recipe], profile: FoodProfile) -> list[Recipe]:
    return [recipe for recipe in recipes if not recipe.conflicts(profile)]


def select_meals(recipes: list[Recipe], profile: FoodProfile, meals: int) -> list[Recipe]:
    compatible = compatible_recipes(recipes, profile)
    return compatible[:meals]


def consolidate_ingredients(recipes: list[Recipe]) -> list[ShoppingItem]:
    quantities: dict[tuple[str, str | None], float] = defaultdict(float)
    has_quantity: dict[tuple[str, str | None], bool] = defaultdict(bool)

    for recipe in recipes:
        for ingredient in recipe.ingredients:
            key = (normalize_name(ingredient.name), ingredient.unit)
            if ingredient.quantity is None:
                quantities[key] += 1
            else:
                quantities[key] += float(ingredient.quantity)
                has_quantity[key] = True

    return _items_from_quantities(quantities, has_quantity)


def subtract_pantry(items: list[ShoppingItem], pantry: Pantry) -> list[ShoppingItem]:
    pantry_quantities: dict[tuple[str, str | None], float] = defaultdict(float)
    pantry_unknown_quantity: set[tuple[str, str | None]] = set()

    for item in pantry.items:
        key = (normalize_name(item.name), item.unit)
        if item.quantity is None:
            pantry_unknown_quantity.add(key)
        else:
            pantry_quantities[key] += float(item.quantity)

    remaining: list[ShoppingItem] = []
    for item in items:
        key = (normalize_name(item.name), item.unit)
        if item.quantity is None:
            if key not in pantry_unknown_quantity and pantry_quantities[key] <= 0:
                remaining.append(item)
            continue

        missing_quantity = float(item.quantity) - pantry_quantities[key]
        if missing_quantity > 0:
            remaining.append(
                ShoppingItem(name=item.name, quantity=missing_quantity, unit=item.unit)
            )

    return remaining


def _items_from_quantities(
    quantities: dict[tuple[str, str | None], float],
    has_quantity: dict[tuple[str, str | None], bool],
) -> list[ShoppingItem]:
    return [
        ShoppingItem(
            name=name,
            quantity=quantity if has_quantity[(name, unit)] else None,
            unit=unit,
        )
        for (name, unit), quantity in sorted(quantities.items())
    ]


def recommend_basket(
    items: list[ShoppingItem],
    offers: list[StoreOffer],
    mode: PriceMode = PriceMode.HYBRID,
    max_stores: int = 2,
    split_min_savings_eur: float = 8.0,
    split_min_savings_percent: float = 10.0,
) -> BasketRecommendation:
    if max_stores < 1:
        raise ValueError("max_stores must be >= 1")

    requested_items = [item.name for item in items]
    offers_by_item: dict[str, list[StoreOffer]] = defaultdict(list)
    stores = sorted({offer.store for offer in offers})

    for offer in offers:
        offers_by_item[offer.item].append(offer)

    missing = [item for item in requested_items if not offers_by_item[item]]
    if missing:
        raise ValueError(f"missing offers for: {', '.join(missing)}")

    best_single = _best_for_store_sets(
        requested_items,
        offers_by_item,
        [(store,) for store in stores],
    )
    if best_single is None:
        raise ValueError("no single store can satisfy the full basket")

    if mode == PriceMode.SIMPLE or max_stores == 1:
        return BasketRecommendation(
            mode=mode,
            stores=best_single[0],
            total=best_single[1],
            by_item=best_single[2],
            savings_vs_best_single=0.0,
            reason="Panier simple sur un seul drive.",
        )

    store_sets = []
    for count in range(1, min(max_stores, len(stores)) + 1):
        store_sets.extend(combinations(stores, count))

    best_split = _best_for_store_sets(requested_items, offers_by_item, store_sets)
    if best_split is None:
        raise ValueError("no store combination can satisfy the full basket")

    savings = best_single[1] - best_split[1]
    savings_percent = (savings / best_single[1]) * 100 if best_single[1] else 0.0

    if mode == PriceMode.HYBRID and len(best_split[0]) > 1:
        if savings < split_min_savings_eur and savings_percent < split_min_savings_percent:
            return BasketRecommendation(
                mode=mode,
                stores=best_single[0],
                total=best_single[1],
                by_item=best_single[2],
                savings_vs_best_single=0.0,
                reason=(
                    "Split non recommandé : économie trop faible "
                    f"({savings:.2f} €, {savings_percent:.1f} %)."
                ),
            )

    return BasketRecommendation(
        mode=mode,
        stores=best_split[0],
        total=best_split[1],
        by_item=best_split[2],
        savings_vs_best_single=savings,
        reason="Meilleur panier selon les contraintes demandées.",
    )


def _best_for_store_sets(
    requested_items: list[str],
    offers_by_item: dict[str, list[StoreOffer]],
    store_sets: list[tuple[str, ...]],
) -> tuple[tuple[str, ...], float, dict[str, StoreOffer]] | None:
    best: tuple[tuple[str, ...], float, dict[str, StoreOffer]] | None = None

    for store_set in store_sets:
        allowed = set(store_set)
        chosen: dict[str, StoreOffer] = {}
        total = 0.0
        possible = True

        for item in requested_items:
            candidates = [offer for offer in offers_by_item[item] if offer.store in allowed]
            if not candidates:
                possible = False
                break
            offer = min(candidates, key=lambda candidate: candidate.price)
            chosen[item] = offer
            total += float(offer.price)

        if possible and (best is None or total < best[1]):
            best = (tuple(sorted(store_set)), total, chosen)

    return best
