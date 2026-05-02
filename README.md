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
panier llm status
PANIER_NO_LLM=1 panier llm status
panier --no-llm explain item "Tomates concassées bio 400g"
panier pantry init
panier pantry add riz --quantity 150 --unit g --min 300g
panier pantry list
panier pantry need examples/chili.yaml
panier pantry consume examples/chili.yaml
panier shopping from-recipe examples/chili.yaml
panier recipe add examples/gratin-emmental.yaml
panier recipe list --tag budget
panier recipe list --balanced
panier recipe score "bowl quinoa thon légumes"
panier recipe show "gratin pâtes emmental"
panier recipe shopping "gratin pâtes emmental"
panier plan --meals 3 --include-tags budget,rapide --max-prep-minutes 20
panier plan --meals 3 --balanced
panier plan --meals 3 --use-pantry --prices examples/prices.yaml --max-stores 2
panier plan --meals 3 --collect leclerc,auchan --compare-by unit-price
panier drive plan examples/shopping-list.yaml --drive leclerc
panier drive open examples/shopping-list.yaml --drive leclerc --profile courses
panier drive collect examples/shopping-list.yaml --drive auchan --output offers.yaml
panier drive pick examples/shopping-list.yaml examples/prices.yaml
panier compare examples/emmental-rape.yaml --prices examples/prices-emmental-rape-leclerc-auchan.yaml --compare-by unit-price --max-stores 2
mkdir -p ~/.panier
cp examples/recipes.yaml ~/.panier/recipes.yaml
panier recipe list --balanced
panier recipe suggest --meals 3
panier week
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

### Recettes comme source primaire

Les recettes sont stockées par défaut dans `~/.panier/recipes.yaml`. Une base d'exemples plus complète est disponible dans `examples/recipes.yaml` : recettes simples, consolidables, avec tags utiles (`budget`, `rapide`, `equilibre`, `vegetarien`, `batch`, etc.). Le format YAML reste volontairement simple, avec `prep_minutes` et `cost_level` optionnels :

```yaml
- name: gratin pâtes emmental
  servings: 2
  prep_minutes: 20
  cost_level: budget
  tags: [rapide, four, budget]
  ingredients:
    - name: pâtes
      quantity: 250
      unit: g
    - name: emmental râpé
      quantity: 150
      unit: g
```

Commandes utiles :

```bash
panier recipe add examples/gratin-emmental.yaml
panier recipe list --tag budget
panier recipe show "gratin pâtes emmental"
panier recipe shopping "gratin pâtes emmental" "pâtes thon tomate"
panier recipe remove "gratin pâtes emmental"
```

`recipe shopping` et `plan` consolident les quantités avant achat : si deux recettes demandent `emmental râpé 100 g` et `emmental râpé 300 g`, la liste affiche `emmental râpé 400 g`. Les unités compatibles (`g/kg`, `ml/cl/l`) sont converties avant addition.

Option équilibre : Panier peut noter une recette avec un score simple et explicable, non médical, basé sur la présence de légumes, protéine, féculent, et sur des pénalités pour ingrédients très riches ou ultra-transformés.

```bash
panier recipe score "bowl quinoa thon légumes"
panier recipe list --balanced              # équivalent à --min-balance-score 70
panier recipe suggest --meals 3 --balanced
panier plan --meals 3 --balanced
panier plan --meals 3 --min-balance-score 60
```

Le flux principal devient :

```bash
panier plan --meals 3 \
  --include-tags budget,rapide \
  --exclude-tags four \
  --max-prep-minutes 20 \
  --use-pantry \
  --prices examples/prices.yaml \
  --compare-by unit-price
```

Et pour collecter directement les drives :

```bash
panier plan --meals 3 \
  --collect leclerc,auchan \
  --compare-by unit-price \
  --collect-output offers.yaml
```

Flux interne : recettes retenues → ingrédients consolidés → soustraction du stock → collecte drive optionnelle → comparaison → recommandation panier.

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

## Architecture déterministe d'abord

Panier privilégie un cœur déterministe : à entrées identiques (profil, recettes, stock, prix YAML/JSON et options CLI), la sélection de recettes, la liste consolidée, les requêtes drive et la recommandation panier doivent produire la même sortie à chaque exécution.

Concrètement :

- les recettes sont filtrées puis scorées par règles locales explicites, avec tri stable ;
- les quantités sont consolidées puis triées par nom et unité normalisés ;
- les requêtes drive sont générées par règles explicites (`build_drive_search_plan`) : nom canonique, marque commune, marque distributeur uniquement sur le drive compatible, sinon requête générique ;
- la comparaison panier trie les magasins et choisit une solution stable en cas d'égalité (un seul drive avant un split équivalent, puis noms de drives triés) ;
- sans `--collect`, Panier ne doit pas ouvrir de navigateur, appeler le réseau ou dépendre d'un LLM : les prix fournis localement sont la seule source d'offres.

## Déterminisme et garde-fou LLM

Panier fonctionne aujourd'hui en déterministe local-first : les commandes de planification, scoring, comparaison et explication n'appellent pas de LLM. Le garde-fou `PANIER_NO_LLM` est disponible pour verrouiller ce comportement avant d'éventuelles intégrations futures :

```bash
panier llm status
PANIER_NO_LLM=1 panier llm status
panier --no-llm llm status
```

`panier llm status` affiche le mode courant, la source du garde-fou et le fait qu'aucun appel LLM n'est implémenté. La variable accepte les valeurs usuelles (`1`, `true`, `yes`, `on` pour activer ; `0`, `false`, `no`, `off` pour désactiver).

Les surfaces d'explication sont également déterministes et sans réseau :

```bash
panier explain item "Tomates concassées bio 400g"
```

Cette commande montre l'entrée, le nom canonique, la requête de recherche locale et une confiance explicite basée sur de simples règles de normalisation. Le module reste minimal pour pouvoir être remplacé/complété par un catalogue produit local.

### Politique d'escalade LLM / réseau

Le flux normal doit rester explicable et rejouable. Une escalade non déterministe n'est acceptable que si l'utilisateur la demande explicitement ou si une commande dédiée la documente clairement :

1. utiliser les règles locales et les fichiers fournis ;
2. afficher les correspondances faibles ou manquantes au lieu de les inventer ;
3. collecter via Managed Browser uniquement avec `drive collect`, `drive open`, `plan --collect` ou `week --collect` ;
4. réserver un éventuel LLM à une étape assistée, auditable et optionnelle (ex. proposition de synonymes ou substitution), jamais à la décision silencieuse du panier final.

Cette approche facilite les tests snapshot/régression, évite les appels externes surprises et garde les recommandations justifiables.

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
