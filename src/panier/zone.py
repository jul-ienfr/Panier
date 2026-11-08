"""Découverte des drives autour d'une zone géographique (code postal / ville).

Deux sources publiques, sans clé ni compte :

- geo.api.gouv.fr pour transformer « 83440 » ou « Montauroux » en coordonnées ;
- Overpass (OpenStreetMap) pour lister les magasins dans un rayon donné.

La sélection reste locale : elle vit dans ``HouseholdProfile.stores``
(``{leclerc: true, carrefour: false}``) et c'est déjà ce que consomment
``prepare_items_and_offers`` et ``collect``. Découvrir ne décide donc rien ;
le foyer décide.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from panier.drive import canonical_drive_name
from panier.household import HouseholdProfile
from panier.models import fold_name, normalize_name

GEO_API_ENV_VAR = "PANIER_GEO_API_URL"
OVERPASS_ENV_VAR = "PANIER_OVERPASS_URL"
DEFAULT_GEO_API_URL = "https://geo.api.gouv.fr"
# Miroirs publics Overpass : le premier répond souvent en 25-40 s sur un rayon
# large, d'où le basculement automatique plutôt qu'un échec sec.
DEFAULT_OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
DEFAULT_OVERPASS_URL = DEFAULT_OVERPASS_MIRRORS[0]
DEFAULT_RADIUS_KM = 20.0
MAX_RADIUS_KM = 100.0
DEFAULT_TIMEOUT_S = 30.0
OVERPASS_TIMEOUT_S = 60.0
USER_AGENT = "panier-cli/0.1 (drive discovery)"
ZONE_CACHE_SUBDIR = Path("cache") / "zone"
ZONE_CACHE_TTL_HOURS = 168.0
ZONE_CACHE_TTL_ENV_VAR = "PANIER_ZONE_CACHE_TTL_HOURS"

COLLECTABLE_STORES = ("auchan", "carrefour", "intermarche", "leclerc", "superu")

_BRAND_ALIASES: dict[str, tuple[str, ...]] = {
    "auchan": ("auchan", "auchan drive", "auchan supermarche"),
    "carrefour": (
        "carrefour",
        "carrefour market",
        "carrefour contact",
        "carrefour city",
        "carrefour drive",
    ),
    "intermarche": ("intermarche", "intermarche super", "intermarche contact"),
    "leclerc": ("leclerc", "e leclerc", "e.leclerc", "e-leclerc", "leclerc drive"),
    "superu": (
        "super u",
        "super-u",
        "hyper u",
        "hyper-u",
        "u express",
        "courses u",
        "coursesu",
    ),
    "casino": ("casino", "casino supermarket", "geant casino", "geant"),
    "cora": ("cora",),
    "lidl": ("lidl",),
    "aldi": ("aldi",),
    "netto": ("netto",),
    "picard": ("picard", "picard surgeles"),
    "grand frais": ("grand frais",),
}


class ZoneError(ValueError):
    """Zone introuvable ou source géographique indisponible."""


@dataclass(frozen=True)
class GeoPoint:
    label: str
    latitude: float
    longitude: float
    city: str | None = None
    postal_code: str | None = None
    insee: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "city": self.city,
            "postal_code": self.postal_code,
            "insee": self.insee,
        }


@dataclass(frozen=True)
class DriveCandidate:
    name: str
    store: str | None
    address: str | None
    city: str | None
    postal_code: str | None
    latitude: float
    longitude: float
    distance_km: float
    collectable: bool
    selected: bool = False
    enabled: bool | None = None
    suggested: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "store": self.store,
            "address": self.address,
            "city": self.city,
            "postal_code": self.postal_code,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "distance_km": round(self.distance_km, 1),
            "collectable": self.collectable,
            "selected": self.selected,
            "enabled": self.enabled,
            "suggested": self.suggested,
        }


@dataclass(frozen=True)
class DriveZone:
    location: GeoPoint
    radius_km: float
    candidates: list[DriveCandidate] = field(default_factory=list)
    enabled_stores: tuple[str, ...] = ()
    disabled_stores: tuple[str, ...] = ()
    suggested_stores: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "location": self.location.as_dict(),
            "radius_km": self.radius_km,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "enabled_stores": list(self.enabled_stores),
            "disabled_stores": list(self.disabled_stores),
            "suggested_stores": list(self.suggested_stores),
            "selected_stores": sorted(
                {
                    candidate.store
                    for candidate in self.candidates
                    if candidate.selected and candidate.store
                }
            ),
        }


def _endpoint(environ: dict[str, str] | None, env_var: str, default: str) -> str:
    values = os.environ if environ is None else environ
    return (values.get(env_var, "").strip() or default).rstrip("/")


def _get_json(
    url: str,
    *,
    environ: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Any:
    request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except OSError as exc:
        raise ZoneError(f"source géographique injoignable ({url}) : {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ZoneError(f"réponse géographique illisible ({url})") from exc


def _looks_like_postal_code(query: str) -> bool:
    return len(query) == 5 and query.isdigit()


def _population(record: dict[str, Any]) -> int:
    value = record.get("population")
    return value if isinstance(value, int) else 0


def _best_named_commune(records: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Retient la commune la plus peuplée parmi les homonymes d'un nom saisi.

    « Saint-Raphaël » existe en 24160 (94 hab.) et en 83700 (37 113 hab.) :
    prendre la première réponse de l'API plaçait la recherche en Dordogne.
    Le nom exact est prioritaire, la population tranche les égalités.
    """
    wanted = fold_name(query)
    exact = [r for r in records if fold_name(str(r.get("nom") or "")) == wanted]
    candidates = exact or records
    return [max(candidates, key=_population)]


