from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote_plus, urljoin

from panier.catalog import DEFAULT_SYNONYMS, ProductCatalog, ResolutionStatus, resolve_item
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

_SYNONYMS: dict[str, tuple[str, ...]] = DEFAULT_SYNONYMS.copy()

_LECLERC_VIUZ_BASE_URL = "https://fd2-courses.leclercdrive.fr/magasin-027419-027419-Viuz-en-Sallaz"

_DRIVE_SEARCH_URLS = {
    "auchan": "https://www.auchan.fr/recherche?text={query}",
    "leclerc": f"{_LECLERC_VIUZ_BASE_URL}/recherche.aspx?TexteRecherche={{query}}&tri=1",
    "carrefour": "https://www.carrefour.fr/s?q={query}",
    "intermarche": "https://www.intermarche.com/recherche/{query}",
}

_DRIVE_BASE_URLS = {
    "auchan": "https://www.auchan.fr",
    "leclerc": _LECLERC_VIUZ_BASE_URL,
    "carrefour": "https://www.carrefour.fr",
    "intermarche": "https://www.intermarche.com",
}

_PRODUCT_EXTRACTION_JS = r"""
(() => {
  const normalizedText = (node) => (node.textContent || '')
    .replace(/\u00a0/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  const leclercProductText = (node) => {
    const title = node.querySelector?.('.aWCRS310_Product')?.textContent?.trim() || '';
    const text = normalizedText(node);
    const match = text.match(/(\d+)\s*€\s*,\s*(\d{1,2})/);
    if (!match) return null;
    const price = `${match[1]},${match[2]} €`;
    const afterPrice = text.slice((match.index || 0) + match[0].length);
    const unitPattern = /\d+(?:[,.]\d{1,2})?\s*€\s*\/\s*(kg|g|l|cl|ml|pièce|unité)/i;
    const unitMatch = afterPrice.match(unitPattern);
    const fallbackTitle = text.split(/Ajouter au panier|Bientôt disponible/)[0].trim();
    return {
      title: title || fallbackTitle,
      price,
      unitPrice: unitMatch?.[0]?.trim() || '',
    };
  };
  const priceText = (node) => {
    const leclerc = leclercProductText(node);
    if (leclerc?.price) return leclerc.price;
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
    const leclerc = leclercProductText(node);
    if (leclerc?.unitPrice) return leclerc.unitPrice;
    const text = node.textContent || '';
    const match = text.match(/\d+[\d\s.,]*\s*€\s*\/\s*(kg|g|l|cl|ml|pièce|unité)/i);
    return match ? match[0].trim() : '';
  };
  const titleText = (node) => {
    const leclerc = leclercProductText(node);
    if (leclerc?.title) return leclerc.title;
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
    '.liWCRS310_Product, [data-testid*=product], [class*=product], [class*=Product], article, li'
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
    items: list[ShoppingItem],
    drive_name: str,
    products: dict[str, DriveProduct] | None = None,
    catalog: ProductCatalog | None = None,
) -> list[DriveSearchQuery]:
    products = products or {}
    plan: list[DriveSearchQuery] = []
    for item in items:
        product = products.get(normalize_name(item.name))
        if product is not None:
            plan.append(
                DriveSearchQuery(
                    item=item,
                    query=build_drive_search_query(item, drive_name, product),
                    confidence="high",
                )
            )
            continue
        if catalog is not None:
            resolved = resolve_item(item, catalog, drive_name=drive_name)
            if resolved.status != ResolutionStatus.UNRESOLVED:
                plan.append(
                    DriveSearchQuery(
                        item=item,
                        query=resolved.query,
                        confidence=_resolution_confidence(resolved.status),
                    )
                )
                continue
        plan.append(
            DriveSearchQuery(
                item=item,
                query=build_drive_search_query(item, drive_name, None),
                confidence="low",
            )
        )
    return plan


def _resolution_confidence(status: ResolutionStatus) -> str:
    return {
        ResolutionStatus.EXACT_CATALOG: "exact",
        ResolutionStatus.EXACT_ALIAS: "high",
        ResolutionStatus.FUZZY_LOCAL: "medium",
    }.get(status, "low")


def drive_search_url(drive_name: str, query: str, *, tri: int | None = None) -> str:
    normalized_drive = normalize_name(drive_name)
    template = _DRIVE_SEARCH_URLS.get(normalized_drive)
    encoded = quote_plus(query)
    if template is None:
        return f"https://www.google.com/search?q={quote_plus(f'{drive_name} drive {query}')}"
    url = template.format(query=encoded)
    if normalized_drive == "leclerc" and tri is not None:
        url = re.sub(r"([?&])tri=\d+", rf"\g<1>tri={tri}", url)
    return url


def open_drive_searches(
    items: list[ShoppingItem],
    drive_name: str,
    browser: ManagedBrowserClient,
    products: dict[str, DriveProduct] | None = None,
    catalog: ProductCatalog | None = None,
) -> list[BrowserSearchResult]:
    results: list[BrowserSearchResult] = []
    for entry in build_drive_search_plan(items, drive_name, products, catalog):
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
    catalog: ProductCatalog | None = None,
) -> list[StoreOffer]:
    """Ouvre les recherches drive puis extrait les premières offres visibles."""
    offers: list[StoreOffer] = []
    for entry in build_drive_search_plan(items, drive_name, products, catalog):
        normalized_drive = normalize_name(drive_name)
        url = drive_search_url(
            drive_name,
            entry.query,
            tri=_leclerc_sort_for_item(entry.item) if normalized_drive == "leclerc" else None,
        )
        browser_result = browser.navigate(url)
        search = BrowserSearchResult(entry=entry, url=url, browser_result=browser_result)
        tab_id = _browser_tab_id(browser_result.data)
        payload = _browser_value(browser.console_eval(_PRODUCT_EXTRACTION_JS, tab_id=tab_id).data)
        raw_items = payload.get("items", []) if isinstance(payload, dict) else []
        item_offers: list[StoreOffer] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            offer = _offer_from_browser_item(search.entry.item, drive_name, raw)
            if offer is not None:
                item_offers.append(offer)
        scored = _strict_sorted_offers(search.entry.item, item_offers)
        offers.extend(score.offer for score in scored[:max_results])
    return offers


def _browser_value(payload: dict) -> object:
    if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
        return payload["result"].get("value", payload["result"])
    return payload


def _browser_tab_id(payload: dict) -> str | None:
    value = _browser_value(payload)
    if not isinstance(value, dict):
        return None
    return value.get("tabId") or value.get("tab_id") or value.get("currentTabId")


def best_offer_for_item(
    item: ShoppingItem, offers: list[StoreOffer], compare_by: str = "price"
) -> OfferScore | None:
    """Choisit l'offre la plus pertinente pour une ligne de courses."""
    scored = _strict_sorted_offers(
        item, [offer for offer in offers if offer.item == item.name], compare_by
    )
    return scored[0] if scored else None


