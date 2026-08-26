from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import yaml
from typer.testing import CliRunner

from panier.cli import app
from panier.dashboard_web import make_dashboard_server, render_dashboard_html

RUNNER = CliRunner()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


def _payload_builder(data_dir: Path):
    def build() -> dict:
        from panier.cli import _dashboard_payload

        return _dashboard_payload(data_dir, "7d")

    return build


def test_render_html_escapes_and_uses_structured_layout() -> None:
    html = render_dashboard_html(
        {
            "preferences": {"allergies": ["<script>x</script>"]},
            "promos": [],
            "promos_since": "7d",
        }
    )

    assert "Tableau de bord" in html
    assert "&lt;script&gt;" in html
    assert "<script>" not in html
    assert "/json" in html
    assert 'http-equiv="refresh"' in html
    # cartes structurées en français, pas de clés brutes en titres
    assert ">Foyer</h2>" in html
    assert ">Préférences</h2>" in html
    assert ">Magasins</h2>" in html
    assert ">Données locales</h2>" in html
    assert "Promos candidates (7d)" in html
    assert "Aucun candidat détecté" in html
    assert 'class="chip' in html
    assert "active_household" not in html
    assert "Interface v2" in html


def test_render_html_shows_promos_table_and_store_chips() -> None:
    html = render_dashboard_html(
        {
            "preferences": {},
            "household": {
                "stores_enabled": ["leclerc"],
                "stores_disabled": ["auchan"],
                "geo_zone": "74350",
                "servings_per_meal": 4,
                "budget_max_eur": 80,
            },
            "promos": [
                {
                    "canonical_name": "emmental râpé",
                    "store": "leclerc",
                    "last_price": 1.9,
                    "median_price": 2.5,
                    "delta_pct": -24.0,
                }
            ],
            "promos_since": "7d",
            "cache_by_store": {
                "leclerc": {"entries": 2, "newest_age_s": 4321.0},
            },
        }
    )

    assert 'class="chip on">leclerc<' in html
    assert 'class="chip off">auchan<' in html
    assert "emmental râpé" in html
    assert "delta-down" in html
    assert "-24.0 %" in html
    assert "1.90 €" in html
    assert "2.50 €" in html
    assert "4321s" in html
    assert "74350" in html
    assert "80.00 €" in html


def test_serve_mode_answers_html_and_json_on_ephemeral_port(tmp_path: Path) -> None:
    (tmp_path / "profile.yaml").write_text(
        yaml.safe_dump({"allergies": ["crevette"]}), encoding="utf-8"
    )
    (tmp_path / "recipes.yaml").write_text(
        "- name: Riz rapide\n  servings: 2\n  tags: [budget]\n  ingredients:\n"
        "    - name: riz\n      quantity: 100\n      unit: g",
        encoding="utf-8",
    )

    server = make_dashboard_server("127.0.0.1", 0, _payload_builder(tmp_path))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            body = response.read().decode("utf-8")
            assert response.status == 200
            assert "Tableau de bord" in body
            assert "crevette" in body

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
            assert payload["preferences"]["allergies"] == ["crevette"]
            assert payload["files"]["recipes_count"] == 1

        # la route /dashboard fonctionne aussi (avec ou sans slash final)
        for path in ("/dashboard", "/dashboard/"):
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}{path}", timeout=5
            ) as response:
                assert response.status == 200
                assert "Tableau de bord" in response.read().decode("utf-8")

        # chemin inconnu : redirection vers l'accueil au lieu d'un 404 sec
        request = urllib.request.Request(f"http://127.0.0.1:{port}/inexistant")
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            opener.open(request, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 302
            assert exc.headers["Location"] == "/"
        else:
            raise AssertionError("302 attendu")

        # le payload est recalculé à chaque requête : un nouveau fichier apparaît
        (tmp_path / "pantry.yaml").write_text(
            yaml.safe_dump({"items": [{"name": "riz", "quantity": 100, "unit": "g"}]}),
            encoding="utf-8",
        )
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
            assert payload["files"]["pantry_items"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dashboard_cli_text_still_works_without_serve(tmp_path: Path) -> None:
    result = RUNNER.invoke(app, ["dashboard", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert "Tableau de bord Panier" in result.output
