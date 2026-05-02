from __future__ import annotations

import json
import os
import shlex
import subprocess
import urllib.error
import urllib.request
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


class HttpRequester(Protocol):
    def __call__(self, method: str, path: str, payload: dict | None = None) -> dict: ...


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


class CamofoxHttpClient:
    def __init__(self, base_url: str | None = None, *, timeout: int = 60) -> None:
        self.base_url = (base_url or os.environ.get("PANIER_MANAGED_BROWSER_URL") or "").rstrip("/")
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def __call__(self, method: str, path: str, payload: dict | None = None) -> dict:
        if not self.base_url:
            raise ManagedBrowserError("PANIER_MANAGED_BROWSER_URL non configuré.")
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ManagedBrowserError(f"Managed Browser HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ManagedBrowserError(f"Managed Browser HTTP indisponible: {exc.reason}") from exc
        try:
            return json.loads(text or "{}")
        except json.JSONDecodeError as exc:
            raise ManagedBrowserError(
                "Réponse Managed Browser HTTP invalide (JSON attendu)."
            ) from exc


class ManagedBrowserClient:
    """Client fin autour du Managed Browser local.

    Chemin primaire : wrapper CLI Node `scripts/managed-browser.js`.
    Fallback : API HTTP Camofox (`POST /tabs`, `POST /tabs/:id/navigate`) quand le wrapper
    n'expose pas encore les routes Managed Browser ou renvoie 500.
    """

    def __init__(
        self,
        *,
        command: str | None = None,
        profile: str = "courses",
        site: str = "leclerc",
        runner: CommandRunner = default_runner,
        http: HttpRequester | None = None,
    ) -> None:
        self.command = command or os.environ.get(
            "PANIER_MANAGED_BROWSER_COMMAND",
            "node /home/jul/tools/camofox-browser/scripts/managed-browser.js",
        )
        self.profile = profile
        self.site = site
        self.runner = runner
        self.http = http or CamofoxHttpClient()
        self._tab_id: str | None = None

    def status(self) -> BrowserCommandResult:
        try:
            return self._run(["profile", "status"])
        except ManagedBrowserError as exc:
            if not self._http_configured():
                raise
            data = self.http("GET", "/health")
            data["wrapper_error"] = str(exc)
            return BrowserCommandResult(action="status", data=data)

    def open(self, url: str | None = None) -> BrowserCommandResult:
        try:
            args = ["lifecycle", "open"]
            if url:
                args.extend(["--url", url])
            return self._run(args)
        except ManagedBrowserError as exc:
            if not self._http_configured():
                raise
            return self._http_open(url, wrapper_error=str(exc))

    def navigate(self, url: str) -> BrowserCommandResult:
        try:
            return self._run(["navigate", "--url", url])
        except ManagedBrowserError as exc:
            if not self._http_configured():
                raise
            if self._tab_id is None:
                return self._http_open(url, wrapper_error=str(exc))
            data = self.http(
                "POST",
                f"/tabs/{self._tab_id}/navigate",
                {"userId": self.profile, "url": url},
            )
            data["wrapper_error"] = str(exc)
            return BrowserCommandResult(action="navigate", data=data)

    def snapshot(self) -> BrowserCommandResult:
        return self._run(["snapshot"])

    def checkpoint(self, reason: str) -> BrowserCommandResult:
        return self._run(["storage", "checkpoint", "--reason", reason])

    def _http_configured(self) -> bool:
        return not isinstance(self.http, CamofoxHttpClient) or self.http.configured

    def _http_open(self, url: str | None, *, wrapper_error: str) -> BrowserCommandResult:
        data = self.http(
            "POST",
            "/tabs",
            {"userId": self.profile, "sessionKey": self.site, "url": url or "about:blank"},
        )
        self._tab_id = data.get("tabId") or data.get("id")
        data["wrapper_error"] = wrapper_error
        return BrowserCommandResult(action="open", data=data)

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
