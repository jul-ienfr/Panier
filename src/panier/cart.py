from __future__ import annotations

import base64
import json
from dataclasses import asdict, dataclass
from urllib.parse import quote_plus

from panier.models import StoreOffer, normalize_name


@dataclass(frozen=True)
class CartLine:
    store: str
    item: str
    product: str
    quantity: int = 1
    url: str | None = None
    search_url: str | None = None
    status: str = "offer_collected"


LECLERC_DRIVE_BASE_URL = "https://fd2-courses.leclercdrive.fr/magasin-027419-027419-Viuz-en-Sallaz"


def store_search_url(store: str, query: str) -> str:
    normalized = normalize_name(store)
    encoded = quote_plus(query)
    if normalized == "leclerc":
        return f"{LECLERC_DRIVE_BASE_URL}/recherche.aspx?TexteRecherche={encoded}&tri=1"
    if normalized == "auchan":
        return f"https://www.auchan.fr/recherche?text={encoded}"
    return ""


def cart_lines_from_recommendation(
    recommendation_items: dict[str, StoreOffer],
) -> dict[str, list[CartLine]]:
    """Groupe les produits recommandés par drive pour préparer l'ajout panier."""
    grouped: dict[str, list[CartLine]] = {}
    for item_name, offer in recommendation_items.items():
        grouped.setdefault(offer.store, []).append(
            CartLine(
                store=offer.store,
                item=item_name,
                product=offer.product,
                quantity=1,
                url=offer.url,
                search_url=store_search_url(offer.store, offer.product or item_name),
                status="offer_collected",
            )
        )
    return {
        store: sorted(lines, key=lambda line: line.item)
        for store, lines in sorted(grouped.items())
    }


def cart_items_param(lines: list[CartLine]) -> str:
    """Encode les lignes panier en format historique compact."""
    return "\n".join(
        "|".join(
            [
                line.item,
                line.product,
                str(line.quantity),
                line.url or "",
                line.search_url or "",
                line.status,
            ]
        )
        for line in lines
    )


def cart_items_b64_param(lines: list[CartLine]) -> str:
    """Encode les lignes panier en base64 pour substitution JS Managed Browser."""
    return base64.b64encode(cart_items_param(lines).encode("utf-8")).decode("ascii")


def cart_items_json_param(lines: list[CartLine]) -> str:
    """Encode les lignes panier en JSON stable pour les flows récents."""
    return json.dumps([asdict(line) for line in lines], ensure_ascii=False, sort_keys=True)


CART_ADD_EVAL_JS = r"""
async ({ item, product, quantity, dryRun }) => {
  /* ruff: noqa: E501 */
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const norm = (value) => String(value || '')
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase();
  const wanted = norm(product || item);
  const wantedTokens = wanted.split(/[^a-z0-9]+/).filter((token) => token.length >= 3);
  const textOf = (node) => norm(node?.innerText || node?.textContent || '');
  const nodes = Array.from(document.querySelectorAll(
    '.liWCRS310_Product, [data-testid*=product], [class*=product], [class*=Product], article, li, [data-testid*=tile], [class*=tile]'
  ));
  const scored = nodes
    .map((node) => {
      const text = textOf(node);
      if (!text || !/\d|€|ajouter|panier|bientot|disponible/.test(text)) return null;
      const score = wantedTokens.reduce((total, token) => total + (text.includes(token) ? 1 : 0), 0);
      const exact = wanted && text.includes(wanted);
      return { node, text, score: exact ? score + 5 : score };
    })
    .filter(Boolean)
    .sort((a, b) => b.score - a.score);
  const card = scored.find((entry) => entry.score > 0)?.node || nodes.find((node) => /ajouter\s+au\s+panier/i.test(node.innerText || node.textContent || '')) || null;
  const catalogFound = Boolean(card);
  const visibleText = card ? (card.innerText || card.textContent || '').replace(/\s+/g, ' ').trim() : '';
  const controls = card ? Array.from(card.querySelectorAll('button, a, input[type=button], input[type=submit], [role=button]')) : [];
  const addButton = controls.find((el) => {
    const label = [el.innerText, el.textContent, el.getAttribute?.('aria-label'), el.getAttribute?.('title'), el.value]
      .filter(Boolean).join(' ');
    const disabled = el.disabled || el.getAttribute?.('aria-disabled') === 'true' || el.closest?.('[aria-disabled=true]');
    if (disabled) return false;
    if (/bient[oô]t\s+disponible|indisponible|rupture|voir\s+les\s+produits/i.test(label)) return false;
    if (/paiement|payer|commande|commander|valider|checkout|livraison/i.test(label)) return false;
    return /ajouter|panier|add/i.test(label);
  });
  const addable = Boolean(addButton);
  const beforeCartText = document.body?.innerText || '';
  let clicked = false;
  let error = null;
  if (addable && !dryRun) {
    try {
      addButton.scrollIntoView({ block: 'center', inline: 'center' });
      await sleep(150);
      for (let i = 0; i < Math.max(1, Number(quantity || 1)); i += 1) {
        if (i > 0) await sleep(300);
        addButton.click();
        clicked = true;
      }
      await sleep(1500);
    } catch (err) {
      error = err?.message || String(err);
    }
  }
  const afterCartText = document.body?.innerText || '';
  const changed = beforeCartText !== afterCartText;
  return {
    item,
    product,
    url: location.href,
    catalog_found: catalogFound,
    addable,
    inserted: clicked && !error,
    clicked,
    changed_after_click: changed,
    button_label: addButton ? (addButton.innerText || addButton.textContent || addButton.getAttribute?.('aria-label') || addButton.value || '').trim() : '',
    visible_text: visibleText.slice(0, 500),
    error,
  };
}
"""
