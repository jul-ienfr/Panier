from __future__ import annotations

import os
from pathlib import Path

import yaml
from typer.testing import CliRunner

from panier.cli import app

GOLDEN_DIR = Path(__file__).parent / "golden"
UPDATE_GOLDEN_ENV_VAR = "PANIER_UPDATE_GOLDEN"


def _update_golden_requested() -> bool:
    return os.environ.get(UPDATE_GOLDEN_ENV_VAR) == "1"


def _write_fixtures(data_dir: Path) -> Path:
    (data_dir / "recipes.yaml").write_text(
        """
- name: Gratin pâtes thon
  servings: 2
  prep_minutes: 25
  cost_level: budget
  tags: [budget, rapide]
  ingredients:
    - name: Pâtes
      quantity: 250
      unit: g
    - name: Thon
      quantity: 1
      unit: boîte
    - name: Emmental râpé
      quantity: 80
      unit: g
- name: Salade riz tomates
  servings: 2
  prep_minutes: 15
  cost_level: budget
  tags: [rapide, batch]
  ingredients:
    - name: Riz
      quantity: 0.2
      unit: kg
    - name: Emmental râpé
      quantity: 60
      unit: g
    - name: Tomates
      quantity: 2
      unit: pièce
""",
        encoding="utf-8",
    )
    (data_dir / "pantry.yaml").write_text(
        """
items:
  - name: riz
    quantity: 100
    unit: g
""",
        encoding="utf-8",
    )
    (data_dir / "catalog.yaml").write_text(
        """
products:
  - name: emmental râpé
    brand: Entremont
aliases:
  thon: thon albacore
""",
        encoding="utf-8",
    )
    prices = data_dir / "prices.yaml"
    prices.write_text(
        """
offers:
  - store: leclerc
    item: pâtes
    product: Pâtes Penne Leclerc 1kg
    price: 1.40
    unit_price: 1.40
    confidence: exact
  - store: leclerc
    item: thon
    product: Thon albacore x3
    price: 4.50
    unit_price: 15.00
    confidence: exact
  - store: leclerc
    item: emmental râpé
    product: Emmental râpé Entremont 200g
    price: 2.30
    unit_price: 11.50
    confidence: exact
  - store: leclerc
    item: riz
    product: Riz long grain Leclerc 1kg
    price: 2.00
    unit_price: 2.00
    confidence: exact
  - store: leclerc
    item: tomates
    product: Tomates rondes Leclerc 1kg
    price: 1.20
    unit_price: 1.20
    confidence: exact
  - store: auchan
    item: pâtes
    product: Pâtes Penne Auchan 1kg
    price: 1.50
    unit_price: 1.50
    confidence: exact
  - store: auchan
    item: thon
    product: Thon albacore Auchan x4
    price: 3.20
    unit_price: 8.00
    confidence: exact
  - store: auchan
    item: emmental râpé
    product: Emmental râpé Auchan 200g
    price: 2.00
    unit_price: 10.00
    confidence: exact
  - store: auchan
    item: riz
    product: Riz basmati Auchan 1kg
    price: 2.10
    unit_price: 2.10
    confidence: exact
  - store: auchan
    item: tomates
    product: Tomates rondes Auchan 1kg
    price: 1.30
    unit_price: 1.30
    confidence: exact
""",
        encoding="utf-8",
    )
    shopping_list = data_dir / "shopping-list.yaml"
    shopping_list.write_text(
        yaml.safe_dump(
            {
                "items": [
                    {"name": "pâtes", "quantity": 250, "unit": "g"},
                    {"name": "thon", "quantity": 1, "unit": "boîte"},
                    {"name": "emmental râpé", "quantity": 140, "unit": "g"},
                    {"name": "riz", "quantity": 100, "unit": "g"},
                    {"name": "tomates", "quantity": 2, "unit": "pièce"},
                ]
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return prices


def _assert_golden(name: str, output: str) -> None:
    assert output.strip(), f"sortie vide pour le golden {name}"
    path = GOLDEN_DIR / f"{name}.txt"
    if _update_golden_requested():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output, encoding="utf-8")
        return
    expected = path.read_text(encoding="utf-8")
    assert output == expected, (
        f"golden {name} divergent. Régénère volontairement avec "
        f"{UPDATE_GOLDEN_ENV_VAR}=1 puis relis la diff."
    )


def _invoke(args: list[str], tmp_path: Path) -> object:
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    return result


def test_golden_compare_simple(tmp_path: Path) -> None:
    prices = _write_fixtures(tmp_path)
    result = _invoke(
        [
            "compare",
            str(tmp_path / "shopping-list.yaml"),
            "--prices",
            str(prices),
            "--mode",
            "simple",
            "--data-dir",
            str(tmp_path),
        ],
        tmp_path,
    )
    _assert_golden("compare_simple", result.output)


def test_golden_compare_economic(tmp_path: Path) -> None:
    prices = _write_fixtures(tmp_path)
    result = _invoke(
        [
            "compare",
            str(tmp_path / "shopping-list.yaml"),
            "--prices",
            str(prices),
            "--mode",
            "economic",
            "--data-dir",
            str(tmp_path),
        ],
        tmp_path,
    )
    _assert_golden("compare_economic", result.output)


def test_golden_compare_hybrid(tmp_path: Path) -> None:
    prices = _write_fixtures(tmp_path)
    result = _invoke(
        [
            "compare",
            str(tmp_path / "shopping-list.yaml"),
            "--prices",
            str(prices),
            "--mode",
            "hybrid",
            "--data-dir",
            str(tmp_path),
        ],
        tmp_path,
    )
    _assert_golden("compare_hybrid", result.output)


def test_golden_plan_with_prices_and_pantry(tmp_path: Path) -> None:
    prices = _write_fixtures(tmp_path)
    result = _invoke(
        [
            "plan",
            "--meals",
            "2",
            "--use-pantry",
            "--prices",
            str(prices),
            "--data-dir",
            str(tmp_path),
        ],
        tmp_path,
    )
    _assert_golden("plan_prices", result.output)


def test_golden_drive_plan_leclerc(tmp_path: Path) -> None:
    _write_fixtures(tmp_path)
    result = _invoke(
        [
            "drive",
            "plan",
            str(tmp_path / "shopping-list.yaml"),
            "--drive",
            "leclerc",
            "--data-dir",
            str(tmp_path),
        ],
        tmp_path,
    )
    _assert_golden("drive_plan_leclerc", result.output)


def test_golden_drive_plan_auchan(tmp_path: Path) -> None:
    _write_fixtures(tmp_path)
    result = _invoke(
        [
            "drive",
            "plan",
            str(tmp_path / "shopping-list.yaml"),
            "--drive",
            "auchan",
            "--data-dir",
            str(tmp_path),
        ],
        tmp_path,
    )
    _assert_golden("drive_plan_auchan", result.output)


def test_golden_recipe_shopping_consolidation(tmp_path: Path) -> None:
    prices = _write_fixtures(tmp_path)
    result = _invoke(
        [
            "recipe",
            "shopping",
            "Gratin pâtes thon",
            "Salade riz tomates",
            "--prices",
            str(prices),
            "--data-dir",
            str(tmp_path),
        ],
        tmp_path,
    )
    _assert_golden("recipe_shopping", result.output)
