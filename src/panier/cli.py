from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
import yaml

from panier import __version__
from panier.brands import (
    BrandPreferenceAction,
    BrandPreferences,
    brand_preferences_path,
    load_brand_preferences,
    save_brand_preferences,
)
from panier.cart import (
    AUCHAN_CART_ADD_EVAL_JS,
    AUCHAN_CART_REMOVE_EVAL_JS,
    CART_ADD_EVAL_JS,
    CART_REMOVE_EVAL_JS,
    CartLine,
    CartRun,
    cart_items_b64_param,
    cart_items_json_param,
    cart_lines_from_recommendation,
    cart_run_path,
    cart_status_expression,
    cart_sync_diff,
    load_cart_run,
    new_cart_run_id,
    save_cart_run,
    store_cart_url,
    store_search_url,
)
from panier.catalog import ProductCatalog, load_catalog
from panier.collector import (
    CollectStatus,
    collect_offers_parallel,
    dedupe_drives,
    managed_browser_profile_for_drive,
    resolve_collect_timeout_seconds,
)
from panier.constraints import (
    BasketConstraints,
    constraints_path,
    load_constraints,
    save_constraints,
)
from panier.deterministic import NO_LLM_ENV_VAR, explain_item, no_llm_status
from panier.drive import (
    best_offer_for_item,
    build_drive_search_plan,
    collect_drive_offers,
    open_drive_searches,
)
from panier.history_store import (
    history_db_path,
    history_stats,
    known_brand_names,
    offers_for_item,
    parse_since,
    price_history_payload,
    promo_candidates,
    record_offers,
    trend_for_item,
)
from panier.household import (
    HouseholdError,
    HouseholdProfile,
    active_household_name,
    household_path,
    list_households,
    load_active_household,
    load_household,
    resolve_profile_file,
    save_household,
    set_active_household,
    slugify,
)
from panier.managed_browser import BrowserCommandResult, ManagedBrowserClient, ManagedBrowserError
from panier.models import (
    FoodProfile,
    Ingredient,
    Pantry,
    PreferenceConfidence,
    PreferenceDetail,
    PreferenceStatus,
    PriceMode,
    Recipe,
    ShoppingItem,
    StoreOffer,
    dump_yaml,
    load_yaml_model,
    normalize_name,
)
from panier.nutrition import BalanceScore, score_recipe_balance
from panier.offers_cache import (
    OffersCacheEntry,
    cache_report_line,
    entry_age_seconds,
    load_cached_entry,
    offers_cache_dir,
    offers_cache_path,
    resolve_cache_ttl_hours,
    save_offers_cache,
)
from panier.planner import (
    WEEK_SLOT_LABELS,
    CompareBy,
    build_week_plan,
    canonical_week_slots,
    compare_basket_options,
    consolidate_ingredients,
    consume_pantry,
    filter_recipes,
    low_stock_items,
    rank_recipes,
    recommend_basket,
    select_meals,
    subtract_pantry,
)
from panier.price_cache import add_offers_to_cache, load_price_cache, price_cache_path
from panier.recipe_cost import RecipeCost, compute_recipe_cost, recipe_cost_sort_key
from panier.substitutions import (
    SubstitutionCatalog,
    load_substitutions,
    save_substitutions,
    substitute_offers_for_requested_items,
    substitutions_path,
)

app = typer.Typer(
    help="Planifie repas et courses optimisées multi-drive.",
    invoke_without_command=True,
)

OutputFormat = Annotated[str, typer.Option("--format", help="Format de sortie: text ou json")]
profile_app = typer.Typer(help="Gérer le profil alimentaire.")
recipe_app = typer.Typer(help="Gérer et suggérer des recettes.")
pantry_app = typer.Typer(help="Gérer le stock local.")
shopping_app = typer.Typer(help="Générer des listes de courses.")
drive_app = typer.Typer(help="Préparer les recherches et paniers drive.")
llm_app = typer.Typer(help="État et garde-fous LLM.")
explain_app = typer.Typer(help="Expliquer les choix déterministes locaux.")
brand_app = typer.Typer(help="Gérer les préférences de marques déterministes.")
cache_app = typer.Typer(help="Gérer le cache local des prix/offres.")
substitution_app = typer.Typer(help="Gérer les substitutions déterministes d'articles.")
constraint_app = typer.Typer(help="Gérer les contraintes panier déterministes.")
doctor_app = typer.Typer(help="Diagnostiquer la configuration déterministe locale.")
cart_app = typer.Typer(help="Relire, synchroniser et appliquer des runs panier drive.")
history_app = typer.Typer(help="Historique local des prix collectés (informatif).")
app.add_typer(profile_app, name="profile")
app.add_typer(recipe_app, name="recipe")
app.add_typer(pantry_app, name="pantry")
app.add_typer(shopping_app, name="shopping")
app.add_typer(drive_app, name="drive")
app.add_typer(llm_app, name="llm")
app.add_typer(explain_app, name="explain")
app.add_typer(brand_app, name="brand")
app.add_typer(cache_app, name="cache")
app.add_typer(substitution_app, name="substitution")
app.add_typer(constraint_app, name="constraint")
app.add_typer(doctor_app, name="doctor")
app.add_typer(cart_app, name="cart")
app.add_typer(history_app, name="history")

DEFAULT_DATA_DIR = Path.home() / ".panier"


def profile_path(data_dir: Path) -> Path:
    return data_dir / "profile.yaml"


def recipes_path(data_dir: Path) -> Path:
    return data_dir / "recipes.yaml"


def pantry_path(data_dir: Path) -> Path:
    return data_dir / "pantry.yaml"


def load_profile(data_dir: Path) -> FoodProfile:
    """Préférences effectives : foyer actif si présent, sinon profile.yaml."""
    active = load_active_household(data_dir)
    if active is not None:
        return active.preferences
    path = profile_path(data_dir)
    if not path.exists():
        return FoodProfile()
    return load_yaml_model(path, FoodProfile)


def save_effective_profile(data_dir: Path, profile: FoodProfile) -> Path:
    """Écrit les préférences là où elles seront relues par load_profile."""
    active = load_active_household(data_dir)
    if active is not None:
        active.preferences = profile
        return save_household(data_dir, active)
    path = profile_path(data_dir)
    dump_yaml(path, profile)
    return path


def load_household_preferences(data_dir: Path) -> HouseholdProfile | None:
    return load_active_household(data_dir)


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


def read_shopping_items(path: Path) -> list[ShoppingItem]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [ShoppingItem.model_validate(item) for item in data.get("items", [])]


def offers_for_requested_items(
    items: list[ShoppingItem], offers: list[StoreOffer]
) -> list[StoreOffer]:
    requested = {item.name for item in items}
    return [offer for offer in offers if offer.item in requested]


def prepare_items_and_offers(
    items: list[ShoppingItem],
    offers: list[StoreOffer],
    data_dir: Path,
) -> tuple[list[ShoppingItem], list[StoreOffer], BasketConstraints]:
    substitutions = load_substitutions(data_dir)
    constraints = load_constraints(data_dir)
    household = load_active_household(data_dir)
    if household is not None:
        disabled = household.disabled_stores()
        blocked = {normalize_name(s) for s in constraints.blocked_stores} | set(disabled)
        constraints = constraints.model_copy(
            update={"blocked_stores": sorted(blocked)}
        )
    expanded_offers = substitute_offers_for_requested_items(items, offers, substitutions)
    filtered_offers = apply_store_constraints(expanded_offers, constraints)
    return items, filtered_offers, constraints


def apply_store_constraints(
    offers: list[StoreOffer], constraints: BasketConstraints
) -> list[StoreOffer]:
    blocked = {normalize_name(store) for store in constraints.blocked_stores}
    if not blocked:
        return offers
    return [offer for offer in offers if normalize_name(offer.store) not in blocked]


def validate_recommendation_constraints(
    total: float, item_count: int, constraints: BasketConstraints
) -> list[str]:
    issues: list[str] = []
    if constraints.max_total_eur is not None and total > constraints.max_total_eur:
        issues.append(f"total {total:.2f} € > budget {constraints.max_total_eur:.2f} €")
    if constraints.min_items is not None and item_count < constraints.min_items:
        issues.append(f"{item_count} articles < minimum {constraints.min_items}")
    if constraints.max_items is not None and item_count > constraints.max_items:
        issues.append(f"{item_count} articles > maximum {constraints.max_items}")
    return issues


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


def cart_flow_name_for_drive(drive: str) -> str:
    normalized = normalize_name(drive)
    if normalized in {"auchan", "leclerc"}:
        return f"add-cart-{normalized}"
    raise typer.BadParameter(f"Drive non supporté pour ajout panier : {drive}")


def cart_remove_flow_name_for_drive(drive: str) -> str:
    normalized = normalize_name(drive)
    if normalized in {"auchan", "leclerc"}:
        return f"remove-cart-{normalized}"
    raise typer.BadParameter(f"Drive non supporté pour suppression panier : {drive}")


def _cart_add_expression(line: CartLine, *, dry_run: bool) -> str:
    payload = {
        "item": line.item,
        "product": line.product,
        "quantity": line.quantity,
        "dryRun": dry_run,
    }
    return f"({CART_ADD_EVAL_JS})({json.dumps(payload, ensure_ascii=False)})"


def _cart_remove_expression(store: str, line: CartLine, *, dry_run: bool) -> str:
    payload = {
        "item": line.item,
        "product": line.product,
        "quantity": line.quantity,
        "dryRun": dry_run,
    }
    if normalize_name(store) == "auchan":
        return f"({AUCHAN_CART_REMOVE_EVAL_JS})({json.dumps(payload, ensure_ascii=False)})"
    return f"({CART_REMOVE_EVAL_JS})({json.dumps(payload, ensure_ascii=False)})"


def _flow_payload_from_line_results(
    store: str, lines: list[CartLine], line_results: list[dict], *, dry_run: bool
) -> dict:
    catalog_found = []
    addable = []
    inserted = []
    for line, result in zip(lines, line_results, strict=False):
        entry = {
            "item": line.item,
            "product": line.product,
            "url": result.get("url") or line.url or line.search_url,
            "button_label": result.get("button_label") or "",
        }
        if result.get("blocked_by"):
            entry["blocked_by"] = result.get("blocked_by")
        if result.get("error"):
            entry["error"] = result.get("error")
        if result.get("catalog_found"):
            catalog_found.append({**entry, "status": "catalog_found"})
        if result.get("addable"):
            addable.append({**entry, "status": "addable"})
        if result.get("inserted"):
            inserted.append({**entry, "status": "inserted"})
    return {
        "ok": True,
        "store": store,
        "dryRun": dry_run,
        "catalog_found": catalog_found,
        "addable": addable,
        "inserted": inserted,
        "line_results": line_results,
        "requires_live_flow": False,
        "message": (
            f"Dry-run {store}: pages produit/recherche inspectées, aucun clic panier exécuté."
            if dry_run
            else (
                f"Live {store}: clics Ajouter au panier exécutés pour les "
                "produits ajoutables; aucun paiement/commande."
            )
        ),
    }


def run_cart_flow_for_store(
    store: str,
    lines: list[CartLine],
    *,
    profile: str,
    browser_command: str | None,
    dry_run: bool,
) -> BrowserCommandResult:
    browser = ManagedBrowserClient(
        command=browser_command,
        profile=managed_browser_profile_for_drive(profile, store),
        site=store,
    )
    if dry_run:
        return browser.flow_run(
            cart_flow_name_for_drive(store),
            params={
                "itemsB64": cart_items_b64_param(lines),
                "itemsJson": cart_items_json_param(lines),
                "dryRunB64": "dHJ1ZQ==",
            },
            max_side_effect_level="read_only",
        )

    line_results: list[dict] = []
    if not lines:
        return BrowserCommandResult(
            action="flow",
            data=_flow_payload_from_line_results(store, lines, [], dry_run=False),
        )
    browser.checkpoint(f"before-live-cart-{store}")
    for line in lines:
        target_urls = [url for url in [line.url, line.search_url] if url]
        if not target_urls:
            line_results.append(
                {
                    "item": line.item,
                    "product": line.product,
                    "catalog_found": False,
                    "addable": False,
                    "inserted": False,
                    "error": "aucune URL produit/recherche disponible",
                }
            )
            continue
        fallback_result: dict | None = None
        for target_url in target_urls:
            browser.navigate(target_url)
            if normalize_name(store) == "auchan":
                payload = {
                    "item": line.item,
                    "product": line.product,
                    "quantity": line.quantity,
                    "dryRun": False,
                }
                expression = (
                    f"({AUCHAN_CART_ADD_EVAL_JS})({json.dumps(payload, ensure_ascii=False)})"
                )
            else:
                expression = _cart_add_expression(line, dry_run=False)
            raw_result = browser.console_eval(expression).data
            value = raw_result.get("result", {}).get("value", raw_result)
            line_result = value if isinstance(value, dict) else {"raw": value}
            if (
                line_result.get("catalog_found")
                or line_result.get("addable")
                or line_result.get("inserted")
            ):
                line_results.append(line_result)
                break
            fallback_result = line_result
        else:
            line_results.append(fallback_result or {"error": "aucun résultat navigateur"})
    return BrowserCommandResult(
        action="flow",
        data=_flow_payload_from_line_results(store, lines, line_results, dry_run=False),
    )


