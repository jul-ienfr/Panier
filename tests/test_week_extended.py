from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from panier.cli import app
from panier.models import Recipe
from panier.planner import build_week_plan, canonical_week_slots

RUNNER = CliRunner()


def _recipe(name: str, *, tags: list[str] | None = None) -> Recipe:
    return Recipe(
        name=name,
        servings=2,
        ingredients=[{"name": "riz", "quantity": 100, "unit": "g"}],
        tags=tags or [],
    )


def test_canonical_week_slots_normalizes_and_validates() -> None:
    assert canonical_week_slots(["dej", "Dîner"]) == ["dej", "diner"]
    try:
        canonical_week_slots(["goûter"])
    except ValueError:
        pass
    else:
        raise AssertionError("ValueError attendu")


def test_build_week_plan_fills_distinct_recipes_first() -> None:
    ranked = [_recipe(n) for n in ["Alpha", "Beta", "Gamma", "Delta"]]

    plan = build_week_plan(ranked, days=2, slots=["diner"], max_repeats_per_week=2)

    names = [a.recipe.name for a in plan.assignments]
    assert names == ["Alpha", "Beta"]
    assert all(a.slot == "diner" for a in plan.assignments)


def test_build_week_plan_reuses_batch_first_under_cap() -> None:
    ranked = [
        _recipe("Alpha", tags=["batch"]),
        _recipe("Beta"),
        _recipe("Gamma"),
    ]

    plan = build_week_plan(ranked, days=4, slots=["diner"], max_repeats_per_week=2)

    names = [a.recipe.name for a in plan.assignments]
    # passe 1 : Alpha, Beta, Gamma ; passe 2 (jour 4) : réutilisation batch d'abord
    assert names == ["Alpha", "Beta", "Gamma", "Alpha"]
    assert plan.counts["alpha"] == 2  # cap respecté


def test_build_week_plan_respects_max_repeats_cap() -> None:
    ranked = [_recipe("Solo", tags=["batch"])]

    plan = build_week_plan(
        ranked, days=5, slots=["diner", "dej"], max_repeats_per_week=2
    )

    names = [a.recipe.name for a in plan.assignments]
    assert len(names) == 10 - 8  # 10 slots demandés, cap 2 -> 2 remplis
    assert len(plan.assignments) == 2
    assert all(name == "Solo" for name in names)
    assert plan.counts == {"solo": 2}


def test_build_week_plan_shuffle_seed_changes_but_deterministic() -> None:
    ranked = [_recipe(n) for n in ["Alpha", "Beta", "Gamma"]]

    first = build_week_plan(ranked, days=1, slots=["dej", "diner"], shuffle_seed=42)
    second = build_week_plan(ranked, days=1, slots=["dej", "diner"], shuffle_seed=42)

    assert [a.recipe.name for a in first.assignments] == [
        a.recipe.name for a in second.assignments
    ]


def test_week_command_grid_slots_costs_and_consolidation(tmp_path: Path) -> None:
    (tmp_path / "recipes.yaml").write_text(
        """
- name: Riz rapide
  servings: 2
  tags: [batch, budget]
  ingredients:
    - name: riz
      quantity: 200
      unit: g
- name: Pâtes vertes
  servings: 2
  tags: [budget]
  ingredients:
    - name: pâtes
      quantity: 250
      unit: g
""".strip(),
        encoding="utf-8",
    )
    prices = tmp_path / "prices.yaml"
    prices.write_text(
        yaml.safe_dump(
            {
                "offers": [
                    {"store": "leclerc", "item": "riz", "product": "Riz 1kg", "price": 2.0},
                    {"store": "leclerc", "item": "pâtes", "product": "Pâtes 1kg", "price": 1.5},
                ]
            }
        ),
        encoding="utf-8",
    )

    result = RUNNER.invoke(
        app,
        [
            "week",
            "--days",
            "2",
            "--slots",
            "dej,diner",
            "--prices",
            str(prices),
            "--data-dir",
            str(tmp_path),
            "--no-pantry",
            "--no-balanced",
        ],
    )

    assert result.exit_code == 0
    assert "Semaine:" in result.output
    assert "Jour 1 déj:" in result.output
    assert "Jour 2 dîner:" in result.output
    assert "Coût hebdo" in result.output
    assert "À acheter:" in result.output


def test_week_command_without_new_flags_stays_legacy(tmp_path: Path) -> None:
    (tmp_path / "recipes.yaml").write_text(
        """
- name: Riz rapide
  servings: 2
  tags: [budget]
  ingredients:
    - name: riz
      quantity: 200
      unit: g
""".strip(),
        encoding="utf-8",
    )

    result = RUNNER.invoke(
        app, ["week", "--meals", "2", "--no-balanced", "--data-dir", str(tmp_path)]
    )

    assert result.exit_code == 0
    assert "Semaine: 2 repas" in result.output
    lines = [line for line in result.output.splitlines() if line.startswith("Jour ")]
    assert len(lines) == 2
    assert all("dîner" in line for line in lines)


def test_week_command_reports_unfilled_slots_when_pool_too_small(tmp_path: Path) -> None:
    (tmp_path / "recipes.yaml").write_text(
        """
- name: Riz rapide
  servings: 2
  tags: [budget]
  ingredients:
    - name: riz
      quantity: 200
      unit: g
""".strip(),
        encoding="utf-8",
    )

    result = RUNNER.invoke(
        app,
        [
            "week",
            "--days",
            "7",
            "--slots",
            "dej,diner",
            "--max-repeats-per-week",
            "1",
            "--no-balanced",
            "--data-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "slot(s) non pourvu(s)" in result.output
