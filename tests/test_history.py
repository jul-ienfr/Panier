from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from typer.testing import CliRunner

import panier.cli as cli
from panier.cli import app
from panier.history_store import (
    history_db_path,
    offers_for_item,
    parse_since,
    price_history_payload,
    record_offers,
    trend_for_item,
)
from panier.models import StoreOffer

RUNNER = CliRunner()


def _offer(store: str, product: str, price: float) -> StoreOffer:
    return StoreOffer(store=store, item="emmental râpé", product=product, price=price)


def _seed_history(data_dir: Path, *, prices: list[float], store: str = "leclerc") -> None:
    base = datetime.now(UTC) - timedelta(hours=len(prices) + 1)
    for index, price in enumerate(prices):
        record_offers(
            data_dir,
            store,
            [_offer(store, f"Emmental râpé {index}", price)],
            collected_at=base + timedelta(hours=index),
        )


def test_record_and_show_roundtrip(tmp_path: Path) -> None:
    _seed_history(tmp_path, prices=[2.5, 2.3])

    result = RUNNER.invoke(
        app, ["history", "show", "emmental râpé", "--data-dir", str(tmp_path)]
    )

    assert result.exit_code == 0
    assert result.output.count("leclerc") == 2
    assert "Emmental râpé 0" in result.output
    assert history_db_path(tmp_path).exists()


def test_history_show_filters_by_store_and_json(tmp_path: Path) -> None:
    _seed_history(tmp_path, prices=[2.5])
    _seed_history(tmp_path, prices=[2.1], store="auchan")

    filtered = RUNNER.invoke(
        app,
        [
            "history",
            "show",
            "emmental râpé",
            "--store",
            "auchan",
            "--data-dir",
            str(tmp_path),
        ],
    )
    payload = RUNNER.invoke(
        app,
        ["history", "show", "emmental râpé", "--data-dir", str(tmp_path), "--format", "json"],
    )

    assert filtered.exit_code == 0
    assert filtered.output.count("auchan") == 1
    assert "leclerc" not in filtered.output
    assert payload.exit_code == 0
    parsed = yaml.safe_load(payload.output)
    assert len(parsed["points"]) == 2


def test_trend_reports_min_mean_median_last_and_promo(tmp_path: Path) -> None:
    _seed_history(tmp_path, prices=[3.0, 2.0, 2.4, 2.6])

    trend = trend_for_item(tmp_path, "emmental râpé")

    assert trend is not None
    assert trend.observations == 4
    assert trend.min_price == 2.0
    assert trend.mean_price == 2.5
    assert trend.median_price == 2.5
    assert trend.last_price == 2.6
    assert trend.is_promo_candidate is False

    promo_run = RUNNER.invoke(
        app, ["history", "trend", "emmental râpé", "--data-dir", str(tmp_path)]
    )
    assert "Candidat promo: non" in promo_run.output


