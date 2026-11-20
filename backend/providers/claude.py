"""Claude (Anthropic) usage provider runtime and pure mapper."""

from __future__ import annotations

import base64
import math
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from backend.backoff import FORCED_RESET_GRANTS_MIN_AGE_SEC, SECONDARY_READ_INTERVAL_SEC
from backend.http_client import HTTPResponse, HTTPTransportError
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
    FetchContext,
    ProviderError,
    make_snapshot,
    oauth_error_code,
    parse_retry_after,
    raise_for_http_error,
    read_json,
    safe_float,
)
from backend.providers.session import OAuthRefresh, authorized
from backend.secondary import SecondaryCache

CLAUDE_CLIENT_ID = base64.b64decode("OWQxYzI1MGEtZTYxYi00NGQ5LTg4ZWQtNTk0NGQxOTYyZjVl").decode("utf-8")
CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_SCOPES = "user:profile user:inference"
CLAUDE_PROFILE_ONLY_SCOPES = "user:profile"

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_RESET_GRANTS_URL = "https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1"
CLAUDE_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
CLAUDE_CODE_VERSION = "2.1.294"

_RESET_KIND_TITLES = {
    "session": "Session Reset",
    "weekly": "Weekly Reset",
    "fable": "Fable Reset",
    "full": "Full Reset",
}


def claude_code_headers(access_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token.strip()}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": f"claude-cli/{CLAUDE_CODE_VERSION} (external, cli)",
    }


def _profile_headers(access_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token.strip()}",
        "Accept": "application/json",
        "anthropic-beta": "oauth-2025-04-20",
    }


def _narrow_scope_on_invalid_scope(resp: HTTPResponse) -> Optional[Dict[str, str]]:
    if resp.status == 400 and oauth_error_code(resp) == "invalid_scope":
        return {"scope": CLAUDE_PROFILE_ONLY_SCOPES}
    return None


OAUTH = OAuthRefresh(
    provider_name="Claude",
    token_url=CLAUDE_TOKEN_URL,
    client_id=CLAUDE_CLIENT_ID,
    extra_fields={"scope": CLAUDE_SCOPES},
    fallback_ttl_sec=3600.0,
    retry_fields=_narrow_scope_on_invalid_scope,
)


def parse_reset_date(value: Any) -> Optional[datetime]:
    """Parse ISO-8601 string or epoch seconds/milliseconds into UTC datetime."""
    if isinstance(value, str):
        text = value.strip()
        if text:
            dt = parse_iso8601(text)
            if dt is not None:
                return dt
    num = safe_float(value)
    if num is None or not math.isfinite(num):
        return None
    secs = num if abs(num) < 1e10 else num / 1000.0
    if secs <= 0:
        return None
    return parse_iso8601(secs)


def format_claude_plan(profile: Optional[Dict[str, Any]]) -> Optional[str]:
    """Port of ClaudeUsageMapper.formatLivePlan(profile:)."""
    if not isinstance(profile, dict):
        return None
    org = profile.get("organization")
    if not isinstance(org, dict):
        return None
    org_type = org.get("organization_type")
    if not isinstance(org_type, str):
        return None
    raw = org_type[len("claude_"):] if org_type.startswith("claude_") else org_type
    raw = raw.strip()
    if not raw:
        return None

    # Title-case words split on space (matching Swift titleCased(separator: { $0 == " " }, lowercasingTail: true))
    words = [w for w in raw.split(" ") if w]
    base = " ".join(w[0].upper() + w[1:].lower() for w in words)
    tier = org.get("rate_limit_tier") if isinstance(org.get("rate_limit_tier"), str) else ""

    if base == "Ultra" or (base == "Max" and ("ultra" in tier or "20x" in tier)):
        return "Ultra"
    if base == "Max":
        return base

    match = re.search(r"\d+x", tier)
    if match:
        return f"{base} {match.group(0)}"
    return base


