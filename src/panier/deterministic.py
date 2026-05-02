from __future__ import annotations

import os
import re
from dataclasses import dataclass

from panier.models import normalize_name

NO_LLM_ENV_VAR = "PANIER_NO_LLM"
_TRUE_VALUES = {"1", "true", "yes", "on", "y", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "off", "n", "disabled", ""}

_ITEM_STOPWORDS = {
    "de",
    "du",
    "des",
    "le",
    "la",
    "les",
    "l",
    "d",
    "au",
    "aux",
    "et",
    "a",
    "à",
    "avec",
    "sans",
    "pour",
    "bio",
    "frais",
    "fraîche",
    "nature",
}

_QUERY_STOPWORDS = _ITEM_STOPWORDS - {"bio", "sans"}

_EQUIVALENCES = {
    "bœuf": "boeuf",
    "oeuf": "œuf",
}


@dataclass(frozen=True)
class LLMStatus:
    no_llm: bool
    source: str
    raw_value: str | None

    @property
    def mode(self) -> str:
        return "deterministic" if self.no_llm else "deterministic-first"


@dataclass(frozen=True)
class ItemExplanation:
    input_name: str
    canonical_name: str
    query: str
    confidence: str
    reason: str


def env_flag_enabled(name: str, environ: dict[str, str] | None = None) -> tuple[bool, str | None]:
    """Return a deterministic boolean interpretation for environment feature flags."""
    values = os.environ if environ is None else environ
    raw_value = values.get(name)
    if raw_value is None:
        return False, None
    normalized = raw_value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True, raw_value
    if normalized in _FALSE_VALUES:
        return False, raw_value
    return True, raw_value


def no_llm_status(environ: dict[str, str] | None = None, *, cli_no_llm: bool = False) -> LLMStatus:
    env_enabled, raw_value = env_flag_enabled(NO_LLM_ENV_VAR, environ)
    if cli_no_llm:
        return LLMStatus(no_llm=True, source="--no-llm", raw_value=raw_value)
    if raw_value is not None:
        return LLMStatus(no_llm=env_enabled, source=NO_LLM_ENV_VAR, raw_value=raw_value)
    return LLMStatus(no_llm=False, source="default", raw_value=None)


def is_no_llm_enabled(environ: dict[str, str] | None = None) -> bool:
    return no_llm_status(environ).no_llm


def require_llm_allowed(environ: dict[str, str] | None = None) -> None:
    """Guard future LLM call sites behind PANIER_NO_LLM.

    Panier currently has no LLM-backed code paths. This helper is intentionally small and
    dependency-free so future integrations can call it before any remote/model invocation.
    """
    if is_no_llm_enabled(environ):
        raise RuntimeError(f"LLM calls disabled by {NO_LLM_ENV_VAR}")


def canonical_item_name(name: str) -> str:
    normalized = normalize_name(name)
    normalized = re.sub(r"\([^)]*\)", " ", normalized)
    quantity_pattern = r"\b\d+(?:[,.]\d+)?\s*(?:g|kg|ml|cl|l|litres?|pi[eè]ces?|pcs?)\b"
    normalized = re.sub(quantity_pattern, " ", normalized)
    normalized = normalized.replace("’", "'")
    for source, target in _EQUIVALENCES.items():
        normalized = normalized.replace(source, target)
    tokens = [token for token in re.split(r"[\s,;:/+-]+", normalized) if token]
    kept = [token for token in tokens if token not in _ITEM_STOPWORDS]
    if not kept:
        kept = tokens
    return " ".join(kept) or normalize_name(name)


def deterministic_query(canonical_name: str) -> str:
    tokens = [token for token in canonical_name.split() if token not in _QUERY_STOPWORDS]
    return " ".join(tokens) or canonical_name


def explain_item(name: str) -> ItemExplanation:
    canonical = canonical_item_name(name)
    query = deterministic_query(canonical)
    normalized = normalize_name(name)
    if canonical == normalized and query == canonical:
        confidence = "medium"
        reason = "normalisation exacte sans catalogue local"
    elif query == canonical:
        confidence = "medium"
        reason = "normalisation locale déterministe"
    else:
        confidence = "low"
        reason = "requête simplifiée par règles locales, sans catalogue produit"
    return ItemExplanation(
        input_name=name,
        canonical_name=canonical,
        query=query,
        confidence=confidence,
        reason=reason,
    )
