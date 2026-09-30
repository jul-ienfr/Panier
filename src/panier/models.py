from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, Field, PositiveFloat, field_validator, model_validator

T = TypeVar("T", bound=BaseModel)


class PreferenceReason(StrEnum):
    ALLERGY = "allergy"
    DISLIKE = "dislike"
    FORBIDDEN = "forbidden"
    LIKE = "like"


class PriceMode(StrEnum):
    SIMPLE = "simple"
    ECONOMIC = "economic"
    HYBRID = "hybrid"


class PreferenceConfidence(StrEnum):
    """Niveau de confiance d'une préférence ; pilote le bonus/malus au tri."""

    CONFIRMED = "confirmed"
    STRONG = "strong"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def weight(self) -> int:
        return {
            PreferenceConfidence.CONFIRMED: 40,
            PreferenceConfidence.STRONG: 25,
            PreferenceConfidence.MEDIUM: 12,
            PreferenceConfidence.LOW: 5,
        }[self]


class PreferenceStatus(StrEnum):
    PERMANENT = "permanent"
    TEMPORARY_CONTEXT = "temporary-context"

    @property
    def filters(self) -> bool:
        """Un refus contextuel pénalise le tri mais ne filtre pas (hors allergène)."""
        return self is PreferenceStatus.PERMANENT


class PreferenceDetail(BaseModel):
    """Métadonnées optionnelles attachées à une valeur de profil."""

    value: str
    status: PreferenceStatus = PreferenceStatus.PERMANENT
    reason: str | None = None
    confidence: PreferenceConfidence = PreferenceConfidence.CONFIRMED
    source: str | None = None
    since: str | None = None

    @field_validator("value")
    @classmethod
    def normalize_value(cls, value: str) -> str:
        return normalize_name(value)


PREFERENCE_KINDS = (
    "allergies",
    "forbidden",
    "dislikes",
    "likes",
    "accepted_recipes",
    "rejected_recipes",
)


class FoodProfile(BaseModel):
    allergies: set[str] = Field(default_factory=set)
    forbidden: set[str] = Field(default_factory=set)
    dislikes: set[str] = Field(default_factory=set)
    likes: set[str] = Field(default_factory=set)
    accepted_recipes: set[str] = Field(default_factory=set)
    rejected_recipes: set[str] = Field(default_factory=set)
    details: dict[str, PreferenceDetail] = Field(default_factory=dict)

    @field_validator(
        "allergies",
        "forbidden",
        "dislikes",
        "likes",
        "accepted_recipes",
        "rejected_recipes",
        mode="before",
    )
    @classmethod
    def normalize_set(cls, value: object) -> set[str]:
        if value is None:
            return set()
        if isinstance(value, str):
            return {normalize_name(value)}
        items = value if isinstance(value, (list, tuple, set)) else [value]
        return {normalize_name(str(item)) for item in items if str(item).strip()}

    @model_validator(mode="before")
    @classmethod
    def extract_details(cls, data: object) -> object:
        """Rétro-compatibilité : les entrées objet {status, reason, ...} alimentent
        `details` sous la clé `kind:value`, et seul le nom reste dans le set."""
        if not isinstance(data, dict):
            return data
        details = dict(data.get("details") or {})
        for kind in PREFERENCE_KINDS:
            values = data.get(kind)
            if not isinstance(values, list):
                continue
            plain = [str(v) for v in values if not isinstance(v, dict)]
            from_dicts: list[str] = []
            for item in values:
                if not isinstance(item, dict):
                    continue
                name = normalize_name(str(item.get("value") or item.get("name") or ""))
                if not name:
                    continue
                from_dicts.append(name)
                details[f"{kind}:{name}"] = {**item, "value": name}
            if from_dicts:
                data[kind] = [*plain, *from_dicts]
        if details:
            data["details"] = details
        return data

    @property
    def detail_key_prefixes(self) -> tuple[str, ...]:
        return tuple(f"{kind}:" for kind in PREFERENCE_KINDS)

    def detail_for(self, kind: str, value: str) -> PreferenceDetail | None:
        return self.details.get(f"{kind}:{normalize_name(value)}")

    def set_detail(self, kind: str, detail: PreferenceDetail) -> None:
        self.details[f"{kind}:{detail.value}"] = detail

    def clear_detail(self, kind: str, value: str) -> None:
        self.details.pop(f"{kind}:{normalize_name(value)}", None)

    def hard_blocks(self) -> set[str]:
        return self.allergies | self.forbidden

    def is_blocked(self, ingredient: str) -> bool:
        normalized = normalize_name(ingredient)
        if normalized in self.hard_blocks():
            return True
        return normalized in self.dislikes and self._dislike_filters(normalized)

    def _dislike_filters(self, normalized: str) -> bool:
        detail = self.detail_for("dislikes", normalized)
        if detail is None:
            return True
        return detail.status.filters

    def blocked_reason(self, ingredient: str) -> PreferenceReason | None:
        normalized = normalize_name(ingredient)
        if normalized in self.allergies or normalized in self.forbidden:
            # Invariant : allergènes et interdits sont rejetés absolument,
            # quel que soit le statut ou la confiance de l'entrée.
            return self._absolute_reason(normalized)
        if normalized in self.dislikes and self._dislike_filters(normalized):
            return PreferenceReason.DISLIKE
        return None

    def _absolute_reason(self, normalized: str) -> PreferenceReason:
        if normalized in self.allergies:
            return PreferenceReason.ALLERGY
        return PreferenceReason.FORBIDDEN


