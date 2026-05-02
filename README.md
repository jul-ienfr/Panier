# Panier

CLI de planification repas et courses optimisées multi-drive avec préférences, allergies et budget.

Objectif : transformer des préférences alimentaires, allergies, recettes et listes de courses en paniers comparables entre drives, avec une recommandation simple : tout commander au même endroit ou séparer uniquement si l'économie vaut la friction.

## MVP

- Profil alimentaire : allergies, interdits, aliments non aimés, aliments aimés.
- Recettes locales : ingrédients + exclusions automatiques selon profil.
- Plan repas : choisir N recettes compatibles.
- Liste consolidée : fusionner les ingrédients nécessaires.
- Stock local : soustraire, consommer et surveiller ce qui est déjà dans le placard (`pantry.yaml`).
- Comparaison panier : comparer des prix fournis en YAML/JSON, en mode `simple`, `economic` ou `hybrid`.

## Installation dev

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

## Exemples

```bash
panier profile init
panier profile dislike add oignons
panier profile allergy add arachides
panier pantry init
panier pantry add riz --quantity 150 --unit g --min 300g
panier pantry list
panier pantry need examples/chili.yaml
panier pantry consume examples/chili.yaml
panier shopping from-recipe examples/chili.yaml
panier drive plan examples/shopping-list.yaml --drive leclerc
panier drive open examples/shopping-list.yaml --drive leclerc --profile courses
panier drive collect examples/shopping-list.yaml --drive auchan --output offers.yaml
panier drive pick examples/shopping-list.yaml examples/prices.yaml
cp examples/recipes.yaml ~/.panier/recipes.yaml
panier recipe suggest --meals 3
panier plan --meals 3
panier plan --meals 2 --prices examples/prices.yaml --max-stores 2
panier compare examples/shopping-list.yaml --prices examples/prices.yaml --max-stores 2
```

### Drive / produits

Les repos de référence Leclerc montrent deux briques utiles intégrées côté CLI :

```bash
panier drive plan examples/shopping-list.yaml --drive leclerc   # requêtes à lancer sur le drive
panier drive pick examples/shopping-list.yaml examples/prices.yaml
```

`drive plan` prépare des termes de recherche stables, avec règles de marque distributeur.
`drive open` ouvre ces recherches via le Managed Browser local au lieu de scraper directement le site :

```bash
panier drive open examples/shopping-list.yaml --drive leclerc --profile panier
```

Par défaut, Panier appelle le wrapper local :

```bash
node /home/jul/tools/camofox-browser/scripts/managed-browser.js ... --json
```

La commande peut être surchargée avec `PANIER_MANAGED_BROWSER_COMMAND` ou `--browser-command`.
`drive pick` choisit ensuite le meilleur produit parmi des offres collectées : correspondance du type demandé d’abord, puis prix.

### Stock / pantry

Commandes dédiées :

```bash
panier pantry init
panier pantry add riz --quantity 500 --unit g --min 300g
panier pantry need examples/chili.yaml      # affiche ce qu’il manque pour la recette
panier pantry consume examples/chili.yaml   # décrémente le stock, signale les manques
panier shopping from-recipe examples/chili.yaml
```

Les unités compatibles sont normalisées pour les bases courantes : `g/kg` et `ml/cl/l`.
Les unités métier (`boîte`, `pièce`, etc.) restent comparées telles quelles.
Les seuils `--min` déclenchent une alerte de réachat quand le stock passe dessous.

## Philosophie

Panier n'est pas juste un comparateur de drives. Le flux cible est :

```text
profil + allergies + stock + recettes
=> liste de courses consolidée
=> correspondances produits par drive
=> comparaison prix / simplicité
=> recommandation panier
```

Règles importantes :

- jamais d'allergène ou d'ingrédient interdit dans une recommandation ;
- ne pas séparer entre plusieurs drives pour une économie ridicule ;
- afficher les substitutions incertaines plutôt que les valider silencieusement ;
- apprendre progressivement depuis les retours utilisateur, sans questionnaire géant au démarrage.
