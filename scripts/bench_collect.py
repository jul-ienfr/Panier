"""Bench de la collecte multi-drives : séquentiel (avant Phase 2) vs parallèle.

Simule la latence réseau/anti-bot de chaque store par un sleep injecté dans
collect_drive_offers, puis mesure le temps mur du même cycle en séquentiel
(boucle historique) et via collect_offers_parallel (un worker par store).

Usage : python scripts/bench_collect.py [latence_par_store_en_secondes]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from panier.collector import collect_offers_parallel, dedupe_drives
from panier.models import ShoppingItem, StoreOffer

DRIVES = ["intermarche", "leclerc", "auchan"]
ITEMS = [ShoppingItem(name="riz", quantity=1, unit="kg")]


def fake_collect(items: list[ShoppingItem], drive: str, browser: object, max_results: int = 5):
    time.sleep(LATENCY)
    return [
        StoreOffer(store=drive, item="riz", product=f"Riz {drive}", price=2.0)
    ]


def sequential_cycle() -> float:
    start = time.perf_counter()
    for drive in DRIVES:
        try:
            fake_collect(ITEMS, drive, None)
        except Exception:
            continue
    return time.perf_counter() - start


def parallel_cycle() -> float:
    start = time.perf_counter()
    worker = lambda drive: fake_collect(ITEMS, drive, None)  # noqa: E731
    collect_offers_parallel(DRIVES, worker)
    return time.perf_counter() - start


if __name__ == "__main__":
    LATENCY = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0
    seq = sequential_cycle()
    par = parallel_cycle()
    print(f"Latence simulée par store : {LATENCY:g}s ; stores : {dedupe_drives(DRIVES)}")
    print(f"Séquentiel (avant) : {seq:.2f}s")
    print(f"Parallèle  (après) : {par:.2f}s")
    print(f"Gain mesuré        : x{seq / par:.2f}")
