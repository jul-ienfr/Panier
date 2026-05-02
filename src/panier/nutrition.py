from __future__ import annotations

from dataclasses import dataclass

from panier.models import Recipe, normalize_name


@dataclass(frozen=True)
class BalanceScore:
    score: int
    verdict: str
    positives: tuple[str, ...]
    penalties: tuple[str, ...]


_PROTEIN_TERMS = {
    "boeuf",
    "bœuf",
    "dinde",
    "haricots",
    "haricots rouges",
    "lentilles",
    "oeuf",
    "oeufs",
    "œuf",
    "œufs",
    "pois chiches",
    "poisson",
    "poulet",
    "saumon",
    "thon",
    "tofu",
    "yaourt",
}
_VEGETABLE_TERMS = {
    "brocoli",
    "carotte",
    "carottes",
    "champignons",
    "courgette",
    "courgettes",
    "épinards",
    "haricots verts",
    "légumes",
    "oignons",
    "poireaux",
    "poivron",
    "poivrons",
    "salade",
    "tomates",
    "tomates concassées",
}
_STARCH_TERMS = {
    "boulgour",
    "pâtes",
    "pommes de terre",
    "quinoa",
    "riz",
    "semoule",
}
_FAT_OR_RICH_TERMS = {
    "beurre",
    "crème",
    "emmental râpé",
    "fromage",
    "huile",
    "lardons",
}
_ULTRA_PROCESSED_TERMS = {
    "cordon bleu",
    "nuggets",
    "sauce industrielle",
    "surimi",
}


def score_recipe_balance(recipe: Recipe) -> BalanceScore:
    """Return a simple, explainable balance score for a recipe.

    This is intentionally heuristic. It is not medical nutrition; it only helps rank
    recipes that contain vegetables, protein and starch while penalizing very rich or
    ultra-processed ingredient markers.
    """
    ingredient_names = {normalize_name(ingredient.name) for ingredient in recipe.ingredients}
    tags = {normalize_name(tag) for tag in recipe.tags}

    positives: list[str] = []
    penalties: list[str] = []
    score = 0

    if _contains_any(ingredient_names, _VEGETABLE_TERMS) or "legumes" in tags or "légumes" in tags:
        score += 35
        positives.append("légumes")
    else:
        penalties.append("pas de légume identifié")

    if _contains_any(ingredient_names, _PROTEIN_TERMS) or "proteine" in tags or "protéine" in tags:
        score += 25
        positives.append("protéine")
    else:
        penalties.append("pas de protéine identifiée")

    if _contains_any(ingredient_names, _STARCH_TERMS) or "feculent" in tags or "féculent" in tags:
        score += 20
        positives.append("féculent")

    if "vegetarien" in tags or "végétarien" in tags:
        score += 5
        positives.append("végétarien")

    rich_matches = _matched_terms(ingredient_names, _FAT_OR_RICH_TERMS)
    if rich_matches:
        penalty = min(20, 8 * len(rich_matches))
        score -= penalty
        penalties.append(f"riche: {', '.join(rich_matches)}")

    processed_matches = _matched_terms(ingredient_names, _ULTRA_PROCESSED_TERMS)
    if processed_matches:
        penalty = min(30, 15 * len(processed_matches))
        score -= penalty
        penalties.append(f"ultra-transformé: {', '.join(processed_matches)}")

    score = max(0, min(100, score))
    return BalanceScore(
        score=score,
        verdict=_verdict(score),
        positives=tuple(positives),
        penalties=tuple(penalties),
    )


def _contains_any(values: set[str], terms: set[str]) -> bool:
    return bool(_matched_terms(values, terms))


def _matched_terms(values: set[str], terms: set[str]) -> list[str]:
    matches: list[str] = []
    for term in sorted(terms):
        if any(term in value or value in term for value in values):
            matches.append(term)
    return matches


def _verdict(score: int) -> str:
    if score >= 70:
        return "équilibré"
    if score >= 45:
        return "correct"
    return "à compléter"
