"""Ollama provider client, mapper, and UTC daily usage history (ported from Ollama*.swift)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from backend.backoff import SECONDARY_READ_INTERVAL_SEC
from backend.http_client import HTTPResponse
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


HOST = "https://ollama.com"
BALANCE_URL = f"{HOST}/api/balance"
ACCOUNT_URL = f"{HOST}/api/me"
USAGE_URL = f"{HOST}/api/usage"
HISTORY_DAY_COUNT = 30

_USAGE_MEASURES: Tuple[Tuple[str, str], ...] = (
    ("request_count", "requests"),
    ("usage_usd", "usageValue"),
    ("input_tokens", "inputTokens"),
    ("cached_input_tokens", "cacheReadTokens"),
    ("output_tokens", "outputTokens"),
)


def _legacy_line(
    entry: Any,
    *,
    key: str,
    title: str,
    period_ms: int,
) -> Optional[MetricLine]:
    if not isinstance(entry, dict):
        return None
    remaining = _parse_number(entry.get("remaining_percent"))
    if remaining is None:
        return None
    resets_dt = parse_iso8601(entry.get("resets_at")) if isinstance(entry.get("resets_at"), str) else None
    used = _clamp_percent(100.0 - remaining)
    return MetricLine(
        key=key,
        title=title,
        line_type="progress",
        kind="percent",
        used=used,
        limit=100.0,
        resets_at=format_iso8601(resets_dt),
        period_duration_ms=period_ms if resets_dt is not None else None,
        resource_key=key,
        resource_kind="consumption",
        resource_unit="percent",
    )


def _monthly_line(included: Dict[str, Any]) -> Optional[MetricLine]:
    allowance = _parse_number(included.get("allowance_usd"))
    balance = _parse_number(included.get("balance_usd"))
    if allowance is None or allowance <= 0 or balance is None:
        return None

    period = included.get("period")
    from_dt: Optional[datetime] = None
    until_dt: Optional[datetime] = None
    if isinstance(period, dict):
        if isinstance(period.get("from"), str):
            from_dt = parse_iso8601(period["from"])
        if isinstance(period.get("until"), str):
            until_dt = parse_iso8601(period["until"])

    period_ms: Optional[int] = None
    if from_dt is not None and until_dt is not None and until_dt > from_dt:
        period_ms = int((until_dt - from_dt).total_seconds() * 1000.0)

    used = min(allowance, max(0.0, allowance - balance))
    return MetricLine(
        key="monthly",
        title="Monthly",
        line_type="progress",
        kind="dollars",
        used=used,
        limit=allowance,
        currency_code="USD",
        resets_at=format_iso8601(until_dt),
        period_duration_ms=period_ms,
        resource_key="monthly",
        resource_kind="consumption",
        resource_unit="usd",
    )


def _purchased_line(purchased: Any) -> Optional[MetricLine]:
    if not isinstance(purchased, dict):
        return None
    balance = _parse_number(purchased.get("balance_usd"))
    if balance is None:
        return None
    return MetricLine(
        key="purchasedCredits",
        title="Extra Usage",
        line_type="values",
        kind="dollars",
        currency_code="USD",
        values=[
            MetricValue(
                number=max(0.0, balance),
                kind="dollars",
                label="left",
                currency_code="USD",
            )
        ],
        resource_key="purchasedCredits",
        resource_kind="balance",
        resource_unit="usd",
    )


def map_ollama_balance(payload: Any) -> List[MetricLine]:
    """Map `/api/balance` JSON into progress/balance MetricLines."""
    if not isinstance(payload, dict):
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Ollama"), kind="refused")
    included = payload.get("included")
    if not isinstance(included, dict):
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Ollama"), kind="refused")

    lines: List[MetricLine] = []
    session = _legacy_line(included.get("session"), key="session", title="Session", period_ms=MetricPeriod.SESSION_MS)
    if session is not None:
        lines.append(session)
    weekly = _legacy_line(included.get("weekly"), key="weekly", title="Weekly", period_ms=MetricPeriod.WEEK_MS)
    if weekly is not None:
        lines.append(weekly)
    monthly = _monthly_line(included)
    if monthly is not None:
        lines.append(monthly)
    purchased = _purchased_line(payload.get("purchased"))
    if purchased is not None:
        lines.append(purchased)

    if not lines:
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Ollama"), kind="refused")
    return lines


def map_ollama_account(payload: Any) -> Tuple[Optional[str], Optional[str]]:
    """Extract (plan, email) from `/api/me` accepting both capitalized and lowercase keys."""
    if not isinstance(payload, dict):
        return None, None
    raw_plan = payload.get("Plan") if "Plan" in payload else payload.get("plan")
    plan: Optional[str] = None
    if isinstance(raw_plan, str) and raw_plan.strip():
        plan = raw_plan.strip().title()

    raw_email = payload.get("Email") if "Email" in payload else payload.get("email")
    email: Optional[str] = None
    if isinstance(raw_email, str) and raw_email.strip():
        email = raw_email.strip()

    return plan, email


def _parse_bucket_totals(obj: Dict[str, Any]) -> Optional[Dict[str, float]]:
    totals: Dict[str, float] = {}
    for field_name, measure in _USAGE_MEASURES:
        if field_name not in obj or obj[field_name] is None:
            continue
        num = _parse_number(obj[field_name])
        if num is None or num < 0:
            return None
        totals[measure] = num
    if "requests" not in totals:
        return None
    if "inputTokens" in totals and "outputTokens" in totals:
        # Cached input is part of input, so total tokens = input + output.
        totals["tokens"] = totals["inputTokens"] + totals["outputTokens"]
    return totals


def parse_ollama_usage_days(payload: Any) -> Optional[Dict[date, Dict[str, Any]]]:
    """Parse `/api/usage?range=...&group_by=model` into UTC-day records.

    Returns None if any bucket or group is malformed, matching OllamaHistorySource.days.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("granularity") != "day":
        return None
    buckets = payload.get("buckets")
    if not isinstance(buckets, list):
        return None

    days: Dict[date, Dict[str, Any]] = {}
    for raw_bucket in buckets:
        if not isinstance(raw_bucket, dict):
            return None
        raw_from = raw_bucket.get("from")
        raw_until = raw_bucket.get("until")
        if not isinstance(raw_from, str) or not isinstance(raw_until, str):
            return None
        from_dt = parse_iso8601(raw_from)
        until_dt = parse_iso8601(raw_until)
        if from_dt is None or until_dt is None:
            return None
        from_utc = from_dt.astimezone(timezone.utc)
        until_utc = until_dt.astimezone(timezone.utc)
        if (
            from_utc.hour != 0
            or from_utc.minute != 0
            or from_utc.second != 0
            or from_utc.microsecond != 0
        ):
            return None
        day_key = from_utc.date()
        if day_key in days:
            return None
        day_end = from_utc + timedelta(days=1)
        if not (from_utc < until_utc <= day_end):
            return None

        totals = _parse_bucket_totals(raw_bucket)
        if totals is None:
            return None

        by_model: Optional[Dict[str, Dict[str, float]]] = None
        if "groups" in raw_bucket and raw_bucket["groups"] is not None:
            raw_groups = raw_bucket["groups"]
            if not isinstance(raw_groups, list):
                return None
            models: Dict[str, Dict[str, float]] = {}
            for raw_group in raw_groups:
                if not isinstance(raw_group, dict):
                    return None
                model_name = raw_group.get("model")
                if not isinstance(model_name, str) or not model_name:
                    return None
                if model_name in models:
                    return None
                model_totals = _parse_bucket_totals(raw_group)
                if model_totals is None:
                    return None
                models[model_name] = model_totals
            by_model = models if raw_bucket.get("remainder") is None else None

        days[day_key] = {"totals": totals, "byModel": by_model}

    return days


