from __future__ import annotations

from difflib import SequenceMatcher, get_close_matches
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from panier.models import ShoppingItem, normalize_name

CATALOG_FILENAMES = ("catalog.yaml", "products.yaml")

DEFAULT_SYNONYMS: dict[str, tuple[str, ...]] = {
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


class CatalogBrandType(StrEnum):
    COMMON = "common"
    STORE_BRAND = "store_brand"
    GENERIC = "generic"


class ResolutionStatus(StrEnum):
    EXACT_ALIAS = "exact_alias"
    EXACT_CATALOG = "exact_catalog"
    FUZZY_LOCAL = "fuzzy/local"
    UNRESOLVED = "unresolved"


class CatalogProduct(BaseModel):
    """Deterministic product entry loaded from the local catalog."""

    name: str
    query: str | None = None
    brand: str | None = None
    brand_type: CatalogBrandType = CatalogBrandType.COMMON
    store_brand_affinity: str | None = None
    aliases: tuple[str, ...] = ()
    synonyms: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    @field_validator("name", "query", "brand", "store_brand_affinity", mode="before")
    @classmethod
    def normalize_optional_name(cls, value: object) -> str | None:
        if value is None:
            return None
        text = normalize_name(str(value))
        return text or None

    @field_validator("aliases", "synonyms", "tags", mode="before")
    @classmethod
    def normalize_tuple(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            values = [value]
        else:
            values = list(value)  # type: ignore[arg-type]
        return tuple(
            dict.fromkeys(normalize_name(str(item)) for item in values if str(item).strip())
        )

    @property
    def canonical_query(self) -> str:
        return self.query or self.name


class CatalogResolution(BaseModel):
    """Result of a deterministic local catalog lookup."""

    original: str
    normalized: str
    status: ResolutionStatus
    canonical_name: str | None = None
    query: str
    matched: str | None = None
    score: float = 0.0
    product: CatalogProduct | None = None
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return self.status != ResolutionStatus.UNRESOLVED


class ProductCatalog(BaseModel):
    products: list[CatalogProduct] = Field(default_factory=list)
    aliases: dict[str, str] = Field(default_factory=dict)
    synonyms: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @field_validator("aliases", mode="before")
    @classmethod
    def normalize_aliases(cls, value: object) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise TypeError("catalog aliases must be a mapping")
        return {
            normalize_name(str(alias)): normalize_name(str(target))
            for alias, target in value.items()
            if str(alias).strip() and str(target).strip()
        }

    @field_validator("synonyms", mode="before")
    @classmethod
    def normalize_synonyms(cls, value: object) -> dict[str, tuple[str, ...]]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise TypeError("catalog synonyms must be a mapping")
        normalized: dict[str, tuple[str, ...]] = {}
        for key, raw_values in value.items():
            canonical = normalize_name(str(key))
            if not canonical:
                continue
            if isinstance(raw_values, str):
                values = [raw_values]
            else:
                values = list(raw_values)  # type: ignore[arg-type]
            normalized[canonical] = tuple(
                dict.fromkeys(normalize_name(str(item)) for item in values if str(item).strip())
            )
        return normalized

    @model_validator(mode="after")
    def validate_unique_products(self) -> ProductCatalog:
        seen: set[str] = set()
        for product in self.products:
            if product.name in seen:
                raise ValueError(f"duplicate catalog product: {product.name}")
            seen.add(product.name)
        return self

    def with_defaults(self) -> ProductCatalog:
        return merge_catalogs(default_catalog(), self)

    def product_index(self) -> dict[str, CatalogProduct]:
        return {product.name: product for product in self.products}

    def synonym_mapping(self) -> dict[str, tuple[str, ...]]:
        merged: dict[str, tuple[str, ...]] = {
            key: tuple(values) for key, values in self.synonyms.items()
        }
        for product in self.products:
            if product.synonyms:
                existing = merged.get(product.name, ())
                merged[product.name] = tuple(dict.fromkeys((*existing, *product.synonyms)))
        return merged


def default_catalog() -> ProductCatalog:
    return ProductCatalog(synonyms=DEFAULT_SYNONYMS)


def catalog_path(data_dir: Path) -> Path:
    for filename in CATALOG_FILENAMES:
        path = data_dir / filename
        if path.exists():
            return path
    return data_dir / CATALOG_FILENAMES[0]


def load_catalog(data_dir: Path | None = None, *, include_defaults: bool = True) -> ProductCatalog:
    """Load local deterministic catalog from data_dir (usually ~/.panier).

    Missing catalog files are not an error: callers get the built-in lightweight defaults,
    currently the synonym table historically used by drive scoring.
    """

    base = default_catalog() if include_defaults else ProductCatalog()
    if data_dir is None:
        data_dir = Path.home() / ".panier"
    path = catalog_path(data_dir)
    if not path.exists():
        return base
    loaded = catalog_from_yaml(path)
    return merge_catalogs(base, loaded) if include_defaults else loaded


def catalog_from_yaml(path: Path) -> ProductCatalog:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if isinstance(payload, list):
        payload = {"products": payload}
    return ProductCatalog.model_validate(payload)


def merge_catalogs(*catalogs: ProductCatalog) -> ProductCatalog:
    products_by_name: dict[str, CatalogProduct] = {}
    aliases: dict[str, str] = {}
    synonyms: dict[str, tuple[str, ...]] = {}
    for catalog in catalogs:
        products_by_name.update(catalog.product_index())
        aliases.update(catalog.aliases)
        for key, values in catalog.synonyms.items():
            synonyms[key] = tuple(dict.fromkeys((*synonyms.get(key, ()), *values)))
    return ProductCatalog(
        products=list(products_by_name.values()), aliases=aliases, synonyms=synonyms
    )


def resolve_item(
    item: ShoppingItem | str,
    catalog: ProductCatalog | None = None,
    *,
    drive_name: str | None = None,
    fuzzy_cutoff: float = 0.82,
) -> CatalogResolution:
    name = item.name if isinstance(item, ShoppingItem) else item
    catalog = catalog or load_catalog()
    normalized = normalize_name(name)
    if not normalized:
        return CatalogResolution(
            original=name,
            normalized=normalized,
            status=ResolutionStatus.UNRESOLVED,
            query=normalized,
            reason="empty name",
        )

    product_by_name = catalog.product_index()
    exact = product_by_name.get(normalized)
    if exact is not None:
        return _resolution(
            name, normalized, ResolutionStatus.EXACT_CATALOG, exact, exact.name, 1.0, drive_name
        )

    alias_target = _alias_target(normalized, catalog)
    if alias_target is not None:
        product = product_by_name.get(alias_target)
        if product is not None:
            return _resolution(
                name,
                normalized,
                ResolutionStatus.EXACT_ALIAS,
                product,
                normalized,
                1.0,
                drive_name,
            )
        return CatalogResolution(
            original=name,
            normalized=normalized,
            status=ResolutionStatus.EXACT_ALIAS,
            canonical_name=alias_target,
            query=alias_target,
            matched=normalized,
            score=1.0,
            reason="catalog alias/synonym",
        )

    fuzzy_match = _fuzzy_match(normalized, catalog, fuzzy_cutoff)
    if fuzzy_match is not None:
        matched, target, score = fuzzy_match
        product = product_by_name.get(target)
        if product is not None:
            return _resolution(
                name, normalized, ResolutionStatus.FUZZY_LOCAL, product, matched, score, drive_name
            )
        return CatalogResolution(
            original=name,
            normalized=normalized,
            status=ResolutionStatus.FUZZY_LOCAL,
            canonical_name=target,
            query=target,
            matched=matched,
            score=score,
            reason="local fuzzy match",
        )

    return CatalogResolution(
        original=name,
        normalized=normalized,
        status=ResolutionStatus.UNRESOLVED,
        canonical_name=None,
        query=normalized,
        score=0.0,
        reason="no local catalog match",
    )


def resolve_items(
    items: list[ShoppingItem] | list[str],
    catalog: ProductCatalog | None = None,
    *,
    drive_name: str | None = None,
    fuzzy_cutoff: float = 0.82,
) -> list[CatalogResolution]:
    catalog = catalog or load_catalog()
    return [
        resolve_item(item, catalog, drive_name=drive_name, fuzzy_cutoff=fuzzy_cutoff)
        for item in items
    ]


def drive_query_for_product(product: CatalogProduct, drive_name: str | None = None) -> str:
    if product.brand_type == CatalogBrandType.GENERIC:
        return product.canonical_query
    if product.brand_type == CatalogBrandType.STORE_BRAND:
        if (
            drive_name is not None
            and product.store_brand_affinity == normalize_name(drive_name)
            and product.brand
        ):
            return f"{product.canonical_query} {product.brand}".strip()
        return product.canonical_query
    if product.brand:
        return f"{product.canonical_query} {product.brand}".strip()
    return product.canonical_query


def _resolution(
    original: str,
    normalized: str,
    status: ResolutionStatus,
    product: CatalogProduct,
    matched: str,
    score: float,
    drive_name: str | None,
) -> CatalogResolution:
    return CatalogResolution(
        original=original,
        normalized=normalized,
        status=status,
        canonical_name=product.name,
        query=drive_query_for_product(product, drive_name),
        matched=matched,
        score=score,
        product=product,
        reason=_resolution_reason(status),
    )


def _resolution_reason(status: ResolutionStatus) -> str:
    if status == ResolutionStatus.EXACT_CATALOG:
        return "catalog product"
    if status == ResolutionStatus.EXACT_ALIAS:
        return "catalog alias/synonym"
    return "local fuzzy match"


def _alias_target(name: str, catalog: ProductCatalog) -> str | None:
    if name in catalog.aliases:
        return catalog.aliases[name]
    for product in catalog.products:
        if name in product.aliases or name in product.synonyms or name == product.query:
            return product.name
    for canonical, synonyms in catalog.synonyms.items():
        if name in synonyms:
            return canonical
    return None


def _fuzzy_match(
    name: str, catalog: ProductCatalog, cutoff: float
) -> tuple[str, str, float] | None:
    candidates: dict[str, str] = {}
    for product in catalog.products:
        candidates[product.name] = product.name
        if product.query:
            candidates[product.query] = product.name
        for alias in (*product.aliases, *product.synonyms):
            candidates[alias] = product.name
    candidates.update(catalog.aliases)
    for canonical, synonyms in catalog.synonyms.items():
        candidates.setdefault(canonical, canonical)
        for synonym in synonyms:
            candidates[synonym] = canonical

    if not candidates:
        return None
    matches = get_close_matches(name, list(candidates), n=1, cutoff=cutoff)
    if not matches:
        return None
    matched = matches[0]
    score = SequenceMatcher(None, name, matched).ratio()
    return matched, candidates[matched], score


def catalog_payload(catalog: ProductCatalog) -> dict[str, Any]:
    return catalog.model_dump(mode="json", exclude_none=True)
