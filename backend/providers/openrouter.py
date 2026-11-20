"""OpenRouter provider client, mapper, and UTC activity source (ported from OpenRouter*.swift)."""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from backend.backoff import SECONDARY_READ_INTERVAL_SEC
from backend.http_client import HTTPResponse
from backend.i18n import tr
from backend.models import (
    MetricLine,
    MetricValue,
    ProviderSnapshot,
    UsageBreakdown,
    UsageBreakdownModel,
    format_iso8601,
    parse_iso8601,
    utc_now,
)
from backend.providers.base import (
    parse_number as _parse_number,
    FetchContext,
    ProviderError,
    make_snapshot,
    raise_for_http_error,
    read_json,
    send_mapped,
)


CREDITS_URL = "https://openrouter.ai/api/v1/credits"
KEY_URL = "https://openrouter.ai/api/v1/key"
KEYS_URL = "https://openrouter.ai/api/v1/keys"
ACTIVITY_URL = "https://openrouter.ai/api/v1/activity"
KEYS_PAGE_SIZE = 100
MAX_KEYS_PAGES = 50
OPENROUTER_ACTIVITY_KEY = "openrouter_activity"  # ledger: {"days", "partialDay", "lastReadAt", "lastReadToday"}
PRIOR_DAYS_COUNT = 30

_ACTIVITY_DATE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})(?: (?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d)?$"
)
_QUIET_TOTALS: Dict[str, float] = {
    "spend": 0.0,
    "byokSpend": 0.0,
    "tokens": 0.0,
    "inputTokens": 0.0,
    "cacheReadTokens": 0.0,
    "outputTokens": 0.0,
    "reasoningTokens": 0.0,
}


def _quiet_day() -> Dict[str, Any]:
    return {"totals": dict(_QUIET_TOTALS), "byModel": {}}


def _utc_window_interval(period: Optional[str], now: datetime) -> Optional[Tuple[datetime, datetime]]:
    """Compute UTC ISO-8601 calendar window [start, end) where weeks start on Monday."""
    if not isinstance(period, str):
        return None
    now_utc = now.astimezone(timezone.utc)
    day_start = datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=timezone.utc)
    if period == "daily":
        return day_start, day_start + timedelta(days=1)
    if period == "weekly":
        monday = day_start - timedelta(days=day_start.weekday())
        return monday, monday + timedelta(days=7)
    if period == "monthly":
        month_start = datetime(now_utc.year, now_utc.month, 1, tzinfo=timezone.utc)
        days_in_month = calendar.monthrange(now_utc.year, now_utc.month)[1]
        return month_start, month_start + timedelta(days=days_in_month)
    return None


def _progress_line(
    *,
    key: str,
    title: str,
    used: float,
    limit: float,
    kind: str,
    resets: Optional[str],
    now: datetime,
    currency_code: Optional[str] = None,
    unit_suffix: Optional[str] = None,
    resource_key: Optional[str] = None,
    resource_unit: Optional[str] = None,
) -> MetricLine:
    interval = _utc_window_interval(resets, now)
    resets_at = format_iso8601(interval[1]) if interval is not None else None
    period_ms = (
        int(round((interval[1] - interval[0]).total_seconds() * 1000.0))
        if interval is not None
        else None
    )
    return MetricLine(
        key=key,
        title=title,
        line_type="progress",
        kind=kind,
        used=used,
        limit=limit,
        currency_code=currency_code,
        unit_suffix=unit_suffix,
        resets_at=resets_at,
        period_duration_ms=period_ms,
        resource_key=resource_key,
        resource_kind="consumption",
        resource_unit=resource_unit,
    )


def _extract_key_spend(key_obj: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    for window_key, period in (("today", "daily"), ("week", "weekly"), ("month", "monthly")):
        spend = _parse_number(key_obj.get(f"usage_{period}"))
        if spend is None:
            raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "OpenRouter"), kind="refused")
        totals: Dict[str, float] = {"spend": max(0.0, spend)}
        byok = _parse_number(key_obj.get(f"byok_usage_{period}"))
        if byok is not None:
            totals["byokSpend"] = max(0.0, byok)
        out[window_key] = totals
    return out


