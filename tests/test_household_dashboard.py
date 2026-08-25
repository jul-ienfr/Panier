from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

import panier.cli as cli
from panier.cli import app
from panier.household import (
    HouseholdProfile,
    active_household_name,
    list_households,
    load_household,
    slugify,
)

RUNNER = CliRunner()


def _base_profile(tmp_path: Path, *, allergies: list[str] | None = None) -> None:
    (tmp_path / "profile.yaml").write_text(
        yaml.safe_dump({"allergies": allergies or []}), encoding="utf-8"
    )


def test_household_lifecycle_create_use_set_remove(tmp_path: Path) -> None:
    create = RUNNER.invoke(
        app,
        [
            "profile",
            "create",
            "famille",
            "--display-name",
            "Famille",
            "--geo-zone",
            "74350",
            "--enable-store",
            "leclerc",
            "--disable-store",
            "auchan",
            "--servings",
            "4",
            "--budget-max-eur",
            "80",
            "--data-dir",
            str(tmp_path),
        ],
    )

    assert create.exit_code == 0, create.output
    assert list_households(tmp_path) == ["famille"]

    use = RUNNER.invoke(app, ["profile", "use", "famille", "--data-dir", str(tmp_path)])
    assert use.exit_code == 0
    assert active_household_name(tmp_path) == "famille"

    set_result = RUNNER.invoke(
        app,
        [
            "profile",
            "set",
            "--enable-store",
            "auchan",
            "--clear-budget",
            "--data-dir",
            str(tmp_path),
        ],
    )
    assert set_result.exit_code == 0

    household = load_household(tmp_path, "famille")
    assert household.stores == {"auchan": True, "leclerc": True}
    assert household.budget_max_eur is None
    assert household.geo_zone == "74350"

    remove_active = RUNNER.invoke(
        app, ["profile", "remove", "famille", "--data-dir", str(tmp_path)]
    )
    assert remove_active.exit_code != 0  # actif : refusé sans --force

    unuse = RUNNER.invoke(app, ["profile", "unuse", "--data-dir", str(tmp_path)])
    assert unuse.exit_code == 0
    remove = RUNNER.invoke(
        app, ["profile", "remove", "famille", "--data-dir", str(tmp_path)]
    )
    assert remove.exit_code == 0
    assert list_households(tmp_path) == []


def test_preferences_commands_target_active_household_file(tmp_path: Path) -> None:
    _base_profile(tmp_path)
    RUNNER.invoke(
        app,
        [
            "profile",
            "create",
            "bebe",
            "--from-base",
            "--geo-zone",
            "Annemasse",
            "--data-dir",
            str(tmp_path),
        ],
    )
    RUNNER.invoke(app, ["profile", "use", "bebe", "--data-dir", str(tmp_path)])

    add = RUNNER.invoke(
        app,
        [
            "profile",
            "allergy",
            "add",
            "arachide",
            "--reason",
            "pédiatre",
            "--data-dir",
            str(tmp_path),
        ],
    )

    assert add.exit_code == 0
    base = yaml.safe_load((tmp_path / "profile.yaml").read_text(encoding="utf-8"))
    household = yaml.safe_load((tmp_path / "profiles" / "bebe.yaml").read_text(encoding="utf-8"))
    assert base["allergies"] == []  # le fichier de base reste intact
    assert "arachide" in household["preferences"]["allergies"]
    assert (
        household["preferences"]["details"]["allergies:arachide"]["reason"] == "pédiatre"
    )

    # la lecture passe bien par le foyer actif
    profile = cli.load_profile(tmp_path)
    assert "arachide" in profile.allergies


def test_legacy_behavior_without_any_household(tmp_path: Path) -> None:
    _base_profile(tmp_path, allergies=["crevette"])

    result = RUNNER.invoke(
        app, ["profile", "dislike", "add", "épinards", "--data-dir", str(tmp_path)]
    )

    assert result.exit_code == 0
    saved = yaml.safe_load((tmp_path / "profile.yaml").read_text(encoding="utf-8"))
    assert saved["allergies"] == ["crevette"]
    assert "épinards" in saved["dislikes"]


def test_disabled_store_is_filtered_from_compare(tmp_path: Path) -> None:
    shopping = tmp_path / "list.yaml"
    prices = tmp_path / "prices.yaml"
    shopping.write_text(yaml.safe_dump({"items": [{"name": "yaourt"}]}), encoding="utf-8")
    prices.write_text(
        yaml.safe_dump(
            {
                "offers": [
                    {
                        "store": "auchan",
                        "item": "yaourt",
                        "product": "Yaourt Auchan",
                        "price": 2.0,
                    },
                    {
                        "store": "leclerc",
                        "item": "yaourt",
                        "product": "Yaourt Leclerc",
                        "price": 2.5,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    save = HouseholdProfile(name="solo", stores={"auchan": False})
    from panier.household import save_household, set_active_household

    save_household(tmp_path, save)
    set_active_household(tmp_path, "solo")

    result = RUNNER.invoke(
        app,
        [
            "compare",
            str(shopping),
            "--prices",
            str(prices),
            "--data-dir",
            str(tmp_path),
            "--max-stores",
            "1",
        ],
    )

    assert result.exit_code == 0
    assert "Yaourt Auchan" not in result.output
    assert "Yaourt Leclerc" in result.output
    assert "Total: 2.50 €" in result.output


def test_dashboard_text_and_json(tmp_path: Path) -> None:
    _base_profile(tmp_path, allergies=["crevette"])
    (tmp_path / "recipes.yaml").write_text(
        "- name: Riz rapide\n  servings: 2\n  tags: [budget]\n  ingredients:\n"
        "    - name: riz\n      quantity: 100\n      unit: g",
        encoding="utf-8",
    )
    RUNNER.invoke(
        app,
        [
            "profile",
            "create",
            "famille",
            "--from-base",
            "--geo-zone",
            "74350",
            "--disable-store",
            "intermarche",
            "--data-dir",
            str(tmp_path),
        ],
    )
    RUNNER.invoke(app, ["profile", "use", "famille", "--data-dir", str(tmp_path)])

    text = RUNNER.invoke(app, ["dashboard", "--data-dir", str(tmp_path)])
    payload = RUNNER.invoke(app, ["dashboard", "--format", "json", "--data-dir", str(tmp_path)])

    assert text.exit_code == 0
    assert "Tableau de bord Panier" in text.output
    assert "foyer:famille" in text.output
    assert "crevette" in text.output
    assert "désactivés: intermarche" in text.output

    assert payload.exit_code == 0
    parsed = yaml.safe_load(payload.output)
    assert parsed["active_household"] == "famille"
    assert "crevette" in parsed["preferences"]["allergies"]
    assert parsed["household"]["stores_disabled"] == ["intermarche"]
    assert parsed["files"]["recipes_count"] == 1


def test_slugify_normalizes_names() -> None:
    assert slugify("Famille Allée") == "famille-allée"
    try:
        HouseholdProfile(name="../evil")
    except Exception:
        pass
    else:
        raise AssertionError("nom invalide refusé attendu")
