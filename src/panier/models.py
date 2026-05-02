from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, Field, PositiveFloat, field_validator

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


class FoodProfile(BaseModel):
    allergies: set[str] = Field(default_factory=set)
    forbidden: set[str] = Field(default_factory=set)
    dislikes: set[str] = Field(default_factory=set)
    likes: set[str] = Field(default_factory=set)
    accepted_recipes: set[str] = Field(default_factory=set)
    rejected_recipes: set[str] = Field(default_factory=set)

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
        return {normalize_name(str(item)) for item in value if str(item).strip()}

    def hard_blocks(self) -> set[str]:
        return self.allergies | self.forbidden

    def is_blocked(self, ingredient: str) -> bool:
        normalized = normalize_name(ingredient)
        return normalized in self.hard_blocks() or normalized in self.dislikes

    def blocked_reason(self, ingredient: str) -> PreferenceReason | None:
        normalized = normalize_name(ingredient)
        if normalized in self.allergies:
            return PreferenceReason.ALLERGY
        if normalized in self.forbidden:
            return PreferenceReason.FORBIDDEN
        if normalized in self.dislikes:
            return PreferenceReason.DISLIKE
        return None


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
        return [normalize_name(str(item)) for item in value if str(item).strip()]

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
