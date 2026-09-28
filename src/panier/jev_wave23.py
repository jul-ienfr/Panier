"""JEV / SystemOne vagues 2+3 Panier : rerank, substitution gatée, valeur, budget.

Même doctrine que :mod:`panier.jev_advice` (dont ce module réutilise
``evaluate``/``JevApiError``, sans aucun import croisé entre dépôts) :
- Chaque nouveauté = NOUVEAU flag env ``=0`` par défaut (OFF = byte-identical,
  zéro appel réseau), ET coupe-circuit ``PANIER_NO_LLM`` respecté (→ disabled).
- Fail-open total : toute erreur → fallback baseline, jamais de levée.
- Vague 2 : substitution gatée (auto seulement si conf ≥ 0.85, sinon review).
- Vague 3 : valeur composite + garde budget, lecture seule.
- JAMAIS d'écriture (ni catalogue, ni substitutions YAML utilisateur, ni
  panier), jamais de changement d'ensemble, jamais de SEND externe.
- Traçabilité : motifs ``[jev:...]``. Questions EN ANGLAIS, state JSON.

Contenu :
- ``PANIER_JEV_RERANK`` : :func:`rerank_promos` (1 noul par paire besoin↔promo,
  UN appel fan-out → tri desc suggéré, affiché à côté du tri baseline).
- ``PANIER_JEV_SUB`` : :func:`suggest_substitution` (choice substitute/wait/drop
  + routing en CODE : auto seulement si conf ≥ 0.85).
- ``PANIER_JEV_VALUE`` : :func:`score_basket_value` (4 scores → combinés en CODE,
  poids configurables, lecture seule).
- ``PANIER_JEV_BUDGET`` : :func:`guard_budget` (nouls + severity → pass/review,
  review-only, jamais de block auto).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from panier import jev_advice

logger = logging.getLogger(__name__)

RERANK_ENV_VAR = "PANIER_JEV_RERANK"
SUB_ENV_VAR = "PANIER_JEV_SUB"
VALUE_ENV_VAR = "PANIER_JEV_VALUE"
BUDGET_ENV_VAR = "PANIER_JEV_BUDGET"

#: Garde-fou fan-out rerank : borne les paires par appel unique.
RERANK_MAX_PAIRS = 20

#: Substitution : confiance minimale pour un routage auto (sinon review).
SUB_AUTO_CONFIDENCE = 0.85

#: Poids valeur composite (somme 1, ajustables sans réentraîner).
VALUE_WEIGHTS = {"savings": 0.35, "need": 0.30, "perishability": 0.15, "preferences": 0.20}

#: Légendes scores valeur (EN, 0-3).
VALUE_LEGENDS = {
    "savings": ["no saving", "small saving", "good saving", "exceptional saving"],
    "need": ["unneeded", "nice to have", "needed", "essential"],
    "perishability": ["durable", "slow perish", "fast perish", "immediate use"],
    "preferences": ["against prefs", "neutral", "liked", "strongly preferred"],
}

#: Seuils guard budget (cookbook guardrails).
BUDGET_REVIEW_MIN = 0.35


def _gate(var: str) -> bool:
    """Gate opt-in + coupe-circuit NO_LLM (documenté, fail-open)."""
    if os.environ.get(var, "0") != "1":
        return False
    try:
        from panier.deterministic import is_no_llm_enabled
    except ImportError:
        return True
    return not is_no_llm_enabled()


def jev_rerank_enabled() -> bool:
    return _gate(RERANK_ENV_VAR)


def jev_sub_enabled() -> bool:
    return _gate(SUB_ENV_VAR)


def jev_value_enabled() -> bool:
    return _gate(VALUE_ENV_VAR)


def jev_budget_enabled() -> bool:
    return _gate(BUDGET_ENV_VAR)


def _as_prob(answer: Any) -> float | None:
    if isinstance(answer, bool) or not isinstance(answer, dict):
        return None
    raw = answer.get("noul")
    if isinstance(raw, bool):
        return 1.0 if raw else 0.0
    if isinstance(raw, (int, float)):
        return max(0.0, min(1.0, float(raw)))
    return None


def _confidence_of(answer: Any) -> float | None:
    if not isinstance(answer, dict):
        return None
    conf = answer.get("confidence")
    if isinstance(conf, bool) or not isinstance(conf, (int, float)):
        return None
    return max(0.0, min(1.0, float(conf)))


def _fail(exc: Exception) -> dict[str, Any]:
    logger.info("JEV panier vague 2+3 fallback (baseline) : %s", exc)
    return {"status": "fallback", "baseline_kept": True,
            "error": f"{type(exc).__name__}: {str(exc)[:150]}"}


def rerank_promos(need: str, promos: list[str]) -> dict[str, Any]:
    """Tri promos suggéré (noul fan-out), lecture seule, jamais de levée."""
    out: dict[str, Any] = {"status": "fallback", "ranked": [], "baseline_kept": True}
    if not jev_rerank_enabled():
        out["status"] = "disabled"
        return out
    if not promos:
        out["status"] = "skipped_empty"
        return out
    try:
        shortlist = list(promos[:RERANK_MAX_PAIRS])
        questions = {
            f"r{i}": {
                "type": "noul",
                "instructions": "Is this promotion relevant for this need?",
                "criteria": {"true": "promotion matches the need", "false": "promotion irrelevant"},
            }
            for i in range(len(shortlist))
        }
        state = json.dumps({"need": need[:500], "count": len(shortlist)}, ensure_ascii=False)
        res = jev_advice.evaluate(state, questions)
        answers = res.get("answers") or {}
        scored = []
        for i, promo in enumerate(shortlist):
            prob = _as_prob(answers.get(f"r{i}"))
            if prob is None:
                out["status"] = "unparseable"
                return out
            scored.append((promo, prob))
        order = sorted(range(len(scored)), key=lambda i: (-scored[i][1], i))
        out["status"] = "ok"
        out["ranked"] = [(scored[i][0], scored[i][1]) for i in order]
        out["advice"] = {"model": res.get("model"), "cost": res.get("cost"),
                         "latency_ms": res.get("latency_ms"), "cached": res.get("cached", False)}
    except (jev_advice.JevApiError, ValueError) as exc:
        out.update(_fail(exc))
    except Exception as exc:
        logger.warning("JEV rerank promos inattendu : %s", exc)
        out.update(_fail(exc))
    return out


def suggest_substitution(missing_item: str, substitutes: list[str]) -> dict[str, Any]:
    """Substitution gatée : auto seulement si conf ≥ 0.85, sinon review.

    Ne modifie JAMAIS le panier/catalogue : retourne suggestion + routing,
    l'appelant décide (et loggue ``[jev:sub:<routing>]``).
    """
    out: dict[str, Any] = {"status": "fallback", "suggestion": None,
                           "routing": "review", "baseline_kept": True}
    if not jev_sub_enabled():
        out["status"] = "disabled"
        return out
    try:
        options = {f"c{i}": s[:200] for i, s in enumerate(substitutes[:8])}
        options["wait"] = "wait for restock"
        options["drop"] = "drop the item"
        state = json.dumps({"missing": missing_item[:300], "n_substitutes": len(options) - 2},
                           ensure_ascii=False)
        res = jev_advice.evaluate(state, {"sub": {
            "type": "choice",
            "instructions": "The item is unavailable. Substitute with a listed alternative, wait, or drop?",
            "criteria": options,
        }})
        answer = (res.get("answers") or {}).get("sub", {})
        choice = answer.get("choice")
        conf = _confidence_of(answer) or 0.0
        if not isinstance(choice, str) or not choice:
            out["status"] = "unparseable"
            return out
        auto = choice.startswith("c") and conf >= SUB_AUTO_CONFIDENCE
        out["status"] = "ok"
        out["suggestion"] = choice
        out["confidence"] = conf
        out["routing"] = "auto-substitute" if auto else "review"
        out["advice"] = {"model": res.get("model"), "cost": res.get("cost"),
                         "latency_ms": res.get("latency_ms"), "cached": res.get("cached", False)}
    except (jev_advice.JevApiError, ValueError) as exc:
        out.update(_fail(exc))
    except Exception as exc:
        logger.warning("JEV substitution inattendue : %s", exc)
        out.update(_fail(exc))
    return out


def score_basket_value(basket_state: dict[str, Any]) -> dict[str, Any]:
    """Score valeur composite (4 scores → combinés en CODE), lecture seule."""
    out: dict[str, Any] = {"status": "fallback", "value_0_1": None, "baseline_kept": True}
    if not jev_value_enabled():
        out["status"] = "disabled"
        return out
    try:
        items = str(basket_state.get("items", ""))[:800]
        total = basket_state.get("total_eur")
        questions = {
            name: {"type": "score",
                   "instructions": f"Rate the {name} dimension of this basket.",
                   "criteria": VALUE_LEGENDS[name]}
            for name in VALUE_WEIGHTS
        }
        state = json.dumps({"items": items, "total_eur": total}, ensure_ascii=False)
        res = jev_advice.evaluate(state, questions)
        answers = res.get("answers") or {}
        parts = {}
        for name in VALUE_WEIGHTS:
            raw = answers.get(name) if isinstance(answers.get(name), dict) else None
            score = raw.get("score") if raw else None
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                out["status"] = "unparseable"
                return out
            parts[name] = max(0.0, min(3.0, float(score))) / 3.0
        out["status"] = "ok"
        out["parts"] = parts
        out["value_0_1"] = sum(parts[k] * VALUE_WEIGHTS[k] for k in VALUE_WEIGHTS)
        out["advice"] = {"model": res.get("model"), "cost": res.get("cost"),
                         "latency_ms": res.get("latency_ms"), "cached": res.get("cached", False)}
    except (jev_advice.JevApiError, ValueError) as exc:
        out.update(_fail(exc))
    except Exception as exc:
        logger.warning("JEV valeur inattendue : %s", exc)
        out.update(_fail(exc))
    return out


def guard_budget(basket_state: dict[str, Any]) -> dict[str, Any]:
    """Garde budget (nouls + severity → pass/review), review-only, jamais de block."""
    out: dict[str, Any] = {"status": "fallback", "verdict": None, "baseline_kept": True}
    if not jev_budget_enabled():
        out["status"] = "disabled"
        return out
    try:
        state = json.dumps({"items": str(basket_state.get("items", ""))[:800],
                            "total_eur": basket_state.get("total_eur"),
                            "budget_eur": basket_state.get("budget_eur")}, ensure_ascii=False)
        res = jev_advice.evaluate(state, {
            "likely_over_budget": {
                "type": "noul",
                "instructions": "Is this basket likely to exceed its budget?",
                "criteria": {"true": "over budget", "false": "within budget"},
            },
            "impulse_heavy": {
                "type": "noul",
                "instructions": "Is this basket heavy with impulse purchases?",
                "criteria": {"true": "impulse heavy", "false": "planned purchases"},
            },
            "severity": {
                "type": "score",
                "instructions": "How severe is the budget risk?",
                "criteria": ["none", "watch", "overrun likely", "severe overrun"],
            },
        })
        answers = res.get("answers") or {}
        over = _as_prob(answers.get("likely_over_budget"))
        impulse = _as_prob(answers.get("impulse_heavy"))
        sev_raw = answers.get("severity") if isinstance(answers.get("severity"), dict) else None
        sev = sev_raw.get("score") if sev_raw else None
        if over is None or impulse is None or not isinstance(sev, (int, float)):
            out["status"] = "unparseable"
            return out
        verdict = "review" if (max(over, impulse) >= BUDGET_REVIEW_MIN or sev >= 1) else "pass"
        out["status"] = "ok"
        out["verdict"] = verdict
        out["needs_review"] = verdict != "pass"
        out["advice"] = {"model": res.get("model"), "cost": res.get("cost"),
                         "latency_ms": res.get("latency_ms"), "cached": res.get("cached", False)}
    except (jev_advice.JevApiError, ValueError) as exc:
        out.update(_fail(exc))
    except Exception as exc:
        logger.warning("JEV budget inattendu : %s", exc)
        out.update(_fail(exc))
    return out
