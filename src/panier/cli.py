from __future__ import annotations

import inspect
from pathlib import Path
from typing import Annotated

import click
import typer
import yaml

from panier import __version__
from panier.catalog import ProductCatalog, load_catalog
from panier.deterministic import NO_LLM_ENV_VAR, explain_item, no_llm_status
from panier.drive import (
    best_offer_for_item,
    build_drive_search_plan,
    collect_drive_offers,
    open_drive_searches,
)
from panier.managed_browser import ManagedBrowserClient, ManagedBrowserError
from panier.models import (
    FoodProfile,
    Ingredient,
    Pantry,
    PriceMode,
    Recipe,
    ShoppingItem,
    StoreOffer,
    dump_yaml,
    load_yaml_model,
    normalize_name,
)
from panier.nutrition import BalanceScore, score_recipe_balance
from panier.planner import (
    CompareBy,
    consolidate_ingredients,
    consume_pantry,
    filter_recipes,
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
llm_app = typer.Typer(help="État et garde-fous LLM.")
explain_app = typer.Typer(help="Expliquer les choix déterministes locaux.")
app.add_typer(profile_app, name="profile")
app.add_typer(recipe_app, name="recipe")
app.add_typer(pantry_app, name="pantry")
app.add_typer(shopping_app, name="shopping")
app.add_typer(drive_app, name="drive")
app.add_typer(llm_app, name="llm")
app.add_typer(explain_app, name="explain")

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
    return read_recipes_file(path)


def read_recipes_file(path: Path) -> list[Recipe]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if isinstance(data, dict):
        data = [data]
    return [Recipe.model_validate(item) for item in data]


def save_recipes(data_dir: Path, recipes: list[Recipe]) -> None:
    path = recipes_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            [recipe.model_dump(mode="json") for recipe in recipes],
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


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


def find_recipe(recipes: list[Recipe], name: str) -> Recipe:
    normalized = normalize_name(name)
    for recipe in recipes:
        if normalize_name(recipe.name) == normalized:
            return recipe
    raise typer.BadParameter(f"Recette introuvable : {name}")


def selected_recipes(data_dir: Path, names: list[str]) -> list[Recipe]:
    recipes = load_recipes(data_dir)
    if not names:
        raise typer.BadParameter("Indique au moins une recette.")
    return [find_recipe(recipes, name) for name in names]


def parse_csv_set(value: str | None) -> set[str] | None:
    if value is None:
        return None
    parsed = {normalize_name(part) for part in value.split(",") if part.strip()}
    return parsed or None


def recipes_to_shopping_payload(items: list[ShoppingItem]) -> dict[str, list[dict[str, object]]]:
    return {"items": [item.model_dump(mode="json", exclude_none=True) for item in items]}


def managed_browser_profile_for_drive(profile: str, drive: str) -> str:
    """Résout le profil Managed Browser adapté au drive.

    Le profil historique `courses` reste valide pour Leclerc, mais Auchan est déclaré
    côté Managed Browser sous `courses-auchan`. Utiliser `courses` avec `site=auchan`
    déclenche une erreur HTTP 500 de politique de profil.
    """
    if normalize_name(profile) == "courses" and normalize_name(drive) == "auchan":
        return "courses-auchan"
    return profile


def collect_offers_for_drives(
    items: list[ShoppingItem],
    drives: list[str],
    *,
    profile: str,
    browser_command: str | None,
    max_results: int,
    catalog: ProductCatalog | None = None,
) -> list[StoreOffer]:
    offers: list[StoreOffer] = []
    for drive in drives:
        resolved_profile = managed_browser_profile_for_drive(profile, drive)
        browser = ManagedBrowserClient(command=browser_command, profile=resolved_profile, site=drive)
        try:
            collected = _collect_drive_offers_with_optional_catalog(
                items, drive, browser, max_results=max_results, catalog=catalog
            )
        except ManagedBrowserError as exc:
            typer.echo(
                f"Avertissement Managed Browser {drive}: {exc}; collecte ignorée pour ce drive.",
                err=True,
            )
            continue
        typer.echo(f"Collecte {drive}: {len(collected)} offres")
        offers.extend(collected)
    return offers


def _collect_drive_offers_with_optional_catalog(
    items: list[ShoppingItem],
    drive: str,
    browser: ManagedBrowserClient,
    *,
    max_results: int,
    catalog: ProductCatalog | None,
) -> list[StoreOffer]:
    # Tests and callers may monkeypatch collect_drive_offers with the historical
    # signature. Keep that backward-compatible while passing catalog to the real
    # implementation when supported.
    if "catalog" in inspect.signature(collect_drive_offers).parameters:
        return collect_drive_offers(
            items, drive, browser, max_results=max_results, catalog=catalog
        )
    return collect_drive_offers(items, drive, browser, max_results=max_results)


def parse_recipe_ingredient(value: str) -> Ingredient:
    parts = [part.strip() for part in value.split(":")]
    if not parts or not parts[0]:
        raise typer.BadParameter("Ingrédient invalide. Format: nom[:quantité[:unité]]")
    name = parts[0]
    quantity = None
    unit = None
    if len(parts) >= 2 and parts[1]:
        quantity = float(parts[1].replace(",", "."))
    if len(parts) >= 3 and parts[2]:
        unit = parts[2]
    if len(parts) > 3:
        raise typer.BadParameter("Ingrédient invalide. Format: nom[:quantité[:unité]]")
    return Ingredient(name=name, quantity=quantity, unit=unit)


def shopping_items_for_recipes(
    recipes: list[Recipe], pantry: Pantry | None = None
) -> list[ShoppingItem]:
    items = consolidate_ingredients(recipes)
    if pantry is not None:
        return subtract_pantry(items, pantry)
    return items


def echo_recipe_selection(recipes: list[Recipe], *, show_balance: bool = False) -> None:
    typer.echo("Recettes:")
    for recipe in recipes:
        suffix = ""
        if show_balance:
            score = score_recipe_balance(recipe)
            suffix = f" — équilibre {score.score}/100 ({score.verdict})"
        typer.echo(f"- {recipe.name}{suffix}")


def format_balance_score(score: BalanceScore) -> str:
    positives = ", ".join(score.positives) if score.positives else "aucun signal positif"
    penalties = ", ".join(score.penalties) if score.penalties else "aucune pénalité"
    return (
        f"Équilibre: {score.score}/100 ({score.verdict})\n"
        f"+ {positives}\n"
        f"- {penalties}"
    )


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
    compare_by: CompareBy = "price",
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
            f"{format_offer_price(offer, items_by_name[item_name], compare_by)} "
            f"({offer.confidence})"
        )


