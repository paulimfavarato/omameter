"""Cursor usage provider runtime, 30-day usage events history, and pure mappers."""

from __future__ import annotations

import math
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from backend.http_client import HTTPResponse, HTTPTransportError
from backend.i18n import tr
from backend.models import (
    MetricLine,
    MetricPeriod,
    MetricValue,
    ProviderSnapshot,
    UsageBreakdown,
    UsageBreakdownModel,
    format_iso8601,
    parse_iso8601,
    utc_now,
)
from backend.jwt_claims import decode_jwt_payload
from backend.providers.base import (
    FetchContext,
    ProviderError,
    is_auth_failure,
    make_snapshot,
    parse_retry_after,
    raise_for_auth_failure,
    raise_for_http_error,
    read_json,
    safe_float,
)
from backend.providers.session import OAuthRefresh, authorized, jwt_only_expiry

CURSOR_CLIENT_ID = "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB"
CURSOR_REFRESH_URL = "https://api2.cursor.sh/oauth/token"
CURSOR_USAGE_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
CURSOR_GROK_BOT_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetSandUsageStatus"
CURSOR_PLAN_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetPlanInfo"
CURSOR_CREDITS_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCreditGrantsBalance"
CURSOR_COMPLAT_CREDITS_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetComplatCreditBalance"
CURSOR_USAGE_STATUS_URL = "https://cursor.com/api/dashboard/v2/usage-status?scope=user"
CURSOR_REST_USAGE_URL = "https://cursor.com/api/usage"
CURSOR_USAGE_SUMMARY_URL = "https://cursor.com/api/usage-summary"
CURSOR_STRIPE_URL = "https://cursor.com/api/auth/stripe"
CURSOR_PROFILE_URL = "https://cursor.com/api/auth/me"
CURSOR_USAGE_EVENTS_URL = "https://cursor.com/api/dashboard/get-filtered-usage-events"

CURSOR_REFRESH_LEAD_SEC = 7 * 86400.0
BILLING_PERIOD_MS = MetricPeriod.MONTH_MS
CHARGEABLE_EVENT_KIND = "USAGE_EVENT_KIND_USAGE_BASED"
ORG_MEMBERSHIP_TYPES = {"enterprise", "team"}

def cursor_session(access_token: str) -> Optional[Tuple[str, str]]:
    """Derive (user_id, WorkosCursorSessionToken) from JWT `sub`."""
    payload = decode_jwt_payload(access_token)
    if not payload:
        return None
    sub = payload.get("sub")
    if not isinstance(sub, str):
        return None
    trimmed = sub.strip()
    if not trimmed:
        return None
    parts = trimmed.split("|")
    user_id = parts[1] if len(parts) > 1 else parts[0]
    if not user_id:
        return None
    return user_id, f"{user_id}%3A%3A{access_token}"


def _should_logout(resp: HTTPResponse, body: Any) -> bool:
    return isinstance(body, dict) and body.get("shouldLogout") is True


# Cursor trades its 60-day refresh JWT for a new 60-day JWT; renew a week early.
OAUTH = OAuthRefresh(
    provider_name="Cursor",
    token_url=CURSOR_REFRESH_URL,
    client_id=CURSOR_CLIENT_ID,
    expired_message=lambda: tr("error.signInExpired", "Sign-in expired for %1. Sign in again.", "Cursor"),
    margin_sec=CURSOR_REFRESH_LEAD_SEC,
    expiry=jwt_only_expiry,
    drop_unknown_expiry=True,
    expiry_from_jwt=True,
    refresh_token_fallback_to_access=True,
    is_terminal=_should_logout,
)


def _cents_to_dollars(cents: float) -> float:
    return cents / 100.0


def _clamp_percent(val: float) -> float:
    return max(0.0, min(100.0, val))


