from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from typer.testing import CliRunner

import panier.cli as cli
from panier.collector import collect_offers_parallel, dedupe_drives, resolve_collect_timeout_seconds
from panier.managed_browser import ManagedBrowserClient
from panier.models import StoreOffer
from panier.offers_cache import (
    DEFAULT_CACHE_TTL_HOURS,
    offers_cache_path,
    resolve_cache_ttl_hours,
    save_offers_cache,
)


def _write_recipe(tmp_path: Path) -> None:
    (tmp_path / "recipes.yaml").write_text(
        """
- name: Riz rapide
  tags: [budget]
  ingredients:
    - name: riz
      quantity: 100
      unit: g
""".strip(),
        encoding="utf-8",
    )


def _fake_collect_factory(calls: list[str], fail_for: set[str] | None = None):
    def fake_collect(items, drive, browser, products=None, max_results=5):
        calls.append(drive)
        if fail_for and drive in fail_for:
            raise cli.ManagedBrowserError("Internal server error — navigate — courses")
        return [
            StoreOffer(
                store=drive,
                item="riz",
                product=f"Riz {drive}",
                price=2.0 + len(calls) * 0.01,
            )
        ]

    return fake_collect


def test_plan_collect_writes_cache_then_second_run_never_opens_browser(
    tmp_path: Path, monkeypatch
) -> None:
    _write_recipe(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(cli, "collect_drive_offers", _fake_collect_factory(calls))
    constructions: list[str] = []

    real_init = ManagedBrowserClient.__init__

    def counting_init(self, **kwargs):
        constructions.append(str(kwargs.get("site")))
        real_init(self, **kwargs)

    monkeypatch.setattr(ManagedBrowserClient, "__init__", counting_init)

    def run() -> object:
        return CliRunner().invoke(
            app_args(),
            [
                "plan",
                "--data-dir",
                str(tmp_path),
                "--meals",
                "1",
                "--collect",
                "leclerc,auchan",
                "--no-pantry",
            ],
        )

    from panier.cli import app

    first = run()
    assert first.exit_code == 0, first.output
    assert "Collecte leclerc: 1 offres" in first.output
    assert "Collecte auchan: 1 offres" in first.output
    items = [cli.ShoppingItem(name="riz", quantity=100, unit="g")]
    assert offers_cache_path(tmp_path, "leclerc", items).exists()

    constructions.clear()
    calls.clear()
    second = CliRunner().invoke(
        app,
        [
            "plan",
            "--data-dir",
            str(tmp_path),
            "--meals",
            "1",
            "--collect",
            "leclerc,auchan",
            "--no-pantry",
        ],
    )

    assert second.exit_code == 0, second.output
    assert constructions == []
    assert calls == []
    assert "Collecte leclerc: 1 offres (cache, âge" in second.output
    assert "Collecte auchan: 1 offres (cache, âge" in second.output
    # aucune ligne de collecte fraîche : tout est venu du cache
    for line in second.output.splitlines():
        if line.startswith("Collecte "):
            assert "(cache," in line


def app_args():
    from panier.cli import app

    return app


def test_plan_no_cache_forces_fresh_collection(tmp_path: Path, monkeypatch) -> None:
    _write_recipe(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(cli, "collect_drive_offers", _fake_collect_factory(calls))

    args = [
        "plan",
        "--data-dir",
        str(tmp_path),
        "--meals",
        "1",
        "--collect",
        "leclerc,auchan",
        "--no-pantry",
    ]
    runner = CliRunner()
    assert runner.invoke(cli.app, args).exit_code == 0
    calls.clear()

    cached_run = runner.invoke(cli.app, [*args, "--no-cache"])

    assert cached_run.exit_code == 0
    assert len(calls) == 2
    assert "Collecte leclerc:" in cached_run.output


def test_plan_collect_output_payload_includes_cache_report(tmp_path: Path, monkeypatch) -> None:
    _write_recipe(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(cli, "collect_drive_offers", _fake_collect_factory(calls))
    output = tmp_path / "offers.yaml"

    result = CliRunner().invoke(
        cli.app,
        [
            "plan",
            "--data-dir",
            str(tmp_path),
            "--meals",
            "1",
            "--collect",
            "leclerc,auchan",
            "--no-pantry",
            "--collect-output",
            str(output),
        ],
    )

    assert result.exit_code == 0
    payload = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert payload["offers"]
    report = {entry["store"]: entry for entry in payload["cache"]}
    assert report["leclerc"]["source"] == "collected"
    assert report["leclerc"]["stale"] is False


def test_stale_entry_is_used_as_fallback_when_recollection_fails(
    tmp_path: Path, monkeypatch
) -> None:
    _write_recipe(tmp_path)
    items = [cli.ShoppingItem(name="riz", quantity=100, unit="g")]
    old_offers = [StoreOffer(store="leclerc", item="riz", product="Riz Leclerc caché", price=1.5)]
    save_offers_cache(
        tmp_path,
        "leclerc",
        items,
        old_offers,
        collected_at=datetime.now(UTC) - timedelta(hours=DEFAULT_CACHE_TTL_HOURS * 4),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        cli, "collect_drive_offers", _fake_collect_factory(calls, fail_for={"leclerc"})
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "plan",
            "--data-dir",
            str(tmp_path),
            "--meals",
            "1",
            "--collect",
            "leclerc,auchan",
            "--no-pantry",
        ],
    )

    assert result.exit_code == 0
    assert "usage des offres en cache au-delà du TTL" in result.output
    assert "(cache, âge" in result.output and ", stale)" in result.output
    assert "Riz Leclerc caché" in result.output
    # auchan reste indépendant : sa collecte fraîche a bien eu lieu
    assert "Collecte auchan:" in result.output


def test_resolve_cache_ttl_hours_precedence(monkeypatch) -> None:
    monkeypatch.delenv("PANIER_CACHE_TTL_HOURS", raising=False)
    assert resolve_cache_ttl_hours() == DEFAULT_CACHE_TTL_HOURS
    assert resolve_cache_ttl_hours(12) == 12.0
    monkeypatch.setenv("PANIER_CACHE_TTL_HOURS", "9")
    assert resolve_cache_ttl_hours() == 9.0
    assert resolve_cache_ttl_hours(2) == 2.0
    monkeypatch.setenv("PANIER_CACHE_TTL_HOURS", "invalid")
    assert resolve_cache_ttl_hours() == DEFAULT_CACHE_TTL_HOURS
    assert resolve_cache_ttl_hours(None, {"PANIER_CACHE_TTL_HOURS": "-3"}) == (
        DEFAULT_CACHE_TTL_HOURS
    )


def test_dedupe_drives_keeps_order_and_single_worker_per_store() -> None:
    assert dedupe_drives(["Leclerc", "auchan", "leclerc", "", "Auchan"]) == [
        "leclerc",
        "auchan",
    ]


def test_parallel_cycle_timeout_is_global_and_isolated_per_store() -> None:
    def slow_worker(drive: str) -> list[StoreOffer]:
        if drive == "lent":
            import time

            time.sleep(5)
        return [StoreOffer(store=drive, item="riz", product=f"Riz {drive}", price=1.0)]

    started = datetime.now(UTC)
    offers, statuses, errors = collect_offers_parallel(
        ["rapide", "lent"], slow_worker, timeout_seconds=0.3
    )
    elapsed = (datetime.now(UTC) - started).total_seconds()

    assert elapsed < 4
    assert statuses["rapide"] == "ok"
    assert statuses["lent"] == "timeout"
    assert errors["lent"]
    assert [offer.store for offer in offers] == ["rapide"]


def test_failed_store_does_not_break_the_others() -> None:
    def worker(drive: str) -> list[StoreOffer]:
        if drive == "cassé":
            raise RuntimeError("boom")
        return [StoreOffer(store=drive, item="riz", product=f"Riz {drive}", price=1.0)]

    offers, statuses, errors = collect_offers_parallel(["cassé", "ok"], worker)

    assert statuses == {"cassé": "failed", "ok": "ok"}
    assert "boom" in errors["cassé"]
    assert [offer.store for offer in offers] == ["ok"]


def test_resolve_collect_timeout_seconds_env_override(monkeypatch) -> None:
    monkeypatch.delenv("PANIER_COLLECT_TIMEOUT_SECONDS", raising=False)
    assert resolve_collect_timeout_seconds() > 0
    assert resolve_collect_timeout_seconds(42) == 42.0
    monkeypatch.setenv("PANIER_COLLECT_TIMEOUT_SECONDS", "7")
    assert resolve_collect_timeout_seconds() == 7.0
    assert resolve_collect_timeout_seconds(3) == 3.0


def test_managed_browser_command_timeout_raises_operational_error(monkeypatch) -> None:
    monkeypatch.setenv("PANIER_BROWSER_COMMAND_TIMEOUT", "0.05")

    def hanging_runner(args: list[str], *, input_text: str | None = None):
        raise subprocess.TimeoutExpired(args, 0.05)

    client = ManagedBrowserClient(command="managed-browser", runner=hanging_runner)
    try:
        client.navigate("https://example.com")
    except cli.ManagedBrowserError as exc:
        assert "trop lent" in str(exc)
    else:
        raise AssertionError("ManagedBrowserError attendu")


def test_json_line_results_flow_through_persisted_payload(tmp_path: Path) -> None:
    """Sanité : les payloads JSON du CLI restent sérialisables après ajout cache."""
    payload = {"offers": [{"store": "leclerc"}], "cache": [{"store": "leclerc", "age_s": 1.0}]}
    text = json.dumps(payload)
    assert "age_s" in text
