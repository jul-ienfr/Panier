"""Vérification mypy progressive contre mypy-baseline.txt.

Les erreurs déjà présentes dans la baseline sont tolérées ; toute erreur
nouvelle échoue. Pour résorber la dette : corriger un fichier, retirer ses
lignes de la baseline (ou régénérer avec --update) et relire la diff.

Usage :
    python scripts/check_mypy.py            # compare au baseline
    python scripts/check_mypy.py --update   # régénère le baseline
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "mypy-baseline.txt"


def current_errors() -> list[str]:
    completed = subprocess.run(
        [sys.executable, "-m", "mypy", "src"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return [
        line.strip()
        for line in completed.stdout.splitlines()
        if ": error:" in line
    ]


def main(argv: list[str]) -> int:
    errors = current_errors()
    if "--update" in argv:
        BASELINE.write_text(
            "\n".join(sorted(errors)) + ("\n" if errors else ""),
            encoding="utf-8",
        )
        print(f"Baseline mise à jour: {len(errors)} erreurs tolérées.")
        return 0

    baseline: set[str] = set()
    if BASELINE.exists():
        baseline = {
            line.strip()
            for line in BASELINE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    new_errors = sorted(set(errors) - baseline)
    resolved = sorted(baseline - set(errors))

    for line in resolved:
        print(f"Résorbé (retirer du baseline): {line}")
    if new_errors:
        print(f"{len(new_errors)} nouvelle(s) erreur(s) mypy absente(s) du baseline :")
        for line in new_errors:
            print(f"  {line}")
        print("Corrige ces erreurs, ou étends volontairement mypy-baseline.txt")
        print("(python scripts/check_mypy.py --update) après relecture.")
        return 1
    print(
        f"mypy OK ({len(baseline)} erreur(s) historique(s) tolérée(s), "
        f"{len(errors)} actuellement)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
