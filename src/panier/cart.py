from __future__ import annotations

import base64
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote_plus
from uuid import uuid4

import yaml

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


def cart_line_from_dict(data: dict) -> CartLine:
    return CartLine(
        store=str(data.get("store") or ""),
        item=str(data.get("item") or ""),
        product=str(data.get("product") or data.get("item") or ""),
        quantity=int(data.get("quantity") or 1),
        url=data.get("url") or None,
        search_url=data.get("search_url") or None,
        status=str(data.get("status") or "offer_collected"),
    )


@dataclass(frozen=True)
class CartRun:
    id: str
    action: str
    dry_run: bool
    grouped_lines: dict[str, list[CartLine]]
    results: dict[str, dict]
    created_at: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "action": self.action,
            "dry_run": self.dry_run,
            "created_at": self.created_at,
            "stores": sorted(self.grouped_lines),
            "grouped_lines": {
                store: [asdict(line) for line in lines]
                for store, lines in sorted(self.grouped_lines.items())
            },
            "results": self.results,
        }


LECLERC_DRIVE_BASE_URL = "https://fd2-courses.leclercdrive.fr/magasin-027419-027419-Viuz-en-Sallaz"


def store_search_url(store: str, query: str) -> str:
    normalized = normalize_name(store)
    encoded = quote_plus(query)
    if normalized == "leclerc":
        return f"{LECLERC_DRIVE_BASE_URL}/recherche.aspx?TexteRecherche={encoded}&tri=1"
    if normalized == "auchan":
        return f"https://www.auchan.fr/recherche?text={encoded}"
    return ""


def store_cart_url(store: str) -> str:
    normalized = normalize_name(store)
    if normalized == "leclerc":
        return f"{LECLERC_DRIVE_BASE_URL}/mon-panier.aspx"
    if normalized == "auchan":
        return "https://www.auchan.fr/panier"
    return ""


CART_REMOVE_EVAL_JS = r"""
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
  const challengeHost = /captcha-delivery|datadome/i.test(document.documentElement?.outerHTML || '');
  if (challengeHost) {
    return { item, product, url: location.href, catalog_found: false, removable: false, removed: false, blocked_by: 'anti-bot', error: 'Blocage anti-bot détecté' };
  }
  const wantedTokens = wanted.split(/[^a-z0-9]+/).filter((token) => token.length >= 3);
  const textOf = (node) => norm(node?.innerText || node?.textContent || '');
  const cartText = () => {
    const cart = document.querySelector('.header-cart, a[aria-label*="panier" i], [class*="header-cart"], [class*="cart" i]');
    return (cart?.innerText || cart?.textContent || '').replace(/\s+/g, ' ').trim();
  };
  const nodes = Array.from(document.querySelectorAll(
    '.product-thumbnail, [class*=product-thumbnail], [data-testid*=product], [class*=product], [class*=Product], article, li, .liWCRS310_Product'
  ));
  const scored = nodes
    .map((node) => {
      const text = textOf(node);
      if (!text || !/\d|€|supprimer|retirer|panier|drive/.test(text)) return null;
      const score = wantedTokens.reduce((total, token) => total + (text.includes(token) ? 1 : 0), 0);
      const exact = wanted && text.includes(wanted);
      return { node, text, score: exact ? score + 5 : score };
    })
    .filter(Boolean)
    .sort((a, b) => b.score - a.score);
  const card = scored.find((entry) => entry.score > 0)?.node || null;
  const catalogFound = Boolean(card);
  const visibleText = card ? (card.innerText || card.textContent || '').replace(/\s+/g, ' ').trim() : '';
  const controls = card ? Array.from(card.querySelectorAll('button, a, input[type=button], input[type=submit], [role=button]')) : [];
  const removeButton = controls.find((el) => {
    const label = [el.innerText, el.textContent, el.getAttribute?.('aria-label'), el.getAttribute?.('title'), el.value]
      .filter(Boolean).join(' ');
    const disabled = el.disabled || el.getAttribute?.('aria-disabled') === 'true' || el.closest?.('[aria-disabled=true]');
    if (disabled) return false;
    if (/ajouter|add|paiement|payer|commande|commander|valider|checkout|livraison/i.test(label)) return false;
    return /supprimer|retirer|remove|moins|less|enlever/i.test(label);
  });
  const removable = Boolean(removeButton);
  const beforeCart = cartText();
  let clicked = false;
  let error = null;
  if (removable && !dryRun) {
    try {
      removeButton.scrollIntoView({ block: 'center', inline: 'center' });
      await sleep(150);
      for (let i = 0; i < Math.max(1, Number(quantity || 1)); i += 1) {
        if (i > 0) await sleep(500);
        removeButton.click();
        clicked = true;
      }
      await sleep(1800);
    } catch (err) {
      error = err?.message || String(err);
    }
  }
  const afterCart = cartText();
  return {
    item,
    product,
    url: location.href,
    catalog_found: catalogFound,
    removable,
    removed: clicked && !error,
    clicked,
    changed_after_click: beforeCart !== afterCart,
    before_cart: beforeCart,
    after_cart: afterCart,
    button_label: removeButton ? (removeButton.innerText || removeButton.textContent || removeButton.getAttribute?.('aria-label') || removeButton.value || '').trim() : '',
    visible_text: visibleText.slice(0, 500),
    blocked_by: challengeHost ? 'anti-bot' : null,
    error,
  };
}
"""