def _sum_measure_dicts(dicts: List[Dict[str, float]]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for d in dicts:
        for k, v in d.items():
            out[k] = out.get(k, 0.0) + v
    return out


def _aggregate_window(
    days_by_date: Dict[date, Dict[str, Any]],
    target_dates: List[date],
) -> Tuple[Optional[Dict[str, float]], Dict[str, Dict[str, float]]]:
    present = [days_by_date[d] for d in target_dates if d in days_by_date]
    if not present:
        return None, {}
    totals = _sum_measure_dicts([entry["totals"] for entry in present])
    models: Dict[str, Dict[str, float]] = {}
    for entry in present:
        by_model = entry.get("byModel")
        if not isinstance(by_model, dict):
            continue
        for name, m_totals in by_model.items():
            if name not in models:
                models[name] = dict(m_totals)
            else:
                for mk, mv in m_totals.items():
                    models[name][mk] = models[name].get(mk, 0.0) + mv
    return totals, models


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
    ):
        val = totals.get(key)
        if val is not None and val > 0:
            token_parts[label] = val

    has_dollars = any((m.get("usageValue") or 0.0) > 0 for m in models.values())
    has_tokens = any((m.get("tokens") or 0.0) > 0 for m in models.values())

    model_rows: List[UsageBreakdownModel] = []
    for name, m_totals in models.items():
        spend_val = m_totals.get("usageValue") if has_dollars else None
        tok_val = m_totals.get("tokens") if has_tokens else None
        if spend_val is not None or tok_val is not None:
            model_rows.append(UsageBreakdownModel(name=name, spend=spend_val, tokens=tok_val))

    return UsageBreakdown(
        title=title,
        spend=totals.get("usageValue"),
        currency_code="USD",
        tokens=totals.get("tokens"),
        token_parts=token_parts,
        models=model_rows,
    )