def _key_limit_line(data: Dict[str, Any], now: datetime) -> Optional[MetricLine]:
    limit = _parse_number(data.get("limit"))
    if limit is None or limit <= 0:
        return None
    remaining = _parse_number(data.get("limit_remaining"))
    if remaining is None:
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "OpenRouter"), kind="refused")
    used = max(0.0, limit - max(0.0, remaining))
    return _progress_line(
        key="keyLimit",
        title="Key Limit",
        used=used,
        limit=limit,
        kind="dollars",
        resets=data.get("limit_reset") if isinstance(data.get("limit_reset"), str) else None,
        now=now,
        currency_code="USD",
        resource_key="keyLimit",
        resource_unit="usd",
    )


def _budget_line(data: Dict[str, Any], now: datetime) -> Optional[MetricLine]:
    budget = data.get("effective_budget")
    if not isinstance(budget, dict):
        return None
    limit = _parse_number(budget.get("limit_usd"))
    if limit is None or limit <= 0:
        return None
    spend = _parse_number(budget.get("spend_usd"))
    if spend is None:
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "OpenRouter"), kind="refused")
    return _progress_line(
        key="budget",
        title="Budget",
        used=max(0.0, spend),
        limit=limit,
        kind="dollars",
        resets=budget.get("reset_interval") if isinstance(budget.get("reset_interval"), str) else None,
        now=now,
        currency_code="USD",
    )


def _free_requests_line(data: Dict[str, Any], now: datetime) -> Optional[MetricLine]:
    free = data.get("free_model_daily_requests")
    if not isinstance(free, dict):
        return None
    limit = _parse_number(free.get("limit"))
    if limit is None or limit <= 0:
        return None
    used = _parse_number(free.get("used"))
    if used is None:
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "OpenRouter"), kind="refused")
    return _progress_line(
        key="freeRequests",
        title="Free Requests",
        used=max(0.0, used),
        limit=limit,
        kind="count",
        resets="daily",
        now=now,
        unit_suffix="requests",
    )


def map_openrouter_key(
    payload: Any,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Map `/api/v1/key` JSON into `{is_management, plan, lines, spend}`."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "OpenRouter"), kind="refused")
    data = payload["data"]
    is_management = data.get("is_management_key")
    if not isinstance(is_management, bool):
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "OpenRouter"), kind="refused")

    now_dt = now or utc_now()
    is_free_tier = data.get("is_free_tier")
    plan = ("Free" if is_free_tier else "Pay-as-you-go") if isinstance(is_free_tier, bool) else None

    lines: List[MetricLine] = []
    for candidate in (
        _key_limit_line(data, now_dt),
        _budget_line(data, now_dt),
        _free_requests_line(data, now_dt),
    ):
        if candidate is not None:
            lines.append(candidate)

    spend: Dict[str, Dict[str, float]] = {} if is_management else _extract_key_spend(data)
    return {
        "is_management": is_management,
        "plan": plan,
        "lines": lines,
        "spend": spend,
    }


def map_openrouter_credits(payload: Any) -> List[MetricLine]:
    """Map `/api/v1/credits` JSON into the Balance line."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "OpenRouter"), kind="refused")
    data = payload["data"]
    total_credits = _parse_number(data.get("total_credits"))
    total_usage = _parse_number(data.get("total_usage"))
    if total_credits is None or total_usage is None:
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "OpenRouter"), kind="refused")
    balance = max(0.0, max(0.0, total_credits) - max(0.0, total_usage))
    return [
        MetricLine(
            key="balance",
            title="Balance",
            line_type="values",
            kind="dollars",
            currency_code="USD",
            values=[
                MetricValue(
                    number=balance,
                    kind="dollars",
                    label="left",
                    currency_code="USD",
                )
            ],
            resource_key="balance",
            resource_kind="balance",
            resource_unit="usd",
        )
    ]


def map_openrouter_keys_page(payload: Any) -> List[Dict[str, Dict[str, float]]]:
    """Map one page of `/api/v1/keys` into per-key spend dicts."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "OpenRouter"), kind="refused")
    result: List[Dict[str, Dict[str, float]]] = []
    for entry in payload["data"]:
        if not isinstance(entry, dict):
            raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "OpenRouter"), kind="refused")
        result.append(_extract_key_spend(entry))
    return result


