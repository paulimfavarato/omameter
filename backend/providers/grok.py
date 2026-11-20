"""Grok (xAI) provider client, OAuth refresh, and usage mapper (ported from Grok*.swift)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from backend.http_client import HTTPResponse
from backend.i18n import tr
from backend.models import (
    MetricLine,
    ProviderSnapshot,
    format_currency,
    format_iso8601,
    parse_iso8601,
)
from backend.providers.base import (
    parse_number as _parse_number,
    clamp_percent as _clamp_percent,
    FetchContext,
    ProviderError,
    make_snapshot,
    raise_for_http_error,
    read_json,
)
from backend.providers.session import OAuthRefresh, authorized


CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
TOKEN_URL = "https://auth.x.ai/oauth2/token"
CREDITS_CONFIG_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
SETTINGS_URL = "https://cli-chat-proxy.grok.com/v1/settings"
TOKEN_AUTH_HEADER = "xai-grok-cli"
WEEKLY_PERIOD_TYPE = "USAGE_PERIOD_TYPE_WEEKLY"

_INT64_MAX = float((1 << 63) - 1)


def _auth_headers(access_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token.strip()}",
        "X-XAI-Token-Auth": TOKEN_AUTH_HEADER,
        "Accept": "application/json",
        "User-Agent": "Omameter",
    }


def map_grok_credits(
    payload: Any,
    *,
    status_code: int = 200,
) -> Tuple[List[MetricLine], Optional[str]]:
    """Map Grok `/v1/billing?format=credits` response into (lines, notice)."""
    if status_code == 412:
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, str) and "no personal team" in err.lower():
                return [], "Grok team accounts have no personal quota."

    if not isinstance(payload, dict):
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Grok"), kind="refused")

    config = payload.get("config")
    if not isinstance(config, dict):
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Grok"), kind="refused")

    period = config.get("currentPeriod")
    if not isinstance(period, dict):
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Grok"), kind="refused")

    raw_type = period.get("type")
    period_type = raw_type.strip() if isinstance(raw_type, str) else ""
    start_dt = parse_iso8601(period.get("start")) if isinstance(period.get("start"), str) else None
    end_dt = parse_iso8601(period.get("end")) if isinstance(period.get("end"), str) else None

    if not period_type or start_dt is None or end_dt is None or end_dt <= start_dt:
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Grok"), kind="refused")

    # Proto-JSON omits zero values, so absent percent is 0%, whereas present non-numeric is invalid.
    if "creditUsagePercent" in config and config["creditUsagePercent"] is not None:
        used_percent = _parse_number(config["creditUsagePercent"])
        if used_percent is None:
            raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Grok"), kind="refused")
    else:
        used_percent = 0.0

    if "onDemandCap" in config and config["onDemandCap"] is not None:
        cap_obj = config["onDemandCap"]
        if not isinstance(cap_obj, dict):
            raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Grok"), kind="refused")
        raw_cap = cap_obj.get("val", 0)
        on_demand_cap = _parse_number(0 if raw_cap is None else raw_cap)
        if on_demand_cap is None or abs(on_demand_cap) >= _INT64_MAX:
            raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Grok"), kind="refused")
    else:
        on_demand_cap = 0.0

    period_ms = int(round((end_dt - start_dt).total_seconds() * 1000.0))

    lines: List[MetricLine] = []
    if period_type == WEEKLY_PERIOD_TYPE:
        lines.append(
            MetricLine(
                key="weekly",
                title="Weekly",
                line_type="progress",
                kind="percent",
                used=_clamp_percent(used_percent),
                limit=100.0,
                resets_at=format_iso8601(end_dt),
                period_duration_ms=period_ms,
                resource_key="weekly",
                resource_kind="consumption",
                resource_unit="percent",
            )
        )

    if on_demand_cap > 0:
        cap_dollars = round(on_demand_cap) / 100.0
        cap_text = format_currency(cap_dollars, "USD", style="row")
        lines.append(
            MetricLine(
                key="payAsYouGo",
                title="Extra Usage",
                line_type="badge",
                kind="dollars",
                badge_text=f"{cap_text} cap",
            )
        )

    return lines, None


def map_grok_plan(payload: Any) -> Optional[str]:
    """Extract subscription plan display name from `/v1/settings`."""
    if not isinstance(payload, dict):
        return None
    plan = payload.get("subscription_tier_display")
    if not isinstance(plan, str):
        return None
    trimmed = plan.strip()
    return trimmed or None


OAUTH = OAuthRefresh(
    provider_name="Grok",
    token_url=TOKEN_URL,
    client_id=CLIENT_ID,
    form=True,
    expired_message=lambda: tr("error.signInExpired", "Sign-in expired for %1. Sign in again.", "Grok"),
    invalid_message=lambda: tr("error.invalidResponse", "%1 returned an invalid response.", "Grok"),
    drop_unknown_expiry=True,
)


GROK_PLAN_KEY = "grok_plan"  # secondary read; value: plan name


def _read_plan_cached(ctx: FetchContext, access_token: str) -> Optional[str]:
    def _fetch() -> Optional[str]:
        resp = ctx.http.get(SETTINGS_URL, headers=_auth_headers(access_token), timeout=10.0)
        return map_grok_plan(resp.json()) if 200 <= resp.status < 300 else None

    value = ctx.secondary_cache.read(GROK_PLAN_KEY, ctx.now, _fetch, force=ctx.force)
    return value if isinstance(value, str) else None


def fetch(ctx: FetchContext) -> ProviderSnapshot:
    """Fetch Grok weekly quota, pay-as-you-go cap, and subscription tier."""
    def _get_credits(token: str) -> HTTPResponse:
        if not token and not ctx.account.is_hub_account:
            raise ProviderError(tr("error.signInExpired", "Sign-in expired for %1. Sign in again.", "Grok"), kind="auth_expired")
        return ctx.http.get(CREDITS_CONFIG_URL, headers=_auth_headers(token), timeout=10.0)

    cred, resp = authorized(ctx, OAUTH, _get_credits)
    access_token = str(cred.get("accessToken") or "").strip()

    # Check team-account 412 notice before generic HTTP error handling
    if resp.status == 412:
        try:
            err_body = resp.json()
        except Exception:
            err_body = None
        if isinstance(err_body, dict) and isinstance(err_body.get("error"), str):
            if "no personal team" in err_body["error"].lower():
                plan = _read_plan_cached(ctx, access_token)
                return make_snapshot(
                    ctx,
                    plan=plan,
                    lines=[],
                    notice="Grok team accounts have no personal quota.",
                )

    raise_for_http_error(resp, "Grok")

    payload = read_json(resp, "Grok", expect=None, invalid_message=tr("error.invalidResponse", "%1 returned an invalid response.", "Grok"))
    lines, notice = map_grok_credits(payload, status_code=resp.status)
    plan = _read_plan_cached(ctx, access_token)
    return make_snapshot(ctx, plan=plan, lines=lines, notice=notice)
