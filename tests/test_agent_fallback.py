from __future__ import annotations

import json

from panier import agent_fallback as af
from panier import managed_browser_shim as shim
from panier.cart import CartLine


def _line() -> CartLine:
    return CartLine(store="leclerc", item="lait", product="lait bio 1L", quantity=2,
                    url="https://example.test/p/lait")


def test_goal_mentions_product_never_checkout() -> None:
    goal = af.cart_goal("leclerc", _line())
    assert "lait bio 1L" in goal
    assert "do not check out" in goal


def test_expected_targets_cart_url() -> None:
    assert "panier" in af.cart_expected("leclerc")["urlContains"]
    assert "panier" in af.cart_expected("auchan")["urlContains"]


def test_fallback_opt_in_default_off(monkeypatch) -> None:
    monkeypatch.delenv("PANIER_AGENT_FALLBACK", raising=False)
    assert af.agent_fallback_enabled() is False
    monkeypatch.setenv("PANIER_AGENT_FALLBACK", "1")
    assert af.agent_fallback_enabled() is True
    assert af.agent_fallback_enabled(explicit=False) is False


def test_max_steps_bounded(monkeypatch) -> None:
    assert af.agent_max_steps() == 8
    monkeypatch.setenv("PANIER_AGENT_MAX_STEPS", "3")
    assert af.agent_max_steps() == 3
    monkeypatch.setenv("PANIER_AGENT_MAX_STEPS", "abc")
    assert af.agent_max_steps() == 8


def test_line_result_done_maps_inserted() -> None:
    out = af.line_result_from_agent(
        "leclerc", _line(), {"status": "done", "job_id": "j1", "steps": [{}, {}]})
    assert out["inserted"] is True and out["addable"] is True
    assert out["agent_job_id"] == "j1" and out["agent_steps"] == 2


def test_line_result_blocked_keeps_reason() -> None:
    out = af.line_result_from_agent(
        "leclerc", _line(), {"status": "blocked", "blocked_reason": "JEV BLOCKED"})
    assert out["blocked_by"] == "agent:JEV BLOCKED"
    assert out.get("inserted") is None


def test_line_result_needs_confirmation() -> None:
    out = af.line_result_from_agent("leclerc", _line(), {"status": "needs_confirmation"})
    assert out["blocked_by"] == "agent_confirmation_requise"


def test_client_agent_run_sdk_path(monkeypatch) -> None:
    from panier.managed_browser import ManagedBrowserClient

    calls: dict = {}

    class FakeSdk:
        def agent_run(self, profile, goal, **kw):
            calls.update(profile=profile, goal=goal, kw=kw)
            return {"ok": True, "status": "done"}

    client = ManagedBrowserClient(profile="courses", site="leclerc", runner=lambda *a, **k: None)  # type: ignore[arg-type]
    client._sdk = FakeSdk()
    res = client.agent_run(
        "Add milk", url="https://x", max_steps=5, expected={"urlContains": "panier"})
    assert res.action == "agent" and res.data["status"] == "done"
    assert calls["profile"] == "courses"
    assert calls["kw"]["confirm_irreversible"] is True
    assert calls["kw"]["site"] == "leclerc"


def test_shim_agent_run_payload(monkeypatch) -> None:
    calls: list = []

    def fake_request(base_url, route, payload_body, *, method="POST"):
        calls.append((route, payload_body))
        return 200, {"ok": True}

    monkeypatch.setattr(shim, "_request", fake_request)
    code = shim.main([
        "agent", "run", "--goal", "Add milk", "--url", "https://x",
        "--max-steps", "5", "--dry-run",
        "--expected", json.dumps({"urlContains": "panier"}),
        "--profile", "courses", "--site", "leclerc", "--json",
    ])
    assert code == 0
    route, payload = calls[0]
    assert route == "/managed/agent/run"
    assert payload["goal"] == "Add milk"
    assert payload["max_steps"] == 5
    assert payload["dry_run"] is True
    assert payload["expected"] == {"urlContains": "panier"}


def test_maybe_agent_fallback_skipped_when_disabled(monkeypatch) -> None:
    from panier.cli import _maybe_agent_fallback

    monkeypatch.delenv("PANIER_AGENT_FALLBACK", raising=False)
    assert _maybe_agent_fallback("leclerc", _line(), None, "courses", None, action="add") is None


def test_maybe_agent_fallback_runs_agent(monkeypatch) -> None:
    import panier.cli as cli

    monkeypatch.setenv("PANIER_AGENT_FALLBACK", "1")
    monkeypatch.setattr(
        af, "run_cart_agent_fallback",
        lambda *a, **k: type("R", (), {"data": {"status": "done", "job_id": "j9"}})(),
    )
    out = cli._maybe_agent_fallback("leclerc", _line(), {"catalog_found": False},
                                    "courses", None, action="add")
    assert out is not None and out["inserted"] is True


def test_maybe_agent_fallback_never_raises(monkeypatch) -> None:
    import panier.cli as cli

    monkeypatch.setenv("PANIER_AGENT_FALLBACK", "1")

    def boom(*a, **k):
        raise RuntimeError("proxy down")

    monkeypatch.setattr(af, "run_cart_agent_fallback", boom)
    out = cli._maybe_agent_fallback("leclerc", _line(), None, "courses", None, action="add")
    assert out is not None and out["blocked_by"].startswith("agent:transport")
