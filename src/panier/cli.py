from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
import yaml

from panier import __version__
from panier.drive import (
    best_offer_for_item,
    build_drive_search_plan,
    collect_drive_offers,
    open_drive_searches,
)
from panier.managed_browser import ManagedBrowserClient, ManagedBrowserError
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
    consume_pantry,
    low_stock_items,
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
pantry_app = typer.Typer(help="Gérer le stock local.")
shopping_app = typer.Typer(help="Générer des listes de courses.")
drive_app = typer.Typer(help="Préparer les recherches et paniers drive.")
app.add_typer(profile_app, name="profile")
app.add_typer(recipe_app, name="recipe")
app.add_typer(pantry_app, name="pantry")
app.add_typer(shopping_app, name="shopping")
app.add_typer(drive_app, name="drive")

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


def load_recipes(data_dir: Path) -> list[Recipe]:
    path = recipes_path(data_dir)
    if not path.exists():
        raise typer.BadParameter(f"Fichier recettes absent : {path}")
    return [
        Recipe.model_validate(item) for item in yaml.safe_load(path.read_text(encoding="utf-8"))
    ]


def load_pantry(data_dir: Path) -> Pantry:
    path = pantry_path(data_dir)
    if not path.exists():
        return Pantry()
    return load_yaml_model(path, Pantry)


def load_pantry_if_exists(data_dir: Path) -> Pantry | None:
    path = pantry_path(data_dir)
    if not path.exists():
        return None
    return load_yaml_model(path, Pantry)


def save_pantry(data_dir: Path, pantry: Pantry) -> None:
    dump_yaml(pantry_path(data_dir), pantry)


def load_offers(prices: Path) -> list[StoreOffer]:
    price_data = yaml.safe_load(prices.read_text(encoding="utf-8")) or {}
    return [StoreOffer.model_validate(offer) for offer in price_data.get("offers", [])]


def parse_quantity_unit(value: str) -> tuple[float, str | None]:
    text = value.strip()
    number = ""
    unit = ""
    for char in text:
        if char.isdigit() or char in ".,":
            if unit:
                raise typer.BadParameter(f"Quantité invalide : {value}")
            number += char.replace(",", ".")
        else:
            unit += char
    if not number:
        raise typer.BadParameter(f"Quantité invalide : {value}")
    return float(number), unit.strip() or None


def load_recipe_file(path: Path) -> Recipe:
    return Recipe.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def recipe_items(recipe: Recipe) -> list[ShoppingItem]:
    return consolidate_ingredients([recipe])


def echo_items(title: str, items: list[ShoppingItem]) -> None:
    typer.echo(title)
    if not items:
        typer.echo("- rien")
        return
    for item in items:
        typer.echo(f"- {format_item(item)}")


def format_item(item: ShoppingItem) -> str:
    quantity = f" {item.quantity:g}" if item.quantity is not None else ""
    unit = f" {item.unit}" if item.unit else ""
    return f"{item.name}{quantity}{unit}"


def echo_recommendation(
    items: list[ShoppingItem],
    recommendation_items: dict[str, StoreOffer],
    mode: PriceMode,
    stores: tuple[str, ...],
    total: float,
    savings_vs_best_single: float,
    reason: str,
) -> None:
    typer.echo("\nRecommandation achat:")
    typer.echo(f"Mode: {mode}")
    typer.echo(f"Drives: {', '.join(stores)}")
    typer.echo(f"Total: {total:.2f} €")
    if savings_vs_best_single:
        typer.echo(f"Économie vs meilleur panier simple: {savings_vs_best_single:.2f} €")
    typer.echo(f"Raison: {reason}")
    typer.echo("\nDétail achat:")
    items_by_name = {item.name: item for item in items}
    for item_name, offer in recommendation_items.items():
        requested = format_item(items_by_name[item_name])
        typer.echo(
            f"- {requested}: {offer.product} — {offer.store} — "
            f"{offer.price:.2f} € ({offer.confidence})"
        )


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


