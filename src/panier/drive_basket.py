"""Panier comparé sur des drives réels, à partir des prix déjà collectés.

Le parcours drive va de la zone (code postal → magasins) à la comparaison du
panier sur les enseignes réellement retenues. Jusqu'ici la comparaison exigeait
un chemin YAML et un fichier d'offres ; ici les offres viennent de l'historique
local collecté par ``panier drive collect``, restreint aux enseignes actives du
foyer et à leur dernière observation connue.

Aucune écriture, aucun réseau : la fonction lit la base d'historique et rend un
payload prêt pour l'UI (couverture par enseigne, offres, recommandation).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from panier.brands import load_brand_preferences
from panier.history_store import connect, parse_since
from panier.household import load_active_household
from panier.models import PriceMode, ShoppingItem, StoreOffer, fold_name, normalize_name
from panier.planner import CompareBy, compare_basket_options, recommend_basket
from panier.zone import COLLECTABLE_STORES

MAX_ITEMS = 60

_OFF_TOPIC_MARKERS = (
    "bébé",
    "bebe",
    "petit pot",
    "petits pots",
    "blédina",
    "bledina",
    "naturnes",
    "hipp",
    "good gout",
    "soupe",
    "risotto",
    "taboulé",
    "taboule",
    "pané",
    "pane ",
    "gratin",
    "cuisiné",
    "cuisine",
    "plat ",
    "pizza",
    "chips",
    "compote",
    "yaourt",
    "glace",
    "boisson",
    "jus de",
)


class DriveBasketError(ValueError):
    """Liste de courses vide, illisible ou aucune enseigne disponible."""


@dataclass(frozen=True)
class StoreCoverage:
    store: str
    enabled: bool | None
    collectable: bool
    items_covered: int
    items_missing: tuple[str, ...]
    offers_count: int
    last_collected_at: str | None

    def as_dict(self, total_items: int) -> dict[str, Any]:
        return {
            "store": self.store,
            "enabled": self.enabled,
            "collectable": self.collectable,
            "items_covered": self.items_covered,
            "items_total": total_items,
            "items_missing": list(self.items_missing),
            "offers_count": self.offers_count,
            "last_collected_at": self.last_collected_at,
        }


def parse_items(values: list[str] | None, *, extra: str = "") -> list[ShoppingItem]:
    """Liste d'articles depuis des options répétables ou une chaîne séparée."""
    raw: list[str] = []
    for value in values or []:
        raw.extend(part for part in str(value).split(","))
    raw.extend(part for part in extra.split(","))
    items: list[ShoppingItem] = []
    seen: set[str] = set()
    for part in raw:
        name = " ".join(part.split())
        if not name:
            continue
        key = normalize_name(name)
        if key in seen:
            continue
        seen.add(key)
        items.append(ShoppingItem(name=name))
    if not items:
        raise DriveBasketError("aucun article : renseignez au moins un article")
    if len(items) > MAX_ITEMS:
        raise DriveBasketError(f"trop d'articles ({len(items)} > {MAX_ITEMS})")
    return items


def _canonical_index(data_dir: Path) -> dict[str, list[str]]:
    conn = connect(data_dir)
    try:
        rows = conn.execute("SELECT DISTINCT canonical_name FROM offers").fetchall()
    finally:
        conn.close()
    index: dict[str, list[str]] = {}
    for (name,) in rows:
        if not name:
            continue
        index.setdefault(normalize_name(str(name)), []).append(str(name))
        index.setdefault(fold_name(str(name)), []).append(str(name))
    return index


def _resolve_names(query: str, index: dict[str, list[str]]) -> list[str]:
    exact = index.get(normalize_name(query))
    if exact:
        return sorted(set(exact))
    folded = index.get(fold_name(query))
    if folded:
        return sorted(set(folded))
    wanted = set(fold_name(query).split())
    if not wanted:
        return []
    matches: set[str] = set()
    for key, names in index.items():
        tokens = set(key.split())
        if wanted <= tokens or tokens <= wanted:
            matches.update(names)
    return sorted(matches)


