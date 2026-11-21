"""Rate-limit reset claim service for Claude, Codex (ChatGPT), and Z.ai."""

from __future__ import annotations

import math
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from backend.http_client import HTTPResponse, HTTPTransportError
from backend.i18n import tr
from backend.models import (
    ResetItem,
    format_duration_seconds,
    parse_iso8601,
    utc_now,
)
from backend.providers import claude, codex
from backend.providers.base import FetchContext, ProviderError, is_auth_failure, safe_float
from backend.providers.claude import (
    CLAUDE_RESET_GRANTS_URL,
    cached_org_uuid,
    claude_code_headers,
    fetch_claude_profile,
    invalidate_reset_grants,
    parse_claude_reset_grants,
    parse_reset_date,
    record_reset_grants,
    remember_org_uuid,
    reset_grants_status,
)
from backend.providers.codex import (
    CODEX_CONSUME_RESET_CREDIT_URL,
    CODEX_RESET_CREDITS_URL,
    codex_reset_credit_headers,
    discard_reset_credits,
    extract_chatgpt_account_id,
    find_codex_reset_credit_id,
)
from backend.providers.session import can_renew, fresh_credential, send_with_retry
from backend.providers.zai import (
    RESET_LIST_URL as ZAI_RESET_LIST_URL,
    USE_RESET_URL as ZAI_USE_RESET_URL,
    discard_resets as discard_zai_resets,
    parse_zai_expiry,
)

# Claim-only state kept in the account's SecondaryCache so retries reuse ids.
CLAUDE_UNSETTLED_CLAIMS_KEY = "claude_unsettled_claims"    # grant_id -> request_id
CODEX_MATCHED_CREDIT_IDS_KEY = "codex_matched_credit_ids"  # request_id -> credit_id

CLAUDE_RESET_CLAIM_URL_TEMPLATE = "https://api.anthropic.com/api/organizations/{org_uuid}/reset_rate_limits"

_GRANT_ID_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# A cooldown ending within this many seconds reads as "soon".
COOLDOWN_SOON_SEC = 300


def _reset_kind_title(kind: Optional[str]) -> Optional[str]:
    """Title of a Claude reset kind, for "Claude wants your ... used first"."""
    if kind == "session":
        return tr("reset.kindSession", "Session Reset")
    if kind == "weekly":
        return tr("reset.kindWeekly", "Weekly Reset")
    if kind == "fable":
        return tr("reset.kindFable", "Fable Reset")
    if kind == "full":
        return tr("reset.kindFull", "Full Reset")
    return None


_ZAI_WINDOW_KEYS = (
    ("FIVE_HOUR", "fiveHourResets"),
    ("WEEK", "weekResets"),
)


def _format_outcome(
    outcome: str,
    *,
    now: Optional[datetime] = None,
    cooldown_until: Optional[datetime] = None,
    next_kind: Optional[str] = None,
) -> Dict[str, Any]:
    now = now or utc_now()
    if outcome == "success":
        return {"ok": True, "outcome": "success", "message": tr("reset.claimed", "Reset claimed. Enjoy!")}
    if outcome == "nothing_to_reset":
        return {
            "ok": False,
            "outcome": "nothing_to_reset",
            "message": tr("reset.nothingToReset", "Your usage doesn't need a reset yet"),
        }
    if outcome == "no_credit":
        return {"ok": False, "outcome": "no_credit", "message": tr("reset.noLongerAvailable", "That reset is no longer available")}
    if outcome == "unconfirmed":
        return {
            "ok": False,
            "outcome": "unconfirmed",
            "message": tr("reset.unconfirmed", "Your reset may still be going through. Check again in a minute."),
        }
    if outcome == "unavailable_now":
        msg = tr("reset.unavailableNow", "Resets can't be used right now.")
        if cooldown_until is not None:
            # Decide on the remaining time, never on translated text.
            remaining_sec = (cooldown_until - now).total_seconds()
            if remaining_sec <= COOLDOWN_SOON_SEC:
                msg = tr("reset.unavailableNowTrySoon", "Resets can't be used right now. Try again soon.")
            else:
                msg = tr(
                    "reset.unavailableNowTryIn",
                    "Resets can't be used right now. Try again in %1.",
                    format_duration_seconds(remaining_sec),
                )
        return {"ok": False, "outcome": "unavailable_now", "message": msg}
    if outcome == "not_next_reset":
        title = _reset_kind_title(next_kind)
        if title:
            msg = tr("reset.wantsKindFirst", "Claude wants your %1 used first. Nothing was used.", title)
        else:
            msg = tr("reset.wantsAnotherFirst", "Claude wants another reset used first. Nothing was used.")
        return {"ok": False, "outcome": "not_next_reset", "message": msg}
    if outcome == "sign_in_expired":
        return {
            "ok": False,
            "outcome": "sign_in_expired",
            "message": tr("reset.signInExpired", "Your sign-in expired. Sign in again from the account's page."),
        }
    if outcome == "not_permitted":
        return {
            "ok": False,
            "outcome": "not_permitted",
            "message": tr("reset.notPermitted", "This sign-in can't use resets. Remove the account and add it again."),
        }
    if outcome == "connection":
        return {
            "ok": False,
            "outcome": "connection",
            "message": tr("reset.connectionFailed", "Couldn't connect. Check your internet connection and try again."),
        }
    return {"ok": False, "outcome": "refused", "message": tr("reset.failed", "Couldn't reset usage. Please try again.")}


