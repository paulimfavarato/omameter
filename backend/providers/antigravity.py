"""Antigravity (Google Cloud Code) usage provider runtime and pure mapper."""

from __future__ import annotations

import math
import platform
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from backend.http_client import HTTPResponse, HTTPTransportError
from backend.i18n import tr
from backend.models import (
    MetricLine,
    MetricPeriod,
    ProviderSnapshot,
    format_iso8601,
    parse_iso8601,
)
from backend.providers.base import (
    FetchContext,
    ProviderError,
    is_auth_failure,
    make_snapshot,
    parse_retry_after,
    safe_float,
)
from backend.providers.session import OAuthRefresh, TokenRejected, with_fresh_token

# Public OAuth client of the Antigravity desktop app (a Google "installed
# application" client). Its client secret ships inside every copy of that app,
# so it is not confidential: Google documents that installed-app secrets can't
# be kept secret. Sign-in is protected by PKCE and the loopback redirect, and
# each user still signs in and consents with their own Google account; this
# value alone grants access to nothing. It is not an Omameter credential.
ANTIGRAVITY_CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
ANTIGRAVITY_CLIENT_SECRET = "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"
ANTIGRAVITY_TOKEN_URL = "https://oauth2.googleapis.com/token"

CLOUD_CODE_URLS = (
    "https://cloudcode-pa.googleapis.com",
    "https://daily-cloudcode-pa.googleapis.com",
)
QUOTA_SUMMARY_URLS = tuple(reversed(CLOUD_CODE_URLS))
LOAD_CODE_ASSIST_PATH = "/v1internal:loadCodeAssist"
QUOTA_SUMMARY_PATH = "/v1internal:retrieveUserQuotaSummary"
LOAD_CODE_ASSIST_PAYLOAD = {
    "metadata": {
        "ideType": "ANTIGRAVITY",
        "platform": "PLATFORM_UNSPECIFIED",
        "pluginType": "GEMINI",
    }
}
ARGON_GROUP_NAME = "Gemini 4 Argon"

PAID_TIER_PLANS = {
    "g1-pro-tier": "Google AI Pro",
    "g1-ultra-tier": "Google AI Ultra",
    "g1-ultra-lite-tier": "Google AI Ultra Lite",
}

# (bucket_id, key, title, resource_key, period_ms, session_start_signal)
SUMMARY_BUCKETS: Tuple[Tuple[str, str, str, str, int, Optional[str]], ...] = (
    ("gemini-5h", "geminiPro", "Gemini 5h", "geminiSession", MetricPeriod.SESSION_MS, "zero_usage"),
    ("gemini-weekly", "geminiWeekly", "Gemini", "geminiWeekly", MetricPeriod.WEEK_MS, None),
    ("3p-5h", "claude", "Claude 5h", "nonGeminiSession", MetricPeriod.SESSION_MS, "zero_usage"),
    ("3p-weekly", "claudeWeekly", "Claude", "nonGeminiWeekly", MetricPeriod.WEEK_MS, None),
)
_SUMMARY_BUCKET_IDS = {b[0] for b in SUMMARY_BUCKETS}

def _client_user_agent() -> str:
    machine = platform.machine().lower()
    arch = "arm64" if ("arm" in machine or "aarch64" in machine) else "amd64"
    return f"antigravity/hub/2.24.0 (aidev_client; os_type=darwin; arch={arch})"


def _cloud_code_headers(token: str) -> Dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": _client_user_agent(),
    }


def _google_token_expiry(payload: Dict[str, Any], access_token: str, now: datetime) -> datetime:
    """Google omits `expires_in` sometimes; its access tokens last an hour."""
    return now + timedelta(seconds=safe_float(payload.get("expires_in"), 3600.0) or 3600.0)


OAUTH = OAuthRefresh(
    provider_name="Antigravity",
    token_url=ANTIGRAVITY_TOKEN_URL,
    client_id=ANTIGRAVITY_CLIENT_ID,
    form=True,
    extra_fields={"client_secret": ANTIGRAVITY_CLIENT_SECRET},
    expiry=_google_token_expiry,
)


def format_antigravity_plan(raw: Optional[str]) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    trimmed = raw.strip()
    if not trimmed:
        return None
    low = trimmed.lower()
    for tier in ("Ultra Lite", "Ultra", "Pro"):
        if tier.lower() in low:
            return f"Google AI {tier}"
    words = [w for w in trimmed.split() if w]
    return " ".join(w[0].upper() + w[1:] for w in words)