AUCHAN_CART_REMOVE_EVAL_JS = r"""
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
  const cartText = () => {
    const cart = document.querySelector('.header-cart, a[aria-label*="panier" i], [class*="header-cart"]');
    return (cart?.innerText || cart?.textContent || '').replace(/\s+/g, ' ').trim();
  };
  const nodes = Array.from(document.querySelectorAll(
    '.product-thumbnail, [class*=product-thumbnail], [data-testid*=product], [class*=product], [class*=Product], article, li'
  ));
  const scored = nodes
    .map((node) => {
      const text = textOf(node);
      if (!text || !/\d|€|supprimer|retirer|panier|drive/.test(text)) return null;
      const score = wantedTokens.reduce((total, token) => total + (text.includes(token) ? 1 : 0), 0);
      const exact = wanted && text.includes(wanted);
      return { node, text, score: exact ? score + 5 : score };
    })
    .filter(Boolean)
    .sort((a, b) => b.score - a.score);
  const card = scored.find((entry) => entry.score > 0)?.node || null;
  const catalogFound = Boolean(card);
  const visibleText = card ? (card.innerText || card.textContent || '').replace(/\s+/g, ' ').trim() : '';
  const controls = card ? Array.from(card.querySelectorAll('button, a, input[type=button], input[type=submit], [role=button]')) : [];
  const removeButton = controls.find((el) => {
    const label = [el.innerText, el.textContent, el.getAttribute?.('aria-label'), el.getAttribute?.('title'), el.value]
      .filter(Boolean).join(' ');
    const disabled = el.disabled || el.getAttribute?.('aria-disabled') === 'true' || el.closest?.('[aria-disabled=true]');
    if (disabled) return false;
    if (/ajouter|add|paiement|payer|commande|commander|valider|checkout|livraison/i.test(label)) return false;
    return /supprimer|retirer|remove|moins|less/i.test(label);
  });
  const removable = Boolean(removeButton);
  const beforeCart = cartText();
  let clicked = false;
  let error = null;
  if (removable && !dryRun) {
    try {
      removeButton.scrollIntoView({ block: 'center', inline: 'center' });
      await sleep(150);
      for (let i = 0; i < Math.max(1, Number(quantity || 1)); i += 1) {
        if (i > 0) await sleep(500);
        removeButton.click();
        clicked = true;
      }
      await sleep(1800);
    } catch (err) {
      error = err?.message || String(err);
    }
  }
  const afterCart = cartText();
  return {
    item,
    product,
    url: location.href,
    catalog_found: catalogFound,
    removable,
    removed: clicked && !error,
    clicked,
    changed_after_click: beforeCart !== afterCart,
    before_cart: beforeCart,
    after_cart: afterCart,
    button_label: removeButton ? (removeButton.innerText || removeButton.textContent || removeButton.getAttribute?.('aria-label') || removeButton.value || '').trim() : '',
    visible_text: visibleText.slice(0, 500),
    error,
  };
}
"""


