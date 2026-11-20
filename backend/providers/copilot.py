"""GitHub Copilot personal and organization billing provider runtime and pure mappers."""

from __future__ import annotations

import math
import re
import urllib.parse
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
    parse_iso8601,
)
from backend.providers.base import (
    FetchContext,
    ProviderError,
    make_snapshot,
    parse_retry_after,
    raise_for_http_error,
    safe_float,
    read_json,
    send_mapped,
    is_auth_failure,
)

COPILOT_PERIOD_MS = MetricPeriod.MONTH_MS
_GHE_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.ghe\.com$")


def parse_github_domain(raw: Optional[str]) -> Optional[str]:
    """Normalize a GitHub Enterprise Cloud (GHE.com) host; returns None for github.com."""
    if not raw or not isinstance(raw, str):
        return None
    host = raw.strip().lower()
    if "://" in host:
        host = host.split("://", 1)[1]
    for sep in ("/", "?", "#"):
        if sep in host:
            host = host.split(sep, 1)[0]
    if not host or host in ("github.com", "www.github.com"):
        return None
    if "." not in host:
        host = f"{host}.ghe.com"
    if not _GHE_HOST_RE.match(host):
        return None
    return host


def github_api_url(path: str, domain: Optional[str] = None) -> str:
    host = parse_github_domain(domain) or "github.com"
    if not path.startswith("/"):
        path = "/" + path
    return f"https://api.{host}{path}"


def organization_usage_url(organization: str) -> str:
    quoted = urllib.parse.quote(organization.strip(), safe="")
    return f"https://api.github.com/organizations/{quoted}/settings/billing/ai_credit/usage"


def _is_github_rate_limit(resp: HTTPResponse) -> bool:
    if resp.status == 429:
        return True
    if resp.status == 403 and (resp.header("x-ratelimit-remaining") == "0" or resp.header("retry-after") is not None):
        return True
    return False


def _parse_bool(val: Any) -> Optional[bool]:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        low = val.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
    if isinstance(val, (int, float)):
        return bool(val)
    return None


def _clamp_percent(val: float) -> float:
    return max(0.0, min(100.0, val))


