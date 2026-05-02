from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

CONSTRAINTS_FILENAME = "constraints.yaml"


class BasketConstraints(BaseModel):
    """Contraintes panier locales, sans appel réseau/LLM."""

    max_total_eur: float | None = None
    min_items: int | None = None
    max_items: int | None = None
    preferred_stores: list[str] = Field(default_factory=list)
    blocked_stores: list[str] = Field(default_factory=list)


def constraints_path(data_dir: Path) -> Path:
    return data_dir / CONSTRAINTS_FILENAME


def load_constraints(data_dir: Path) -> BasketConstraints:
    path = constraints_path(data_dir)
    if not path.exists():
        return BasketConstraints()
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return BasketConstraints.model_validate(payload)


def save_constraints(data_dir: Path, constraints: BasketConstraints) -> None:
    path = constraints_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(constraints.model_dump(mode="json"), allow_unicode=True, sort_keys=True),
        encoding="utf-8",
    )