def test_promos_flags_current_price_below_median(tmp_path: Path) -> None:
    _seed_history(tmp_path, prices=[3.0, 2.8, 3.2, 1.9])

    result = RUNNER.invoke(app, ["history", "promos", "--since", "7d", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert "emmental râpé (leclerc): 1.90 €" in result.output

    none_run = RUNNER.invoke(
        app,
        ["history", "promos", "--since", "7d", "--data-dir", str(tmp_path / "vide")],
    )
    assert "Aucun candidat promo" in none_run.output


def test_parse_since_formats() -> None:
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)

    assert parse_since(None, now=now) is None
    assert parse_since("7d", now=now) == now - timedelta(days=7)
    assert parse_since("24h", now=now) == now - timedelta(hours=24)
    assert parse_since("2026-08-20", now=now) == datetime(2026, 8, 20, tzinfo=UTC)

    try:
        parse_since("n'importe quoi", now=now)
    except ValueError:
        pass
    else:
        raise AssertionError("ValueError attendu")


def test_quantity_and_brand_parsing_from_titles() -> None:
    from panier.history_store import detect_brand, parse_quantity_unit

    assert parse_quantity_unit("Emmental râpé Entremont 200g") == (200.0, "g")
    assert parse_quantity_unit("Lait demi-écrémé 1L") == (1000.0, "ml")
    assert parse_quantity_unit("Tomates") == (None, None)
    assert detect_brand("Emmental râpé entremont", {"entremont"}) == "entremont"
    assert detect_brand("Yaourt nature", {"entremont"}) is None


def test_plan_collect_feeds_history_automatically(tmp_path: Path, monkeypatch) -> None:
    _write_recipe(tmp_path)

    def fake_collect(items, drive, browser, products=None, max_results=5):
        return [
            StoreOffer(
                store=drive,
                item="riz",
                product=f"Riz {drive} 1kg",
                price=2.0,
            )
        ]

    monkeypatch.setattr(cli, "collect_drive_offers", fake_collect)

    result = RUNNER.invoke(
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

    assert result.exit_code == 0
    leclerc_points = offers_for_item(tmp_path, "riz", store="leclerc")
    auchan_points = offers_for_item(tmp_path, "riz", store="auchan")
    assert len(leclerc_points) == 1
    assert len(auchan_points) == 1
    assert leclerc_points[0].quantity == 1000.0
    assert leclerc_points[0].unit == "g"


def test_price_history_block_is_informational_only(tmp_path: Path, monkeypatch) -> None:
    """Invariant doctrine : l'historique n'influence jamais la recommandation."""
    _write_recipe(tmp_path)

    def fake_collect(items, drive, browser, products=None, max_results=5):
        # auchan plus cher : la recommandation doit rester identique même si
        # l'historique de leclerc indique une hausse.
        return [
            StoreOffer(
                store=drive,
                item="riz",
                product=f"Riz {drive}",
                price=2.0 if drive == "leclerc" else 3.0,
            )
        ]

    monkeypatch.setattr(cli, "collect_drive_offers", fake_collect)

    def run() -> object:
        return RUNNER.invoke(
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

    first = run()
    assert first.exit_code == 0
    assert "Total: 2.00 €" in first.output

    # Avec --no-price-history : aucun bloc, recommandation identique.
    blockless = RUNNER.invoke(
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
            "--no-price-history",
        ],
    )
    assert blockless.exit_code == 0
    assert "Historique prix (informatif)" not in blockless.output
    assert "Total: 2.00 €" in blockless.output
    assert "Drives: leclerc\n" in first.output and "Drives: leclerc\n" in blockless.output

    # Run suivant (défaut) : le bloc informatif s'affiche sans changer la reco.
    second = run()
    assert second.exit_code == 0
    assert "Historique prix (informatif)" in second.output
    assert "Total: 2.00 €" in second.output
    assert "Riz leclerc" in second.output
    assert "Drives: leclerc\n" in second.output


def test_collect_output_payload_gains_price_history_block(tmp_path: Path, monkeypatch) -> None:
    _write_recipe(tmp_path)

    def fake_collect(items, drive, browser, products=None, max_results=5):
        return [StoreOffer(store=drive, item="riz", product=f"Riz {drive}", price=2.0)]

    monkeypatch.setattr(cli, "collect_drive_offers", fake_collect)
    output = tmp_path / "offers.yaml"
    args = [
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
    ]

    assert RUNNER.invoke(app, args).exit_code == 0
    assert RUNNER.invoke(app, args).exit_code == 0

    payload = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert "price_history" in payload
    entry = payload["price_history"]["riz"]
    assert set(entry) >= {"last_price", "delta_pct", "is_promo_candidate"}
    # stats par store : le dernier point connu détermine le store analysé
    assert entry["store"] == "auchan"
    assert entry["observations"] == 1


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


def test_price_history_payload_skips_unknown_items(tmp_path: Path) -> None:
    assert price_history_payload(tmp_path, ["inconnu"]) == {}
