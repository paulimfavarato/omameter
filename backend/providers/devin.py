"""Devin provider client and usage mapper (ported from DevinUsageClient/DevinUsageMapper.swift)."""

from __future__ import annotations

import math
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from backend.i18n import tr
from backend.models import (
    MetricLine,
    MetricPeriod,
    MetricValue,
    ProviderSnapshot,
    format_iso8601,
)
from backend.providers.base import (
    parse_number as _parse_number,
    clamp_percent as _clamp_percent,
    FetchContext,
    ProviderError,
    make_snapshot,
    raise_for_http_error,
    read_json,
    send_mapped,
)


DEFAULT_API_SERVER_URL = "https://server.codeium.com"
API_SERVER_URL_KEY = "api_server_url"
CLOUD_COMPAT_VERSION = "1.108.2"
USER_STATUS_PATH = "/exa.seat_management_pb.SeatManagementService/GetUserStatus"


def _parse_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"true", "1"}:
            return True
        if low in {"false", "0"}:
            return False
    return None


def _parse_quota(
    plan_status: Dict[str, Any],
    remaining_key: str,
    reset_key: str,
) -> Optional[Tuple[float, Optional[datetime]]]:
    """Parse a Devin quota window.

    Proto3 JSON omits zero values, so a window with a reset timestamp and no remaining share
    means 0% remaining. A remaining field that is present but non-numeric indicates a schema error.
    """
    reset_raw = _parse_number(plan_status.get(reset_key))
    resets_at: Optional[datetime] = None
    if reset_raw is not None:
        try:
            resets_at = datetime.fromtimestamp(reset_raw, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            resets_at = None

    if remaining_key not in plan_status or plan_status[remaining_key] is None:
        if resets_at is not None:
            return (0.0, resets_at)
        return None

    remaining = _parse_number(plan_status[remaining_key])
    if remaining is None:
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Devin"), kind="refused")
    return (remaining, resets_at)


def _quota_line(
    key: str,
    title: str,
    quota: Tuple[float, Optional[datetime]],
    period_ms: int,
) -> MetricLine:
    remaining, resets_at = quota
    return MetricLine(
        key=key,
        title=title,
        line_type="progress",
        kind="percent",
        used=_clamp_percent(100.0 - remaining),
        limit=100.0,
        resets_at=format_iso8601(resets_at),
        period_duration_ms=period_ms,
        resource_key=key,
        resource_kind="consumption",
        resource_unit="percent",
    )


def map_devin_usage(payload: Any) -> Tuple[Optional[str], List[MetricLine], Optional[str]]:
    """Map GetUserStatus JSON response into (plan, metric_lines, discovered_email)."""
    if not isinstance(payload, dict):
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Devin"), kind="refused")
    user_status = payload.get("userStatus")
    if not isinstance(user_status, dict):
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Devin"), kind="refused")

    plan_status = user_status.get("planStatus")
    if not isinstance(plan_status, dict):
        plan_status = {}
    plan_info = plan_status.get("planInfo")
    if not isinstance(plan_info, dict):
        plan_info = {}

    daily = _parse_quota(plan_status, "dailyQuotaRemainingPercent", "dailyQuotaResetAtUnix")
    weekly = _parse_quota(plan_status, "weeklyQuotaRemainingPercent", "weeklyQuotaResetAtUnix")

    lines: List[MetricLine] = []
    if _parse_bool(plan_info.get("hideDailyQuota")) is True:
        # A plan that hides its daily quota still meters it. With no weekly window,
        # the Weekly row carries it without a reset timestamp.
        shown = weekly if weekly is not None else ((daily[0], None) if daily is not None else None)
        if shown is not None:
            lines.append(_quota_line("weekly", "Weekly", shown, MetricPeriod.WEEK_MS))
    else:
        if daily is not None:
            lines.append(_quota_line("daily", "Daily", daily, MetricPeriod.DAY_MS))
        if weekly is not None:
            lines.append(_quota_line("weekly", "Weekly", weekly, MetricPeriod.WEEK_MS))

    overage_micros = _parse_number(plan_status.get("overageBalanceMicros"))
    if overage_micros is not None:
        balance_dollars = max(0.0, overage_micros) / 1_000_000.0
        lines.append(
            MetricLine(
                key="extra",
                title="Extra Usage",
                line_type="values",
                kind="dollars",
                currency_code="USD",
                values=[
                    MetricValue(
                        number=balance_dollars,
                        kind="dollars",
                        label="left",
                        currency_code="USD",
                    )
                ],
                resource_key="extraUsageBalance",
                resource_kind="balance",
                resource_unit="usd",
            )
        )

    if not lines:
        raise ProviderError(tr("provider.devin.noQuota", "Devin reports no quota for this account."), kind="no_quota")

    raw_plan = plan_info.get("planName")
    plan = raw_plan.strip() if isinstance(raw_plan, str) and raw_plan.strip() else None

    raw_email = user_status.get("email")
    email = raw_email.strip() if isinstance(raw_email, str) and raw_email.strip() else None

    return plan, lines, email


def _resolve_api_server_url(credential: Dict[str, Any]) -> str:
    extras = credential.get("extras")
    candidate = None
    if isinstance(extras, dict):
        candidate = extras.get(API_SERVER_URL_KEY)
    if not candidate:
        candidate = credential.get(API_SERVER_URL_KEY)
    if isinstance(candidate, str) and candidate.strip():
        parsed = urllib.parse.urlsplit(candidate.strip())
        scheme = (parsed.scheme or "").lower()
        host = (parsed.hostname or "").lower()
        if parsed.netloc and (scheme == "https" or (scheme == "http" and host in {"localhost", "127.0.0.1", "::1"})):
            return candidate.strip().rstrip("/")
    return DEFAULT_API_SERVER_URL


def fetch(ctx: FetchContext) -> ProviderSnapshot:
    """Fetch Devin seat/plan quota via SeatManagementService/GetUserStatus."""
    access_token = str(ctx.credential.get("accessToken") or "").strip()
    if not access_token and not ctx.account.is_hub_account:
        raise ProviderError(tr("error.signInExpired", "Sign-in expired for %1. Sign in again.", "Devin"), kind="auth_expired")

    base_url = _resolve_api_server_url(ctx.credential)
    url = f"{base_url}{USER_STATUS_PATH}"
    body = {
        "metadata": {
            "apiKey": access_token,
            "ideName": "devin",
            "ideVersion": CLOUD_COMPAT_VERSION,
            "extensionName": "devin",
            "extensionVersion": CLOUD_COMPAT_VERSION,
            "locale": "en",
        }
    }
    headers = {
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
    }

    resp = send_mapped(lambda: ctx.http.post(url, headers=headers, json_body=body, timeout=15.0))

    raise_for_http_error(resp, "Devin")

    payload = read_json(resp, "Devin", expect=None, invalid_message=tr("error.invalidResponse", "%1 returned an invalid response.", "Devin"))

    plan, lines, email = map_devin_usage(payload)
    return make_snapshot(ctx, plan=plan, lines=lines, account_label_discovered=email)
