"""OpenCode Go provider client, OAuth refresh, and usage mapper (ported from OpenCode*.swift)."""

from __future__ import annotations

import email.utils
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from backend.http_client import HTTPResponse
from backend.i18n import tr
from backend.models import (
    MetricLine,
    MetricPeriod,
    ProviderSnapshot,
    format_iso8601,
    parse_iso8601,
    utc_now,
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
from backend.providers.session import OAuthRefresh, authorized


CLIENT_ID = "opencode-cli"
TOKEN_URL = "https://opencode.ai/console/auth/device/token"
USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
GO_STATUS_URL = "https://opencode.ai/console/api/go/status"
ORG_KEY = "org_id"
PLACEHOLDER_RESET_TOLERANCE_SEC = 2.0

_PLAN_NAMES: Dict[str, str] = {
    "go": "Go",
    "go-plus": "Go Plus",
    "go-teams": "Go Teams",
}


def _parse_http_date(resp: HTTPResponse) -> Optional[datetime]:
    raw = resp.header("date")
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def map_opencode_error_type(payload: Any) -> Optional[str]:
    """Extract `error.type` from an OpenCode error JSON body."""
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    err_type = error.get("type")
    if not isinstance(err_type, str):
        return None
    trimmed = err_type.strip()
    return trimmed or None


def map_opencode_plan(payload: Any) -> str:
    """Map `/console/api/go/status` `product` field to human-readable plan name."""
    if not isinstance(payload, dict):
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "OpenCode"), kind="refused")
    product = payload.get("product")
    if isinstance(product, str) and product in _PLAN_NAMES:
        return _PLAN_NAMES[product]
    raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "OpenCode"), kind="refused")


def _map_window(
    raw: Any,
    *,
    key: str,
    title: str,
    period_ms: int,
    captured_at: datetime,
    drops_placeholder_reset: bool = False,
    session_start_signal: Optional[str] = None,
) -> MetricLine:
    if not isinstance(raw, dict):
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "OpenCode"), kind="refused")
    percent = _parse_number(raw.get("percent"))
    reset_str = raw.get("resetsAt")
    reported_reset = parse_iso8601(reset_str) if isinstance(reset_str, str) else None
    if percent is None or reported_reset is None:
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "OpenCode"), kind="refused")

    used = _clamp_percent(percent)
    resets_at: Optional[datetime] = reported_reset
    if drops_placeholder_reset and used == 0.0:
        period_sec = period_ms / 1000.0
        distance = abs((reported_reset - captured_at).total_seconds() - period_sec)
        if distance <= PLACEHOLDER_RESET_TOLERANCE_SEC:
            resets_at = None

    return MetricLine(
        key=key,
        title=title,
        line_type="progress",
        kind="percent",
        used=used,
        limit=100.0,
        resets_at=format_iso8601(resets_at),
        period_duration_ms=period_ms,
        session_start_signal=session_start_signal,
        resource_key=key,
        resource_kind="consumption",
        resource_unit="percent",
    )


def map_opencode_usage(
    payload: Any,
    *,
    captured_at: Optional[datetime] = None,
    go_status_payload: Any = None,
) -> Tuple[Optional[str], List[MetricLine]]:
    """Map OpenCode `/zen/go/v1/usage` (and optional `/console/api/go/status`) into (plan, lines)."""
    if not isinstance(payload, dict):
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "OpenCode"), kind="refused")
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "OpenCode"), kind="refused")

    at = captured_at or utc_now()
    lines = [
        _map_window(
            usage.get("rolling"),
            key="session",
            title="Session",
            period_ms=MetricPeriod.SESSION_MS,
            captured_at=at,
            drops_placeholder_reset=True,
            session_start_signal="missing_reset_date",
        ),
        _map_window(
            usage.get("weekly"),
            key="weekly",
            title="Weekly",
            period_ms=MetricPeriod.WEEK_MS,
            captured_at=at,
        ),
        _map_window(
            usage.get("monthly"),
            key="monthly",
            title="Monthly",
            period_ms=MetricPeriod.MONTH_MS,
            captured_at=at,
        ),
    ]

    plan: Optional[str] = None
    if go_status_payload is not None:
        plan = map_opencode_plan(go_status_payload)

    return plan, lines


