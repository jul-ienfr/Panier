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
    url TEXT,
    promo_label TEXT
);
CREATE INDEX IF NOT EXISTS idx_offers_item_store_time
    ON offers (canonical_name, store, collected_at);
"""

# Colonnes ajoutées après la première version du schéma : les bases
# existantes doivent être migrées à l'ouverture, sinon les lectures
# échouent sur un `no such column`.
_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("offers", "promo_label TEXT"),
)

_QUANTITY_UNIT_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(kg|g|l|cl|ml)\b",
    re.IGNORECASE,
)
_MULTIPACK_RE = re.compile(
    r"(\d+)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*(kg|g|l|cl|ml)\b",
    re.IGNORECASE,
)

# Labels MDD (marques de distributeur) FR détectés en sous-chaîne du titre
# produit ou du brand normalisés. Scope volontairement restreint aux labels
# stables des 4 drives supportés + Lidl (bloqué hors repo mais présent en histo).
MDD_LABELS = frozenset(
    {
        "marque repere",
        "marque repère",
        "eco+",
        "nos regions ont du talent",
        "nos régions ont du talent",
        "bio village",
        "simpl",
        "carrefour classic",
        "carrefour bio",
        "reflets de france",
        "pouce",
        "auchan bio",
        "top budget",
        "paturages",
        "pâturages",
        "chabrior",
        "monique ranou",
        "paquito",
        "milbona",
        "enzo",
    }
)


def history_db_path(data_dir: Path) -> Path:
    return data_dir / HISTORY_DB_FILENAME


def connect(data_dir: Path) -> sqlite3.Connection:
    path = history_db_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    _apply_migrations(conn)
    return conn


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Ajoute les colonnes manquantes aux bases créées par une version antérieure."""
    for table, column_spec in _MIGRATIONS:
        column = column_spec.split()[0]
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column in existing:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_spec}")
        conn.commit()


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
    promo_label: str | None = None


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
    # Règle median_price : base de la médiane ("per_litre" si ≥1 point
    # volumique normalisable, sinon "raw" = comportement historique).
    price_basis: str = "raw"
    # Médiane sur prix bruts (avant normalisation) ; = median_price si raw.
    median_price_raw: float = 0.0
    # Nombre de points ayant participé à la médiane normalisée.
    normalized_observations: int = 0


def detect_brand(product_title: str, known_brands: set[str] | frozenset[str]) -> str | None:
    """Marque détectée déterministement si un nom connu apparaît dans le titre."""
    product = normalize_name(product_title)
    matches = [brand for brand in known_brands if brand and brand in product]
    return sorted(matches)[0] if matches else None


def is_mdd_offer(point: HistoryPoint) -> bool:
    """Scope MDD : True si le brand ou le titre porte un label MDD connu.

    Non-MDD (marque nationale comme Lactel/Président) = hors scope : exclues
    de la médiane normalisée pour ne pas polluer la comparaison MDD.
    """
    candidates = [normalize_name(point.brand or ""), normalize_name(point.product_title)]
    return any(label and label in text for label in MDD_LABELS for text in candidates)


def price_per_litre(point: HistoryPoint) -> float | None:
    """Prix normalisé au litre (€/L) pour un point volumique (ml), sinon None.

    Priorité : unit_price collecté (déjà un prix/L sur les drives) si le
    conditionnement est volumique ; sinon prix brut / volume en litres déduit
    du titre (parse_quantity_unit → ml). Massiques (g) et inconnus → None.
    """
    if point.unit == "ml" and point.quantity:
        if point.unit_price is not None and point.unit_price > 0:
            return float(point.unit_price)
        litres = point.quantity / 1000.0
        if litres > 0:
            return float(point.price) / litres
    return None


def parse_quantity_unit(product_title: str) -> tuple[float | None, str | None]:
    """Quantité/unité du conditionnement détectées dans le titre produit."""
    title = normalize_name(product_title)
    # Multipack d'abord : "6x1L" = 6000 ml (volume total, pas 1L).
    multi = _MULTIPACK_RE.search(title)
    if multi is not None:
        count = int(multi.group(1))
        value = float(multi.group(2).replace(",", "."))
        unit = normalize_name(multi.group(3))
        if unit == "kg":
            value *= 1000
            unit = "g"
        elif unit == "l":
            value *= 1000
            unit = "ml"
        elif unit == "cl":
            value *= 10
            unit = "ml"
        return count * value, unit
    match = _QUANTITY_UNIT_RE.search(title)
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
                    quantity, unit, price, unit_price, promo, url, promo_label
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        1 if offer.promo else 0,
                        offer.url,
                        offer.promo_label,
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
                   quantity, unit, price, unit_price, promo, url, promo_label
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
        promo_label=row[12],
    )


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


# Baisse minimale, sur un MÊME produit, pour parler de promo. Comparer deux
# produits différents du même ingrédient (« Spaghetti Eco+ 1 kg » à 0,88 € vs
# « Penne Turini 500 g » à 0,55 €) ne mesure pas une promotion mais un
# changement de référence : c'était la source des faux positifs.
MIN_PROMO_DROP_PCT = 10.0


