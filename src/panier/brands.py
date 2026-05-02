from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, PositiveFloat, field_validator

from panier.models import StoreOffer, normalize_name

BRAND_PREFERENCES_FILENAME = "brand_preferences.yaml"


class BrandPreferenceAction(StrEnum):
    PREFER = "prefer"
    AVOID = "avoid"
    BLOCK = "block"
    NEUTRAL = "neutral"


class BrandPreferences(BaseModel):
    """Local deterministic brand preferences.

    Rules are deliberately simple and global: a brand is detected when its
    normalized text appears in an offer product name. The basket planner then
    applies deterministic price guardrails instead of asking a LLM.
    """

    prefer: set[str] = Field(default_factory=set)
    avoid: set[str] = Field(default_factory=set)
    block: set[str] = Field(default_factory=set)
    prefer_max_price_delta_eur: PositiveFloat = 1.0
    prefer_max_price_delta_percent: PositiveFloat = 15.0
    avoid_min_savings_eur: PositiveFloat = 2.0
    avoid_min_savings_percent: PositiveFloat = 25.0

    @field_validator("prefer", "avoid", "block", mode="before")
    @classmethod
    def normalize_brand_set(cls, value: object) -> set[str]:
        if value is None:
            return set()
        if isinstance(value, str):
            values: Iterable[object] = [value]
        else:
            values = value  # type: ignore[assignment]
        return {normalize_name(str(item)) for item in values if str(item).strip()}

    def action_for_brand(self, brand: str) -> BrandPreferenceAction:
        normalized = normalize_name(brand)
        if normalized in self.block:
            return BrandPreferenceAction.BLOCK
        if normalized in self.prefer:
            return BrandPreferenceAction.PREFER
        if normalized in self.avoid:
            return BrandPreferenceAction.AVOID
        return BrandPreferenceAction.NEUTRAL

    def action_for_offer(self, offer: StoreOffer) -> BrandPreferenceAction:
        return self.match_offer(offer).action

    def match_offer(self, offer: StoreOffer) -> BrandPreferenceMatch:
        product = normalize_name(offer.product)
        matches: list[BrandPreferenceMatch] = []
        for action, brands in (
            (BrandPreferenceAction.BLOCK, self.block),
            (BrandPreferenceAction.PREFER, self.prefer),
            (BrandPreferenceAction.AVOID, self.avoid),
        ):
            for brand in brands:
                if brand and brand in product:
                    matches.append(BrandPreferenceMatch(brand=brand, action=action))
        if not matches:
            return BrandPreferenceMatch(brand=None, action=BrandPreferenceAction.NEUTRAL)
        return sorted(matches, key=lambda match: (match.action_rank, match.brand or ""))[0]

    def add(self, action: BrandPreferenceAction, brand: str) -> str:
        normalized = normalize_name(brand)
        if not normalized:
            raise ValueError("brand must not be empty")
        self.prefer.discard(normalized)
        self.avoid.discard(normalized)
        self.block.discard(normalized)
        if action == BrandPreferenceAction.PREFER:
            self.prefer.add(normalized)
        elif action == BrandPreferenceAction.AVOID:
            self.avoid.add(normalized)
        elif action == BrandPreferenceAction.BLOCK:
            self.block.add(normalized)
        else:
            raise ValueError("action must be prefer, avoid or block")
        return normalized

    def remove(self, brand: str) -> str:
        normalized = normalize_name(brand)
        self.prefer.discard(normalized)
        self.avoid.discard(normalized)
        self.block.discard(normalized)
        return normalized


@dataclass(frozen=True)
class BrandPreferenceMatch:
    brand: str | None
    action: BrandPreferenceAction

    @property
    def action_rank(self) -> int:
        return {
            BrandPreferenceAction.BLOCK: 0,
            BrandPreferenceAction.PREFER: 1,
            BrandPreferenceAction.AVOID: 2,
            BrandPreferenceAction.NEUTRAL: 3,
        }[self.action]


def brand_preferences_path(data_dir: Path) -> Path:
    return data_dir / BRAND_PREFERENCES_FILENAME


def load_brand_preferences(data_dir: Path | None = None) -> BrandPreferences:
    if data_dir is None:
        data_dir = Path.home() / ".panier"
    path = brand_preferences_path(data_dir)
    if not path.exists():
        return BrandPreferences()
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return BrandPreferences.model_validate(payload)


def save_brand_preferences(data_dir: Path, preferences: BrandPreferences) -> None:
    path = brand_preferences_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            preferences.model_dump(mode="json"),
            allow_unicode=True,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
