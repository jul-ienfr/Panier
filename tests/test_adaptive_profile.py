from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

import panier.cli as cli
from panier.cli import app
from panier.models import FoodProfile, PreferenceConfidence, Recipe, load_yaml_model
from panier.planner import rank_recipes

RUNNER = CliRunner()


def _recipes() -> list[Recipe]:
    return [
        Recipe(
            name="Pâtes jambon",
            servings=2,
            ingredients=[
                {"name": "pâtes", "quantity": 250, "unit": "g"},
                {"name": "jambon", "quantity": 2, "unit": "tranche"},
            ],
            tags=["rapide"],
        ),
        Recipe(
            name="Riz courgettes",
            servings=2,
            ingredients=[
                {"name": "riz", "quantity": 200, "unit": "g"},
                {"name": "courgettes", "quantity": 2, "unit": "pièce"},
            ],
            tags=["equilibre"],
        ),
    ]


def test_retrocompatible_plain_string_profile(tmp_path: Path) -> None:
    (tmp_path / "profile.yaml").write_text(
        yaml.safe_dump({"allergies": ["crevette"], "dislikes": ["épinards"]}),
        encoding="utf-8",
    )

    profile = cli.load_profile(tmp_path)

    assert profile.allergies == {"crevette"}
    assert profile.dislikes == {"épinards"}
    assert profile.blocked_reason("crevette") is not None


def test_object_entries_extract_details_and_keep_values(tmp_path: Path) -> None:
    (tmp_path / "profile.yaml").write_text(
        yaml.safe_dump(
            {
                "dislikes": [
                    "oignons",
                    {
                        "value": "chou fleur",
                        "status": "temporary-context",
                        "reason": "regime passager",
                        "confidence": "medium",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    path = tmp_path / "profile.yaml"

    profile = load_yaml_model(path, FoodProfile)

    assert "oignons" in profile.dislikes
    assert "chou fleur" in profile.dislikes
    detail = profile.detail_for("dislikes", "chou fleur")
    assert detail is not None
    assert detail.status.value == "temporary-context"
    assert detail.confidence is PreferenceConfidence.MEDIUM
    assert profile.detail_for("dislikes", "oignons") is None
    # un refus contextuel ne filtre pas
    assert profile.is_blocked("chou fleur") is False
    # un détesté permanent filtre toujours
    assert profile.is_blocked("oignons") is True


def test_allergy_is_absolute_regardless_of_status_or_confidence(tmp_path: Path) -> None:
    """INVARIANT : allergène rejeté absolument, même en temporary-context/low."""
    recipes = _recipes()
    profile = FoodProfile()
    profile.allergies.add("courgettes")
    profile.set_detail(
        "allergies",
        cli.PreferenceDetail(
            value="courgettes",
            status=cli.PreferenceStatus.TEMPORARY_CONTEXT,
            confidence=PreferenceConfidence.LOW,
        ),
    )

    ranked = rank_recipes(recipes, profile)

    assert [recipe.name for recipe in ranked] == ["Pâtes jambon"]


def test_temporary_context_dislike_penalizes_but_does_not_filter(tmp_path: Path) -> None:
    recipes = _recipes()
    profile = FoodProfile()
    profile.dislikes.add("courgettes")
    profile.set_detail(
        "dislikes",
        cli.PreferenceDetail(value="courgettes", status=cli.PreferenceStatus.TEMPORARY_CONTEXT),
    )

    ranked = rank_recipes(recipes, profile)

    assert {recipe.name for recipe in ranked} == {
        "Pâtes jambon",
        "Riz courgettes",
    }, "le refus contextuel ne doit pas exclure"
    # pénalité : la recette détestée passe après l'autre
    assert ranked[-1].name == "Riz courgettes"


def test_confirmed_like_boosts_ranking_order() -> None:
    recipes = [
        Recipe(
            name="Zeitoun riz",
            servings=2,
            ingredients=[{"name": "riz", "quantity": 1, "unit": "verre"}],
        ),
        Recipe(
            name="Bowl quinoa",
            servings=2,
            ingredients=[{"name": "quinoa", "quantity": 1, "unit": "verre"}],
        ),
    ]
    liked = FoodProfile()
    liked.likes.add("riz")
    liked.set_detail(
        "likes",
        cli.PreferenceDetail(value="riz", confidence=PreferenceConfidence.CONFIRMED),
    )

    with_like = rank_recipes(recipes, liked)
    without = rank_recipes(recipes, FoodProfile())

    # sans préférence : tri par nom ; avec un like confirmé : la recette aimée passe devant
    assert without[0].name == "Bowl quinoa"
    assert with_like[0].name == "Zeitoun riz"


def test_cli_add_remove_with_reason_confidence_status(tmp_path: Path) -> None:
    result = RUNNER.invoke(
        app,
        [
            "profile",
            "dislike",
            "add",
            "chou",
            "--reason",
            "digestion",
            "--confidence",
            "strong",
            "--status",
            "temporary-context",
            "--data-dir",
            str(tmp_path),
        ],
    )
    add_result = result
    show_result = RUNNER.invoke(app, ["profile", "show", "--data-dir", str(tmp_path)])
    remove_result = RUNNER.invoke(
        app, ["profile", "dislike", "remove", "chou", "--data-dir", str(tmp_path)]
    )

    assert add_result.exit_code == 0
    assert show_result.exit_code == 0
    assert "temporary-context" in show_result.output
    assert "digestion" in show_result.output
    assert remove_result.exit_code == 0
    after = RUNNER.invoke(app, ["profile", "show", "--data-dir", str(tmp_path)])
    assert "chou" not in after.output


def test_accept_recipe_with_reason_persisted(tmp_path: Path) -> None:
    result = RUNNER.invoke(
        app,
        [
            "profile",
            "accept-recipe",
            "add",
            "Chili doux",
            "--reason",
            "validée en famille",
            "--data-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    saved = yaml.safe_load((tmp_path / "profile.yaml").read_text(encoding="utf-8"))
    assert "chili doux" in saved["accepted_recipes"]
    assert saved["details"]["accepted_recipes:chili doux"]["reason"] == (
        "validée en famille"
    )


def test_scoring_never_filters_on_soft_preferences(tmp_path: Path) -> None:
    """Likes et refus contextuels ne retirent aucune recette du pool."""
    from panier.planner import compatible_recipes

    profile = FoodProfile()
    profile.likes.add("riz")
    profile.dislikes.add("courgettes")
    profile.set_detail(
        "dislikes",
        cli.PreferenceDetail(value="courgettes", status=cli.PreferenceStatus.TEMPORARY_CONTEXT),
    )

    compatible = compatible_recipes(_recipes(), profile)

    assert {recipe.name for recipe in compatible} == {"Pâtes jambon", "Riz courgettes"}
