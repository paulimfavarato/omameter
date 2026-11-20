"""Provider registry dispatching all 13 provider kinds via lazy module loading."""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Callable, Dict, Tuple

from backend.i18n import tr
from backend.models import ProviderSnapshot
from backend.providers.base import FetchContext, ProviderError


PROVIDER_MODULES: Dict[str, str] = {
    "claude": "backend.providers.claude",
    "codex": "backend.providers.codex",
    "cursor": "backend.providers.cursor",
    "amp": "backend.providers.amp",
    "antigravity": "backend.providers.antigravity",
    "copilot": "backend.providers.copilot",
    "devin": "backend.providers.devin",
    "grok": "backend.providers.grok",
    "muse": "backend.providers.muse",
    "ollama": "backend.providers.ollama",
    "opencode": "backend.providers.opencode",
    "openrouter": "backend.providers.openrouter",
    "zai": "backend.providers.zai",
}

SUPPORTED_PROVIDERS: Tuple[str, ...] = tuple(PROVIDER_MODULES.keys())

_MODULE_CACHE: Dict[str, ModuleType] = {}


def get_provider_module(kind: str) -> ModuleType:
    """Lazily import and return the provider module for `kind`."""
    normalized = (kind or "").strip().lower()
    module_path = PROVIDER_MODULES.get(normalized)
    if module_path is None:
        raise ProviderError(tr("error.unsupportedProvider", "Unsupported provider kind: %1", kind), kind="refused")
    cached = _MODULE_CACHE.get(normalized)
    if cached is not None:
        return cached
    module = importlib.import_module(module_path)
    _MODULE_CACHE[normalized] = module
    return module


def get_fetcher(kind: str) -> Callable[[FetchContext], ProviderSnapshot]:
    """Return the `fetch(ctx)` callable for the given provider `kind`."""
    module = get_provider_module(kind)
    fetcher = getattr(module, "fetch", None)
    if not callable(fetcher):
        raise ProviderError(f"Provider module '{kind}' does not expose fetch(ctx).", kind="refused")
    return fetcher


def fetch_provider(ctx: FetchContext) -> ProviderSnapshot:
    """Dispatch `ctx` to the provider module matching `ctx.account.kind`."""
    fetcher = get_fetcher(ctx.account.kind)
    return fetcher(ctx)


fetch = fetch_provider

__all__ = [
    "PROVIDER_MODULES",
    "SUPPORTED_PROVIDERS",
    "get_provider_module",
    "get_fetcher",
    "fetch_provider",
    "fetch",
]
