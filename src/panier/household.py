"""Profils foyer nommés : allergènes, préférences, magasins, zone géo.

Chaque foyer vit dans <data_dir>/profiles/<slug>.yaml et étend un
FoodProfile classique avec ses magasins activés, sa zone géographique et
ses paramètres de planification. Le foyer « actif » est mémorisé dans
profiles/active.txt ; tant qu'aucun foyer n'est actif, les commandes
profil continuent d'utiliser le profile.yaml historique (zéro rupture).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from panier.models import FoodProfile, normalize_name

HOUSEHOLDS_DIRNAME = "profiles"
ACTIVE_HOUSEHOLD_FILENAME = "active.txt"

SAFE_HOUSEHOLD_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class HouseholdError(ValueError):
    """Erreur opérateur-safe sur la gestion des foyers."""


class HouseholdProfile(BaseModel):
    """Un foyer : préférences alimentaires + contexte magasins/géographie."""

    name: str
    display_name: str | None = None
    geo_zone: str | None = None
    stores: dict[str, bool] = Field(default_factory=dict)
    servings_per_meal: int | None = None
    budget_max_eur: float | None = None
    notes: str | None = None
    preferences: FoodProfile = Field(default_factory=FoodProfile)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        slug = slugify(value)
        if not SAFE_HOUSEHOLD_NAME_RE.fullmatch(slug):
            raise ValueError(
                "nom de foyer invalide (lettre/chiffre en premier, "
                "puis lettres, chiffres, - ou _, 40 caractères max)"
            )
        return slug

    @field_validator("stores", mode="before")
    @classmethod
    def normalize_stores(cls, value: object) -> dict[str, bool]:
        if not isinstance(value, dict):
            return {}
        return {
            normalize_name(str(store)): bool(enabled)
            for store, enabled in value.items()
            if str(store).strip()
        }

    def enabled_stores(self) -> set[str]:
        """Stores explicitement activés."""
        return {store for store, enabled in self.stores.items() if enabled}

    def disabled_stores(self) -> set[str]:
        """Stores explicitement désactivés."""
        return {store for store, enabled in self.stores.items() if not enabled}


def slugify(name: str) -> str:
    slug = normalize_name(name).replace(" ", "-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug


def households_dir(data_dir: Path) -> Path:
    return data_dir / HOUSEHOLDS_DIRNAME


def active_household_file(data_dir: Path) -> Path:
    return households_dir(data_dir) / ACTIVE_HOUSEHOLD_FILENAME


def household_path(data_dir: Path, name: str) -> Path:
    slug = slugify(name)
    if not SAFE_HOUSEHOLD_NAME_RE.fullmatch(slug):
        raise HouseholdError(f"nom de foyer invalide : {name}")
    return households_dir(data_dir) / f"{slug}.yaml"


def list_households(data_dir: Path) -> list[str]:
    directory = households_dir(data_dir)
    if not directory.exists():
        return []
    return sorted(
        path.stem
        for path in directory.glob("*.yaml")
        if path.name != ACTIVE_HOUSEHOLD_FILENAME
    )


def active_household_name(data_dir: Path) -> str | None:
    marker = active_household_file(data_dir)
    if not marker.exists():
        return None
    raw = marker.read_text(encoding="utf-8").strip()
    return raw or None


def set_active_household(data_dir: Path, name: str | None) -> None:
    marker = active_household_file(data_dir)
    if name is None:
        marker.unlink(missing_ok=True)
        return
    path = household_path(data_dir, name)
    if not path.exists():
        raise HouseholdError(f"foyer introuvable : {slugify(name)}")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(slugify(name) + "\n", encoding="utf-8")


def load_household(data_dir: Path, name: str) -> HouseholdProfile:
    path = household_path(data_dir, name)
    if not path.exists():
        raise HouseholdError(f"foyer introuvable : {slugify(name)}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return HouseholdProfile.model_validate(payload)


def save_household(data_dir: Path, household: HouseholdProfile) -> Path:
    path = household_path(data_dir, household.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            household.model_dump(mode="json"),
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def resolve_profile_file(data_dir: Path) -> Path:
    """Fichier effectif des commandes `profile *`.

    Foyer actif présent ? Son fichier. Sinon le profile.yaml historique.
    """
    active = active_household_name(data_dir)
    if active is not None:
        return household_path(data_dir, active)
    return data_dir / "profile.yaml"


def load_active_household(data_dir: Path) -> HouseholdProfile | None:
    name = active_household_name(data_dir)
    if name is None:
        return None
    try:
        return load_household(data_dir, name)
    except HouseholdError:
        return None