AUCHAN_CART_ADD_EVAL_JS = r"""
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
  const cartText = () => {
    const cart = document.querySelector('.header-cart, a[aria-label*="panier" i], [class*="header-cart"]');
    return (cart?.innerText || cart?.textContent || '').replace(/\s+/g, ' ').trim();
  };
  const nodes = Array.from(document.querySelectorAll(
    '.product-thumbnail, [class*=product-thumbnail], [data-testid*=product], [class*=product], [class*=Product], article, li'
  ));
  const scored = nodes
    .map((node) => {
      const text = textOf(node);
      if (!text || !/\d|€|ajouter|panier|drive/.test(text)) return null;
      const score = wantedTokens.reduce((total, token) => total + (text.includes(token) ? 1 : 0), 0);
      const exact = wanted && text.includes(wanted);
      return { node, text, score: exact ? score + 5 : score };
    })
    .filter(Boolean)
    .sort((a, b) => b.score - a.score);
  const card = scored.find((entry) => entry.score > 0)?.node || null;
  const catalogFound = Boolean(card);
  const visibleText = card ? (card.innerText || card.textContent || '').replace(/\s+/g, ' ').trim() : '';
  const controls = card ? Array.from(card.querySelectorAll('button, a, input[type=button], input[type=submit], [role=button]')) : [];
  const addButton = controls.find((el) => {
    const label = [el.innerText, el.textContent, el.getAttribute?.('aria-label'), el.getAttribute?.('title'), el.value]
      .filter(Boolean).join(' ');
    const disabled = el.disabled || el.getAttribute?.('aria-disabled') === 'true' || el.closest?.('[aria-disabled=true]');
    if (disabled) return false;
    if (/supprimer|retirer|less|moins|bient[oô]t\s+disponible|indisponible|rupture/i.test(label)) return false;
    if (/paiement|payer|commande|commander|valider|checkout|livraison/i.test(label)) return false;
    return /ajouter.*panier|panier/i.test(label);
  });
  const addable = Boolean(addButton);
  const beforeCart = cartText();
  let clicked = false;
  let error = null;
  if (addable && !dryRun) {
    try {
      addButton.scrollIntoView({ block: 'center', inline: 'center' });
      await sleep(150);
      for (let i = 0; i < Math.max(1, Number(quantity || 1)); i += 1) {
        if (i > 0) await sleep(500);
        addButton.click();
        clicked = true;
      }
      await sleep(1800);
    } catch (err) {
      error = err?.message || String(err);
    }
  }
  const afterCart = cartText();
  return {
    item,
    product,
    url: location.href,
    catalog_found: catalogFound,
    addable,
    inserted: clicked && !error,
    clicked,
    changed_after_click: beforeCart !== afterCart,
    before_cart: beforeCart,
    after_cart: afterCart,
    button_label: addButton ? (addButton.innerText || addButton.textContent || addButton.getAttribute?.('aria-label') || addButton.value || '').trim() : '',
    visible_text: visibleText.slice(0, 500),
    error,
  };
}
"""