def _grant_kind(grant: Dict[str, Any]) -> str:
    if grant.get("gives_back_weekly") is True:
        return "fable"
    gtype = grant.get("type")
    if gtype in ("session", "weekly", "full"):
        return str(gtype)
    clears = grant.get("clears") if isinstance(grant.get("clears"), list) else []
    has_session = "five_hour" in clears
    has_other = any(c != "five_hour" for c in clears)
    if has_session and not has_other:
        return "session"
    if has_other and not has_session:
        return "weekly"
    return "full"


def _grant_confirm_detail(grant: Dict[str, Any]) -> Optional[str]:
    clears = grant.get("clears") if isinstance(grant.get("clears"), list) else []
    names: List[str] = []
    for window in clears:
        if window == "five_hour":
            names.append("session")
        elif window == "seven_day":
            names.append("weekly")
        elif window == "seven_day_overage_included":
            names.append("Fable")
        else:
            return None
    if not names:
        return None
    if len(names) == 1:
        detail = f"Refills your {names[0]} limit now"
    else:
        detail = f"Refills your {', '.join(names[:-1])} and {names[-1]} limits now"
    if grant.get("gives_back_weekly") is True and "seven_day" not in clears:
        detail += " and gives back what Fable used of your weekly limit"
    return detail + ". This can't be undone."


def parse_claude_reset_grants(cedar_ember: Any) -> Optional[Dict[str, Any]]:
    """Parse the `cedar_ember` block into a normalized status dict."""
    if not isinstance(cedar_ember, dict):
        return None
    raw_grants = cedar_ember.get("grants")
    if raw_grants is None:
        raw_grants = []
    if not isinstance(raw_grants, list):
        return None

    grants: List[Dict[str, Any]] = []
    for entry in raw_grants:
        if not isinstance(entry, dict):
            return None
        gid = entry.get("id")
        left_num = safe_float(entry.get("resets_left"))
        if not isinstance(gid, str) or not gid or left_num is None or not math.isfinite(left_num):
            return None
        resets_left = max(0, int(math.floor(left_num)))
        ends_at_dt = parse_reset_date(entry.get("ends_at"))
        clears = [str(c) for c in entry.get("clears", []) if isinstance(c, str)] if isinstance(entry.get("clears"), list) else []
        grants.append({
            "id": gid,
            "resets_left": resets_left,
            "ends_at": format_iso8601(ends_at_dt),
            "clears": clears,
            "type": entry.get("type") if isinstance(entry.get("type"), str) else None,
            "gives_back_weekly": entry.get("gives_back_weekly") is True,
            "usable_now": entry.get("usable_now") is True,
            "paused": entry.get("paused") is True,
        })

    next_gid = cedar_ember.get("next_grant_id")
    if not isinstance(next_gid, str) or not next_gid:
        next_gid = None

    return {
        "eligible": cedar_ember.get("eligible") is True,
        "grants": grants,
        "next_grant_id": next_gid,
    }


def build_claude_resets_line(status: Dict[str, Any], now: Optional[datetime] = None) -> MetricLine:
    """Build the `Rate Limit Resets` MetricLine and ResetItems from parsed grant status."""
    now = now or utc_now()
    eligible = status.get("eligible") is True
    grants = status.get("grants") if isinstance(status.get("grants"), list) else []
    next_grant_id = status.get("next_grant_id")

    next_grant_kind: Optional[str] = None
    if next_grant_id:
        for g in grants:
            if g.get("id") == next_grant_id:
                next_grant_kind = _grant_kind(g)
                break

    total_count = 0
    expiries: List[str] = []
    items: List[ResetItem] = []

    if eligible:
        for grant in grants:
            resets_left = int(grant.get("resets_left") or 0)
            if resets_left < 1:
                continue
            ends_at_str = grant.get("ends_at")
            ends_at_dt = parse_iso8601(ends_at_str) if ends_at_str else None
            if ends_at_dt is not None and ends_at_dt <= now:
                continue

            total_count += resets_left
            kind = _grant_kind(grant)
            title = _RESET_KIND_TITLES.get(kind, "Full Reset")
            usable = bool(grant.get("usable_now")) and not bool(grant.get("paused"))
            confirm_detail = _grant_confirm_detail(grant)
            gid = str(grant["id"])

            if ends_at_str:
                expiries.extend([ends_at_str] * resets_left)
                for idx in range(resets_left):
                    items.append(
                        ResetItem(
                            id=f"{gid}:{idx}",
                            provider_kind="claude",
                            kind=kind,
                            title=title,
                            expires_at=ends_at_str,
                            usable_now=usable,
                            claim_payload={
                                "grant_id": gid,
                                "kind": kind,
                                "next_grant_id": next_grant_id,
                                "next_grant_kind": next_grant_kind,
                                "confirm_detail": confirm_detail,
                            },
                            unavailable_reason="Paused" if grant.get("paused") else (None if usable else "Not usable right now"),
                        )
                    )

    expiries.sort()
    items.sort(key=lambda r: r.sort_key())

    return MetricLine(
        key="rateLimitResets",
        title="Limit Resets",
        line_type="values",
        kind="count",
        values=[MetricValue(number=float(total_count), kind="count", label="available")],
        expiries_at=expiries,
        reset_items=items,
        resource_key="rateLimitResets",
        resource_kind="balance",
        resource_unit="resets",
    )