def _merge_spend_dicts(
    acc: Dict[str, Dict[str, float]],
    other: Dict[str, Dict[str, float]],
) -> Dict[str, Dict[str, float]]:
    merged = {w: dict(vals) for w, vals in acc.items()}
    for window_key, vals in other.items():
        target = merged.setdefault(window_key, {})
        for measure, num in vals.items():
            target[measure] = target.get(measure, 0.0) + num
    return merged


def _parse_activity_row(row: Dict[str, Any]) -> Optional[Dict[str, float]]:
    fields = (
        ("spend", "usage"),
        ("byokSpend", "byok_usage_inference"),
        ("inputTokens", "prompt_tokens"),
        ("cacheReadTokens", "cached_tokens"),
        ("outputTokens", "completion_tokens"),
        ("reasoningTokens", "reasoning_tokens"),
    )
    totals: Dict[str, float] = {}
    for measure, field_name in fields:
        if field_name not in row or row[field_name] is None:
            continue
        num = _parse_number(row[field_name])
        if num is None or num < 0:
            return None
        totals[measure] = num

    if "spend" not in totals or "inputTokens" not in totals or "outputTokens" not in totals:
        return None
    prompt = totals["inputTokens"]
    completion = totals["outputTokens"]
    # Prompt tokens include cached ones; completion tokens include reasoning.
    totals["tokens"] = prompt + completion
    totals["inputTokens"] = prompt - totals.get("cacheReadTokens", 0.0)
    totals["outputTokens"] = completion - totals.get("reasoningTokens", 0.0)
    return totals


def _add_totals(a: Dict[str, float], b: Dict[str, float]) -> Dict[str, float]:
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0.0) + v
    return out