def _cache_ttl_hours(environ: dict[str, str] | None) -> float:
    """TTL du cache zone : variable d'environnement au-dessus du défaut."""
    values = os.environ if environ is None else environ
    raw = values.get(ZONE_CACHE_TTL_ENV_VAR, "").strip()
    if raw:
        try:
            parsed = float(raw.replace(",", "."))
        except ValueError:
            return ZONE_CACHE_TTL_HOURS
        if parsed > 0:
            return parsed
    return ZONE_CACHE_TTL_HOURS


def _cache_path(cache_dir: Path | None, key: str) -> Path | None:
    if cache_dir is None:
        return None
    return cache_dir / f"{key}.json"


def _read_cache(path: Path | None, *, ttl_hours: float) -> tuple[dict[str, Any] | None, bool]:
    """Charge une entrée : ``(données, fraîches)``.

    Une entrée absente, illisible ou corrompue vaut ``(None, False)`` : le cache
    ne peut jamais être la cause d'un échec, il ne sert qu'à accélérer.
    """
    if path is None:
        return None, False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        collected_at = payload["collected_at"]
        data = payload["data"]
    except (OSError, ValueError, KeyError, TypeError):
        return None, False
    if not isinstance(data, dict) or not isinstance(collected_at, (int, float)):
        return None, False
    age_hours = max(0.0, (time.time() - float(collected_at)) / 3600.0)
    return data, age_hours <= ttl_hours


