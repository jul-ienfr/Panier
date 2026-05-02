from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote_plus, urljoin

from panier.managed_browser import BrowserCommandResult, ManagedBrowserClient
from panier.models import ShoppingItem, StoreOffer, normalize_name


class BrandType(StrEnum):
    COMMON = "common"
    STORE_BRAND = "store_brand"
    GENERIC = "generic"


@dataclass(frozen=True)
class DriveProduct:
    """Produit canonique que Panier sait chercher sur un drive."""

    name: str
    brand: str | None = None
    brand_type: BrandType = BrandType.COMMON
    store_brand_affinity: str | None = None
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", normalize_name(self.name))
        if self.brand is not None:
            object.__setattr__(self, "brand", normalize_name(self.brand))
        if self.store_brand_affinity is not None:
            object.__setattr__(
                self, "store_brand_affinity", normalize_name(self.store_brand_affinity)
            )
        object.__setattr__(self, "aliases", tuple(normalize_name(alias) for alias in self.aliases))


@dataclass(frozen=True)
class DriveSearchQuery:
    item: ShoppingItem
    query: str
    confidence: str


@dataclass(frozen=True)
class OfferScore:
    offer: StoreOffer
    score: float
    reason: str


@dataclass(frozen=True)
class BrowserSearchResult:
    entry: DriveSearchQuery
    url: str
    browser_result: BrowserCommandResult


_STOPWORDS = {
    "de",
    "du",
    "des",
    "le",
    "la",
    "les",
    "l",
    "d",
    "au",
    "aux",
    "et",
    "a",
    "à",
    "avec",
    "sans",
    "pour",
    "bio",
    "frais",
    "fraîche",
    "nature",
}

_SYNONYMS: dict[str, tuple[str, ...]] = {
    "lardons": ("allumettes", "poitrine fumée", "bacon"),
    "allumettes": ("lardons", "bacon"),
    "steak haché": ("viande hachée", "boeuf haché", "bœuf haché"),
    "crème": ("crème fraîche", "crème liquide", "crème épaisse"),
    "parmesan": ("parmigiano reggiano", "grana padano", "fromage râpé italien"),
    "filet de poulet": ("blanc de poulet", "escalope de poulet"),
    "pâtes": ("spaghetti", "tagliatelle", "penne"),
    "oignon": ("oignon jaune", "oignon blanc", "oignon rouge", "échalote"),
    "lait": ("lait demi-écrémé", "lait entier", "lait écrémé"),
    "beurre": ("beurre doux", "beurre demi-sel", "beurre salé"),
    "fromage": ("emmental", "gruyère", "comté", "cheddar", "fromage râpé"),
    "poulet": ("blanc de poulet", "filet de poulet", "escalope de poulet"),
    "tomates concassées": ("tomates pelées", "pulpe de tomate", "concassé de tomates"),
    "haricots rouges": ("haricots chili", "red kidney beans"),
}

_DRIVE_SEARCH_URLS = {
    "auchan": "https://www.auchan.fr/recherche?text={query}",
    "leclerc": "https://www.e.leclerc/recherche?text={query}",
    "carrefour": "https://www.carrefour.fr/s?q={query}",
    "intermarche": "https://www.intermarche.com/recherche/{query}",
}

_DRIVE_BASE_URLS = {
    "auchan": "https://www.auchan.fr",
    "leclerc": "https://www.e.leclerc",
    "carrefour": "https://www.carrefour.fr",
    "intermarche": "https://www.intermarche.com",
}

