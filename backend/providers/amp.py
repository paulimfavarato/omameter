"""Amp usage provider runtime and pure displayText mapper."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from backend.http_client import HTTPResponse
from backend.i18n import tr
from backend.models import (
    MetricLine,
    MetricPeriod,
    MetricValue,
    ProviderSnapshot,
    format_iso8601,
)
from backend.providers.base import (
    FetchContext,
    ProviderError,
    make_snapshot,
    raise_for_auth_failure,
    raise_for_http_error,
    read_json,
)
from backend.providers.session import OAuthRefresh, authorized, jwt_or_expires_in_expiry

AMP_CLIENT_ID = "client_01JNSEYM3V0J5AXK4YXNXRXTGP"
AMP_AUTHENTICATE_URL = "https://authapi.ampcode.com/user_management/authenticate"
AMP_BALANCE_URL = "https://ampcode.com/api/internal?userDisplayBalanceInfo"

_RE_TIER = re.compile(
    r"Amp (?P<plan>[^\n]+?) Tier: agent usage \$(?P<left>[0-9.,]+) of \$(?P<limit>[0-9.,]+) remaining(?P<rest>[^\n]*)"
)
_RE_ORB = re.compile(r"orb usage (?P<left>[0-9.,]+)h of (?P<limit>[0-9.,]+)h")
_RE_PERIOD = re.compile(r"period (?P<start>\d{4}-\d{2}-\d{2}) to (?P<end>\d{4}-\d{2}-\d{2})")
_RE_FREE_DOLLARS = re.compile(r"Amp Free: \$(?P<left>[0-9.,]+)\s*/\s*\$(?P<limit>[0-9.,]+) remaining")
_RE_FREE_PERCENT = re.compile(r"Amp Free: (?P<left>[0-9.]+)% remaining")
_RE_CREDITS = re.compile(r"Individual credits: \$(?P<left>[0-9.,]+) remaining")
_RE_EMAIL = re.compile(r"Signed in as (?P<email>[^\s(]+)")


# WorkOS OAuth: 5-minute JWT access tokens, rotating refresh token.
OAUTH = OAuthRefresh(
    provider_name="Amp",
    token_url=AMP_AUTHENTICATE_URL,
    client_id=AMP_CLIENT_ID,
    expiry=jwt_or_expires_in_expiry,
    fallback_ttl_sec=3600.0,
)


def _parse_number(text: str) -> Optional[float]:
    try:
        val = float(text.replace(",", ""))
        return val if math.isfinite(val) else None
    except ValueError:
        return None


def _parse_billing_period(text: str) -> Optional[Tuple[datetime, datetime]]:
    m = _RE_PERIOD.search(text)
    if not m:
        return None
    try:
        start = datetime.strptime(m.group("start"), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = datetime.strptime(m.group("end"), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    if end <= start:
        return None
    return start, end


def extract_amp_display_text(body: Any) -> Optional[str]:
    if not isinstance(body, dict):
        return None
    result = body.get("result")
    if not isinstance(result, dict):
        return None
    text = result.get("displayText")
    if not isinstance(text, str):
        return None
    return text.replace("**", "")


def extract_amp_email(body: Any) -> Optional[str]:
    text = extract_amp_display_text(body)
    if not text:
        return None
    m = _RE_EMAIL.search(text)
    return m.group("email") if m else None


def map_amp_usage(body: Dict[str, Any]) -> Tuple[Optional[str], List[MetricLine], Optional[str]]:
    """Pure mapper from Amp `userDisplayBalanceInfo` JSON to (plan, MetricLines, email)."""
    text = extract_amp_display_text(body)
    if text is None:
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Amp"), kind="refused")

    plan: Optional[str] = None
    lines: List[MetricLine] = []

    tier_match = _RE_TIER.search(text)
    if tier_match:
        left = _parse_number(tier_match.group("left"))
        limit = _parse_number(tier_match.group("limit"))
        if left is not None and limit is not None and limit > 0:
            plan = tier_match.group("plan")
            rest = tier_match.group("rest")
            period = _parse_billing_period(rest)
            resets_at = format_iso8601(period[1]) if period else None
            period_ms = int((period[1] - period[0]).total_seconds() * 1000) if period else None
            used_dollars = round((limit - left) * 100.0) / 100.0
            lines.append(
                MetricLine(
                    key="agent",
                    title="Agent Usage",
                    line_type="progress",
                    kind="dollars",
                    used=used_dollars,
                    limit=limit,
                    currency_code="USD",
                    resets_at=resets_at,
                    period_duration_ms=period_ms,
                    resource_key="agentUsage",
                    resource_kind="consumption",
                    resource_unit="usd",
                )
            )
            orb_match = _RE_ORB.search(rest)
            if orb_match:
                orb_left = _parse_number(orb_match.group("left"))
                orb_limit = _parse_number(orb_match.group("limit"))
                if orb_left is not None and orb_limit is not None and orb_limit > 0:
                    used_hours = round((orb_limit - orb_left) * 10.0) / 10.0
                    lines.append(
                        MetricLine(
                            key="orbHours",
                            title="Orb Hours",
                            line_type="progress",
                            kind="count",
                            used=used_hours,
                            limit=orb_limit,
                            unit_suffix="hours",
                            resets_at=resets_at,
                            period_duration_ms=period_ms,
                            resource_key="orbHours",
                            resource_kind="consumption",
                            resource_unit="hours",
                        )
                    )

    free_dollar_match = _RE_FREE_DOLLARS.search(text)
    if free_dollar_match:
        f_left = _parse_number(free_dollar_match.group("left"))
        f_limit = _parse_number(free_dollar_match.group("limit"))
        if f_left is not None and f_limit is not None and f_limit > 0:
            lines.append(
                MetricLine(
                    key="free",
                    title="Amp Free",
                    line_type="progress",
                    kind="percent",
                    used=(f_limit - f_left) / f_limit * 100.0,
                    limit=100.0,
                    period_duration_ms=MetricPeriod.DAY_MS,
                    resource_key="free",
                    resource_kind="consumption",
                    resource_unit="percent",
                )
            )
    else:
        free_pct_match = _RE_FREE_PERCENT.search(text)
        if free_pct_match:
            f_left = _parse_number(free_pct_match.group("left"))
            if f_left is not None:
                lines.append(
                    MetricLine(
                        key="free",
                        title="Amp Free",
                        line_type="progress",
                        kind="percent",
                        used=100.0 - f_left,
                        limit=100.0,
                        period_duration_ms=MetricPeriod.DAY_MS,
                        resource_key="free",
                        resource_kind="consumption",
                        resource_unit="percent",
                    )
                )

    if plan is None and any(line.key == "free" for line in lines):
        plan = "Free"

    credits_match = _RE_CREDITS.search(text)
    if credits_match:
        c_left = _parse_number(credits_match.group("left"))
        if c_left is not None:
            lines.append(
                MetricLine(
                    key="credits",
                    title="Credits",
                    line_type="values",
                    kind="dollars",
                    currency_code="USD",
                    subtitle="left",
                    values=[MetricValue(number=c_left, kind="dollars")],
                    resource_key="credits",
                    resource_kind="balance",
                    resource_unit="usd",
                )
            )

    if not lines:
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Amp"), kind="refused")

    email = extract_amp_email(body)
    return plan, lines, email


def fetch(ctx: FetchContext) -> ProviderSnapshot:
    """Fetch live Amp balance with proactive WorkOS OAuth token refresh."""
    def _send(access_token: str) -> HTTPResponse:
        return ctx.http.post(
            AMP_BALANCE_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json_body={"method": "userDisplayBalanceInfo", "params": {}},
            timeout=15.0,
        )

    _, resp = authorized(ctx, OAUTH, _send)
    raise_for_auth_failure(resp, "Amp")
    raise_for_http_error(resp, "Amp")
    body = read_json(resp, "Amp")

    plan, lines, email = map_amp_usage(body)
    return make_snapshot(ctx, plan=plan, lines=lines, account_label_discovered=email)
