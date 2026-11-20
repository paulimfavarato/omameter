"""Codex (ChatGPT) usage provider runtime, analytics history, and pure mapper."""

from __future__ import annotations

import math
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from backend.http_client import HTTPResponse, HTTPTransportError
from backend.models import (
    MetricLine,
    MetricPeriod,
    MetricValue,
    ProviderSnapshot,
    ResetItem,
    UsageBreakdown,
    UsageBreakdownModel,
    format_iso8601,
    parse_iso8601,
    utc_now,
)
from backend.jwt_claims import decode_jwt_payload
from backend.providers.base import (
    FetchContext,
    make_snapshot,
    parse_retry_after,
    raise_for_auth_failure,
    raise_for_http_error,
    read_json,
    safe_float,
)
from backend.providers.session import OAuthRefresh, authorized, jwt_or_expires_in_expiry
from backend.secondary import SecondaryCache

CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"

CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_RESET_CREDITS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
CODEX_CONSUME_RESET_CREDIT_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume"
CODEX_REPORTS_BASE_URL = "https://chatgpt.com/backend-api/wham/"

CREDIT_USD_RATE = 0.04

_TEAM_CREDIT_PLANS = {
    "team",
    "self_serve_business_prolite",
    "self_serve_business_usage_based",
    "sci",
    "sci_plus",
}
_ENTERPRISE_CREDIT_PLANS = {
    "business",
    "ent26",
    "enterprise_cbp_automation",
    "enterprise_cbp_usage_based",
    "enterprise",
    "edu",
    "edu_plus",
    "edu_pro",
}


def extract_chatgpt_account_id(credential: Dict[str, Any]) -> Optional[str]:
    extras = credential.get("extras")
    if isinstance(extras, dict):
        acc_id = extras.get("chatgpt_account_id")
        if isinstance(acc_id, str) and acc_id:
            return acc_id
    direct = credential.get("chatgpt_account_id")
    if isinstance(direct, str) and direct:
        return direct
    payload = decode_jwt_payload(str(credential.get("accessToken") or ""))
    if payload:
        auth = payload.get("https://api.openai.com/auth")
        if isinstance(auth, dict) and isinstance(auth.get("chatgpt_account_id"), str):
            return auth["chatgpt_account_id"]
    return None