def _plan_label(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    parts = [p for p in re.split(r"[_ -]+", raw) if p]
    if not parts:
        return None
    return " ".join(p[0].upper() + p[1:].lower() for p in parts)


def _parse_reset_date(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    dt = parse_iso8601(raw)
    if dt is not None:
        return format_iso8601(dt)
    try:
        day_dt = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return format_iso8601(day_dt)
    except ValueError:
        return None


def _snapshot_line(
    *,
    key: str,
    title: str,
    resource_key: str,
    resource_unit: str,
    raw: Any,
    resets_at: Optional[str],
) -> Optional[MetricLine]:
    if not isinstance(raw, dict):
        return None
    entitlement = safe_float(raw.get("entitlement"))
    remaining = safe_float(raw.get("remaining"))
    if _parse_bool(raw.get("unlimited")) is True or entitlement == -1 or remaining == -1:
        return None
    if entitlement == 0:
        return None

    pct_rem = safe_float(raw.get("percent_remaining"))
    if pct_rem is not None and math.isfinite(pct_rem):
        used_pct = _clamp_percent(100.0 - pct_rem)
    elif entitlement is not None and entitlement > 0 and remaining is not None and math.isfinite(remaining):
        used_pct = _clamp_percent(100.0 - (remaining / entitlement) * 100.0)
    else:
        return None

    return MetricLine(
        key=key,
        title=title,
        line_type="progress",
        kind="percent",
        used=used_pct,
        limit=100.0,
        resets_at=resets_at,
        period_duration_ms=COPILOT_PERIOD_MS,
        resource_key=resource_key,
        resource_kind="consumption",
        resource_unit=resource_unit,
    )


def _overage_line(raw: Any) -> Optional[MetricLine]:
    if not isinstance(raw, dict):
        return None
    if _parse_bool(raw.get("overage_permitted")) is not True:
        return None
    overage = max(0.0, safe_float(raw.get("overage_count"), 0.0) or 0.0)
    return MetricLine(
        key="extra",
        title="Extra Usage",
        line_type="values",
        kind="count",
        values=[MetricValue(number=overage, kind="count")],
        resource_key="extraUsage",
        resource_kind="consumption",
        resource_unit="count",
    )


def _limited_line(
    *,
    key: str,
    title: str,
    resource_key: str,
    remaining_raw: Any,
    total_raw: Any,
    resets_at: Optional[str],
) -> Optional[MetricLine]:
    total = safe_float(total_raw)
    remaining = safe_float(remaining_raw)
    if total is None or remaining is None or total <= 0 or not math.isfinite(total) or not math.isfinite(remaining):
        return None
    used = max(0.0, total - remaining)
    return MetricLine(
        key=key,
        title=title,
        line_type="progress",
        kind="percent",
        used=_clamp_percent((used / total) * 100.0),
        limit=100.0,
        resets_at=resets_at,
        period_duration_ms=COPILOT_PERIOD_MS,
        resource_key=resource_key,
        resource_kind="consumption",
        resource_unit="percent",
    )


def map_copilot_usage(body: Dict[str, Any]) -> Tuple[Optional[str], List[MetricLine]]:
    """Pure mapper from GitHub `/copilot_internal/user` JSON to (plan, MetricLines)."""
    plan = _plan_label(body.get("copilot_plan"))
    resets_at = _parse_reset_date(body.get("quota_reset_date")) or _parse_reset_date(body.get("limited_user_reset_date"))

    lines: List[MetricLine] = []
    snapshots = body.get("quota_snapshots") if isinstance(body.get("quota_snapshots"), dict) else {}
    premium = snapshots.get("premium_interactions")

    credits_line = _snapshot_line(
        key="premium",
        title="Credits",
        resource_key="premiumCredits",
        resource_unit="credits",
        raw=premium,
        resets_at=resets_at,
    )
    if credits_line is not None:
        lines.append(credits_line)
        ov = _overage_line(premium)
        if ov is not None:
            lines.append(ov)

    chat_line = _snapshot_line(
        key="chat",
        title="Chat",
        resource_key="chat",
        resource_unit="percent",
        raw=snapshots.get("chat"),
        resets_at=resets_at,
    )
    if chat_line is not None:
        lines.append(chat_line)

    comp_line = _snapshot_line(
        key="completions",
        title="Completions",
        resource_key="completions",
        resource_unit="percent",
        raw=snapshots.get("completions"),
        resets_at=resets_at,
    )
    if comp_line is not None:
        lines.append(comp_line)

    if not lines:
        limited = body.get("limited_user_quotas") if isinstance(body.get("limited_user_quotas"), dict) else {}
        monthly = body.get("monthly_quotas") if isinstance(body.get("monthly_quotas"), dict) else {}
        l_chat = _limited_line(
            key="chat",
            title="Chat",
            resource_key="chat",
            remaining_raw=limited.get("chat"),
            total_raw=monthly.get("chat"),
            resets_at=resets_at,
        )
        if l_chat is not None:
            lines.append(l_chat)
        l_comp = _limited_line(
            key="completions",
            title="Completions",
            resource_key="completions",
            remaining_raw=limited.get("completions"),
            total_raw=monthly.get("completions"),
            resets_at=resets_at,
        )
        if l_comp is not None:
            lines.append(l_comp)

    if not lines:
        if _parse_bool(body.get("token_based_billing")) is True:
            personal_lines: List[MetricLine] = []
            if isinstance(premium, dict):
                credits_used = safe_float(premium.get("credits_used"))
                if credits_used is not None and credits_used > 0 and math.isfinite(credits_used):
                    personal_lines.append(
                        MetricLine(
                            key="premium",
                            title="Credits",
                            line_type="values",
                            kind="count",
                            values=[MetricValue(number=credits_used, kind="count")],
                            resource_key="premiumCredits",
                            resource_kind="consumption",
                            resource_unit="credits",
                        )
                    )
            return plan, personal_lines
        raise ProviderError(tr("provider.copilot.noUsageData", "Copilot usage data is unavailable for this account."), kind="no_quota")

    return plan, lines


def map_copilot_org_billing(body: Dict[str, Any]) -> List[MetricLine]:
    """Pure mapper from GitHub org AI credit billing report JSON to MetricLines."""
    items = body.get("usageItems")
    if not isinstance(items, list):
        raise ProviderError(tr("provider.copilot.invalidBillingResponse", "GitHub returned an invalid organization billing response."), kind="refused")

    credits = 0.0
    spend = 0.0
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("product") != "Copilot":
            continue
        qty = safe_float(item.get("grossQuantity"))
        amt = safe_float(item.get("netAmount"))
        if qty is None or amt is None or not math.isfinite(qty) or not math.isfinite(amt):
            raise ProviderError(tr("provider.copilot.invalidBillingItem", "GitHub returned an invalid organization billing item."), kind="refused")
        credits += qty
        spend += amt

    return [
        MetricLine(
            key="orgCredits",
            title="Org Credits",
            line_type="values",
            kind="count",
            values=[MetricValue(number=credits, kind="count", label="credits")],
            resource_key="orgCredits",
            resource_kind="consumption",
            resource_unit="credits",
        ),
        MetricLine(
            key="orgSpend",
            title="Org Spend",
            line_type="values",
            kind="dollars",
            currency_code="USD",
            subtitle="spent",
            values=[MetricValue(number=spend, kind="dollars")],
            resource_key="orgSpend",
            resource_kind="consumption",
            resource_unit="usd",
        ),
    ]


def fetch(ctx: FetchContext) -> ProviderSnapshot:
    """Fetch GitHub Copilot personal quota or organization AI credit billing usage."""
    if ctx.account.organization:
        org = ctx.account.organization
        token = str(ctx.credential.get("apiKey") or ctx.credential.get("accessToken") or "")
        resp = send_mapped(lambda: ctx.http.get(
            organization_usage_url(org),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "Omameter",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=15.0,
        ))

        if _is_github_rate_limit(resp):
            raise ProviderError(
                tr("error.rateLimited", "%1 rate limited the request.", "GitHub"),
                kind="rate_limited",
                retry_after=parse_retry_after(resp),
                status_code=resp.status,
            )
        if is_auth_failure(resp):
            raise ProviderError(tr("provider.copilot.tokenRejected", "GitHub rejected the token."), kind="api_key_rejected", status_code=resp.status)
        if resp.status == 404:
            raise ProviderError(
                tr("provider.copilot.orgNotFound", 'GitHub has no organization "%1" this token can read.', org),
                kind="no_quota",
                status_code=404,
            )
        raise_for_http_error(resp, "GitHub", is_api_key=True)

        body = read_json(resp, "GitHub")
        lines = map_copilot_org_billing(body)
        return make_snapshot(ctx, plan=None, lines=lines)

    # Personal Copilot seat
    access_token = str(ctx.credential.get("accessToken") or "")
    url = github_api_url("/copilot_internal/user", domain=ctx.account.domain)
    resp = send_mapped(lambda: ctx.http.get(
        url,
        headers={
            "Authorization": f"token {access_token}",
            "Accept": "application/json",
            "Editor-Version": "vscode/1.96.2",
            "Editor-Plugin-Version": "copilot-chat/0.26.7",
            "User-Agent": "GitHubCopilotChat/0.26.7",
            "X-Github-Api-Version": "2025-04-01",
        },
        timeout=15.0,
    ))

    if _is_github_rate_limit(resp):
        raise ProviderError(
            tr("error.rateLimited", "%1 rate limited the request.", "Copilot"),
            kind="rate_limited",
            retry_after=parse_retry_after(resp),
            status_code=resp.status,
        )
    if is_auth_failure(resp):
        raise ProviderError(
            tr("error.signInExpired", "Sign-in expired for %1. Sign in again.", "Copilot"),
            kind="auth_expired",
            status_code=resp.status,
        )
    raise_for_http_error(resp, "Copilot")

    body = read_json(resp, "Copilot")

    plan, lines = map_copilot_usage(body)
    return make_snapshot(ctx, plan=plan, lines=lines)
