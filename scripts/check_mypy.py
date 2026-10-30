"""Vérification mypy progressive contre mypy-baseline.txt.

Les erreurs déjà présentes dans la baseline sont tolérées ; toute erreur
nouvelle échoue. Pour résorber la dette : corriger un fichier, retirer ses
lignes de la baseline (ou régénérer avec --update) et relire la diff.

Usage :
    python scripts/check_mypy.py            # compare au baseline
    python scripts/check_mypy.py --update   # régénère le baseline
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "mypy-baseline.txt"

LINE_RE = re.compile(r"^(?P<file>[^:]+):\d+: (?P<message>error: .*)$")


def normalize(line: str) -> str:
    """Ignore les numéros de ligne: un décalage ne doit pas créer d'erreur « nouvelle »."""
    match = LINE_RE.match(line.strip())
    if match:
        return f"{match.group('file')}: {match.group('message')}"
    return line.strip()


def current_errors() -> list[str]:
    completed = subprocess.run(
        [sys.executable, "-m", "mypy", "src"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return [normalize(line) for line in completed.stdout.splitlines() if ": error:" in line]


def read_baseline() -> list[str]:
    if not BASELINE.exists():
        return []
    return [
        normalize(line)
        for line in BASELINE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main(argv: list[str]) -> int:
    errors = read_baseline() if "--update" not in argv else []
    if "--update" in argv:
        errors = current_errors()
        BASELINE.write_text(
            "\n".join(sorted(errors)) + ("\n" if errors else ""),
            encoding="utf-8",
        )
        print(f"Baseline mise à jour: {len(errors)} erreurs tolérées.")
        return 0

    current = current_errors()
    baseline = read_baseline()
    new_errors = sorted(set(current) - set(baseline))
    resolved = sorted(set(baseline) - set(current))

    for line in resolved:
        print(f"Résorbé (retirer du baseline): {line}")
    if new_errors:
        print(f"{len(new_errors)} nouvelle(s) erreur(s) mypy absente(s) du baseline :")
        for line in new_errors:
            print(f"  {line}")
        print("Corrige ces erreurs, ou étends volontairement mypy-baseline.txt")
        print("(python scripts/check_mypy.py --update) après relecture.")
        return 1
    if resolved:
        print(
            "Des erreurs tolérées ont été résorbées: régénère le baseline "
            "(python scripts/check_mypy.py --update) pour le resserrer."
        )
        return 0
    print(
        f"mypy OK ({len(baseline)} erreur(s) historique(s) tolérée(s), "
        f"{len(current)} actuellement)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
