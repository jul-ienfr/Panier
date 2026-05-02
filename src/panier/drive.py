from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote_plus

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


def build_drive_search_query(
    item: ShoppingItem, drive_name: str, product: DriveProduct | None = None
) -> str:
    """Construit une requête stable, adaptée aux marques distributeur.

    Règle reprise des apps drive inspectées :
    - produit courant avec marque connue -> nom + marque ;
    - marque distributeur du drive ciblé -> nom + marque ;
    - marque distributeur d'un autre drive -> nom seul ;
    - générique -> nom seul.
    """
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


_DRIVE_SEARCH_URLS = {
    "leclerc": "https://www.e.leclerc/recherche?text={query}",
    "carrefour": "https://www.carrefour.fr/s?q={query}",
    "intermarche": "https://www.intermarche.com/recherche/{query}",
    "auchan": "https://www.auchan.fr/recherche?text={query}",
}


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


def best_offer_for_item(item: ShoppingItem, offers: list[StoreOffer]) -> OfferScore | None:
    """Choisit l'offre la plus pertinente pour une ligne de courses.

    Le tri privilégie d'abord la similarité sémantique nom produit <-> besoin,
    puis le prix unitaire/prix. C'est la version déterministe CLI du choix IA
    observé dans l'extension Leclerc : respecter le type demandé, puis optimiser le prix.
    """
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
