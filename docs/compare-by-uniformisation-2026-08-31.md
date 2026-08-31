# Uniformisation `compare_by` — compare / plan / week — 2026-08-31

## Décision

**Uniformiser sur `default: price` partout**, garder `unit-price` en option explicite.

- `enum: [price, unit-price]` identique sur les trois queries.
- `--compare-by` exposé en CLI et relayé en registry (`argv` + `params`).
- `drive.py` aligné sur `price` par défaut (cohérent avec `best_offer_for_item` et `planner.py`).

Alternative écartée : garder `week` à `unit-price` — rejetée car incohérente avec `compare`/`plan` et avec l'intention produit (comparer le panier, pas le prix au kg par défaut).

---

## Constats avant patch

| Couche | compare | plan | week | Verdict |
|---|---|---|---|---|
| `src/panier/cli.py` (`--compare-by` default) | `price` (l4025) | `price` (l3291) | `unit-price` (l3736) | **incohérent** |
| `src/panier/drive.py` | `best_offer_for_item(compare_by="price")` l371 | `_offer_compare_value` branche sur `unit_price` | `_strict_sorted_offers(compare_by="unit_price")` l410 | **incohérent** — strict triait par défaut en unit-price alors que le reste compare en price |
| `src/panier/planner.py` | `CompareBy = Literal["price","unit_price"]` + tous defaults `price` | `price` | `price` | OK |
| `examples/addons.yaml` / `~/.config/argos/addons.yaml` registry | `argv: [... --max-stores {max_stores}]` sans `compare_by` | `argv: [plan --meals {meals}]` sans `compare_by` | `argv: [week ... --profile {profile}]` sans `compare_by` | **manquant** |

Vérifié par :
```bash
/home/jul/projects/Panier/.venv/bin/panier compare --help  # --compare-by [default: price]
/home/jul/projects/Panier/.venv/bin/panier week --help     # AVANT: [default: unit-price] -> APRES: [default: price]
/home/jul/projects/Panier/.venv/bin/panier plan --help     # [default: price]
grep -n "compare_by\|compare-by" src/panier/cli.py src/panier/drive.py src/panier/planner.py
```

---

## Patch appliqué

### 1. `src/panier/drive.py`

```diff
 def _strict_sorted_offers(
-    item: ShoppingItem, offers: list[StoreOffer], compare_by: str = "unit_price"
+    item: ShoppingItem, offers: list[StoreOffer], compare_by: str = "price"
 ) -> list[OfferScore]:
```

Cohérence avec `best_offer_for_item(compare_by="price")` et `_offer_compare_value(offer, compare_by)` qui fait `if compare_by == "unit_price" and offer.unit_price is not None: return unit_price else: return price`.

### 2. `src/panier/cli.py` — `week`

```diff
 def week(
     ...
-    compare_by: Annotated[str, typer.Option("--compare-by")] = "unit-price",
+    compare_by: Annotated[str, typer.Option("--compare-by")] = "price",
```

`plan` (l3291) et `compare` (l4025) déjà à `price` — inchangés. `normalize_compare_by()` gère déjà `price` / `unit-price` (avec tiret ou underscore).

### 3. Registry — `examples/addons.yaml` + `~/.config/argos/addons.yaml` (synchro)

**`plan`** — ajout `compare_by` :

```yaml
    - id: plan
      invoke:
        argv:
        - plan
        - --meals
        - '{meals}'
        - --format
        - json
        - --compare-by
        - '{compare_by}'
      params:
      - name: compare_by
        enum: [price, unit-price]
        default: price
        description: critère de comparaison panier (price ou unit-price)
```

**`week`** — ajout `compare_by` :

```yaml
        - --profile
        - '{profile}'
        - --compare-by
        - '{compare_by}'
      params:
      - name: compare_by
        enum: [price, unit-price]
        default: price
```

**`compare`** — ajout `compare_by` :

```yaml
        - --max-stores
        - '{max_stores}'
        - --compare-by
        - '{compare_by}'
      params:
      - name: compare_by
        enum: [price, unit-price]
        default: price
```

Fichiers sync : `examples/addons.yaml` (source) et `~/.config/argos/addons.yaml` (runtime) patchés à l'identique.

---

## Validations

### CLI --help (après patch, venv Panier)

```
panier compare --help  -> --compare-by <str> [default: price]  OK
panier week --help     -> --compare-by <str> [default: price]  OK  (était unit-price)
panier plan --help     -> --compare-by <str> [default: price]  OK
```

### `drive.py` cohérence

```
best_offer_for_item      default= price
_offer_compare_value     no default (branch unit_price -> unit_price else price)
_strict_sorted_offers    default= price  (était unit_price)
```

### Registry placeholders ⊆ params + enum

```
compare: placeholders=['shopping_list','mode','max_stores','compare_by'] params=[...,'compare_by'] missing=[] has_compare_by=True enum_ok=True -> OK
week:    placeholders=['meals','days','slots','max_repeats_per_week','shuffle_seed','mode','max_stores','profile','compare_by'] params=[...,'compare_by'] missing=[] -> OK
plan:    placeholders=['meals','compare_by'] params=['meals','compare_by'] missing=[] -> OK
```

Vérifié sur `examples/addons.yaml` **et** `~/.config/argos/addons.yaml`.

### YAML

```
examples/addons.yaml YAML OK
~/.config/argos/addons.yaml YAML OK
```

---

## Diff complet (panier strict, hors bruit argos)

```diff
diff --git a/src/panier/cli.py b/src/panier/cli.py
-    compare_by: Annotated[str, typer.Option("--compare-by")] = "unit-price",
+    compare_by: Annotated[str, typer.Option("--compare-by")] = "price",
diff --git a/src/panier/drive.py b/src/panier/drive.py
-    item: ShoppingItem, offers: list[StoreOffer], compare_by: str = "unit_price"
+    item: ShoppingItem, offers: list[StoreOffer], compare_by: str = "price"
```

Registry (bloc panier) : voir section Patch ci-dessus ; `git diff examples/addons.yaml` montre uniquement les ajouts `--compare-by`/`compare_by` pour `plan`/`week`/`compare` (le reste du diff argos est hors scope).

---

## Restes / risques

- Aucun — `plan` n'exposait pas `compare_by` avant : ajout non cassant (default `price` préserve le comportement existant).
- `week` change de default `unit-price` → `price` : impact voulu (uniformisation demandée). Un appel explicite `--compare-by unit-price` reste possible.
- Tests : `pytest` Panier non relancé ici (pas de suite dédiée `compare_by`) — recommandé `uv run pytest -q -k compare` si ajout futur.
