"""User-facing text for the daemon, read from the same catalogs as the QML.

The QML side translates through Omarchy's `Commons.Translations`; the daemon
emits text of its own (toasts, provider errors, sign-in status, desktop
notifications, pace hints and generic metric titles). Both read
`i18n/<language>.json`, so one catalog per language covers the whole plugin.

The active language is pushed by `OmameterService.qml` with the
`set_language` command (Omarchy Setup > Language). Lookup follows the same
chain as `Commons.Translations`: the language (pt-BR), its base (pt), English,
then the fallback passed by the caller. Placeholders are `%1`..`%9`, the same
as QML's `String.arg`.

Every `tr("key", "English fallback")` call must have its key in both
`i18n/en.json` and `i18n/pt-BR.json`, with the English value equal to the
fallback; `scripts/check_i18n.py` enforces it.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional

CATALOG_DIR = Path(__file__).resolve().parent.parent / "i18n"
DEFAULT_LANGUAGE = "en"

_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_PLACEHOLDER_RE = re.compile(r"%([1-9])")

_lock = threading.Lock()
_language = DEFAULT_LANGUAGE
_layers: tuple = ()
_cache: Dict[str, Dict[str, Any]] = {}


def _load_catalog(code: str) -> Dict[str, Any]:
    if code in _cache:
        return _cache[code]
    catalog: Dict[str, Any] = {}
    path = CATALOG_DIR / f"{code}.json"
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            catalog = parsed
    except (OSError, ValueError):
        catalog = {}
    _cache[code] = catalog
    return catalog


def _build_layers(code: str) -> tuple:
    codes = [code]
    base = code.split("-")[0]
    if base != code:
        codes.append(base)
    if DEFAULT_LANGUAGE not in codes:
        codes.append(DEFAULT_LANGUAGE)
    return tuple(_load_catalog(c) for c in codes)


def normalize_language(code: Any) -> str:
    text = str(code or "").strip()
    return text if _LANGUAGE_RE.match(text) else DEFAULT_LANGUAGE


def set_language(code: Any) -> bool:
    """Switch the active language; True when it changed."""
    global _language, _layers
    normalized = normalize_language(code)
    with _lock:
        if normalized == _language and _layers:
            return False
        _language = normalized
        _layers = _build_layers(normalized)
        return True


def language() -> str:
    return _language


def _lookup(key: str) -> Optional[str]:
    layers = _layers or _build_layers(_language)
    for catalog in layers:
        value = catalog.get(key)
        if isinstance(value, dict):
            value = value.get("other")
        if isinstance(value, str) and value:
            return value
    return None


def fill(template: str, *args: Any) -> str:
    """Replace %1..%9 with args (missing args leave the placeholder as is)."""
    if not args:
        return template

    def sub(match: "re.Match[str]") -> str:
        index = int(match.group(1)) - 1
        return str(args[index]) if index < len(args) else match.group(0)

    return _PLACEHOLDER_RE.sub(sub, template)


def tr(key: str, fallback: str, *args: Any) -> str:
    """Translate `key` in the active language, falling back to `fallback`."""
    template = _lookup(key)
    return fill(template if template is not None else fallback, *args)


def _slug(text: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", text)
    if not words:
        return ""
    return words[0].lower() + "".join(w[:1].upper() + w[1:].lower() for w in words[1:])


def _data_phrase(prefix: str, text: Any) -> str:
    # The text is already English; only other languages look it up, so an
    # English catalog entry can never change the provider's casing or wording.
    value = str(text or "")
    slug = _slug(value)
    if not slug or _language.split("-")[0] == DEFAULT_LANGUAGE:
        return value
    return tr(f"{prefix}.{slug}", value)


def metric_title(title: Any) -> str:
    """Translate a generic metric title ("Weekly", "Extra Usage") through
    `metric.<camelCase>`; product names with no catalog entry pass through."""
    return _data_phrase("metric", title)


def unit_label(label: Any) -> str:
    """Translate a lowercase value label or subtitle ("left", "credits",
    "in 30 days") through `unit.<camelCase>`."""
    return _data_phrase("unit", label)


def data_text(text: Any) -> str:
    """Translate English text stored in snapshots (provider notices, reset
    unavailable reasons) through `data.<camelCase>`. Snapshots keep English so
    the cache and the CLI stay language-neutral; the dashboard edge translates."""
    return _data_phrase("data", text)


def reset_cache() -> None:
    """Forget loaded catalogs (tests that edit catalogs on disk)."""
    global _layers
    with _lock:
        _cache.clear()
        _layers = ()
