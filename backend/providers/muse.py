"""Muse Code (Meta) provider client and usage mapper (ported from MuseUsageClient/MuseUsageMapper.swift)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional, Tuple

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


KEY_URL = "https://api.meta.ai/muse-code/key"


MUSE_LAST_KNOWN_KEY = "muse_last_known"  # raw: last mapped MetricLines

def _make_window_line(
    *,
    key: str,
    title: str,
    used: float,
    resets_at: Optional[str],
    period_ms: int,
    signal: Optional[str],
) -> MetricLine:
    return MetricLine(
        key=key,
        title=title,
        line_type="progress",
        kind="percent",
        used=_clamp_percent(used),
        limit=100.0,
        resets_at=resets_at,
        period_duration_ms=period_ms,
        session_start_signal=signal,
        resource_key=key,
        resource_kind="consumption",
        resource_unit="percent",
    )


def _aged_line(
    kept: MetricLine,
    *,
    key: str,
    title: str,
    period_ms: int,
    signal: Optional[str],
    now: datetime,
) -> MetricLine:
    """Keep a cached window line unless its reset timestamp has already passed, in which case reset to 0%."""
    resets_dt = parse_iso8601(kept.resets_at)
    if resets_dt is not None and resets_dt <= now:
        return _make_window_line(
            key=key,
            title=title,
            used=0.0,
            resets_at=None,
            period_ms=kept.period_duration_ms or period_ms,
            signal=signal,
        )
    return _make_window_line(
        key=key,
        title=title,
        used=kept.used,
        resets_at=kept.resets_at,
        period_ms=kept.period_duration_ms or period_ms,
        signal=signal,
    )


def map_muse_usage(
    payload: Any,
    *,
    last_known: Optional[List[MetricLine]] = None,
    now: Optional[datetime] = None,
) -> Tuple[Optional[str], List[MetricLine], Optional[str]]:
    """Map Muse Code `/muse-code/key` JSON into (plan, lines, discovered_email).

    Only subscription and usage fields are read; any Model API key present in the response is ignored.
    Muse omits `subs_usage` until first use and while the 5-hour window is idle even when weekly has usage,
    so previous values are preserved when `subs_usage` is null/absent, and past-due windows reset to 0%.
    """
    if not isinstance(payload, dict):
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Muse Code"), kind="refused")

    is_subscribed = payload.get("is_subs_active")
    if not isinstance(is_subscribed, bool):
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Muse Code"), kind="refused")
    if not is_subscribed:
        raise ProviderError(tr("provider.muse.noSubscription", "No Muse Code subscription on this account."), kind="no_quota")

    now_dt = now or utc_now()
    subs_usage = payload.get("subs_usage")
    if subs_usage is not None and not isinstance(subs_usage, dict):
        raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Muse Code"), kind="refused")

    kept_lines = list(last_known or [])
    specs = (
        ("session", "Session", "window", MetricPeriod.SESSION_MS, "missing_reset_date"),
        ("weekly", "Weekly", "weekly", MetricPeriod.WEEK_MS, None),
    )

    lines: List[MetricLine] = []
    for key, title, field_name, period_ms, signal in specs:
        raw_win = subs_usage.get(field_name) if isinstance(subs_usage, dict) else None
        if raw_win is not None:
            if not isinstance(raw_win, dict):
                raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Muse Code"), kind="refused")
            used_pct = _parse_number(raw_win.get("used_percent"))
            if used_pct is None:
                raise ProviderError(tr("error.invalidResponse", "%1 returned an invalid response.", "Muse Code"), kind="refused")
            resets_dt = parse_iso8601(raw_win.get("resets_at"))
            lines.append(
                _make_window_line(
                    key=key,
                    title=title,
                    used=used_pct,
                    resets_at=format_iso8601(resets_dt),
                    period_ms=period_ms,
                    signal=signal,
                )
            )
            continue

        if subs_usage is None:
            kept = next(
                (line for line in kept_lines if line.key == key or line.title == title),
                None,
            )
            if kept is not None:
                lines.append(
                    _aged_line(
                        kept,
                        key=key,
                        title=title,
                        period_ms=period_ms,
                        signal=signal,
                        now=now_dt,
                    )
                )
                continue

        lines.append(
            _make_window_line(
                key=key,
                title=title,
                used=0.0,
                resets_at=None,
                period_ms=period_ms,
                signal=signal,
            )
        )

    raw_plan = payload.get("subs_tier_name")
    plan = raw_plan.strip() if isinstance(raw_plan, str) and raw_plan.strip() else None

    raw_email = payload.get("user_email")
    email = raw_email.strip() if isinstance(raw_email, str) and raw_email.strip() else None

    return plan, lines, email


def fetch(ctx: FetchContext) -> ProviderSnapshot:
    """Fetch Muse Code subscription usage via POST https://api.meta.ai/muse-code/key."""
    access_token = str(ctx.credential.get("accessToken") or "").strip()
    if not access_token:
        raise ProviderError(tr("error.signInExpired", "Sign-in expired for %1. Sign in again.", "Muse Code"), kind="auth_expired")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-api-version": "1.0.0",
    }

    resp = send_mapped(lambda: ctx.http.post(KEY_URL, headers=headers, json_body={"onboard": False}, timeout=15.0))

    raise_for_http_error(resp, "Muse Code")

    payload = read_json(resp, "Muse Code", expect=None, invalid_message=tr("error.invalidResponse", "%1 returned an invalid response.", "Muse Code"))

    last_known: List[MetricLine] = []
    if ctx.previous_snapshot is not None and ctx.previous_snapshot.lines:
        last_known = list(ctx.previous_snapshot.lines)
    elif isinstance(ctx.secondary_cache.get(MUSE_LAST_KNOWN_KEY), list):
        last_known = list(ctx.secondary_cache[MUSE_LAST_KNOWN_KEY])

    plan, lines, email = map_muse_usage(payload, last_known=last_known, now=ctx.now)
    ctx.secondary_cache[MUSE_LAST_KNOWN_KEY] = list(lines)

    return make_snapshot(ctx, plan=plan, lines=lines, account_label_discovered=email)
