from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote_plus, urljoin

from panier.catalog import DEFAULT_SYNONYMS, ProductCatalog, ResolutionStatus, resolve_item
from panier.managed_browser import (
    BrowserCommandResult,
    ManagedBrowserClient,
    ManagedBrowserError,
)
from panier.models import ShoppingItem, StoreOffer, fold_name, normalize_name


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

# Nombre de tokens de tête où le libellé doit apparaître pour être considéré
# comme le produit principal (« Tomates pelées … ») et non un ingrédient
# secondaire d'une préparation (« … poulet paprika et tomates »).
_HEAD_TOKEN_WINDOW = 3

# Drive Leclerc par défaut (magasin Ville-la-Grand / Annemasse). Le magasin
# historique Viuz-en-Sallaz (027419) a fermé ; surcharger via
# PANIER_LECLERC_STORE_URL pour changer de drive sans commit.
DEFAULT_LECLERC_STORE_URL = (
    "https://fd2-courses.leclercdrive.fr/magasin-027411-001261-ville-la-grand-annemasse-"
)
LECLERC_STORE_URL_ENV_VAR = "PANIER_LECLERC_STORE_URL"


# Drive Super U / Courses U par défaut (Roujan, prouvé fonctionnel le
# 2026-09-29 : recherche /recherche?q= OK sans login, sans challenge).
# Courses U exige un contexte magasin pour prix/panier ; surcharger via
# PANIER_SUPERU_STORE_URL pour pointer son propre drive sans commit.
DEFAULT_SUPERU_STORE_URL = "https://www.coursesu.com/drive-superu-roujan"
SUPERU_STORE_URL_ENV_VAR = "PANIER_SUPERU_STORE_URL"


def superu_store_base_url(environ: dict[str, str] | None = None) -> str:
    values = os.environ if environ is None else environ
    raw = values.get(SUPERU_STORE_URL_ENV_VAR, "").strip()
    url = raw or DEFAULT_SUPERU_STORE_URL
    return url.rstrip("/")


def leclerc_store_base_url(environ: dict[str, str] | None = None) -> str:
    values = os.environ if environ is None else environ
    raw = values.get(LECLERC_STORE_URL_ENV_VAR, "").strip()
    url = raw or DEFAULT_LECLERC_STORE_URL
    return url.rstrip("/")


_DRIVE_SEARCH_URLS = {
    "auchan": "https://www.auchan.fr/recherche?text={query}",
    "leclerc": "/recherche.aspx?TexteRecherche={query}&tri=1",
    "carrefour": "https://www.carrefour.fr/s?q={query}",
    "intermarche": "https://www.intermarche.com/recherche/{query}",
    "superu": "https://www.coursesu.com/recherche?q={query}",
}

_DRIVE_BASE_URLS = {
    "auchan": "https://www.auchan.fr",
    "leclerc": DEFAULT_LECLERC_STORE_URL,
    "carrefour": "https://www.carrefour.fr",
    "intermarche": "https://www.intermarche.com",
    "superu": "https://www.coursesu.com",
}

# Variantes saisies par l'utilisateur ("super u", "courses u", "coursesu")
# → clé canonique "superu". normalize_name("super u") == "super u" (espaces
# conservées), donc la table directe _DRIVE_SEARCH_URLS ne suffit pas.
_DRIVE_ALIASES = {
    "super u": "superu",
    "coursesu": "superu",
    "courses u": "superu",
    "u drive": "superu",
}


def canonical_drive_name(drive_name: str) -> str:
    normalized = normalize_name(drive_name)
    return _DRIVE_ALIASES.get(normalized, normalized)