_PRODUCT_EXTRACTION_JS = r"""
(() => {
  const priceText = (node) => {
    const candidates = [
      '[data-testid*=price]', '[class*=price]', '[class*=Price]',
      '[aria-label*=prix i]', '[itemprop=price]'
    ];
    for (const selector of candidates) {
      const found = node.querySelector(selector);
      const value = found?.getAttribute?.('content') || found?.textContent;
      if (value && /\d/.test(value) && /€|eur/i.test(value)) return value.trim();
    }
    const own = node.textContent || '';
    const match = own.match(/\d+[\d\s.,]*\s*€/);
    return match ? match[0].trim() : '';
  };
  const unitText = (node) => {
    const text = node.textContent || '';
    const match = text.match(/\d+[\d\s.,]*\s*€\s*\/\s*(kg|g|l|cl|ml|pièce|unité)/i);
    return match ? match[0].trim() : '';
  };
  const titleText = (node) => {
    const candidates = [
      '[data-testid*=title]', '[class*=title]', '[class*=Title]', 'h2', 'h3', 'a'
    ];
    for (const selector of candidates) {
      const found = node.querySelector(selector);
      const value = found?.getAttribute?.('aria-label') || found?.textContent;
      if (value && value.trim().length > 2) return value.trim();
    }
    return '';
  };
  const nodes = Array.from(document.querySelectorAll(
    '[data-testid*=product], [class*=product], [class*=Product], article, li'
  ));
  const items = [];
  for (const node of nodes) {
    const title = titleText(node);
    const price = priceText(node);
    if (!title || !price) continue;
    const link = node.querySelector('a[href]')?.href || '';
    items.push({ title, price, unitPrice: unitText(node), url: link });
  }
  return { items };
})()
"""


def build_drive_search_query(
    item: ShoppingItem, drive_name: str, product: DriveProduct | None = None
) -> str:
    """Construit une requête stable, adaptée aux marques distributeur."""
    if product is None:
        return normalize_name(item.name)

    if product.brand_type == BrandType.GENERIC:
        return product.name
    if product.brand_type == BrandType.STORE_BRAND:
        if product.store_brand_affinity == normalize_name(drive_name) and product.brand:
            return f"{product.name} {product.brand}".strip()
        return product.name
    if product.brand:
        return f"{product.name} {product.brand}".strip()
    return product.name


def build_drive_search_plan(
    items: list[ShoppingItem], drive_name: str, products: dict[str, DriveProduct] | None = None
) -> list[DriveSearchQuery]:
    products = products or {}
    plan: list[DriveSearchQuery] = []
    for item in items:
        product = products.get(normalize_name(item.name))
        plan.append(
            DriveSearchQuery(
                item=item,
                query=build_drive_search_query(item, drive_name, product),
                confidence="high" if product else "low",
            )
        )
    return plan


def drive_search_url(drive_name: str, query: str) -> str:
    template = _DRIVE_SEARCH_URLS.get(normalize_name(drive_name))
    encoded = quote_plus(query)
    if template is None:
        return f"https://www.google.com/search?q={quote_plus(f'{drive_name} drive {query}')}"
    return template.format(query=encoded)


def open_drive_searches(
    items: list[ShoppingItem],
    drive_name: str,
    browser: ManagedBrowserClient,
    products: dict[str, DriveProduct] | None = None,
) -> list[BrowserSearchResult]:
    results: list[BrowserSearchResult] = []
    for entry in build_drive_search_plan(items, drive_name, products):
        url = drive_search_url(drive_name, entry.query)
        browser_result = browser.navigate(url)
        results.append(BrowserSearchResult(entry=entry, url=url, browser_result=browser_result))
    return results


def collect_drive_offers(
    items: list[ShoppingItem],
    drive_name: str,
    browser: ManagedBrowserClient,
    products: dict[str, DriveProduct] | None = None,
    max_results: int = 5,
) -> list[StoreOffer]:
    """Ouvre les recherches drive puis extrait les premières offres visibles."""
    offers: list[StoreOffer] = []
    for search in open_drive_searches(items, drive_name, browser, products):
        tab_id = search.browser_result.data.get("tabId") or search.browser_result.data.get(
            "currentTabId"
        )
        payload = browser.console_eval(_PRODUCT_EXTRACTION_JS, tab_id=tab_id).data
        raw_items = payload.get("items", []) if isinstance(payload, dict) else []
        item_offers: list[StoreOffer] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            offer = _offer_from_browser_item(search.entry.item, drive_name, raw)
            if offer is not None:
                item_offers.append(offer)
        scored = sorted(
            (score_offer(search.entry.item, offer) for offer in item_offers),
            key=lambda offer_score: (
                offer_score.score,
                -(offer_score.offer.unit_price or offer_score.offer.price),
                -offer_score.offer.price,
            ),
            reverse=True,
        )
        offers.extend(score.offer for score in scored[:max_results])
    return offers


