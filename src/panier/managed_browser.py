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

    def snapshot(self) -> BrowserCommandResult:
        return self._run(["snapshot"])

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
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise ManagedBrowserError(
                f"Managed Browser a échoué ({completed.returncode}): {detail}"
            )
        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise ManagedBrowserError("Réponse Managed Browser invalide (JSON attendu).") from exc
        return BrowserCommandResult(action=args[0], data=payload)
