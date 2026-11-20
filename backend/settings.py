"""Settings keys, defaults and bounds, derived from the `schema` in manifest.json.

The manifest is the single source: Omarchy reads it for Setup -> Plugins, this
module reads it for the daemon, and `Settings.js` embeds a copy that the parity
tests hold to it. A new settings key goes into the manifest schema first.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "manifest.json"

_TYPES = ("enum", "integer", "boolean")


class SettingsSchemaError(RuntimeError):
    """manifest.json is missing, unreadable, or its settings schema is malformed."""


def load_schema(path: Path = MANIFEST_PATH) -> List[Dict[str, Any]]:
    """Read and check `barWidget.schema`; raises SettingsSchemaError instead of inventing defaults."""
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SettingsSchemaError(f"cannot read settings schema from {path}: {exc}") from exc
    schema = (manifest.get("barWidget") or {}).get("schema") if isinstance(manifest, dict) else None
    if not isinstance(schema, list) or not schema:
        raise SettingsSchemaError(f"{path} has no barWidget.schema list")
    seen = set()
    for field in schema:
        key = field.get("key") if isinstance(field, dict) else None
        kind = field.get("type") if isinstance(field, dict) else None
        if not isinstance(key, str) or not key or key in seen or kind not in _TYPES or "defaultValue" not in field:
            raise SettingsSchemaError(f"{path} has a malformed settings field: {field!r}")
        if kind == "enum" and field["defaultValue"] not in (field.get("options") or []):
            raise SettingsSchemaError(f"{path}: default of {key} is not one of its options")
        if kind == "integer" and not ("min" in field and "max" in field):
            raise SettingsSchemaError(f"{path}: integer {key} needs min and max")
        seen.add(key)
    return schema


SCHEMA: List[Dict[str, Any]] = load_schema()
FIELDS: Dict[str, Dict[str, Any]] = {f["key"]: f for f in SCHEMA}
DEFAULT_SETTINGS: Dict[str, Any] = {f["key"]: f["defaultValue"] for f in SCHEMA}


def _coerce(field: Mapping[str, Any], value: Any) -> Any:
    kind = field["type"]
    default = field["defaultValue"]
    if kind == "boolean":
        return value if isinstance(value, bool) else default
    if kind == "enum":
        return value if value in field["options"] else default
    # integer
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return int(max(field["min"], min(field["max"], int(number))))


MAX_BAR_ITEMS_FULL = 5


def max_bar_items_cap(bar_label_style: Any) -> int:
    """Up to 5 items in Full style; up to schema max (8) in Compact or Icons styles."""
    schema_max = int(FIELDS["maxBarItems"]["max"])
    if str(bar_label_style or "full") in ("compact", "icon"):
        return schema_max
    return min(schema_max, MAX_BAR_ITEMS_FULL)


def normalize_settings(raw: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Every schema key, typed and bounded; missing or invalid values fall back to the default.

    Keys outside the schema are dropped. The two thresholds also keep their linked
    gap (see PriorityLimits), and maxBarItems is capped at 5 when barLabelStyle is "full".
    """
    src = raw if isinstance(raw, Mapping) else {}
    out = {key: _coerce(field, src[key]) if key in src else field["defaultValue"] for key, field in FIELDS.items()}
    out["maxBarItems"] = min(out["maxBarItems"], max_bar_items_cap(out["barLabelStyle"]))
    lim = PriorityLimits(warning=out["warningThreshold"], critical=out["criticalThreshold"])
    out["warningThreshold"] = lim.warning
    out["criticalThreshold"] = lim.critical
    return out


def apply_patch(current: Mapping[str, Any], patch: Mapping[str, Any]) -> Dict[str, Any]:
    """Normalized settings after a patch; changing one threshold pushes the other to keep the gap."""
    base = normalize_settings(current)
    upd = {k: v for k, v in (patch or {}).items() if k in FIELDS}
    has_w = "warningThreshold" in upd
    has_c = "criticalThreshold" in upd
    if has_w != has_c:
        cur = PriorityLimits(warning=base["warningThreshold"], critical=base["criticalThreshold"])
        key = "warningThreshold" if has_w else "criticalThreshold"
        value = _coerce(FIELDS[key], upd[key])
        nxt = cur.with_warning(value) if has_w else cur.with_critical(value)
        upd["warningThreshold"] = nxt.warning
        upd["criticalThreshold"] = nxt.critical
    merged = dict(base)
    merged.update(upd)
    return normalize_settings(merged)


# ---------------------------------------------------------------------------
# Linked Priority thresholds (bounds and step come from the schema)
# ---------------------------------------------------------------------------

def _threshold_bounds(key: str) -> Dict[str, int]:
    field = FIELDS[key]
    return {"min": int(field["min"]), "max": int(field["max"]), "step": int(field.get("step") or 1)}


_WARN = _threshold_bounds("warningThreshold")
_CRIT = _threshold_bounds("criticalThreshold")
if _WARN["step"] != _CRIT["step"]:
    raise SettingsSchemaError("warningThreshold and criticalThreshold must share one step")


@dataclass
class PriorityLimits:
    warning: int = DEFAULT_SETTINGS["warningThreshold"]
    critical: int = DEFAULT_SETTINGS["criticalThreshold"]

    STEP = _WARN["step"]
    WARNING_MIN = _WARN["min"]
    WARNING_MAX = _WARN["max"]
    CRITICAL_MIN = _CRIT["min"]
    CRITICAL_MAX = _CRIT["max"]

    def __post_init__(self) -> None:
        self.warning = self._snap(self.warning, self.WARNING_MIN, self.WARNING_MAX)
        self.critical = self._snap(self.critical, self.CRITICAL_MIN, self.CRITICAL_MAX)
        if self.critical > self.warning - self.STEP:
            self.critical = max(self.CRITICAL_MIN, self.warning - self.STEP)
        if self.warning < self.critical + self.STEP:
            self.warning = min(self.WARNING_MAX, self.critical + self.STEP)

    @classmethod
    def _snap(cls, value: int, lo: int, hi: int) -> int:
        clamped = max(lo, min(hi, int(value)))
        return int(round(clamped / cls.STEP) * cls.STEP)

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any]) -> "PriorityLimits":
        s = normalize_settings(settings)
        return cls(warning=s["warningThreshold"], critical=s["criticalThreshold"])

    def with_warning(self, value: int) -> "PriorityLimits":
        w = self._snap(value, self.WARNING_MIN, self.WARNING_MAX)
        c = self.critical
        if c > w - self.STEP:
            c = max(self.CRITICAL_MIN, w - self.STEP)
        return PriorityLimits(warning=w, critical=c)

    def with_critical(self, value: int) -> "PriorityLimits":
        c = self._snap(value, self.CRITICAL_MIN, self.CRITICAL_MAX)
        w = self.warning
        if w < c + self.STEP:
            w = min(self.WARNING_MAX, c + self.STEP)
        return PriorityLimits(warning=w, critical=c)