def best_offer_for_item(item: ShoppingItem, offers: list[StoreOffer]) -> OfferScore | None:
    """Choisit l'offre la plus pertinente pour une ligne de courses."""
    scored = [score_offer(item, offer) for offer in offers if offer.item == item.name]
    if not scored:
        return None
    return max(
        scored,
        key=lambda scored_offer: (
            scored_offer.score,
            -(scored_offer.offer.unit_price or scored_offer.offer.price),
            -scored_offer.offer.price,
        ),
    )


def score_offer(item: ShoppingItem, offer: StoreOffer) -> OfferScore:
    item_tokens = set(_tokens(item.name))
    product_tokens = set(_tokens(offer.product))
    overlap = (
        len(item_tokens & product_tokens) / len(item_tokens | product_tokens)
        if item_tokens
        else 0.0
    )
    containment = 1.0 if normalize_name(item.name) in normalize_name(offer.product) else 0.0
    synonym = 1.0 if _has_synonym_match(item.name, offer.product) else 0.0
    confidence_bonus = {"exact": 0.15, "high": 0.10, "medium": 0.05}.get(offer.confidence, 0.0)
    score = min(1.0, 0.65 * overlap + 0.2 * containment + 0.15 * synonym + confidence_bonus)
    parts = [f"tokens {overlap:.2f}"]
    if containment:
        parts.append("nom inclus")
    if synonym:
        parts.append("synonyme")
    if confidence_bonus:
        parts.append(f"confiance {offer.confidence}")
    return OfferScore(offer=offer, score=score, reason=", ".join(parts))


def _offer_from_browser_item(
    item: ShoppingItem, drive_name: str, raw: dict[str, object]
) -> StoreOffer | None:
    title = str(raw.get("title") or raw.get("product") or "").strip()
    price = _parse_euro_price(raw.get("price"))
    if not title or price is None:
        return None
    unit_price = _parse_euro_price(raw.get("unitPrice") or raw.get("unit_price"))
    url = _absolute_product_url(drive_name, raw.get("url"))
    candidate = StoreOffer(
        store=normalize_name(drive_name),
        item=item.name,
        product=title,
        price=price,
        unit_price=unit_price,
        confidence="medium",
        url=url,
    )
    scored = score_offer(item, candidate)
    if normalize_name(item.name) in normalize_name(candidate.product):
        confidence = "exact"
    elif scored.score >= 0.75:
        confidence = "exact"
    elif scored.score >= 0.5:
        confidence = "high"
    elif scored.score >= 0.25:
        confidence = "medium"
    else:
        confidence = "low"
    return candidate.model_copy(update={"confidence": confidence})


def _parse_euro_price(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value) if value > 0 else None
    text = str(value).replace("\xa0", " ").strip()
    match = re.search(r"(\d+(?:[\s.]\d{3})*(?:[,.]\d{1,2})?|\d+)", text)
    if not match:
        return None
    number = match.group(1).replace(" ", "").replace(".", "").replace(",", ".")
    try:
        parsed = float(number)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _absolute_product_url(drive_name: str, value: object) -> str | None:
    if not value:
        return None
    url = str(value).strip()
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        return url
    base_url = _DRIVE_BASE_URLS.get(normalize_name(drive_name))
    return urljoin(base_url, url) if base_url else url


def _tokens(text: str) -> list[str]:
    cleaned = normalize_name(text).replace("'", " ").replace("-", " ")
    return [token for token in cleaned.split() if token not in _STOPWORDS and len(token) > 1]


def _has_synonym_match(item_name: str, product_name: str) -> bool:
    item = normalize_name(item_name)
    product = normalize_name(product_name)
    for key, synonyms in _SYNONYMS.items():
        candidates = (key, *synonyms)
        if any(candidate in item for candidate in candidates):
            return any(candidate in product for candidate in candidates)
    return False
