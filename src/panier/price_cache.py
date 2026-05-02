from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from panier.models import StoreOffer, normalize_name

PRICE_CACHE_FILENAME = "price_cache.yaml"


class PriceCache(BaseModel):
    """Cache local déterministe des offres collectées/importées."""

    offers: list[StoreOffer] = Field(default_factory=list)


def price_cache_path(data_dir: Path) -> Path:
    return data_dir / PRICE_CACHE_FILENAME


def load_price_cache(data_dir: Path) -> PriceCache:
    path = price_cache_path(data_dir)
    if not path.exists():
        return PriceCache()
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return PriceCache.model_validate(payload)


def save_price_cache(data_dir: Path, cache: PriceCache) -> None:
    path = price_cache_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(cache.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def offer_cache_key(offer: StoreOffer) -> tuple[str, str, str, str]:
    return (
        normalize_name(offer.store),
        normalize_name(offer.item),
        normalize_name(offer.product),
        offer.url or "",
    )


def merge_offers(existing: list[StoreOffer], incoming: list[StoreOffer]) -> list[StoreOffer]:
    """Fusionne des offres sans doublon, avec ordre stable et dernière valeur gagnante."""

    by_key = {offer_cache_key(offer): offer for offer in existing}
    for offer in incoming:
        by_key[offer_cache_key(offer)] = offer
    return [by_key[key] for key in sorted(by_key)]


def add_offers_to_cache(data_dir: Path, offers: list[StoreOffer]) -> PriceCache:
    cache = load_price_cache(data_dir)
    cache = PriceCache(offers=merge_offers(cache.offers, offers))
    save_price_cache(data_dir, cache)
    return cache
