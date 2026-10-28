"""Fixtures partagées : neutralise l'absence du SDK Managed Browser hors poste local.

`managed_browser_client` est un paquet local (`Hermes-Managed-Browser/server/sdk/python`)
qui n'est pas distribuable sur PyPI. En CI il est absent, et le client Panier lève
alors au moment de sa construction, ce qui casse des tests sans rapport avec le
transport. On installe un stub minimal seulement quand le vrai SDK manque : localement
le comportement réel reste inchangé.
"""

from __future__ import annotations

import sys
import types


def _install_sdk_stub() -> None:
    try:
        import managed_browser_client  # noqa: F401  # type: ignore[import-not-found]
    except ImportError:
        pass
    else:
        return

    module = types.ModuleType("managed_browser_client")

    class ManagedBrowserClient:
        def __init__(self, base_url: str, **kwargs: object) -> None:
            self.base_url = base_url
            self.kwargs = kwargs

        def profile_status(self, profile: str) -> dict[str, object]:
            return {"profile": profile, "ok": True}

        def open(self, *, profile: str, url: str | None = None) -> dict[str, object]:
            return {"profile": profile, "url": url, "ok": True}

        def console_eval(self, *args: object, **kwargs: object) -> dict[str, object]:
            return {"ok": True}

        def snapshot(self, *args: object, **kwargs: object) -> dict[str, object]:
            return {"ok": True}

        def flow_run(self, *args: object, **kwargs: object) -> dict[str, object]:
            return {"ok": True}

        def storage_checkpoint(self, *args: object, **kwargs: object) -> dict[str, object]:
            return {"ok": True}

        def agent_run(self, *args: object, **kwargs: object) -> dict[str, object]:
            return {"ok": True, "status": "done"}

    class TabConflict(Exception):
        pass

    class BusyRetryable(Exception):
        pass

    class RateLimited(Exception):
        pass

    module.ManagedBrowserClient = ManagedBrowserClient  # type: ignore[attr-defined]
    module.TabConflict = TabConflict  # type: ignore[attr-defined]
    module.BusyRetryable = BusyRetryable  # type: ignore[attr-defined]
    module.RateLimited = RateLimited  # type: ignore[attr-defined]
    module.__stub__ = True  # type: ignore[attr-defined]
    sys.modules["managed_browser_client"] = module


_install_sdk_stub()
