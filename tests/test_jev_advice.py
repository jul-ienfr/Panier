"""Unit tests for panier/jev_advice (SystemOne advisory, opt-in, fail-open).

Contrat :
- OFF (PANIER_JEV_CALIB != 1) = aucun appel réseau, advise → disabled.
- PANIER_NO_LLM actif → désactivé même si PANIER_JEV_CALIB=1 (coupe-circuit).
- advise_best_offer ne lève jamais (fallback + baseline_kept).
- validate_questions refuse criteria du mauvais type (422 évité en local).
- _strict_sorted_offers OFF = byte-identical (même ordre, aucun motif jev).
- ON + top-2 proche + confiance ≥ 0.7 → swap top-2, motif [jev:pick].
- ON + confiance < 0.7 / top-2 éloigné / 503 → baseline inchangée.
"""

from __future__ import annotations

import pytest

from panier import jev_advice
from panier.drive import _strict_sorted_offers
from panier.models import ShoppingItem, StoreOffer


def _item() -> ShoppingItem:
    return ShoppingItem(name="lait entier")


def _offer(product: str, price: float) -> StoreOffer:
    return StoreOffer(store="leclerc", item="lait entier", product=product,
                      price=price, confidence="medium")


def test_validate_rejects_bad_criteria():
    with pytest.raises(ValueError):
        jev_advice.validate_questions({"q": {"type": "choice", "instructions": "?", "criteria": "x"}})
    with pytest.raises(ValueError):
        jev_advice.validate_questions({})


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("PANIER_JEV_CALIB", raising=False)
    monkeypatch.delenv("PANIER_NO_LLM", raising=False)
    assert jev_advice.jev_calib_enabled() is False
    out = jev_advice.advise_best_offer("lait", [{"product": "a"}, {"product": "b"}])
    assert out["status"] == "disabled"
    assert out["picked_index"] is None
    assert out["baseline_kept"] is True


def test_no_llm_overrides_opt_in(monkeypatch):
    monkeypatch.setenv("PANIER_JEV_CALIB", "1")
    monkeypatch.setenv("PANIER_NO_LLM", "1")
    assert jev_advice.jev_calib_enabled() is False
    out = jev_advice.advise_best_offer("lait", [{"product": "a"}, {"product": "b"}])
    assert out["status"] == "disabled"


def test_single_skipped(monkeypatch):
    monkeypatch.setenv("PANIER_JEV_CALIB", "1")
    monkeypatch.delenv("PANIER_NO_LLM", raising=False)
    out = jev_advice.advise_best_offer("lait", [{"product": "a"}])
    assert out["status"] == "skipped_single"


def test_network_error_fallback(monkeypatch):
    monkeypatch.setenv("PANIER_JEV_CALIB", "1")
    monkeypatch.delenv("PANIER_NO_LLM", raising=False)
    monkeypatch.setattr(
        jev_advice, "evaluate",
        lambda *a, **k: (_ for _ in ()).throw(jev_advice.JevApiError("503 pool")),
    )
    out = jev_advice.advise_best_offer("lait", [{"product": "a"}, {"product": "b"}])
    assert out["status"] == "fallback"
    assert out["picked_index"] is None
    assert out["baseline_kept"] is True


def test_ok_high_confidence_picks(monkeypatch):
    monkeypatch.setenv("PANIER_JEV_CALIB", "1")
    monkeypatch.delenv("PANIER_NO_LLM", raising=False)
    monkeypatch.setattr(jev_advice, "evaluate", lambda *a, **k: {
        "model": "jev-1.13-free", "answers": {"pick": {"choice": "c1", "confidence": 0.85}},
        "usage": {}, "cost": "0", "latency_ms": 5, "cached": False, "baseline_kept": True})
    out = jev_advice.advise_best_offer("lait", [{"product": "a"}, {"product": "b"}])
    assert out["status"] == "ok"
    assert out["picked_index"] == 1


