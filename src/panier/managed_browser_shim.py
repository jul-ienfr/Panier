"""Shim CLI : traduit le contrat legacy de Panier vers le daemon Managed Browser v2.

Panier émet encore la CLI Node historique (`navigate --url ... --profile P --site S
--json`). Le daemon v2 (Hermes-Managed-Browser ≥ 2.0) expose la même sémantique en
HTTP (routes Node-compat de `camofox/api/legacy.py`). Ce shim préserve le contrat
public de `ManagedBrowserClient` : mêmes argv, JSON sur stdout, code de sortie.

Routes utilisées :
- `profile status`            → GET /managed/profiles/{profile}/status
- `lifecycle open [--url U]`  → POST /managed/cli/open
- `navigate --url U`          → POST /managed/cli/open (navigue l'onglet existant,
                                fallback moteur anti-bot inclus)
- `console eval --expr ...`   → POST /console/eval
- `snapshot`                  → POST /managed/cli/snapshot
- `flow run NAME --param k=v` → POST /flow/run
- `storage checkpoint ...`    → POST /storage/checkpoint
- `agent run --goal G ...`    → POST /managed/agent/run (pilote JEV, opt-in)

Base URL : PANIER_MANAGED_BROWSER_URL (défaut http://127.0.0.1:9377).
Timeout : PANIER_BROWSER_COMMAND_TIMEOUT secondes (défaut 120).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:9377"

_SUBCOMMAND_ROUTES = {
    ("profile", "status"): "/managed/profiles/{profile}/status",
    ("lifecycle", "open"): "/managed/cli/open",
    ("navigate",): "/managed/cli/open",
    ("console", "eval"): "/console/eval",
    ("snapshot",): "/managed/cli/snapshot",
    ("flow", "run"): "/flow/run",
    ("storage", "checkpoint"): "/storage/checkpoint",
    ("agent", "run"): "/managed/agent/run",
}

_VALUE_FLAGS = {
    "--url": "url",
    "--expression": "expression",
    "--tab-id": "tab_id",
    "--reason": "reason",
    "--goal": "goal",
    "--max-steps": "max_steps",
    "--expected": "expected",
    "--max-side-effect-level": "_max_side_effect_level",
    "--param": "_params",
}


def parse_args(argv: list[str]) -> tuple[tuple[str, ...], dict[str, object], str, str]:
    """Sépare sous-commande, options, --profile et --site du contrat legacy."""
    command: list[str] = []
    options: dict[str, object] = {}
    profile = ""
    site = ""
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--profile":
            index += 1
            profile = argv[index]
        elif token == "--site":
            index += 1
            site = argv[index]
        elif token == "--json" or token == "--allow-llm-repair" or token == "--dry-run":
            options[token] = True
        elif token in _VALUE_FLAGS:
            index += 1
            key = _VALUE_FLAGS[token]
            if key == "_params":
                params = options.setdefault("_params", {})
                if isinstance(params, dict):
                    name, _, value = str(argv[index]).partition("=")
                    params[name] = value
            else:
                options[key] = argv[index]
        elif not token.startswith("-"):
            candidate = tuple(command + [token])
            if tuple(command) in _SUBCOMMAND_ROUTES:
                # sous-commande complète : token suivant = nom de flow (flow run NAME)
                options.setdefault("_flow", token)
            elif candidate in _SUBCOMMAND_ROUTES or len(candidate) < 2:
                command.append(token)
            else:
                options.setdefault("_flow", token)
        index += 1
    return tuple(command), options, profile, site


def build_payload(
    command: tuple[str, ...], options: dict[str, object], profile: str, site: str
) -> tuple[str, dict[str, object]]:
    """Retourne (route, corps JSON) pour la sous-commande legacy demandée."""
    if command not in _SUBCOMMAND_ROUTES:
        raise SystemExit(f"sous-commande inconnue : {' '.join(command) or '(vide)'}")
    route = _SUBCOMMAND_ROUTES[command]
    if command == ("profile", "status"):
        return route.format(profile=profile), {}
    payload: dict[str, object] = {"profile": profile, "site": site}
    if "url" in options:
        payload["url"] = options["url"]
    if command == ("console", "eval"):
        payload["expression"] = options.get("expression", "")
        if options.get("tab_id"):
            payload["tab_id"] = options["tab_id"]
    elif command == ("flow", "run"):
        payload["flow"] = options.get("_flow", "")
        params = options.get("_params")
        if isinstance(params, dict):
            payload["params"] = params
        if options.get("--allow-llm-repair"):
            payload["allow_llm_repair"] = True
    elif command == ("storage", "checkpoint"):
        payload["reason"] = options.get("reason", "panier")
    elif command == ("agent", "run"):
        payload["goal"] = options.get("goal", "")
        if options.get("max_steps"):
            try:
                payload["max_steps"] = int(str(options["max_steps"]))
            except (TypeError, ValueError):
                pass
        if options.get("--dry-run"):
            payload["dry_run"] = True
        if options.get("expected"):
            try:
                payload["expected"] = json.loads(str(options["expected"]))
            except json.JSONDecodeError:
                pass
    return route, payload


def _request(
    base_url: str, route: str, payload: dict[str, object], *, method: str = "POST"
) -> tuple[int, object]:
    if method == "GET":
        request = urllib.request.Request(base_url.rstrip("/") + route, method="GET")
    else:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            base_url.rstrip("/") + route,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    try:
        with urllib.request.urlopen(request, timeout=_timeout()) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8") or "{}"
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"error": raw}


def _timeout() -> float:
    raw = os.environ.get("PANIER_BROWSER_COMMAND_TIMEOUT")
    try:
        return float(raw) if raw else 120.0
    except ValueError:
        return 120.0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command, options, profile, site = parse_args(argv)
    route, payload = build_payload(command, options, profile, site)
    base_url = os.environ.get("PANIER_MANAGED_BROWSER_URL", DEFAULT_BASE_URL)
    method = "GET" if command == ("profile", "status") else "POST"
    status, response = _request(base_url, route, payload, method=method)
    # 409 legacy = « onglet déjà ouvert, utilise console/eval » : succès pour
    # l'appelant, le payload detail porte le tab_id.
    if status >= 400 and status != 409:
        detail = response if isinstance(response, dict) else {"error": response}
        print(json.dumps(detail, ensure_ascii=False))
        return 1
    print(json.dumps(response if isinstance(response, dict) else {}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