def _remove_payload_from_line_results(
    store: str, lines: list[CartLine], line_results: list[dict], *, dry_run: bool
) -> dict:
    found = []
    removable = []
    removed = []
    for line, result in zip(lines, line_results, strict=False):
        entry = {
            "item": line.item,
            "product": line.product,
            "url": result.get("url") or line.url or line.search_url,
            "button_label": result.get("button_label") or "",
        }
        if result.get("error"):
            entry["error"] = result.get("error")
        if result.get("catalog_found"):
            found.append({**entry, "status": "catalog_found"})
        if result.get("removable"):
            removable.append({**entry, "status": "removable"})
        if result.get("removed"):
            removed.append({**entry, "status": "removed"})
    return {
        "ok": True,
        "store": store,
        "dryRun": dry_run,
        "catalog_found": found,
        "removable": removable,
        "removed": removed,
        "line_results": line_results,
        "message": (
            f"Dry-run {store}: lignes panier inspectées, aucun retrait exécuté."
            if dry_run
            else f"Live {store}: suppressions panier exécutées; aucun paiement/commande."
        ),
    }


def run_cart_remove_flow_for_store(
    store: str,
    lines: list[CartLine],
    *,
    profile: str,
    browser_command: str | None,
    dry_run: bool,
) -> BrowserCommandResult:
    browser = ManagedBrowserClient(
        command=browser_command,
        profile=managed_browser_profile_for_drive(profile, store),
        site=store,
    )
    if dry_run:
        return browser.flow_run(
            cart_remove_flow_name_for_drive(store),
            params={
                "itemsB64": cart_items_b64_param(lines),
                "itemsJson": cart_items_json_param(lines),
                "dryRunB64": "dHJ1ZQ==",
            },
            max_side_effect_level="read_only",
        )
    line_results: list[dict] = []
    if not lines:
        return BrowserCommandResult(
            action="flow",
            data=_remove_payload_from_line_results(store, lines, [], dry_run=False),
        )
    browser.checkpoint(f"before-live-cart-{store}-remove")
    for line in lines:
        target_urls = [url for url in [line.url, line.search_url] if url]
        if not target_urls:
            line_results.append(
                {
                    "item": line.item,
                    "product": line.product,
                    "catalog_found": False,
                    "removable": False,
                    "removed": False,
                    "error": "aucune URL produit/recherche disponible",
                }
            )
            continue
        fallback_result: dict | None = None
        for target_url in target_urls:
            browser.navigate(target_url)
            raw_result = browser.console_eval(
                _cart_remove_expression(store, line, dry_run=False)
            ).data
            value = raw_result.get("result", {}).get("value", raw_result)
            line_result = value if isinstance(value, dict) else {"raw": value}
            if (
                line_result.get("catalog_found")
                or line_result.get("removable")
                or line_result.get("removed")
            ):
                line_results.append(line_result)
                break
            fallback_result = line_result
        else:
            line_results.append(fallback_result or {"error": "aucun résultat navigateur"})
    return BrowserCommandResult(
        action="flow",
        data=_remove_payload_from_line_results(store, lines, line_results, dry_run=False),
    )


def echo_cart_plan(grouped_lines: dict[str, list[CartLine]], *, action: str = "add") -> None:
    if action == "remove":
        title = "\nPaniers à retirer:"
        note = "offre collectée; présence/retrait panier à vérifier"
    else:
        title = "\nPaniers à préparer:"
        note = "offre collectée; disponibilité/ajout panier à vérifier"
    typer.echo(title)
    for store, lines in grouped_lines.items():
        typer.echo(f"- {store}:")
        for line in lines:
            typer.echo(f"  - {line.product} x{line.quantity} ({line.item}) — {note}")


def cart_flow_value(result: BrowserCommandResult) -> dict:
    """Extrait le payload métier retourné par un flow Managed Browser."""
    payload = result.data
    nested = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(nested, dict):
        return payload if isinstance(payload, dict) else {}
    steps = nested.get("results")
    if isinstance(steps, list) and steps:
        last = steps[-1]
        if isinstance(last, dict):
            step_result = last.get("result")
            if isinstance(step_result, dict) and isinstance(step_result.get("value"), dict):
                return step_result["value"]
    return nested


def _cart_result_counts(value: dict, action: str) -> dict[str, int]:
    catalog_found = (
        value.get("catalog_found") if isinstance(value.get("catalog_found"), list) else []
    )
    if action == "remove":
        available = value.get("removable") if isinstance(value.get("removable"), list) else []
        done = value.get("removed") if isinstance(value.get("removed"), list) else []
    else:
        available = value.get("addable") if isinstance(value.get("addable"), list) else []
        done = value.get("inserted") if isinstance(value.get("inserted"), list) else []
    return {"catalog_found": len(catalog_found), "available": len(available), "done": len(done)}


def echo_cart_flow_result(
    store: str, result: BrowserCommandResult, *, dry_run: bool, action: str = "add"
) -> None:
    value = cart_flow_value(result)
    suffix = "dry-run" if dry_run else "live"
    catalog_found = (
        value.get("catalog_found") if isinstance(value.get("catalog_found"), list) else []
    )
    if action == "remove":
        available = value.get("removable") if isinstance(value.get("removable"), list) else []
        done = value.get("removed") if isinstance(value.get("removed"), list) else []
        available_label = "Produits retirables/disponibles"
        done_label = "Produits effectivement retirés"
    else:
        available = value.get("addable") if isinstance(value.get("addable"), list) else []
        done = value.get("inserted") if isinstance(value.get("inserted"), list) else []
        available_label = "Produits ajoutables/disponibles"
        done_label = "Produits effectivement insérés"
    typer.echo(f"Flow panier {store} exécuté ({suffix}).")
    typer.echo(f"  Produits trouvés/catalogue: {len(catalog_found)}")
    typer.echo(f"  {available_label}: {len(available)}")
    typer.echo(f"  {done_label}: {len(done)}")
    message = value.get("message")
    if message:
        typer.echo(f"  Note: {message}")
    line_results = value.get("line_results") if isinstance(value.get("line_results"), list) else []
    blocked = [
        entry for entry in line_results if isinstance(entry, dict) and entry.get("blocked_by")
    ]
    for entry in blocked:
        target = entry.get("item") or entry.get("product")
        reason = entry.get("error") or entry.get("blocked_by")
        typer.echo(f"  Blocage {target}: {reason}", err=True)
    if value.get("requires_live_flow"):
        typer.echo(
            "  Attention: le clic réel d'ajout panier n'est pas encore "
            "appris/validé pour ce drive.",
            err=True,
        )


def _persist_cart_run(
    data_dir: Path,
    *,
    action: str,
    dry_run: bool,
    grouped_lines: dict[str, list[CartLine]],
    results: dict[str, dict],
) -> Path:
    created_at = datetime.now(UTC).isoformat(timespec="seconds")
    run = CartRun(
        id=new_cart_run_id(),
        action=action,
        dry_run=dry_run,
        grouped_lines=grouped_lines,
        results=results,
        created_at=created_at,
    )
    return save_cart_run(data_dir, run)


def _cart_run_results_summary(run: CartRun) -> dict[str, int]:
    totals = {"stores": len(run.grouped_lines), "catalog_found": 0, "available": 0, "done": 0}
    for value in run.results.values():
        counts = _cart_result_counts(value, run.action)
        totals["catalog_found"] += counts["catalog_found"]
        totals["available"] += counts["available"]
        totals["done"] += counts["done"]
    return totals


def _run_cart_action(
    action: str,
    grouped_lines: dict[str, list[CartLine]],
    *,
    profile: str,
    browser_command: str | None,
    dry_run: bool,
) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for store, lines in grouped_lines.items():
        if action == "remove":
            result = run_cart_remove_flow_for_store(
                store,
                lines,
                profile=profile,
                browser_command=browser_command,
                dry_run=dry_run,
            )
        else:
            result = run_cart_flow_for_store(
                store,
                lines,
                profile=profile,
                browser_command=browser_command,
                dry_run=dry_run,
            )
        echo_cart_flow_result(store, result, dry_run=dry_run, action=action)
        results[store] = cart_flow_value(result)
    return results


def run_cart_status_for_store(
    store: str,
    lines: list[CartLine],
    *,
    profile: str,
    browser_command: str | None,
) -> dict:
    browser = ManagedBrowserClient(
        command=browser_command,
        profile=managed_browser_profile_for_drive(profile, store),
        site=store,
    )
    cart_url = store_cart_url(store)
    if not cart_url:
        raise typer.BadParameter(f"Drive non supporté pour lecture panier : {store}")
    browser.navigate(cart_url)
    raw_result = browser.console_eval(cart_status_expression(store, lines)).data
    value = raw_result.get("result", {}).get("value", raw_result)
    return value if isinstance(value, dict) else {"raw": value}


def _echo_cart_status(store: str, status: dict) -> None:
    counts = status.get("counts") if isinstance(status.get("counts"), dict) else {}
    typer.echo(f"État panier {store} (read-only).")
    typer.echo(f"  URL: {status.get('url', '')}")
    typer.echo(f"  Attendus: {counts.get('expected', 0)}")
    typer.echo(f"  Candidats panier: {counts.get('actual_candidates', 0)}")
    typer.echo(f"  Matchés: {counts.get('matched', 0)}")
    if status.get("blocked_by"):
        typer.echo(f"  Blocage: {status.get('blocked_by')}", err=True)


def _echo_cart_sync_diff(store: str, diff: dict) -> None:
    summary = diff.get("summary") if isinstance(diff.get("summary"), dict) else {}
    typer.echo(f"Diff sync panier {store} (dry-run).")
    typer.echo(f"  À garder: {summary.get('unchanged_count', 0)}")
    typer.echo(f"  À ajouter: {summary.get('to_add_count', 0)}")
    typer.echo(f"  À retirer: {summary.get('to_remove_count', 0)}")
    typer.echo(f"  Quantités à corriger: {summary.get('to_update_quantity_count', 0)}")
    typer.echo(f"  Ambigus: {summary.get('ambiguous_count', 0)}")


def _store_worker(
    items: list[ShoppingItem],
    drives_profile: str,
    browser_command: str | None,
    max_results: int,
    catalog: ProductCatalog | None,
    bucket: dict[str, list[StoreOffer]] | None = None,
) -> Callable[[str], list[StoreOffer]]:
    """Worker d'un store : client isolé (profil par store) + collecte séquentielle.

    Si `bucket` est fourni, les offres du store y sont déposées (dict thread-safe
    pour une écriture par clé unique) afin de ventiler le résultat agrégé.
    """

    def worker(drive: str) -> list[StoreOffer]:
        resolved_profile = managed_browser_profile_for_drive(drives_profile, drive)
        browser = ManagedBrowserClient(
            command=browser_command, profile=resolved_profile, site=drive
        )
        offers = _collect_drive_offers_with_optional_catalog(
            items, drive, browser, max_results=max_results, catalog=catalog
        )
        if bucket is not None:
            bucket[drive] = list(offers)
        return list(offers)

    return worker


def _echo_collect_status(
    drive: str,
    status: CollectStatus,
    offer_count: int,
    error: str | None,
    timeout_seconds: float,
) -> None:
    if status == "ok":
        typer.echo(f"Collecte {drive}: {offer_count} offres")
        return
    if status == "timeout":
        typer.echo(
            f"Avertissement timeout {drive}: cycle de collecte > {timeout_seconds:g}s; "
            "collecte ignorée pour ce drive.",
            err=True,
        )
        return
    typer.echo(
        f"Avertissement Managed Browser {drive}: {error}; collecte ignorée pour ce drive.",
        err=True,
    )


