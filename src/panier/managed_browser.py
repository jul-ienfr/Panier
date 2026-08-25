from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from typing import Protocol


class ManagedBrowserError(RuntimeError):
    """Erreur opérateur-safe pour l'adapter Managed Browser."""


@dataclass(frozen=True)
class BrowserCommandResult:
    action: str
    data: dict


class CommandRunner(Protocol):
    def __call__(
        self, args: list[str], *, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]: ...


def default_runner(
    args: list[str], *, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )


class TimeoutRunner:
    """Runner bornant chaque invocation navigateur (anti-hang)."""

    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds

    def __call__(
        self, args: list[str], *, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
            timeout=self.timeout_seconds,
        )


class ManagedBrowserClient:
    """Client fin autour du wrapper Managed Browser local.

    Le contrat public reste la CLI Node `scripts/managed-browser.js` : Panier ne connaît
    pas les routes HTTP internes du daemon et reste testable avec un runner injecté.
    """

    def __init__(
        self,
        *,
        command: str | None = None,
        profile: str = "courses",
        site: str = "leclerc",
        runner: CommandRunner = default_runner,
    ) -> None:
        self.command = command or os.environ.get(
            "PANIER_MANAGED_BROWSER_COMMAND",
            "node /home/jul/tools/camofox-browser/scripts/managed-browser.js",
        )
        self.profile = profile
        self.site = site
        if runner is default_runner:
            timeout = os.environ.get("PANIER_BROWSER_COMMAND_TIMEOUT")
            try:
                timeout_seconds = float(timeout) if timeout else 120.0
            except ValueError:
                timeout_seconds = 120.0
            if timeout_seconds > 0:
                runner = TimeoutRunner(timeout_seconds)
        self.runner = runner

    def status(self) -> BrowserCommandResult:
        return self._run(["profile", "status"])

    def open(self, url: str | None = None) -> BrowserCommandResult:
        args = ["lifecycle", "open"]
        if url:
            args.extend(["--url", url])
        return self._run(args)

    def navigate(self, url: str) -> BrowserCommandResult:
        return self._run(["navigate", "--url", url])

    def console_eval(self, expression: str, tab_id: str | None = None) -> BrowserCommandResult:
        args = ["console", "eval", "--expression", expression]
        if tab_id:
            args.extend(["--tab-id", tab_id])
        return self._run(args)

    def snapshot(self) -> BrowserCommandResult:
        return self._run(["snapshot"])

    def flow_run(
        self,
        flow: str,
        *,
        params: dict[str, str] | None = None,
        max_side_effect_level: str = "submit_apply",
        allow_llm_repair: bool = False,
    ) -> BrowserCommandResult:
        args = ["flow", "run", flow]
        for key, value in (params or {}).items():
            args.extend(["--param", f"{key}={value}"])
        args.extend(["--max-side-effect-level", max_side_effect_level])
        if allow_llm_repair:
            args.append("--allow-llm-repair")
        return self._run(args)

    def checkpoint(self, reason: str) -> BrowserCommandResult:
        return self._run(["storage", "checkpoint", "--reason", reason])

    def _run(self, args: list[str]) -> BrowserCommandResult:
        command_args = [
            *shlex.split(self.command),
            *args,
            "--profile",
            self.profile,
            "--site",
            self.site,
            "--json",
        ]
        try:
            completed = self.runner(command_args)
        except FileNotFoundError as exc:
            raise ManagedBrowserError(
                f"Managed Browser introuvable: {command_args[0]}. "
                "Configure PANIER_MANAGED_BROWSER_COMMAND."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ManagedBrowserError(
                f"Managed Browser trop lent (> {exc.timeout:g}s) : {args[0]} abandonné."
            ) from exc
        if completed.returncode != 0:
            detail = self._error_detail(completed)
            raise ManagedBrowserError(
                f"Managed Browser a échoué ({completed.returncode}): {detail}"
            )
        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise ManagedBrowserError("Réponse Managed Browser invalide (JSON attendu).") from exc
        return BrowserCommandResult(action=args[0], data=payload)

    @staticmethod
    def _error_detail(completed: subprocess.CompletedProcess[str]) -> str:
        """Retourne le détail le plus utile d'un échec Managed Browser."""
        raw = (completed.stderr or completed.stdout or "").strip()
        if not raw:
            return "erreur inconnue"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        if not isinstance(payload, dict):
            return raw
        parts = [str(payload[key]) for key in ("error", "operation", "profile") if payload.get(key)]
        return " — ".join(parts) or raw