def drive_store_url_for_profile(profile: str | None, drive_name: str) -> str | None:
    """URL magasin imposée par le profil navigateur (centralisation profil→magasin).

    - profile == "courses-superu" ou drive canonique "superu" →
      superu_store_base_url() (défaut Roujan, surchargeable via
      PANIER_SUPERU_STORE_URL) ;
    - drive canonique "leclerc" → leclerc_store_base_url() ;
    - sinon None (aucun contexte magasin requis).
    """
    if (profile or "").strip().lower() == "courses-superu" or canonical_drive_name(
        drive_name
    ) == "superu":
        return superu_store_base_url()
    if canonical_drive_name(drive_name) == "leclerc":
        return leclerc_store_base_url()
    return None

_PRODUCT_EXTRACTION_JS = r"""
(async () => {
  /* Les catalogues hydratent leurs prix après le rendu initial : scroll de
     réveil du lazy-loading puis stabilisation courte avant extraction. */
  const settle = async () => {
    window.scrollTo(0, Math.min(1200, document.body?.scrollHeight || 0));
    await new Promise((resolve) => setTimeout(resolve, 1200));
    window.scrollTo(0, 0);
    await new Promise((resolve) => setTimeout(resolve, 600));
  };
  try { await settle(); } catch (err) { /* scroll best-effort */ }
  const normalizedText = (node) => (node.textContent || '')
    .replace(/\u00a0/g, ' ')
    .replace(/\u202f/g, ' ')
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
  };  const unitText = (node) => {
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
      /* Les libellés des drives sont multilignes et préfixés par des compteurs
         (« (62) », avis) : on les replie pour que le matching travaille sur un
         titre propre, sinon « (62) Tomates rondes » n'est plus reconnu. */
      const clean = normalizedText({ textContent: value })
        .replace(/^\(\s*\d+\s*\)\s*/, '')
        /* Les cartes concatènent le prix dans le texte du titre :
           « Riz long grain 1kg 1,89€ / kg » → on coupe au prix pour ne garder
           que la désignation produit (le prix est extrait séparément). */
        .replace(/\s*\d+[,.]\d{1,2}\s*€(\s*\/\s*(kg|g|l|cl|ml|pièce|unité))?\s*$/i, '')
        .trim();
      if (clean.length > 2) return clean;
    }
    return '';
  };
  /* Badge promo : chaque drive l'expose dans son propre DOM (Carrefour
     « sticker-promo », Auchan « product-discount-label », Super U
     « product-promotion-banner »). On lit l'étiquette réelle plutôt que de
     déduire une promo d'une baisse de prix entre deux collectes : la collecte
     ne voit qu'un instantané, le drive sait qu'il y a une offre. */
  const promoLabelText = (node) => {
    const prioritized = [
      '.sticker-promo__text', '.product-discount-label',
      '.product-promotion-banner', '[class*=promo i]', '[class*=discount i]',
      '[class*=remise i]', '[class*=reduction i]', '[class*=prix-choc i]'
    ];
    for (const selector of prioritized) {
      for (const found of node.querySelectorAll(selector)) {
        const label = normalizedText(found);
        if (
          label
          && label.length <= 60
          && /promo|discount|remise|reduc|%|choc|mini|euro|€|cagnott|club|anti.?gaspi/i.test(label)
        ) {
          return label;
        }
      }
    }
    return '';
  };
  const nodes = Array.from(document.querySelectorAll(
    '.liWCRS310_Product, [data-testid*=product], [class*=product], [class*=Product], article, li'
  ));
  const items = [];
  const seen = new Set();
  for (const node of nodes) {
    const title = titleText(node);
    const price = priceText(node);
    if (!title || !price) continue;
    /* Les sélecteurs sont larges (article, li, [class*=product]) et un même
       produit apparaît dans plusieurs conteneurs imbriqués : sans dédup, le
       même article était enregistré 2 à 3 fois par collecte, ce qui gonflait
       artificiellement les observations de l'historique. */
    const key = `${title}|${price}|${unitText(node)}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const link = node.querySelector('a[href]')?.href || '';
    const promoLabel = promoLabelText(node);
    items.push({
      title, price, unitPrice: unitText(node), url: link,
      promo: promoLabel !== '', promoLabel,
    });
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
    normalized_drive = canonical_drive_name(drive_name)
    encoded = quote_plus(query)
    if normalized_drive == "leclerc":
        template = leclerc_store_base_url() + _DRIVE_SEARCH_URLS["leclerc"]
    else:
        template = _DRIVE_SEARCH_URLS.get(normalized_drive, "")
    if not template:
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


def _extract_items_with_retry(
    browser: ManagedBrowserClient, url: str, tab_id: str | None
) -> list[dict[str, object]]:
    """Extraction avec replis : DataDome peut servir une page vide au premier
    hit, et les catalogues hydratent tard. Un résultat VIDE n'est pas une
    erreur pour page.evaluate : on relit, puis on re-navigue une fois."""
    import time

    items: list[dict[str, object]] = []
    for attempt in range(3):
        if attempt == 1:
            time.sleep(3.0)
        if attempt == 2:
            try:
                browser.navigate(url)
                time.sleep(3.0)
            except ManagedBrowserError:
                pass
        payload = _browser_value(browser.console_eval(_PRODUCT_EXTRACTION_JS, tab_id=tab_id).data)
        raw_items = payload.get("items", []) if isinstance(payload, dict) else []
        items = [raw for raw in raw_items if isinstance(raw, dict)]
        if items:
            break
    return items


def _offer_identity(offer: StoreOffer) -> str:
    """Clé de dédup : libellé replié + prix + prix au kilo.

    Le JS d'extraction voit parent+enfant, et certains drives exposent le même
    produit sous plusieurs identifiants (Auchan : `pr-C1240700` vs
    `pr-C1224692`). L'URL n'est donc pas une clé fiable ; deux cartes de même
    libellé au même prix sont le même produit.
    """
    return f"{fold_name(offer.product)}|{offer.price}|{offer.unit_price}"


def _dedupe_offers(offers: list[StoreOffer]) -> list[StoreOffer]:
    """Déduplique en préservant l'ordre (JS d'extraction voit parent+enfant).

    Un même produit peut apparaître deux fois, une carte portant le badge promo
    et l'autre non (parent/enfant, variantes de gabarit) : on garde alors la
    version qui porte l'information promo, sinon on perd l'offre.
    """
    index_by_key: dict[str, int] = {}
    unique: list[StoreOffer] = []
    for offer in offers:
        key = _offer_identity(offer)
        position = index_by_key.get(key)
        if position is None:
            index_by_key[key] = len(unique)
            unique.append(offer)
            continue
        current = unique[position]
        if offer.promo and not current.promo:
            unique[position] = offer
    return unique


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
        normalized_drive = canonical_drive_name(drive_name)
        url = drive_search_url(
            drive_name,
            entry.query,
            tri=_leclerc_sort_for_item(entry.item) if normalized_drive == "leclerc" else None,
        )
        if normalized_drive == "superu":
            # Courses U exige un contexte magasin : poser le magasin imposé par
            # le profil (défaut Roujan, surchargeable via
            # PANIER_SUPERU_STORE_URL) avant la recherche globale /recherche?q=.
            store_url = drive_store_url_for_profile(
                getattr(browser, "profile", None), drive_name
            )
            if store_url:
                try:
                    browser.navigate(store_url)
                except ManagedBrowserError:
                    pass
        browser_result = browser.navigate(url)
        search = BrowserSearchResult(entry=entry, url=url, browser_result=browser_result)
        tab_id = _browser_tab_id(browser_result.data)
        raw_items = _extract_items_with_retry(browser, url, tab_id)
        item_offers: list[StoreOffer] = []
        for raw in raw_items:
            offer = _offer_from_browser_item(search.entry.item, drive_name, raw)
            if offer is not None:
                item_offers.append(offer)
        scored = _strict_sorted_offers(search.entry.item, _dedupe_offers(item_offers))
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
    ranked = sorted(
        eligible,
        key=lambda offer_score: (
            _offer_compare_value(offer_score.offer, compare_by),
            float(offer_score.offer.price),
            -offer_score.score,
            offer_score.offer.product,
        ),
    )
    return _maybe_jev_reorder(item, ranked)


def _maybe_jev_reorder(item: ShoppingItem, ranked: list[OfferScore]) -> list[OfferScore]:
    """Second avis JEV consultatif sur le top-2 proche (fail-open, jamais d'écriture).

    OFF (``PANIER_JEV_CALIB != 1`` ou ``PANIER_NO_LLM`` actif) → ``ranked``
    inchangé (byte-identical). ON : un seul appel, uniquement si le top-2
    est proche (prix à 10 % près ET écart de score ≤ 0.15) ; swap du top-2
    uniquement si confiance JEV ≥ 0.7. Toute erreur → baseline. Ne lève
    jamais.
    """
    if len(ranked) < 2:
        return ranked
    try:
        from panier import jev_advice
    except ImportError:
        return ranked
    try:
        if not jev_advice.jev_calib_enabled():
            return ranked
        head, second = ranked[0], ranked[1]
        cheaper = min(float(head.offer.price), float(second.offer.price))
        dearer = max(float(head.offer.price), float(second.offer.price))
        price_close = cheaper > 0 and (dearer - cheaper) / cheaper <= 0.10
        score_close = abs(head.score - second.score) <= 0.15
        if not (price_close and score_close):
            return ranked
        summaries = [
            {"product": s.offer.product, "price": float(s.offer.price),
             "unit_price": s.offer.unit_price, "score": round(s.score, 3),
             "reason": s.reason}
            for s in ranked[:4]
        ]
        out = jev_advice.advise_best_offer(item.name, summaries)
        picked = out.get("picked_index")
        if out.get("status") != "ok" or not isinstance(picked, int):
            return ranked
        if picked == 0 or not (0 <= picked < len(ranked)):
            return ranked
        new_head = ranked[picked]
        rest = [s for i, s in enumerate(ranked) if i != picked]
        enriched = OfferScore(
            offer=new_head.offer,
            score=new_head.score,
            reason=f"{new_head.reason} [jev:pick]",
        )
        return [enriched] + rest
    except Exception:
        return ranked


def _is_strict_equivalent(item: ShoppingItem, offer: StoreOffer, score: float) -> bool:
    item_name = fold_name(item.name)
    product_name = fold_name(offer.product)
    if _violates_strict_exclusions(item_name, product_name):
        return False
    item_tokens = set(_tokens(item_name))
    product_tokens = set(_tokens(product_name))
    if not item_tokens:
        return score >= 0.5
    required = _strict_required_tokens(item_tokens)
    if not required.issubset(product_tokens):
        # Synonyme métier (« tomates concassées » ≈ « pulpe de tomate ») : les
        # tokens littéraux diffèrent, mais le produit reste le bon. Exiger que
        # le synonyme soit en tête évite de valider une simple mention en fin
        # de composition (« plat cuisiné … pulpe de tomate »).
        return _synonym_head_match(item_name, product_name)
    # Le libellé doit désigner le produit, pas figurer en fin de composition :
    # « Palets de légumes ... courgettes » ou « Sachet cuisson poulet tomates »
    # contiennent le mot sans être l'ingrédient demandé.
    if not _tokens_head(product_name, _HEAD_TOKEN_WINDOW) & required:
        return False
    return score >= 0.1 or item_name in product_name


def _tokens_head(text: str, window: int) -> set[str]:
    return set(_tokens(text)[:window])


def _synonym_head_match(item_name: str, product_name: str) -> bool:
    """Le synonyme du produit demandé apparaît-il en tête du libellé ?"""
    item = fold_name(item_name)
    head = set(_tokens(product_name)[:_HEAD_TOKEN_WINDOW])
    if not head:
        return False
    for key, values in _SYNONYMS.items():
        candidates = tuple(fold_name(candidate) for candidate in (key, *values))
        if not any(candidate in item for candidate in candidates):
            continue
        for candidate in candidates:
            candidate_tokens = _tokens(candidate)
            if candidate_tokens and set(candidate_tokens).issubset(head):
                return True
    return False


def _strict_required_tokens(tokens: set[str]) -> set[str]:
    required = set(tokens)
    if "nature" in required:
        required.remove("nature")
    return required


# Formes de produit qui ne sont jamais un équivalent d'un ingrédient brut :
# un pot pour bébé, une soupe ou une sauce tomate ne remplacent pas des tomates.
_FORBIDDEN_PRODUCT_FORMS = {
    "bebe",
    "baby",
    "4m",
    "6m",
    "8m",
    "12m",
    "soupe",
    "potage",
    "veloute",
    "sauce",
    "puree",
    "coulis",
    "concentre",
    "croquettes",
    "patee",
    "chat",
    "chien",
    "salade",
    "plat",
    "cuisine",
    "prepare",
    "dessert",
    "glace",
    "boisson",
    "chips",
    "aperitif",
    "taboule",
    "gratin",
    "risotto",
    "pane",
    "galette",
    "beignet",
    "quiche",
    "pizza",
    "lasagne",
    "cake",
    "tarte",
}


def _violates_strict_exclusions(item_name: str, product_name: str) -> bool:
    item_tokens = set(_tokens(item_name))
    product_tokens_all = set(_tokens(product_name))
    # Comparaison par préfixe : les drives accordent et dérivent (« cuisinées »
    # pour « cuisine »). Seules les formes d'au moins 5 lettres sont préfixées,
    # sinon « chat » intercepterait « châtaigne ».
    forbidden = {
        token
        for token in product_tokens_all
        if any(len(form) >= 5 and token.startswith(form) for form in _FORBIDDEN_PRODUCT_FORMS)
        or token in _FORBIDDEN_PRODUCT_FORMS
    }
    if forbidden - item_tokens:
        return True
    exclusion_by_item = {
        "quinoa": {"boulgour", "ble", "lentilles", "carottes", "duo", "melange", "mélange"},
        "thon": {"huile"},
    }
    product_tokens = set(_tokens(product_name))
    product_text = fold_name(product_name)
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
    promo_label = str(raw.get("promoLabel") or "").strip()
    candidate = StoreOffer(
        store=canonical_drive_name(drive_name),
        item=item.name,
        product=title,
        price=price,
        unit_price=unit_price,
        confidence="medium",
        url=url,
        promo=bool(raw.get("promo")) or bool(promo_label),
        promo_label=promo_label or None,
    )
    scored = score_offer(item, candidate)
    # La confiance reflète le matching strict, pas le score lexical ni la
    # simple inclusion du libellé : « petits pots bébé courgettes » contient
    # bien « courgettes » mais n'est pas un équivalent. Sans ce garde-fou,
    # tout produit mentionnant le mot s'affichait « exact ».
    item_folded = fold_name(item.name)
    product_folded = fold_name(candidate.product)
    if _violates_strict_exclusions(item_folded, product_folded):
        confidence = "low"
    elif _is_strict_equivalent(item, candidate, scored.score):
        if item_folded in product_folded:
            confidence = "exact"
        elif _synonym_head_match(item_folded, product_folded) or scored.score >= 0.5:
            confidence = "high"
        else:
            confidence = "medium"
    elif scored.score >= 0.4:
        confidence = "medium"
    else:
        confidence = "low"
    return candidate.model_copy(update={"confidence": confidence})


# « 3 euros 84 centimes » : les drives accessibles annoncent parfois le prix
# en toutes lettres avant le format numérique.
_VERBAL_PRICE_RE = re.compile(
    r"(\d+)\s*euros?(?:\s*(?:et\s*)?(\d{1,2})\s*centimes?)?",
    re.IGNORECASE,
)
# « 1€99 » / « 0€89 » : centimes collés après le symbole, sans séparateur.
_CENTS_AFTER_SYMBOL_RE = re.compile(r"(\d+)\s*(?:€|eur)\s*(\d{2})(?!\d)", re.IGNORECASE)
# Nombre monétaire : chiffres, espaces (milliers Carrefour « 1 234 ») et un
# séparateur éventuellement entouré d'espaces (« 3  ,59  € »).
_MONEY_AMOUNT_RE = re.compile(
    r"(\d+(?:[\s.\u00a0\u202f]\d{3})*(?:\s*[.,]\s*\d{1,2})?|\d+)"
)


def _decimal_from_amount(fragment: str) -> float | None:
    """Convertit « 1 234,56 », « 3  ,59 » ou « 3.59 » en flottant."""
    number = re.sub(r"[\s\u00a0\u202f]", "", fragment).strip(".,")
    if not number:
        return None
    if "," in number and "." in number:
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")
    elif "," in number:
        head, _, tail = number.rpartition(",")
        if len(tail) in (1, 2) and tail.isdigit():
            number = f"{head.replace(',', '')}.{tail}"
        else:
            number = head.replace(",", "") + tail
    elif "." in number:
        head, _, tail = number.rpartition(".")
        if len(tail) in (1, 2) and tail.isdigit():
            number = f"{head.replace('.', '')}.{tail}"
        else:
            number = head.replace(".", "") + tail
    try:
        return float(number)
    except ValueError:
        return None


def _parse_euro_price(value: object) -> float | None:
    """Extrait un prix en euros, quel que soit le format du drive.

    Formats réellement observés : « 1,99 € », « 3  ,59  € » (Carrefour),
    « 3 euros 84 centimes » (Super U, prix en toutes lettres), « 1€99 »,
    « 1 234,56 € ». Le premier montant rencontré gagne : les textes Super U
    concatènent prix, prix au kilo puis re-prix, tous dans l'ordre.
    """
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value) if value > 0 else None
    text = re.sub(r"[\s\u00a0\u202f]+", " ", str(value)).strip()
    if not text:
        return None
    verbal = _VERBAL_PRICE_RE.search(text)
    if verbal is not None:
        euros = int(verbal.group(1))
        cents = int(verbal.group(2) or 0)
        parsed = euros + cents / 100
        return parsed if parsed > 0 else None
    glued = _CENTS_AFTER_SYMBOL_RE.search(text)
    if glued is not None:
        parsed = int(glued.group(1)) + int(glued.group(2)) / 100
        return parsed if parsed > 0 else None
    match = _MONEY_AMOUNT_RE.search(text)
    if not match:
        return None
    amount = _decimal_from_amount(match.group(1))
    if amount is None or amount <= 0:
        return None
    return amount


def _absolute_product_url(drive_name: str, value: object) -> str | None:
    if not value:
        return None
    url = str(value).strip()
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        return url
    canonical = canonical_drive_name(drive_name)
    if canonical == "leclerc":
        return urljoin(leclerc_store_base_url(), url)
    base_url = _DRIVE_BASE_URLS.get(canonical)
    return urljoin(base_url, url) if base_url else url


def _tokens(text: str) -> list[str]:
    cleaned = fold_name(text).replace("'", " ").replace("-", " ")
    return [token for token in cleaned.split() if token not in _STOPWORDS and len(token) > 1]


def _has_synonym_match(
    item_name: str, product_name: str, synonyms: dict[str, tuple[str, ...]] | None = None
) -> bool:
    item = fold_name(item_name)
    product = fold_name(product_name)
    for key, values in (synonyms or _SYNONYMS).items():
        candidates = (key, *values)
        if any(candidate in item for candidate in candidates):
            return any(candidate in product for candidate in candidates)
    return False