def _record_history(data_dir: Path, drive: str, collected: list[StoreOffer]) -> None:
    """Alimente l'historique SQLite ; un échec local n'interrompt jamais la collecte."""
    try:
        record_offers(data_dir, drive, collected, known_brands=known_brand_names(data_dir))
    except Exception as exc:
        typer.echo(f"Avertissement historique {drive}: {exc}", err=True)


def echo_price_history_block(
    recommendation_items: dict[str, StoreOffer],
    data_dir: Path,
    *,
    enabled: bool,
) -> None:
    """Bloc strictement informatif : n'influence jamais la recommandation."""
    if not enabled or not history_db_path(data_dir).exists():
        return
    block = price_history_payload(
        data_dir, [offer.item for offer in recommendation_items.values()]
    )
    if not block:
        return
    typer.echo("\nHistorique prix (informatif):")
    for name in sorted(block):
        info = block[name]
        delta = (
            f"{info['delta_pct']:+.1f} %" if info["delta_pct"] is not None else "n/a"
        )
        promo = "candidat promo" if info["is_promo_candidate"] else "pas une promo"
        typer.echo(
            f"- {name}: dernier {info['last_price']:.2f} € ({delta} vs moyenne; "
            f"{info['observations']} obs.) — {promo}"
        )


def _enrich_collect_output_with_history(
    output: Path,
    recommendation_items: dict[str, StoreOffer],
    data_dir: Path,
) -> None:
    """Ajoute le bloc price_history au payload déjà écrit (--collect-output)."""
    try:
        payload = yaml.safe_load(output.read_text(encoding="utf-8")) or {}
        block = price_history_payload(
            data_dir, [offer.item for offer in recommendation_items.values()]
        )
        if block:
            payload["price_history"] = block
            output.write_text(
                yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
    except Exception as exc:
        typer.echo(f"Avertissement historique (payload): {exc}", err=True)


def collect_offers_for_drives(
    items: list[ShoppingItem],
    drives: list[str],
    *,
    profile: str,
    browser_command: str | None,
    max_results: int,
    catalog: ProductCatalog | None = None,
) -> tuple[list[StoreOffer], bool]:
    """Collecte parallélisée entre stores (un worker par store), sans cache."""
    unique_drives = dedupe_drives(drives)
    timeout_seconds = resolve_collect_timeout_seconds()
    bucket: dict[str, list[StoreOffer]] = {}
    worker = _store_worker(items, profile, browser_command, max_results, catalog, bucket)
    _, statuses, errors = collect_offers_parallel(unique_drives, worker)
    aggregated: list[StoreOffer] = []
    for drive in unique_drives:
        status = statuses.get(drive, "timeout")
        offers_of_drive = bucket.get(drive, [])
        aggregated.extend(offers_of_drive)
        _echo_collect_status(
            drive, status, len(offers_of_drive), errors.get(drive), timeout_seconds
        )
    return aggregated, any(status != "ok" for status in statuses.values())


def collect_offers_with_cache(
    items: list[ShoppingItem],
    drives: list[str],
    *,
    profile: str,
    browser_command: str | None,
    max_results: int,
    catalog: ProductCatalog | None,
    data_dir: Path | None = None,
    use_cache: bool = True,
    ttl_hours: float | None = None,
    timeout_seconds: float | None = None,
) -> tuple[list[StoreOffer], bool, dict[str, dict]]:
    """Cycle complet : cache offres (TTL) puis collecte parallèle des manquants.

    - cache hit frais : zéro requête réseau pour ce store ;
    - entrée stale : tentative de recollecte, repli sur l'entrée stale si échec ;
    - chaque collecte réussie (même partielle) réécrit le cache du store.

    Retourne (offres agrégées dans l'ordre demandé, échec présent, rapport
    par store {source, count, age_s, stale}).
    """
    unique_drives = dedupe_drives(drives)
    effective_ttl = resolve_cache_ttl_hours(ttl_hours)
    effective_timeout = resolve_collect_timeout_seconds(timeout_seconds)
    now = datetime.now(UTC)
    report: dict[str, dict] = {}
    fresh_cache_offers: dict[str, list[StoreOffer]] = {}
    stale_entries: dict[str, OffersCacheEntry] = {}
    pending: list[str] = []

    for drive in unique_drives:
        entry = (
            load_cached_entry(offers_cache_path(data_dir, drive, items))
            if use_cache and data_dir is not None
            else None
        )
        if entry is None or not entry.offers:
            pending.append(drive)
            continue
        age_s = entry_age_seconds(entry, now=now)
        if age_s < effective_ttl * 3600:
            fresh_cache_offers[drive] = list(entry.offers)
            report[drive] = {
                "source": "cache",
                "count": len(entry.offers),
                "age_s": age_s,
                "stale": False,
            }
        else:
            stale_entries[drive] = entry
            pending.append(drive)

    bucket: dict[str, list[StoreOffer]] = {}
    worker = _store_worker(items, profile, browser_command, max_results, catalog, bucket)
    _, statuses, errors = collect_offers_parallel(
        pending, worker, timeout_seconds=timeout_seconds
    )

    aggregated: list[StoreOffer] = []
    had_failure = False
    for drive in unique_drives:
        if drive in fresh_cache_offers:
            cached = fresh_cache_offers[drive]
            aggregated.extend(cached)
            info = report[drive]
            typer.echo(cache_report_line(drive, info["count"], age_s=info["age_s"], stale=False))
            continue
        status = statuses.get(drive, "timeout")
        collected = bucket.get(drive, [])
        if status == "ok":
            aggregated.extend(collected)
            typer.echo(f"Collecte {drive}: {len(collected)} offres")
            if data_dir is not None and use_cache and collected:
                save_offers_cache(
                    data_dir, drive, items, collected, ttl_hours=effective_ttl
                )
                report[drive] = {
                    "source": "collected",
                    "count": len(collected),
                    "age_s": 0.0,
                    "stale": False,
                }
            else:
                report[drive] = {
                    "source": "collected",
                    "count": len(collected),
                    "age_s": None,
                    "stale": False,
                }
            if data_dir is not None and collected:
                _record_history(data_dir, drive, collected)
            continue

        had_failure = True
        entry = stale_entries.get(drive)
        if entry is not None and entry.offers:
            entry_offers = list(entry.offers)
            age_s = entry_age_seconds(entry, now=now)
            typer.echo(
                f"Avertissement {drive}: recollecte impossible ({errors.get(drive)}); "
                "usage des offres en cache au-delà du TTL.",
                err=True,
            )
            aggregated.extend(entry_offers)
            report[drive] = {
                "source": "cache_stale",
                "count": len(entry_offers),
                "age_s": age_s,
                "stale": True,
            }
            typer.echo(cache_report_line(drive, len(entry_offers), age_s=age_s, stale=True))
            continue
        report[drive] = {"source": str(status), "count": 0, "age_s": None, "stale": False}
        _echo_collect_status(drive, status, 0, errors.get(drive), effective_timeout)

    return aggregated, had_failure, report


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
        return collect_drive_offers(items, drive, browser, max_results=max_results, catalog=catalog)
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
    return f"Équilibre: {score.score}/100 ({score.verdict})\n+ {positives}\n- {penalties}"


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


def echo_basket_options(
    items: list[ShoppingItem],
    offers: list[StoreOffer],
    *,
    max_stores: int,
    compare_by: CompareBy,
    brand_preferences: BrandPreferences | None = None,
) -> None:
    options = compare_basket_options(
        items,
        offers,
        max_stores=max_stores,
        compare_by=compare_by,
        brand_preferences=brand_preferences,
    )
    if not options:
        return

    typer.echo("\nComparatif paniers:")
    for option in options:
        label = " + ".join(option.stores)
        if len(option.stores) == 1:
            label = f"Tout {label}"
        else:
            label = f"Hybride {label}"
        if option.total is None:
            partial_total = sum(float(offer.price) for offer in option.by_item.values())
            typer.echo(
                f"- {label}: incomplet ({partial_total:.2f} € partiel; manque: "
                f"{', '.join(option.missing_items)})"
            )
        else:
            typer.echo(f"- {label}: {option.total:.2f} €")


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
    if len(stores) > 1:
        typer.echo("Type: panier hybride — commander dans plusieurs drives")
    else:
        typer.echo("Type: panier simple — commander dans un seul drive")
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


def explain_offer_lines(
    item: ShoppingItem,
    offer: StoreOffer,
    *,
    compare_by: CompareBy,
    brand_preferences: BrandPreferences,
    substitutions: SubstitutionCatalog,
) -> list[str]:
    chosen = best_offer_for_item(item, [offer], compare_by=compare_by)
    brand_action = brand_preferences.action_for_offer(offer)
    substitute_source = next(
        (
            rule.item
            for rule in substitutions.rules
            if normalize_name(offer.item) in rule.substitutes
            and normalize_name(item.name) == rule.item
        ),
        None,
    )
    lines = [
        f"Article demandé: {format_item(item)}",
        f"Offre: {offer.product}",
        f"Drive: {offer.store}",
        f"Prix: {format_offer_price(offer, item, compare_by)}",
        f"Confiance: {offer.confidence}",
    ]
    if chosen is not None:
        lines.append(f"Score matching: {chosen.score:.2f} ({chosen.reason})")
    lines.append(f"Préférence marque: {brand_action.value}")
    if substitute_source:
        lines.append(f"Substitution: {substitute_source} -> {offer.item}")
    else:
        lines.append("Substitution: non")
    lines.append("Décision: déterministe locale, aucun appel LLM")
    return lines


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


def normalize_output_format(value: str) -> str:
    normalized = normalize_name(value)
    if normalized not in {"text", "json"}:
        raise typer.BadParameter("format doit être 'text' ou 'json'")
    return normalized


def echo_json(payload: dict) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


_cli_no_llm = False


def _history_since(value: str | None) -> datetime | None:
    try:
        return parse_since(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


@history_app.command("show")
def history_show(
    ingredient: Annotated[str, typer.Argument(help="Nom canonique d'ingrédient")],
    store: Annotated[str | None, typer.Option("--store")] = None,
    since: Annotated[str | None, typer.Option("--since", help="Ex: 7d, 24h, date ISO")] = None,
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
    output_format: OutputFormat = "text",
) -> None:
    cutoff = _history_since(since)
    points = offers_for_item(data_dir, ingredient, store=store, since=cutoff)
    if normalize_output_format(output_format) == "json":
        echo_json(
            {
                "ingredient": normalize_name(ingredient),
                "points": [point.__dict__ for point in points],
            }
        )
        return
    if not points:
        typer.echo(f"Aucun historique pour {normalize_name(ingredient)}")
        return
    for point in points:
        price = f"{point.price:.2f} €"
        unit_price = f" ({point.unit_price:.2f} €/kg-L)" if point.unit_price else ""
        brand = f" [{point.brand}]" if point.brand else ""
        typer.echo(
            f"- {point.collected_at} {point.store}: {point.product_title}{brand}"
            f" — {price}{unit_price}"
        )


@history_app.command("trend")
def history_trend(
    ingredient: Annotated[str, typer.Argument(help="Nom canonique d'ingrédient")],
    store: Annotated[str | None, typer.Option("--store")] = None,
    since: Annotated[str | None, typer.Option("--since", help="Ex: 7d, 24h, date ISO")] = None,
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
    output_format: OutputFormat = "text",
) -> None:
    cutoff = _history_since(since)
    trend = trend_for_item(data_dir, ingredient, store=store, since=cutoff)
    if trend is None:
        typer.echo(f"Aucun historique pour {normalize_name(ingredient)}")
        return
    if normalize_output_format(output_format) == "json":
        echo_json(
            {
                "canonical_name": trend.canonical_name,
                "store": trend.store,
                "observations": trend.observations,
                "min_price": trend.min_price,
                "mean_price": round(trend.mean_price, 4),
                "median_price": round(trend.median_price, 4),
                "last_price": trend.last_price,
                "last_collected_at": trend.last_collected_at,
                "delta_pct": (
                    round(trend.delta_pct, 2) if trend.delta_pct is not None else None
                ),
                "is_promo_candidate": trend.is_promo_candidate,
            }
        )
        return
    typer.echo(f"Tendance {trend.canonical_name} ({trend.store}):")
    typer.echo(f"  Observations: {trend.observations}")
    typer.echo(f"  Min: {trend.min_price:.2f} €")
    typer.echo(f"  Moyenne: {trend.mean_price:.2f} €")
    typer.echo(f"  Médiane: {trend.median_price:.2f} €")
    typer.echo(f"  Dernier: {trend.last_price:.2f} € ({trend.last_collected_at})")
    if trend.delta_pct is not None:
        typer.echo(f"  Variation dernier vs moyenne: {trend.delta_pct:+.1f} %")
    verdict = "candidat promo" if trend.is_promo_candidate else "pas une promo"
    typer.echo(f"  Candidat promo: {'oui' if trend.is_promo_candidate else 'non'} ({verdict})")


@history_app.command("promos")
def history_promos(
    since: Annotated[str | None, typer.Option("--since", help="Ex: 7d (défaut), date ISO")] = "7d",
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
    output_format: OutputFormat = "text",
) -> None:
    cutoff = _history_since(since)
    candidates = promo_candidates(data_dir, since=cutoff)
    if normalize_output_format(output_format) == "json":
        echo_json(
            {
                "candidates": [
                    {
                        "canonical_name": trend.canonical_name,
                        "store": trend.store,
                        "last_price": trend.last_price,
                        "median_price": round(trend.median_price, 4),
                        "delta_pct": (
                            round(trend.delta_pct, 2) if trend.delta_pct is not None else None
                        ),
                    }
                    for trend in candidates
                ]
            }
        )
        return
    if not candidates:
        typer.echo("Aucun candidat promo détecté")
        return
    typer.echo("Candidats promo (prix courant < médiane historique):")
    for trend in candidates:
        delta = f"{trend.delta_pct:+.1f} %" if trend.delta_pct is not None else "n/a"
        typer.echo(
            f"- {trend.canonical_name} ({trend.store}): {trend.last_price:.2f} € "
            f"vs médiane {trend.median_price:.2f} € ({delta})"
        )


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
    global _cli_no_llm
    _cli_no_llm = bool(no_llm)
    if version:
        typer.echo(__version__)
        raise typer.Exit()


def current_cli_no_llm() -> bool:
    return _cli_no_llm


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


def _brand_action_label(action: BrandPreferenceAction) -> str:
    return {
        BrandPreferenceAction.PREFER: "préférée",
        BrandPreferenceAction.AVOID: "à éviter",
        BrandPreferenceAction.BLOCK: "bloquée",
        BrandPreferenceAction.NEUTRAL: "neutre",
    }[action]


def _set_brand_preference(action: BrandPreferenceAction, brand: str, data_dir: Path) -> None:
    preferences = load_brand_preferences(data_dir)
    normalized = preferences.add(action, brand)
    save_brand_preferences(data_dir, preferences)
    typer.echo(f"Marque {normalized}: {_brand_action_label(action)}")


@explain_app.command("offer")
def explain_offer_command(
    item: Annotated[str, typer.Argument(help="Article demandé")],
    product: Annotated[str, typer.Argument(help="Produit/offre à expliquer")],
    price: Annotated[float, typer.Option("--price", min=0.01)],
    store: Annotated[str, typer.Option("--store")] = "local",
    unit_price: Annotated[float | None, typer.Option("--unit-price", min=0.01)] = None,
    confidence: Annotated[str, typer.Option("--confidence")] = "medium",
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
    compare_by: Annotated[str, typer.Option("--compare-by")] = "price",
) -> None:
    shopping_item = ShoppingItem(name=item)
    offer = StoreOffer(
        store=store,
        item=shopping_item.name,
        product=product,
        price=price,
        unit_price=unit_price,
        confidence=confidence,
    )
    for line in explain_offer_lines(
        shopping_item,
        offer,
        compare_by=normalize_compare_by(compare_by),
        brand_preferences=load_brand_preferences(data_dir),
        substitutions=load_substitutions(data_dir),
    ):
        typer.echo(line)


@brand_app.command("prefer")
def brand_prefer(
    brand: Annotated[str, typer.Argument(help="Marque à privilégier")],
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    _set_brand_preference(BrandPreferenceAction.PREFER, brand, data_dir)


@brand_app.command("avoid")
def brand_avoid(
    brand: Annotated[str, typer.Argument(help="Marque à éviter sauf gros avantage prix")],
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    _set_brand_preference(BrandPreferenceAction.AVOID, brand, data_dir)


@brand_app.command("block")
def brand_block(
    brand: Annotated[str, typer.Argument(help="Marque à exclure totalement")],
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    _set_brand_preference(BrandPreferenceAction.BLOCK, brand, data_dir)


@brand_app.command("remove")
def brand_remove(
    brand: Annotated[str, typer.Argument(help="Marque à retirer des préférences")],
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    preferences = load_brand_preferences(data_dir)
    normalized = preferences.remove(brand)
    save_brand_preferences(data_dir, preferences)
    typer.echo(f"Marque retirée: {normalized}")


@brand_app.command("list")
def brand_list(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    preferences = load_brand_preferences(data_dir)
    typer.echo(f"Fichier: {brand_preferences_path(data_dir)}")
    for label, values in (
        ("prefer", preferences.prefer),
        ("avoid", preferences.avoid),
        ("block", preferences.block),
    ):
        rendered = ", ".join(sorted(values)) if values else "—"
        typer.echo(f"{label}: {rendered}")
    typer.echo(
        "Garde-fou prefer: "
        f"+{preferences.prefer_max_price_delta_eur:g} € et "
        f"+{preferences.prefer_max_price_delta_percent:g} % max"
    )
    typer.echo(
        "Garde-fou avoid: avantage prix significatif si "
        f">={preferences.avoid_min_savings_eur:g} € ou "
        f">={preferences.avoid_min_savings_percent:g} %"
    )


@brand_app.command("show")
def brand_show(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    preferences = load_brand_preferences(data_dir)
    typer.echo(
        yaml.safe_dump(
            preferences.model_dump(mode="json"),
            allow_unicode=True,
            sort_keys=True,
        )
    )


@cache_app.command("show")
def cache_show(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    cache = load_price_cache(data_dir)
    typer.echo(f"Fichier: {price_cache_path(data_dir)}")
    typer.echo(f"Offres: {len(cache.offers)}")
    for offer in cache.offers:
        typer.echo(f"- {offer.item}: {offer.product} — {offer.store} — {offer.price:.2f} €")


@cache_app.command("import")
def cache_import(
    prices: Annotated[Path, typer.Argument(help="YAML: offers: [...]")],
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    offers = load_offers(prices)
    cache = add_offers_to_cache(data_dir, offers)
    typer.echo(f"Offres importées: {len(offers)}")
    typer.echo(f"Cache: {price_cache_path(data_dir)} ({len(cache.offers)} offres)")


@substitution_app.command("add")
def substitution_add(
    item: Annotated[str, typer.Argument(help="Article source")],
    substitute: Annotated[str, typer.Argument(help="Article substitut")],
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    catalog = load_substitutions(data_dir)
    normalized_item, normalized_substitute = catalog.add(item, substitute)
    save_substitutions(data_dir, catalog)
    typer.echo(f"Substitution ajoutée: {normalized_item} -> {normalized_substitute}")


@substitution_app.command("remove")
def substitution_remove(
    item: Annotated[str, typer.Argument(help="Article source")],
    substitute: Annotated[
        str | None, typer.Argument(help="Substitut précis, sinon règle entière")
    ] = None,
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    catalog = load_substitutions(data_dir)
    normalized_item, normalized_substitute = catalog.remove(item, substitute)
    save_substitutions(data_dir, catalog)
    if normalized_substitute:
        typer.echo(f"Substitution retirée: {normalized_item} -> {normalized_substitute}")
    else:
        typer.echo(f"Substitutions retirées: {normalized_item}")


@substitution_app.command("list")
def substitution_list(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    catalog = load_substitutions(data_dir)
    typer.echo(f"Fichier: {substitutions_path(data_dir)}")
    if not catalog.rules:
        typer.echo("- aucune substitution")
        return
    for rule in catalog.rules:
        typer.echo(f"- {rule.item}: {', '.join(rule.substitutes)}")


@constraint_app.command("show")
def constraint_show(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    typer.echo(
        yaml.safe_dump(
            load_constraints(data_dir).model_dump(mode="json"),
            allow_unicode=True,
            sort_keys=True,
        )
    )


@constraint_app.command("set")
def constraint_set(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
    max_total_eur: Annotated[float | None, typer.Option("--max-total-eur", min=0.01)] = None,
    min_items: Annotated[int | None, typer.Option("--min-items", min=0)] = None,
    max_items: Annotated[int | None, typer.Option("--max-items", min=1)] = None,
    blocked_store: Annotated[list[str] | None, typer.Option("--blocked-store")] = None,
    preferred_store: Annotated[list[str] | None, typer.Option("--preferred-store")] = None,
) -> None:
    constraints = load_constraints(data_dir)
    payload = constraints.model_dump()
    for key, value in (
        ("max_total_eur", max_total_eur),
        ("min_items", min_items),
        ("max_items", max_items),
    ):
        if value is not None:
            payload[key] = value
    if blocked_store is not None:
        payload["blocked_stores"] = [normalize_name(store) for store in blocked_store]
    if preferred_store is not None:
        payload["preferred_stores"] = [normalize_name(store) for store in preferred_store]
    updated = BasketConstraints.model_validate(payload)
    save_constraints(data_dir, updated)
    typer.echo("Contraintes sauvegardées")


def _safe_count(load_fn) -> int | None:
    try:
        value = load_fn()
    except Exception:
        return None
    if isinstance(value, list):
        return len(value)
    items = getattr(value, "items", None)
    if isinstance(items, list):
        return len(items)
    rules = getattr(value, "rules", None)
    if isinstance(rules, list):
        return len(rules)
    offers = getattr(value, "offers", None)
    if isinstance(offers, list):
        return len(offers)
    return None


def doctor_status_payload(data_dir: Path) -> dict:
    llm = no_llm_status(cli_no_llm=current_cli_no_llm())
    files = {
        "profile": {
            "path": str(profile_path(data_dir)),
            "present": profile_path(data_dir).exists(),
            "effective_path": str(resolve_profile_file(data_dir)),
            "household": active_household_name(data_dir),
        },
        "recipes": {
            "path": str(recipes_path(data_dir)),
            "present": recipes_path(data_dir).exists(),
            "count": _safe_count(lambda: load_recipes(data_dir)),
        },
        "pantry": {
            "path": str(pantry_path(data_dir)),
            "present": pantry_path(data_dir).exists(),
            "count": _safe_count(lambda: load_pantry(data_dir)),
        },
        "brands": {
            "path": str(brand_preferences_path(data_dir)),
            "present": brand_preferences_path(data_dir).exists(),
        },
        "substitutions": {
            "path": str(substitutions_path(data_dir)),
            "present": substitutions_path(data_dir).exists(),
            "count": _safe_count(lambda: load_substitutions(data_dir)),
        },
        "constraints": {
            "path": str(constraints_path(data_dir)),
            "present": constraints_path(data_dir).exists(),
        },
        "price_cache": {
            "path": str(price_cache_path(data_dir)),
            "present": price_cache_path(data_dir).exists(),
        },
        "offers_cache": {
            "path": str(offers_cache_dir(data_dir)),
            "present": offers_cache_dir(data_dir).exists(),
        },
        "history_db": {
            "path": str(history_db_path(data_dir)),
            "present": history_db_path(data_dir).exists(),
        },
    }
    next_actions: list[str] = []
    if not files["profile"]["present"]:
        next_actions.append("panier profile init")
    if not files["recipes"]["present"]:
        next_actions.append("panier recipe add <recette.yaml>")
    if not files["pantry"]["present"]:
        next_actions.append("panier pantry init")
    if not files["price_cache"]["present"]:
        next_actions.append("panier drive collect <liste.yaml> --output <offers.yaml>")
    return {
        "mode": llm.mode,
        "llm_allowed": not llm.no_llm,
        "llm_source": llm.source,
        "data_dir": str(data_dir),
        "files": files,
        "critical_path": "règles locales + fichiers YAML + tie-breaks stables",
        "next_actions": next_actions,
    }


@doctor_app.command("status")
def doctor_status(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
    output_format: OutputFormat = "text",
) -> None:
    payload = doctor_status_payload(data_dir)
    if normalize_output_format(output_format) == "json":
        echo_json(payload)
        return
    typer.echo("Diagnostic Panier")
    typer.echo(f"Mode: {payload['mode']}")
    typer.echo(f"LLM autorisé: {'oui' if payload['llm_allowed'] else 'non'}")
    for label, info in payload["files"].items():
        count = info.get("count")
        count_suffix = f" ({count})" if count is not None else ""
        typer.echo(
            f"{label}: {'présent' if info['present'] else 'absent'}{count_suffix} — {info['path']}"
        )
    typer.echo(f"Chemin critique: {payload['critical_path']}")
    if payload["next_actions"]:
        typer.echo("Prochaines actions:")
        for action in payload["next_actions"]:
            typer.echo(f"- {action}")


@doctor_app.command("determinism")
def doctor_determinism(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    payload = doctor_status_payload(data_dir)
    typer.echo("Diagnostic déterminisme Panier")
    typer.echo(f"Mode: {payload['mode']}")
    typer.echo(f"LLM autorisé: {'oui' if payload['llm_allowed'] else 'non'}")
    label_map = {
        "profile": "profil",
        "recipes": "recettes",
        "pantry": "stock",
        "brands": "marques",
        "substitutions": "substitutions",
        "price_cache": "cache prix",
    }
    for key, label in label_map.items():
        info = payload["files"][key]
        typer.echo(f"{label}: {'présent' if info['present'] else 'absent'} — {info['path']}")
    typer.echo(f"Chemin critique: {payload['critical_path']}")


@app.command("init")
def init_project(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
    force: Annotated[bool, typer.Option("--force", help="Réécrire les fichiers starter.")] = False,
) -> None:
    typer.echo("Initialisation Panier")
    profile = profile_path(data_dir)
    recipes = recipes_path(data_dir)
    pantry = pantry_path(data_dir)
    constraints = constraints_path(data_dir)
    starter_recipes = [
        Recipe(
            name="Bowl riz thon",
            servings=1,
            prep_minutes=10,
            cost_level="budget",
            tags=["rapide", "budget"],
            ingredients=[
                Ingredient(name="riz", quantity=150, unit="g"),
                Ingredient(name="thon", quantity=1, unit="boîte"),
                Ingredient(name="tomates", quantity=2, unit="pièce"),
            ],
        )
    ]
    targets: list[tuple[Path, object]] = [
        (profile, FoodProfile()),
        (recipes, [recipe.model_dump(mode="json") for recipe in starter_recipes]),
        (pantry, Pantry()),
        (constraints, BasketConstraints()),
    ]
    for path, payload in targets:
        if path.exists() and not force:
            typer.echo(f"déjà présent: {path}")
            continue
        if isinstance(payload, list):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
        else:
            dump_yaml(path, payload)
        typer.echo(f"créé: {path}")
    typer.echo("Suite: panier doctor status puis panier plan --data-dir <dir>")


@doctor_app.command("drive")
def doctor_drive(
    store: Annotated[str, typer.Argument(help="Drive à diagnostiquer: leclerc ou auchan")],
    profile: Annotated[str, typer.Option("--profile")] = "courses",
    browser_command: Annotated[str | None, typer.Option("--browser-command")] = None,
) -> None:
    normalized = normalize_name(store)
    browser = ManagedBrowserClient(
        command=browser_command,
        profile=managed_browser_profile_for_drive(profile, normalized),
        site=normalized,
    )
    url = store_search_url(normalized, "riz")
    typer.echo(f"Diagnostic drive {normalized}")
    typer.echo(f"Profil Managed Browser: {managed_browser_profile_for_drive(profile, normalized)}")
    typer.echo(f"URL test: {url}")
    try:
        if url:
            browser.navigate(url)
        status = run_cart_status_for_store(
            normalized,
            [CartLine(store=normalized, item="riz", product="riz", search_url=url)],
            profile=profile,
            browser_command=browser_command,
        )
    except ManagedBrowserError as exc:
        typer.echo(f"Managed Browser indisponible: {exc}", err=True)
        raise typer.Exit(1) from exc
    _echo_cart_status(normalized, status)
    if normalized == "leclerc" and status.get("blocked_by"):
        typer.echo(
            "Leclerc: session/drive bloqué par anti-bot; ajout panier live non validable.", err=True
        )


@profile_app.command("init")
def profile_init(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    path = resolve_profile_file(data_dir)
    if path.exists() and not force:
        typer.echo(f"Profil déjà présent : {path}")
        return
    save_effective_profile(data_dir, FoodProfile())
    typer.echo(f"Profil créé : {path}")


def _echo_household_meta(household: HouseholdProfile) -> None:
    typer.echo(f"Foyer: {household.name}")
    if household.display_name:
        typer.echo(f"  Nom affiché: {household.display_name}")
    if household.geo_zone:
        typer.echo(f"  Zone géographique: {household.geo_zone}")
    enabled = sorted(household.enabled_stores())
    disabled = sorted(household.disabled_stores())
    unknown = sorted(set(household.stores) - set(enabled) - set(disabled))
    if enabled or disabled:
        parts = []
        if enabled:
            parts.append(f"activés: {', '.join(enabled)}")
        if disabled:
            parts.append(f"désactivés: {', '.join(disabled)}")
        suffix = f" (non déclarés: {', '.join(unknown)})" if unknown else ""
        typer.echo(f"  Magasins {'; '.join(parts)}{suffix}")
    if household.servings_per_meal is not None:
        typer.echo(f"  Portions par repas: {household.servings_per_meal}")
    if household.budget_max_eur is not None:
        typer.echo(f"  Budget max: {household.budget_max_eur:.2f} €")
    if household.notes:
        typer.echo(f"  Notes: {household.notes}")


@profile_app.command("create")
def profile_create(
    name: Annotated[str, typer.Argument()],
    display_name: Annotated[str | None, typer.Option("--display-name")] = None,
    geo_zone: Annotated[str | None, typer.Option("--geo-zone", help="Ville / code postal")] = None,
    enable_store: Annotated[list[str] | None, typer.Option("--enable-store")] = None,
    disable_store: Annotated[list[str] | None, typer.Option("--disable-store")] = None,
    servings_per_meal: Annotated[int | None, typer.Option("--servings", min=1)] = None,
    budget_max_eur: Annotated[float | None, typer.Option("--budget-max-eur", min=0.01)] = None,
    notes: Annotated[str | None, typer.Option("--notes")] = None,
    from_base: Annotated[
        bool,
        typer.Option(
            "--from-base",
            help="Initialiser les préférences depuis le profile.yaml historique.",
        ),
    ] = False,
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    path = household_path(data_dir, name)
    if path.exists():
        raise typer.BadParameter(f"le foyer existe déjà : {slugify(name)}")
    preferences = load_profile(data_dir) if from_base else FoodProfile()
    stores: dict[str, bool] = {}
    for store in enable_store or []:
        stores[normalize_name(store)] = True
    for store in disable_store or []:
        stores[normalize_name(store)] = False
    household = HouseholdProfile(
        name=name,
        display_name=display_name,
        geo_zone=geo_zone,
        stores=stores,
        servings_per_meal=servings_per_meal,
        budget_max_eur=budget_max_eur,
        notes=notes,
        preferences=preferences,
    )
    saved = save_household(data_dir, household)
    typer.echo(f"Foyer créé : {saved}")
    _echo_household_meta(household)


@profile_app.command("list")
def profile_list(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    names = list_households(data_dir)
    active = active_household_name(data_dir)
    base_path = profile_path(data_dir)
    typer.echo(
        f"Base historique: {base_path}"
        + (" (effective)" if active is None and base_path.exists() else "")
    )
    if not names:
        typer.echo("Aucun foyer nommé (panier profile create <nom>)")
        return
    for name in names:
        marker = " [actif]" if name == active else ""
        try:
            household = load_household(data_dir, name)
            label = household.display_name or ""
            zone = f" — {household.geo_zone}" if household.geo_zone else ""
            extra = f" ({label}{zone})" if label or zone else ""
        except HouseholdError:
            extra = " (illisible)"
        typer.echo(f"- {name}{marker}{extra}")


@profile_app.command("use")
def profile_use(
    name: Annotated[str, typer.Argument()],
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    try:
        set_active_household(data_dir, name)
    except HouseholdError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Foyer actif : {slugify(name)}")


@profile_app.command("unuse")
def profile_unuse(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    set_active_household(data_dir, None)
    typer.echo("Retour au profil de base (profile.yaml)")


@profile_app.command("remove")
def profile_remove(
    name: Annotated[str, typer.Argument()],
    force: Annotated[
        bool,
        typer.Option("--force", help="Autoriser la suppression du foyer actif"),
    ] = False,
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    slug = slugify(name)
    path = household_path(data_dir, name)
    if not path.exists():
        raise typer.BadParameter(f"foyer introuvable : {slug}")
    if active_household_name(data_dir) == slug and not force:
        raise typer.BadParameter(
            f"{slug} est le foyer actif ; bascule d'abord (profile use) ou --force."
        )
    path.unlink()
    if active_household_name(data_dir) == slug:
        set_active_household(data_dir, None)
    typer.echo(f"Foyer supprimé : {slug}")


@profile_app.command("set")
def profile_set(
    name: Annotated[
        str | None,
        typer.Argument(help="Foyer à modifier (défaut : foyer actif)"),
    ] = None,
    display_name: Annotated[str | None, typer.Option("--display-name")] = None,
    geo_zone: Annotated[str | None, typer.Option("--geo-zone")] = None,
    enable_store: Annotated[list[str] | None, typer.Option("--enable-store")] = None,
    disable_store: Annotated[list[str] | None, typer.Option("--disable-store")] = None,
    servings_per_meal: Annotated[int | None, typer.Option("--servings", min=1)] = None,
    budget_max_eur: Annotated[float | None, typer.Option("--budget-max-eur", min=0.01)] = None,
    notes: Annotated[str | None, typer.Option("--notes")] = None,
    clear_geo: Annotated[bool, typer.Option("--clear-geo-zone")] = False,
    clear_budget: Annotated[bool, typer.Option("--clear-budget")] = False,
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    target = name or active_household_name(data_dir)
    if target is None:
        raise typer.BadParameter(
            "aucun foyer actif ; précisez un nom ou activez un foyer (profile use)."
        )
    try:
        household = load_household(data_dir, target)
    except HouseholdError as exc:
        raise typer.BadParameter(str(exc)) from exc
    updates: dict[str, object] = {}
    if display_name is not None:
        updates["display_name"] = display_name
    if geo_zone is not None:
        updates["geo_zone"] = geo_zone
    if clear_geo:
        updates["geo_zone"] = None
    if servings_per_meal is not None:
        updates["servings_per_meal"] = servings_per_meal
    if budget_max_eur is not None:
        updates["budget_max_eur"] = budget_max_eur
    if clear_budget:
        updates["budget_max_eur"] = None
    if notes is not None:
        updates["notes"] = notes
    stores = dict(household.stores)
    for store in enable_store or []:
        stores[normalize_name(store)] = True
    for store in disable_store or []:
        stores[normalize_name(store)] = False
    if stores != household.stores:
        updates["stores"] = stores
    if not updates:
        typer.echo("Rien à modifier.")
        return
    updated = household.model_copy(update=updates)
    save_household(data_dir, updated)
    typer.echo(f"Foyer mis à jour : {updated.name}")
    _echo_household_meta(updated)


@profile_app.command("show")
def profile_show(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    household = load_active_household(data_dir)
    if household is not None:
        _echo_household_meta(household)
        typer.echo("Préférences:")
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
    save_effective_profile(data_dir, profile)
    typer.echo(f"Ajouté à {kind}: {normalized}")


def preference_label(kind: str) -> str:
    return {
        "allergies": "allergies",
        "forbidden": "interdits",
        "dislikes": "détestés",
        "likes": "préférés",
        "accepted_recipes": "recettes acceptées",
        "rejected_recipes": "recettes rejetées",
    }.get(kind, kind)


def _preference_confidence(value: str | None) -> PreferenceConfidence:
    if value is None:
        return PreferenceConfidence.CONFIRMED
    try:
        return PreferenceConfidence(normalize_name(value))
    except ValueError as exc:
        raise typer.BadParameter(
            f"confiance invalide : {value} "
            "(attendues: confirmed, strong, medium, low)"
        ) from exc


def _preference_status(value: str | None) -> PreferenceStatus:
    if value is None:
        return PreferenceStatus.PERMANENT
    normalized = normalize_name(value).replace("_", "-")
    try:
        return PreferenceStatus(normalized)
    except ValueError as exc:
        raise typer.BadParameter(
            f"statut invalide : {value} (attendus: permanent, temporary-context)"
        ) from exc


def set_preference_detail(
    kind: str,
    value: str,
    data_dir: Path,
    *,
    reason: str | None = None,
    confidence: str | None = None,
    status: str | None = None,
    source: str | None = None,
) -> None:
    profile = load_profile(data_dir)
    normalized = normalize_name(value)
    getattr(profile, kind).add(normalized)
    profile.set_detail(
        kind,
        PreferenceDetail(
            value=normalized,
            reason=reason,
            confidence=_preference_confidence(confidence),
            status=_preference_status(status),
            source=source,
            since=datetime.now(UTC).date().isoformat(),
        ),
    )
    if kind == "accepted_recipes":
        profile.rejected_recipes.discard(normalized)
        profile.clear_detail("rejected_recipes", normalized)
    elif kind == "rejected_recipes":
        profile.accepted_recipes.discard(normalized)
        profile.clear_detail("accepted_recipes", normalized)
    save_effective_profile(data_dir, profile)


def remove_preference_value(kind: str, value: str, data_dir: Path) -> None:
    profile = load_profile(data_dir)
    normalized = normalize_name(value)
    getattr(profile, kind).discard(normalized)
    profile.clear_detail(kind, normalized)
    save_effective_profile(data_dir, profile)
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


def _profile_preference_command(
    kind: str,
    action: str,
    value: str,
    data_dir: Path,
    *,
    reason: str | None = None,
    confidence: str | None = None,
    status: str | None = None,
    source: str | None = None,
) -> None:
    if action == "add":
        set_preference_detail(
            kind,
            value,
            data_dir,
            reason=reason,
            confidence=confidence,
            status=status,
            source=source,
        )
        typer.echo(f"Ajouté à {preference_label(kind)}: {normalize_name(value)}")
        return
    if action == "remove":
        remove_preference_value(kind, value, data_dir)
        return
    raise typer.BadParameter("Action attendue : add ou remove.")


@profile_app.command("allergy")
def profile_allergy(
    action: Annotated[str, typer.Argument(help="add|remove")],
    value: Annotated[str, typer.Argument()],
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    confidence: Annotated[str | None, typer.Option("--confidence")] = None,
    status: Annotated[
        str | None,
        typer.Option("--status", help="permanent (défaut) ou temporary-context"),
    ] = None,
    source: Annotated[str | None, typer.Option("--source")] = None,
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    _profile_preference_command(
        "allergies",
        action,
        value,
        data_dir,
        reason=reason,
        confidence=confidence,
        status=status,
        source=source,
    )


@profile_app.command("dislike")
def profile_dislike(
    action: Annotated[str, typer.Argument(help="add|remove")],
    value: Annotated[str, typer.Argument()],
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    confidence: Annotated[str | None, typer.Option("--confidence")] = None,
    status: Annotated[
        str | None,
        typer.Option("--status", help="permanent (défaut) ou temporary-context"),
    ] = None,
    source: Annotated[str | None, typer.Option("--source")] = None,
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    _profile_preference_command(
        "dislikes",
        action,
        value,
        data_dir,
        reason=reason,
        confidence=confidence,
        status=status,
        source=source,
    )


@profile_app.command("forbid")
def profile_forbid(
    action: Annotated[str, typer.Argument(help="add|remove")],
    value: Annotated[str, typer.Argument()],
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    confidence: Annotated[str | None, typer.Option("--confidence")] = None,
    status: Annotated[
        str | None,
        typer.Option("--status", help="permanent (défaut) ou temporary-context"),
    ] = None,
    source: Annotated[str | None, typer.Option("--source")] = None,
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    _profile_preference_command(
        "forbidden",
        action,
        value,
        data_dir,
        reason=reason,
        confidence=confidence,
        status=status,
        source=source,
    )


@profile_app.command("like")
def profile_like(
    action: Annotated[str, typer.Argument(help="add|remove")],
    value: Annotated[str, typer.Argument()],
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    confidence: Annotated[str | None, typer.Option("--confidence")] = None,
    status: Annotated[
        str | None,
        typer.Option("--status", help="permanent (défaut) ou temporary-context"),
    ] = None,
    source: Annotated[str | None, typer.Option("--source")] = None,
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    _profile_preference_command(
        "likes",
        action,
        value,
        data_dir,
        reason=reason,
        confidence=confidence,
        status=status,
        source=source,
    )


@profile_app.command("accept-recipe")
def profile_accept_recipe(
    action: Annotated[str, typer.Argument(help="add|remove")],
    value: Annotated[str, typer.Argument()],
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    confidence: Annotated[str | None, typer.Option("--confidence")] = None,
    source: Annotated[str | None, typer.Option("--source")] = None,
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    _profile_preference_command(
        "accepted_recipes",
        action,
        value,
        data_dir,
        reason=reason,
        confidence=confidence,
        source=source,
    )


@profile_app.command("reject-recipe")
def profile_reject_recipe(
    action: Annotated[str, typer.Argument(help="add|remove")],
    value: Annotated[str, typer.Argument()],
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    confidence: Annotated[str | None, typer.Option("--confidence")] = None,
    status: Annotated[
        str | None,
        typer.Option("--status", help="permanent (défaut) ou temporary-context"),
    ] = None,
    source: Annotated[str | None, typer.Option("--source")] = None,
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    _profile_preference_command(
        "rejected_recipes",
        action,
        value,
        data_dir,
        reason=reason,
        confidence=confidence,
        status=status,
        source=source,
    )


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
        brand_preferences=load_brand_preferences(data_dir),
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
    items = read_shopping_items(shopping_list)
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
    items = read_shopping_items(shopping_list)
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
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
) -> None:
    items = read_shopping_items(shopping_list)
    offers = load_offers(prices)
    comparison = normalize_compare_by(compare_by)
    items, offers, _constraints = prepare_items_and_offers(items, offers, data_dir)
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
    update_cache: Annotated[bool, typer.Option("--update-cache/--no-update-cache")] = False,
    data_dir: Annotated[
        Path, typer.Option("--data-dir", help="Répertoire données Panier")
    ] = DEFAULT_DATA_DIR,
) -> None:
    items = read_shopping_items(shopping_list)
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

    if update_cache:
        add_offers_to_cache(data_dir, offers)
        typer.echo(f"Cache prix mis à jour: {price_cache_path(data_dir)}")
    if offers:
        _record_history(data_dir, drive, offers)
    payload = {"offers": [offer.model_dump(mode="json") for offer in offers]}
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        typer.echo(f"Offres collectées: {len(offers)} -> {output}")
    else:
        typer.echo(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False))


def echo_recipe_cost(cost: RecipeCost) -> None:
    """Affiche le coût d'une recette ; ingrédients non pricés explicites."""
    typer.echo(f"Coût {cost.recipe.name} ({cost.recipe.servings} portions):")
    for entry in cost.costs:
        if entry.price is None:
            typer.echo(f"- {entry.name}: unpriced (aucun prix connu)")
            continue
        source = f"historique {entry.store}" if entry.source == "history" else entry.store
        typer.echo(f"- {entry.name}: {entry.price:.2f} € ({source})")
    if cost.total is not None:
        typer.echo(f"Total: {cost.total:.2f} €")
        per = cost.per_servings()
        if per is not None:
            typer.echo(f"Coût par portion: {per:.2f} €")
    else:
        typer.echo(
            f"Total partiel: {cost.partial_total:.2f} € "
            f"({len(cost.unpriced)} article(s) non pricé(s))"
        )


@recipe_app.command("cost")
def recipe_cost_command(
    name: Annotated[str, typer.Argument()],
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
    prices: Annotated[
        Path | None,
        typer.Option("--prices", help="YAML prioritaire: offers: [...]"),
    ] = None,
    output_format: OutputFormat = "text",
) -> None:
    recipe = find_recipe(load_recipes(data_dir), name)
    offers = load_offers(prices) if prices is not None else None
    cost = compute_recipe_cost(recipe, data_dir=data_dir, prices_offers=offers)
    if normalize_output_format(output_format) == "json":
        echo_json(
            {
                "recipe": recipe.name,
                "servings": recipe.servings,
                "total": cost.total,
                "partial_total": cost.partial_total if cost.total is None else None,
                "unpriced": cost.unpriced,
                "items": [
                    {
                        "name": entry.name,
                        "price": entry.price,
                        "source": entry.source,
                        "store": entry.store,
                    }
                    for entry in cost.costs
                ],
            }
        )
        return
    echo_recipe_cost(cost)


def _sorted_recipes_by_cost(
    recipes: list, *, data_dir: Path, prices: Path | None
) -> list:
    offers = load_offers(prices) if prices is not None else None
    costs = [
        compute_recipe_cost(recipe, data_dir=data_dir, prices_offers=offers)
        for recipe in recipes
    ]
    return sorted(costs, key=recipe_cost_sort_key)


def _recipe_cost_suffix(cost: RecipeCost) -> str:
    total = cost.total
    if total is None:
        return " — coût inconnu"
    return f" — {total:.2f} €"


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
    balanced: Annotated[bool, typer.Option("--balanced", help="Recettes équilibrées")] = False,
    sort_cost: Annotated[
        bool,
        typer.Option("--sort-cost", help="Trier par coût croissant (non pricées en dernier)."),
    ] = False,
    prices: Annotated[
        Path | None,
        typer.Option("--prices", help="YAML prioritaire pour les coûts: offers: [...]"),
    ] = None,
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
    if sort_cost:
        costs = _sorted_recipes_by_cost(recipes, data_dir=data_dir, prices=prices)
        for cost in costs:
            tags = f" [{', '.join(cost.recipe.tags)}]" if cost.recipe.tags else ""
            balance = ""
            if balanced or min_balance_score is not None:
                score = score_recipe_balance(cost.recipe)
                balance = f" — équilibre {score.score}/100 ({score.verdict})"
            typer.echo(
                f"- {cost.recipe.name}{tags}{balance}{_recipe_cost_suffix(cost)}"
            )
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
        brand_preferences=load_brand_preferences(data_dir),
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
    balanced: Annotated[bool, typer.Option("--balanced", help="Recettes équilibrées")] = False,
    max_cost_per_meal: Annotated[
        float | None,
        typer.Option(
            "--max-cost-per-meal",
            min=0.01,
            help="Exclut les recettes dont le coût connu dépasse ce montant.",
        ),
    ] = None,
    prices: Annotated[
        Path | None,
        typer.Option("--prices", help="YAML prioritaire pour les coûts: offers: [...]"),
    ] = None,
) -> None:
    if balanced and min_balance_score is None:
        min_balance_score = 70
    profile_data = load_profile(data_dir)
    recipes = apply_recipe_feedback_order(load_recipes(data_dir), profile_data)
    offers = load_offers(prices) if prices is not None else None
    cost_suffix_by_name: dict[str, str] = {}
    if max_cost_per_meal is not None:
        affordable: list[Recipe] = []
        for recipe in recipes:
            cost = compute_recipe_cost(recipe, data_dir=data_dir, prices_offers=offers)
            cost_suffix_by_name[normalize_name(recipe.name)] = _recipe_cost_suffix(cost)
            if cost.total is None or cost.total <= max_cost_per_meal:
                affordable.append(recipe)
        recipes = affordable
    selected = select_meals(
        recipes,
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
        if normalize_name(recipe.name) in cost_suffix_by_name:
            suffix += cost_suffix_by_name[normalize_name(recipe.name)]
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
    balanced: Annotated[bool, typer.Option("--balanced", help="Recettes équilibrées")] = False,
    add_to_cart: Annotated[
        bool,
        typer.Option(
            "--add-to-cart",
            help="Préparer les paniers drive avec les produits recommandés.",
        ),
    ] = False,
    remove_from_cart: Annotated[
        bool,
        typer.Option(
            "--remove-from-cart",
            help="Retirer du panier les produits recommandés déjà présents.",
        ),
    ] = False,
    cart_dry_run: Annotated[
        bool,
        typer.Option(
            "--cart-dry-run/--cart-live",
            help="Dry-run par défaut: n'ajoute rien réellement.",
        ),
    ] = True,
    no_cache: Annotated[
        bool,
        typer.Option("--no-cache", help="Forcer une collecte fraîche, ignorer le cache offres."),
    ] = False,
    cache_ttl_hours: Annotated[
        float | None,
        typer.Option(
            "--cache-ttl-hours",
            min=0.01,
            help="TTL du cache offres en heures (défaut 6, env PANIER_CACHE_TTL_HOURS).",
        ),
    ] = None,
    collect_timeout_seconds: Annotated[
        float | None,
        typer.Option(
            "--collect-timeout-seconds",
            min=0.1,
            help="Timeout global du cycle de collecte (env PANIER_COLLECT_TIMEOUT_SECONDS).",
        ),
    ] = None,
    price_history: Annotated[
        bool,
        typer.Option(
            "--price-history/--no-price-history",
            help="Bloc historique prix informatif si disponible.",
        ),
    ] = True,
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
    collect_had_failure = False
    cache_report: dict[str, dict] = {}
    if collect:
        offers, collect_had_failure, cache_report = collect_offers_with_cache(
            items,
            [drive.strip() for drive in collect.split(",") if drive.strip()],
            profile=profile,
            browser_command=browser_command,
            max_results=max_results,
            catalog=load_catalog(data_dir),
            data_dir=data_dir,
            use_cache=not no_cache,
            ttl_hours=cache_ttl_hours,
            timeout_seconds=collect_timeout_seconds,
        )
        if collect_output is not None:
            payload_offers: dict[str, object] = {
                "offers": [offer.model_dump(mode="json") for offer in offers]
            }
            if not no_cache:
                payload_offers["cache"] = [
                    {"store": store, **info} for store, info in sorted(cache_report.items())
                ]
            collect_output.parent.mkdir(parents=True, exist_ok=True)
            collect_output.write_text(
                yaml.safe_dump(payload_offers, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            typer.echo(f"Offres collectées: {len(offers)} -> {collect_output}")
    elif prices is not None:
        offers = load_offers(prices)

    if offers is None:
        return
    if not offers and collect_had_failure:
        typer.echo(
            "Recommandation indisponible: aucune offre collectée; "
            "voir les avertissements Managed Browser.",
            err=True,
        )
        return

    comparison = normalize_compare_by(compare_by)
    items, offers, constraints = prepare_items_and_offers(items, offers, data_dir)
    brand_preferences = load_brand_preferences(data_dir)
    echo_basket_options(
        items,
        offers,
        max_stores=max_stores,
        compare_by=comparison,
        brand_preferences=brand_preferences,
    )
    try:
        recommendation = recommend_basket(
            items,
            offers,
            mode=mode,
            max_stores=max_stores,
            compare_by=comparison,
            brand_preferences=brand_preferences,
        )
    except ValueError as exc:
        if collect:
            typer.echo(f"Recommandation indisponible: {exc}", err=True)
            return
        raise
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
    constraint_issues = validate_recommendation_constraints(
        recommendation.total, len(recommendation.by_item), constraints
    )
    if constraint_issues:
        typer.echo("\nContraintes non satisfaites:", err=True)
        for issue in constraint_issues:
            typer.echo(f"- {issue}", err=True)
    echo_price_history_block(recommendation.by_item, data_dir, enabled=price_history)
    if collect_output is not None and price_history:
        _enrich_collect_output_with_history(
            collect_output, recommendation.by_item, data_dir
        )
    if add_to_cart and remove_from_cart:
        raise typer.BadParameter(
            "Choisis soit --add-to-cart soit --remove-from-cart, pas les deux."
        )
    if add_to_cart or remove_from_cart:
        grouped_lines = cart_lines_from_recommendation(recommendation.by_item)
        action = "remove" if remove_from_cart else "add"
        echo_cart_plan(grouped_lines, action=action)
        results: dict[str, dict] = {}
        for store, lines in grouped_lines.items():
            try:
                store_results = _run_cart_action(
                    action,
                    {store: lines},
                    profile=profile,
                    browser_command=browser_command,
                    dry_run=cart_dry_run,
                )
            except ManagedBrowserError as exc:
                operation = "Suppression panier" if remove_from_cart else "Ajout panier"
                typer.echo(f"{operation} {store} indisponible: {exc}", err=True)
                continue
            results.update(store_results)
        run_path = _persist_cart_run(
            data_dir,
            action=action,
            dry_run=cart_dry_run,
            grouped_lines=grouped_lines,
            results=results,
        )
        typer.echo(f"Run panier sauvegardé: {run_path}")


@cart_app.command("status")
def cart_status(
    run_id: Annotated[
        str,
        typer.Option("--run", help="ID du run panier à relire, ou latest."),
    ] = "latest",
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
    profile: Annotated[str, typer.Option("--profile")] = "courses",
    browser_command: Annotated[str | None, typer.Option("--browser-command")] = None,
    browser: Annotated[
        bool,
        typer.Option("--browser/--no-browser", help="Lire aussi l'état réel via Managed Browser."),
    ] = False,
    output_format: OutputFormat = "text",
) -> None:
    try:
        run = load_cart_run(data_dir, run_id)
    except FileNotFoundError as exc:
        raise typer.BadParameter(str(exc)) from exc
    summary = _cart_run_results_summary(run)
    if normalize_output_format(output_format) == "json" and not browser:
        echo_json(
            {
                "id": run.id,
                "action": run.action,
                "dry_run": run.dry_run,
                "created_at": run.created_at,
                "summary": summary,
                "file": str(cart_run_path(data_dir, run.id)),
                "grouped_lines": {
                    store: [
                        line.model_dump() if hasattr(line, "model_dump") else line.__dict__
                        for line in lines
                    ]
                    for store, lines in run.grouped_lines.items()
                },
                "results": run.results,
            }
        )
        return
    typer.echo(f"Dernier run panier: {run.id}")
    typer.echo(f"  Action: {run.action}")
    typer.echo(f"  Mode: {'dry-run' if run.dry_run else 'live'}")
    typer.echo(f"  Stores: {summary['stores']}")
    typer.echo(f"  Produits trouvés/catalogue: {summary['catalog_found']}")
    label = "retirables/disponibles" if run.action == "remove" else "ajoutables/disponibles"
    done = "retirés" if run.action == "remove" else "insérés"
    typer.echo(f"  Produits {label}: {summary['available']}")
    typer.echo(f"  Produits effectivement {done}: {summary['done']}")
    typer.echo(f"  Fichier: {cart_run_path(data_dir, run.id)}")
    if not browser:
        return
    for store, lines in run.grouped_lines.items():
        status = run_cart_status_for_store(
            store, lines, profile=profile, browser_command=browser_command
        )
        _echo_cart_status(store, status)


@cart_app.command("add")
def cart_add(
    run_id: Annotated[str, typer.Option("--run", help="ID du run panier à appliquer.")] = "latest",
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
    profile: Annotated[str, typer.Option("--profile")] = "courses",
    browser_command: Annotated[str | None, typer.Option("--browser-command")] = None,
    cart_dry_run: Annotated[bool, typer.Option("--cart-dry-run/--cart-live")] = True,
) -> None:
    try:
        run = load_cart_run(data_dir, run_id)
    except FileNotFoundError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if run.action == "remove":
        raise typer.BadParameter("ce run est une suppression; relance cart add avec un run d'ajout")
    echo_cart_plan(run.grouped_lines, action="add")
    results = _run_cart_action(
        "add",
        run.grouped_lines,
        profile=profile,
        browser_command=browser_command,
        dry_run=cart_dry_run,
    )
    path = _persist_cart_run(
        data_dir,
        action="add",
        dry_run=cart_dry_run,
        grouped_lines=run.grouped_lines,
        results=results,
    )
    typer.echo(f"Run panier sauvegardé: {path}")


@cart_app.command("remove")
def cart_remove(
    run_id: Annotated[str, typer.Option("--run", help="ID du run panier à appliquer.")] = "latest",
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
    profile: Annotated[str, typer.Option("--profile")] = "courses",
    browser_command: Annotated[str | None, typer.Option("--browser-command")] = None,
    cart_dry_run: Annotated[bool, typer.Option("--cart-dry-run/--cart-live")] = True,
) -> None:
    try:
        run = load_cart_run(data_dir, run_id)
    except FileNotFoundError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if run.action == "add":
        raise typer.BadParameter(
            "ce run est un ajout; relance cart remove avec un run de suppression"
        )
    echo_cart_plan(run.grouped_lines, action="remove")
    results = _run_cart_action(
        "remove",
        run.grouped_lines,
        profile=profile,
        browser_command=browser_command,
        dry_run=cart_dry_run,
    )
    path = _persist_cart_run(
        data_dir,
        action="remove",
        dry_run=cart_dry_run,
        grouped_lines=run.grouped_lines,
        results=results,
    )
    typer.echo(f"Run panier sauvegardé: {path}")


@cart_app.command("sync")
def cart_sync(
    run_id: Annotated[
        str, typer.Option("--run", help="ID du run panier à synchroniser.")
    ] = "latest",
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
    profile: Annotated[str, typer.Option("--profile")] = "courses",
    browser_command: Annotated[str | None, typer.Option("--browser-command")] = None,
    apply: Annotated[
        bool,
        typer.Option(
            "--apply", help="Réservé: le sync reste dry-run tant que le diff n'est pas confirmé."
        ),
    ] = False,
) -> None:
    if apply:
        raise typer.BadParameter(
            "cart sync est en dry-run uniquement; "
            "utilise cart add/remove --cart-live après vérification."
        )
    run = load_cart_run(data_dir, run_id)
    for store, lines in run.grouped_lines.items():
        status = run_cart_status_for_store(
            store, lines, profile=profile, browser_command=browser_command
        )
        _echo_cart_status(store, status)
        diff = cart_sync_diff(store, lines, status)
        _echo_cart_sync_diff(store, diff)


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
    no_cache: Annotated[
        bool,
        typer.Option("--no-cache", help="Forcer une collecte fraîche, ignorer le cache offres."),
    ] = False,
    cache_ttl_hours: Annotated[
        float | None,
        typer.Option(
            "--cache-ttl-hours",
            min=0.01,
            help="TTL du cache offres en heures (défaut 6, env PANIER_CACHE_TTL_HOURS).",
        ),
    ] = None,
    collect_timeout_seconds: Annotated[
        float | None,
        typer.Option(
            "--collect-timeout-seconds",
            min=0.1,
            help="Timeout global du cycle de collecte (env PANIER_COLLECT_TIMEOUT_SECONDS).",
        ),
    ] = None,
    price_history: Annotated[
        bool,
        typer.Option(
            "--price-history/--no-price-history",
            help="Bloc historique prix informatif si disponible.",
        ),
    ] = True,
    days: Annotated[
        int | None,
        typer.Option("--days", min=1, help="Nombre de jours du plan (avec --slots)."),
    ] = None,
    slots: Annotated[
        str | None,
        typer.Option("--slots", help="Slots par jour, ex: dej,diner"),
    ] = None,
    max_repeats_per_week: Annotated[
        int,
        typer.Option("--max-repeats-per-week", min=1, help="Répétitions max d'une recette."),
    ] = 2,
    shuffle_seed: Annotated[
        int | None,
        typer.Option(
            "--shuffle-seed",
            help="Graine explicite : seule source de variabilité du plan.",
        ),
    ] = None,
) -> None:
    if balanced and min_balance_score is None:
        min_balance_score = 70
    try:
        slot_list = (
            canonical_week_slots([part.strip() for part in slots.split(",") if part.strip()])
            if slots
            else ["diner"]
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    meal_count = (
        (days or 7) * len(slot_list) if (days is not None or slots is not None) else meals
    )
    display_days = -(-meal_count // len(slot_list))
    profile_data = load_profile(data_dir)
    ranked = rank_recipes(
        apply_recipe_feedback_order(load_recipes(data_dir), profile_data),
        profile_data,
        include_tags=parse_csv_set(include_tags),
        exclude_tags=parse_csv_set(exclude_tags),
        max_prep_minutes=max_prep_minutes,
        cost_level=cost_level,
        min_balance_score=min_balance_score,
    )
    week_plan = build_week_plan(
        ranked,
        days=display_days,
        slots=slot_list,
        max_repeats_per_week=max_repeats_per_week,
        shuffle_seed=shuffle_seed,
    )
    selected = week_plan.recipes_in_order()
    unfilled = meal_count - len(selected)
    items = consolidate_ingredients(selected)
    if use_pantry:
        pantry = load_pantry_if_exists(data_dir)
        if pantry is not None:
            items = subtract_pantry(items, pantry)

    typer.echo(f"Semaine: {len(selected)} repas")
    prices_offers = load_offers(prices) if prices is not None else None
    costs_by_name: dict[str, RecipeCost] = {}
    for assignment in week_plan.assignments:
        score = score_recipe_balance(assignment.recipe)
        label = WEEK_SLOT_LABELS[assignment.slot]
        key = normalize_name(assignment.recipe.name)
        if key not in costs_by_name:
            costs_by_name[key] = compute_recipe_cost(
                assignment.recipe, data_dir=data_dir, prices_offers=prices_offers
            )
        typer.echo(
            f"Jour {assignment.day} {label}: {assignment.recipe.name}"
            f" — équilibre {score.score}/100 ({score.verdict})"
            f"{_recipe_cost_suffix(costs_by_name[key])}"
        )
    if unfilled > 0:
        typer.echo(
            f"Attention: {unfilled} slot(s) non pourvu(s) "
            "(pas assez de recettes compatibles / cap de répétition atteint).",
            err=True,
        )
    priced_totals = [cost.total for cost in costs_by_name.values() if cost.total is not None]
    repeat_sum = sum(week_plan.counts.values())
    if len(priced_totals) == len(costs_by_name) and costs_by_name:
        total_cost = sum(
            float(costs_by_name[key].total) * count
            for key, count in week_plan.counts.items()
        )
        typer.echo(
            f"Coût hebdo estimé ({repeat_sum} cuissons): {total_cost:.2f} €"
        )
    elif priced_totals:
        partial = sum(
            float(costs_by_name[key].total) * count
            for key, count in week_plan.counts.items()
            if costs_by_name[key].total is not None
        )
        unpriced = sorted(
            key for key, cost in costs_by_name.items() if cost.total is None
        )
        typer.echo(
            f"Coût hebdo partiel: {partial:.2f} € "
            f"(coût inconnu pour: {', '.join(unpriced)})"
        )
    typer.echo("\nÀ acheter:")
    if not items:
        typer.echo("- rien à acheter")
        return
    for item in items:
        typer.echo(f"- {format_item(item)}")

    offers: list[StoreOffer] | None = None
    collect_had_failure = False
    cache_report: dict[str, dict] = {}
    if collect:
        offers, collect_had_failure, cache_report = collect_offers_with_cache(
            items,
            [drive.strip() for drive in collect.split(",") if drive.strip()],
            profile=profile,
            browser_command=browser_command,
            max_results=max_results,
            catalog=load_catalog(data_dir),
            data_dir=data_dir,
            use_cache=not no_cache,
            ttl_hours=cache_ttl_hours,
            timeout_seconds=collect_timeout_seconds,
        )
        if collect_output is not None:
            payload_offers: dict[str, object] = {
                "offers": [offer.model_dump(mode="json") for offer in offers]
            }
            if not no_cache:
                payload_offers["cache"] = [
                    {"store": store, **info} for store, info in sorted(cache_report.items())
                ]
            collect_output.parent.mkdir(parents=True, exist_ok=True)
            collect_output.write_text(
                yaml.safe_dump(payload_offers, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            typer.echo(f"Offres collectées: {len(offers)} -> {collect_output}")
    elif prices is not None:
        offers = load_offers(prices)

    if offers is None:
        return
    if not offers and collect_had_failure:
        typer.echo(
            "Recommandation indisponible: aucune offre collectée; "
            "voir les avertissements Managed Browser.",
            err=True,
        )
        return
    comparison = normalize_compare_by(compare_by)
    items, offers, constraints = prepare_items_and_offers(items, offers, data_dir)
    brand_preferences = load_brand_preferences(data_dir)
    echo_basket_options(
        items,
        offers,
        max_stores=max_stores,
        compare_by=comparison,
        brand_preferences=brand_preferences,
    )
    try:
        recommendation = recommend_basket(
            items,
            offers,
            mode=mode,
            max_stores=max_stores,
            compare_by=comparison,
            brand_preferences=brand_preferences,
        )
    except ValueError as exc:
        if collect:
            typer.echo(f"Recommandation indisponible: {exc}", err=True)
            return
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
    constraint_issues = validate_recommendation_constraints(
        recommendation.total, len(recommendation.by_item), constraints
    )
    if constraint_issues:
        typer.echo("\nContraintes non satisfaites:", err=True)
        for issue in constraint_issues:
            typer.echo(f"- {issue}", err=True)
    echo_price_history_block(recommendation.by_item, data_dir, enabled=price_history)
    if collect_output is not None and price_history:
        _enrich_collect_output_with_history(
            collect_output, recommendation.by_item, data_dir
        )


@app.command("compare")
def compare(
    shopping_list: Annotated[Path, typer.Argument(help="YAML: items: [{name, quantity, unit}]")],
    prices: Annotated[Path | None, typer.Option("--prices", help="YAML: offers: [...]")] = None,
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
    mode: Annotated[PriceMode, typer.Option("--mode")] = PriceMode.HYBRID,
    max_stores: Annotated[int, typer.Option("--max-stores", min=1)] = 2,
    compare_by: Annotated[str, typer.Option("--compare-by")] = "price",
    price_history: Annotated[
        bool,
        typer.Option(
            "--price-history/--no-price-history",
            help="Afficher le bloc historique prix informatif si disponible.",
        ),
    ] = True,
) -> None:
    items = read_shopping_items(shopping_list)
    raw_offers = load_offers(prices) if prices is not None else load_price_cache(data_dir).offers
    comparison = normalize_compare_by(compare_by)
    items, offers, constraints = prepare_items_and_offers(items, raw_offers, data_dir)
    brand_preferences = load_brand_preferences(data_dir)
    echo_basket_options(
        items,
        offers,
        max_stores=max_stores,
        compare_by=comparison,
        brand_preferences=brand_preferences,
    )
    recommendation = recommend_basket(
        items,
        offers,
        mode=mode,
        max_stores=max_stores,
        compare_by=comparison,
        brand_preferences=brand_preferences,
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
    constraint_issues = validate_recommendation_constraints(
        recommendation.total, len(recommendation.by_item), constraints
    )
    if constraint_issues:
        typer.echo("\nContraintes non satisfaites:", err=True)
        for issue in constraint_issues:
            typer.echo(f"- {issue}", err=True)
    echo_price_history_block(recommendation.by_item, data_dir, enabled=price_history)


@app.command("dashboard")
def dashboard(
    data_dir: Annotated[Path, typer.Option("--data-dir")] = DEFAULT_DATA_DIR,
    promo_since: Annotated[
        str, typer.Option("--promo-since", help="Fenêtre promos (ex: 7d)")
    ] = "7d",
    output_format: OutputFormat = "text",
) -> None:
    """Vue d'ensemble : foyer, préférences, magasins, fichiers, historique, cache."""
    from panier.cart import cart_run_dir
    from panier.offers_cache import list_cache_entries

    household = load_active_household(data_dir)
    profile = load_profile(data_dir)
    cutoff = _history_since(promo_since)
    history = history_stats(data_dir, since=cutoff)
    promos = promo_candidates(data_dir, since=cutoff)
    cache_entries = list_cache_entries(data_dir)

    def store_status(store: str) -> bool:
        return household.stores.get(normalize_name(store), True)

    known_stores = ["auchan", "carrefour", "intermarche", "leclerc"]
    if household is not None:
        for store in household.stores:
            if store not in known_stores:
                known_stores.append(store)

    recipes_count = len(load_recipes(data_dir)) if recipes_path(data_dir).exists() else 0
    pantry = load_pantry_if_exists(data_dir)
    latest_run_id: str | None = None
    latest_file = cart_run_dir(data_dir) / "latest.txt"
    if latest_file.exists():
        latest_run_id = latest_file.read_text(encoding="utf-8").strip()

    payload: dict[str, object] = {
        "active_household": household.name if household else None,
        "profile_source": (
            f"foyer:{household.name}" if household else str(profile_path(data_dir))
        ),
        "preferences": {
            "allergies": sorted(profile.allergies),
            "forbidden": sorted(profile.forbidden),
            "dislikes": sorted(
                f"{value} ({profile.detail_for('dislikes', value).status})"
                if profile.detail_for("dislikes", value)
                else value
                for value in profile.dislikes
            ),
            "likes": sorted(profile.likes),
            "accepted_recipes": sorted(profile.accepted_recipes),
            "rejected_recipes": sorted(profile.rejected_recipes),
        },
        "household": {
            "geo_zone": household.geo_zone if household else None,
            "servings_per_meal": household.servings_per_meal if household else None,
            "budget_max_eur": household.budget_max_eur if household else None,
            "stores_enabled": sorted(store for store in known_stores if store_status(store)),
            "stores_disabled": sorted(store for store in known_stores if not store_status(store)),
        }
        if household
        else {"geo_zone": None, "servings_per_meal": None, "budget_max_eur": None,
              "stores_enabled": sorted(known_stores), "stores_disabled": []},
        "files": {
            "recipes_count": recipes_count,
            "pantry_items": len(pantry.items) if pantry else 0,
            "offers_cache_entries": len(cache_entries),
            "history_points": history["total_points"],
        },
        "history": history,
        "promos_since": promo_since,
        "promos": [
            {
                "canonical_name": trend.canonical_name,
                "store": trend.store,
                "last_price": trend.last_price,
                "median_price": round(trend.median_price, 4),
            }
            for trend in promos
        ],
        "latest_cart_run": latest_run_id,
        "cache_by_store": {
            store: {
                "entries": sum(1 for e in cache_entries if e.store == store),
                "newest_age_s": min(
                    (entry_age_seconds(e) for e in cache_entries if e.store == store),
                    default=None,
                ),
            }
            for store in sorted({e.store for e in cache_entries})
        },
    }

    if normalize_output_format(output_format) == "json":
        echo_json(payload)
        return

    typer.echo("Tableau de bord Panier")
    typer.echo(f"Foyer actif: {payload['profile_source']}")
    if household is not None:
        _echo_household_meta(household)

    prefs = payload["preferences"]
    typer.echo("\nPréférences:")
    typer.echo(f"  Allergènes: {', '.join(prefs['allergies']) or '—'}")
    typer.echo(f"  Interdits: {', '.join(prefs['forbidden']) or '—'}")
    typer.echo(f"  Détestés: {', '.join(prefs['dislikes']) or '—'}")
    typer.echo(f"  Préférés: {', '.join(prefs['likes']) or '—'}")
    typer.echo(f"  Recettes acceptées: {', '.join(prefs['accepted_recipes']) or '—'}")
    typer.echo(f"  Recettes rejetées: {', '.join(prefs['rejected_recipes']) or '—'}")

    meta = payload["household"]
    typer.echo("\nMagasins & budget:")
    typer.echo(f"  Activés: {', '.join(meta['stores_enabled']) or '—'}")
    typer.echo(f"  Désactivés: {', '.join(meta['stores_disabled']) or '—'}")
    typer.echo(f"  Zone géographique: {meta['geo_zone'] or '—'}")
    typer.echo(f"  Budget max: {meta['budget_max_eur'] or '—'}")

    files = payload["files"]
    typer.echo("\nDonnées locales:")
    typer.echo(f"  Recettes: {files['recipes_count']} | Placard: {files['pantry_items']} articles")
    typer.echo(f"  Cache offres: {files['offers_cache_entries']} entrée(s)")
    typer.echo(
        f"  Historique prix: {history['total_points']} point(s), "
        f"{history['distinct_items']} ingrédient(s)"
        + (f" (dernier {history['newest']})" if history["newest"] else "")
    )

    typer.echo(f"\nPromos candidates (--since {promo_since}):")
    if not promos:
        typer.echo("  —")
    for trend in promos:
        delta = f"{trend.delta_pct:+.1f} %" if trend.delta_pct is not None else "n/a"
        typer.echo(
            f"  - {trend.canonical_name} ({trend.store}): "
            f"{trend.last_price:.2f} € vs médiane {trend.median_price:.2f} € ({delta})"
        )

    by_store = payload["cache_by_store"]
    typer.echo("\nCache offres par store:")
    if not by_store:
        typer.echo("  —")
    for store, info in sorted(by_store.items()):
        age = info["newest_age_s"]
        age_text = f"{age:.0f}s" if isinstance(age, float) else "?"
        typer.echo(f"  - {store}: {info['entries']} entrée(s), plus récente âge {age_text}")

    typer.echo("\nDernier run panier:")
    typer.echo(f"  {latest_run_id or '—'}")
