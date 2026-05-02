from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations
from typing import Literal

from panier.models import (
    FoodProfile,
    Pantry,
    PriceMode,
    Recipe,
    ShoppingItem,
    StoreOffer,
    normalize_name,
)
from panier.nutrition import score_recipe_balance


@dataclass(frozen=True)
class BasketRecommendation:
    mode: PriceMode
    stores: tuple[str, ...]
    total: float
    by_item: dict[str, StoreOffer]
    savings_vs_best_single: float
    reason: str


CompareBy = Literal["price", "unit_price"]

_UNIT_FACTORS: dict[str, tuple[str, float]] = {
    "g": ("g", 1.0),
    "gramme": ("g", 1.0),
    "grammes": ("g", 1.0),
    "kg": ("g", 1000.0),
    "ml": ("ml", 1.0),
    "cl": ("ml", 10.0),
    "l": ("ml", 1000.0),
    "litre": ("ml", 1000.0),
    "litres": ("ml", 1000.0),
}


def compatible_recipes(recipes: list[Recipe], profile: FoodProfile) -> list[Recipe]:
    return [recipe for recipe in recipes if not recipe.conflicts(profile)]


def filter_recipes(
    recipes: list[Recipe],
    *,
    include_tags: set[str] | None = None,
    exclude_tags: set[str] | None = None,
    max_prep_minutes: int | None = None,
    cost_level: str | None = None,
    min_balance_score: int | None = None,
) -> list[Recipe]:
    filtered: list[Recipe] = []
    for recipe in recipes:
        recipe_tags = {normalize_name(tag) for tag in recipe.tags}
        if include_tags and not include_tags.issubset(recipe_tags):
            continue
        if exclude_tags and recipe_tags.intersection(exclude_tags):
            continue
        if max_prep_minutes is not None and recipe.prep_minutes is not None:
            if recipe.prep_minutes > max_prep_minutes:
                continue
        if cost_level is not None and recipe.cost_level != normalize_name(cost_level):
            continue
        if min_balance_score is not None:
            if score_recipe_balance(recipe).score < min_balance_score:
                continue
        filtered.append(recipe)
    return filtered


def recipe_selection_score(
    recipe: Recipe,
    *,
    profile: FoodProfile | None = None,
    include_tags: set[str] | None = None,
    cost_level: str | None = None,
    min_balance_score: int | None = None,
) -> int:
    """Return a deterministic planner score for a recipe.

    The score only uses local recipe/profile data and the requested constraints.
    Hard constraints are still enforced by ``filter_recipes``; this score ranks
    the remaining compatible recipes before deterministic tie-breaks are applied.
    """
    recipe_tags = {normalize_name(tag) for tag in recipe.tags}
    normalized_cost_level = normalize_name(cost_level) if cost_level else None

    score = 0
    if profile is not None and normalize_name(recipe.name) in profile.accepted_recipes:
        score += 1000

    if include_tags:
        score += 10 * len(recipe_tags.intersection(include_tags))

    if min_balance_score is not None:
        score += score_recipe_balance(recipe).score

    if normalized_cost_level is not None:
        if recipe.cost_level == normalized_cost_level:
            score += 20
    elif recipe.cost_level == "budget" or "budget" in recipe_tags:
        score += 10

    return score


def _recipe_prep_sort_value(recipe: Recipe) -> int:
    return recipe.prep_minutes if recipe.prep_minutes is not None else 10**9


def select_meals(
    recipes: list[Recipe],
    profile: FoodProfile,
    meals: int,
    *,
    include_tags: set[str] | None = None,
    exclude_tags: set[str] | None = None,
    max_prep_minutes: int | None = None,
    cost_level: str | None = None,
    min_balance_score: int | None = None,
) -> list[Recipe]:
    compatible = compatible_recipes(recipes, profile)
    compatible = filter_recipes(
        compatible,
        include_tags=include_tags,
        exclude_tags=exclude_tags,
        max_prep_minutes=max_prep_minutes,
        cost_level=cost_level,
        min_balance_score=min_balance_score,
    )
    scored = [
        (
            recipe,
            recipe_selection_score(
                recipe,
                profile=profile,
                include_tags=include_tags,
                cost_level=cost_level,
                min_balance_score=min_balance_score,
            ),
        )
        for recipe in compatible
    ]
    ranked = [
        recipe
        for recipe, _ in sorted(
            scored,
            key=lambda item: (
                -item[1],
                _recipe_prep_sort_value(item[0]),
                normalize_name(item[0].name),
            ),
        )
    ]
    return ranked[:meals]


def consolidate_ingredients(recipes: list[Recipe]) -> list[ShoppingItem]:
    quantities: dict[tuple[str, str | None], float] = defaultdict(float)
    has_quantity: dict[tuple[str, str | None], bool] = defaultdict(bool)

    for recipe in recipes:
        for ingredient in recipe.ingredients:
            key = _quantity_key(ingredient.name, ingredient.unit)
            if ingredient.quantity is None:
                quantities[key] += 1
            else:
                quantities[key] += _to_base_quantity(float(ingredient.quantity), ingredient.unit)
                has_quantity[key] = True

    return _items_from_quantities(quantities, has_quantity)


def subtract_pantry(items: list[ShoppingItem], pantry: Pantry) -> list[ShoppingItem]:
    pantry_quantities, pantry_unknown_quantity = _index_pantry(pantry)

    remaining: list[ShoppingItem] = []
    for item in items:
        key = _quantity_key(item.name, item.unit)
        if item.quantity is None:
            if key not in pantry_unknown_quantity and pantry_quantities[key] <= 0:
                remaining.append(item)
            continue

        missing_quantity = (
            _to_base_quantity(float(item.quantity), item.unit) - pantry_quantities[key]
        )
        if missing_quantity > 0:
            remaining.append(_item_from_base(item.name, missing_quantity, key[1]))

    return remaining


