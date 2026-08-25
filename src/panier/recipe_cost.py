"""Coût par recette : derniers prix connus par ingrédient canonique.

Priorité : un YAML --prices fourni reste déterministe et prioritaire ;
l'historique SQLite n'est qu'un repli. Un ingrédient sans aucune source
est listé `unpriced` explicitement — jamais d'estimation silencieuse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from panier.drive import best_offer_for_item
from panier.history_store import offers_for_item
from panier.models import Recipe, ShoppingItem, StoreOffer, normalize_name
from panier.planner import consolidate_ingredients


@dataclass(frozen=True)
class IngredientCost:
    name: str
    price: float | None
    source: str  # "prices" | "history" | "unpriced"
    store: str | None = None


@dataclass(frozen=True)
class RecipeCost:
    recipe: Recipe
    items: list[ShoppingItem] = field(default_factory=list)
    costs: list[IngredientCost] = field(default_factory=list)

    @property
    def total(self) -> float | None:
        total = 0.0
        for cost in self.costs:
            if cost.price is None:
                return None
            total += cost.price
        return round(total, 4)

    @property
    def partial_total(self) -> float:
        total = 0.0
        for cost in self.costs:
            if cost.price is not None:
                total += cost.price
        return round(total, 4)

    @property
    def unpriced(self) -> list[str]:
        return [cost.name for cost in self.costs if cost.price is None]

    def per_servings(self) -> float | None:
        total = self.total
        if total is None or self.recipe.servings <= 0:
            return None
        return round(total / self.recipe.servings, 4)


def _latest_price_from_history(
    data_dir: Path, canonical_name: str
) -> tuple[float, str] | None:
    """Prix le plus récent par store ; retient le moins cher à date égale."""
    points = offers_for_item(data_dir, canonical_name)
    if not points:
        return None
    latest_at = max(point.collected_at for point in points)
    candidates = [
        point
        for point in points
        if point.collected_at == latest_at
    ]
    best = min(candidates, key=lambda point: (point.price, point.store))
    return best.price, best.store


def resolve_ingredient_costs(
    items: list[ShoppingItem],
    *,
    data_dir: Path,
    prices_offers: list[StoreOffer] | None = None,
) -> list[IngredientCost]:
    costs: list[IngredientCost] = []
    for item in sorted(items, key=lambda entry: entry.name):
        if prices_offers is not None:
            chosen = best_offer_for_item(item, prices_offers, compare_by="price")
            if chosen is not None:
                costs.append(
                    IngredientCost(
                        name=item.name,
                        price=float(chosen.offer.price),
                        source="prices",
                        store=chosen.offer.store,
                    )
                )
                continue
        historical = _latest_price_from_history(data_dir, item.name)
        if historical is not None:
            costs.append(
                IngredientCost(
                    name=item.name,
                    price=historical[0],
                    source="history",
                    store=historical[1],
                )
            )
            continue
        costs.append(IngredientCost(name=item.name, price=None, source="unpriced"))
    return costs


def compute_recipe_cost(
    recipe: Recipe,
    *,
    data_dir: Path,
    prices_offers: list[StoreOffer] | None = None,
) -> RecipeCost:
    items = consolidate_ingredients([recipe])
    costs = resolve_ingredient_costs(items, data_dir=data_dir, prices_offers=prices_offers)
    return RecipeCost(recipe=recipe, items=items, costs=costs)


def recipe_cost_sort_key(cost: RecipeCost) -> tuple[int, float, str]:
    """Tri coût croissant ; recettes non pricées en dernier, tie-break par nom."""
    total = cost.total
    if total is None:
        return (1, float("inf"), normalize_name(cost.recipe.name))
    return (0, total, normalize_name(cost.recipe.name))
