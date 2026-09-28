"""Tests vagues 2+3 JEV panier : rerank, substitution gatée, valeur, budget.

Contrat : OFF → disabled ; NO_LLM → disabled ; erreur → fallback jamais de
levée ; seuils gatés ; valeur ∈ [0,1] ; budget review-only (jamais de block).
ZÉRO réseau (evaluate mocké).
"""

from __future__ import annotations

from panier import jev_advice, jev_wave23


def fake(answers: dict) -> object:
    def _fake(state, questions):
        assert isinstance(state, str) and state
        return {"status": "ok", "answers": answers, "model": "mock", "cost": 0}

    return _fake


def test_off_by_default(monkeypatch):
    for var in ("PANIER_JEV_RERANK", "PANIER_JEV_SUB", "PANIER_JEV_VALUE", "PANIER_JEV_BUDGET"):
        monkeypatch.delenv(var, raising=False)
    assert jev_wave23.rerank_promos("lait", ["a"])["status"] == "disabled"
    assert jev_wave23.suggest_substitution("lait", ["b"])["status"] == "disabled"
    assert jev_wave23.score_basket_value({})["status"] == "disabled"
    assert jev_wave23.guard_budget({})["status"] == "disabled"


def test_no_llm_kill_switch(monkeypatch):
    monkeypatch.setenv("PANIER_JEV_RERANK", "1")
    monkeypatch.setenv("PANIER_NO_LLM", "1")
    assert jev_wave23.rerank_promos("lait", ["a"])["status"] == "disabled"


def test_rerank_sorts_desc(monkeypatch):
    monkeypatch.setenv("PANIER_JEV_RERANK", "1")
    monkeypatch.delenv("PANIER_NO_LLM", raising=False)
    monkeypatch.setattr(jev_advice, "evaluate",
                        fake({"r0": {"noul": 0.2}, "r1": {"noul": 0.9}}))
    out = jev_wave23.rerank_promos("lait bio", ["x", "y"])
    assert out["status"] == "ok"
    assert [p for p, _ in out["ranked"]] == ["y", "x"]


def test_sub_gated(monkeypatch):
    monkeypatch.setenv("PANIER_JEV_SUB", "1")
    monkeypatch.delenv("PANIER_NO_LLM", raising=False)
    monkeypatch.setattr(jev_advice, "evaluate",
                        fake({"sub": {"choice": "c0", "confidence": 0.9}}))
    out = jev_wave23.suggest_substitution("lait", ["lait AOP"])
    assert out["routing"] == "auto-substitute"
    monkeypatch.setattr(jev_advice, "evaluate",
                        fake({"sub": {"choice": "c0", "confidence": 0.6}}))
    out = jev_wave23.suggest_substitution("lait", ["lait AOP"])
    assert out["routing"] == "review"


def test_value_bounded(monkeypatch):
    monkeypatch.setenv("PANIER_JEV_VALUE", "1")
    monkeypatch.delenv("PANIER_NO_LLM", raising=False)
    monkeypatch.setattr(jev_advice, "evaluate", fake({
        "savings": {"score": 2.0}, "need": {"score": 3.0},
        "perishability": {"score": 1.0}, "preferences": {"score": 2.5}}))
    out = jev_wave23.score_basket_value({"items": "lait, pain", "total_eur": 12})
    assert out["status"] == "ok"
    assert 0.0 <= out["value_0_1"] <= 1.0


def test_budget_review_only(monkeypatch):
    monkeypatch.setenv("PANIER_JEV_BUDGET", "1")
    monkeypatch.delenv("PANIER_NO_LLM", raising=False)
    monkeypatch.setattr(jev_advice, "evaluate", fake({
        "likely_over_budget": {"noul": 0.8}, "impulse_heavy": {"noul": 0.2},
        "severity": {"score": 1.5}}))
    out = jev_wave23.guard_budget({"items": "x", "total_eur": 90, "budget_eur": 60})
    assert out["verdict"] == "review"
    assert "block" not in out["verdict"]


def test_fallback_never_raises(monkeypatch):
    monkeypatch.setenv("PANIER_JEV_SUB", "1")
    monkeypatch.delenv("PANIER_NO_LLM", raising=False)

    def _boom(state, questions):
        raise jev_advice.JevApiError("503")

    monkeypatch.setattr(jev_advice, "evaluate", _boom)
    out = jev_wave23.suggest_substitution("lait", ["x"])
    assert out["status"] == "fallback" and out["routing"] == "review"
