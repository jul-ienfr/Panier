"""Serveur HTTP minimal (stdlib) pour le dashboard Panier.

Zéro dépendance : http.server + rendu HTML générique du payload du
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


def render_dashboard_html(payload: dict[str, Any]) -> str:
    """Rendu HTML autonome, sans asset externe, auto-refresh léger."""
    sections: list[str] = []
    for key, value in payload.items():
        title = key.replace("_", " ")
        sections.append(f"<h2>{html.escape(title)}</h2>{_render_value(value)}")
    generated_at = datetime.now(UTC).isoformat(timespec="seconds")
    return (
        "<!doctype html><html lang=\"fr\"><head><meta charset=\"utf-8\">"
        f"<meta http-equiv=\"refresh\" content=\"{_REFRESH_SECONDS}\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>Panier — Dashboard</title><style>"
        "body{font-family:system-ui,sans-serif;margin:2rem;max-width:60rem;"
        "color:#1c1c1c}h1{border-bottom:2px solid #2e7d32;padding-bottom:.3rem}"
        "h2{margin-top:1.5rem;color:#2e7d32}ul{list-style:none;padding-left:.8rem}"
        "li{margin:.15rem 0}.k{display:inline-block;min-width:11rem;font-weight:600}"
        ".muted{color:#666}.promo{color:#b71c1c;font-weight:600}"
        "</style></head><body>"
        "<h1>🧺 Panier — Tableau de bord</h1>"
        f"<p class=\"muted\">Généré à {html.escape(generated_at)} UTC — "
        f"auto-refresh {_REFRESH_SECONDS}s — <a href=\"/json\">/json</a></p>"
        + "".join(sections)
        + "</body></html>"
    )


def _render_value(value: Any, *, depth: int = 0) -> str:
    if isinstance(value, dict):
        if not value:
            return "<ul><li class=\"muted\">—</li></ul>"
        items = "".join(
            f"<li><span class=\"k\">{html.escape(str(key))}</span>"
            f"{_render_value(item, depth=depth + 1)}</li>"
            for key, item in sorted(value.items())
        )
        return f"<ul>{items}</ul>"
    if isinstance(value, list):
        if not value:
            return "<ul><li class=\"muted\">—</li></ul>"
        items = "".join(
            f"<li>{_render_value(item, depth=depth + 1)}</li>" for item in value
        )
        return f"<ul>{items}</ul>"
    text = html.escape(str(value)) if value is not None else "—"
    css = " class=\"promo\"" if isinstance(value, bool) and value else ""
    return f"<span{css}>{text}</span>"


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
