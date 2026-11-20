"""Base provider runtime contract, typed errors, and HTTP/JSON error mapping."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, TypeVar

from backend.backoff import STALENESS_THRESHOLD_SEC
from backend.http_client import HTTPClient, HTTPResponse, HTTPTransportError
from backend.i18n import tr
from backend.models import Account, ProviderSnapshot, format_iso8601, utc_now
from backend.secondary import SecondaryCache


OAUTH_REFRESH_MARGIN_SEC = 300  # 5 minutes

# OAuth error codes meaning the refresh token itself is dead: sign in again.
TERMINAL_REFRESH_ERRORS = frozenset({
    "invalid_grant",
    "refresh_token_expired",
    "refresh_token_reused",
    "refresh_token_invalidated",
})

T = TypeVar("T")


class ProviderError(Exception):
    """Typed provider failure driving backoff and UI notice copy."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "refused",  # "auth_expired" | "api_key_rejected" | "rate_limited" | "offline" | "unreachable" | "refused" | "no_quota"
        retry_after: Optional[float] = None,
        status_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after
        self.status_code = status_code


@dataclass
class FetchContext:
    account: Account
    credential: Dict[str, Any]
    http: HTTPClient
    now: datetime = field(default_factory=utc_now)
    force: bool = False
    previous_snapshot: Optional[ProviderSnapshot] = None
    secondary_cache: SecondaryCache = field(default_factory=SecondaryCache)
    save_credential: Optional[Callable[[Dict[str, Any]], None]] = None

    def persist_credential(self, updated: Dict[str, Any]) -> None:
        self.credential = updated
        if self.save_credential is not None:
            self.save_credential(updated)


def parse_retry_after(resp: HTTPResponse) -> Optional[float]:
    raw = resp.header("retry-after")
    if not raw:
        return None
    raw = raw.strip()
    try:
        secs = float(raw)
        return max(1.0, secs)
    except ValueError:
        return None


def raise_for_http_error(resp: HTTPResponse, provider_name: str, *, is_api_key: bool = False) -> None:
    if 200 <= resp.status < 300:
        return
    if resp.status == 401:
        if is_api_key:
            raise ProviderError(
                tr("error.apiKeyRejected", "%1 rejected the API key.", provider_name),
                kind="api_key_rejected",
                status_code=401,
            )
        raise ProviderError(
            tr("error.signInExpired", "Sign-in expired for %1. Sign in again.", provider_name),
            kind="auth_expired",
            status_code=401,
        )
    if resp.status == 403:
        if is_api_key:
            raise ProviderError(
                tr("error.apiKeyRejected", "%1 rejected the API key.", provider_name),
                kind="api_key_rejected",
                status_code=403,
            )
        raise ProviderError(
            tr("error.accessDenied", "%1 denied access (HTTP 403).", provider_name),
            kind="refused",
            status_code=403,
        )
    if resp.status == 429:
        raise ProviderError(
            tr("error.rateLimited", "%1 rate limited the request.", provider_name),
            kind="rate_limited",
            retry_after=parse_retry_after(resp),
            status_code=429,
        )
    raise ProviderError(
        tr("error.httpStatus", "%1 returned HTTP %2.", provider_name, resp.status),
        kind="refused",
        status_code=resp.status,
    )


def make_snapshot(
    ctx: FetchContext,
    *,
    plan: Optional[str],
    lines: List[Any],
    notice: Optional[str] = None,
    account_label_discovered: Optional[str] = None,
) -> ProviderSnapshot:
    now = ctx.now
    return ProviderSnapshot(
        account_id=ctx.account.id,
        card_id=ctx.account.card_id,
        kind=ctx.account.kind,
        plan=plan,
        lines=lines,
        refreshed_at=format_iso8601(now) or "",
        stale_at=format_iso8601(now + timedelta(seconds=STALENESS_THRESHOLD_SEC)) or "",
        notice=notice,
        account_label_discovered=account_label_discovered,
    )


def safe_float(val: Any, default: Optional[float] = None) -> Optional[float]:
    if val is None or isinstance(val, bool):
        return default
    try:
        f = float(val)
        return f
    except (TypeError, ValueError):
        return default


def safe_int(val: Any, default: Optional[int] = None) -> Optional[int]:
    f = safe_float(val)
    return int(round(f)) if f is not None else default


def parse_number(value: Any) -> Optional[float]:
    """Parse a finite float from a number or numeric string; None otherwise."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        return num if math.isfinite(num) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            num = float(text)
            return num if math.isfinite(num) else None
        except ValueError:
            return None
    return None


def clamp_percent(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(100.0, float(value)))


def oauth_error_code(resp: HTTPResponse) -> Optional[str]:
    """Extract an OAuth error code (`error`, `error.code`, or `code`) from a JSON error body."""
    try:
        data = resp.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    candidates = [err, err.get("code") if isinstance(err, dict) else None, data.get("code")]
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def send_mapped(call: Callable[[], T]) -> T:
    """Run one HTTP call, turning transport failures into `offline` / `unreachable`."""
    try:
        return call()
    except HTTPTransportError as exc:
        raise ProviderError(str(exc), kind="offline" if exc.offline else "unreachable") from exc


def read_json(
    resp: HTTPResponse,
    provider_name: str,
    *,
    expect: Optional[type] = dict,
    invalid_message: Optional[str] = None,
) -> Any:
    """Parse a response body as JSON of type `expect` (None accepts any), else `refused`."""
    try:
        body = resp.json()
    except Exception as exc:
        raise ProviderError(invalid_message or tr("error.invalidJsonResponse", "%1 returned an invalid JSON response.", provider_name), kind="refused") from exc
    if expect is not None and not isinstance(body, expect):
        raise ProviderError(invalid_message or tr("error.unexpectedResponse", "%1 returned an unexpected response.", provider_name), kind="refused")
    return body


def is_auth_failure(resp: HTTPResponse) -> bool:
    """401, or a 403 that rate-limit headers don't explain."""
    if resp.status == 401:
        return True
    return resp.status == 403 and resp.header("x-ratelimit-remaining") != "0" and not resp.header("retry-after")


def raise_for_auth_failure(resp: HTTPResponse, provider_name: str) -> None:
    """Treat a 403 auth failure as an expired sign-in (401s go through `raise_for_http_error`)."""
    if resp.status == 403 and is_auth_failure(resp):
        raise ProviderError(tr("error.signInExpired", "Sign-in expired for %1. Sign in again.", provider_name), kind="auth_expired", status_code=403)
