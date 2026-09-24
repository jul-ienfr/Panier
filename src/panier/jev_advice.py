"""JEV / SystemOne advisory client (read-only, opt-in, fail-open).

Copie du pattern Mining CLI ``providers/jev.py``, adaptée à Panier : aucun
import mining/browser, aucune dépendance partagée (principe P7 : chaque
système dans son dépôt).

JEV n'est ni un LLM de chat ni un chemin chaud : évaluateur structuré
``POST {base}/v1/systemone`` via proxy :4000. Usage unique (opt-in,
fail-open) : départage consultatif entre offres proches
(``PANIER_JEV_CALIB``) — jamais d'écriture (ni catalogue, ni substitutions
YAML utilisateur, ni exclusions), jamais de changement d'ensemble, jamais
de SEND externe.

Garde-fous :
- ``PANIER_NO_LLM=1`` (ou toute valeur truthy) → JEV désactivé (statut
  ``disabled``) : le coupe-circuit déterministe prime sur l'opt-in.
- OFF (``PANIER_JEV_CALIB != 1``) → aucun appel réseau, baseline pure.
- Appelant (``drive._strict_sorted_offers``) : un seul appel JEV, uniquement
  quand le top-2 est proche (prix à 10 % près ET écart de score ≤ 0.15) ;
  swap du top-2 uniquement si confiance JEV ≥ 0.7, motif enrichi
  ``[jev:pick]`` sur la nouvelle tête (traçabilité, comme resell).

Variables d'environnement :
- ``PANIER_JEV_CALIB`` : ``1`` pour activer (défaut ``0`` = byte-identical).
- ``PANIER_NO_LLM`` : coupe-circuit (voir ``deterministic.py``).
- ``JEV_BASE_URL`` : défaut ``http://192.168.31.59:4000``.
- ``JEV_MODEL`` : défaut ``jev-1.13-free``.
- ``JEV_TIMEOUT`` : secondes, défaut ``10`` (court : jamais chemin chaud).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "jev-1.13-free"
DEFAULT_BASE_URL = "http://192.168.31.59:4000"
DEFAULT_TIMEOUT = 10

VALID_TYPES = frozenset({"noul", "choice", "score"})

# Env gate : OFF = byte-identical (aucun appel réseau, baseline pure).
CALIB_ENV_VAR = "PANIER_JEV_CALIB"

# Seuil de confiance minimal pour honorer le pick JEV (swap top-2).
MIN_PICK_CONFIDENCE = 0.7

# Cache mémoire borné : hash(state+questions) -> (answers, timestamp).
_CACHE: dict[str, tuple[dict[str, Any], float]] = {}
_CACHE_TTL_S = 300.0
_CACHE_MAX = 128


class JevApiError(Exception):
    """Erreur d'appel JEV (réseau, HTTP non-200, payload invalide)."""


def jev_calib_enabled() -> bool:
    """Gate opt-in : '1' uniquement, ET coupe-circuit PANIER_NO_LLM respecté."""
    if os.environ.get(CALIB_ENV_VAR, "0") != "1":
        return False
    try:
        from panier.deterministic import is_no_llm_enabled
    except ImportError:
        return True
    return not is_no_llm_enabled()


