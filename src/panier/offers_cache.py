"""Cache offres par drive avec TTL, sous <data_dir>/cache/offers/.

Chaque collect réussie (même partielle) écrit un fichier
<store>-<hash-liste>.yaml. Un cache hit frais évite toute requête réseau ;
au-delà du TTL l'entrée est marquée stale et ne sert que de repli si une
recollecte fraîche échoue.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from panier.models import StoreOffer, normalize_name

OFFERS_CACHE_SUBDIR = Path("cache") / "offers"
DEFAULT_CACHE_TTL_HOURS = 6.0
CACHE_TTL_ENV_VAR = "PANIER_CACHE_TTL_HOURS"


class OffersCacheEntry(BaseModel):
    """Entrée de cache offres pour un couple (store, liste d'articles)."""

    store: str
    items_hash: str
    item_names: list[str] = Field(default_factory=list)
    collected_at: str
    ttl_hours: float = DEFAULT_CACHE_TTL_HOURS
    offers: list[StoreOffer] = Field(default_factory=list)


def resolve_cache_ttl_hours(
    override: float | None = None, environ: dict[str, str] | None = None
) -> float:
    """TTL effectif : argument explicite > variable d'environnement > défaut."""
    if override is not None and override > 0:
        return override
    values = os.environ if environ is None else environ
    raw = values.get(CACHE_TTL_ENV_VAR)
    if raw:
        try:
            parsed = float(raw.replace(",", "."))
        except ValueError:
            return DEFAULT_CACHE_TTL_HOURS
        if parsed > 0:
            return parsed
    return DEFAULT_CACHE_TTL_HOURS


def offers_cache_dir(data_dir: Path) -> Path:
    return data_dir / OFFERS_CACHE_SUBDIR


def items_fingerprint(items: list) -> str:
    """Empreinte stable de la liste demandée (noms canoniques triés)."""
    names = sorted({normalize_name(item.name) for item in items})
    digest = hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()
    return digest[:16]


def offers_cache_path(data_dir: Path, store: str, items: list) -> Path:
    normalized_store = normalize_name(store)
    filename = f"{normalized_store}-{items_fingerprint(items)}.yaml"
    return offers_cache_dir(data_dir) / filename


def parse_collected_at(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def entry_age_seconds(entry: OffersCacheEntry, now: datetime | None = None) -> float:
    reference = now or datetime.now(UTC)
    collected = parse_collected_at(entry.collected_at)
    if collected is None:
        return float("inf")
    return max(0.0, (reference - collected).total_seconds())


def entry_is_stale(
    entry: OffersCacheEntry,
    *,
    now: datetime | None = None,
    ttl_hours_override: float | None = None,
) -> bool:
    ttl_hours = (
        ttl_hours_override
        if ttl_hours_override is not None and ttl_hours_override > 0
        else entry.ttl_hours
    )
    return entry_age_seconds(entry, now=now) >= ttl_hours * 3600


def load_cached_entry(path: Path) -> OffersCacheEntry | None:
    """Charge une entrée de cache ; tout fichier illisible est simplement ignoré."""
    if not path.exists():
        return None
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return OffersCacheEntry.model_validate(payload)
    except Exception:
        return None


def save_offers_cache(
    data_dir: Path,
    store: str,
    items: list,
    offers: list[StoreOffer],
    *,
    collected_at: datetime | None = None,
    ttl_hours: float | None = None,
) -> Path:
    stamp = collected_at or datetime.now(UTC)
    path = offers_cache_path(data_dir, store, items)
    entry = OffersCacheEntry(
        store=normalize_name(store),
        items_hash=items_fingerprint(items),
        item_names=sorted({normalize_name(item.name) for item in items}),
        collected_at=stamp.isoformat(timespec="seconds"),
        ttl_hours=ttl_hours if ttl_hours is not None and ttl_hours > 0 else DEFAULT_CACHE_TTL_HOURS,
        offers=list(offers),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(entry.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def cache_report_line(store: str, count: int, *, age_s: float | None, stale: bool) -> str:
    """Message texte normalisé pour un résultat venu du cache."""
    suffix = f" (cache, âge {age_s:.0f}s" if age_s is not None else " (cache"
    if stale:
        suffix += ", stale"
    return f"Collecte {store}: {count} offres{suffix})"