def test_ok_low_confidence_keeps_baseline(monkeypatch):
    monkeypatch.setenv("PANIER_JEV_CALIB", "1")
    monkeypatch.delenv("PANIER_NO_LLM", raising=False)
    monkeypatch.setattr(jev_advice, "evaluate", lambda *a, **k: {
        "model": "jev-1.13-free", "answers": {"pick": {"choice": "c1", "confidence": 0.4}},
        "usage": {}, "cost": "0", "latency_ms": 5, "cached": False, "baseline_kept": True})
    out = jev_advice.advise_best_offer("lait", [{"product": "a"}, {"product": "b"}])
    assert out["status"] == "low_confidence"
    assert out["picked_index"] is None


def test_sorted_off_is_byte_identical(monkeypatch):
    """OFF : même ordre que la baseline, aucun motif jev."""
    monkeypatch.delenv("PANIER_JEV_CALIB", raising=False)
    ranked = _strict_sorted_offers(_item(), [
        _offer("lait entier 1l", 1.10),
        _offer("lait entier bio 1l", 1.12),
    ])
    assert len(ranked) == 2
    assert all("[jev:" not in s.reason for s in ranked)
    assert ranked[0].offer.price <= ranked[1].offer.price


def test_sorted_on_close_swaps_on_confident_pick(monkeypatch):
    monkeypatch.setenv("PANIER_JEV_CALIB", "1")
    monkeypatch.delenv("PANIER_NO_LLM", raising=False)
    monkeypatch.setattr(jev_advice, "evaluate", lambda *a, **k: {
        "model": "jev-1.13-free", "answers": {"pick": {"choice": "c1", "confidence": 0.9}},
        "usage": {}, "cost": "0", "latency_ms": 5, "cached": False, "baseline_kept": True})
    ranked = _strict_sorted_offers(_item(), [
        _offer("lait entier 1l", 1.10),
        _offer("lait entier bio 1l", 1.12),
    ])
    assert ranked[0].offer.product == "lait entier bio 1l"
    assert "[jev:pick]" in ranked[0].reason


def test_sorted_on_far_prices_no_call(monkeypatch):
    """Top-2 éloigné en prix : aucun appel JEV même ON."""
    monkeypatch.setenv("PANIER_JEV_CALIB", "1")
    monkeypatch.delenv("PANIER_NO_LLM", raising=False)
    called = []
    monkeypatch.setattr(jev_advice, "advise_best_offer",
                        lambda *a, **k: called.append(1) or {"status": "ok", "picked_index": 1})
    ranked = _strict_sorted_offers(_item(), [
        _offer("lait entier 1l", 1.10),
        _offer("lait entier bio 1l", 2.50),
    ])
    assert called == []
    assert ranked[0].offer.price == 1.10


def test_sorted_on_fallback_keeps_baseline(monkeypatch):
    monkeypatch.setenv("PANIER_JEV_CALIB", "1")
    monkeypatch.delenv("PANIER_NO_LLM", raising=False)
    monkeypatch.setattr(
        jev_advice, "evaluate",
        lambda *a, **k: (_ for _ in ()).throw(jev_advice.JevApiError("503 pool")),
    )
    ranked = _strict_sorted_offers(_item(), [
        _offer("lait entier 1l", 1.10),
        _offer("lait entier bio 1l", 1.12),
    ])
    assert ranked[0].offer.price == 1.10
    assert all("[jev:" not in s.reason for s in ranked)


def test_sorted_no_llm_keeps_baseline(monkeypatch):
    monkeypatch.setenv("PANIER_JEV_CALIB", "1")
    monkeypatch.setenv("PANIER_NO_LLM", "1")
    called = []
    monkeypatch.setattr(jev_advice, "advise_best_offer",
                        lambda *a, **k: called.append(1) or {"status": "ok", "picked_index": 1})
    ranked = _strict_sorted_offers(_item(), [
        _offer("lait entier 1l", 1.10),
        _offer("lait entier bio 1l", 1.12),
    ])
    assert called == []
    assert ranked[0].offer.price == 1.10