def _write_cache(path: Path | None, data: dict[str, Any]) -> None:
    """Persiste une entrée en best-effort : un disque en erreur ne casse rien."""
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f"{path.name}.{os.getpid()}.tmp"
        tmp.write_text(
            json.dumps({"collected_at": time.time(), "data": data}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError:
        return


def _candidates_from_cache(data: dict[str, Any]) -> list[DriveCandidate] | None:
    items = data.get("candidates")
    if not isinstance(items, list):
        return None
    try:
        return [DriveCandidate(**item) for item in items if isinstance(item, dict)]
    except TypeError:
        return None


def geocode(
    query: str,
    *,
    environ: dict[str, str] | None = None,
    cache_dir: Path | None = None,
) -> GeoPoint:
    """Transforme un code postal ou un nom de ville en point géographique.

    Un code postal couvre souvent plusieurs communes (83440 → Callian, Fayence,
    Montauroux…). Dans ce cas le point retenu est le barycentre du groupe et le
    libellé reste le code postal : nommer « Callian » une recherche 83440
    suggérerait une précision qui n'existe pas.

    Un nom de ville peut désigner plusieurs communes homonymes : la plus
    peuplée est retenue, pour ne pas géolocaliser « Saint-Raphaël » en Dordogne.
    """
    cleaned = " ".join(query.split())
    if not cleaned:
        raise ZoneError("zone vide (code postal ou ville attendu)")
    path = _cache_path(cache_dir, f"geo-{hashlib.sha1(cleaned.encode('utf-8')).hexdigest()[:16]}")
    data, fresh = _read_cache(path, ttl_hours=_cache_ttl_hours(environ))
    if data is not None and fresh:
        try:
            return GeoPoint(**data)
        except TypeError:
            pass
    point = _geocode_remote(cleaned, environ=environ)
    _write_cache(path, point.as_dict())
    return point


def _geocode_remote(cleaned: str, *, environ: dict[str, str] | None = None) -> GeoPoint:
    base = _endpoint(environ, GEO_API_ENV_VAR, DEFAULT_GEO_API_URL)
    fields = "nom,centre,code,codesPostaux,population"
    if _looks_like_postal_code(cleaned):
        url = f"{base}/communes?codePostal={urllib.parse.quote(cleaned)}&fields={fields}"
    else:
        url = f"{base}/communes?nom={urllib.parse.quote(cleaned)}&fields={fields}&limit=10"
    payload = _get_json(url, environ=environ)
    if not isinstance(payload, list) or not payload:
        raise ZoneError(f"zone introuvable : {cleaned}")
    records = [
        record
        for record in payload
        if isinstance(record, dict)
        and isinstance(record.get("centre"), dict)
        and isinstance(record["centre"].get("coordinates"), list)
        and len(record["centre"]["coordinates"]) == 2
    ]
    if not records:
        raise ZoneError(f"zone sans coordonnées exploitables : {cleaned}")
    if not _looks_like_postal_code(cleaned):
        records = _best_named_commune(records, cleaned)
    points: list[tuple[float, float, str, dict[str, Any]]] = []
    for record in records:
        coordinates = record["centre"]["coordinates"]
        latitude = float(coordinates[1])
        longitude = float(coordinates[0])
        city = str(record.get("nom") or "")
        points.append((latitude, longitude, city, record))
    latitude = sum(point[0] for point in points) / len(points)
    longitude = sum(point[1] for point in points) / len(points)
    first = points[0][3]
    postal_codes = first.get("codesPostaux")
    postal = postal_codes[0] if isinstance(postal_codes, list) and postal_codes else None
    if postal is None and _looks_like_postal_code(cleaned):
        postal = cleaned
    resolved_city: str | None
    if len(points) == 1:
        label = f"{points[0][2]} ({postal})" if postal else points[0][2]
        resolved_city = points[0][2]
    else:
        label = f"{postal} — {len(points)} communes"
        resolved_city = None
    return GeoPoint(
        label=label,
        latitude=latitude,
        longitude=longitude,
        city=resolved_city,
        postal_code=str(postal) if postal else None,
        insee=str(first.get("code")) if first.get("code") else None,
    )


def haversine_km(lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> float:
    radius = 6371.0
    phi_a, phi_b = math.radians(lat_a), math.radians(lat_b)
    delta_phi = math.radians(lat_b - lat_a)
    delta_lambda = math.radians(lon_b - lon_a)
    value = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi_a) * math.cos(phi_b) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(value))


def _fold_brand(value: str) -> str:
    folded = fold_name(value).replace(".", " ").replace("-", " ")
    return " ".join(folded.split())


def _match_brand_alias(value: str) -> str | None:
    """Enseigne canonique reconnue depuis un libellé libre, sinon None."""
    folded = _fold_brand(value)
    for store, aliases in _BRAND_ALIASES.items():
        for alias in aliases:
            if folded == alias or folded.startswith(alias + " ") or f" {alias} " in f" {folded} ":
                return canonical_drive_name(store)
    return None


def canonical_store(name: str) -> str:
    """Normalise une enseigne saisie (« Carrefour Market » → carrefour)."""
    cleaned = " ".join(name.split())
    if not cleaned:
        raise ZoneError("enseigne vide")
    matched = _match_brand_alias(cleaned)
    if matched is not None:
        return matched
    return canonical_drive_name(cleaned)


def _brand_store(tags: dict[str, Any]) -> tuple[str | None, str]:
    """Enseigne canonique reconnue depuis les tags OSM (« Intermarché » → intermarche)."""
    raw = str(tags.get("brand") or tags.get("name") or tags.get("operator") or "").strip()
    return _match_brand_alias(raw), raw


_NON_DRIVE_HINTS = ("espace culturel", "librairie", "station service", "pharmacie", "opticien")


def _address(tags: dict[str, Any]) -> str | None:
    number = str(tags.get("addr:housenumber") or "").strip()
    street = str(tags.get("addr:street") or "").strip()
    joined = " ".join(part for part in (number, street) if part)
    return joined or None


def _overpass_query(latitude: float, longitude: float, radius_km: float) -> str:
    radius_m = int(round(radius_km * 1000))
    # Deux contraintes apprises à la dure sur l'API publique :
    # - regex ANCRÉE : « supermarket|hypermarket|… » non ancré se solde par un
    #   502/504 côté serveur (évaluation du motif sur tous les tags) ;
    # - pas de department_store/wholesale : ces valeurs multiplient le volume
    #   sans apporter de drive, et font dépasser le délai du serveur.
    shops = "^(supermarket|hypermarket)$"
    return (
        "[out:json][timeout:25];"
        "("
        f'node(around:{radius_m},{latitude},{longitude})[shop~"{shops}"];'
        f'way(around:{radius_m},{latitude},{longitude})[shop~"{shops}"];'
        ");"
        "out tags center 200;"
    )


def _overpass_mirrors(environ: dict[str, str] | None) -> tuple[str, ...]:
    """Miroirs à essayer : URL imposée par l'env, sinon les miroirs publics."""
    values = os.environ if environ is None else environ
    override = values.get(OVERPASS_ENV_VAR, "").strip()
    if override:
        return (override.rstrip("/"),)
    return DEFAULT_OVERPASS_MIRRORS


def discover_drives(
    location: GeoPoint,
    *,
    radius_km: float = DEFAULT_RADIUS_KM,
    environ: dict[str, str] | None = None,
    cache_dir: Path | None = None,
) -> list[DriveCandidate]:
    """Liste les magasins autour d'une zone, triés par distance.

    La découverte est la seule étape lente (Overpass : 25-120 s, 502
    fréquents). Elle est donc cacheable : une zone déjà explorée est relue
    sans appel réseau, et un échec OpenStreetMap retombe sur la dernière
    découverte connue. La sélection du foyer n'est pas dans ce cache — elle
    est réappliquée à chaque appel par ``drive_zone``, pour qu'une bascule
    soit visible immédiatement.
    """
    if radius_km <= 0 or radius_km > MAX_RADIUS_KM:
        raise ZoneError(f"rayon invalide : {radius_km} km (attendu 0 < rayon <= {MAX_RADIUS_KM})")
    path = _cache_path(
        cache_dir,
        f"osm-{location.latitude:.3f}-{location.longitude:.3f}-{radius_km:g}",
    )
    ttl_hours = _cache_ttl_hours(environ)
    data, fresh = _read_cache(path, ttl_hours=ttl_hours)
    if data is not None and fresh:
        cached = _candidates_from_cache(data)
        if cached is not None:
            return cached
    try:
        candidates = _discover_drives_remote(location, radius_km=radius_km, environ=environ)
    except ZoneError:
        stale = _candidates_from_cache(data) if data is not None else None
        if stale is not None:
            return stale
        raise
    _write_cache(path, {"candidates": [asdict(candidate) for candidate in candidates]})
    return candidates


def _discover_drives_remote(
    location: GeoPoint,
    *,
    radius_km: float = DEFAULT_RADIUS_KM,
    environ: dict[str, str] | None = None,
) -> list[DriveCandidate]:
    query = _overpass_query(location.latitude, location.longitude, radius_km)
    body = urllib.parse.urlencode({"data": query}).encode("utf-8")
    payload: Any = None
    errors: list[str] = []
    for mirror in _overpass_mirrors(environ):
        try:
            payload = _get_json(mirror, environ=environ, data=body, timeout=OVERPASS_TIMEOUT_S)
            break
        except ZoneError as exc:
            errors.append(str(exc))
    if payload is None:
        raise ZoneError("aucun miroir OpenStreetMap disponible : " + " | ".join(errors))
    elements = payload.get("elements") if isinstance(payload, dict) else None
    if not isinstance(elements, list):
        raise ZoneError("réponse OpenStreetMap inattendue")
    candidates: list[DriveCandidate] = []
    seen: set[tuple[str, int, int]] = set()
    for element in elements:
        if not isinstance(element, dict):
            continue
        tags = element.get("tags") or {}
        if not isinstance(tags, dict):
            continue
        centre: dict[str, Any] = (
            element["center"] if isinstance(element.get("center"), dict) else element
        )
        try:
            latitude = float(str(centre.get("lat")))
            longitude = float(str(centre.get("lon")))
        except (TypeError, ValueError):
            continue
        store, label = _brand_store(tags)
        name = str(tags.get("name") or label or "Magasin").strip()
        folded_name = fold_name(name)
        # « E.Leclerc espace culturel » partage l'enseigne mais n'est pas un
        # drive : le marquer collectable ferait échouer la collecte panier.
        is_drive = store is not None and not any(hint in folded_name for hint in _NON_DRIVE_HINTS)
        key = (normalize_name(name), int(round(latitude * 1000)), int(round(longitude * 1000)))
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            DriveCandidate(
                name=name,
                store=store,
                address=_address(tags),
                city=str(tags.get("addr:city") or "").strip() or None,
                postal_code=str(tags.get("addr:postcode") or "").strip() or None,
                latitude=latitude,
                longitude=longitude,
                distance_km=haversine_km(
                    location.latitude, location.longitude, latitude, longitude
                ),
                collectable=is_drive and store in COLLECTABLE_STORES,
            )
        )
    candidates.sort(key=lambda candidate: candidate.distance_km)
    return candidates


