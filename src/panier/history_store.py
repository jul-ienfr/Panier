"""Historique append-only des offres collectées, en SQLite stdlib.

Fichier : <data_dir>/history.sqlite3. Chaque collecte fraîche réussie
(même partielle) ajoute ses lignes ; rien n'est jamais mis à jour ni
supprimé. Les lectures (trend, promos) sont purement informatives et ne
participent jamais à la décision déterministe sans flag dédié.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from panier.models import StoreOffer, normalize_name

HISTORY_DB_FILENAME = "history.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at TEXT NOT NULL,
    store TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    product_title TEXT NOT NULL,
    brand TEXT,
    quantity REAL,
    unit TEXT,
    price REAL NOT NULL,
    unit_price REAL,
    promo INTEGER NOT NULL DEFAULT 0,
    url TEXT
);
CREATE INDEX IF NOT EXISTS idx_offers_item_store_time
    ON offers (canonical_name, store, collected_at);
"""

_QUANTITY_UNIT_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(kg|g|l|cl|ml)\b",
    re.IGNORECASE,
)


def history_db_path(data_dir: Path) -> Path:
    return data_dir / HISTORY_DB_FILENAME


def connect(data_dir: Path) -> sqlite3.Connection:
    path = history_db_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    return conn


@dataclass(frozen=True)
class HistoryPoint:
    id: int
    collected_at: str
    store: str
    canonical_name: str
    product_title: str
    brand: str | None
    quantity: float | None
    unit: str | None
    price: float
    unit_price: float | None
    promo: bool
    url: str | None


@dataclass(frozen=True)
class PriceTrend:
    canonical_name: str
    store: str
    observations: int
    min_price: float
    mean_price: float
    median_price: float
    last_price: float
    last_collected_at: str
    delta_pct: float | None
    is_promo_candidate: bool


def detect_brand(product_title: str, known_brands: set[str] | frozenset[str]) -> str | None:
    """Marque détectée déterministement si un nom connu apparaît dans le titre."""
    product = normalize_name(product_title)
    matches = [brand for brand in known_brands if brand and brand in product]
    return sorted(matches)[0] if matches else None


def parse_quantity_unit(product_title: str) -> tuple[float | None, str | None]:
    """Quantité/unité du conditionnement détectées dans le titre produit."""
    match = _QUANTITY_UNIT_RE.search(normalize_name(product_title))
    if match is None:
        return None, None
    value = float(match.group(1).replace(",", "."))
    unit = normalize_name(match.group(2))
    if unit == "kg":
        value *= 1000
        unit = "g"
    elif unit == "l":
        value *= 1000
        unit = "ml"
    elif unit == "cl":
        value *= 10
        unit = "ml"
    return value, unit


