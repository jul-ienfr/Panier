from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from panier.models import ShoppingItem, StoreOffer, normalize_name

SUBSTITUTIONS_FILENAME = "substitutions.yaml"


class SubstitutionRule(BaseModel):
    item: str
    substitutes: list[str] = Field(default_factory=list)

    @field_validator("item")
    @classmethod
    def normalize_item(cls, value: str) -> str:
        return normalize_name(value)

    @field_validator("substitutes", mode="before")
    @classmethod
    def normalize_substitutes(cls, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [normalize_name(value)]
        return [normalize_name(str(item)) for item in value if str(item).strip()]


class SubstitutionCatalog(BaseModel):
    rules: list[SubstitutionRule] = Field(default_factory=list)

    def substitutes_for(self, item_name: str) -> list[str]:
        normalized = normalize_name(item_name)
        for rule in self.rules:
            if rule.item == normalized:
                return sorted(set(rule.substitutes))
        return []

    def add(self, item_name: str, substitute: str) -> tuple[str, str]:
        normalized_item = normalize_name(item_name)
        normalized_substitute = normalize_name(substitute)
        rules_by_item = {rule.item: rule for rule in self.rules}
        current = rules_by_item.get(normalized_item)
        if current is None:
            self.rules.append(
                SubstitutionRule(item=normalized_item, substitutes=[normalized_substitute])
            )
        else:
            current.substitutes = sorted(set(current.substitutes) | {normalized_substitute})
        self.rules = sorted(self.rules, key=lambda rule: rule.item)
        return normalized_item, normalized_substitute

    def remove(self, item_name: str, substitute: str | None = None) -> tuple[str, str | None]:
        normalized_item = normalize_name(item_name)
        normalized_substitute = normalize_name(substitute) if substitute else None
        kept: list[SubstitutionRule] = []
        for rule in self.rules:
            if rule.item != normalized_item:
                kept.append(rule)
                continue
            if normalized_substitute is None:
                continue
            substitutes = [value for value in rule.substitutes if value != normalized_substitute]
            if substitutes:
                kept.append(SubstitutionRule(item=rule.item, substitutes=substitutes))
        self.rules = sorted(kept, key=lambda rule: rule.item)
        return normalized_item, normalized_substitute


def substitutions_path(data_dir: Path) -> Path:
    return data_dir / SUBSTITUTIONS_FILENAME


def load_substitutions(data_dir: Path) -> SubstitutionCatalog:
    path = substitutions_path(data_dir)
    if not path.exists():
        return SubstitutionCatalog()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if isinstance(data, list):
        data = {"rules": data}
    return SubstitutionCatalog.model_validate(data)


def save_substitutions(data_dir: Path, catalog: SubstitutionCatalog) -> None:
    path = substitutions_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(catalog.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def expand_items_with_substitutions(
    items: list[ShoppingItem], substitutions: SubstitutionCatalog
) -> list[ShoppingItem]:
    expanded: list[ShoppingItem] = []
    seen: set[str] = set()
    for item in items:
        candidates = [item]
        candidates.extend(
            item.model_copy(update={"name": substitute})
            for substitute in substitutions.substitutes_for(item.name)
        )
        for candidate in candidates:
            normalized = normalize_name(candidate.name)
            if normalized in seen:
                continue
            seen.add(normalized)
            expanded.append(candidate)
    return expanded


def substitute_offers_for_requested_items(
    items: list[ShoppingItem],
    offers: list[StoreOffer],
    substitutions: SubstitutionCatalog,
) -> list[StoreOffer]:
    """Map substitute offers back to their requested item.

    The planner works by exact item keys. If `lait -> boisson avoine` is allowed,
    a `boisson avoine` offer must also be visible under item `lait`; otherwise the
    original request is reported as missing even though a deterministic substitute
    exists.
    """
    result = list(offers)
    seen = {(offer.store, offer.item, offer.product, float(offer.price)) for offer in offers}
    offers_by_item: dict[str, list[StoreOffer]] = {}
    for offer in offers:
        offers_by_item.setdefault(normalize_name(offer.item), []).append(offer)

    for item in items:
        requested = normalize_name(item.name)
        for substitute in substitutions.substitutes_for(requested):
            for offer in offers_by_item.get(substitute, []):
                mapped = offer.model_copy(update={"item": requested})
                key = (mapped.store, mapped.item, mapped.product, float(mapped.price))
                if key in seen:
                    continue
                seen.add(key)
                result.append(mapped)
    return result
