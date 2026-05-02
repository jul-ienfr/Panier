from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
import yaml

from panier import __version__
from panier.models import (
    FoodProfile,
    Pantry,
    PriceMode,
    Recipe,
    ShoppingItem,
    StoreOffer,
    dump_yaml,
    load_yaml_model,
    normalize_name,
)
from panier.planner import (
    consolidate_ingredients,
    recommend_basket,
    select_meals,
    subtract_pantry,
)

app = typer.Typer(
    help="Planifie repas et courses optimisées multi-drive.",
    invoke_without_command=True,
)
profile_app = typer.Typer(help="Gérer le profil alimentaire.")
recipe_app = typer.Typer(help="Gérer et suggérer des recettes.")
app.add_typer(profile_app, name="profile")
app.add_typer(recipe_app, name="recipe")

DEFAULT_DATA_DIR = Path.home() / ".panier"


def profile_path(data_dir: Path) -> Path:
    return data_dir / "profile.yaml"


def recipes_path(data_dir: Path) -> Path:
    return data_dir / "recipes.yaml"


def pantry_path(data_dir: Path) -> Path:
    return data_dir / "pantry.yaml"


def load_profile(data_dir: Path) -> FoodProfile:
    path = profile_path(data_dir)
    if not path.exists():
        return FoodProfile()
    return load_yaml_model(path, FoodProfile)


@app.callback()
def main(
    version: Annotated[bool, typer.Option("--version", help="Afficher la version.")] = False,
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()


@profile_app.command("init")
def profile_init(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    path = profile_path(data_dir)
    if path.exists() and not force:
        typer.echo(f"Profil déjà présent : {path}")
        return
    dump_yaml(path, FoodProfile())
    typer.echo(f"Profil créé : {path}")


@profile_app.command("show")
def profile_show(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    typer.echo(
        yaml.safe_dump(
            load_profile(data_dir).model_dump(mode="json"),
            allow_unicode=True,
            sort_keys=True,
        )
    )


def add_preference(kind: str, value: str, data_dir: Path) -> None:
    profile = load_profile(data_dir)
    normalized = normalize_name(value)
    getattr(profile, kind).add(normalized)
    dump_yaml(profile_path(data_dir), profile)
    typer.echo(f"Ajouté à {kind}: {normalized}")


@profile_app.command("allergy")
def profile_allergy(
    action: Annotated[str, typer.Argument(help="add")],
    value: Annotated[str, typer.Argument()],
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    if action != "add":
        raise typer.BadParameter("Seule l'action 'add' existe pour l'instant.")
    add_preference("allergies", value, data_dir)


@profile_app.command("dislike")
def profile_dislike(
    action: Annotated[str, typer.Argument(help="add")],
    value: Annotated[str, typer.Argument()],
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    if action != "add":
        raise typer.BadParameter("Seule l'action 'add' existe pour l'instant.")
    add_preference("dislikes", value, data_dir)


@profile_app.command("forbid")
def profile_forbid(
    action: Annotated[str, typer.Argument(help="add")],
    value: Annotated[str, typer.Argument()],
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    if action != "add":
        raise typer.BadParameter("Seule l'action 'add' existe pour l'instant.")
    add_preference("forbidden", value, data_dir)


@profile_app.command("like")
def profile_like(
    action: Annotated[str, typer.Argument(help="add")],
    value: Annotated[str, typer.Argument()],
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    if action != "add":
        raise typer.BadParameter("Seule l'action 'add' existe pour l'instant.")
    add_preference("likes", value, data_dir)


@recipe_app.command("suggest")
def recipe_suggest(
    meals: Annotated[int, typer.Option("--meals", min=1)] = 3,
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    profile = load_profile(data_dir)
    path = recipes_path(data_dir)
    if not path.exists():
        raise typer.BadParameter(f"Fichier recettes absent : {path}")
    recipes = [
        Recipe.model_validate(item)
        for item in yaml.safe_load(path.read_text(encoding="utf-8"))
    ]
    selected = select_meals(recipes, profile, meals)
    for recipe in selected:
        typer.echo(f"- {recipe.name}")


@app.command("plan")
def plan(
    meals: Annotated[int, typer.Option("--meals", min=1)] = 3,
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    profile = load_profile(data_dir)
    path = recipes_path(data_dir)
    if not path.exists():
        raise typer.BadParameter(f"Fichier recettes absent : {path}")
    recipes = [
        Recipe.model_validate(item)
        for item in yaml.safe_load(path.read_text(encoding="utf-8"))
    ]
    selected = select_meals(recipes, profile, meals)
    items = consolidate_ingredients(selected)
    pantry_file = pantry_path(data_dir)
    if pantry_file.exists():
        items = subtract_pantry(items, load_yaml_model(pantry_file, Pantry))
    typer.echo("Recettes:")
    for recipe in selected:
        typer.echo(f"- {recipe.name}")
    typer.echo("\nListe à acheter:")
    if not items:
        typer.echo("- rien à acheter")
        return
    for item in items:
        quantity = f" {item.quantity:g}" if item.quantity is not None else ""
        unit = f" {item.unit}" if item.unit else ""
        typer.echo(f"- {item.name}{quantity}{unit}")


@app.command("compare")
def compare(
    shopping_list: Annotated[Path, typer.Argument(help="YAML: items: [{name, quantity, unit}]")],
    prices: Annotated[Path, typer.Option("--prices", help="YAML: offers: [...]")],
    mode: Annotated[PriceMode, typer.Option("--mode")] = PriceMode.HYBRID,
    max_stores: Annotated[int, typer.Option("--max-stores", min=1)] = 2,
) -> None:
    list_data = yaml.safe_load(shopping_list.read_text(encoding="utf-8")) or {}
    price_data = yaml.safe_load(prices.read_text(encoding="utf-8")) or {}
    items = [ShoppingItem.model_validate(item) for item in list_data.get("items", [])]
    offers = [StoreOffer.model_validate(offer) for offer in price_data.get("offers", [])]
    recommendation = recommend_basket(items, offers, mode=mode, max_stores=max_stores)

    typer.echo(f"Mode: {recommendation.mode}")
    typer.echo(f"Drives: {', '.join(recommendation.stores)}")
    typer.echo(f"Total: {recommendation.total:.2f} €")
    if recommendation.savings_vs_best_single:
        typer.echo(
            "Économie vs meilleur panier simple: "
            f"{recommendation.savings_vs_best_single:.2f} €"
        )
    typer.echo(f"Raison: {recommendation.reason}")
    typer.echo("\nDétail:")
    for item, offer in recommendation.by_item.items():
        typer.echo(
            f"- {item}: {offer.product} — {offer.store} — "
            f"{offer.price:.2f} € ({offer.confidence})"
        )