@dataclass(frozen=True)
class PromoSignal:
    """Promo observée : badge affiché par le drive ou baisse sur le même produit."""

    canonical_name: str
    store: str
    product_title: str
    last_price: float
    reference_price: float | None
    delta_pct: float | None
    observations: int
    last_collected_at: str
    promo_label: str | None = None
    # "badge" = le drive annonce l'offre ; "price_drop" = même produit moins cher
    # que sa propre médiane historique.
    reason: str = "price_drop"
    # Les drives réutilisent le même libellé pour deux formats : le prix au kilo
    # est le seul moyen de distinguer « 530 g à 1,45 € » de « 220 g à 0,69 € ».
    unit_price: float | None = None


def _product_key(point: HistoryPoint) -> str:
    """Identité d'un produit : libellé + prix au kilo.

    Les drives réutilisent le même libellé pour plusieurs formats : chez
    Carrefour, « SIMPL Riz Long Etuvé SIMPL » existe à 1,35 € (1,35 €/kg) et à
    0,89 € (1,78 €/kg), et « MUTTI Pulpe de tomate » à 1,09 / 2,55 / 2,95 €.
    Sans le prix au kilo, ces références se mélangent et la moins chère passe
    pour une promo permanente.
    """
    unit_price = point.unit_price
    price_part = "" if unit_price is None else round(unit_price, 3)
    return f"{normalize_name(point.product_title)}|{price_part}"


def _promo_signal(points: list[HistoryPoint]) -> PromoSignal | None:
    """Signal promo pour un groupe de points d'un même produit."""
    if not points:
        return None
    last = max(points, key=lambda point: (point.collected_at, point.id))
    prices = [point.price for point in points]
    reference = _median(prices) if len(prices) > 1 else None
    delta_pct = None
    if reference and reference > 0:
        delta_pct = (last.price - reference) / reference * 100
    badge = last.promo or bool(last.promo_label)
    drop = delta_pct is not None and delta_pct <= -MIN_PROMO_DROP_PCT
    if not badge and not drop:
        return None
    return PromoSignal(
        canonical_name=last.canonical_name,
        store=last.store,
        product_title=last.product_title,
        last_price=last.price,
        reference_price=reference,
        delta_pct=delta_pct,
        observations=len(prices),
        last_collected_at=last.collected_at,
        promo_label=last.promo_label,
        reason="badge" if badge else "price_drop",
        unit_price=last.unit_price,
    )


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
    median_raw = _median(prices)
    # Règle median_price : médiane sur prix normalisés au litre, scope MDD.
    # Seuls les points (volumiques → prix/L calculable) ET MDD participent ;
    # si aucun point ne qualifie → repli "raw" (comportement historique).
    normalized = [
        per_litre
        for point in points
        if is_mdd_offer(point) and (per_litre := price_per_litre(point)) is not None
    ]
    if normalized:
        median = _median(normalized)
        price_basis = "per_litre"
    else:
        median = median_raw
        price_basis = "raw"
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
        price_basis=price_basis,
        median_price_raw=median_raw,
        normalized_observations=len(normalized),
    )


def promo_candidates(
    data_dir: Path,
    *,
    since: datetime | None = None,
) -> list[PromoSignal]:
    """Promos réelles : badge du drive, ou baisse ≥ 10 % sur un MÊME produit.

    Le regroupement se fait par (ingrédient, magasin, libellé, prix au kilo) :
    comparer des références différentes du même ingrédient (« Pâtes Eco+ 1 kg »
    à 0,88 € et « Penne Turini 500 g » à 0,55 €) fabriquait des faux positifs
    permanents, et ignorer le prix au kilo mélangeait les formats d'un même
    libellé.
    """
    conn = connect(data_dir)
    try:
        clauses = ["1 = 1"]
        params: list[object] = []
        if since is not None:
            clauses.append("collected_at >= ?")
            params.append(since.isoformat(timespec="seconds"))
        rows = conn.execute(
            f"""
            SELECT id, collected_at, store, canonical_name, product_title, brand,
                   quantity, unit, price, unit_price, promo, url, promo_label
            FROM offers
            WHERE {" AND ".join(clauses)}
            ORDER BY collected_at ASC, id ASC
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    groups: dict[tuple[str, str, str], list[HistoryPoint]] = {}
    for row in rows:
        point = _point_from_row(row)
        key = (point.canonical_name, point.store, _product_key(point))
        groups.setdefault(key, []).append(point)
    signals: list[PromoSignal] = []
    for points in groups.values():
        signal = _promo_signal(points)
        if signal is not None:
            signals.append(signal)
    signals.sort(key=lambda signal: (signal.canonical_name, signal.store, signal.product_title))
    return signals


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


def history_stats(data_dir: Path, *, since: datetime | None = None) -> dict:
    """Statistiques globales de l'historique (pour le dashboard)."""
    if not history_db_path(data_dir).exists():
        return {"total_points": 0, "distinct_items": 0, "oldest": None, "newest": None}
    conn = connect(data_dir)
    try:
        clause = ""
        params: list[object] = []
        if since is not None:
            clause = "WHERE collected_at >= ?"
            params.append(since.isoformat(timespec="seconds"))
        row = conn.execute(
            f"""
            SELECT COUNT(*), COUNT(DISTINCT canonical_name),
                   MIN(collected_at), MAX(collected_at)
            FROM offers {clause}
            """,
            params,
        ).fetchone()
    finally:
        conn.close()
    return {
        "total_points": int(row[0] or 0),
        "distinct_items": int(row[1] or 0),
        "oldest": row[2],
        "newest": row[3],
    }


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
