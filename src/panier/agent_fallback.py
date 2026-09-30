"""Fallback agentique JEV pour les flows panier déterministes.

Quand le chemin déterministe (JS injecté / flow appris) ne trouve pas le
produit ou ne parvient pas à cliquer « Ajouter au panier » (sélecteur cassé,
page redesignée), ce module bascule sur le pilote agentique du Managed
Browser (``POST /managed/agent/run``) au lieu d'abandonner la ligne.

Garde-fous :
- opt-in via ``PANIER_AGENT_FALLBACK=1`` (défaut OFF) ;
- jamais d'étape irréversible : ajout panier uniquement, jamais
  paiement/commande (le serveur impose en plus ``confirm_irreversible=True``) ;
- ``expected`` systématique (URL panier du drive) → statut ``done`` vérifié,
  sinon la ligne reste en échec explicite ;
- ``max_steps`` borné (défaut 8) pour limiter le coût JEV.
"""

from __future__ import annotations

import os

from panier.cart import CartLine, store_cart_url
from panier.managed_browser import BrowserCommandResult, ManagedBrowserClient

AGENT_FALLBACK_ENV_VAR = "PANIER_AGENT_FALLBACK"
AGENT_FALLBACK_DEFAULT_MAX_STEPS = 8

#: Fragments d'URL attendus après un ajout panier réussi, par drive.
_CART_URL_HINTS = {
    "leclerc": ("mon-panier", "panier"),
    "auchan": ("panier", "cart"),
    "carrefour": ("cart", "panier"),
}


def agent_fallback_enabled(explicit: bool | None = None) -> bool:
    """Le fallback agent est-il actif ? Flag explicite > env (défaut OFF)."""
    if explicit is not None:
        return explicit
    return os.environ.get(AGENT_FALLBACK_ENV_VAR, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def agent_max_steps(explicit: int | None = None) -> int:
    if explicit is not None:
        return max(1, int(explicit))
    raw = os.environ.get("PANIER_AGENT_MAX_STEPS", "")
    try:
        return max(1, int(raw)) if raw else AGENT_FALLBACK_DEFAULT_MAX_STEPS
    except ValueError:
        return AGENT_FALLBACK_DEFAULT_MAX_STEPS


def cart_goal(store: str, line: CartLine, *, action: str = "add") -> str:
    """Goal en langage naturel pour le pilote JEV (pas de secret dedans)."""
    verb = "Remove from the cart" if action == "remove" else "Add to the cart"
    return (
        f"On {store}, {verb}: {line.product} (quantity {line.quantity}). "
        "Stay on this site, do not check out, do not pay, do not place any order."
    )


def cart_expected(store: str) -> dict:
    """Outcome attendu : la page panier du drive après l'action."""
    url = store_cart_url(store)
    hints = _CART_URL_HINTS.get(store.lower(), ("panier", "cart"))
    fragment = next((h for h in hints if h in (url or "").lower()), hints[0])
    return {"urlContains": fragment}


def agent_status_of(payload: dict) -> str:
    """Statut normalisé du run : done / needs_confirmation / blocked / error."""
    status = str(payload.get("status") or "")
    if status in {"done", "needs_confirmation", "blocked", "done_unverified"}:
        return status
    if payload.get("blocked_reason"):
        return "blocked"
    return "error"


def run_cart_agent_fallback(
    store: str,
    line: CartLine,
    *,
    profile: str,
    browser_command: str | None,
    action: str = "add",
    dry_run: bool = False,
    max_steps: int | None = None,
) -> BrowserCommandResult:
    """Exécute UNE ligne panier via le pilote JEV. Lève si le transport échoue."""
    from panier.collector import managed_browser_profile_for_drive

    browser = ManagedBrowserClient(
        command=browser_command,
        profile=managed_browser_profile_for_drive(profile, store),
        site=store,
    )
    target_url = line.url or line.search_url
    data = browser.agent_run(
        cart_goal(store, line, action=action),
        url=target_url,
        max_steps=agent_max_steps(max_steps),
        dry_run=dry_run,
        expected=None if dry_run else cart_expected(store),
    ).data
    return BrowserCommandResult(action="agent", data=data if isinstance(data, dict) else {})


def line_result_from_agent(
    store: str, line: CartLine, payload: dict, *, action: str = "add"
) -> dict:
    """Mappe la réponse /managed/agent/run vers une line_result panier."""
    status = agent_status_of(payload)
    base = {
        "item": line.item,
        "product": line.product,
        "url": line.url or line.search_url,
        "agent_status": status,
        "agent_job_id": payload.get("job_id"),
        "agent_steps": len(payload.get("steps") or []),
        "agent_jev_calls": payload.get("jev_calls", 0),
    }
    if status == "done":
        key = "removed" if action == "remove" else "inserted"
        avail = "removable" if action == "remove" else "addable"
        return {**base, "catalog_found": True, avail: True, key: True}
    if status == "needs_confirmation":
        return {**base, "catalog_found": True, "blocked_by": "agent_confirmation_requise"}
    reason = payload.get("blocked_reason") or "agent_sans_succes"
    return {**base, "catalog_found": False, "blocked_by": f"agent:{reason}"}
