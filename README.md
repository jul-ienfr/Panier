# Panier

CLI de planification repas et courses optimisées multi-drive avec préférences, allergies et budget.

Objectif : transformer des préférences alimentaires, allergies, recettes et listes de courses en paniers comparables entre drives, avec une recommandation simple : tout commander au même endroit ou séparer uniquement si l'économie vaut la friction.

## MVP

- Profil alimentaire : allergies, interdits, aliments non aimés, aliments aimés.
- Recettes locales : ingrédients + exclusions automatiques selon profil.
- Plan repas : choisir N recettes compatibles.
- Liste consolidée : fusionner les ingrédients nécessaires.
- Stock local : soustraire ce qui est déjà dans le placard (`pantry.yaml`).
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
panier pantry add riz --quantity 150 --unit g
panier pantry list
cp examples/recipes.yaml ~/.panier/recipes.yaml
panier recipe suggest --meals 3
panier plan --meals 3
panier plan --meals 2 --prices examples/prices.yaml --max-stores 2
panier compare examples/shopping-list.yaml --prices examples/prices.yaml --max-stores 2
```

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
