from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from typer.testing import CliRunner

import panier.cli as cli
from panier.cli import app
from panier.history_store import record_offers
from panier.models import StoreOffer
from panier.recipe_cost import compute_recipe_cost, recipe_cost_sort_key

RUNNER = CliRunner()


def _setup(tmp_path: Path) -> None:
    (tmp_path / "recipes.yaml").write_text(
        """
- name: Pâtes thon
  servings: 2
  prep_minutes: 15
  cost_level: budget
  tags: [budget, rapide]
  ingredients:
    - name: pâtes
      quantity: 250
      unit: g
    - name: thon
      quantity: 1
      unit: boîte
- name: Riz courgettes
  servings: 2
  prep_minutes: 20
  cost_level: budget
  tags: [budget]
  ingredients:
    - name: riz
      quantity: 200
      unit: g
    - name: courgettes
      quantity: 2
      unit: pièce
""".strip(),
        encoding="utf-8",
    )


def _prices_file(tmp_path: Path) -> Path:
    prices = tmp_path / "prices.yaml"
    prices.write_text(
        yaml.safe_dump(
            {
                "offers": [
                    {
                        "store": "leclerc",
                        "item": "pâtes",
                        "product": "Pâtes 1kg",
                        "price": 1.4,
                    },
                    {
                        "store": "leclerc",
                        "item": "thon",
                        "product": "Thon x3",
                        "price": 4.5,
                    },
                    {
                        "store": "auchan",
                        "item": "riz",
                        "product": "Riz 1kg",
                        "price": 2.0,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return prices


def test_recipe_cost_from_prices_yaml_is_deterministic(tmp_path: Path) -> None:
    _setup(tmp_path)
    prices = _prices_file(tmp_path)

    result = RUNNER.invoke(
        app,
        ["recipe", "cost", "Pâtes thon", "--prices", str(prices), "--data-dir", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert "- pâtes: 1.40 € (leclerc)" in result.output
    assert "- thon: 4.50 € (leclerc)" in result.output
    assert "Total: 5.90 €" in result.output
    assert "Coût par portion: 2.95 €" in result.output


def test_recipe_cost_falls_back_to_history_and_flags_unpriced(tmp_path: Path) -> None:
    _setup(tmp_path)
    base = datetime.now(UTC) - timedelta(hours=2)
    record_offers(
        tmp_path,
        "leclerc",
        [StoreOffer(store="leclerc", item="riz", product="Riz Leclerc", price=1.8)],
        collected_at=base,
    )

    result = RUNNER.invoke(app, ["recipe", "cost", "Riz courgettes", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert "- riz: 1.80 € (historique leclerc)" in result.output
    assert "- courgettes: unpriced (aucun prix connu)" in result.output
    assert "Total partiel: 1.80 € (1 article(s) non pricé(s))" in result.output


def test_prices_yaml_overrides_history(tmp_path: Path) -> None:
    _setup(tmp_path)
    record_offers(
        tmp_path,
        "leclerc",
        [StoreOffer(store="leclerc", item="riz", product="Riz Leclerc", price=9.9)],
    )
    prices = tmp_path / "only-riz.yaml"
    prices.write_text(
        yaml.safe_dump(
            {"offers": [{"store": "auchan", "item": "riz", "product": "Riz Auchan", "price": 2.0}]}
        )
    )

    cost = compute_recipe_cost(
        next(r for r in cli.load_recipes(tmp_path) if r.name == "Riz courgettes"),
        data_dir=tmp_path,
        prices_offers=cli.load_offers(prices),
    )

    entry = next(c for c in cost.costs if c.name == "riz")
    assert entry.source == "prices"
    assert entry.price == 2.0
    assert entry.store == "auchan"


def test_recipe_list_sort_cost_orders_then_unpriced_last(tmp_path: Path) -> None:
    _setup(tmp_path)
    record_offers(
        tmp_path,
        "leclerc",
        [
            StoreOffer(store="leclerc", item="riz", product="Riz L", price=2.0),
            StoreOffer(store="leclerc", item="courgettes", product="Courgettes L", price=1.5),
        ],
    )

    result = RUNNER.invoke(app, ["recipe", "list", "--sort-cost", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if line.startswith("- ")]
    assert len(lines) == 2
    assert lines[0].startswith("- Riz courgettes") and "3.50 €" in lines[0]
    assert "Pâtes thon" in lines[1] and "coût inconnu" in lines[1]


def test_recipe_cost_sort_key_puts_unpriced_last_with_name_tiebreak() -> None:
    from panier.models import Recipe

    cheap = compute_recipe_cost(
        Recipe(name="A", servings=1, ingredients=[]),
        data_dir=Path("/nonexistent"),
    )
    expensive = compute_recipe_cost(
        Recipe(name="B", servings=1, ingredients=[]),
        data_dir=Path("/nonexistent"),
    )
    # sans aucune source : tout est unpriced -> tri par nom
    assert recipe_cost_sort_key(cheap) <= recipe_cost_sort_key(expensive)


def test_recipe_suggest_max_cost_per_meal_filters_known_costs_only(
    tmp_path: Path,
) -> None:
    _setup(tmp_path)
    record_offers(
        tmp_path,
        "leclerc",
        [
            StoreOffer(store="leclerc", item="riz", product="Riz L", price=2.0),
            StoreOffer(store="leclerc", item="courgettes", product="Courgettes L", price=6.0),
            StoreOffer(store="leclerc", item="pâtes", product="Pâtes L", price=1.4),
            StoreOffer(store="leclerc", item="thon", product="Thon L", price=4.5),
        ],
    )

    strict = RUNNER.invoke(
        app,
        [
            "recipe",
            "suggest",
            "--meals",
            "2",
            "--max-cost-per-meal",
            "6",
            "--data-dir",
            str(tmp_path),
        ],
    )

    assert strict.exit_code == 0
    # Pâtes thon coûte 5.90 € (<= 6, conservée) ; Riz courgettes 8.00 € (> 6, exclue).
    assert "Pâtes thon" in strict.output
    assert "Riz courgettes" not in strict.output

    loose = RUNNER.invoke(
        app,
        [
            "recipe",
            "suggest",
            "--meals",
            "2",
            "--max-cost-per-meal",
            "10",
            "--data-dir",
            str(tmp_path),
        ],
    )

    assert loose.exit_code == 0
    assert "Pâtes thon" in loose.output
    assert "Riz courgettes" in loose.output


def test_unpriced_recipes_survive_max_cost_filter_with_marker(tmp_path: Path) -> None:
    _setup(tmp_path)

    result = RUNNER.invoke(
        app,
        [
            "recipe",
            "suggest",
            "--meals",
            "1",
            "--max-cost-per-meal",
            "1",
            "--data-dir",
            str(tmp_path),
        ],
    )

    # aucun prix connu : rien n'est exclu, marqueur coût inconnu affiché
    assert result.exit_code == 0
    assert result.output.strip().startswith("- ")
    assert "coût inconnu" in result.output