def parse_openrouter_activity_days(payload: Any) -> Optional[Dict[date, Dict[str, Any]]]:
    """Parse `/api/v1/activity` rows summed per UTC date and model; None if malformed."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return None
    days: Dict[date, Dict[str, Any]] = {}
    for row in payload["data"]:
        if not isinstance(row, dict):
            return None
        raw_date = row.get("date")
        model = row.get("model")
        if not isinstance(raw_date, str) or not isinstance(model, str):
            return None
        match = _ACTIVITY_DATE_RE.match(raw_date)
        if not match:
            return None
        try:
            day_key = date.fromisoformat(match.group(1))
        except ValueError:
            return None
        totals = _parse_activity_row(row)
        if totals is None:
            return None

        day_entry = days.get(day_key)
        if day_entry is None:
            day_entry = _quiet_day()
            days[day_key] = day_entry

        day_entry["totals"] = _add_totals(day_entry["totals"], totals)
        by_model: Dict[str, Dict[str, float]] = day_entry["byModel"]
        if model in by_model:
            by_model[model] = _add_totals(by_model[model], totals)
        else:
            by_model[model] = dict(totals)
    return days


def _window_dates(window_key: str, now_utc: datetime) -> List[date]:
    today = now_utc.date()
    if window_key == "today":
        return [today]
    if window_key == "yesterday":
        return [today - timedelta(days=1)]
    if window_key == "prior30":
        return [today - timedelta(days=offset) for offset in range(1, PRIOR_DAYS_COUNT + 1)]
    if window_key == "week":
        monday = today - timedelta(days=today.weekday())
        span_days = (today - monday).days + 1
        return [monday + timedelta(days=i) for i in range(span_days)]
    if window_key == "month":
        first = date(today.year, today.month, 1)
        span_days = (today - first).days + 1
        return [first + timedelta(days=i) for i in range(span_days)]
    return []


def _build_breakdown(
    title: str,
    totals: Dict[str, float],
    models: Dict[str, Dict[str, float]],
) -> UsageBreakdown:
    token_parts: Dict[str, float] = {}
    for key, label in (
        ("inputTokens", "Input"),
        ("cacheReadTokens", "Cache Read"),
        ("outputTokens", "Output"),
        ("reasoningTokens", "Reasoning"),
    ):
        val = totals.get(key)
        if val is not None and val > 0:
            token_parts[label] = val

    has_dollars = any((m.get("spend") or 0.0) > 0 for m in models.values())
    has_tokens = any((m.get("tokens") or 0.0) > 0 for m in models.values())

    model_rows: List[UsageBreakdownModel] = []
    for name, m_totals in models.items():
        spend_val = m_totals.get("spend") if has_dollars else None
        tok_val = m_totals.get("tokens") if has_tokens else None
        if spend_val is not None or tok_val is not None:
            model_rows.append(UsageBreakdownModel(name=name, spend=spend_val, tokens=tok_val))

    return UsageBreakdown(
        title=title,
        spend=totals.get("spend"),
        currency_code="USD",
        tokens=totals.get("tokens"),
        token_parts=token_parts,
        models=model_rows,
    )


def map_openrouter_history_lines(
    *,
    reported_spend: Optional[Dict[str, Dict[str, float]]] = None,
    activity_days: Optional[Dict[date, Dict[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> List[MetricLine]:
    """Build Today, Yesterday, This Week, This Month, and Prior 30 Days rows."""
    now_utc = (now or utc_now()).astimezone(timezone.utc)
    reported = reported_spend or {}
    days = activity_days or {}

    windows: Tuple[Tuple[str, str], ...] = (
        ("today", "Today"),
        ("yesterday", "Yesterday"),
        ("week", "This Week"),
        ("month", "This Month"),
        ("prior30", "Prior 30 Days"),
    )

    lines: List[MetricLine] = []
    for window_key, title in windows:
        dates = _window_dates(window_key, now_utc)
        present = [days[d] for d in dates if d in days]

        summed: Optional[Dict[str, float]] = None
        models: Dict[str, Dict[str, float]] = {}
        if present:
            summed = {}
            for entry in present:
                summed = _add_totals(summed, entry.get("totals") or {})
                by_model = entry.get("byModel") or {}
                for name, m_totals in by_model.items():
                    if name in models:
                        models[name] = _add_totals(models[name], m_totals)
                    else:
                        models[name] = dict(m_totals)

        rep = reported.get(window_key)
        if rep is None and summed is None:
            continue

        # Reported measures win over summed days (matching UsageHistory.totals(over:)).
        combined: Dict[str, float] = dict(summed or {})
        if rep is not None:
            combined.update(rep)

        if "spend" not in combined:
            continue

        lines.append(
            MetricLine(
                key=window_key,
                title=title,
                line_type="values",
                kind="dollars",
                currency_code="USD",
                values=[MetricValue(number=combined["spend"], kind="dollars", currency_code="USD")],
                breakdown=_build_breakdown(title, combined, models),
            )
        )

    return lines


def _api_headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }


def _http_get(ctx: FetchContext, url: str, api_key: str) -> HTTPResponse:
    return send_mapped(lambda: ctx.http.get(url, headers=_api_headers(api_key), timeout=15.0))


def _fetch_credits_lines(ctx: FetchContext, api_key: str) -> List[MetricLine]:
    resp = _http_get(ctx, CREDITS_URL, api_key)
    # Workspace-scoped management keys receive 403 on account-level credits.
    if resp.status == 403:
        return []
    raise_for_http_error(resp, "OpenRouter", is_api_key=True)
    payload = read_json(resp, "OpenRouter", expect=None, invalid_message=tr("error.invalidResponse", "%1 returned an invalid response.", "OpenRouter"))
    return map_openrouter_credits(payload)


def _fetch_listed_keys_spend(ctx: FetchContext, api_key: str) -> Dict[str, Dict[str, float]]:
    total: Dict[str, Dict[str, float]] = {}
    for page in range(MAX_KEYS_PAGES):
        offset = page * KEYS_PAGE_SIZE
        url = f"{KEYS_URL}?include_disabled=true&include_expired=true&offset={offset}"
        resp = _http_get(ctx, url, api_key)
        raise_for_http_error(resp, "OpenRouter", is_api_key=True)
        payload = read_json(resp, "OpenRouter", expect=None, invalid_message=tr("error.invalidResponse", "%1 returned an invalid response.", "OpenRouter"))
        keys = map_openrouter_keys_page(payload)
        for key_spend in keys:
            total = _merge_spend_dicts(total, key_spend)
        if len(keys) < KEYS_PAGE_SIZE:
            return total
    raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "OpenRouter"), kind="refused")


def _read_activity_cached(ctx: FetchContext, api_key: str) -> Dict[date, Dict[str, Any]]:
    now_utc = ctx.now.astimezone(timezone.utc)
    today = now_utc.date()
    # Span reaches back 31 days (today + 30 prior days), matching HistoryRow.dayCount
    span = [today - timedelta(days=offset) for offset in range(PRIOR_DAYS_COUNT + 1)]
    span_set = set(span)

    ledger = ctx.secondary_cache.get(OPENROUTER_ACTIVITY_KEY)
    if not isinstance(ledger, dict):
        ledger = {"days": {}, "partialDay": None, "lastReadAt": None, "lastReadToday": None}

    days: Dict[date, Dict[str, Any]] = dict(ledger.get("days") or {})
    partial_day: Optional[date] = ledger.get("partialDay")
    last_read_at = parse_iso8601(ledger.get("lastReadAt"))
    last_read_today: Optional[date] = ledger.get("lastReadToday")

    if (
        not ctx.force
        and last_read_at is not None
        and last_read_today == today
        and (ctx.now - last_read_at).total_seconds() < SECONDARY_READ_INTERVAL_SEC
    ):
        return {d: v for d, v in days.items() if d in span_set}

    due = list(span[:2]) + [d for d in span[2:] if d not in days or d == partial_day]
    # `/api/v1/activity` only covers finished UTC days (strictly before today).
    finished_due = [d for d in due if d < today]

    read_days: Dict[date, Dict[str, Any]] = {}
    if finished_due:
        query = f"?date={finished_due[0].isoformat()}" if len(finished_due) == 1 else ""
        try:
            resp = ctx.http.get(f"{ACTIVITY_URL}{query}", headers=_api_headers(api_key), timeout=15.0)
            if 200 <= resp.status < 300:
                parsed = parse_openrouter_activity_days(resp.json())
                if parsed is not None:
                    for d in finished_due:
                        read_days[d] = parsed.get(d, _quiet_day())
        except Exception:
            pass

    kept_keys = span_set.difference(due)
    updated_days: Dict[date, Dict[str, Any]] = {d: v for d, v in days.items() if d in kept_keys}
    updated_days.update(read_days)

    ctx.secondary_cache[OPENROUTER_ACTIVITY_KEY] = {
        "days": updated_days,
        "partialDay": today,
        "lastReadAt": format_iso8601(ctx.now),
        "lastReadToday": today,
    }
    return updated_days


def fetch(ctx: FetchContext) -> ProviderSnapshot:
    """Fetch OpenRouter balance, key limits/budget/free requests, and spend history."""
    api_key = str(ctx.credential.get("apiKey") or ctx.credential.get("accessToken") or "").strip()
    if not api_key:
        raise ProviderError(tr("error.apiKeyRejected", "%1 rejected the API key.", "OpenRouter"), kind="api_key_rejected")

    key_resp = _http_get(ctx, KEY_URL, api_key)
    raise_for_http_error(key_resp, "OpenRouter", is_api_key=True)
    try:
        key_payload = key_resp.json()
    except Exception as exc:
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "OpenRouter"), kind="refused") from exc

    parsed_key = map_openrouter_key(key_payload, now=ctx.now)

    if not parsed_key["is_management"]:
        history_lines = map_openrouter_history_lines(
            reported_spend=parsed_key["spend"],
            activity_days=None,
            now=ctx.now,
        )
        return make_snapshot(
            ctx,
            plan=parsed_key["plan"],
            lines=history_lines + parsed_key["lines"],
        )

    credits_lines = _fetch_credits_lines(ctx, api_key)
    listed_spend = _fetch_listed_keys_spend(ctx, api_key)
    activity_days = _read_activity_cached(ctx, api_key)
    history_lines = map_openrouter_history_lines(
        reported_spend=listed_spend,
        activity_days=activity_days,
        now=ctx.now,
    )

    return make_snapshot(
        ctx,
        plan=parsed_key["plan"],
        lines=credits_lines + history_lines + parsed_key["lines"],
    )