def map_ollama_history_lines(
    days_by_date: Dict[date, Dict[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> List[MetricLine]:
    """Build Today, Yesterday, Last 30 Days, and Requests history rows from UTC day records."""
    if not days_by_date:
        return []
    now_utc = (now or utc_now()).astimezone(timezone.utc)
    today = now_utc.date()
    yesterday = today - timedelta(days=1)
    last_30 = [today - timedelta(days=offset) for offset in range(HISTORY_DAY_COUNT)]

    lines: List[MetricLine] = []
    windows: Tuple[Tuple[str, str, List[date]], ...] = (
        ("today", "Today", [today]),
        ("yesterday", "Yesterday", [yesterday]),
        ("last30", "Last 30 Days", last_30),
    )

    for key, title, dates in windows:
        totals, models = _aggregate_window(days_by_date, dates)
        if totals is None:
            continue
        values: List[MetricValue] = []
        if "usageValue" in totals:
            values.append(MetricValue(number=totals["usageValue"], kind="dollars", currency_code="USD"))
        if "tokens" in totals:
            values.append(MetricValue(number=totals["tokens"], kind="count", label="tokens"))
        if not values:
            continue
        lines.append(
            MetricLine(
                key=key,
                title=title,
                line_type="values",
                kind=values[0].kind,
                currency_code="USD" if values[0].kind == "dollars" else None,
                values=values,
                breakdown=_build_breakdown(title, totals, models),
            )
        )

    req_totals, _ = _aggregate_window(days_by_date, last_30)
    if req_totals is not None and "requests" in req_totals:
        lines.append(
            MetricLine(
                key="requests",
                title="Requests",
                line_type="values",
                kind="count",
                values=[
                    MetricValue(
                        number=req_totals["requests"],
                        kind="count",
                        label="in 30 days",
                    )
                ],
            )
        )

    return lines


def _api_headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }


OLLAMA_ACCOUNT_KEY = "ollama_account"  # secondary read; value: {"plan", "email"}
OLLAMA_HISTORY_KEY = "ollama_history"  # ledger: {"days", "partialDay", "lastReadAt", "lastReadToday"}


def _read_account_cached(ctx: FetchContext, api_key: str) -> Tuple[Optional[str], Optional[str]]:
    # Each field keeps its previous value when the fresh read lacks it.
    def _fetch() -> Dict[str, Optional[str]]:
        prev = ctx.secondary_cache.value(OLLAMA_ACCOUNT_KEY)
        prev = prev if isinstance(prev, dict) else {}
        fresh_plan: Optional[str] = None
        fresh_email: Optional[str] = None
        try:
            resp = ctx.http.post(ACCOUNT_URL, headers=_api_headers(api_key), timeout=15.0)
            if 200 <= resp.status < 300:
                fresh_plan, fresh_email = map_ollama_account(resp.json())
        except Exception:
            pass
        return {
            "plan": fresh_plan if fresh_plan is not None else prev.get("plan"),
            "email": fresh_email if fresh_email is not None else prev.get("email"),
        }

    value = ctx.secondary_cache.read(OLLAMA_ACCOUNT_KEY, ctx.now, _fetch, force=ctx.force)
    value = value if isinstance(value, dict) else {}
    return value.get("plan"), value.get("email")


def _read_history_cached(ctx: FetchContext, api_key: str) -> Dict[date, Dict[str, Any]]:
    now_utc = ctx.now.astimezone(timezone.utc)
    today = now_utc.date()
    span = [today - timedelta(days=offset) for offset in range(HISTORY_DAY_COUNT)]
    span_set = set(span)

    ledger = ctx.secondary_cache.get(OLLAMA_HISTORY_KEY)
    if not isinstance(ledger, dict):
        ledger = {"days": {}, "partialDay": None, "lastReadAt": None, "lastReadToday": None}

    days: Dict[date, Dict[str, Any]] = dict(ledger.get("days") or {})
    partial_day: Optional[date] = ledger.get("partialDay")
    last_read_at = parse_iso8601(ledger.get("lastReadAt"))
    last_read_today: Optional[date] = ledger.get("lastReadToday")

    # Rest for 1h on the same UTC day unless forced
    if (
        not ctx.force
        and last_read_at is not None
        and last_read_today == today
        and (ctx.now - last_read_at).total_seconds() < SECONDARY_READ_INTERVAL_SEC
    ):
        return {d: v for d, v in days.items() if d in span_set}

    # Today and yesterday are always due; older days until read after they ended
    due = list(span[:2]) + [d for d in span[2:] if d not in days or d == partial_day]
    week_ago = today - timedelta(days=7)
    range_param = "7d" if all(d >= week_ago for d in due) else "30d"

    read_days: Dict[date, Dict[str, Any]] = {}
    try:
        resp: HTTPResponse = ctx.http.get(
            f"{USAGE_URL}?range={range_param}&group_by=model",
            headers=_api_headers(api_key),
            timeout=15.0,
        )
        if 200 <= resp.status < 300:
            parsed = parse_ollama_usage_days(resp.json())
            if parsed is not None:
                unreported = {"totals": {}, "byModel": None}
                for d in due:
                    read_days[d] = parsed.get(d, unreported)
    except Exception:
        pass

    kept_keys = span_set.difference(due)
    updated_days: Dict[date, Dict[str, Any]] = {d: v for d, v in days.items() if d in kept_keys}
    updated_days.update(read_days)

    ctx.secondary_cache[OLLAMA_HISTORY_KEY] = {
        "days": updated_days,
        "partialDay": today,
        "lastReadAt": format_iso8601(ctx.now),
        "lastReadToday": today,
    }
    return updated_days


def fetch(ctx: FetchContext) -> ProviderSnapshot:
    """Fetch Ollama balance, plan/email, and UTC daily usage history."""
    api_key = str(ctx.credential.get("apiKey") or ctx.credential.get("accessToken") or "").strip()
    if not api_key:
        raise ProviderError(tr("error.apiKeyRejected", "%1 rejected the API key.", "Ollama"), kind="api_key_rejected")

    resp = send_mapped(lambda: ctx.http.get(BALANCE_URL, headers=_api_headers(api_key), timeout=15.0))

    raise_for_http_error(resp, "Ollama", is_api_key=True)

    balance_payload = read_json(resp, "Ollama", expect=None, invalid_message=tr("error.invalidResponse", "%1 returned an invalid response.", "Ollama"))

    lines = map_ollama_balance(balance_payload)
    plan, email = _read_account_cached(ctx, api_key)
    history_days = _read_history_cached(ctx, api_key)
    if history_days:
        lines.extend(map_ollama_history_lines(history_days, now=ctx.now))

    return make_snapshot(ctx, plan=plan, lines=lines, account_label_discovered=email)