def _failure_from_error(exc: Exception) -> str:
    if isinstance(exc, ProviderError):
        if exc.kind in ("auth_expired", "api_key_rejected"):
            return "sign_in_expired"
        if exc.kind in ("offline", "unreachable"):
            return "connection"
        return "refused"
    if isinstance(exc, HTTPTransportError):
        return "connection"
    return "refused"


# ---------------------------------------------------------------------------
# Claude reset grant claim
# ---------------------------------------------------------------------------

def map_claude_claim_response(
    resp: HTTPResponse,
    *,
    next_kind: Optional[str] = None,
    renewable_sign_in: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Map Anthropic `reset_rate_limits` HTTP response to claim outcome dict."""
    if resp.status == 401 and renewable_sign_in:
        return _format_outcome("not_permitted", now=now)
    if resp.status == 403 and "scope requirement" in resp.text:
        return _format_outcome("not_permitted", now=now)
    if is_auth_failure(resp):
        return _format_outcome("sign_in_expired", now=now)
    if not (200 <= resp.status < 300):
        return _format_outcome("refused", now=now)
    try:
        body = resp.json()
    except Exception:
        return _format_outcome("refused", now=now)
    if not isinstance(body, dict) or not isinstance(body.get("result"), str):
        return _format_outcome("refused", now=now)

    result = body["result"]
    cooldown_until = parse_reset_date(body.get("cooldown_until"))

    def _map_code(code: str) -> Optional[Dict[str, Any]]:
        if code in ("reset", "already_used"):
            return _format_outcome("success", now=now)
        if code == "not_limited":
            return _format_outcome("nothing_to_reset", now=now)
        if code in ("ineligible", "no_grant", "unknown_grant"):
            return _format_outcome("no_credit", now=now)
        if code == "not_next_grant":
            return _format_outcome("not_next_reset", next_kind=next_kind, now=now)
        if code in ("reset_unconfirmed", "stamp_indeterminate"):
            return _format_outcome("unconfirmed", now=now)
        if code in ("cooldown", "paused", "low_priority_active"):
            return _format_outcome("unavailable_now", cooldown_until=cooldown_until, now=now)
        return None

    if result == "unavailable":
        reason = body.get("reason")
        if isinstance(reason, str):
            mapped = _map_code(reason)
            if mapped is not None:
                return mapped
        return _format_outcome("refused", now=now)

    mapped = _map_code(result)
    return mapped if mapped is not None else _format_outcome("refused", now=now)


def _is_valid_uuid(val: Any) -> bool:
    if not isinstance(val, str) or not val:
        return False
    try:
        uuid.UUID(val)
        return True
    except ValueError:
        return False


def _claim_claude_reset(ctx: FetchContext, item: Dict[str, Any], request_id: str) -> Dict[str, Any]:
    payload = item.get("claimPayload") if isinstance(item.get("claimPayload"), dict) else {}
    grant_id = payload.get("grant_id")
    if not isinstance(grant_id, str) or not grant_id:
        raw_id = str(item.get("id") or "")
        grant_id = raw_id.split(":", 1)[0]

    try:
        cred = fresh_credential(ctx, claude.OAUTH)
    except Exception as exc:
        return _format_outcome(_failure_from_error(exc), now=ctx.now)
    access_token = str(cred.get("accessToken") or "").strip()

    # Verify fresh reset grants if none have been loaded yet
    cache = ctx.secondary_cache
    if reset_grants_status(cache) is None:
        try:
            g_resp = ctx.http.get(CLAUDE_RESET_GRANTS_URL, headers=claude_code_headers(access_token), timeout=10.0)
            if is_auth_failure(g_resp):
                return _format_outcome("sign_in_expired", now=ctx.now)
            if not (200 <= g_resp.status < 300):
                return _format_outcome("refused", now=ctx.now)
            g_json = g_resp.json()
            status = parse_claude_reset_grants(g_json.get("cedar_ember") if isinstance(g_json, dict) else None)
            record_reset_grants(cache, status, ctx.now)
        except Exception as exc:
            return _format_outcome(_failure_from_error(exc), now=ctx.now)

    status = reset_grants_status(cache)
    chosen_grant = None
    if isinstance(status, dict) and status.get("eligible") is True:
        for g in status.get("grants") or []:
            if isinstance(g, dict) and g.get("id") == grant_id and int(g.get("resets_left") or 0) > 0:
                chosen_grant = g
                break
    if chosen_grant is None:
        return _format_outcome("no_credit", now=ctx.now)

    unsettled: Dict[str, str] = cache.setdefault(CLAUDE_UNSETTLED_CLAIMS_KEY, {})
    eff_request_id = unsettled.get(grant_id) or request_id

    if not _GRANT_ID_RE.match(grant_id) or not _REQUEST_ID_RE.match(eff_request_id):
        return _format_outcome("refused", now=ctx.now)

    # Resolve organization UUID
    org_uuid = cached_org_uuid(cache)
    if not _is_valid_uuid(org_uuid):
        try:
            profile = fetch_claude_profile(ctx, access_token)
            org = profile.get("organization") if isinstance(profile, dict) and isinstance(profile.get("organization"), dict) else {}
            org_uuid = org.get("uuid")
            if _is_valid_uuid(org_uuid):
                remember_org_uuid(cache, org_uuid)
        except Exception as exc:
            return _format_outcome(_failure_from_error(exc), now=ctx.now)

    if not _is_valid_uuid(org_uuid):
        return _format_outcome("refused", now=ctx.now)

    claim_url = CLAUDE_RESET_CLAIM_URL_TEMPLATE.format(org_uuid=org_uuid)
    req_body = {"program": "cedar_ember", "grant_id": grant_id, "request_id": eff_request_id}

    def _post_claim(tok: str) -> HTTPResponse:
        return ctx.http.post(
            claim_url,
            headers=claude_code_headers(tok),
            json_body=req_body,
            timeout=25.0,
        )

    try:
        cred, resp = send_with_retry(ctx, claude.OAUTH, cred, _post_claim)
    except Exception as exc:
        unsettled[grant_id] = eff_request_id
        return _format_outcome(_failure_from_error(exc), now=ctx.now)

    succeeded = 200 <= resp.status < 300
    body = None
    if succeeded:
        try:
            parsed = resp.json()
            if isinstance(parsed, dict):
                body = parsed
        except Exception:
            body = None

    if isinstance(body, dict) and body.get("result") is not None:
        unsettled.pop(grant_id, None)
    elif succeeded or resp.status >= 500:
        unsettled[grant_id] = eff_request_id

    chosen_kind = item.get("kind") or payload.get("kind")
    next_kind = payload.get("next_grant_kind")
    if next_kind == chosen_kind:
        next_kind = None

    renewable = can_renew(ctx, cred)
    outcome_dict = map_claude_claim_response(resp, next_kind=next_kind, renewable_sign_in=renewable, now=ctx.now)

    if outcome_dict["outcome"] in ("success", "unconfirmed", "no_credit", "not_next_reset"):
        if outcome_dict["outcome"] == "success":
            resets_left_num = safe_float(body.get("resets_left")) if isinstance(body, dict) else None
            if resets_left_num is not None and math.isfinite(resets_left_num):
                chosen_grant["resets_left"] = max(0, int(math.floor(resets_left_num)))
            else:
                chosen_grant["resets_left"] = max(0, int(chosen_grant.get("resets_left") or 0) - 1)
        # Allow next refresh to pick up updated server state
        invalidate_reset_grants(cache)

    return outcome_dict


# ---------------------------------------------------------------------------
# Codex (ChatGPT) reset credit claim
# ---------------------------------------------------------------------------

def map_codex_claim_response(resp: HTTPResponse, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Map ChatGPT `/rate-limit-reset-credits/consume` HTTP response to claim outcome dict."""
    if is_auth_failure(resp):
        return _format_outcome("sign_in_expired", now=now)
    if not (200 <= resp.status < 300):
        return _format_outcome("refused", now=now)
    try:
        body = resp.json()
    except Exception:
        return _format_outcome("refused", now=now)
    code = body.get("code") if isinstance(body, dict) else None
    if not isinstance(code, str):
        return _format_outcome("refused", now=now)
    if code in ("reset", "already_redeemed"):
        return _format_outcome("success", now=now)
    if code == "nothing_to_reset":
        return _format_outcome("nothing_to_reset", now=now)
    if code == "no_credit":
        return _format_outcome("no_credit", now=now)
    return _format_outcome("refused", now=now)


def _claim_codex_reset(ctx: FetchContext, item: Dict[str, Any], request_id: str) -> Dict[str, Any]:
    payload = item.get("claimPayload") if isinstance(item.get("claimPayload"), dict) else {}
    expiry_raw = payload.get("expires_at") or item.get("expiresAt")
    target_expiry = parse_iso8601(expiry_raw)
    if target_expiry is None:
        return _format_outcome("no_credit", now=ctx.now)

    try:
        cred = fresh_credential(ctx, codex.OAUTH)
    except Exception as exc:
        return _format_outcome(_failure_from_error(exc), now=ctx.now)
    discard_reset_credits(ctx.secondary_cache)

    def _headers(token: str) -> Dict[str, str]:
        return codex_reset_credit_headers(token, extract_chatgpt_account_id(ctx.credential))

    matched_ids: Dict[str, str] = ctx.secondary_cache.setdefault(CODEX_MATCHED_CREDIT_IDS_KEY, {})
    credit_id = matched_ids.get(request_id)

    if not credit_id:
        try:
            cred, list_resp = send_with_retry(
                ctx,
                codex.OAUTH,
                cred,
                lambda token: ctx.http.get(CODEX_RESET_CREDITS_URL, headers=_headers(token), timeout=10.0),
            )
        except Exception as exc:
            return _format_outcome(_failure_from_error(exc), now=ctx.now)

        if is_auth_failure(list_resp):
            return _format_outcome("sign_in_expired", now=ctx.now)
        if not (200 <= list_resp.status < 300):
            return _format_outcome("refused", now=ctx.now)
        try:
            list_body = list_resp.json()
        except Exception:
            return _format_outcome("refused", now=ctx.now)
        if not isinstance(list_body, dict):
            return _format_outcome("refused", now=ctx.now)

        credit_id = find_codex_reset_credit_id(list_body, target_expiry)
        if not credit_id:
            return _format_outcome("no_credit", now=ctx.now)
        matched_ids[request_id] = credit_id

    consume_body = {"credit_id": credit_id, "redeem_request_id": request_id}

    def _consume(token: str) -> HTTPResponse:
        headers = _headers(token)
        headers["Content-Type"] = "application/json"
        return ctx.http.post(CODEX_CONSUME_RESET_CREDIT_URL, headers=headers, json_body=consume_body, timeout=15.0)

    try:
        _, resp = send_with_retry(ctx, codex.OAUTH, cred, _consume)
    except Exception as exc:
        return _format_outcome(_failure_from_error(exc), now=ctx.now)

    return map_codex_claim_response(resp, now=ctx.now)


# ---------------------------------------------------------------------------
# Z.ai banked reset claim
# ---------------------------------------------------------------------------


def parse_zai_resets(body: Any, now: datetime) -> List[Dict[str, Any]]:
    """Parse Z.ai available unexpired resets, sorted earliest first by (expires_at, record_id)."""
    if not isinstance(body, dict) or body.get("success") is False:
        raise ProviderError(tr("reset.zaiInvalidList", "Z.ai returned an invalid reset list response."), kind="refused")
    data = body.get("data")
    if not isinstance(data, dict):
        raise ProviderError(tr("reset.zaiInvalidList", "Z.ai returned an invalid reset list response."), kind="refused")

    resets: List[Dict[str, Any]] = []
    for window_name, list_key in _ZAI_WINDOW_KEYS:
        if list_key not in data or data[list_key] is None:
            continue
        records = data[list_key]
        if not isinstance(records, list):
            raise ProviderError(tr("reset.zaiInvalidList", "Z.ai returned an invalid reset list response."), kind="refused")
        for rec in records:
            if not isinstance(rec, dict) or rec.get("available") is not True:
                continue
            record_id = rec.get("recordId")
            expire_raw = rec.get("expireTime")
            grant_type = rec.get("grantType")
            if not isinstance(record_id, int) or isinstance(record_id, bool):
                raise ProviderError(tr("reset.zaiInvalidRecord", "Z.ai returned an invalid reset record."), kind="refused")
            expires_dt = parse_zai_expiry(expire_raw) if isinstance(expire_raw, str) else None
            if expires_dt is None or not isinstance(grant_type, str) or not grant_type:
                raise ProviderError(tr("reset.zaiInvalidRecord", "Z.ai returned an invalid reset record."), kind="refused")
            if expires_dt > now:
                resets.append({
                    "window": window_name,
                    "record_id": record_id,
                    "expires_at": expires_dt,
                    "grant_type": grant_type,
                })

    resets.sort(key=lambda r: (r["expires_at"], r["record_id"]))
    return resets


def map_zai_claim_response(resp: HTTPResponse, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Map Z.ai `/customer-package-reset/use` HTTP response to claim outcome dict."""
    if is_auth_failure(resp):
        return _format_outcome("sign_in_expired", now=now)
    if not (200 <= resp.status < 300):
        return _format_outcome("refused", now=now)
    try:
        body = resp.json()
    except Exception:
        return _format_outcome("refused", now=now)
    if isinstance(body, dict) and body.get("success") is True:
        return _format_outcome("success", now=now)
    return _format_outcome("refused", now=now)


def _claim_zai_reset(ctx: FetchContext, item: Dict[str, Any], request_id: str) -> Dict[str, Any]:
    api_key = str(ctx.credential.get("apiKey") or ctx.credential.get("accessToken") or "").strip()
    if not api_key:
        return _format_outcome("sign_in_expired", now=ctx.now)

    payload = item.get("claimPayload") if isinstance(item.get("claimPayload"), dict) else {}
    window = payload.get("window") or payload.get("resetType")
    if not window:
        kind = item.get("kind")
        window = "FIVE_HOUR" if kind == "session" else ("WEEK" if kind == "weekly" else None)
    record_id = payload.get("record_id") if "record_id" in payload else payload.get("recordId")
    grant_type = payload.get("grant_type") or payload.get("grantType")
    target_expiry = parse_iso8601(payload.get("expires_at") or item.get("expiresAt"))

    if not isinstance(window, str) or not isinstance(record_id, int) or isinstance(record_id, bool) or not isinstance(grant_type, str) or not grant_type:
        return _format_outcome("no_credit", now=ctx.now)

    # Re-read the reset list and confirm this exact record is still its window's earliest
    try:
        list_resp = ctx.http.get(
            ZAI_RESET_LIST_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            timeout=15.0,
        )
    except Exception as exc:
        return _format_outcome(_failure_from_error(exc), now=ctx.now)

    if is_auth_failure(list_resp):
        return _format_outcome("sign_in_expired", now=ctx.now)
    if not (200 <= list_resp.status < 300):
        return _format_outcome("refused", now=ctx.now)

    try:
        current = parse_zai_resets(list_resp.json(), ctx.now)
    except Exception as exc:
        return _format_outcome(_failure_from_error(exc), now=ctx.now)

    earliest_for_window = next((r for r in current if r["window"] == window), None)
    if earliest_for_window is None:
        return _format_outcome("no_credit", now=ctx.now)
    expiry_matches = (
        target_expiry is None
        or abs((earliest_for_window["expires_at"] - target_expiry).total_seconds()) < 1.0
    )
    if (
        earliest_for_window["record_id"] != record_id
        or earliest_for_window["grant_type"] != grant_type
        or not expiry_matches
    ):
        return _format_outcome("no_credit", now=ctx.now)

    discard_zai_resets(ctx.secondary_cache)

    try:
        resp = ctx.http.post(
            ZAI_USE_RESET_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json_body={
                "grantType": grant_type,
                "recordId": record_id,
                "requestId": request_id,
                "resetType": window,
                "targetType": "PERSONAL",
            },
            timeout=15.0,
        )
    except Exception as exc:
        return _format_outcome(_failure_from_error(exc), now=ctx.now)

    return map_zai_claim_response(resp, now=ctx.now)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def claim_reset(ctx: FetchContext, reset_item: Any) -> Dict[str, Any]:
    """Spend the chosen rate-limit reset for Claude, Codex (ChatGPT), or Z.ai.

    Returns `{"ok": bool, "outcome": str, "message": str}`.
    """
    if isinstance(reset_item, ResetItem):
        item = reset_item.to_dict()
    elif isinstance(reset_item, dict):
        item = dict(reset_item)
    else:
        return _format_outcome("refused", now=ctx.now)

    provider_kind = str(item.get("providerKind") or ctx.account.kind or "").lower()
    request_id = str(item.get("requestId") or uuid.uuid4())

    if provider_kind == "claude":
        return _claim_claude_reset(ctx, item, request_id)
    if provider_kind == "codex":
        return _claim_codex_reset(ctx, item, request_id)
    if provider_kind == "zai":
        return _claim_zai_reset(ctx, item, request_id)

    return _format_outcome("refused", now=ctx.now)