CART_STATUS_EVAL_JS = r"""
async ({ expected = [], store = null }) => {
  const norm = (value) => String(value || '')
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase();
  const tokens = (value) => norm(value).split(/[^a-z0-9]+/).filter((token) => token.length >= 3);
  const textOf = (node) => (node?.innerText || node?.textContent || '').replace(/\s+/g, ' ').trim();
  const pageText = [document.title || '', document.body?.innerText || ''].join(' ').slice(0, 6000);
  const blockedBy = /captcha-delivery|datadome|captcha|robot|verification/i.test(pageText) ? 'anti_bot_challenge' : null;
  const root = document.querySelector('[class*="cart" i], [class*="panier" i], [data-testid*="cart" i], [data-testid*="basket" i], main') || document.body;
  const selector = '[data-testid*="cart" i], [data-testid*="basket" i], [data-testid*="product" i], [class*="cart" i], [class*="basket" i], [class*="panier" i], [class*="product" i], [class*="item" i], article, li';
  const seen = new Set();
  const actualLines = Array.from(root.querySelectorAll(selector)).map((node) => {
    const text = textOf(node);
    if (!text || text.length < 8 || !/[0-9]|€|eur|panier|supprimer|retirer|quantit/i.test(text)) return null;
    const key = text.slice(0, 240);
    if (seen.has(key)) return null;
    seen.add(key);
    const quantityMatch = text.match(/(?:quantit[eé]\s*:?\s*)?([0-9]+)\s*(?:x|×)?/i);
    const priceMatch = text.match(/([0-9]+(?:[,.][0-9]{1,2})?)\s*(?:€|eur)/i);
    const controls = Array.from(node.querySelectorAll('button, a, input[type=button], input[type=submit], [role=button]'));
    const controlLabels = controls.map((el) => [el.innerText, el.textContent, el.getAttribute?.('aria-label'), el.getAttribute?.('title'), el.value]
      .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim()).filter(Boolean);
    return {
      title: text.slice(0, 180),
      visible_text: text.slice(0, 500),
      quantity: quantityMatch ? Number(quantityMatch[1]) : null,
      price_text: priceMatch ? priceMatch[0] : null,
      removable: controlLabels.some((label) => /supprimer|retirer|remove|moins|less|enlever/i.test(label)),
      control_labels: controlLabels.slice(0, 8),
    };
  }).filter(Boolean).slice(0, 80);
  const expectedMatches = expected.map((line, index) => {
    const wanted = norm(line.product || line.item);
    const wantedTokens = tokens(wanted);
    const scored = actualLines.map((actual, actualIndex) => {
      const haystack = norm(actual.visible_text || actual.title);
      const tokenScore = wantedTokens.reduce((total, token) => total + (haystack.includes(token) ? 1 : 0), 0);
      const score = wanted && haystack.includes(wanted) ? tokenScore + 5 : tokenScore;
      return { actual_index: actualIndex, score, matched_tokens: wantedTokens.filter((token) => haystack.includes(token)) };
    }).sort((a, b) => b.score - a.score);
    const best = scored[0] || null;
    const matched = Boolean(best && best.score > 0);
    return {
      expected_index: index,
      item: line.item || '',
      product: line.product || '',
      expected_quantity: Number(line.quantity || 1),
      matched,
      confidence: !matched ? 'none' : best.score >= Math.max(3, wantedTokens.length) ? 'high' : 'low',
      actual_index: matched ? best.actual_index : null,
      actual_quantity: matched ? actualLines[best.actual_index]?.quantity : null,
      matched_tokens: matched ? best.matched_tokens : [],
    };
  });
  const headerCart = document.querySelector('.header-cart, a[aria-label*="panier" i], [class*="header-cart" i], [class*="cart" i]');
  return {
    ok: !blockedBy,
    mode: 'read_only_cart_status',
    store,
    url: location.href,
    title: document.title || '',
    blocked_by: blockedBy,
    cart_summary_text: textOf(headerCart).slice(0, 300),
    actual_lines: actualLines,
    expected_matches: expectedMatches,
    counts: {
      expected: expected.length,
      actual_candidates: actualLines.length,
      matched: expectedMatches.filter((line) => line.matched).length,
      unmatched_expected: expectedMatches.filter((line) => !line.matched).length,
    },
    side_effects: { clicked: false, forms_submitted: false, storage_written: false, network_intent: 'none_from_script' },
    errors: [],
  };
}
"""


def cart_run_dir(data_dir: Path) -> Path:
    return data_dir / "runs"


SAFE_CART_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def validate_cart_run_id(run_id: str) -> str:
    if not SAFE_CART_RUN_ID_RE.fullmatch(run_id):
        raise ValueError("identifiant de run panier invalide")
    return run_id


def cart_run_path(data_dir: Path, run_id: str) -> Path:
    safe_run_id = validate_cart_run_id(run_id)
    base = cart_run_dir(data_dir).resolve()
    path = (base / f"{safe_run_id}.yaml").resolve()
    if base != path.parent:
        raise ValueError("identifiant de run panier hors dossier runs")
    return path


def new_cart_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"cart-{stamp}-{uuid4().hex[:8]}"