def codex_usage_headers(access_token: str, account_id: Optional[str] = None) -> Dict[str, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "Omameter",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    return headers


def codex_reset_credit_headers(access_token: str, account_id: Optional[str] = None) -> Dict[str, str]:
    headers = codex_usage_headers(access_token, account_id)
    headers["OpenAI-Beta"] = "codex-1"
    headers["originator"] = "Codex Desktop"
    return headers


OAUTH = OAuthRefresh(
    provider_name="ChatGPT",
    token_url=CODEX_TOKEN_URL,
    client_id=CODEX_CLIENT_ID,
    form=True,
    expiry=jwt_or_expires_in_expiry,
    fallback_ttl_sec=3600.0,
)


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


def format_codex_plan(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    low = raw.lower()
    if low == "prolite":
        return "Pro 100"
    if low == "pro":
        return "Pro 200"
    if low == "promax":
        return "Pro 500"
    if low == "self_serve_business_prolite":
        return "Business Premium"
    parts = [p for p in raw.split("_") if p]
    return " ".join(p[0].upper() + p[1:] for p in parts)


def _window_reset_date(window: Optional[Dict[str, Any]], now: datetime) -> Optional[datetime]:
    if not isinstance(window, dict):
        return None
    reset_at = safe_float(window.get("reset_at"))
    if reset_at is not None and math.isfinite(reset_at):
        return datetime.fromtimestamp(reset_at, tz=timezone.utc)
    reset_after = safe_float(window.get("reset_after_seconds"))
    if reset_after is not None and math.isfinite(reset_after):
        return now + timedelta(seconds=reset_after)
    return None


def _window_period_ms(window: Optional[Dict[str, Any]]) -> Optional[int]:
    if not isinstance(window, dict):
        return None
    secs = safe_float(window.get("limit_window_seconds"))
    if secs is None or not math.isfinite(secs):
        return None
    return int(round(secs * 1000.0))


def _map_window_lines(
    rate_limit: Optional[Dict[str, Any]],
    *,
    session_meta: Tuple[str, str, str],  # (key, title, resource_key)
    weekly_meta: Tuple[str, str, str],
    header_primary: Optional[float] = None,
    header_secondary: Optional[float] = None,
    now: datetime,
    include_missing_sibling: bool = False,
) -> List[MetricLine]:
    raw_slots = [
        (rate_limit.get("primary_window") if isinstance(rate_limit, dict) else None, header_primary),
        (rate_limit.get("secondary_window") if isinstance(rate_limit, dict) else None, header_secondary),
    ]
    slots: List[Optional[Tuple[Dict[str, Any], Optional[float]]]] = []
    for val, hdr_pct in raw_slots:
        if isinstance(val, dict):
            used = safe_float(val.get("used_percent"))
            slots.append((val, used if used is not None else hdr_pct))
        elif hdr_pct is not None:
            slots.append(({}, hdr_pct))
        else:
            slots.append(None)

    meters = [
        (session_meta, MetricPeriod.SESSION_MS),
        (weekly_meta, MetricPeriod.WEEK_MS),
    ]
    familiar_periods = {MetricPeriod.SESSION_MS, MetricPeriod.WEEK_MS}

    lines: List[MetricLine] = []
    for slot_idx, ((key, title, resource_key), default_period_ms) in enumerate(meters):
        exact = None
        for cand in slots:
            if cand is not None and _window_period_ms(cand[0]) == default_period_ms:
                exact = cand
                break

        fallback = None
        slot_cand = slots[slot_idx]
        if slot_cand is not None:
            pms = _window_period_ms(slot_cand[0])
            if pms not in familiar_periods:
                fallback = slot_cand

        chosen = exact if exact is not None else fallback
        if chosen is None or chosen[1] is None:
            continue

        window_dict, used_pct = chosen
        period_ms = _window_period_ms(window_dict) or default_period_ms
        resets_dt = _window_reset_date(window_dict, now)
        lines.append(
            MetricLine(
                key=key,
                title=title,
                line_type="progress",
                kind="percent",
                used=used_pct,
                limit=100.0,
                resets_at=format_iso8601(resets_dt),
                period_duration_ms=period_ms,
                resource_key=resource_key,
                resource_kind="consumption",
                resource_unit="percent",
            )
        )

    if include_missing_sibling and len(lines) == 1:
        present_keys = {ln.key for ln in lines}
        s_key, s_title, s_res = session_meta
        w_key, w_title, w_res = weekly_meta
        if s_key not in present_keys:
            lines.insert(
                0,
                MetricLine(
                    key=s_key,
                    title=s_title,
                    line_type="badge",
                    kind="count",
                    badge_text="Not reported",
                    resource_key=s_res,
                ),
            )
        elif w_key not in present_keys:
            lines.append(
                MetricLine(
                    key=w_key,
                    title=w_title,
                    line_type="badge",
                    kind="count",
                    badge_text="Not reported",
                    resource_key=w_res,
                ),
            )

    return lines


def _available_count(source: Optional[Dict[str, Any]]) -> Optional[int]:
    if not isinstance(source, dict):
        return None
    count = safe_float(source.get("available_count"))
    if count is None or not math.isfinite(count) or count < 0:
        return None
    return int(math.floor(count))


def _parse_credit_expiry(value: Any) -> Optional[datetime]:
    if isinstance(value, str):
        dt = parse_iso8601(value)
        if dt is not None:
            return dt
    secs = safe_float(value)
    if secs is not None and math.isfinite(secs) and secs > 0:
        return datetime.fromtimestamp(secs, tz=timezone.utc)
    return None


def parse_available_reset_credits(source: Optional[Dict[str, Any]]) -> List[Tuple[Optional[str], Optional[datetime]]]:
    if not isinstance(source, dict):
        return []
    raw = source.get("credits")
    if not isinstance(raw, list):
        return []
    out: List[Tuple[Optional[str], Optional[datetime]]] = []
    for credit in raw:
        if not isinstance(credit, dict):
            continue
        status = credit.get("status")
        if isinstance(status, str) and status != "available":
            continue
        cid = credit.get("id") if isinstance(credit.get("id"), str) else None
        exp = _parse_credit_expiry(credit.get("expires_at"))
        out.append((cid, exp))
    return out


def find_codex_reset_credit_id(body: Dict[str, Any], target_expiry: datetime) -> Optional[str]:
    for cid, exp in parse_available_reset_credits(body):
        if cid and exp is not None and abs((exp - target_expiry).total_seconds()) < 1.0:
            return cid
    return None


def _read_reset_credits(
    body: Dict[str, Any],
    reset_credits_body: Optional[Dict[str, Any]],
) -> Optional[Tuple[int, List[Tuple[Optional[str], datetime]]]]:
    embedded = body.get("rate_limit_reset_credits")
    embedded_dict = embedded if isinstance(embedded, dict) else None
    dedicated_dict = (
        reset_credits_body
        if isinstance(reset_credits_body, dict) and _available_count(reset_credits_body) is not None
        else None
    )

    count = _available_count(embedded_dict)
    if count is None:
        count = _available_count(dedicated_dict)
    if count is None:
        return None
    if count <= 0:
        return 0, []

    if dedicated_dict is not None and _available_count(dedicated_dict) == count:
        pairs = [(cid, exp) for cid, exp in parse_available_reset_credits(dedicated_dict) if exp is not None]
        pairs.sort(key=lambda pair: pair[1])
        return count, pairs

    pairs = [(cid, exp) for cid, exp in parse_available_reset_credits(embedded_dict) if exp is not None]
    pairs.sort(key=lambda pair: pair[1])
    return count, pairs


# ---------------------------------------------------------------------------
# Codex daily analytics reports (Counts + Workspace/Enterprise Credits)
# ---------------------------------------------------------------------------

def _non_neg_float(val: Any) -> Optional[float]:
    num = safe_float(val)
    if num is None or not math.isfinite(num) or num < 0:
        return None
    return num


def _all_numbers(mapping: Any) -> Optional[Dict[str, float]]:
    if not isinstance(mapping, dict):
        return None
    out: Dict[str, float] = {}
    for k, v in mapping.items():
        num = safe_float(v)
        if num is None or not math.isfinite(num):
            return None
        out[str(k)] = num
    return out


def _add_day_record(days: Dict[str, Dict[str, Any]], date_key: str, totals: Dict[str, float], by_model: Optional[Dict[str, Dict[str, float]]]) -> None:
    if date_key not in days:
        days[date_key] = {"totals": dict(totals), "by_model": {m: dict(v) for m, v in by_model.items()} if by_model is not None else None}
        return
    existing = days[date_key]
    for k, v in totals.items():
        existing["totals"][k] = existing["totals"].get(k, 0.0) + v
    if by_model is not None:
        if existing["by_model"] is None:
            existing["by_model"] = {}
        for m, mv in by_model.items():
            target = existing["by_model"].setdefault(m, {})
            for mk, mval in mv.items():
                target[mk] = target.get(mk, 0.0) + mval


def parse_codex_counts_report(body: Any) -> Optional[Dict[str, Dict[str, Any]]]:
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        return None
    days: Dict[str, Dict[str, Any]] = {}
    token_fields = (
        ("text_total_tokens", "tokens"),
        ("uncached_text_input_tokens", "inputTokens"),
        ("cached_text_input_tokens", "cacheReadTokens"),
        ("text_output_tokens", "outputTokens"),
    )
    for rec in body["data"]:
        if not isinstance(rec, dict):
            return None
        raw_date = rec.get("date")
        raw_totals = rec.get("totals")
        if not isinstance(raw_date, str) or len(raw_date) < 10 or not isinstance(raw_totals, dict):
            return None
        date_key = raw_date[:10]
        turns = _non_neg_float(raw_totals.get("turns"))
        if turns is None:
            return None
        vals: Dict[str, float] = {"turns": turns}
        for src_key, dst_key in token_fields:
            raw_v = raw_totals.get(src_key)
            if raw_v is None:
                continue
            parsed = _non_neg_float(raw_v)
            if parsed is None:
                return None
            vals[dst_key] = parsed

        by_model: Optional[Dict[str, Dict[str, float]]] = None
        raw_models = rec.get("models")
        if isinstance(raw_models, list) and raw_models:
            by_model = {}
            for m in raw_models:
                if not isinstance(m, dict):
                    continue
                name = m.get("model") if isinstance(m.get("model"), str) else "Unknown"
                entry = by_model.setdefault(name, {})
                for src_k, dst_k in (("turns", "turns"), ("text_total_tokens", "tokens")):
                    num = _non_neg_float(m.get(src_k))
                    if num is not None:
                        entry[dst_k] = entry.get(dst_k, 0.0) + num

        _add_day_record(days, date_key, vals, by_model)
    return days


def parse_codex_team_credits_report(body: Any) -> Optional[Dict[str, Dict[str, Any]]]:
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        return None
    days: Dict[str, Dict[str, Any]] = {}
    for rec in body["data"]:
        if not isinstance(rec, dict):
            return None
        raw_date = rec.get("date")
        if not isinstance(raw_date, str) or len(raw_date) < 10:
            return None
        date_key = raw_date[:10]
        prem = rec.get("premium_usage_values")
        credit_usage = prem.get("credit_usage_credits") if isinstance(prem, dict) else None
        if not isinstance(credit_usage, dict):
            return {}
        charged = _all_numbers(credit_usage)
        if charged is None:
            return None
        total_credits = sum(charged.values())
        by_model: Dict[str, Dict[str, float]] = {}
        raw_models = rec.get("models")
        if isinstance(raw_models, list):
            for m in raw_models:
                if not isinstance(m, dict):
                    continue
                c = safe_float(m.get("on_demand_credits"))
                if c is None or not math.isfinite(c):
                    continue
                name = m.get("model") if isinstance(m.get("model"), str) else "Unknown"
                entry = by_model.setdefault(name, {})
                entry["credits"] = entry.get("credits", 0.0) + c
        _add_day_record(days, date_key, {"credits": total_credits}, by_model)
    return days


def parse_codex_enterprise_credits_report(body: Any) -> Optional[Dict[str, Dict[str, Any]]]:
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        return None
    days: Dict[str, Dict[str, Any]] = {}
    for rec in body["data"]:
        if not isinstance(rec, dict):
            return None
        raw_date = rec.get("date")
        if not isinstance(raw_date, str) or len(raw_date) < 10:
            return None
        date_key = raw_date[:10]
        raw_vals = rec.get("values")
        if not isinstance(raw_vals, dict):
            return {}
        models = _all_numbers(raw_vals)
        if models is None:
            return None
        total_credits = sum(models.values())
        by_model = {m: {"credits": v} for m, v in models.items()}
        _add_day_record(days, date_key, {"credits": total_credits}, by_model)
    return days


def combine_codex_reports(
    counts_days: Dict[str, Dict[str, Any]],
    credits_days: Optional[Dict[str, Dict[str, Any]]],
) -> Dict[str, Dict[str, Any]]:
    result = {
        d: {
            "totals": dict(v.get("totals") or {}),
            "by_model": {m: dict(mv) for m, mv in v["by_model"].items()} if isinstance(v.get("by_model"), dict) else None,
        }
        for d, v in counts_days.items()
    }
    if not credits_days:
        return result
    for date_key, cday in credits_days.items():
        if date_key not in result:
            continue
        target = result[date_key]
        for k, val in (cday.get("totals") or {}).items():
            target["totals"][k] = target["totals"].get(k, 0.0) + val
        cmodels = cday.get("by_model")
        if isinstance(cmodels, dict):
            if target["by_model"] is None:
                target["by_model"] = {}
            for m, mv in cmodels.items():
                mt = target["by_model"].setdefault(m, {})
                for mk, mval in mv.items():
                    mt[mk] = mt.get(mk, 0.0) + mval
    return result


def build_codex_history_lines(
    days_by_date: Dict[str, Dict[str, Any]],
    now: datetime,
) -> List[MetricLine]:
    """Build Today / Yesterday / Last 30 Days token MetricLines with UsageBreakdown."""
    utc_today = now.astimezone(timezone.utc).date()
    windows = [
        ("today", "Today", [0]),
        ("yesterday", "Yesterday", [1]),
        ("last30", "Last 30 Days", list(range(30))),
    ]
    lines: List[MetricLine] = []
    for key, title, offsets in windows:
        date_keys = [(utc_today - timedelta(days=o)).strftime("%Y-%m-%d") for o in offsets]
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

        tokens_total = totals.get("tokens")
        if tokens_total is None:
            continue

        token_parts: Dict[str, float] = {}
        for src, label in (
            ("inputTokens", "Input"),
            ("cacheReadTokens", "Cache Read"),
            ("cacheWriteTokens", "Cache Write"),
            ("outputTokens", "Output"),
            ("reasoningTokens", "Reasoning"),
        ):
            if totals.get(src, 0.0) > 0:
                token_parts[label] = totals[src]

        has_model_tokens = any((m.get("tokens") or 0.0) > 0 for m in models_agg.values())
        share_measure = None
        for cand in ("credits", "turns"):
            if totals.get(cand, 0.0) > 0 and any((m.get(cand) or 0.0) > 0 for m in models_agg.values()):
                share_measure = cand
                break

        breakdown_models: List[UsageBreakdownModel] = []
        for mname, mvals in models_agg.items():
            share_val = None
            if share_measure and mvals.get(share_measure) is not None and totals.get(share_measure, 0.0) > 0:
                frac = mvals[share_measure] / totals[share_measure]
                if math.isfinite(frac):
                    share_val = frac
            breakdown_models.append(
                UsageBreakdownModel(
                    name=mname,
                    tokens=mvals.get("tokens") if has_model_tokens else None,
                    share=share_val,
                )
            )

        breakdown = UsageBreakdown(
            title=title,
            tokens=tokens_total,
            token_parts=token_parts,
            models=breakdown_models,
        )
        lines.append(
            MetricLine(
                key=key,
                title=title,
                line_type="values",
                kind="count",
                values=[MetricValue(number=tokens_total, kind="count", label="tokens")],
                breakdown=breakdown,
                resource_key=None,
            )
        )
    return lines


def map_codex_usage(
    body: Dict[str, Any],
    *,
    headers: Optional[Dict[str, str]] = None,
    reset_credits_body: Optional[Dict[str, Any]] = None,
    history_days: Optional[Dict[str, Dict[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> Tuple[Optional[str], List[MetricLine]]:
    """Pure mapper from ChatGPT `/backend-api/wham/usage` response to (plan, MetricLines)."""
    now = now or utc_now()
    hdrs = {k.lower(): v for k, v in (headers or {}).items()}
    lines: List[MetricLine] = []

    rate_limit = body.get("rate_limit") if isinstance(body.get("rate_limit"), dict) else None
    lines.extend(
        _map_window_lines(
            rate_limit,
            session_meta=("session", "Session", "session"),
            weekly_meta=("weekly", "Weekly", "weekly"),
            header_primary=safe_float(hdrs.get("x-codex-primary-used-percent")),
            header_secondary=safe_float(hdrs.get("x-codex-secondary-used-percent")),
            now=now,
            include_missing_sibling=True,
        )
    )

    # Spark lines from additional_rate_limits
    additional = body.get("additional_rate_limits")
    if isinstance(additional, list):
        for entry in additional:
            if not isinstance(entry, dict):
                continue
            names = [entry.get("limit_name"), entry.get("metered_feature")]
            if any(isinstance(n, str) and "spark" in n.lower() for n in names):
                spark_rl = entry.get("rate_limit") if isinstance(entry.get("rate_limit"), dict) else None
                if spark_rl is not None:
                    lines.extend(
                        _map_window_lines(
                            spark_rl,
                            session_meta=("spark", "Spark", "spark"),
                            weekly_meta=("sparkWeekly", "Spark Weekly", "sparkWeekly"),
                            now=now,
                        )
                    )
                break

    # Rate Limit Resets
    resets_info = _read_reset_credits(body, reset_credits_body)
    if resets_info is not None:
        count, pairs = resets_info
        expiries: List[str] = []
        reset_items: List[ResetItem] = []
        for idx, (cid, exp_dt) in enumerate(pairs):
            exp_iso = format_iso8601(exp_dt)
            if not exp_iso:
                continue
            expiries.append(exp_iso)
            reset_items.append(
                ResetItem(
                    id=cid or f"codex:{exp_iso}:{idx}",
                    provider_kind="codex",
                    kind="full",
                    title="Full Reset",
                    expires_at=exp_iso,
                    usable_now=True,
                    claim_payload={"expires_at": exp_iso, "credit_id": cid},
                )
            )
        lines.append(
            MetricLine(
                key="rateLimitResets",
                title="Limit Resets",
                line_type="values",
                kind="count",
                values=[MetricValue(number=float(count), kind="count", label="available")],
                expiries_at=sorted(expiries),
                reset_items=sorted(reset_items, key=lambda r: r.sort_key()),
                resource_key="rateLimitResets",
                resource_kind="balance",
                resource_unit="resets",
            )
        )

    # Credits
    credits_obj = body.get("credits") if isinstance(body.get("credits"), dict) else {}
    has_credits = _parse_bool(credits_obj.get("has_credits"))
    if has_credits is None:
        has_credits = _parse_bool(hdrs.get("x-codex-credits-has-credits"))
    if has_credits is not False:
        remaining = safe_float(credits_obj.get("balance"))
        if remaining is None:
            remaining = safe_float(hdrs.get("x-codex-credits-balance"))
        if remaining is not None and math.isfinite(remaining):
            credits_int = max(0, int(math.floor(remaining)))
            lines.append(
                MetricLine(
                    key="credits",
                    title="Extra Usage",
                    line_type="values",
                    kind="count",
                    values=[
                        MetricValue(number=float(credits_int) * CREDIT_USD_RATE, kind="dollars"),
                        MetricValue(number=float(credits_int), kind="count", label="credits"),
                    ],
                    resource_key="credits",
                    resource_kind="balance",
                    resource_unit="credits",
                )
            )

    # Credit Limit (workspace spend control)
    spend_control = body.get("spend_control")
    ind_limit = spend_control.get("individual_limit") if isinstance(spend_control, dict) else None
    if isinstance(ind_limit, dict):
        used = safe_float(ind_limit.get("used"))
        cap = safe_float(ind_limit.get("limit"))
        if used is not None and cap is not None and used >= 0 and cap >= 0:
            reset_ts = safe_float(ind_limit.get("reset_at"))
            reset_iso = format_iso8601(datetime.fromtimestamp(reset_ts, tz=timezone.utc)) if reset_ts is not None else None
            lines.append(
                MetricLine(
                    key="creditLimit",
                    title="Credit Limit",
                    line_type="progress",
                    kind="count",
                    used=used,
                    limit=cap,
                    unit_suffix="credits",
                    resets_at=reset_iso,
                    resource_key=None,
                )
            )

    if history_days:
        lines.extend(build_codex_history_lines(history_days, now))

    plan = format_codex_plan(body.get("plan_type"))
    return plan, lines


# Secondary reads owned by this provider (see backend/secondary.py)
CODEX_RESET_CREDITS_KEY = "codex_reset_credits"  # value: reset-credits list body
CODEX_HISTORY_KEY = "codex_history"              # ledger: {"readAt", "today", "days", "holdUntil"?}


def discard_reset_credits(cache: SecondaryCache) -> None:
    """Forget the cached reset-credits list so the next refresh re-reads it."""
    cache.drop(CODEX_RESET_CREDITS_KEY)


def _fetch_report(
    ctx: FetchContext,
    path: str,
    params: Dict[str, str],
    access_token: str,
    account_id: Optional[str],
) -> Optional[Tuple[Optional[Dict[str, Any]], Optional[float]]]:
    url = CODEX_REPORTS_BASE_URL + path
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    try:
        resp = ctx.http.get(url, headers=codex_usage_headers(access_token, account_id), timeout=15.0)
    except HTTPTransportError:
        return None, None
    if resp.status == 429:
        return None, parse_retry_after(resp) or 3600.0
    if not (200 <= resp.status < 300):
        return None, None
    try:
        data = resp.json()
    except Exception:
        return None, None
    return (data if isinstance(data, dict) else None), None


def _refresh_codex_history(
    ctx: FetchContext,
    access_token: str,
    account_id: Optional[str],
    plan_type: Optional[str],
) -> Optional[Dict[str, Dict[str, Any]]]:
    cache = ctx.secondary_cache.get(CODEX_HISTORY_KEY)
    utc_today = ctx.now.astimezone(timezone.utc).date()
    today_str = utc_today.strftime("%Y-%m-%d")

    if (
        not ctx.force
        and isinstance(cache, dict)
        and cache.get("today") == today_str
        and not ctx.secondary_cache.is_due(CODEX_HISTORY_KEY, ctx.now)
    ):
        return cache.get("days")

    hold_until = parse_iso8601(cache.get("holdUntil")) if isinstance(cache, dict) else None
    if hold_until is not None and ctx.now < hold_until:
        return cache.get("days") if isinstance(cache, dict) else None

    start_str = (utc_today - timedelta(days=29)).strftime("%Y-%m-%d")
    end_str = today_str

    raw_counts, rl_wait = _fetch_report(
        ctx,
        "analytics/daily-workspace-usage-counts",
        {
            "start_date": start_str,
            "end_date": end_str,
            "group_by": "day",
            "workspace_user": "true",
        },
        access_token,
        account_id,
    )
    if rl_wait is not None:
        ctx.secondary_cache[CODEX_HISTORY_KEY] = {
            "readAt": format_iso8601(ctx.now),
            "today": today_str,
            "holdUntil": format_iso8601(ctx.now + timedelta(seconds=rl_wait)),
            "days": cache.get("days") if isinstance(cache, dict) else None,
        }
        return cache.get("days") if isinstance(cache, dict) else None

    counts_days = parse_codex_counts_report(raw_counts) if raw_counts is not None else None
    if counts_days is None:
        return cache.get("days") if isinstance(cache, dict) else None

    # Fill missing days in the 30-day span with empty totals (quietDay)
    full_counts: Dict[str, Dict[str, Any]] = {}
    for offset in range(30):
        dk = (utc_today - timedelta(days=offset)).strftime("%Y-%m-%d")
        full_counts[dk] = counts_days.get(dk, {"totals": {}, "by_model": None})

    credits_days: Optional[Dict[str, Dict[str, Any]]] = None
    low_plan = (plan_type or "").lower()
    if low_plan in _TEAM_CREDIT_PLANS:
        raw_c, _ = _fetch_report(
            ctx,
            "usage/daily-workspace-user-token-usage-breakdown",
            {"start_date": start_str, "end_date": end_str, "group_by": "day"},
            access_token,
            account_id,
        )
        if raw_c is not None:
            credits_days = parse_codex_team_credits_report(raw_c)
    elif low_plan in _ENTERPRISE_CREDIT_PLANS:
        raw_c, _ = _fetch_report(
            ctx,
            "usage/daily-workspace-user-credit-usage",
            {"start_date": start_str, "end_date": end_str, "breakdown": "model"},
            access_token,
            account_id,
        )
        if raw_c is not None:
            credits_days = parse_codex_enterprise_credits_report(raw_c)

    combined = combine_codex_reports(full_counts, credits_days)
    ctx.secondary_cache[CODEX_HISTORY_KEY] = {
        "readAt": format_iso8601(ctx.now),
        "today": today_str,
        "days": combined,
    }
    return combined


def fetch(ctx: FetchContext) -> ProviderSnapshot:
    """Fetch ChatGPT / Codex usage, hourly reset credits, and hourly analytics history."""
    def _get_usage(token: str) -> HTTPResponse:
        account = extract_chatgpt_account_id(ctx.credential)
        return ctx.http.get(CODEX_USAGE_URL, headers=codex_usage_headers(token, account), timeout=10.0)

    cred, resp = authorized(ctx, OAUTH, _get_usage)
    access_token = str(cred.get("accessToken") or "").strip()
    account_id = extract_chatgpt_account_id(cred)
    raise_for_auth_failure(resp, "ChatGPT")
    raise_for_http_error(resp, "ChatGPT")
    body = read_json(resp, "ChatGPT")

    # Hourly reset credits fetch
    def _read_reset_credits() -> Optional[Dict[str, Any]]:
        rc_resp = ctx.http.get(
            CODEX_RESET_CREDITS_URL,
            headers=codex_reset_credit_headers(access_token, account_id),
            timeout=10.0,
        )
        if 200 <= rc_resp.status < 300:
            parsed_rc = rc_resp.json()
            if isinstance(parsed_rc, dict):
                return parsed_rc
        return None

    reset_credits_body = ctx.secondary_cache.read(
        CODEX_RESET_CREDITS_KEY, ctx.now, _read_reset_credits, force=ctx.force
    )

    plan_type = body.get("plan_type") if isinstance(body.get("plan_type"), str) else None
    history_days = _refresh_codex_history(ctx, access_token, account_id, plan_type)

    plan, lines = map_codex_usage(
        body,
        headers=resp.headers,
        reset_credits_body=reset_credits_body,
        history_days=history_days,
        now=ctx.now,
    )

    return make_snapshot(ctx, plan=plan, lines=lines)