@pantry_app.command("init")
def pantry_init(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    path = pantry_path(data_dir)
    if path.exists() and not force:
        typer.echo(f"Stock déjà présent : {path}")
        return
    dump_yaml(path, Pantry())
    typer.echo(f"Stock créé : {path}")


@pantry_app.command("list")
def pantry_list(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    pantry = load_pantry(data_dir)
    if not pantry.items:
        typer.echo("Stock vide")
        return
    for item in pantry.items:
        line = f"- {format_item(item)}"
        if item.min_quantity is not None:
            min_unit = item.min_unit or item.unit or ""
            line += f" (min {item.min_quantity:g}{(' ' + min_unit) if min_unit else ''})"
        typer.echo(line)
    low = low_stock_items(pantry)
    if low:
        echo_items("\nAlerte réachat:", low)


@pantry_app.command("add")
def pantry_add(
    name: Annotated[str, typer.Argument()],
    quantity: Annotated[float | None, typer.Option("--quantity", "-q", min=0)] = None,
    unit: Annotated[str | None, typer.Option("--unit", "-u")] = None,
    minimum: Annotated[
        str | None, typer.Option("--min", help="Seuil de réachat, ex: 300g ou 1kg")
    ] = None,
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    pantry = load_pantry(data_dir)
    min_quantity = None
    min_unit = None
    if minimum is not None:
        min_quantity, min_unit = parse_quantity_unit(minimum)
    item = ShoppingItem(
        name=name, quantity=quantity, unit=unit, min_quantity=min_quantity, min_unit=min_unit
    )
    matched = False
    for existing in pantry.items:
        if existing.name == item.name and existing.unit == item.unit:
            matched = True
            if existing.quantity is None or item.quantity is None:
                existing.quantity = item.quantity
            else:
                existing.quantity += item.quantity
            if min_quantity is not None:
                existing.min_quantity = min_quantity
                existing.min_unit = min_unit or unit
            break
    if not matched:
        pantry.items.append(item)
    save_pantry(data_dir, pantry)
    typer.echo(f"Stock ajouté : {format_item(item)}")


@pantry_app.command("remove")
def pantry_remove(
    name: Annotated[str, typer.Argument()],
    quantity: Annotated[float | None, typer.Option("--quantity", "-q", min=0)] = None,
    unit: Annotated[str | None, typer.Option("--unit", "-u")] = None,
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    pantry = load_pantry(data_dir)
    item = ShoppingItem(name=name, quantity=quantity, unit=unit)
    remaining: list[ShoppingItem] = []
    removed = False
    for existing in pantry.items:
        if existing.name != item.name or existing.unit != item.unit:
            remaining.append(existing)
            continue
        removed = True
        if quantity is None or existing.quantity is None:
            continue
        new_quantity = existing.quantity - quantity
        if new_quantity > 0:
            existing.quantity = new_quantity
            remaining.append(existing)
    pantry.items = remaining
    save_pantry(data_dir, pantry)
    if removed:
        typer.echo(f"Stock retiré : {format_item(item)}")
    else:
        typer.echo(f"Stock introuvable : {format_item(item)}")


@pantry_app.command("need")
def pantry_need(
    recipe: Annotated[Path, typer.Argument(help="YAML recette: {name, ingredients}")],
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    missing = subtract_pantry(recipe_items(load_recipe_file(recipe)), load_pantry(data_dir))
    echo_items("Manquant:", missing)


@pantry_app.command("consume")
def pantry_consume(
    recipe: Annotated[Path, typer.Argument(help="YAML recette: {name, ingredients}")],
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    requested = recipe_items(load_recipe_file(recipe))
    updated, missing = consume_pantry(requested, load_pantry(data_dir))
    save_pantry(data_dir, updated)
    consumed = subtract_pantry(requested, Pantry(items=missing))
    echo_items("Consommé:", consumed)
    if missing:
        echo_items("\nManquant:", missing)
    low = low_stock_items(updated)
    if low:
        echo_items("\nAlerte réachat:", low)


@shopping_app.command("from-recipe")
def shopping_from_recipe(
    recipe: Annotated[Path, typer.Argument(help="YAML recette: {name, ingredients}")],
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
    prices: Annotated[Path | None, typer.Option("--prices", help="YAML: offers: [...]")] = None,
    mode: Annotated[PriceMode, typer.Option("--mode")] = PriceMode.HYBRID,
    max_stores: Annotated[int, typer.Option("--max-stores", min=1)] = 2,
) -> None:
    items = subtract_pantry(recipe_items(load_recipe_file(recipe)), load_pantry(data_dir))
    echo_items("Liste à acheter:", items)
    if prices is None or not items:
        return
    recommendation = recommend_basket(items, load_offers(prices), mode=mode, max_stores=max_stores)
    echo_recommendation(
        items=items,
        recommendation_items=recommendation.by_item,
        mode=recommendation.mode,
        stores=recommendation.stores,
        total=recommendation.total,
        savings_vs_best_single=recommendation.savings_vs_best_single,
        reason=recommendation.reason,
    )


@drive_app.command("plan")
def drive_plan(
    shopping_list: Annotated[Path, typer.Argument(help="YAML: items: [{name, quantity, unit}]")],
    drive: Annotated[str, typer.Option("--drive", help="Nom du drive cible")] = "leclerc",
) -> None:
    data = yaml.safe_load(shopping_list.read_text(encoding="utf-8")) or {}
    items = [ShoppingItem.model_validate(item) for item in data.get("items", [])]
    echo_items("Liste drive:", items)
    typer.echo("\nRecherches à lancer:")
    for entry in build_drive_search_plan(items, drive):
        typer.echo(f"- {format_item(entry.item)} -> {entry.query} ({entry.confidence})")


@drive_app.command("open")
def drive_open(
    shopping_list: Annotated[Path, typer.Argument(help="YAML: items: [{name, quantity, unit}]")],
    drive: Annotated[str, typer.Option("--drive", help="Nom du drive cible")] = "leclerc",
    profile: Annotated[str, typer.Option("--profile", help="Profil Managed Browser")] = "courses",
    site: Annotated[str | None, typer.Option("--site", help="Site Managed Browser")] = None,
    browser_command: Annotated[
        str | None,
        typer.Option("--browser-command", help="Commande wrapper Managed Browser"),
    ] = None,
) -> None:
    data = yaml.safe_load(shopping_list.read_text(encoding="utf-8")) or {}
    items = [ShoppingItem.model_validate(item) for item in data.get("items", [])]
    browser = ManagedBrowserClient(
        command=browser_command,
        profile=profile,
        site=site or drive,
    )
    try:
        results = open_drive_searches(items, drive, browser)
    except ManagedBrowserError as exc:
        typer.echo(f"Erreur Managed Browser: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo("Recherches ouvertes dans Managed Browser:")
    for result in results:
        tab_id = result.browser_result.data.get("tabId") or result.browser_result.data.get(
            "currentTabId"
        )
        suffix = f" [tab {tab_id}]" if tab_id else ""
        typer.echo(f"- {format_item(result.entry.item)} -> {result.url}{suffix}")


@drive_app.command("pick")
def drive_pick(
    shopping_list: Annotated[Path, typer.Argument(help="YAML: items: [...]")],
    prices: Annotated[Path, typer.Argument(help="YAML: offers: [...]")],
) -> None:
    data = yaml.safe_load(shopping_list.read_text(encoding="utf-8")) or {}
    items = [ShoppingItem.model_validate(item) for item in data.get("items", [])]
    offers = load_offers(prices)
    typer.echo("Meilleurs produits:")
    for item in items:
        chosen = best_offer_for_item(item, offers)
        if chosen is None:
            typer.echo(f"- {format_item(item)}: aucune offre")
            continue
        typer.echo(
            f"- {format_item(item)}: {chosen.offer.product} — {chosen.offer.store} — "
            f"{chosen.offer.price:.2f} € (score {chosen.score:.2f}; {chosen.reason})"
        )


@drive_app.command("collect")
def drive_collect(
    shopping_list: Annotated[Path, typer.Argument(help="YAML: items: [...]")],
    drive: Annotated[str, typer.Option("--drive", help="Nom du drive cible")] = "leclerc",
    profile: Annotated[str, typer.Option("--profile", help="Profil Managed Browser")] = "courses",
    site: Annotated[str | None, typer.Option("--site", help="Site Managed Browser")] = None,
    browser_command: Annotated[
        str | None,
        typer.Option("--browser-command", help="Commande wrapper Managed Browser"),
    ] = None,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    max_results: Annotated[int, typer.Option("--max-results", min=1)] = 5,
) -> None:
    data = yaml.safe_load(shopping_list.read_text(encoding="utf-8")) or {}
    items = [ShoppingItem.model_validate(item) for item in data.get("items", [])]
    browser = ManagedBrowserClient(
        command=browser_command,
        profile=profile,
        site=site or drive,
    )
    try:
        offers = collect_drive_offers(items, drive, browser, max_results=max_results)
    except ManagedBrowserError as exc:
        typer.echo(f"Erreur Managed Browser: {exc}", err=True)
        raise typer.Exit(1) from exc

    payload = {"offers": [offer.model_dump(mode="json") for offer in offers]}
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        typer.echo(f"Offres collectées: {len(offers)} -> {output}")
    else:
        typer.echo(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False))


@recipe_app.command("suggest")
def recipe_suggest(
    meals: Annotated[int, typer.Option("--meals", min=1)] = 3,
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    selected = select_meals(load_recipes(data_dir), load_profile(data_dir), meals)
    for recipe in selected:
        typer.echo(f"- {recipe.name}")


@app.command("plan")
def plan(
    meals: Annotated[int, typer.Option("--meals", min=1)] = 3,
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
    prices: Annotated[Path | None, typer.Option("--prices", help="YAML: offers: [...]")] = None,
    mode: Annotated[PriceMode, typer.Option("--mode")] = PriceMode.HYBRID,
    max_stores: Annotated[int, typer.Option("--max-stores", min=1)] = 2,
) -> None:
    selected = select_meals(load_recipes(data_dir), load_profile(data_dir), meals)
    items = consolidate_ingredients(selected)
    pantry = load_pantry_if_exists(data_dir)
    if pantry is not None:
        items = subtract_pantry(items, pantry)

    typer.echo("Recettes:")
    for recipe in selected:
        typer.echo(f"- {recipe.name}")
    typer.echo("\nListe à acheter:")
    if not items:
        typer.echo("- rien à acheter")
        return
    for item in items:
        typer.echo(f"- {format_item(item)}")

    if prices is None:
        return

    recommendation = recommend_basket(
        items,
        load_offers(prices),
        mode=mode,
        max_stores=max_stores,
    )
    echo_recommendation(
        items=items,
        recommendation_items=recommendation.by_item,
        mode=recommendation.mode,
        stores=recommendation.stores,
        total=recommendation.total,
        savings_vs_best_single=recommendation.savings_vs_best_single,
        reason=recommendation.reason,
    )


@app.command("compare")
def compare(
    shopping_list: Annotated[Path, typer.Argument(help="YAML: items: [{name, quantity, unit}]")],
    prices: Annotated[Path, typer.Option("--prices", help="YAML: offers: [...]")],
    mode: Annotated[PriceMode, typer.Option("--mode")] = PriceMode.HYBRID,
    max_stores: Annotated[int, typer.Option("--max-stores", min=1)] = 2,
) -> None:
    list_data = yaml.safe_load(shopping_list.read_text(encoding="utf-8")) or {}
    items = [ShoppingItem.model_validate(item) for item in list_data.get("items", [])]
    recommendation = recommend_basket(
        items,
        load_offers(prices),
        mode=mode,
        max_stores=max_stores,
    )

    echo_recommendation(
        items=items,
        recommendation_items=recommendation.by_item,
        mode=recommendation.mode,
        stores=recommendation.stores,
        total=recommendation.total,
        savings_vs_best_single=recommendation.savings_vs_best_single,
        reason=recommendation.reason,
    )
