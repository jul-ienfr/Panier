"""Collecte multi-drives : parallélisation ENTRE stores uniquement.

Invariants :
- un worker maximum par store (les doublons de drives sont dédupliqués) ;
- chaque worker construit son propre navigateur/profil (isolation par store,
  aucune contention de session) ;
- l'échec ou le timeout d'un store n'affecte jamais les autres ;
- le timeout porte sur le cycle de collecte complet, pas sur chaque futur.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Literal

from panier.models import normalize_name

DEFAULT_COLLECT_TIMEOUT_SECONDS = 300.0
COLLECT_TIMEOUT_ENV_VAR = "PANIER_COLLECT_TIMEOUT_SECONDS"

CollectStatus = Literal["ok", "failed", "timeout"]

StoreWorker = Callable[[str], list]


def resolve_collect_timeout_seconds(
    override: float | None = None, environ: dict[str, str] | None = None
) -> float:
    """Timeout effectif : argument explicite > variable d'environnement > défaut."""
    if override is not None and override > 0:
        return override
    values = os.environ if environ is None else environ
    raw = values.get(COLLECT_TIMEOUT_ENV_VAR)
    if raw:
        try:
            parsed = float(raw.replace(",", "."))
        except ValueError:
            return DEFAULT_COLLECT_TIMEOUT_SECONDS
        if parsed > 0:
            return parsed
    return DEFAULT_COLLECT_TIMEOUT_SECONDS


def dedupe_drives(drives: list[str]) -> list[str]:
    """Déduplique en conservant l'ordre ; garantit un worker unique par store."""
    seen: set[str] = set()
    ordered: list[str] = []
    for drive in drives:
        key = normalize_name(drive)
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(key)
    return ordered


def managed_browser_profile_for_drive(profile: str, drive: str) -> str:
    """Résout le profil Managed Browser adapté au drive.

    Le profil historique `courses` reste valide pour Leclerc, mais chaque autre
    drive est déclaré côté Managed Browser sous son propre profil
    (`courses-auchan`, `courses-carrefour`). Utiliser un profil non déclaré
    déclenche une erreur HTTP 500 de politique de profil.
    """
    normalized_profile = normalize_name(profile)
    normalized_drive = normalize_name(drive)
    if normalized_profile == "courses" and normalized_drive in {"auchan", "carrefour"}:
        return f"courses-{normalized_drive}"
    return profile


def collect_offers_parallel(
    drives: list[str],
    worker: StoreWorker,
    *,
    timeout_seconds: float | None = None,
) -> tuple[list, dict[str, CollectStatus], dict[str, str]]:
    """Exécute un worker par store en parallèle puis agrège dans l'ordre demandé.

    Le worker reçoit le nom du drive normalisé et retourne ses offres ; une
    exception marque le store « failed ». Les stores non terminés à l'échéance
    globale sont marqués « timeout » et annulés s'ils n'ont pas démarré.
    """
    unique_drives = dedupe_drives(drives)
    timeout = resolve_collect_timeout_seconds(timeout_seconds)
    statuses: dict[str, CollectStatus] = {drive: "timeout" for drive in unique_drives}
    errors: dict[str, str] = {}
    offers_by_drive: dict[str, list] = {}
    if not unique_drives:
        return [], statuses, errors

    executor = ThreadPoolExecutor(max_workers=len(unique_drives), thread_name_prefix="panier")
    futures: dict[str, Future] = {
        drive: executor.submit(worker, drive) for drive in unique_drives
    }
    try:
        _, pending = wait(list(futures.values()), timeout=timeout)
        timed_out = {id(future) for future in pending}
        for drive, future in futures.items():
            if id(future) in timed_out:
                errors[drive] = f"cycle de collecte > {timeout:g}s"
                continue
            error = future.exception()
            if error is not None:
                statuses[drive] = "failed"
                errors[drive] = str(error)
                continue
            statuses[drive] = "ok"
            offers_by_drive[drive] = list(future.result() or [])
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    aggregated: list = []
    for drive in unique_drives:
        aggregated.extend(offers_by_drive.get(drive, []))
    return aggregated, statuses, errors
