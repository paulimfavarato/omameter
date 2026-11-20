"""Z.ai provider client, quota mapper, and banked rate-limit resets (ported from ZAI*.swift)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from backend.i18n import tr
from backend.models import (
    MetricLine,
    MetricPeriod,
    MetricValue,
    ProviderSnapshot,
    ResetItem,
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
from backend.secondary import SecondaryCache


SUBSCRIPTION_URL = "https://api.z.ai/api/biz/subscription/list"
QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
RESET_LIST_URL = "https://api.z.ai/api/biz/customer-package-reset/list?targetType=PERSONAL"
USE_RESET_URL = "https://api.z.ai/api/biz/customer-package-reset/use"
ZAI_TZ = timezone(timedelta(hours=8))

_INT64_MAX = float((1 << 63) - 1)
_UNIT_MS: Dict[int, float] = {
    3: 60.0 * 60.0 * 1000.0,               # hour
    4: 24.0 * 60.0 * 60.0 * 1000.0,        # day
    6: 7.0 * 24.0 * 60.0 * 60.0 * 1000.0,  # week
    5: 30.0 * 24.0 * 60.0 * 60.0 * 1000.0, # month
}
_RESET_WINDOW_SPECS: Tuple[Tuple[str, str, str, str], ...] = (
    ("FIVE_HOUR", "fiveHourResets", "session", "Session Reset"),
    ("WEEK", "weekResets", "weekly", "Weekly Reset"),
)


def _epoch_ms_to_iso(raw_ms: Any) -> Optional[str]:
    ms = _parse_number(raw_ms)
    if ms is None:
        return None
    try:
        dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None
    return format_iso8601(dt)


def _classify_token_window(entry: Dict[str, Any]) -> Optional[Tuple[str, str, int]]:
    unit_num = _parse_number(entry.get("unit"))
    count = _parse_number(entry.get("number"))
    if unit_num is None or count is None or count <= 0:
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Z.ai"), kind="refused")

    unit_int = int(unit_num) if unit_num.is_integer() else None
    if unit_int not in _UNIT_MS:
        return None

    duration = _UNIT_MS[unit_int] * count
    if duration < 1.0 or abs(duration) >= _INT64_MAX:
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Z.ai"), kind="refused")

    period_ms = int(duration)
    if period_ms < MetricPeriod.DAY_MS:
        return ("session", "Session", period_ms)
    return ("weekly", "Weekly", period_ms)


def _percent_line(
    entry: Dict[str, Any],
    *,
    key: str,
    title: str,
    period_ms: int,
) -> MetricLine:
    raw_pct = _parse_number(entry.get("percentage"))
    if raw_pct is None:
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Z.ai"), kind="refused")
    return MetricLine(
        key=key,
        title=title,
        line_type="progress",
        kind="percent",
        used=_clamp_percent(raw_pct),
        limit=100.0,
        resets_at=_epoch_ms_to_iso(entry.get("nextResetTime")),
        period_duration_ms=period_ms,
        resource_key=key,
        resource_kind="consumption",
        resource_unit="percent",
    )


def _web_search_line(entry: Dict[str, Any]) -> MetricLine:
    used = _parse_number(entry.get("currentValue"))
    limit = _parse_number(entry.get("usage"))
    if used is None or limit is None or used < 0 or limit < 0:
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Z.ai"), kind="refused")
    return MetricLine(
        key="webSearches",
        title="Web Searches",
        line_type="progress",
        kind="count",
        used=used,
        limit=limit,
        unit_suffix="searches",
        resets_at=_epoch_ms_to_iso(entry.get("nextResetTime")),
        period_duration_ms=MetricPeriod.MONTH_MS,
        resource_key="webSearches",
        resource_kind="consumption",
        resource_unit="searches",
    )


def map_zai_quota(payload: Any) -> List[MetricLine]:
    """Map `/api/monitor/usage/quota/limit` JSON into Session, Weekly, and Web Searches lines."""
    if not isinstance(payload, dict):
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Z.ai"), kind="refused")

    if payload.get("success") is False:
        msg = payload.get("msg")
        if isinstance(msg, str) and "coding plan" in msg.lower():
            raise ProviderError(
                tr("provider.zai.noCodingPlan", "No active GLM Coding Plan. Subscribe at z.ai/subscribe to see usage."),
                kind="no_quota",
            )
        if payload.get("code") == 401:
            raise ProviderError(tr("error.apiKeyRejected", "%1 rejected the API key.", "Z.ai"), kind="api_key_rejected", status_code=401)

    if "data" in payload:
        container = payload["data"]
        if not isinstance(container, dict):
            raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Z.ai"), kind="refused")
    else:
        container = payload

    limits = container.get("limits")
    if not isinstance(limits, list) or not all(isinstance(x, dict) for x in limits):
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Z.ai"), kind="refused")
    if not limits:
        return []

    lines: List[MetricLine] = []
    for entry in limits:
        entry_type = entry.get("type") if isinstance(entry.get("type"), str) else entry.get("name")
        if entry_type in {"CREDIT_LIMIT", "TOKENS_LIMIT"}:
            classified = _classify_token_window(entry)
            if classified is None:
                continue
            key, title, period_ms = classified
            lines.append(_percent_line(entry, key=key, title=title, period_ms=period_ms))

    web_entry = next(
        (
            entry
            for entry in limits
            if entry.get("type") == "TIME_LIMIT" or entry.get("name") == "TIME_LIMIT"
        ),
        None,
    )
    if web_entry is not None:
        lines.append(_web_search_line(web_entry))

    return lines


def map_zai_plan(payload: Any) -> Optional[str]:
    """Extract active subscription `productName` from `/api/biz/subscription/list`."""
    if not isinstance(payload, dict):
        return None
    items = payload.get("data")
    if not isinstance(items, list) or not items:
        return None
    first = items[0]
    if not isinstance(first, dict):
        return None
    name = first.get("productName")
    if not isinstance(name, str):
        return None
    trimmed = name.strip()
    return trimmed or None


def parse_zai_expiry(text: str) -> Optional[datetime]:
    """Parse Z.ai `yyyy-MM-dd HH:mm:ss` timestamp interpreted in UTC+8."""
    if not isinstance(text, str):
        return None
    try:
        naive = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    if naive.strftime("%Y-%m-%d %H:%M:%S") != text:
        return None
    return naive.replace(tzinfo=ZAI_TZ).astimezone(timezone.utc)


def map_zai_resets(
    payload: Any,
    *,
    now: Optional[datetime] = None,
) -> List[ResetItem]:
    """Map `/api/biz/customer-package-reset/list` into sorted, unexpired ResetItems."""
    if not isinstance(payload, dict) or payload.get("success") is False:
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Z.ai"), kind="refused")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Z.ai"), kind="refused")

    now_dt = now or utc_now()
    parsed: List[Tuple[datetime, int, str, str, str, str]] = []

    for reset_type, list_key, kind, title in _RESET_WINDOW_SPECS:
        if list_key not in data or data[list_key] is None:
            continue
        records = data[list_key]
        if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
            raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Z.ai"), kind="refused")
        for record in records:
            if record.get("available") is not True:
                continue
            record_id = record.get("recordId")
            if not isinstance(record_id, int) or isinstance(record_id, bool):
                raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Z.ai"), kind="refused")
            raw_expire = record.get("expireTime")
            expires_at = parse_zai_expiry(raw_expire) if isinstance(raw_expire, str) else None
            grant_type = record.get("grantType")
            if expires_at is None or not isinstance(grant_type, str) or not grant_type:
                raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Z.ai"), kind="refused")
            if expires_at > now_dt:
                parsed.append((expires_at, record_id, reset_type, kind, title, grant_type))

    parsed.sort(key=lambda item: (item[0], item[1]))

    seen_windows: set[str] = set()
    items: List[ResetItem] = []
    for expires_at, record_id, reset_type, kind, title, grant_type in parsed:
        is_earliest = reset_type not in seen_windows
        seen_windows.add(reset_type)
        items.append(
            ResetItem(
                id=f"zai:{reset_type}:{record_id}",
                provider_kind="zai",
                kind=kind,
                title=title,
                expires_at=format_iso8601(expires_at),
                usable_now=is_earliest,
                claim_payload={
                    "targetType": "PERSONAL",
                    "resetType": reset_type,
                    "recordId": record_id,
                    "grantType": grant_type,
                },
                unavailable_reason=None if is_earliest else "Wait until the earlier reset is used.",
            )
        )
    return items


def build_zai_resets_line(reset_items: List[ResetItem]) -> MetricLine:
    """Build the Rate Limit Resets MetricLine from unexpired ResetItems."""
    expiries = [item.expires_at for item in reset_items if item.expires_at]
    return MetricLine(
        key="rateLimitResets",
        title="Limit Resets",
        line_type="values",
        kind="count",
        values=[
            MetricValue(
                number=float(len(reset_items)),
                kind="count",
                label="available",
            )
        ],
        expiries_at=expiries,
        reset_items=list(reset_items),
        resource_key="rateLimitResets",
        resource_kind="balance",
        resource_unit="resets",
    )


def _api_headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }


# Secondary reads owned by this provider (see backend/secondary.py)
ZAI_SUBSCRIPTION_KEY = "zai_subscription"  # value: plan name
ZAI_RESETS_KEY = "zai_resets"              # value: List[ResetItem]


def discard_resets(cache: SecondaryCache) -> None:
    """Forget the cached reset list so the next refresh re-reads it."""
    cache.drop(ZAI_RESETS_KEY)


def _read_plan_cached(ctx: FetchContext, api_key: str) -> Optional[str]:
    def _fetch() -> Optional[str]:
        resp = ctx.http.get(SUBSCRIPTION_URL, headers=_api_headers(api_key), timeout=15.0)
        return map_zai_plan(resp.json()) if 200 <= resp.status < 300 else None

    value = ctx.secondary_cache.read(ZAI_SUBSCRIPTION_KEY, ctx.now, _fetch, force=ctx.force)
    return value if isinstance(value, str) else None


def _filter_unexpired_items(items: List[ResetItem], now: datetime) -> List[ResetItem]:
    kept: List[ResetItem] = []
    seen_windows: set[str] = set()
    for item in items:
        exp = parse_iso8601(item.expires_at)
        if exp is None or exp <= now:
            continue
        reset_type = str(item.claim_payload.get("resetType") or item.kind)
        is_earliest = reset_type not in seen_windows
        seen_windows.add(reset_type)
        kept.append(
            ResetItem(
                id=item.id,
                provider_kind=item.provider_kind,
                kind=item.kind,
                title=item.title,
                expires_at=item.expires_at,
                usable_now=is_earliest,
                claim_payload=dict(item.claim_payload),
                unavailable_reason=None if is_earliest else "Wait until the earlier reset is used.",
            )
        )
    return kept


def _read_resets_cached(ctx: FetchContext, api_key: str) -> Optional[List[ResetItem]]:
    def _fetch() -> Optional[List[ResetItem]]:
        resp = ctx.http.get(RESET_LIST_URL, headers=_api_headers(api_key), timeout=15.0)
        return map_zai_resets(resp.json(), now=ctx.now) if 200 <= resp.status < 300 else None

    items = ctx.secondary_cache.read(ZAI_RESETS_KEY, ctx.now, _fetch, force=ctx.force)
    if not isinstance(items, list):
        return None
    return _filter_unexpired_items(items, ctx.now)


def fetch(ctx: FetchContext) -> ProviderSnapshot:
    """Fetch Z.ai quota limits, subscription plan, and banked rate-limit resets."""
    api_key = str(ctx.credential.get("apiKey") or ctx.credential.get("accessToken") or "").strip()
    if not api_key:
        raise ProviderError(tr("error.apiKeyRejected", "%1 rejected the API key.", "Z.ai"), kind="api_key_rejected")

    resp = send_mapped(lambda: ctx.http.get(QUOTA_URL, headers=_api_headers(api_key), timeout=15.0))

    raise_for_http_error(resp, "Z.ai", is_api_key=True)

    quota_payload = read_json(resp, "Z.ai", expect=None, invalid_message=tr("error.invalidResponse", "%1 returned an invalid response.", "Z.ai"))

    lines = map_zai_quota(quota_payload)
    plan = _read_plan_cached(ctx, api_key)
    resets = _read_resets_cached(ctx, api_key)
    if resets is not None:
        lines.append(build_zai_resets_line(resets))

    return make_snapshot(ctx, plan=plan, lines=lines)
