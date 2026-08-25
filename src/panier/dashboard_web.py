"""Serveur HTTP minimal (stdlib) pour le dashboard Panier.

Zéro dépendance : http.server + rendu HTML structuré du payload du
dashboard, plus une route /json pour les consommateurs machines. Le
payload est reconstruit à chaque requête : les données affichées suivent
les fichiers locaux sans redémarrage.
"""

from __future__ import annotations

import html
import json
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

_REFRESH_SECONDS = 60

_CSS = """
:root{color-scheme:light}
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,sans-serif;margin:0;background:#f4f6f4;color:#1d221d}
header{background:#1b5e20;color:#fff;padding:1.2rem 2rem;display:flex;flex-wrap:wrap;
  align-items:baseline;gap:.8rem}
header h1{margin:0;font-size:1.35rem}
header .meta{color:#c8e6c9;font-size:.85rem}
header a{color:#a5d6a7}
main{max-width:72rem;margin:1.2rem auto;padding:0 1.2rem;display:grid;
  grid-template-columns:repeat(auto-fit,minmax(21rem,1fr));gap:1rem}
.card{background:#fff;border:1px solid #dfe5df;border-radius:.6rem;padding:1rem 1.1rem;
  box-shadow:0 1px 2px rgba(0,0,0,.04)}
.card h2{margin:.1rem 0 .7rem;font-size:1rem;color:#1b5e20;text-transform:uppercase;
  letter-spacing:.04em}
.card .sub{color:#5f6b5f;font-size:.85rem;margin:0 0 .6rem}
.row{margin:.35rem 0}
.row .label{display:inline-block;min-width:9.5rem;color:#5f6b5f;font-size:.88rem}
.chips{display:flex;flex-wrap:wrap;gap:.35rem;align-items:center}
.chip{display:inline-block;padding:.12rem .6rem;border-radius:999px;font-size:.85rem;
  background:#eceff1;border:1px solid #d5dbdb}
.chip.on{background:#e8f5e9;border-color:#a5d6a7;color:#1b5e20}
.chip.off{background:#fbe9e7;border-color:#ffab91;color:#bf360c;text-decoration:line-through}
.chip.warn{background:#fff3e0;border-color:#ffcc80;color:#e65100}
.chip.none{background:transparent;border-style:dashed;color:#9aa5a1}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(7.5rem,1fr));gap:.7rem}
.stat{text-align:center;padding:.6rem .3rem;background:#f6f8f6;border-radius:.5rem}
.stat b{display:block;font-size:1.5rem;color:#1b5e20}
.stat span{font-size:.8rem;color:#5f6b5f}
table{width:100%;border-collapse:collapse;font-size:.9rem}
th{text-align:left;color:#5f6b5f;font-weight:600;border-bottom:1px solid #dfe5df;
  padding:.3rem .4rem}
td{padding:.32rem .4rem;border-bottom:1px solid #eef1ee}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.delta-down{color:#1b5e20;font-weight:600}
.delta-up{color:#b71c1c}
.empty{color:#9aa5a1;font-style:italic}
footer{max-width:72rem;margin:.8rem auto 2rem;padding:0 1.2rem;color:#5f6b5f;font-size:.85rem}
"""


def render_dashboard_html(payload: dict[str, Any]) -> str:
    """Rendu HTML structuré, sans asset externe, auto-refresh léger."""
    generated_at = datetime.now(UTC).isoformat(timespec="seconds")
    prefs = payload.get("preferences") or {}
    meta = payload.get("household") or {}
    files = payload.get("files") or {}
    history = payload.get("history") or {}
    promos = payload.get("promos") or []
    cache_by_store = payload.get("cache_by_store") or {}
    active = payload.get("active_household")
    source = payload.get("profile_source")

    return (
        "<!doctype html><html lang=\"fr\"><head><meta charset=\"utf-8\">"
        f"<meta http-equiv=\"refresh\" content=\"{_REFRESH_SECONDS}\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>Panier — Tableau de bord</title>"
        f"<style>{_CSS}</style></head><body>"
        "<header><h1>🧺 Panier — Tableau de bord</h1>"
        f"<span class=\"meta\">Généré à {html.escape(generated_at)} UTC · "
        f"auto-refresh {_REFRESH_SECONDS}s · <a href=\"/json\">JSON</a></span></header>"
        "<main>"
        + _household_card(active, source, meta)
        + _preferences_card(prefs)
        + _stores_card(meta)
        + _files_card(files, history)
        + _promos_card(payload.get("promos_since"), promos)
        + _cache_card(cache_by_store)
        + "</main>"
        "<footer>Dernier run panier : "
        + html.escape(str(payload.get("latest_cart_run") or "—"))
        + "</footer></body></html>"
    )


def _household_card(active: Any, source: Any, meta: dict[str, Any]) -> str:
    rows = "".join(
        _row(label, value)
        for label, value in (
            ("Foyer actif", active if active else "profil de base"),
            ("Source", source),
            ("Zone géographique", meta.get("geo_zone")),
            ("Portions / repas", meta.get("servings_per_meal")),
            ("Budget max", _euro(meta.get("budget_max_eur"))),
        )
    )
    return _card("Foyer", rows)


def _preferences_card(prefs: dict[str, Any]) -> str:
    blocks = "".join(
        f"<div class=\"row\"><span class=\"label\">{html.escape(label)}</span>"
        f"{_chips(prefs.get(key) or [])}</div>"
        for key, label in (
            ("allergies", "Allergènes"),
            ("forbidden", "Interdits"),
            ("dislikes", "Détestés"),
            ("likes", "Préférés"),
            ("accepted_recipes", "Recettes acceptées"),
            ("rejected_recipes", "Recettes rejetées"),
        )
    )
    return _card("Préférences", blocks)