def _extract_org_id(credential: Dict[str, Any]) -> Optional[str]:
    extras = credential.get("extras")
    raw = extras.get(ORG_KEY) if isinstance(extras, dict) else None
    if not raw:
        raw = credential.get(ORG_KEY)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _is_legacy_api_key(ctx: FetchContext) -> bool:
    cred = ctx.credential
    has_api_key = bool(str(cred.get("apiKey") or "").strip())
    has_access_token = bool(str(cred.get("accessToken") or "").strip())
    if has_api_key and not has_access_token:
        return True
    if ctx.account.effective_auth_method == "api_key" and not has_access_token:
        return True
    return False


OAUTH = OAuthRefresh(
    provider_name="OpenCode",
    token_url=TOKEN_URL,
    client_id=CLIENT_ID,
    form=True,
    headers={"Accept": "application/json"},
    expired_message=lambda: tr("error.signInExpired", "Sign-in expired for %1. Sign in again.", "OpenCode"),
    invalid_message=lambda: tr("error.invalidResponse", "%1 returned an invalid response.", "OpenCode"),
    drop_unknown_expiry=True,
)


OPENCODE_PLAN_KEY = "opencode_plan"  # secondary read; value: plan name


def _read_plan_cached(
    ctx: FetchContext,
    access_token: str,
    org_id: Optional[str],
) -> Optional[str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    if org_id:
        headers["x-org-id"] = org_id

    def _fetch() -> Optional[str]:
        resp = ctx.http.get(GO_STATUS_URL, headers=headers, timeout=15.0)
        return map_opencode_plan(resp.json()) if 200 <= resp.status < 300 else None

    # Matches Swift `planRead = SecondaryRead(); planRead.record(plan, at: now())`:
    # replaces the cached plan on each hourly check (clears stale plan if lookup failed).
    value = ctx.secondary_cache.read(OPENCODE_PLAN_KEY, ctx.now, _fetch, force=ctx.force, on_failure="clear")
    return value if isinstance(value, str) else None


def _fetch_usage_http(
    ctx: FetchContext,
    token: str,
    org_id: Optional[str],
) -> HTTPResponse:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if org_id:
        headers["x-opencode-org-id"] = org_id
    return ctx.http.get(USAGE_URL, headers=headers, timeout=15.0)


def fetch(ctx: FetchContext) -> ProviderSnapshot:
    """Fetch OpenCode Go usage using either a Console OAuth token or a legacy API key."""
    is_api_key = _is_legacy_api_key(ctx)
    sign_in_token: Optional[str] = None
    org_id: Optional[str] = None

    if is_api_key:
        api_key = str(ctx.credential.get("apiKey") or ctx.credential.get("accessToken") or "").strip()
        if not api_key:
            raise ProviderError(tr("error.apiKeyRejected", "%1 rejected the API key.", "OpenCode"), kind="api_key_rejected")
        resp = send_mapped(lambda: _fetch_usage_http(ctx, api_key, None))
    else:
        def _get_usage(token: str) -> HTTPResponse:
            if not token and not ctx.account.is_hub_account:
                raise ProviderError(tr("error.signInExpired", "Sign-in expired for %1. Sign in again.", "OpenCode"), kind="auth_expired")
            return _fetch_usage_http(ctx, token, _extract_org_id(ctx.credential))

        cred, resp = authorized(ctx, OAUTH, _get_usage)
        access_token = str(cred.get("accessToken") or "").strip()
        org_id = _extract_org_id(cred)
        sign_in_token = access_token

    if resp.status == 403:
        try:
            err_payload = resp.json()
        except Exception:
            err_payload = None
        if map_opencode_error_type(err_payload) == "EntitlementError":
            raise ProviderError(
                tr("provider.opencode.noGoSubscription", "No OpenCode Go subscription on this account."),
                kind="no_quota",
                status_code=403,
            )

    raise_for_http_error(resp, "OpenCode", is_api_key=is_api_key)

    payload = read_json(resp, "OpenCode", expect=None, invalid_message=tr("error.invalidResponse", "%1 returned an invalid response.", "OpenCode"))
    captured_at = _parse_http_date(resp) or ctx.now
    _, lines = map_opencode_usage(payload, captured_at=captured_at)

    plan: Optional[str] = None
    if sign_in_token is not None:
        plan = _read_plan_cached(ctx, sign_in_token, org_id)

    return make_snapshot(ctx, plan=plan, lines=lines)