class Ingredient(BaseModel):
    name: str
    quantity: PositiveFloat | None = None
    unit: str | None = None

    @field_validator("name")
    @classmethod
    def normalize_ingredient_name(cls, value: str) -> str:
        return normalize_name(value)


class Recipe(BaseModel):
    name: str
    servings: int = 1
    ingredients: list[Ingredient]
    tags: list[str] = Field(default_factory=list)
    prep_minutes: int | None = None
    cost_level: str | None = None

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [normalize_name(value)]
        items = value if isinstance(value, (list, tuple, set)) else [value]
        return [normalize_name(str(item)) for item in items if str(item).strip()]

    @field_validator("cost_level")
    @classmethod
    def normalize_cost_level(cls, value: str | None) -> str | None:
        return normalize_name(value) if value else None

    def conflicts(self, profile: FoodProfile) -> list[tuple[str, PreferenceReason]]:
        conflicts: list[tuple[str, PreferenceReason]] = []
        for ingredient in self.ingredients:
            reason = profile.blocked_reason(ingredient.name)
            if reason is not None:
                conflicts.append((ingredient.name, reason))
        return conflicts


class ShoppingItem(BaseModel):
    name: str
    quantity: PositiveFloat | None = None
    unit: str | None = None
    min_quantity: PositiveFloat | None = None
    min_unit: str | None = None

    @field_validator("name")
    @classmethod
    def normalize_item_name(cls, value: str) -> str:
        return normalize_name(value)


class Pantry(BaseModel):
    items: list[ShoppingItem] = Field(default_factory=list)


class StoreOffer(BaseModel):
    store: str
    item: str
    product: str
    price: PositiveFloat
    unit_price: PositiveFloat | None = None
    confidence: str = "exact"
    url: str | None = None

    @field_validator("item")
    @classmethod
    def normalize_offer_item(cls, value: str) -> str:
        return normalize_name(value)


def normalize_name(value: str) -> str:
    return " ".join(value.strip().lower().replace("’", "'").split())


def load_yaml_model(path: Path, model: type[T]) -> T:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return model.model_validate(data)


def dump_yaml(path: Path, data: BaseModel | dict) -> None:
    payload = data.model_dump(mode="json") if isinstance(data, BaseModel) else data
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=True), encoding="utf-8")