def _stores_card(meta: dict[str, Any]) -> str:
    enabled = meta.get("stores_enabled") or []
    disabled = meta.get("stores_disabled") or []
    body = (
        f"<div class=\"row\"><span class=\"label\">Activés</span>{_chips(enabled, 'on')}</div>"
        f"<div class=\"row\"><span class=\"label\">Désactivés</span>{_chips(disabled, 'off')}</div>"
    )
    return _card("Magasins", body)


def _files_card(files: dict[str, Any], history: dict[str, Any]) -> str:
    tiles = "".join(
        "<div class=\"stat\">"
        f"<b>{html.escape(str(value))}</b>"
        f"<span>{html.escape(label)}</span></div>"
        for value, label in (
            (files.get("recipes_count", 0), "recettes"),
            (files.get("pantry_items", 0), "placard"),
            (files.get("offers_cache_entries", 0), "caches offres"),
            (history.get("total_points", 0), "points prix"),
            (history.get("distinct_items", 0), "ingrédients suivis"),
        )
    )
    newest = history.get("newest")
    suffix = (
        f"<p class=\"sub\">Dernier point d'historique : {html.escape(str(newest))}</p>"
        if newest
        else ""
    )
    return _card("Données locales", f"<div class=\"stats\">{tiles}</div>{suffix}")


def _promos_card(since: Any, promos: list[dict[str, Any]]) -> str:
    if not promos:
        return _card(
            f"Promos candidates ({html.escape(str(since or ''))})".strip(),
            "<p class=\"empty\">Aucun candidat détecté</p>",
        )
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(promo.get('canonical_name')))}</td>"
        f"<td>{html.escape(str(promo.get('store')))}</td>"
        f"<td class=\"num\">{_euro(promo.get('last_price'))}</td>"
        f"<td class=\"num\">{_euro(promo.get('median_price'))}</td>"
        f"<td class=\"num {_delta_class(promo.get('delta_pct'))}\">"
        f"{_delta_text(promo.get('delta_pct'))}</td>"
        "</tr>"
        for promo in promos
    )
    table = (
        "<table><thead><tr><th>Ingrédient</th><th>Store</th>"
        "<th class=\"num\">Dernier</th><th class=\"num\">Médiane</th>"
        "<th class=\"num\">Écart</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )
    return _card(f"Promos candidates ({html.escape(str(since or ''))})", table)


def _cache_card(cache_by_store: dict[str, Any]) -> str:
    if not cache_by_store:
        return _card("Cache offres", "<p class=\"empty\">Aucune entrée</p>")
    rows = "".join(
        f"<tr><td>{html.escape(str(store))}</td>"
        f"<td class=\"num\">{html.escape(str(info.get('entries', 0)))}</td>"
        f"<td class=\"num\">{_age(info.get('newest_age_s'))}</td></tr>"
        for store, info in sorted(cache_by_store.items())
    )
    table = (
        "<table><thead><tr><th>Store</th><th class=\"num\">Entrées</th>"
        "<th class=\"num\">Plus récente</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )
    return _card("Cache offres", table)


def _card(title: str, body: str) -> str:
    return f"<section class=\"card\"><h2>{html.escape(title)}</h2>{body}</section>"


def _row(label: str, value: Any) -> str:
    text = "—" if value is None or value == "" else str(value)
    return (
        f"<div class=\"row\"><span class=\"label\">{html.escape(label)}</span>"
        f"<b>{html.escape(text)}</b></div>"
    )


def _chips(values: list[Any], css_class: str = "") -> str:
    if not values:
        return "<span class=\"chip none\">—</span>"
    css = f"chip {css_class}".strip()
    return "".join(
        f"<span class=\"{css}\">{html.escape(str(value))}</span>" for value in values
    )


def _euro(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.2f} €"
    except (TypeError, ValueError):
        return str(value)


def _delta_text(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):+.1f} %"
    except (TypeError, ValueError):
        return str(value)


def _delta_class(value: Any) -> str:
    try:
        return "delta-down" if float(value) <= 0 else "delta-up"
    except (TypeError, ValueError):
        return ""


def _age(seconds: Any) -> str:
    if isinstance(seconds, (int, float)):
        return f"{seconds:.0f}s"
    return "—"


def make_dashboard_server(
    host: str,
    port: int,
    payload_builder: Callable[[], dict[str, Any]],
) -> ThreadingHTTPServer:
    """Serveur threading dont chaque GET recalcule le payload."""

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "PanierDashboard/1.0"

        def do_GET(self) -> None:  # noqa: N802 (API http.server)
            path = self.path.split("?", maxsplit=1)[0].rstrip("/") or "/"
            if path in {"/", "/index.html", "/dashboard"}:
                self._respond(render_dashboard_html(payload_builder()), "text/html")
                return
            if path == "/json" or path == "/dashboard/json":
                body = json.dumps(payload_builder(), ensure_ascii=False, default=str)
                self._respond(body, "application/json")
                return
            # Chemin inconnu : retour accueil plutôt qu'un 404 sec.
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _respond(self, body: str, content_type: str) -> None:
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass  # silencieux : un dashboard LAN ne doit pas polluer stderr

    return ThreadingHTTPServer((host, port), DashboardHandler)