def format_offer_price(
    offer: StoreOffer, item: ShoppingItem | None = None, compare_by: CompareBy = "price"
) -> str:
    price = f"{offer.price:.2f} €"
    if compare_by == "unit_price" and offer.unit_price is not None:
        return f"{price}; {offer.unit_price:.2f} €/{unit_price_label(item)}"
    return price


def unit_price_label(item: ShoppingItem | None) -> str:
    if item is None or item.unit is None:
        return "kg/L"
    unit = normalize_name(item.unit)
    if unit in {"l", "litre", "litres", "ml", "cl"}:
        return "L"
    if unit in {"g", "kg", "gramme", "grammes"}:
        return "kg"
    return "unité"


def normalize_compare_by(value: str) -> CompareBy:
    normalized = normalize_name(value).replace("-", "_")
    if normalized in {"unit_price", "prix_unitaire", "prix_quantite", "kg", "l"}:
        return "unit_price"
    if normalized == "price":
        return "price"
    raise typer.BadParameter("compare-by doit être 'price' ou 'unit-price'")


@app.callback()
def main(
    version: Annotated[bool, typer.Option("--version", help="Afficher la version.")] = False,
    no_llm: Annotated[
        bool,
        typer.Option(
            "--no-llm",
            help=f"Forcer le mode déterministe sans LLM (équivalent {NO_LLM_ENV_VAR}=1).",
            envvar=NO_LLM_ENV_VAR,
        ),
    ] = False,
) -> None:
    if no_llm:
        click.get_current_context().obj = {"no_llm": True}
    if version:
        typer.echo(__version__)
        raise typer.Exit()


