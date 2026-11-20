"""JWT claim decoding shared by sign-in flows and providers (no signature check)."""

from __future__ import annotations

import base64
import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def decode_jwt_payload(token: str) -> Optional[Dict[str, Any]]:
    """Claims of a JWT's payload segment, or None when it isn't a decodable JWT."""
    if not token or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    segment = parts[1]
    pad = "=" * ((4 - len(segment) % 4) % 4)
    try:
        parsed = json.loads(base64.urlsafe_b64decode(segment + pad).decode("utf-8"))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def jwt_expiry(token: str) -> Optional[datetime]:
    """The `exp` claim as a UTC datetime, or None when absent or invalid."""
    payload = decode_jwt_payload(token)
    if not payload:
        return None
    exp = payload.get("exp")
    if isinstance(exp, bool):
        return None
    try:
        secs = float(exp)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(secs) or secs <= 0:
        return None
    return datetime.fromtimestamp(secs, tz=timezone.utc)