def _latest_rows(
    data_dir: Path, canonical_names: list[str], *, since: datetime | None
) -> list[tuple[str, str, str, float, float | None, int, str | None, str | None, str]]:
    if not canonical_names:
        return []
    placeholders = ",".join("?" for _ in canonical_names)
    clauses = [f"canonical_name IN ({placeholders})"]
    params: list[object] = list(canonical_names)
    if since is not None:
        clauses.append("collected_at >= ?")
        params.append(since.isoformat(timespec="seconds"))
    conn = connect(data_dir)
    try:
        return conn.execute(
            f"""
            SELECT store, canonical_name, product_title, price, unit_price,
                   promo, promo_label, url, collected_at
            FROM offers
            WHERE {" AND ".join(clauses)}
            ORDER BY collected_at DESC, id DESC
            """,
            params,
        ).fetchall()
    finally:
        conn.close()


def _is_off_topic(product: str) -> bool:
    lowered = fold_name(product)
    return any(marker in lowered for marker in _OFF_TOPIC_MARKERS)


def _store_state(data_dir: Path) -> tuple[dict[str, bool], set[str]]:
    household = load_active_household(data_dir)
    if household is None:
        return {}, set()
    return dict(household.stores), household.disabled_stores()


def collect_offers(
    data_dir: Path,
    items: list[ShoppingItem],
    *,
    stores: list[str] | None = None,
    since: str | None = "90d",
    include_disabled: bool = False,
) -> tuple[list[StoreOffer], dict[str, list[str]], list[str]]:
    """Dernières offres connues par (article, enseigne, produit).

    Retourne les offres, les articles manquants par enseigne et la liste des
    enseignes effectivement interrogées.
    """
    store_states, disabled = _store_state(data_dir)
    requested = []
    for store in stores or []:
        for part in str(store).split(","):
            cleaned = normalize_name(part)
            if cleaned:
                requested.append(cleaned)
    requested = sorted(dict.fromkeys(requested))
    if requested:
        allowed = {s for s in requested if include_disabled or s not in disabled}
    elif store_states:
        enabled = {store for store, on in store_states.items() if on}
        allowed = enabled - disabled
    else:
        allowed = None
    if allowed is not None and not allowed:
        raise DriveBasketError("aucune enseigne active : activez au moins un drive")

    since_dt = parse_since(since)
    index = _canonical_index(data_dir)
    offers: list[StoreOffer] = []
    seen: set[tuple[str, str, str]] = set()
    covered: dict[str, set[str]] = {}
    seen_stores: set[str] = set()

    for item in items:
        names = _resolve_names(item.name, index)
        rows = _latest_rows(data_dir, names, since=since_dt)
        on_topic = [row for row in rows if not _is_off_topic(str(row[2]))]
        for row in on_topic or rows:
            store = normalize_name(str(row[0]))
            if allowed is not None and store not in allowed:
                continue
            key = (store, str(row[2]), str(row[1]))
            if key in seen:
                continue
            seen.add(key)
            seen_stores.add(store)
            covered.setdefault(store, set()).add(item.name)
            offers.append(
                StoreOffer(
                    store=store,
                    item=item.name,
                    product=str(row[2]),
                    price=float(row[3]),
                    unit_price=float(row[4]) if row[4] is not None else None,
                    promo=bool(row[5]),
                    promo_label=row[6],
                    url=row[7],
                )
            )

    tracked = set(seen_stores)
    if allowed is not None:
        tracked |= allowed
    missing_by_store: dict[str, list[str]] = {
        store: sorted({item.name for item in items} - covered.get(store, set()))
        for store in sorted(tracked)
    }
    return offers, missing_by_store, sorted(tracked)