def score_offer(
    item: ShoppingItem, offer: StoreOffer, synonyms: dict[str, tuple[str, ...]] | None = None
) -> OfferScore:
    item_tokens = set(_tokens(item.name))
    product_tokens = set(_tokens(offer.product))
    overlap = (
        len(item_tokens & product_tokens) / len(item_tokens | product_tokens)
        if item_tokens
        else 0.0
    )
    containment = 1.0 if normalize_name(item.name) in normalize_name(offer.product) else 0.0
    synonym = 1.0 if _has_synonym_match(item.name, offer.product, synonyms) else 0.0
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


def _offer_compare_value(offer: StoreOffer, compare_by: str) -> float:
    if compare_by == "unit_price" and offer.unit_price is not None:
        return float(offer.unit_price)
    return float(offer.price)


def _strict_sorted_offers(
    item: ShoppingItem, offers: list[StoreOffer], compare_by: str = "unit_price"
) -> list[OfferScore]:
    scored = [score_offer(item, offer) for offer in offers]
    hard_allowed = [
        score
        for score in scored
        if not _violates_strict_exclusions(
            normalize_name(item.name), normalize_name(score.offer.product)
        )
    ]
    eligible = [
        score for score in hard_allowed if _is_strict_equivalent(item, score.offer, score.score)
    ]
    if not eligible:
        eligible = hard_allowed
    return sorted(
        eligible,
        key=lambda offer_score: (
            _offer_compare_value(offer_score.offer, compare_by),
            float(offer_score.offer.price),
            -offer_score.score,
            offer_score.offer.product,
        ),
    )


def _is_strict_equivalent(item: ShoppingItem, offer: StoreOffer, score: float) -> bool:
    item_name = normalize_name(item.name)
    product_name = normalize_name(offer.product)
    if _violates_strict_exclusions(item_name, product_name):
        return False
    item_tokens = set(_tokens(item_name))
    product_tokens = set(_tokens(product_name))
    if not item_tokens:
        return score >= 0.5
    required = _strict_required_tokens(item_tokens)
    if not required.issubset(product_tokens):
        return False
    return score >= 0.1 or item_name in product_name


def _strict_required_tokens(tokens: set[str]) -> set[str]:
    required = set(tokens)
    if "nature" in required:
        required.remove("nature")
    return required


def _violates_strict_exclusions(item_name: str, product_name: str) -> bool:
    exclusion_by_item = {
        "quinoa": {"boulgour", "ble", "lentilles", "carottes", "duo", "melange", "mélange"},
        "thon": {"huile"},
    }
    product_tokens = set(_tokens(product_name))
    product_text = normalize_name(product_name)
    for item_token, excluded_tokens in exclusion_by_item.items():
        if item_token in item_name and (
            product_tokens & excluded_tokens
            or any(excluded in product_text for excluded in excluded_tokens)
        ):
            return True
    if "nature" in item_name and "thon" in item_name and "huile" in product_text:
        return True
    return False


def _leclerc_sort_for_item(item: ShoppingItem) -> int:
    # Leclerc: tri=4 => prix/kg/L croissant, tri=2 => prix croissant.
    # Le matching strict côté Panier reste la vraie barrière anti-substitution ;
    # le tri ne sert qu'à faire remonter les candidats les moins chers ensuite.
    if item.unit and normalize_name(item.unit) in {"kg", "g", "l", "cl", "ml"}:
        return 4
    return 2


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


def _has_synonym_match(
    item_name: str, product_name: str, synonyms: dict[str, tuple[str, ...]] | None = None
) -> bool:
    item = normalize_name(item_name)
    product = normalize_name(product_name)
    for key, values in (synonyms or _SYNONYMS).items():
        candidates = (key, *values)
        if any(candidate in item for candidate in candidates):
            return any(candidate in product for candidate in candidates)
    return False
