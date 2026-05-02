from pathlib import Path

from panier.catalog import (
    CatalogBrandType,
    ProductCatalog,
    ResolutionStatus,
    catalog_from_yaml,
    load_catalog,
    resolve_item,
    resolve_items,
)
from panier.drive import build_drive_search_plan, score_offer
from panier.models import ShoppingItem, StoreOffer


def test_load_catalog_normalizes_products_aliases_and_synonyms(tmp_path: Path) -> None:
    (tmp_path / "catalog.yaml").write_text(
        """
products:
  - name: Riz Basmati
    query: riz basmati
    aliases: [riz, "riz long"]
    synonyms: [basmati]
aliases:
  creme: crème fraîche
synonyms:
  pois chiches: [chickpeas]
""",
        encoding="utf-8",
    )

    catalog = load_catalog(tmp_path, include_defaults=False)

    assert catalog.products[0].name == "riz basmati"
    assert catalog.products[0].aliases == ("riz", "riz long")
    assert catalog.aliases == {"creme": "crème fraîche"}
    assert catalog.synonyms == {"pois chiches": ("chickpeas",)}


def test_resolve_item_exact_catalog_and_alias_to_canonical_query() -> None:
    catalog = ProductCatalog.model_validate(
        {
            "products": [
                {
                    "name": "riz basmati",
                    "query": "riz basmati 1kg",
                    "aliases": ["riz"],
                }
            ]
        }
    )

    exact = resolve_item("Riz Basmati", catalog)
    alias = resolve_item(ShoppingItem(name="riz"), catalog)

    assert exact.status == ResolutionStatus.EXACT_CATALOG
    assert exact.canonical_name == "riz basmati"
    assert exact.query == "riz basmati 1kg"
    assert alias.status == ResolutionStatus.EXACT_ALIAS
    assert alias.canonical_name == "riz basmati"
    assert alias.query == "riz basmati 1kg"


def test_resolve_item_uses_global_alias_without_product_entry() -> None:
    catalog = ProductCatalog(aliases={"creme": "crème fraîche"})

    resolved = resolve_item("creme", catalog)

    assert resolved.status == ResolutionStatus.EXACT_ALIAS
    assert resolved.canonical_name == "crème fraîche"
    assert resolved.query == "crème fraîche"


def test_resolve_item_fuzzy_local_and_unresolved() -> None:
    catalog = ProductCatalog.model_validate({"products": [{"name": "tomates concassées"}]})

    fuzzy = resolve_item("tomates concassees", catalog, fuzzy_cutoff=0.7)
    unresolved = resolve_item("papier toilette", catalog, fuzzy_cutoff=0.95)

    assert fuzzy.status == ResolutionStatus.FUZZY_LOCAL
    assert fuzzy.canonical_name == "tomates concassées"
    assert fuzzy.query == "tomates concassées"
    assert unresolved.status == ResolutionStatus.UNRESOLVED
    assert unresolved.query == "papier toilette"


def test_drive_plan_uses_catalog_resolution_and_preserves_fallback() -> None:
    catalog = ProductCatalog.model_validate(
        {
            "products": [
                {
                    "name": "cola",
                    "brand": "marque repère",
                    "brand_type": "store_brand",
                    "store_brand_affinity": "leclerc",
                    "aliases": ["soda cola"],
                }
            ]
        }
    )

    plan = build_drive_search_plan(
        [ShoppingItem(name="soda cola"), ShoppingItem(name="sel")], "leclerc", catalog=catalog
    )

    assert [(entry.query, entry.confidence) for entry in plan] == [
        ("cola marque repère", "high"),
        ("sel", "low"),
    ]


def test_catalog_synonyms_can_be_passed_to_drive_scoring() -> None:
    catalog = ProductCatalog(synonyms={"pois chiches": ("chickpeas",)})
    item = ShoppingItem(name="pois chiches")
    offer = StoreOffer(store="test", item="pois chiches", product="Chickpeas conserve", price=1.0)

    scored_without_catalog = score_offer(item, offer)
    scored_with_catalog = score_offer(item, offer, synonyms=catalog.synonym_mapping())

    assert scored_with_catalog.score > scored_without_catalog.score
    assert "synonyme" in scored_with_catalog.reason


def test_catalog_from_yaml_accepts_top_level_product_list(tmp_path: Path) -> None:
    path = tmp_path / "products.yaml"
    path.write_text(
        """
- name: lait
  brand_type: generic
""",
        encoding="utf-8",
    )

    catalog = catalog_from_yaml(path)
    resolutions = resolve_items([ShoppingItem(name="lait")], catalog)

    assert catalog.products[0].brand_type == CatalogBrandType.GENERIC
    assert resolutions[0].status == ResolutionStatus.EXACT_CATALOG