def current_cli_no_llm() -> bool:
    obj = click.get_current_context().find_root().obj
    return bool(isinstance(obj, dict) and obj.get("no_llm"))


@llm_app.command("status")
def llm_status() -> None:
    status = no_llm_status(cli_no_llm=current_cli_no_llm())
    typer.echo(f"Mode: {status.mode}")
    typer.echo(f"LLM autorisé: {'non' if status.no_llm else 'oui'}")
    typer.echo(f"Garde-fou: {NO_LLM_ENV_VAR}")
    typer.echo(f"Source: {status.source}")
    if status.raw_value is not None:
        typer.echo(f"Valeur: {status.raw_value}")
    typer.echo("Appels LLM implémentés: non")


@explain_app.command("item")
def explain_item_command(
    name: Annotated[str, typer.Argument(help="Nom d'ingrédient ou produit à expliquer")],
) -> None:
    explanation = explain_item(name)
    typer.echo(f"Entrée: {explanation.input_name}")
    typer.echo(f"Nom canonique: {explanation.canonical_name}")
    typer.echo(f"Requête: {explanation.query}")
    typer.echo(f"Confiance: {explanation.confidence}")
    typer.echo(f"Raison: {explanation.reason}")


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


def preference_label(kind: str) -> str:
    return {
        "accepted_recipes": "recettes acceptées",
        "rejected_recipes": "recettes rejetées",
    }.get(kind, kind)


def add_profile_value(kind: str, value: str, data_dir: Path) -> None:
    profile = load_profile(data_dir)
    normalized = normalize_name(value)
    getattr(profile, kind).add(normalized)
    if kind == "accepted_recipes":
        profile.rejected_recipes.discard(normalized)
    elif kind == "rejected_recipes":
        profile.accepted_recipes.discard(normalized)
    dump_yaml(profile_path(data_dir), profile)
    typer.echo(f"Ajouté à {preference_label(kind)}: {normalized}")


def remove_profile_value(kind: str, value: str, data_dir: Path) -> None:
    profile = load_profile(data_dir)
    normalized = normalize_name(value)
    values = getattr(profile, kind)
    values.discard(normalized)
    dump_yaml(profile_path(data_dir), profile)
    typer.echo(f"Retiré de {preference_label(kind)}: {normalized}")


def apply_recipe_feedback_order(recipes: list[Recipe], profile: FoodProfile) -> list[Recipe]:
    accepted = profile.accepted_recipes
    rejected = profile.rejected_recipes
    kept = [recipe for recipe in recipes if normalize_name(recipe.name) not in rejected]
    return sorted(
        kept,
        key=lambda recipe: (
            normalize_name(recipe.name) not in accepted,
            normalize_name(recipe.name),
        ),
    )


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