def record_offers(
    data_dir: Path,
    store: str,
    offers: list[StoreOffer],
    *,
    collected_at: datetime | None = None,
    known_brands: set[str] | frozenset[str] = frozenset(),
) -> int:
    """Ajoute les offres au fil d'historique. Retourne le nombre de lignes écrites."""
    stamp = (collected_at or datetime.now(UTC)).isoformat(timespec="seconds")
    conn = connect(data_dir)
    try:
        with conn:
            conn.executemany(
                """
                INSERT INTO offers (
                    collected_at, store, canonical_name, product_title, brand,
                    quantity, unit, price, unit_price, promo, url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        stamp,
                        normalize_name(store),
                        offer.item,
                        offer.product,
                        detect_brand(offer.product, known_brands),
                        *parse_quantity_unit(offer.product),
                        float(offer.price),
                        float(offer.unit_price) if offer.unit_price is not None else None,
                        0,
                        offer.url,
                    )
                    for offer in offers
                ],
            )
    finally:
        conn.close()
    return len(offers)


def offers_for_item(
    data_dir: Path,
    canonical_name: str,
    *,
    store: str | None = None,
    since: datetime | None = None,
) -> list[HistoryPoint]:
    conn = connect(data_dir)
    try:
        clauses = ["canonical_name = ?"]
        params: list[object] = [normalize_name(canonical_name)]
        if store is not None:
            clauses.append("store = ?")
            params.append(normalize_name(store))
        if since is not None:
            clauses.append("collected_at >= ?")
            params.append(since.isoformat(timespec="seconds"))
        rows = conn.execute(
            f"""
            SELECT id, collected_at, store, canonical_name, product_title, brand,
                   quantity, unit, price, unit_price, promo, url
            FROM offers
            WHERE {" AND ".join(clauses)}
            ORDER BY collected_at ASC, id ASC
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    return [_point_from_row(row) for row in rows]


def _point_from_row(row: tuple) -> HistoryPoint:
    return HistoryPoint(
        id=int(row[0]),
        collected_at=str(row[1]),
        store=str(row[2]),
        canonical_name=str(row[3]),
        product_title=str(row[4]),
        brand=row[5],
        quantity=row[6],
        unit=row[7],
        price=float(row[8]),
        unit_price=float(row[9]) if row[9] is not None else None,
        promo=bool(row[10]),
        url=row[11],
    )


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def trend_for_item(
    data_dir: Path,
    canonical_name: str,
    *,
    store: str | None = None,
    since: datetime | None = None,
) -> PriceTrend | None:
    points = offers_for_item(data_dir, canonical_name, store=store, since=since)
    if not points:
        return None
    prices = [point.price for point in points]
    last = points[-1]
    mean = sum(prices) / len(prices)
    median = _median(prices)
    delta_pct = ((last.price - mean) / mean * 100) if mean else None
    return PriceTrend(
        canonical_name=last.canonical_name,
        store=last.store,
        observations=len(prices),
        min_price=min(prices),
        mean_price=mean,
        median_price=median,
        last_price=last.price,
        last_collected_at=last.collected_at,
        delta_pct=delta_pct,
        is_promo_candidate=last.price < median,
    )


def promo_candidates(
    data_dir: Path,
    *,
    since: datetime | None = None,
) -> list[PriceTrend]:
    """Prix courant (dernier point par item+store) sous la médiane historique."""
    conn = connect(data_dir)
    try:
        clauses = ["1 = 1"]
        params: list[object] = []
        if since is not None:
            clauses.append("collected_at >= ?")
            params.append(since.isoformat(timespec="seconds"))
        groups = conn.execute(
            f"""
            SELECT canonical_name, store FROM offers
            WHERE {" AND ".join(clauses)}
            GROUP BY canonical_name, store
            ORDER BY canonical_name ASC, store ASC
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    candidates: list[PriceTrend] = []
    for canonical_name, store in groups:
        trend = trend_for_item(data_dir, str(canonical_name), store=str(store), since=since)
        if trend is not None and trend.is_promo_candidate:
            candidates.append(trend)
    return candidates


def parse_since(value: str | None, *, now: datetime | None = None) -> datetime | None:
    """Fenêtre `--since` : '<N>d', '<N>h' ou date ISO. None = tout l'historique."""
    if value is None or not value.strip():
        return None
    text = value.strip().lower()
    reference = now or datetime.now(UTC)
    match = re.fullmatch(r"(\d+)\s*(d|h|j)", text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        if unit == "h":
            return reference - timedelta(hours=amount)
        return reference - timedelta(days=amount)
    if text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Fenêtre --since invalide : {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def known_brand_names(data_dir: Path) -> frozenset[str]:
    """Noms de marques connues localement (préférences utilisateur)."""
    from panier.brands import load_brand_preferences

    preferences = load_brand_preferences(data_dir)
    return frozenset(preferences.prefer | preferences.avoid | preferences.block)


def price_history_payload(
    data_dir: Path,
    canonical_names: list[str],
    *,
    now: datetime | None = None,
) -> dict[str, dict]:
    """Bloc informatif price_history pour une liste de noms canoniques.

    Les statistiques sont calculées sur le store du dernier point connu de
    l'ingrédient : comparer un prix au median d'un autre drive serait trompeur.
    """
    block: dict[str, dict] = {}
    for name in sorted(set(canonical_names)):
        points = offers_for_item(data_dir, name)
        if not points:
            continue
        # Dernier point connu ; à collecte simultanée (horodatage à la seconde),
        # l'id d'insertion tranche de façon déterministe (ordre d'agrégation).
        last = max(points, key=lambda point: (point.collected_at, point.id))
        trend = trend_for_item(data_dir, name, store=last.store)
        if trend is None:
            continue
        block[name] = {
            "last_price": trend.last_price,
            "delta_pct": round(trend.delta_pct, 2) if trend.delta_pct is not None else None,
            "is_promo_candidate": trend.is_promo_candidate,
            "observations": trend.observations,
            "store": trend.store,
        }
    return block