def consume_pantry(items: list[ShoppingItem], pantry: Pantry) -> tuple[Pantry, list[ShoppingItem]]:
    """Consume requested items from pantry and return updated pantry + missing items.

    Consumption is safe/partial: available stock is decremented, and shortages are returned
    instead of silently going negative.
    """
    remaining_to_consume: dict[tuple[str, str | None], float | None] = {}
    display_names: dict[tuple[str, str | None], str] = {}
    for item in items:
        key = _quantity_key(item.name, item.unit)
        display_names[key] = item.name
        if item.quantity is None:
            remaining_to_consume[key] = None
        else:
            remaining_to_consume[key] = (remaining_to_consume.get(key) or 0.0) + _to_base_quantity(
                float(item.quantity), item.unit
            )

    updated_items: list[ShoppingItem] = []
    for pantry_item in pantry.items:
        key = _quantity_key(pantry_item.name, pantry_item.unit)
        requested = remaining_to_consume.get(key)
        if requested is None and key in remaining_to_consume:
            remaining_to_consume[key] = 0.0
            continue
        if requested is None:
            updated_items.append(pantry_item)
            continue
        if pantry_item.quantity is None:
            remaining_to_consume[key] = 0.0
            continue

        available = _to_base_quantity(float(pantry_item.quantity), pantry_item.unit)
        consumed = min(available, requested)
        left = available - consumed
        remaining_to_consume[key] = requested - consumed
        if left > 0:
            updated_items.append(
                ShoppingItem(
                    name=pantry_item.name,
                    quantity=left,
                    unit=key[1],
                    min_quantity=pantry_item.min_quantity,
                    min_unit=pantry_item.min_unit,
                )
            )

    missing = [
        _item_from_base(display_names.get(key, key[0]), quantity, key[1])
        for key, quantity in remaining_to_consume.items()
        if quantity not in (None, 0.0) and quantity > 0
    ]
    return Pantry(
        items=sorted(updated_items, key=lambda item: (item.name, item.unit or ""))
    ), missing


def low_stock_items(pantry: Pantry) -> list[ShoppingItem]:
    low: list[ShoppingItem] = []
    for item in pantry.items:
        if item.quantity is None or item.min_quantity is None:
            continue
        quantity = _to_base_quantity(float(item.quantity), item.unit)
        minimum = _to_base_quantity(float(item.min_quantity), item.min_unit or item.unit)
        if quantity < minimum:
            low.append(
                ShoppingItem(
                    name=item.name,
                    quantity=minimum - quantity,
                    unit=_canonical_unit(item.min_unit or item.unit),
                )
            )
    return low


def _index_pantry(
    pantry: Pantry,
) -> tuple[dict[tuple[str, str | None], float], set[tuple[str, str | None]]]:
    pantry_quantities: dict[tuple[str, str | None], float] = defaultdict(float)
    pantry_unknown_quantity: set[tuple[str, str | None]] = set()

    for item in pantry.items:
        key = _quantity_key(item.name, item.unit)
        if item.quantity is None:
            pantry_unknown_quantity.add(key)
        else:
            pantry_quantities[key] += _to_base_quantity(float(item.quantity), item.unit)
    return pantry_quantities, pantry_unknown_quantity


def _quantity_key(name: str, unit: str | None) -> tuple[str, str | None]:
    return normalize_name(name), _canonical_unit(unit)


def _canonical_unit(unit: str | None) -> str | None:
    if unit is None:
        return None
    normalized = normalize_name(unit)
    return _UNIT_FACTORS.get(normalized, (normalized, 1.0))[0]


def _to_base_quantity(quantity: float, unit: str | None) -> float:
    if unit is None:
        return quantity
    return quantity * _UNIT_FACTORS.get(normalize_name(unit), (unit, 1.0))[1]


def _item_from_base(name: str, quantity: float, canonical_unit: str | None) -> ShoppingItem:
    return ShoppingItem(name=name, quantity=quantity, unit=canonical_unit)


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
    compare_by: CompareBy = "price",
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
        compare_by=compare_by,
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

    best_split = _best_for_store_sets(
        requested_items, offers_by_item, store_sets, compare_by=compare_by
    )
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
    compare_by: CompareBy = "price",
) -> tuple[tuple[str, ...], float, dict[str, StoreOffer]] | None:
    best: tuple[tuple[str, ...], float, dict[str, StoreOffer]] | None = None
    best_metric: float | None = None

    for store_set in store_sets:
        allowed = set(store_set)
        chosen: dict[str, StoreOffer] = {}
        total = 0.0
        comparable_total = 0.0
        possible = True

        for item in requested_items:
            candidates = [offer for offer in offers_by_item[item] if offer.store in allowed]
            if not candidates:
                possible = False
                break
            offer = min(
                candidates,
                key=lambda candidate: _offer_compare_value(candidate, compare_by),
            )
            chosen[item] = offer
            total += float(offer.price)
            comparable_total += _offer_compare_value(offer, compare_by)

        if possible and (best_metric is None or comparable_total < best_metric):
            best_metric = comparable_total
            best = (tuple(sorted(store_set)), total, chosen)

    return best


def _offer_compare_value(offer: StoreOffer, compare_by: CompareBy) -> float:
    if compare_by == "unit_price" and offer.unit_price is not None:
        return float(offer.unit_price)
    return float(offer.price)