@profile_app.command("accept-recipe")
def profile_accept_recipe(
    action: Annotated[str, typer.Argument(help="add|remove")],
    value: Annotated[str, typer.Argument()],
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    if action == "add":
        add_profile_value("accepted_recipes", value, data_dir)
        return
    if action == "remove":
        remove_profile_value("accepted_recipes", value, data_dir)
        return
    raise typer.BadParameter("Action attendue : add ou remove.")


@profile_app.command("reject-recipe")
def profile_reject_recipe(
    action: Annotated[str, typer.Argument(help="add|remove")],
    value: Annotated[str, typer.Argument()],
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    if action == "add":
        add_profile_value("rejected_recipes", value, data_dir)
        return
    if action == "remove":
        remove_profile_value("rejected_recipes", value, data_dir)
        return
    raise typer.BadParameter("Action attendue : add ou remove.")


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
    compare_by: Annotated[str, typer.Option("--compare-by")] = "price",
) -> None:
    items = subtract_pantry(recipe_items(load_recipe_file(recipe)), load_pantry(data_dir))
    echo_items("Liste à acheter:", items)
    if prices is None or not items:
        return
    comparison = normalize_compare_by(compare_by)
    recommendation = recommend_basket(
        items,
        load_offers(prices),
        mode=mode,
        max_stores=max_stores,
        compare_by=comparison,
    )
    echo_recommendation(
        items=items,
        recommendation_items=recommendation.by_item,
        mode=recommendation.mode,
        stores=recommendation.stores,
        total=recommendation.total,
        savings_vs_best_single=recommendation.savings_vs_best_single,
        reason=recommendation.reason,
        compare_by=comparison,
    )


@drive_app.command("plan")
def drive_plan(
    shopping_list: Annotated[Path, typer.Argument(help="YAML: items: [{name, quantity, unit}]")],
    drive: Annotated[str, typer.Option("--drive", help="Nom du drive cible")] = "leclerc",
    data_dir: Annotated[
        Path, typer.Option("--data-dir", help="Répertoire données Panier")
    ] = DEFAULT_DATA_DIR,
) -> None:
    data = yaml.safe_load(shopping_list.read_text(encoding="utf-8")) or {}
    items = [ShoppingItem.model_validate(item) for item in data.get("items", [])]
    catalog = load_catalog(data_dir)
    echo_items("Liste drive:", items)
    typer.echo("\nRecherches à lancer:")
    for entry in build_drive_search_plan(items, drive, catalog=catalog):
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
    data_dir: Annotated[
        Path, typer.Option("--data-dir", help="Répertoire données Panier")
    ] = DEFAULT_DATA_DIR,
) -> None:
    data = yaml.safe_load(shopping_list.read_text(encoding="utf-8")) or {}
    items = [ShoppingItem.model_validate(item) for item in data.get("items", [])]
    catalog = load_catalog(data_dir)
    browser = ManagedBrowserClient(
        command=browser_command,
        profile=profile,
        site=site or drive,
    )
    try:
        results = open_drive_searches(items, drive, browser, catalog=catalog)
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
    compare_by: Annotated[str, typer.Option("--compare-by")] = "price",
) -> None:
    data = yaml.safe_load(shopping_list.read_text(encoding="utf-8")) or {}
    items = [ShoppingItem.model_validate(item) for item in data.get("items", [])]
    offers = load_offers(prices)
    comparison = normalize_compare_by(compare_by)
    typer.echo("Meilleurs produits:")
    for item in items:
        chosen = best_offer_for_item(item, offers, compare_by=comparison)
        if chosen is None:
            typer.echo(f"- {format_item(item)}: aucune offre")
            continue
        typer.echo(
            f"- {format_item(item)}: {chosen.offer.product} — {chosen.offer.store} — "
            f"{format_offer_price(chosen.offer, item, comparison)} "
            f"(score {chosen.score:.2f}; {chosen.reason})"
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
    data_dir: Annotated[
        Path, typer.Option("--data-dir", help="Répertoire données Panier")
    ] = DEFAULT_DATA_DIR,
) -> None:
    data = yaml.safe_load(shopping_list.read_text(encoding="utf-8")) or {}
    items = [ShoppingItem.model_validate(item) for item in data.get("items", [])]
    resolved_profile = managed_browser_profile_for_drive(profile, site or drive)
    catalog = load_catalog(data_dir)
    browser = ManagedBrowserClient(
        command=browser_command,
        profile=resolved_profile,
        site=site or drive,
    )
    try:
        offers = _collect_drive_offers_with_optional_catalog(
            items, drive, browser, max_results=max_results, catalog=catalog
        )
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


@recipe_app.command("list")
def recipe_list(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
    tag: Annotated[str | None, typer.Option("--tag", help="Filtrer par tag")] = None,
    include_tags: Annotated[str | None, typer.Option("--include-tags")] = None,
    exclude_tags: Annotated[str | None, typer.Option("--exclude-tags")] = None,
    max_prep_minutes: Annotated[int | None, typer.Option("--max-prep-minutes", min=1)] = None,
    cost_level: Annotated[str | None, typer.Option("--cost-level")] = None,
    min_balance_score: Annotated[
        int | None, typer.Option("--min-balance-score", min=0, max=100)
    ] = None,
    balanced: Annotated[bool, typer.Option("--balanced", help="Recettes équilibrées")]
    = False,
) -> None:
    if balanced and min_balance_score is None:
        min_balance_score = 70
    include = parse_csv_set(include_tags)
    if tag:
        include = (include or set()) | {normalize_name(tag)}
    recipes = filter_recipes(
        load_recipes(data_dir),
        include_tags=include,
        exclude_tags=parse_csv_set(exclude_tags),
        max_prep_minutes=max_prep_minutes,
        cost_level=cost_level,
        min_balance_score=min_balance_score,
    )
    if not recipes:
        typer.echo("Aucune recette")
        return
    for recipe in recipes:
        tags = f" [{', '.join(recipe.tags)}]" if recipe.tags else ""
        prep = f" ({recipe.prep_minutes} min)" if recipe.prep_minutes is not None else ""
        cost = f" {{{recipe.cost_level}}}" if recipe.cost_level else ""
        balance = ""
        if balanced or min_balance_score is not None:
            score = score_recipe_balance(recipe)
            balance = f" — équilibre {score.score}/100 ({score.verdict})"
        typer.echo(f"- {recipe.name}{tags}{prep}{cost}{balance}")