def save_cart_run(data_dir: Path, run: CartRun) -> Path:
    path = cart_run_path(data_dir, run.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(run.to_dict(), allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    latest = cart_run_dir(data_dir) / "latest.txt"
    latest.write_text(run.id + "\n", encoding="utf-8")
    return path


def load_cart_run(data_dir: Path, run_id: str | None = None) -> CartRun:
    if run_id is None or run_id == "latest":
        latest = cart_run_dir(data_dir) / "latest.txt"
        if not latest.exists():
            raise FileNotFoundError("aucun run panier persistant")
        run_id = latest.read_text(encoding="utf-8").strip()
    try:
        path = cart_run_path(data_dir, run_id)
    except ValueError as exc:
        raise FileNotFoundError(str(exc)) from exc
    if not path.exists():
        raise FileNotFoundError(f"run panier introuvable: {run_id}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    grouped = {
        str(store): [cart_line_from_dict(line) for line in (lines or [])]
        for store, lines in (data.get("grouped_lines") or {}).items()
    }
    return CartRun(
        id=str(data.get("id") or run_id),
        action=str(data.get("action") or "add"),
        dry_run=bool(data.get("dry_run", True)),
        grouped_lines=grouped,
        results=data.get("results") or {},
        created_at=str(data.get("created_at") or ""),
    )


def cart_status_expression(store: str, lines: list[CartLine]) -> str:
    payload = {"store": store, "expected": [asdict(line) for line in lines]}
    return f"({CART_STATUS_EVAL_JS})({json.dumps(payload, ensure_ascii=False)})"


def cart_sync_diff(store: str, desired: list[CartLine], status: dict) -> dict:
    matches = (
        status.get("expected_matches") if isinstance(status.get("expected_matches"), list) else []
    )
    actual = status.get("actual_lines") if isinstance(status.get("actual_lines"), list) else []
    used_actual = {
        m.get("actual_index") for m in matches if isinstance(m, dict) and m.get("matched")
    }
    operations = []
    for index, line in enumerate(desired):
        match = next(
            (m for m in matches if isinstance(m, dict) and m.get("expected_index") == index), None
        )
        if not match or not match.get("matched"):
            operations.append(
                {
                    "op": "add",
                    "item": line.item,
                    "product": line.product,
                    "desired_quantity": line.quantity,
                    "reason": "missing_from_cart",
                    "url": line.url,
                    "search_url": line.search_url,
                }
            )
            continue
        actual_qty = match.get("actual_quantity")
        if match.get("confidence") != "high":
            operations.append(
                {
                    "op": "ambiguous",
                    "item": line.item,
                    "product": line.product,
                    "reason": "low_confidence_match",
                    "confidence": match.get("confidence"),
                }
            )
        elif actual_qty is not None and int(actual_qty) != int(line.quantity):
            operations.append(
                {
                    "op": "update_quantity",
                    "item": line.item,
                    "product": line.product,
                    "desired_quantity": line.quantity,
                    "actual_quantity": actual_qty,
                    "delta": int(line.quantity) - int(actual_qty),
                    "actual_index": match.get("actual_index"),
                    "confidence": "high",
                }
            )
        else:
            operations.append(
                {
                    "op": "keep",
                    "item": line.item,
                    "product": line.product,
                    "desired_quantity": line.quantity,
                    "actual_quantity": actual_qty,
                    "actual_index": match.get("actual_index"),
                    "confidence": "high",
                }
            )
    for index, line in enumerate(actual):
        if index not in used_actual:
            operations.append(
                {
                    "op": "remove",
                    "actual_index": index,
                    "actual_title": line.get("title") or line.get("visible_text", "")[:120],
                    "actual_quantity": line.get("quantity"),
                    "reason": "not_in_desired_cart",
                }
            )
    blocked = bool(status.get("blocked_by"))
    ambiguous = any(op["op"] == "ambiguous" for op in operations)
    return {
        "ok": not blocked,
        "store": store,
        "mode": "cart_sync_diff",
        "source_status_url": status.get("url"),
        "summary": {
            "desired_count": len(desired),
            "actual_count": len(actual),
            "to_add_count": sum(1 for op in operations if op["op"] == "add"),
            "to_remove_count": sum(1 for op in operations if op["op"] == "remove"),
            "to_update_quantity_count": sum(
                1 for op in operations if op["op"] == "update_quantity"
            ),
            "unchanged_count": sum(1 for op in operations if op["op"] == "keep"),
            "ambiguous_count": sum(1 for op in operations if op["op"] == "ambiguous"),
            "blocked": blocked,
        },
        "operations": operations,
        "requires_confirmation": True,
        "safe_to_apply": False if blocked or ambiguous else False,
        "errors": [status.get("blocked_by")] if blocked else [],
    }


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
        store: sorted(lines, key=lambda line: line.item) for store, lines in sorted(grouped.items())
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
  const challengeHost = /captcha-delivery|datadome/i.test(document.documentElement?.outerHTML || '');
  if (challengeHost) {
    return {
      item,
      product,
      url: location.href,
      catalog_found: false,
      addable: false,
      inserted: false,
      clicked: false,
      changed_after_click: false,
      button_label: '',
      visible_text: '',
      blocked_by: 'anti_bot_challenge',
      error: 'Leclerc bloque la page par challenge anti-bot/captcha; ajout panier non exécuté.',
    };
  }
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