def _config() -> tuple[str, str, int]:
    base_url = (os.environ.get("JEV_BASE_URL", DEFAULT_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
    model = os.environ.get("JEV_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL
    try:
        timeout = int(os.environ.get("JEV_TIMEOUT", str(DEFAULT_TIMEOUT)))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    return base_url, model, max(1, timeout)


def validate_questions(questions: dict[str, Any]) -> None:
    """Validation locale du payload (évite les 400/422 avant d'appeler)."""
    if not isinstance(questions, dict) or not questions:
        raise ValueError("questions doit être un dict non vide")
    for qid, q in questions.items():
        if not isinstance(q, dict):
            raise ValueError(f"question '{qid}': dict attendu")
        qtype = q.get("type")
        if qtype not in VALID_TYPES:
            raise ValueError(f"question '{qid}': type inconnu {qtype!r} (noul|choice|score)")
        if not q.get("instructions"):
            raise ValueError(f"question '{qid}': instructions requises")
        criteria = q.get("criteria")
        if qtype == "choice":
            if not isinstance(criteria, dict) or not criteria:
                raise ValueError(f"question '{qid}': choice.criteria = dict label→valeur requis")
        elif qtype == "score":
            if not isinstance(criteria, list) or not criteria:
                raise ValueError(f"question '{qid}': score.criteria = liste de légendes requise")
        # noul : instructions seul, pas de criteria.


def _cache_key(model: str, state: str, questions: dict[str, Any]) -> str:
    raw = json.dumps({"m": model, "s": state, "q": questions}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def evaluate(state: str, questions: dict[str, Any]) -> dict[str, Any]:
    """Appelle JEV SystemOne, lève JevApiError sur toute erreur (fail-open appelant).

    Retourne ``{"model", "answers", "usage", "cost", "latency_ms"}``.
    """
    validate_questions(questions)
    base_url, model, timeout = _config()
    key = _cache_key(model, state, questions)
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit is not None and now - hit[1] < _CACHE_TTL_S:
        answers = hit[0]
        return {"model": model, "answers": answers, "usage": {}, "cost": "0",
                "latency_ms": 0, "cached": True, "baseline_kept": True}
    body = json.dumps({"model": model, "state": state, "questions": questions}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/v1/systemone", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            status = getattr(resp, "status", 200)
    except Exception as exc:
        raise JevApiError(f"JEV unreachable ({base_url}): {exc}") from exc
    latency_ms = int((time.monotonic() - t0) * 1000)
    if status != 200:
        raise JevApiError(f"JEV HTTP {status}: {raw[:200]}")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JevApiError(f"JEV réponse non-JSON: {exc}") from exc
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        raise JevApiError("JEV réponse sans answers{}")
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = (answers, now)
    return {
        "model": payload.get("model", model),
        "answers": answers,
        "usage": payload.get("usage", {}),
        "cost": str(payload.get("cost", "0")),
        "latency_ms": latency_ms,
        "cached": False,
        "baseline_kept": True,
    }


def _offer_label(offer_summary: dict[str, Any]) -> str:
    parts = [str(offer_summary.get("product", "?"))]
    for k in ("price", "unit_price", "score", "reason"):
        v = offer_summary.get(k)
        if v is not None:
            parts.append(f"{k}={v}")
    return " | ".join(parts)[:200]


def advise_best_offer(
    item_name: str,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Second avis JEV (choice) sur le top des offres proches. Fail-open total.

    Préconditions (validées par l'appelant ``drive``) : ≥2 offres, top-2
    proche (prix + score). Retourne toujours un dict ``{"status",
    "picked_index"|None, "advice", "baseline_kept": True}`` ; ne lève jamais
    (toute erreur → ``status: fallback`` + baseline).
    """
    out: dict[str, Any] = {
        "status": "fallback",
        "picked_index": None,
        "advice": None,
        "baseline_kept": True,
    }
    if not jev_calib_enabled():
        out["status"] = "disabled"
        return out
    if len(candidates) < 2:
        out["status"] = "skipped_single"
        return out
    try:
        options = {f"c{i}": _offer_label(c) for i, c in enumerate(candidates)}
        state = (
            "Comparateur drive : le score déterministe local a classé "
            f"{len(candidates)} offres proches pour l'article « {item_name} ». "
            "Choisir l'offre au meilleur rapport qualité-prix effectif "
            "(pertinence produit d'abord, prix ensuite)."
        )
        res = evaluate(state, {"pick": {
            "type": "choice",
            "instructions": "Quelle offre choisir pour cet article ?",
            "criteria": options,
        }})
        answer = (res.get("answers") or {}).get("pick", {})
        choice = answer.get("choice")
        idx = None
        if isinstance(choice, str) and choice.startswith("c") and choice[1:].isdigit():
            idx = int(choice[1:])
        try:
            confidence = float(answer.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if idx is not None and 0 <= idx < len(candidates) and confidence >= MIN_PICK_CONFIDENCE:
            out["status"] = "ok"
            out["picked_index"] = idx
            out["advice"] = {
                "model": res.get("model"),
                "choice": choice,
                "confidence": answer.get("confidence"),
                "probabilities": answer.get("probabilities"),
                "usage": res.get("usage"),
                "cost": res.get("cost"),
                "latency_ms": res.get("latency_ms"),
                "cached": res.get("cached", False),
            }
        elif idx is None or not (0 <= idx < len(candidates)):
            out["status"] = "unparseable"
        else:
            out["status"] = "low_confidence"
    except (JevApiError, ValueError) as exc:
        logger.info("JEV panier fallback (baseline souveraine): %s", exc)
        out["status"] = "fallback"
        out["error"] = str(exc)[:200]
    except Exception as exc:  # never raises : garde-fou ultime
        logger.warning("JEV panier inattendu, baseline: %s", exc)
        out["status"] = "fallback"
        out["error"] = f"{type(exc).__name__}: {str(exc)[:150]}"
    return out