def coverage(
    data_dir: Path,
    items: list[ShoppingItem],
    missing_by_store: dict[str, list[str]],
    offers: list[StoreOffer],
) -> list[StoreCoverage]:
    store_states, _ = _store_state(data_dir)
    last_seen: dict[str, str] = {}
    counts: dict[str, int] = {}
    conn = connect(data_dir)
    try:
        for store, latest, total in conn.execute(
            "SELECT store, MAX(collected_at), COUNT(*) FROM offers GROUP BY store"
        ):
            key = normalize_name(str(store))
            last_seen[key] = str(latest)
            counts[key] = int(total)
    finally:
        conn.close()
    result: list[StoreCoverage] = []
    for store in sorted({offer.store for offer in offers} | set(missing_by_store)):
        missing = tuple(missing_by_store.get(store, []))
        result.append(
            StoreCoverage(
                store=store,
                enabled=store_states.get(store),
                collectable=store in COLLECTABLE_STORES,
                items_covered=max(0, len(items) - len(missing)),
                items_missing=missing,
                offers_count=counts.get(store, 0),
                last_collected_at=last_seen.get(store),
            )
        )
    return result


def _option_payload(option: Any) -> dict[str, Any]:
    return {
        "stores": list(option.stores),
        "total": round(option.total, 2) if option.total is not None else None,
        "complete": not option.missing_items,
        "missing_items": list(option.missing_items),
        "by_item": [
            {
                "item": item,
                "product": offer.product,
                "store": offer.store,
                "price": offer.price,
                "unit_price": offer.unit_price,
            }
            for item, offer in option.by_item.items()
        ],
    }


def compare_from_history(
    data_dir: Path,
    items: list[ShoppingItem],
    *,
    stores: list[str] | None = None,
    since: str | None = "90d",
    mode: PriceMode = PriceMode.HYBRID,
    max_stores: int = 2,
    compare_by: str = "price",
    include_disabled: bool = False,
) -> dict[str, Any]:
    """Compare une liste sur les prix collectés des drives retenus."""
    offers, missing_by_store, seen_stores = collect_offers(
        data_dir,
        items,
        stores=stores,
        since=since,
        include_disabled=include_disabled,
    )
    if not offers:
        raise DriveBasketError(
            "aucune offre connue pour ces articles : lancez une collecte drive "
            "(panier drive collect) sur les enseignes sélectionnées"
        )
    resolved_compare: CompareBy = (
        "unit_price" if normalize_name(compare_by) == "unit-price" else "price"
    )
    brand_preferences = load_brand_preferences(data_dir)
    covered_items = [item for item in items if any(offer.item == item.name for offer in offers)]
    missing_items = [item.name for item in items if item not in covered_items]

    recommendation: dict[str, Any] | None = None
    error: str | None = None
    try:
        best = recommend_basket(
            covered_items,
            offers,
            mode=mode,
            max_stores=max(1, max_stores),
            compare_by=resolved_compare,
            brand_preferences=brand_preferences,
        )
    except ValueError as exc:
        error = str(exc)
    else:
        recommendation = {
            "stores": list(best.stores),
            "mode": str(best.mode),
            "total": round(best.total, 2),
            "reason": best.reason,
            "savings_vs_best_single": round(best.savings_vs_best_single, 2),
            "by_item": [
                {
                    "item": item,
                    "product": offer.product,
                    "store": offer.store,
                    "price": offer.price,
                    "unit_price": offer.unit_price,
                    "url": offer.url,
                }
                for item, offer in best.by_item.items()
            ],
        }

    options = compare_basket_options(
        covered_items,
        offers,
        max_stores=max(1, max_stores),
        compare_by=resolved_compare,
        brand_preferences=brand_preferences,
    )
    payload = {
        "items": [
            {"name": item.name, "quantity": item.quantity, "unit": item.unit} for item in items
        ],
        "stores": [
            entry.as_dict(len(items))
            for entry in coverage(data_dir, items, missing_by_store, offers)
        ],
        "stores_used": seen_stores,
        "offers": [
            {
                "item": offer.item,
                "product": offer.product,
                "store": offer.store,
                "price": offer.price,
                "unit_price": offer.unit_price,
                "promo": offer.promo,
                "promo_label": offer.promo_label,
                "url": offer.url,
            }
            for offer in sorted(offers, key=lambda offer: (offer.item, offer.price, offer.store))
        ],
        "options": [_option_payload(option) for option in options],
        "recommendation": recommendation,
        "missing_items": missing_items,
        "compare_by": resolved_compare,
        "since": since,
        "error": error,
    }
    return payload
