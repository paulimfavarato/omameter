"""Exact omameter.limits.v2 JSON envelope serializer for the `omameter` CLI."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from backend.models import Account, MetricLine, ProviderSnapshot, format_iso8601, utc_now


SCHEMA_VERSION = "omameter.limits.v2"


def _resolve_unit(line: MetricLine) -> str:
    if line.line_type == "progress":
        if line.kind == "percent":
            return "percent"
        if line.kind == "dollars":
            return (line.currency_code or "usd").lower()
        if line.unit_suffix and line.unit_suffix.strip():
            return line.unit_suffix.strip().lower()
        return (line.resource_unit or "count").lower()
    if line.values and line.values[0].currency_code:
        return line.values[0].currency_code.lower()
    return (line.resource_unit or "count").lower()


def _encode_resource(line: MetricLine) -> Optional[Dict[str, Any]]:
    if not line.resource_key:
        return None
    unit = _resolve_unit(line)
    kind = line.resource_kind or "consumption"

    if line.is_bounded and line.limit is not None:
        bounded_limit = max(0.0, float(line.limit))
        bounded_used = max(0.0, float(line.used))
        res: Dict[str, Any] = {
            "kind": kind,
            "unit": unit,
            "limit": bounded_limit,
            "remaining": max(0.0, bounded_limit - bounded_used),
        }
        if kind == "consumption":
            res["used"] = bounded_used
        else:
            res["available"] = bounded_used
        if bounded_limit > 0:
            res["utilization"] = bounded_used / bounded_limit
        if line.resets_at:
            res["resetsAt"] = line.resets_at
        if line.period_duration_ms and line.period_duration_ms > 0:
            res["windowSeconds"] = line.period_duration_ms / 1000.0
        return res

    if line.values:
        val = line.values[0]
        res = {
            "kind": kind,
            "unit": unit,
        }
        if kind == "balance":
            res["available"] = float(val.number)
        else:
            res["used"] = float(val.number)
        if line.expiries_at:
            res["expiresAt"] = sorted(line.expiries_at)
        return res

    return None


def account_matches_filter(acc: Any, filter_arg: str) -> bool:
    """True when the CLI filter names this account's provider kind, card id, or id."""
    return filter_arg in (acc.kind, acc.card_id, acc.id)


def build_limits_envelope(
    accounts: List[Account],
    snapshots: Dict[str, ProviderSnapshot],
    *,
    now: Optional[datetime] = None,
    filter_arg: Optional[str] = None,
) -> Dict[str, Any]:
    now = now or utc_now()
    providers: Dict[str, Any] = {}

    for acc in accounts:
        if filter_arg and not account_matches_filter(acc, filter_arg):
            continue
        snap = snapshots.get(acc.id)
        if snap is None:
            continue
        resources: Dict[str, Any] = {}
        for line in snap.lines:
            if not line.resource_key:
                continue
            encoded = _encode_resource(line)
            if encoded is not None:
                resources[line.resource_key] = encoded

        entry: Dict[str, Any] = {
            "displayName": acc.full_name,
            "fetchedAt": snap.refreshed_at,
            "expiresAt": snap.stale_at,
            "stale": snap.is_stale(now),
            "resources": resources,
        }
        if snap.plan:
            entry["plan"] = snap.plan
        providers[acc.card_id] = entry

    return {
        "schema": SCHEMA_VERSION,
        "generatedAt": format_iso8601(now),
        "providers": providers,
    }


def encode_limits_envelope_json(envelope: Dict[str, Any]) -> str:
    try:
        return json.dumps(envelope, indent=2, sort_keys=True)
    except Exception:
        return json.dumps({"providers": {}, "schema": SCHEMA_VERSION}, sort_keys=True)