def parse_load_code_assist_plan(data: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    paid = data.get("paidTier") if isinstance(data.get("paidTier"), dict) else None
    curr = data.get("currentTier") if isinstance(data.get("currentTier"), dict) else None
    if paid and isinstance(paid.get("id"), str) and paid["id"] in PAID_TIER_PLANS:
        return PAID_TIER_PLANS[paid["id"]]
    raw_name = (paid.get("name") if paid else None) or (curr.get("name") if curr else None)
    return format_antigravity_plan(raw_name if isinstance(raw_name, str) else None)


def parse_load_code_assist_project(data: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    proj = data.get("cloudaicompanionProject")
    if isinstance(proj, str) and proj.strip():
        return proj.strip()
    return None


def _extract_groups(body: Any) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(body, dict):
        return None
    resp_obj = body.get("response")
    if isinstance(resp_obj, dict) and isinstance(resp_obj.get("groups"), list):
        return [g for g in resp_obj["groups"] if isinstance(g, dict)]
    if isinstance(body.get("groups"), list):
        return [g for g in body["groups"] if isinstance(g, dict)]
    return None


def is_complete_summary(body: Any) -> bool:
    groups = _extract_groups(body)
    if groups is None:
        return False
    usable: set[str] = set()
    for grp in groups:
        if grp.get("displayName") == ARGON_GROUP_NAME:
            continue
        raw_buckets = grp.get("buckets")
        if not isinstance(raw_buckets, list):
            continue
        for b in raw_buckets:
            if not isinstance(b, dict):
                continue
            bid = b.get("bucketId")
            frac = safe_float(b.get("remainingFraction"))
            if isinstance(bid, str) and frac is not None and math.isfinite(frac):
                usable.add(bid)
    return all(bid in usable for bid in _SUMMARY_BUCKET_IDS)


def _percent_line(
    *,
    key: str,
    title: str,
    resource_key: str,
    fraction: float,
    reset_time: Optional[str],
    period_ms: int,
    session_signal: Optional[str] = None,
) -> MetricLine:
    clamped = max(0.0, min(1.0, fraction))
    used = (1.0 - clamped) * 100.0
    reset_dt = parse_iso8601(reset_time) if isinstance(reset_time, str) else None
    return MetricLine(
        key=key,
        title=title,
        line_type="progress",
        kind="percent",
        used=used,
        limit=100.0,
        resets_at=format_iso8601(reset_dt),
        period_duration_ms=period_ms,
        session_start_signal=session_signal,
        resource_key=resource_key,
        resource_kind="consumption",
        resource_unit="percent",
    )


def _argon_weekly_line(groups: List[Dict[str, Any]]) -> Optional[MetricLine]:
    argon_groups = [g for g in groups if g.get("displayName") == ARGON_GROUP_NAME]
    if not argon_groups:
        return None
    buckets: List[Dict[str, Any]] = []
    for g in argon_groups:
        if isinstance(g.get("buckets"), list):
            buckets.extend(b for b in g["buckets"] if isinstance(b, dict))
    weekly = [b for b in buckets if b.get("window") == "weekly"]
    if len(weekly) != 1:
        return None
    bucket = weekly[0]
    bid = bucket.get("bucketId")
    if not isinstance(bid, str) or not bid.strip() or bid in _SUMMARY_BUCKET_IDS:
        return None
    if "disabled" in bucket and bucket["disabled"] is not None:
        if not isinstance(bucket["disabled"], bool):
            return None
        if bucket["disabled"] is True:
            return None
    frac = safe_float(bucket.get("remainingFraction"))
    if frac is None or not math.isfinite(frac):
        return None
    return _percent_line(
        key="argonWeekly",
        title="Argon",
        resource_key="argonWeekly",
        fraction=frac,
        reset_time=bucket.get("resetTime") if isinstance(bucket.get("resetTime"), str) else None,
        period_ms=MetricPeriod.WEEK_MS,
    )


def map_antigravity_quota(
    bodies: List[Dict[str, Any]],
    *,
    code_assist: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], List[MetricLine]]:
    """Pure mapper from Antigravity quota summary JSON bodies to (plan, MetricLines)."""
    group_lists: List[List[Dict[str, Any]]] = []
    for b in bodies:
        grps = _extract_groups(b)
        if grps is not None:
            group_lists.append(grps)
    if not group_lists:
        raise ProviderError(tr("provider.antigravity.invalidQuotaSummary", "Antigravity returned an invalid quota summary response."), kind="refused")

    pooled: Dict[str, Tuple[float, Optional[str]]] = {}
    for groups in group_lists:
        for grp in groups:
            if grp.get("displayName") == ARGON_GROUP_NAME:
                continue
            raw_buckets = grp.get("buckets")
            if not isinstance(raw_buckets, list):
                continue
            for bucket in raw_buckets:
                if not isinstance(bucket, dict):
                    continue
                bid = bucket.get("bucketId")
                if not isinstance(bid, str) or bid not in _SUMMARY_BUCKET_IDS:
                    continue
                if bid in pooled:
                    continue
                frac = safe_float(bucket.get("remainingFraction"))
                if frac is None or not math.isfinite(frac):
                    continue
                rtime = bucket.get("resetTime") if isinstance(bucket.get("resetTime"), str) else None
                pooled[bid] = (frac, rtime)

    lines: List[MetricLine] = []
    for bid, key, title, rkey, pms, sig in SUMMARY_BUCKETS:
        if bid not in pooled:
            continue
        frac, rtime = pooled[bid]
        lines.append(
            _percent_line(
                key=key,
                title=title,
                resource_key=rkey,
                fraction=frac,
                reset_time=rtime,
                period_ms=pms,
                session_signal=sig,
            )
        )

    argon_line: Optional[MetricLine] = None
    for groups in group_lists:
        argon_line = _argon_weekly_line(groups)
        if argon_line is not None:
            break
    if argon_line is not None:
        insert_idx = len(lines)
        for idx, line in enumerate(lines):
            if line.key in ("claude", "claudeWeekly"):
                insert_idx = idx
                break
        lines.insert(insert_idx, argon_line)

    plan = parse_load_code_assist_plan(code_assist)
    return plan, lines


# Secondary read owned by this provider (see backend/secondary.py)
ANTIGRAVITY_CODE_ASSIST_KEY = "antigravity_code_assist"  # value: loadCodeAssist body


def _requires_subscription(resp: HTTPResponse) -> bool:
    if resp.status != 403:
        return False
    try:
        data = resp.json()
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    err = data.get("error")
    details = err.get("details") if isinstance(err, dict) else None
    if not isinstance(details, list):
        return False
    return any(isinstance(d, dict) and d.get("reason") == "SUBSCRIPTION_REQUIRED" for d in details)


def _fetch_load_code_assist(ctx: FetchContext, token: str) -> Optional[Dict[str, Any]]:
    for base in CLOUD_CODE_URLS:
        url = base + LOAD_CODE_ASSIST_PATH
        try:
            resp = ctx.http.post(
                url,
                headers=_cloud_code_headers(token),
                json_body=LOAD_CODE_ASSIST_PAYLOAD,
                timeout=15.0,
            )
        except HTTPTransportError:
            continue
        if is_auth_failure(resp):
            return None
        if 200 <= resp.status < 300:
            try:
                data = resp.json()
                if isinstance(data, dict):
                    return data
            except Exception:
                continue
    return None


def _fetch_quota_summaries(ctx: FetchContext, token: str, project: Optional[str]) -> List[Dict[str, Any]]:
    payload: Dict[str, Any] = {"project": project} if project else {}
    responses: List[HTTPResponse] = []
    last_transport_err: Optional[HTTPTransportError] = None

    for idx, base in enumerate(QUOTA_SUMMARY_URLS):
        url = base + QUOTA_SUMMARY_PATH
        try:
            resp = ctx.http.post(
                url,
                headers=_cloud_code_headers(token),
                json_body=payload,
                timeout=15.0,
            )
        except HTTPTransportError as exc:
            last_transport_err = exc
            continue
        responses.append(resp)
        if idx == 0 and 200 <= resp.status < 300:
            try:
                parsed = resp.json()
                if is_complete_summary(parsed):
                    break
            except Exception:
                pass

    if not responses and last_transport_err is not None:
        raise ProviderError(
            str(last_transport_err),
            kind="offline" if last_transport_err.offline else "unreachable",
        ) from last_transport_err

    ok_bodies: List[Dict[str, Any]] = []
    for r in responses:
        if 200 <= r.status < 300:
            try:
                parsed = r.json()
                if isinstance(parsed, dict):
                    ok_bodies.append(parsed)
            except Exception:
                pass
    if ok_bodies:
        return ok_bodies

    if any(r.status == 401 for r in responses):
        raise TokenRejected()
    if any(_requires_subscription(r) for r in responses):
        msg = (
            tr(
                "provider.antigravity.noProjectAccess",
                "Antigravity couldn't find a project with quota access for this account. Try again shortly.",
            )
            if project is None
            else tr("provider.antigravity.noQuotaSubscription", "Google reports no Antigravity quota subscription for this project.")
        )
        raise ProviderError(msg, kind="no_quota", status_code=403)
    if any(r.status == 403 and is_auth_failure(r) for r in responses):
        raise ProviderError(tr("error.signInExpired", "Sign-in expired for %1. Sign in again.", "Antigravity"), kind="auth_expired", status_code=403)
    for r in responses:
        if r.status == 429:
            raise ProviderError(
                tr("error.rateLimited", "%1 rate limited the request.", "Antigravity"),
                kind="rate_limited",
                retry_after=parse_retry_after(r),
                status_code=429,
            )
    raise ProviderError(
        tr("provider.antigravity.unavailable", "Antigravity usage is temporarily unavailable. Try again shortly."),
        kind="refused",
    )


def _run_antigravity_read(ctx: FetchContext, token: str) -> ProviderSnapshot:
    code_assist = ctx.secondary_cache.read(
        ANTIGRAVITY_CODE_ASSIST_KEY,
        ctx.now,
        lambda: _fetch_load_code_assist(ctx, token),
        force=ctx.force,
    )
    project = parse_load_code_assist_project(code_assist)
    bodies = _fetch_quota_summaries(ctx, token, project)
    plan, lines = map_antigravity_quota(bodies, code_assist=code_assist)
    return make_snapshot(ctx, plan=plan, lines=lines)


def fetch(ctx: FetchContext) -> ProviderSnapshot:
    """Fetch live Antigravity quota summary with Google OAuth refresh and 401 retry."""
    return with_fresh_token(ctx, OAUTH, lambda token: _run_antigravity_read(ctx, token))
