from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
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


def _default_command() -> str:
    """Échappatoire explicite : shim CLI lancé par l'interpréteur courant."""
    return f"{sys.executable} -m panier.managed_browser_shim"


DEFAULT_BASE_URL = "http://127.0.0.1:9377"


def _sdk_base_url() -> str:
    return os.environ.get("PANIER_MANAGED_BROWSER_URL", DEFAULT_BASE_URL).rstrip("/")


def _sdk_api_key() -> str | None:
    return os.environ.get("PANIER_MANAGED_BROWSER_KEY") or None


class ManagedBrowserClient:
    """Client Managed Browser pour Panier (plan connecteur universel, P4.19).

    Deux transports :

    - **SDK (défaut)** : appels HTTP in-process via le SDK partagé
      ``managed_browser_client`` (installé depuis
      ``Hermes-Managed-Browser/server/sdk/python``). Config :
      ``PANIER_MANAGED_BROWSER_URL`` (défaut http://127.0.0.1:9377) et
      ``PANIER_MANAGED_BROWSER_KEY`` (clé consommateur ``panier``).
    - **Échappatoire CLI** : ``PANIER_MANAGED_BROWSER_COMMAND`` (ou
      ``command=``) réactive l'ancien chemin subprocess (shim/runner
      injecté) — conservé pour les tests et le dépannage.

    Le contrat public est inchangé : mêmes méthodes, mêmes
    :class:`BrowserCommandResult`, 409 = succès porteur du ``tab_id``.
    """

    def __init__(
        self,
        *,
        command: str | None = None,
        profile: str = "courses",
        site: str = "leclerc",
        runner: CommandRunner = default_runner,
    ) -> None:
        self.profile = profile
        self.site = site
        explicit_command = command or os.environ.get("PANIER_MANAGED_BROWSER_COMMAND")
        self.command = explicit_command
        self.runner = runner
        self._sdk = None
        if explicit_command is None:
            self._sdk = self._build_sdk()
        else:
            if runner is default_runner:
                timeout = os.environ.get("PANIER_BROWSER_COMMAND_TIMEOUT")
                try:
                    timeout_seconds = float(timeout) if timeout else 120.0
                except ValueError:
                    timeout_seconds = 120.0
                if timeout_seconds > 0:
                    self.runner = TimeoutRunner(timeout_seconds)

    def _build_sdk(self):
        """Construit le client SDK partagé ; erreur actionnable si absent."""
        try:
            from managed_browser_client import ManagedBrowserClient as _SdkClient
        except ImportError as exc:  # SDK non installé dans ce venv
            raise ManagedBrowserError(
                "SDK managed_browser_client introuvable. Installe-le : "
                "pip install -e /home/jul/projects/Hermes-Managed-Browser/server/sdk/python "
                f"(ou bascule sur l'échappatoire PANIER_MANAGED_BROWSER_COMMAND). ({exc})"
            ) from exc
        timeout = os.environ.get("PANIER_BROWSER_COMMAND_TIMEOUT")
        try:
            timeout_seconds = float(timeout) if timeout else 120.0
        except ValueError:
            timeout_seconds = 120.0
        return _SdkClient(
            _sdk_base_url(),
            api_key=_sdk_api_key(),
            timeout_seconds=min(max(timeout_seconds, 1.0), 300.0),
            user_agent="panier-drive-cli",
        )

    def status(self) -> BrowserCommandResult:
        if self.command is None and getattr(self, "_sdk", None) is not None:
            data = self._sdk.profile_status(self.profile)
            return BrowserCommandResult(action="profile", data=data)
        return self._run(["profile", "status"])

    def open(self, url: str | None = None) -> BrowserCommandResult:
        if self.command is None and getattr(self, "_sdk", None) is not None:
            return BrowserCommandResult(action="lifecycle", data=self._open_via_sdk(url))
        args = ["lifecycle", "open"]
        if url:
            args.extend(["--url", url])
        return self._run(args)

    def navigate(self, url: str) -> BrowserCommandResult:
        if self.command is None and getattr(self, "_sdk", None) is not None:
            return BrowserCommandResult(action="navigate", data=self._open_via_sdk(url))
        return self._run(["navigate", "--url", url])

    def _open_via_sdk(self, url: str | None) -> dict:
        """cli/open via SDK. 409 = succès (détail porte le tab_id), comme le shim."""
        from managed_browser_client import TabConflict

        try:
            return self._sdk.open(profile=self.profile, url=url)
        except TabConflict as exc:
            return exc.body if isinstance(exc.body, dict) else {"detail": {"tab_id": exc.tab_id}}
        except Exception as exc:
            raise self._translate(exc) from None

    def console_eval(self, expression: str, tab_id: str | None = None) -> BrowserCommandResult:
        args = ["console", "eval", "--expression", expression]
        if tab_id:
            args.extend(["--tab-id", tab_id])
        try:
            if self.command is None and getattr(self, "_sdk", None) is not None:
                data = self._sdk.console_eval(self.profile, self.site, expression, tab_id=tab_id)
                return BrowserCommandResult(action="console", data=data)
            return self._run(args)
        except ManagedBrowserError as exc:
            # Les pages à redirection client-side (recherche Leclerc/Auchan)
            # détruisent le contexte d'exécution pendant l'eval : on laisse
            # la navigation se poser puis on retente une fois.
            if "Execution context was destroyed" not in str(exc):
                raise
            time.sleep(2.0)
            if self.command is None and getattr(self, "_sdk", None) is not None:
                return BrowserCommandResult(
                    action="console",
                    data=self._sdk.console_eval(self.profile, self.site, expression, tab_id=tab_id),
                )
            return self._run(args)

    def snapshot(self) -> BrowserCommandResult:
        if self.command is None and getattr(self, "_sdk", None) is not None:
            try:
                data = self._sdk.snapshot(profile=self.profile)
            except Exception as exc:
                raise self._translate(exc) from None
            return BrowserCommandResult(action="snapshot", data=data)
        return self._run(["snapshot"])

    def flow_run(
        self,
        flow: str,
        *,
        params: dict[str, str] | None = None,
        max_side_effect_level: str = "submit_apply",
        allow_llm_repair: bool = False,
    ) -> BrowserCommandResult:
        if self.command is None and getattr(self, "_sdk", None) is not None:
            try:
                data = self._sdk.flow_run(
                    self.profile, flow, site=self.site, params=params,
                    allow_llm_repair=allow_llm_repair,
                )
            except Exception as exc:
                raise self._translate(exc) from None
            return BrowserCommandResult(action="flow", data=data)
        args = ["flow", "run", flow]
        for key, value in (params or {}).items():
            args.extend(["--param", f"{key}={value}"])
        args.extend(["--max-side-effect-level", max_side_effect_level])
        if allow_llm_repair:
            args.append("--allow-llm-repair")
        return self._run(args)

    def checkpoint(self, reason: str) -> BrowserCommandResult:
        if self.command is None and getattr(self, "_sdk", None) is not None:
            try:
                data = self._sdk.storage_checkpoint(self.profile, self.site, reason=reason)
            except Exception as exc:
                raise self._translate(exc) from None
            return BrowserCommandResult(action="storage", data=data)
        return self._run(["storage", "checkpoint", "--reason", reason])

    @staticmethod
    def _translate(exc: Exception) -> ManagedBrowserError:
        """Exceptions SDK → ManagedBrowserError opérateur-safe."""
        from managed_browser_client import BusyRetryable, RateLimited

        if isinstance(exc, RateLimited):
            return ManagedBrowserError(
                "Managed Browser : plafond du consommateur atteint (429, non-retryable)."
            )
        if isinstance(exc, BusyRetryable):
            return ManagedBrowserError(f"Managed Browser occupé : {exc}")
        return ManagedBrowserError(f"Managed Browser : {exc}")

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