@recipe_app.command("score")
def recipe_score(
    name: Annotated[str, typer.Argument()],
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    recipe = find_recipe(load_recipes(data_dir), name)
    typer.echo(recipe.name)
    typer.echo(format_balance_score(score_recipe_balance(recipe)))


@recipe_app.command("add")
def recipe_add(
    source: Annotated[str, typer.Argument(help="Fichier YAML à importer ou nom de recette")],
    ingredients: Annotated[
        list[str] | None,
        typer.Option(
            "--ingredient",
            "-i",
            help="Ingrédient au format nom[:quantité[:unité]], ex: 'emmental râpé:100:g'",
        ),
    ] = None,
    tags: Annotated[list[str] | None, typer.Option("--tag", "-t")] = None,
    servings: Annotated[int, typer.Option("--servings", min=1)] = 1,
    prep_minutes: Annotated[int | None, typer.Option("--prep-minutes", min=1)] = None,
    cost_level: Annotated[str | None, typer.Option("--cost-level")] = None,
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    source_path = Path(source)
    if source_path.exists():
        new_recipes = read_recipes_file(source_path)
    else:
        parsed_ingredients = [parse_recipe_ingredient(value) for value in ingredients or []]
        if not parsed_ingredients:
            raise typer.BadParameter("Passe un fichier YAML ou ajoute au moins un --ingredient.")
        new_recipes = [
            Recipe(
                name=source,
                servings=servings,
                tags=list(tags or []),
                prep_minutes=prep_minutes,
                cost_level=cost_level,
                ingredients=parsed_ingredients,
            )
        ]

    recipes = load_recipes(data_dir) if recipes_path(data_dir).exists() else []
    existing = {normalize_name(recipe.name) for recipe in recipes}
    for recipe in new_recipes:
        if normalize_name(recipe.name) in existing:
            raise typer.BadParameter(f"Recette déjà présente : {recipe.name}")
        recipes.append(recipe)
        existing.add(normalize_name(recipe.name))
    save_recipes(data_dir, recipes)
    for recipe in new_recipes:
        typer.echo(f"Recette ajoutée : {recipe.name}")


@recipe_app.command("show")
def recipe_show(
    name: Annotated[str, typer.Argument()],
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    recipe = find_recipe(load_recipes(data_dir), name)
    typer.echo(recipe.name)
    typer.echo(f"Portions: {recipe.servings}")
    if recipe.tags:
        typer.echo(f"Tags: {', '.join(recipe.tags)}")
    if recipe.prep_minutes is not None:
        typer.echo(f"Préparation: {recipe.prep_minutes} min")
    if recipe.cost_level:
        typer.echo(f"Coût: {recipe.cost_level}")
    echo_items(
        "Ingrédients:",
        [
            ShoppingItem(name=ingredient.name, quantity=ingredient.quantity, unit=ingredient.unit)
            for ingredient in recipe.ingredients
        ],
    )


@recipe_app.command("remove")
def recipe_remove(
    name: Annotated[str, typer.Argument()],
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    recipes = load_recipes(data_dir)
    normalized_name = normalize_name(name)
    kept = [recipe for recipe in recipes if normalize_name(recipe.name) != normalized_name]
    if len(kept) == len(recipes):
        typer.echo(f"Recette introuvable : {name}")
        return
    save_recipes(data_dir, kept)
    typer.echo(f"Recette supprimée : {name}")


@recipe_app.command("shopping")
def recipe_shopping(
    names: Annotated[list[str], typer.Argument(help="Noms des recettes à consolider")],
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
    prices: Annotated[Path | None, typer.Option("--prices", help="YAML: offers: [...]")] = None,
    mode: Annotated[PriceMode, typer.Option("--mode")] = PriceMode.HYBRID,
    max_stores: Annotated[int, typer.Option("--max-stores", min=1)] = 2,
    compare_by: Annotated[str, typer.Option("--compare-by")] = "price",
) -> None:
    recipes = selected_recipes(data_dir, names)
    pantry = load_pantry_if_exists(data_dir)
    items = shopping_items_for_recipes(recipes, pantry)
    echo_recipe_selection(recipes)
    echo_items("\nListe à acheter:", items)
    if prices is None or not items:
        return
    comparison = normalize_compare_by(compare_by)
    recommendation = recommend_basket(
        items,
        load_offers(prices),
        mode=mode,
        max_stores=max_stores,
        compare_by=comparison,
    )
    echo_recommendation(
        items=items,
        recommendation_items=recommendation.by_item,
        mode=recommendation.mode,
        stores=recommendation.stores,
        total=recommendation.total,
        savings_vs_best_single=recommendation.savings_vs_best_single,
        reason=recommendation.reason,
        compare_by=comparison,
    )


@recipe_app.command("suggest")
def recipe_suggest(
    meals: Annotated[int, typer.Option("--meals", min=1)] = 3,
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
    include_tags: Annotated[str | None, typer.Option("--include-tags")] = None,
    exclude_tags: Annotated[str | None, typer.Option("--exclude-tags")] = None,
    max_prep_minutes: Annotated[int | None, typer.Option("--max-prep-minutes", min=1)] = None,
    cost_level: Annotated[str | None, typer.Option("--cost-level")] = None,
    min_balance_score: Annotated[
        int | None, typer.Option("--min-balance-score", min=0, max=100)
    ] = None,
    balanced: Annotated[bool, typer.Option("--balanced", help="Recettes équilibrées")]
    = False,
) -> None:
    if balanced and min_balance_score is None:
        min_balance_score = 70
    profile_data = load_profile(data_dir)
    selected = select_meals(
        apply_recipe_feedback_order(load_recipes(data_dir), profile_data),
        profile_data,
        meals,
        include_tags=parse_csv_set(include_tags),
        exclude_tags=parse_csv_set(exclude_tags),
        max_prep_minutes=max_prep_minutes,
        cost_level=cost_level,
        min_balance_score=min_balance_score,
    )
    for recipe in selected:
        suffix = ""
        if balanced or min_balance_score is not None:
            score = score_recipe_balance(recipe)
            suffix = f" — équilibre {score.score}/100 ({score.verdict})"
        typer.echo(f"- {recipe.name}{suffix}")


@app.command("plan")
def plan(
    meals: Annotated[int, typer.Option("--meals", min=1)] = 3,
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
    prices: Annotated[Path | None, typer.Option("--prices", help="YAML: offers: [...]")] = None,
    collect: Annotated[
        str | None,
        typer.Option("--collect", help="Drives à collecter, ex: leclerc,auchan"),
    ] = None,
    collect_output: Annotated[
        Path | None,
        typer.Option("--collect-output", help="Sauvegarder les offres collectées"),
    ] = None,
    profile: Annotated[str, typer.Option("--profile", help="Profil Managed Browser")] = "courses",
    browser_command: Annotated[
        str | None,
        typer.Option("--browser-command", help="Commande wrapper Managed Browser"),
    ] = None,
    max_results: Annotated[int, typer.Option("--max-results", min=1)] = 5,
    mode: Annotated[PriceMode, typer.Option("--mode")] = PriceMode.HYBRID,
    max_stores: Annotated[int, typer.Option("--max-stores", min=1)] = 2,
    compare_by: Annotated[str, typer.Option("--compare-by")] = "price",
    use_pantry: Annotated[bool, typer.Option("--use-pantry/--no-pantry")] = True,
    include_tags: Annotated[str | None, typer.Option("--include-tags")] = None,
    exclude_tags: Annotated[str | None, typer.Option("--exclude-tags")] = None,
    max_prep_minutes: Annotated[int | None, typer.Option("--max-prep-minutes", min=1)] = None,
    cost_level: Annotated[str | None, typer.Option("--cost-level")] = None,
    min_balance_score: Annotated[
        int | None, typer.Option("--min-balance-score", min=0, max=100)
    ] = None,
    balanced: Annotated[bool, typer.Option("--balanced", help="Recettes équilibrées")]
    = False,
) -> None:
    if balanced and min_balance_score is None:
        min_balance_score = 70
    profile_data = load_profile(data_dir)
    selected = select_meals(
        apply_recipe_feedback_order(load_recipes(data_dir), profile_data),
        profile_data,
        meals,
        include_tags=parse_csv_set(include_tags),
        exclude_tags=parse_csv_set(exclude_tags),
        max_prep_minutes=max_prep_minutes,
        cost_level=cost_level,
        min_balance_score=min_balance_score,
    )
    items = consolidate_ingredients(selected)
    if use_pantry:
        pantry = load_pantry_if_exists(data_dir)
        if pantry is not None:
            items = subtract_pantry(items, pantry)

    typer.echo("Recettes retenues:")
    for recipe in selected:
        suffix = ""
        if balanced or min_balance_score is not None:
            score = score_recipe_balance(recipe)
            suffix = f" — équilibre {score.score}/100 ({score.verdict})"
        typer.echo(f"- {recipe.name}{suffix}")
    typer.echo("\nÀ acheter:")
    if not items:
        typer.echo("- rien à acheter")
        return
    for item in items:
        typer.echo(f"- {format_item(item)}")

    offers: list[StoreOffer] | None = None
    if collect:
        drives = [drive.strip() for drive in collect.split(",") if drive.strip()]
        offers = collect_offers_for_drives(
            items,
            drives,
            profile=profile,
            browser_command=browser_command,
            max_results=max_results,
            catalog=load_catalog(data_dir),
        )
        if collect_output is not None:
            collect_output.parent.mkdir(parents=True, exist_ok=True)
            collect_output.write_text(
                yaml.safe_dump(
                    {"offers": [offer.model_dump(mode="json") for offer in offers]},
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            typer.echo(f"Offres collectées: {len(offers)} -> {collect_output}")
    elif prices is not None:
        offers = load_offers(prices)

    if offers is None:
        return

    comparison = normalize_compare_by(compare_by)
    recommendation = recommend_basket(
        items,
        offers,
        mode=mode,
        max_stores=max_stores,
        compare_by=comparison,
    )
    echo_recommendation(
        items=items,
        recommendation_items=recommendation.by_item,
        mode=recommendation.mode,
        stores=recommendation.stores,
        total=recommendation.total,
        savings_vs_best_single=recommendation.savings_vs_best_single,
        reason=recommendation.reason,
        compare_by=comparison,
    )


@app.command("week")
def week(
    meals: Annotated[int, typer.Option("--meals", min=1)] = 7,
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
    prices: Annotated[Path | None, typer.Option("--prices", help="YAML: offers: [...]")] = None,
    collect: Annotated[
        str | None,
        typer.Option("--collect", help="Drives à collecter, ex: leclerc,auchan"),
    ] = None,
    collect_output: Annotated[Path | None, typer.Option("--collect-output")] = None,
    profile: Annotated[str, typer.Option("--profile", help="Profil Managed Browser")] = "courses",
    browser_command: Annotated[str | None, typer.Option("--browser-command")] = None,
    max_results: Annotated[int, typer.Option("--max-results", min=1)] = 5,
    mode: Annotated[PriceMode, typer.Option("--mode")] = PriceMode.HYBRID,
    max_stores: Annotated[int, typer.Option("--max-stores", min=1)] = 2,
    compare_by: Annotated[str, typer.Option("--compare-by")] = "unit-price",
    use_pantry: Annotated[bool, typer.Option("--use-pantry/--no-pantry")] = True,
    include_tags: Annotated[str | None, typer.Option("--include-tags")] = None,
    exclude_tags: Annotated[str | None, typer.Option("--exclude-tags")] = None,
    max_prep_minutes: Annotated[int | None, typer.Option("--max-prep-minutes", min=1)] = 45,
    cost_level: Annotated[str | None, typer.Option("--cost-level")] = None,
    balanced: Annotated[bool, typer.Option("--balanced/--no-balanced")] = True,
    min_balance_score: Annotated[
        int | None, typer.Option("--min-balance-score", min=0, max=100)
    ] = None,
) -> None:
    if balanced and min_balance_score is None:
        min_balance_score = 70
    profile_data = load_profile(data_dir)
    selected = select_meals(
        apply_recipe_feedback_order(load_recipes(data_dir), profile_data),
        profile_data,
        meals,
        include_tags=parse_csv_set(include_tags),
        exclude_tags=parse_csv_set(exclude_tags),
        max_prep_minutes=max_prep_minutes,
        cost_level=cost_level,
        min_balance_score=min_balance_score,
    )
    items = consolidate_ingredients(selected)
    if use_pantry:
        pantry = load_pantry_if_exists(data_dir)
        if pantry is not None:
            items = subtract_pantry(items, pantry)

    typer.echo("Semaine:")
    for index, recipe in enumerate(selected, start=1):
        score = score_recipe_balance(recipe)
        typer.echo(f"{index}. {recipe.name} — équilibre {score.score}/100 ({score.verdict})")
    typer.echo("\nÀ acheter:")
    if not items:
        typer.echo("- rien à acheter")
        return
    for item in items:
        typer.echo(f"- {format_item(item)}")

    offers: list[StoreOffer] | None = None
    if collect:
        drives = [drive.strip() for drive in collect.split(",") if drive.strip()]
        offers = collect_offers_for_drives(
            items,
            drives,
            profile=profile,
            browser_command=browser_command,
            max_results=max_results,
            catalog=load_catalog(data_dir),
        )
        if collect_output is not None:
            collect_output.parent.mkdir(parents=True, exist_ok=True)
            collect_output.write_text(
                yaml.safe_dump(
                    {"offers": [offer.model_dump(mode="json") for offer in offers]},
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            typer.echo(f"Offres collectées: {len(offers)} -> {collect_output}")
    elif prices is not None:
        offers = load_offers(prices)

    if offers is None:
        return
    comparison = normalize_compare_by(compare_by)
    try:
        recommendation = recommend_basket(
            items, offers, mode=mode, max_stores=max_stores, compare_by=comparison
        )
    except ValueError as exc:
        typer.echo(f"Recommandation indisponible: {exc}", err=True)
        raise typer.Exit(1) from exc
    echo_recommendation(
        items=items,
        recommendation_items=recommendation.by_item,
        mode=recommendation.mode,
        stores=recommendation.stores,
        total=recommendation.total,
        savings_vs_best_single=recommendation.savings_vs_best_single,
        reason=recommendation.reason,
        compare_by=comparison,
    )


@app.command("compare")
def compare(
    shopping_list: Annotated[Path, typer.Argument(help="YAML: items: [{name, quantity, unit}]")],
    prices: Annotated[Path, typer.Option("--prices", help="YAML: offers: [...]")],
    mode: Annotated[PriceMode, typer.Option("--mode")] = PriceMode.HYBRID,
    max_stores: Annotated[int, typer.Option("--max-stores", min=1)] = 2,
    compare_by: Annotated[str, typer.Option("--compare-by")] = "price",
) -> None:
    list_data = yaml.safe_load(shopping_list.read_text(encoding="utf-8")) or {}
    items = [ShoppingItem.model_validate(item) for item in list_data.get("items", [])]
    comparison = normalize_compare_by(compare_by)
    recommendation = recommend_basket(
        items,
        load_offers(prices),
        mode=mode,
        max_stores=max_stores,
        compare_by=comparison,
    )

    echo_recommendation(
        items=items,
        recommendation_items=recommendation.by_item,
        mode=recommendation.mode,
        stores=recommendation.stores,
        total=recommendation.total,
        savings_vs_best_single=recommendation.savings_vs_best_single,
        reason=recommendation.reason,
        compare_by=comparison,
    )