def selection_state(household: HouseholdProfile | None) -> tuple[set[str], set[str]]:
    if household is None:
        return set(), set()
    return household.enabled_stores(), household.disabled_stores()


def apply_selection(
    candidates: list[DriveCandidate],
    household: HouseholdProfile | None,
) -> list[DriveCandidate]:
    """Croise les candidats découverts avec la sélection persistée du foyer.

    ``selected`` reste False pour une enseigne absente du foyer : découvrir ne
    vaut pas activer, sinon la liste OSM déciderait à la place de l'utilisateur.
    """
    enabled, disabled = selection_state(household)
    result: list[DriveCandidate] = []
    for candidate in candidates:
        store = candidate.store
        if store in enabled:
            result.append(replace(candidate, selected=True, enabled=True))
        elif store in disabled:
            result.append(replace(candidate, selected=False, enabled=False))
        else:
            result.append(replace(candidate, selected=False, enabled=None))
    return result


def drive_zone(
    query: str,
    *,
    radius_km: float = DEFAULT_RADIUS_KM,
    household: HouseholdProfile | None = None,
    environ: dict[str, str] | None = None,
    cache_dir: Path | None = None,
) -> DriveZone:
    location = geocode(query, environ=environ, cache_dir=cache_dir)
    enabled, disabled = selection_state(household)
    discovered = discover_drives(
        location, radius_km=radius_km, environ=environ, cache_dir=cache_dir
    )
    suggested = default_selection(discovered, disabled=disabled)
    candidates = [
        replace(candidate, suggested=candidate.store in suggested)
        for candidate in apply_selection(discovered, household)
    ]
    return DriveZone(
        location=location,
        radius_km=radius_km,
        candidates=candidates,
        enabled_stores=tuple(sorted(enabled)),
        disabled_stores=tuple(sorted(disabled)),
        suggested_stores=tuple(sorted(suggested)),
    )


def toggle_store(household: HouseholdProfile, store: str, *, enabled: bool) -> HouseholdProfile:
    """Active ou désactive une enseigne dans le foyer (sélection persistée)."""
    key = canonical_store(store)
    stores = dict(household.stores)
    stores[key] = enabled
    return household.model_copy(update={"stores": stores})


def default_selection(
    candidates: list[DriveCandidate],
    *,
    keep: int = 2,
    disabled: set[str] | None = None,
) -> dict[str, bool]:
    """Présélection hybride : les enseignes collectables distinctes les plus proches.

    Un magasin explicitement désactivé par le foyer n'est jamais reproposé :
    la suggestion automatique ne doit pas annuler un choix manuel.
    """
    excluded = disabled or set()
    selection: dict[str, bool] = {}
    for candidate in candidates:
        store = candidate.store
        if not store or not candidate.collectable or store in excluded:
            continue
        if store in selection:
            continue
        selection[store] = True
        if len(selection) >= keep:
            break
    return selection