def _plan_label(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    words = [w for w in trimmed.split() if w]
    return " ".join(w[0].upper() + w[1:] for w in words)


def _normalized_membership(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    low = value.strip().lower()
    return low or None


def resolved_plan_name(plan_info_name: Optional[str], membership_type: Optional[str]) -> Optional[str]:
    membership = _normalized_membership(membership_type)
    plan_info = plan_info_name.strip() if isinstance(plan_info_name, str) and plan_info_name.strip() else None
    if membership in ORG_MEMBERSHIP_TYPES and (plan_info is None or plan_info.lower() in ("pro", "pro plan")):
        return membership
    return plan_info or membership


def resolved_plan_label(plan_info_name: Optional[str], membership_type: Optional[str]) -> Optional[str]:
    return _plan_label(resolved_plan_name(plan_info_name, membership_type))


def stripe_balance_cents(body: Optional[Dict[str, Any]]) -> float:
    if not isinstance(body, dict):
        return 0.0
    bal = safe_float(body.get("customerBalance"))
    if bal is None or not math.isfinite(bal) or bal >= 0:
        return 0.0
    return abs(bal)


class CursorPlanUsageFacts:
    def __init__(self, usage: Dict[str, Any]) -> None:
        self.is_enabled = usage.get("enabled") is not False
        plan_usage = usage.get("planUsage") if isinstance(usage.get("planUsage"), dict) else None
        self.has_plan_usage = plan_usage is not None
        self.limit = safe_float(plan_usage.get("limit")) if plan_usage else None
        self.total_percent_used = safe_float(plan_usage.get("totalPercentUsed")) if plan_usage else None
        auto = safe_float(plan_usage.get("autoPercentUsed")) if plan_usage else None
        api = safe_float(plan_usage.get("apiPercentUsed")) if plan_usage else None
        if plan_usage:
            total_spend = safe_float(plan_usage.get("totalSpend"))
            if total_spend is not None:
                spent = total_spend
            else:
                lim = self.limit or 0.0
                rem = safe_float(plan_usage.get("remaining"))
                spent = lim - (rem if rem is not None else lim)
        else:
            spent = 0.0

        if auto is not None and api is not None and math.isfinite(auto) and math.isfinite(api) and auto >= 0 and api >= 0:
            self.has_model_pools = auto > 0 or api > 0 or spent == 0
        else:
            self.has_model_pools = False

        spend_limit_usage = usage.get("spendLimitUsage") if isinstance(usage.get("spendLimitUsage"), dict) else None
        raw_lt = spend_limit_usage.get("limitType") if spend_limit_usage else None
        self.spend_limit_type = raw_lt.lower() if isinstance(raw_lt, str) else None
        self.pooled_limit = safe_float(spend_limit_usage.get("pooledLimit"), 0.0) if spend_limit_usage else 0.0
        if self.pooled_limit is None:
            self.pooled_limit = 0.0

    @property
    def has_limit(self) -> bool:
        return self.limit is not None

    @property
    def has_total_usage_percent(self) -> bool:
        return self.total_percent_used is not None

    @property
    def plan_usage_limit_missing(self) -> bool:
        return self.has_plan_usage and not self.has_limit

    @property
    def plan_usage_unusable(self) -> bool:
        return not self.has_plan_usage or (self.plan_usage_limit_missing and not self.has_model_pools)

    @property
    def is_team_by_shape(self) -> bool:
        return self.spend_limit_type == "team" or self.pooled_limit > 0

    @property
    def should_try_generic_request_fallback(self) -> bool:
        return self.is_enabled and self.has_plan_usage and not self.has_limit and not self.has_total_usage_percent and not self.has_model_pools


def should_use_request_based_fallback(facts: CursorPlanUsageFacts, plan_name: Optional[str]) -> Tuple[bool, str]:
    if not facts.is_enabled:
        return False, ""
    normalized_plan = plan_name.strip().lower() if isinstance(plan_name, str) else ""
    if facts.plan_usage_unusable and not facts.has_total_usage_percent and normalized_plan == "enterprise":
        return True, tr("provider.cursor.enterpriseUnavailable", "Enterprise usage data unavailable. Try again later.")
    if facts.plan_usage_unusable and not facts.has_total_usage_percent and normalized_plan == "team":
        return True, tr("provider.cursor.teamRequestUnavailable", "Team request-based usage data unavailable. Try again later.")
    if facts.plan_usage_unusable and not facts.has_total_usage_percent and not normalized_plan and plan_name is None:
        return True, tr("provider.cursor.requestUnavailable", "Cursor request-based usage data unavailable. Try again later.")
    if facts.is_team_by_shape and facts.plan_usage_limit_missing and not facts.has_model_pools:
        return True, tr("provider.cursor.requestUnavailable", "Cursor request-based usage data unavailable. Try again later.")
    return False, ""


def map_grok_bot_usage(usage: Dict[str, Any]) -> Optional[MetricLine]:
    if usage.get("usesPooledEnterpriseAllowance") is True:
        return None
    if usage.get("hasNonZeroIncludedLimit") is False:
        return None
    if usage.get("includedLimitZero") is True:
        return None
    pct = safe_float(usage.get("usagePercent"))
    if pct is None or not math.isfinite(pct) or pct < 0:
        return None

    reset_dt = parse_iso8601(usage.get("nextResetTimestampUtc"))
    start_dt = parse_iso8601(usage.get("currentPeriodStart"))
    if start_dt is not None and reset_dt is not None and reset_dt > start_dt:
        period_ms = int((reset_dt - start_dt).total_seconds() * 1000)
    else:
        period_ms = MetricPeriod.WEEK_MS

    return MetricLine(
        key="grokBot",
        title="Grok Bot",
        line_type="progress",
        kind="percent",
        used=_clamp_percent(pct),
        limit=100.0,
        resets_at=format_iso8601(reset_dt),
        period_duration_ms=period_ms,
        resource_key="grokBot",
        resource_kind="consumption",
        resource_unit="percent",
    )


def _dollar_meter(bucket: Any) -> Optional[Tuple[float, float]]:
    if not isinstance(bucket, dict) or bucket.get("enabled") is False:
        return None
    limit = safe_float(bucket.get("limit"))
    if limit is None or not math.isfinite(limit) or limit <= 0:
        return None
    reported_used = safe_float(bucket.get("used"))
    rem = safe_float(bucket.get("remaining"))
    inferred_used = max(0.0, limit - (rem if rem is not None else limit))
    used = reported_used if (reported_used is not None and reported_used > 0) else inferred_used
    return max(0.0, used), limit


def _usable_dollar_bucket(bucket: Any) -> bool:
    if not isinstance(bucket, dict) or bucket.get("enabled") is False:
        return False
    return _dollar_meter(bucket) is not None or (safe_float(bucket.get("used"), 0.0) or 0.0) > 0


def has_usable_summary_payload(summary: Dict[str, Any]) -> bool:
    start = parse_iso8601(summary.get("billingCycleStart"))
    end = parse_iso8601(summary.get("billingCycleEnd"))
    if start is not None and end is not None and end > start:
        return True
    ind = summary.get("individualUsage") if isinstance(summary.get("individualUsage"), dict) else {}
    team = summary.get("teamUsage") if isinstance(summary.get("teamUsage"), dict) else {}
    plan = ind.get("plan") if isinstance(ind.get("plan"), dict) else {}
    if any(safe_float(plan.get(k)) is not None for k in ("totalPercentUsed", "autoPercentUsed", "apiPercentUsed")):
        return True
    return any(_usable_dollar_bucket(b) for b in (ind.get("onDemand"), ind.get("overall"), team.get("onDemand"), team.get("pooled")))


def _summary_billing_cycle(summary: Optional[Dict[str, Any]], request_usage: Optional[Dict[str, Any]]) -> Tuple[Optional[str], int]:
    if isinstance(summary, dict):
        start = parse_iso8601(summary.get("billingCycleStart"))
        end = parse_iso8601(summary.get("billingCycleEnd"))
        if start is not None and end is not None and end > start:
            return format_iso8601(end), int((end - start).total_seconds() * 1000)
    if isinstance(request_usage, dict):
        req_start = parse_iso8601(request_usage.get("startOfMonth"))
        if req_start is not None:
            return format_iso8601(req_start + timedelta(milliseconds=BILLING_PERIOD_MS)), BILLING_PERIOD_MS
    return None, BILLING_PERIOD_MS


def _append_summary_on_demand_bucket(bucket: Any, resets_at: Optional[str], period_ms: int, lines: List[MetricLine]) -> bool:
    if not isinstance(bucket, dict) or bucket.get("enabled") is False:
        return False
    meter = _dollar_meter(bucket)
    if meter is not None:
        lines.append(
            MetricLine(
                key="onDemand",
                title="Extra Usage",
                line_type="progress",
                kind="dollars",
                used=_cents_to_dollars(meter[0]),
                limit=_cents_to_dollars(meter[1]),
                currency_code="USD",
                resets_at=resets_at,
                period_duration_ms=period_ms,
                resource_key="onDemand",
                resource_kind="consumption",
                resource_unit="usd",
            )
        )
        return True
    used_cents = safe_float(bucket.get("used"))
    if used_cents is not None and used_cents > 0:
        lines.append(
            MetricLine(
                key="onDemand",
                title="Extra Usage",
                line_type="values",
                kind="dollars",
                currency_code="USD",
                subtitle="spent",
                values=[MetricValue(number=_cents_to_dollars(used_cents), kind="dollars")],
                resource_key="onDemand",
                resource_kind="consumption",
                resource_unit="usd",
            )
        )
        return True
    return False


def _append_summary_on_demand(summary: Optional[Dict[str, Any]], resets_at: Optional[str], period_ms: int, lines: List[MetricLine]) -> None:
    if not isinstance(summary, dict):
        return
    ind = summary.get("individualUsage") if isinstance(summary.get("individualUsage"), dict) else {}
    team = summary.get("teamUsage") if isinstance(summary.get("teamUsage"), dict) else {}
    if _append_summary_on_demand_bucket(ind.get("onDemand"), resets_at, period_ms, lines):
        return
    _append_summary_on_demand_bucket(team.get("onDemand"), resets_at, period_ms, lines)


def map_cursor_summary(
    *,
    summary: Optional[Dict[str, Any]],
    request_usage: Optional[Dict[str, Any]],
    plan_name: Optional[str],
    unavailable_message: str,
) -> Tuple[Optional[str], List[MetricLine]]:
    """Pure mapper for request-based / summary fallback Cursor accounts."""
    resets_at, period_ms = _summary_billing_cycle(summary, request_usage)
    lines: List[MetricLine] = []

    has_requests = False
    gpt4 = request_usage.get("gpt-4") if isinstance(request_usage, dict) else None
    if isinstance(gpt4, dict):
        req_limit = safe_float(gpt4.get("maxRequestUsage"))
        if req_limit is not None and req_limit > 0:
            req_used = safe_float(gpt4.get("numRequests"))
            if req_used is None:
                req_used = safe_float(gpt4.get("numRequestsTotal"), 0.0) or 0.0
            req_used = max(0.0, req_used)
            lines.append(
                MetricLine(
                    key="usage",
                    title="Total Usage",
                    line_type="progress",
                    kind="count",
                    used=req_used,
                    limit=req_limit,
                    unit_suffix="requests",
                    resets_at=resets_at,
                    period_duration_ms=period_ms,
                    resource_key="totalUsage",
                    resource_kind="consumption",
                    resource_unit="requests",
                )
            )
            lines.append(
                MetricLine(
                    key="requests",
                    title="Requests",
                    line_type="progress",
                    kind="count",
                    used=req_used,
                    limit=req_limit,
                    unit_suffix="requests",
                    resets_at=resets_at,
                    period_duration_ms=period_ms,
                    resource_key="requests",
                    resource_kind="consumption",
                    resource_unit="requests",
                )
            )
            has_requests = True

    if not has_requests and isinstance(summary, dict):
        ind = summary.get("individualUsage") if isinstance(summary.get("individualUsage"), dict) else {}
        team = summary.get("teamUsage") if isinstance(summary.get("teamUsage"), dict) else {}
        limit_type = (summary.get("limitType") or "").lower() if isinstance(summary.get("limitType"), str) else ""
        ind_plan = ind.get("plan") if isinstance(ind.get("plan"), dict) else {}

        pooled_meter = _dollar_meter(team.get("pooled"))
        total_pct = safe_float(ind_plan.get("totalPercentUsed"))
        overall_meter = _dollar_meter(ind.get("overall"))

        if limit_type == "team" and pooled_meter is not None:
            lines.append(
                MetricLine(
                    key="usage",
                    title="Total Usage",
                    line_type="progress",
                    kind="dollars",
                    used=_cents_to_dollars(pooled_meter[0]),
                    limit=_cents_to_dollars(pooled_meter[1]),
                    currency_code="USD",
                    resets_at=resets_at,
                    period_duration_ms=period_ms,
                    resource_key="totalUsage",
                    resource_kind="consumption",
                    resource_unit="usd",
                )
            )
        elif total_pct is not None:
            lines.append(
                MetricLine(
                    key="usage",
                    title="Total Usage",
                    line_type="progress",
                    kind="percent",
                    used=total_pct,
                    limit=100.0,
                    resets_at=resets_at,
                    period_duration_ms=period_ms,
                    resource_key="totalUsage",
                    resource_kind="consumption",
                    resource_unit="percent",
                )
            )
        elif overall_meter is not None:
            lines.append(
                MetricLine(
                    key="usage",
                    title="Total Usage",
                    line_type="progress",
                    kind="dollars",
                    used=_cents_to_dollars(overall_meter[0]),
                    limit=_cents_to_dollars(overall_meter[1]),
                    currency_code="USD",
                    resets_at=resets_at,
                    period_duration_ms=period_ms,
                    resource_key="totalUsage",
                    resource_kind="consumption",
                    resource_unit="usd",
                )
            )
        elif pooled_meter is not None:
            lines.append(
                MetricLine(
                    key="usage",
                    title="Total Usage",
                    line_type="progress",
                    kind="dollars",
                    used=_cents_to_dollars(pooled_meter[0]),
                    limit=_cents_to_dollars(pooled_meter[1]),
                    currency_code="USD",
                    resets_at=resets_at,
                    period_duration_ms=period_ms,
                    resource_key="totalUsage",
                    resource_kind="consumption",
                    resource_unit="usd",
                )
            )

    if isinstance(summary, dict):
        ind = summary.get("individualUsage") if isinstance(summary.get("individualUsage"), dict) else {}
        ind_plan = ind.get("plan") if isinstance(ind.get("plan"), dict) else {}
        for src_key, mkey, title, rkey in (
            ("autoPercentUsed", "auto", "Cursor Models", "autoUsage"),
            ("apiPercentUsed", "api", "Other Models", "apiUsage"),
        ):
            pct = safe_float(ind_plan.get(src_key))
            if pct is not None:
                lines.append(
                    MetricLine(
                        key=mkey,
                        title=title,
                        line_type="progress",
                        kind="percent",
                        used=pct,
                        limit=100.0,
                        resets_at=resets_at,
                        period_duration_ms=period_ms,
                        resource_key=rkey,
                        resource_kind="consumption",
                        resource_unit="percent",
                    )
                )

    _append_summary_on_demand(summary, resets_at, period_ms, lines)

    if not lines:
        raise ProviderError(unavailable_message or tr("provider.cursor.usageUnavailable", "Cursor usage data unavailable."), kind="no_quota")

    membership = summary.get("membershipType") if isinstance(summary, dict) and isinstance(summary.get("membershipType"), str) else None
    return resolved_plan_label(plan_name, membership), lines


def _usage_billing_cycle(usage: Dict[str, Any]) -> Tuple[Optional[str], int]:
    cycle_start = safe_float(usage.get("billingCycleStart"))
    cycle_end = safe_float(usage.get("billingCycleEnd"))
    if cycle_start is not None and cycle_end is not None and cycle_end > cycle_start:
        end_dt = datetime.fromtimestamp(cycle_end / 1000.0, tz=timezone.utc)
        return format_iso8601(end_dt), int(round(cycle_end - cycle_start))
    if cycle_end is not None and cycle_end > 0:
        end_dt = datetime.fromtimestamp(cycle_end / 1000.0, tz=timezone.utc)
        return format_iso8601(end_dt), BILLING_PERIOD_MS
    return None, BILLING_PERIOD_MS


def _on_demand_spend_cents(reported: List[float], limit: float, remaining: Optional[float]) -> float:
    for val in reported:
        if val > 0:
            return val
    inferred = max(0.0, limit - remaining) if remaining is not None else 0.0
    if inferred > 0:
        return inferred
    return reported[0] if reported else 0.0


def map_cursor_usage(
    usage: Dict[str, Any],
    *,
    plan_name: Optional[str] = None,
    membership_type: Optional[str] = None,
    credit_grants: Optional[Dict[str, Any]] = None,
    stripe_balance_cents_val: float = 0.0,
    unified_credit_balance: Optional[Dict[str, Any]] = None,
    unified_usage_status: Optional[Dict[str, Any]] = None,
    is_merged: bool = False,
    grok_bot: Optional[Dict[str, Any]] = None,
    summary_for_on_demand: Optional[Dict[str, Any]] = None,
    history_days: Optional[Dict[str, Dict[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> Tuple[Optional[str], List[MetricLine]]:
    """Pure mapper from Cursor `GetCurrentPeriodUsage` JSON to (plan, MetricLines)."""
    facts = CursorPlanUsageFacts(usage)
    plan_usage = usage.get("planUsage") if isinstance(usage.get("planUsage"), dict) else None
    if not facts.is_enabled or plan_usage is None:
        raise ProviderError(tr("provider.cursor.noSubscription", "No active Cursor subscription."), kind="no_quota")

    if not (facts.has_limit or facts.has_total_usage_percent or facts.has_model_pools):
        raise ProviderError(tr("provider.cursor.totalLimitMissing", "Total usage limit missing from API response."), kind="no_quota")

    normalized_plan = (plan_name or "").strip().lower()
    lines: List[MetricLine] = []

    # Credits
    if is_merged:
        if isinstance(unified_credit_balance, dict):
            rem_cents = max(0.0, safe_float(unified_credit_balance.get("usdRemainingCents"), 0.0) or 0.0)
            lines.append(
                MetricLine(
                    key="credits",
                    title="Credits",
                    line_type="values",
                    kind="dollars",
                    currency_code="USD",
                    subtitle="left",
                    values=[MetricValue(number=_cents_to_dollars(rem_cents), kind="dollars")],
                    resource_key="credits",
                    resource_kind="balance",
                    resource_unit="usd",
                )
            )
    else:
        has_grants = isinstance(credit_grants, dict) and credit_grants.get("hasCreditGrants") is True
        grant_total = (safe_float(credit_grants.get("totalCents"), 0.0) or 0.0) if has_grants else 0.0
        grant_used = (safe_float(credit_grants.get("usedCents"), 0.0) or 0.0) if has_grants else 0.0
        has_valid_grant = has_grants and grant_total > 0
        combined_total = (grant_total if has_valid_grant else 0.0) + stripe_balance_cents_val
        rem_cents = max(0.0, combined_total - (grant_used if has_valid_grant else 0.0))
        if credit_grants is not None or combined_total > 0:
            lines.append(
                MetricLine(
                    key="credits",
                    title="Credits",
                    line_type="values",
                    kind="dollars",
                    currency_code="USD",
                    subtitle="left",
                    values=[MetricValue(number=_cents_to_dollars(rem_cents), kind="dollars")],
                    resource_key="credits",
                    resource_kind="balance",
                    resource_unit="usd",
                )
            )

    # Total Usage
    total_spend_val = safe_float(plan_usage.get("totalSpend"))
    if total_spend_val is not None:
        plan_used_cents = total_spend_val
    else:
        plan_used_cents = (facts.limit or 0.0) - (safe_float(plan_usage.get("remaining"), 0.0) or 0.0)
    computed_pct = (plan_used_cents / facts.limit * 100.0) if (facts.limit is not None and facts.limit > 0) else 0.0
    total_pct = facts.total_percent_used if facts.total_percent_used is not None else computed_pct

    resets_at, period_ms = _usage_billing_cycle(usage)
    is_team_account = normalized_plan == "team" or facts.is_team_by_shape

    if is_team_account and facts.has_model_pools:
        if facts.total_percent_used is not None:
            lines.append(
                MetricLine(
                    key="usage",
                    title="Total Usage",
                    line_type="progress",
                    kind="percent",
                    used=facts.total_percent_used,
                    limit=100.0,
                    resets_at=resets_at,
                    period_duration_ms=period_ms,
                    resource_key="totalUsage",
                    resource_kind="consumption",
                    resource_unit="percent",
                )
            )
    elif is_team_account:
        if facts.limit is not None:
            lines.append(
                MetricLine(
                    key="usage",
                    title="Total Usage",
                    line_type="progress",
                    kind="dollars",
                    used=_cents_to_dollars(plan_used_cents),
                    limit=_cents_to_dollars(facts.limit),
                    currency_code="USD",
                    resets_at=resets_at,
                    period_duration_ms=period_ms,
                    resource_key="totalUsage",
                    resource_kind="consumption",
                    resource_unit="usd",
                )
            )
        elif facts.total_percent_used is not None:
            lines.append(
                MetricLine(
                    key="usage",
                    title="Total Usage",
                    line_type="progress",
                    kind="percent",
                    used=facts.total_percent_used,
                    limit=100.0,
                    resets_at=resets_at,
                    period_duration_ms=period_ms,
                    resource_key="totalUsage",
                    resource_kind="consumption",
                    resource_unit="percent",
                )
            )
        else:
            raise ProviderError(tr("provider.cursor.requestUnavailable", "Cursor request-based usage data unavailable. Try again later."), kind="no_quota")
    else:
        lines.append(
            MetricLine(
                key="usage",
                title="Total Usage",
                line_type="progress",
                kind="percent",
                used=total_pct,
                limit=100.0,
                resets_at=resets_at,
                period_duration_ms=period_ms,
                resource_key="totalUsage",
                resource_kind="consumption",
                resource_unit="percent",
            )
        )

    if is_merged:
        # Unified additional usage
        if isinstance(unified_usage_status, dict):
            avail = unified_usage_status.get("availability")
            monthly = unified_usage_status.get("monthlyUsage")
            if (avail is None or avail == "available") and isinstance(monthly, dict) and monthly.get("additionalUsageOff") is not True:
                limit_cents = safe_float(monthly.get("limitCents"))
                if limit_cents != 0:
                    used = _cents_to_dollars(max(0.0, safe_float(monthly.get("usedCents"), 0.0) or 0.0))
                    if limit_cents is not None and limit_cents > 0:
                        u_reset = parse_iso8601(monthly.get("resetsAt"))
                        u_start = parse_iso8601(monthly.get("periodStart"))
                        u_pms = int((u_reset - u_start).total_seconds() * 1000) if (u_start and u_reset and u_reset > u_start) else MetricPeriod.MONTH_MS
                        lines.append(
                            MetricLine(
                                key="onDemand",
                                title="Extra Usage",
                                line_type="progress",
                                kind="dollars",
                                used=used,
                                limit=_cents_to_dollars(limit_cents),
                                currency_code="USD",
                                resets_at=format_iso8601(u_reset),
                                period_duration_ms=u_pms,
                                resource_key="onDemand",
                                resource_kind="consumption",
                                resource_unit="usd",
                            )
                        )
                    else:
                        lines.append(
                            MetricLine(
                                key="onDemand",
                                title="Extra Usage",
                                line_type="values",
                                kind="dollars",
                                currency_code="USD",
                                subtitle="spent",
                                values=[MetricValue(number=used, kind="dollars")],
                                resource_key="onDemand",
                                resource_kind="consumption",
                                resource_unit="usd",
                            )
                        )
        plan_lbl = resolved_plan_label(plan_name, membership_type)
        if history_days:
            lines.extend(build_cursor_history_lines(history_days, now or utc_now()))
        return plan_lbl, lines

    # Structured pools
    auto_pct = safe_float(plan_usage.get("autoPercentUsed"))
    if auto_pct is not None:
        lines.append(
            MetricLine(
                key="auto",
                title="Cursor Models",
                line_type="progress",
                kind="percent",
                used=auto_pct,
                limit=100.0,
                resets_at=resets_at,
                period_duration_ms=period_ms,
                resource_key="autoUsage",
                resource_kind="consumption",
                resource_unit="percent",
            )
        )

    api_pct = safe_float(plan_usage.get("apiPercentUsed"))
    if api_pct is not None:
        lines.append(
            MetricLine(
                key="api",
                title="Other Models",
                line_type="progress",
                kind="percent",
                used=api_pct,
                limit=100.0,
                resets_at=resets_at,
                period_duration_ms=period_ms,
                resource_key="apiUsage",
                resource_kind="consumption",
                resource_unit="percent",
            )
        )

    # Spend limit (On-demand / Extra Usage)
    spend_limit_usage = usage.get("spendLimitUsage") if isinstance(usage.get("spendLimitUsage"), dict) else None
    if spend_limit_usage is not None:
        ind_limit = safe_float(spend_limit_usage.get("individualLimit"))
        has_team_spend = facts.is_team_by_shape or (safe_float(spend_limit_usage.get("pooledUsed"), 0.0) or 0.0) > 0
        if ind_limit is not None:
            limit = ind_limit
            spent_keys = ["individualUsed"] if has_team_spend else ["individualUsed", "totalSpend"]
            rem_key = "individualRemaining"
        elif facts.pooled_limit > 0:
            limit = facts.pooled_limit
            spent_keys = ["pooledUsed"]
            rem_key = "pooledRemaining"
        else:
            limit = 0.0
            spent_keys = ["individualUsed", "pooledUsed", "totalSpend"]
            rem_key = None

        reported_vals = [v for k in spent_keys if (v := safe_float(spend_limit_usage.get(k))) is not None]
        rem_val = safe_float(spend_limit_usage.get(rem_key)) if rem_key else None
        spent = _on_demand_spend_cents(reported_vals, limit, rem_val)
        on_demand_title = "Team Spend" if (ind_limit is None and facts.is_team_by_shape and (limit > 0 or spent > 0)) else "Extra Usage"

        if limit > 0:
            lines.append(
                MetricLine(
                    key="onDemand",
                    title=on_demand_title,
                    line_type="progress",
                    kind="dollars",
                    used=_cents_to_dollars(spent),
                    limit=_cents_to_dollars(limit),
                    currency_code="USD",
                    resets_at=resets_at,
                    period_duration_ms=period_ms,
                    resource_key="onDemand",
                    resource_kind="consumption",
                    resource_unit="usd",
                )
            )
        elif spent > 0:
            lines.append(
                MetricLine(
                    key="onDemand",
                    title=on_demand_title,
                    line_type="values",
                    kind="dollars",
                    currency_code="USD",
                    subtitle="spent",
                    values=[MetricValue(number=_cents_to_dollars(spent), kind="dollars")],
                    resource_key="onDemand",
                    resource_kind="consumption",
                    resource_unit="usd",
                )
            )

    # Optional summary fallback for missing On-demand on team/org seats
    if summary_for_on_demand is not None and not any(l.key == "onDemand" for l in lines):
        s_resets, s_pms = _summary_billing_cycle(summary_for_on_demand, None)
        _append_summary_on_demand(summary_for_on_demand, s_resets, s_pms, lines)

    # Grok Bot (Sand)
    if grok_bot is not None:
        gb_line = map_grok_bot_usage(grok_bot)
        if gb_line is not None:
            lines.append(gb_line)

    if history_days:
        lines.extend(build_cursor_history_lines(history_days, now or utc_now()))

    plan_lbl = resolved_plan_label(plan_name, membership_type)
    return plan_lbl, lines


# ---------------------------------------------------------------------------
# Cursor 30-day usage events history
# ---------------------------------------------------------------------------

def parse_cursor_usage_events_page(body: Any) -> Optional[Tuple[int, List[Dict[str, Any]]]]:
    """Parse one page of `/api/dashboard/get-filtered-usage-events`."""
    if not isinstance(body, dict):
        return None
    total = safe_float(body.get("totalUsageEventsCount"))
    if total is None or not math.isfinite(total) or total < 0 or round(total) != total or total >= 1e15:
        return None
    raw_events = body.get("usageEventsDisplay")
    if raw_events is None:
        raw_events = []
    if not isinstance(raw_events, list):
        return None

    token_fields = (
        ("inputTokens", "inputTokens"),
        ("cacheReadTokens", "cacheReadTokens"),
        ("cacheWriteTokens", "cacheWriteTokens"),
        ("outputTokens", "outputTokens"),
    )
    events: List[Dict[str, Any]] = []
    for ev in raw_events:
        if not isinstance(ev, dict):
            return None
        ts_ms = safe_float(ev.get("timestamp"))
        if ts_ms is None or not math.isfinite(ts_ms) or ts_ms < 0:
            return None
        spend = 0.0
        if ev.get("kind") == CHARGEABLE_EVENT_KIND and ev.get("isChargeable") is not False and ev.get("chargedCents") is not None:
            cents = safe_float(ev.get("chargedCents"))
            if cents is None or not math.isfinite(cents) or cents < 0:
                return None
            spend = cents / 100.0

        usage = ev.get("tokenUsage")
        if usage is None:
            usage = {}
        if not isinstance(usage, dict):
            return None
        totals: Dict[str, float] = {"spend": spend, "usageEvents": 1.0}
        tok_sum = 0.0
        for src_k, dst_k in token_fields:
            if src_k in usage and usage[src_k] is not None:
                c = safe_float(usage[src_k])
                if c is None or not math.isfinite(c) or c < 0:
                    return None
                totals[dst_k] = c
                tok_sum += c
            else:
                totals[dst_k] = 0.0
        totals["tokens"] = tok_sum
        events.append({
            "timestamp_ms": ts_ms,
            "model": ev.get("model") if isinstance(ev.get("model"), str) else None,
            "totals": totals,
        })

    return int(total), events


def summarize_cursor_events_day(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    measures = ("spend", "usageEvents", "tokens", "inputTokens", "cacheReadTokens", "cacheWriteTokens", "outputTokens")
    totals = {m: 0.0 for m in measures}
    by_model: Dict[str, Dict[str, float]] = {}
    for ev in events:
        et = ev["totals"]
        for m in measures:
            totals[m] += et.get(m, 0.0)
        model = ev.get("model")
        if model:
            mt = by_model.setdefault(model, {m: 0.0 for m in measures})
            for m in measures:
                mt[m] += et.get(m, 0.0)
    return {"totals": totals, "by_model": by_model}


def build_cursor_history_lines(days_by_date: Dict[str, Dict[str, Any]], now: datetime) -> List[MetricLine]:
    """Build Today / Yesterday / Last 30 Days spend + tokens MetricLines with UsageBreakdown."""
    local_today = now.astimezone().date()
    windows = [
        ("today", "Today", [0]),
        ("yesterday", "Yesterday", [1]),
        ("last30", "Last 30 Days", list(range(30))),
    ]
    lines: List[MetricLine] = []
    for key, title, offsets in windows:
        date_keys = [(local_today - timedelta(days=o)).strftime("%Y-%m-%d") for o in offsets]
        matched = [days_by_date[dk] for dk in date_keys if dk in days_by_date]
        if not matched:
            continue

        totals: Dict[str, float] = {}
        models_agg: Dict[str, Dict[str, float]] = {}
        for day in matched:
            for mk, mv in (day.get("totals") or {}).items():
                totals[mk] = totals.get(mk, 0.0) + mv
            by_model = day.get("by_model")
            if isinstance(by_model, dict):
                for mname, mvals in by_model.items():
                    entry = models_agg.setdefault(mname, {})
                    for mk, mv in mvals.items():
                        entry[mk] = entry.get(mk, 0.0) + mv

        vals: List[MetricValue] = []
        if "spend" in totals:
            vals.append(MetricValue(number=totals["spend"], kind="dollars"))
        if "tokens" in totals:
            vals.append(MetricValue(number=totals["tokens"], kind="count", label="tokens"))
        if not vals:
            continue

        token_parts: Dict[str, float] = {}
        for src, label in (
            ("inputTokens", "Input"),
            ("cacheReadTokens", "Cache Read"),
            ("cacheWriteTokens", "Cache Write"),
            ("outputTokens", "Output"),
        ):
            if totals.get(src, 0.0) > 0:
                token_parts[label] = totals[src]

        has_cost = any((m.get("spend") or 0.0) > 0 for m in models_agg.values())
        has_tokens = any((m.get("tokens") or 0.0) > 0 for m in models_agg.values())
        breakdown_models = [
            UsageBreakdownModel(
                name=mname,
                spend=mvals.get("spend") if has_cost else None,
                tokens=mvals.get("tokens") if has_tokens else None,
            )
            for mname, mvals in models_agg.items()
        ]
        breakdown = UsageBreakdown(
            title=title,
            spend=totals.get("spend"),
            currency_code="USD",
            tokens=totals.get("tokens"),
            token_parts=token_parts,
            models=breakdown_models,
        )
        lines.append(
            MetricLine(
                key=key,
                title=title,
                line_type="values",
                kind="dollars",
                values=vals,
                breakdown=breakdown,
                resource_key=None,
            )
        )
    return lines


# Secondary reads owned by this provider (see backend/secondary.py)
CURSOR_PLAN_KEY = "cursor_plan"              # value: {"planName", "isMerged"}
CURSOR_MEMBERSHIP_KEY = "cursor_membership"  # value: normalized membershipType
CURSOR_HISTORY_KEY = "cursor_history"        # ledger: {"days", "partialDay", "holdUntil"}


def _connect_post(ctx: FetchContext, url: str, access_token: str) -> HTTPResponse:
    return ctx.http.post(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
        },
        body=b"{}",
        timeout=10.0,
    )


def _optional_connect_json(ctx: FetchContext, url: str, access_token: str) -> Optional[Dict[str, Any]]:
    try:
        resp = _connect_post(ctx, url, access_token)
        if not (200 <= resp.status < 300):
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _optional_session_json(ctx: FetchContext, url: str, access_token: str) -> Optional[Dict[str, Any]]:
    sess = cursor_session(access_token)
    if sess is None:
        return None
    _, sess_tok = sess
    try:
        resp = ctx.http.get(
            url,
            headers={"Cookie": f"WorkosCursorSessionToken={sess_tok}"},
            timeout=10.0,
        )
        if not (200 <= resp.status < 300):
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _refresh_cursor_history(ctx: FetchContext, access_token: str) -> Optional[Dict[str, Dict[str, Any]]]:
    sess = cursor_session(access_token)
    if sess is None:
        return None
    _, sess_tok = sess

    local_now = ctx.now.astimezone()
    local_tz = local_now.tzinfo or timezone.utc
    local_today = local_now.date()
    span_dates = [(local_today - timedelta(days=o)).strftime("%Y-%m-%d") for o in range(30)]
    span_set = set(span_dates)

    ledger = ctx.secondary_cache.setdefault(CURSOR_HISTORY_KEY, {"days": {}, "partialDay": None, "holdUntil": None})
    cached_days: Dict[str, Dict[str, Any]] = dict(ledger.get("days") or {})
    partial_day = ledger.get("partialDay")
    hold_until = parse_iso8601(ledger.get("holdUntil"))

    # Due days: today and yesterday on every refresh, plus older days not yet read or previously partial
    due_dates = span_dates[:2] + [d for d in span_dates[2:] if d not in cached_days or d == partial_day]

    if hold_until is not None and ctx.now < hold_until:
        kept = {k: v for k, v in cached_days.items() if k in span_set and k not in set(due_dates)}
        ledger["days"] = kept
        ledger["partialDay"] = span_dates[0]
        return kept

    deadline = time.monotonic() + 20.0
    rate_limited_for: Optional[float] = None
    read_days: Dict[str, Dict[str, Any]] = {}

    def _read_one_day(date_str: str) -> Tuple[str, Optional[Dict[str, Any]], Optional[float]]:
        if time.monotonic() >= deadline:
            return date_str, None, None
        dt_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        day_start = datetime(dt_date.year, dt_date.month, dt_date.day, tzinfo=local_tz)
        next_day_start = day_start + timedelta(days=1)
        end_dt = min(next_day_start, local_now)
        start_ms = int(day_start.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000) - 1
        if end_ms < start_ms:
            end_ms = start_ms

        all_events: List[Dict[str, Any]] = []
        expected_total: Optional[int] = None
        for page in range(1, 50):
            if time.monotonic() >= deadline:
                return date_str, None, None
            try:
                resp = ctx.http.post(
                    CURSOR_USAGE_EVENTS_URL,
                    headers={
                        "Cookie": f"WorkosCursorSessionToken={sess_tok}",
                        "Content-Type": "application/json",
                        "Origin": "https://cursor.com",
                    },
                    json_body={
                        "startDate": str(start_ms),
                        "endDate": str(end_ms),
                        "page": page,
                        "pageSize": 1000,
                    },
                    timeout=15.0,
                )
            except HTTPTransportError:
                return date_str, None, None
            if resp.status == 429:
                return date_str, None, parse_retry_after(resp) or 3600.0
            if not (200 <= resp.status < 300):
                return date_str, None, None
            try:
                parsed = parse_cursor_usage_events_page(resp.json())
            except Exception:
                return date_str, None, None
            if parsed is None:
                return date_str, None, None
            total_count, page_events = parsed
            for ev in page_events:
                ev_day = datetime.fromtimestamp(ev["timestamp_ms"] / 1000.0, tz=local_tz).strftime("%Y-%m-%d")
                if ev_day != date_str:
                    return date_str, None, None
            if expected_total is not None and expected_total != total_count:
                return date_str, None, None
            expected_total = total_count
            all_events.extend(page_events)
            if len(all_events) == total_count:
                return date_str, summarize_cursor_events_day(all_events), None
            if len(all_events) > total_count or not page_events:
                return date_str, None, None
        return date_str, None, None

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_read_one_day, d): d for d in due_dates}
        for fut in as_completed(futures):
            if time.monotonic() >= deadline:
                break
            try:
                d_str, day_obj, rl_sec = fut.result()
            except Exception:
                continue
            if rl_sec is not None:
                rate_limited_for = rl_sec
                break
            if day_obj is not None:
                read_days[d_str] = day_obj

    due_set = set(due_dates)
    merged = {k: v for k, v in cached_days.items() if k in span_set and k not in due_set}
    merged.update(read_days)
    ledger["days"] = merged
    ledger["partialDay"] = span_dates[0]
    if rate_limited_for is not None:
        ledger["holdUntil"] = format_iso8601(ctx.now + timedelta(seconds=rate_limited_for))
    return merged


def fetch(ctx: FetchContext) -> ProviderSnapshot:
    """Fetch live Cursor usage, plan/membership, optional Grok Bot, and 30-day events history."""
    cred, resp = authorized(
        ctx, OAUTH, lambda token: _connect_post(ctx, CURSOR_USAGE_URL, token), retry_when=is_auth_failure
    )
    access_token = str(cred.get("accessToken") or "").strip()
    raise_for_auth_failure(resp, "Cursor")
    raise_for_http_error(resp, "Cursor")
    usage = read_json(resp, "Cursor")

    summary_loaded = False
    summary_cache_local: Optional[Dict[str, Any]] = None

    def _read_summary() -> Optional[Dict[str, Any]]:
        nonlocal summary_loaded, summary_cache_local
        if not summary_loaded:
            summary_loaded = True
            summary_cache_local = _optional_session_json(ctx, CURSOR_USAGE_SUMMARY_URL, access_token)
        return summary_cache_local

    cache = ctx.secondary_cache

    # Hourly plan & isMerged read; each field keeps its previous value when missing
    def _read_plan() -> Dict[str, Any]:
        prev = cache.value(CURSOR_PLAN_KEY)
        prev = prev if isinstance(prev, dict) else {}
        plan_json = _optional_connect_json(ctx, CURSOR_PLAN_URL, access_token)
        plan_info = plan_json.get("planInfo") if isinstance(plan_json, dict) and isinstance(plan_json.get("planInfo"), dict) else {}
        raw_pname = plan_info.get("planName")
        fresh_plan = raw_pname.strip() if isinstance(raw_pname, str) and raw_pname.strip() else None
        is_merged_val = (plan_json.get("isMerged") is True) if isinstance(plan_json, dict) else None
        return {
            "planName": fresh_plan if fresh_plan is not None else prev.get("planName"),
            "isMerged": is_merged_val if is_merged_val is not None else bool(prev.get("isMerged", False)),
        }

    # Hourly membershipType read
    def _read_membership() -> Optional[str]:
        s_json = _read_summary()
        return _normalized_membership(s_json.get("membershipType")) if isinstance(s_json, dict) else None

    plan_value = cache.read(CURSOR_PLAN_KEY, ctx.now, _read_plan, force=ctx.force) or {}
    membership = cache.read(CURSOR_MEMBERSHIP_KEY, ctx.now, _read_membership, force=ctx.force)

    plan_name = plan_value.get("planName")
    is_merged = bool(plan_value.get("isMerged"))
    is_org_seat = membership in ORG_MEMBERSHIP_TYPES
    display_plan = resolved_plan_name(plan_name, membership)

    facts = CursorPlanUsageFacts(usage)
    fb_needed, fb_msg = should_use_request_based_fallback(facts, membership if is_org_seat else plan_name)

    def _read_request_usage() -> Optional[Dict[str, Any]]:
        sess = cursor_session(access_token)
        if sess is None:
            return None
        uid, _ = sess
        url = f"{CURSOR_REST_USAGE_URL}?{urllib.parse.urlencode({'user': uid})}"
        return _optional_session_json(ctx, url, access_token)

    grok_bot_json: Optional[Dict[str, Any]] = None
    if not is_merged:
        grok_bot_json = _optional_connect_json(ctx, CURSOR_GROK_BOT_URL, access_token)

    history_days = _refresh_cursor_history(ctx, access_token)

    if fb_needed:
        plan_lbl, lines = map_cursor_summary(
            summary=_read_summary(),
            request_usage=_read_request_usage(),
            plan_name=display_plan,
            unavailable_message=fb_msg,
        )
        if grok_bot_json is not None:
            gb = map_grok_bot_usage(grok_bot_json)
            if gb is not None:
                lines.append(gb)
        if history_days:
            lines.extend(build_cursor_history_lines(history_days, ctx.now))
        return make_snapshot(ctx, plan=plan_lbl, lines=lines)

    if facts.should_try_generic_request_fallback:
        req_usage = _read_request_usage()
        if req_usage is not None:
            try:
                plan_lbl, lines = map_cursor_summary(
                    summary=None,
                    request_usage=req_usage,
                    plan_name=display_plan,
                    unavailable_message="",
                )
                if grok_bot_json is not None:
                    gb = map_grok_bot_usage(grok_bot_json)
                    if gb is not None:
                        lines.append(gb)
                if history_days:
                    lines.extend(build_cursor_history_lines(history_days, ctx.now))
                return make_snapshot(ctx, plan=plan_lbl, lines=lines)
            except ProviderError:
                pass

    if is_merged:
        complat_bal = _optional_connect_json(ctx, CURSOR_COMPLAT_CREDITS_URL, access_token)
        usage_status = _optional_session_json(ctx, CURSOR_USAGE_STATUS_URL, access_token)
        plan_lbl, lines = map_cursor_usage(
            usage,
            plan_name=plan_name,
            membership_type=membership,
            unified_credit_balance=complat_bal,
            unified_usage_status=usage_status,
            is_merged=True,
            history_days=history_days,
            now=ctx.now,
        )
        return make_snapshot(ctx, plan=plan_lbl, lines=lines)

    stripe_json = _optional_session_json(ctx, CURSOR_STRIPE_URL, access_token)
    credits_json = _optional_connect_json(ctx, CURSOR_CREDITS_URL, access_token)

    plan_lbl, lines = map_cursor_usage(
        usage,
        plan_name=plan_name,
        membership_type=membership,
        credit_grants=credits_json,
        stripe_balance_cents_val=stripe_balance_cents(stripe_json),
        is_merged=False,
        grok_bot=None,
        history_days=None,
        now=ctx.now,
    )

    needs_on_demand = not any(l.key == "onDemand" for l in lines)
    if (is_org_seat and needs_on_demand) or (not is_org_seat and facts.is_team_by_shape):
        s_json = _read_summary()
        if s_json is not None and has_usable_summary_payload(s_json):
            if needs_on_demand:
                s_resets, s_pms = _summary_billing_cycle(s_json, None)
                _append_summary_on_demand(s_json, s_resets, s_pms, lines)
            s_mem = _normalized_membership(s_json.get("membershipType")) or membership
            plan_lbl = resolved_plan_label(plan_name, s_mem) or plan_lbl

    if grok_bot_json is not None:
        gb = map_grok_bot_usage(grok_bot_json)
        if gb is not None:
            lines.append(gb)

    if history_days:
        lines.extend(build_cursor_history_lines(history_days, ctx.now))

    return make_snapshot(ctx, plan=plan_lbl, lines=lines)