def map_claude_usage(
    body: Dict[str, Any],
    *,
    plan: Optional[str] = None,
    reset_grants_status: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> List[MetricLine]:
    """Pure mapper from Claude `/api/oauth/usage` JSON to MetricLines."""
    lines: List[MetricLine] = []

    def _append_window(
        key: str,
        title: str,
        resource_key: str,
        obj: Any,
        period_ms: int,
        session_signal: Optional[str] = None,
    ) -> bool:
        if not isinstance(obj, dict):
            return False
        used = safe_float(obj.get("utilization"))
        if used is None or not math.isfinite(used):
            return False
        resets_dt = parse_reset_date(obj.get("resets_at"))
        lines.append(
            MetricLine(
                key=key,
                title=title,
                line_type="progress",
                kind="percent",
                used=used,
                limit=100.0,
                resets_at=format_iso8601(resets_dt),
                period_duration_ms=period_ms,
                session_start_signal=session_signal,
                resource_key=resource_key,
                resource_kind="consumption",
                resource_unit="percent",
            )
        )
        return True

    has_session = _append_window("session", "Session", "session", body.get("five_hour"), MetricPeriod.SESSION_MS, "missing_reset_date")
    has_weekly = _append_window("weekly", "Weekly", "weekly", body.get("seven_day"), MetricPeriod.WEEK_MS)
    if has_session and not has_weekly:
        lines.append(
            MetricLine(
                key="weekly",
                title="Weekly",
                line_type="badge",
                kind="count",
                badge_text="Not reported",
                resource_key="weekly",
            )
        )
    elif has_weekly and not has_session:
        lines.insert(
            0,
            MetricLine(
                key="session",
                title="Session",
                line_type="badge",
                kind="count",
                badge_text="Not reported",
                resource_key="session",
            ),
        )
    _append_window("sonnet", "Sonnet", "sonnet", body.get("seven_day_sonnet"), MetricPeriod.WEEK_MS)

    # Scoped weekly model limit: Fable
    raw_limits = body.get("limits")
    if isinstance(raw_limits, list):
        for entry in raw_limits:
            if not isinstance(entry, dict) or entry.get("kind") != "weekly_scoped":
                continue
            scope = entry.get("scope")
            model = scope.get("model") if isinstance(scope, dict) else None
            if not isinstance(model, dict) or model.get("display_name") != "Fable":
                continue
            used = safe_float(entry.get("percent"))
            if used is None or not math.isfinite(used):
                continue
            resets_dt = parse_reset_date(entry.get("resets_at"))
            lines.append(
                MetricLine(
                    key="fable",
                    title="Fable",
                    line_type="progress",
                    kind="percent",
                    used=used,
                    limit=100.0,
                    resets_at=format_iso8601(resets_dt),
                    period_duration_ms=MetricPeriod.WEEK_MS,
                    resource_key="fable",
                    resource_kind="consumption",
                    resource_unit="percent",
                )
            )
            break

    # Extra usage
    extra = body.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled") is True:
        currency = extra.get("currency") if isinstance(extra.get("currency"), str) and extra.get("currency") else "USD"
        dec_places = safe_float(extra.get("decimal_places"), 2.0)
        minor_units = 10.0 ** (dec_places if dec_places is not None else 2.0)
        used_credits = safe_float(extra.get("used_credits"), 0.0) or 0.0
        used = round(used_credits) / minor_units
        title = "Spend" if (plan and plan.startswith("Enterprise")) else "Extra Usage"
        limit_minor = safe_float(extra.get("monthly_limit"))
        if limit_minor is not None and limit_minor > 0:
            lines.append(
                MetricLine(
                    key="extra",
                    title=title,
                    line_type="progress",
                    kind="dollars",
                    used=used,
                    limit=round(limit_minor) / minor_units,
                    currency_code=currency,
                    resource_key="extraUsage",
                    resource_kind="consumption",
                    resource_unit=currency.lower(),
                )
            )
        else:
            lines.append(
                MetricLine(
                    key="extra",
                    title=title,
                    line_type="values",
                    kind="dollars",
                    currency_code=currency,
                    subtitle="spent",
                    values=[MetricValue(number=used, kind="dollars", currency_code=currency)],
                    resource_key="extraUsage",
                    resource_kind="consumption",
                    resource_unit=currency.lower(),
                )
            )

    if reset_grants_status is not None:
        lines.append(build_claude_resets_line(reset_grants_status, now=now))

    return lines


# ---------------------------------------------------------------------------
# Secondary reads owned by this provider (see backend/secondary.py)
# ---------------------------------------------------------------------------

CLAUDE_PLAN_KEY = "claude_plan"                  # value: {"plan", "orgUuid", "email"}
CLAUDE_RESET_GRANTS_KEY = "claude_reset_grants"  # value: {"status": parsed cedar_ember | None}
CLAUDE_RATE_LIMIT_UNTIL_KEY = "claude_rate_limit_until"  # raw ISO timestamp


def reset_grants_status(cache: SecondaryCache) -> Optional[Dict[str, Any]]:
    """Last parsed reset-grant status, or None when never read / not offered."""
    value = cache.value(CLAUDE_RESET_GRANTS_KEY)
    status = value.get("status") if isinstance(value, dict) else None
    return status if isinstance(status, dict) else None


def record_reset_grants(cache: SecondaryCache, status: Optional[Dict[str, Any]], now: datetime) -> None:
    cache[CLAUDE_RESET_GRANTS_KEY] = {"readAt": format_iso8601(now), "value": {"status": status}}


def invalidate_reset_grants(cache: SecondaryCache) -> None:
    """Make the next refresh re-read grants even inside the forced minimum age."""
    cache.invalidate(CLAUDE_RESET_GRANTS_KEY)


def cached_org_uuid(cache: SecondaryCache) -> Optional[str]:
    value = cache.value(CLAUDE_PLAN_KEY)
    org_uuid = value.get("orgUuid") if isinstance(value, dict) else None
    return org_uuid if isinstance(org_uuid, str) else None


def remember_org_uuid(cache: SecondaryCache, org_uuid: str) -> None:
    """Store the organization UUID without marking the plan read as fresh."""
    entry = cache.get(CLAUDE_PLAN_KEY)
    if not isinstance(entry, dict):
        entry = {"readAt": None, "value": None}
        cache[CLAUDE_PLAN_KEY] = entry
    if not isinstance(entry.get("value"), dict):
        entry["value"] = {}
    entry["value"]["orgUuid"] = org_uuid


def _rate_limited_until(cache: SecondaryCache) -> Optional[datetime]:
    return parse_iso8601(cache.get(CLAUDE_RATE_LIMIT_UNTIL_KEY))


def _remember_rate_limit(ctx: FetchContext, retry_after: Optional[float]) -> None:
    if retry_after:
        ctx.secondary_cache[CLAUDE_RATE_LIMIT_UNTIL_KEY] = format_iso8601(ctx.now + timedelta(seconds=retry_after))


def fetch_claude_profile(ctx: FetchContext, access_token: str) -> Optional[Dict[str, Any]]:
    """Fetch `/api/oauth/profile` and cache plan, organization UUID, and email."""
    try:
        resp = ctx.http.get(CLAUDE_PROFILE_URL, headers=_profile_headers(access_token), timeout=10.0)
    except HTTPTransportError:
        return None
    if resp.status in (401, 403):
        return None
    if not (200 <= resp.status < 300):
        return None
    try:
        profile = resp.json()
    except Exception:
        return None
    if not isinstance(profile, dict):
        return None
    return profile


def fetch(ctx: FetchContext) -> ProviderSnapshot:
    """Fetch live Claude usage, hourly reset grants, and hourly plan/profile."""
    cache = ctx.secondary_cache
    # Honor per-account 429 cooldown window if cached
    rl_until = _rate_limited_until(cache)
    if rl_until is not None and ctx.now < rl_until:
        wait = max(1.0, (rl_until - ctx.now).total_seconds())
        raise ProviderError(tr("error.rateLimited", "%1 rate limited the request.", "Claude"), kind="rate_limited", retry_after=wait, status_code=429)

    cred, resp = authorized(
        ctx, OAUTH, lambda token: ctx.http.get(CLAUDE_USAGE_URL, headers=claude_code_headers(token), timeout=10.0)
    )
    if resp.status == 429:
        _remember_rate_limit(ctx, parse_retry_after(resp))
        raise_for_http_error(resp, "Claude")
    if resp.status == 403:
        raise ProviderError(
            tr("error.signInExpired", "Sign-in expired for %1. Sign in again.", "Claude"),
            kind="auth_expired",
            status_code=403,
        )
    raise_for_http_error(resp, "Claude")
    cache.pop(CLAUDE_RATE_LIMIT_UNTIL_KEY, None)

    body = read_json(resp, "Claude")
    access_token = str(cred.get("accessToken") or "")

    # Reset grants (at most once an hour; on force refresh, at most once per 15 min).
    # A 2xx always replaces the status (even with "not offered"); anything else keeps it.
    def _read_grants() -> Optional[Dict[str, Any]]:
        grants_resp = ctx.http.get(CLAUDE_RESET_GRANTS_URL, headers=claude_code_headers(access_token), timeout=10.0)
        if 200 <= grants_resp.status < 300:
            grants_json = grants_resp.json()
            return {"status": parse_claude_reset_grants(grants_json.get("cedar_ember") if isinstance(grants_json, dict) else None)}
        if grants_resp.status == 429:
            _remember_rate_limit(ctx, parse_retry_after(grants_resp))
        return None

    cache.read(
        CLAUDE_RESET_GRANTS_KEY,
        ctx.now,
        _read_grants,
        interval_sec=FORCED_RESET_GRANTS_MIN_AGE_SEC if ctx.force else SECONDARY_READ_INTERVAL_SEC,
    )
    grants_status = reset_grants_status(cache)

    # Profile / Plan read (hourly, or forced), skipped while rate limited
    def _read_profile() -> Optional[Dict[str, Any]]:
        profile = fetch_claude_profile(ctx, access_token)
        if profile is None:
            return None
        org = profile.get("organization") if isinstance(profile.get("organization"), dict) else {}
        acc = profile.get("account") if isinstance(profile.get("account"), dict) else {}
        return {
            "plan": format_claude_plan(profile),
            "orgUuid": org.get("uuid") if isinstance(org.get("uuid"), str) else None,
            "email": acc.get("email") if isinstance(acc.get("email"), str) else (profile.get("email") if isinstance(profile.get("email"), str) else None),
        }

    rl_active = _rate_limited_until(cache)
    if not (rl_active is not None and ctx.now < rl_active):
        cache.read(CLAUDE_PLAN_KEY, ctx.now, _read_profile, force=ctx.force)

    plan_entry = cache.value(CLAUDE_PLAN_KEY)
    if not isinstance(plan_entry, dict):
        plan_entry = {}
    plan = plan_entry.get("plan") or (ctx.previous_snapshot.plan if ctx.previous_snapshot else None)
    email_discovered = plan_entry.get("email")

    lines = map_claude_usage(
        body,
        plan=plan,
        reset_grants_status=grants_status,
        now=ctx.now,
    )

    return make_snapshot(
        ctx,
        plan=plan,
        lines=lines,
        account_label_discovered=email_discovered,
    )
