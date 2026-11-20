"""OAuth session engine: credential freshness, token renewal, and the 401 retry.

Every OAuth provider declares one `OAuthRefresh` (where and how to renew its
token) and reads through the engine instead of handling tokens itself:

    OAUTH = OAuthRefresh(provider_name="Amp", token_url=..., client_id=...)

    cred, resp = authorized(ctx, OAUTH, lambda token: ctx.http.get(url, headers=...))

The engine:
- renews early when the access token expires within `margin_sec`, and keeps the
  current token when renewal fails but the token hasn't expired yet;
- renews once and resends when the read answers 401 (or `retry_when`);
- maps transport failures to `offline` / `unreachable`, terminal OAuth codes to
  `auth_expired`, and malformed token responses to `refused`;
- never renews a hub account: the hub renews its tokens itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Mapping, Optional, Tuple, TypeVar, Union

from backend.http_client import HTTPResponse
from backend.i18n import tr
from backend.jwt_claims import jwt_expiry
from backend.models import format_iso8601, parse_iso8601
from backend.providers.base import (
    OAUTH_REFRESH_MARGIN_SEC,
    TERMINAL_REFRESH_ERRORS,
    FetchContext,
    ProviderError,
    oauth_error_code,
    parse_number,
    raise_for_http_error,
    send_mapped,
)

T = TypeVar("T")

# `expiry(token_payload, access_token, now)` -> when the renewed token expires, or None if unknown.
TokenExpiry = Callable[[Dict[str, Any], str, datetime], Optional[datetime]]


def expires_in_expiry(payload: Dict[str, Any], access_token: str, now: datetime) -> Optional[datetime]:
    """Expiry from the token response's `expires_in` seconds (the OAuth default)."""
    seconds = parse_number(payload.get("expires_in"))
    if seconds is None or seconds <= 0:
        return None
    return now + timedelta(seconds=seconds)


def jwt_or_expires_in_expiry(payload: Dict[str, Any], access_token: str, now: datetime) -> Optional[datetime]:
    """The access token's own JWT `exp`, falling back to `expires_in`."""
    return jwt_expiry(access_token) or expires_in_expiry(payload, access_token, now)


def jwt_only_expiry(payload: Dict[str, Any], access_token: str, now: datetime) -> Optional[datetime]:
    return jwt_expiry(access_token)


def _copy(message: Optional[Union[str, Callable[[], str]]]) -> Optional[str]:
    return message() if callable(message) else message


@dataclass(frozen=True, eq=False)
class OAuthRefresh:
    """How one provider renews its OAuth access token."""

    provider_name: str
    token_url: str
    client_id: str
    form: bool = False                                   # form-encoded body instead of JSON
    extra_fields: Mapping[str, str] = field(default_factory=dict)  # e.g. client_secret, scope
    headers: Mapping[str, str] = field(default_factory=dict)
    # Copy overrides: a string, or a callable so translated copy follows the active language.
    expired_message: Optional[Union[str, Callable[[], str]]] = None  # terminal refresh failure copy
    invalid_message: Optional[Union[str, Callable[[], str]]] = None  # malformed token response copy
    margin_sec: float = OAUTH_REFRESH_MARGIN_SEC         # renew this long before expiry
    expiry: TokenExpiry = expires_in_expiry
    fallback_ttl_sec: Optional[float] = None             # conservative TTL when `expiry` returns None
    drop_unknown_expiry: bool = False                    # forget `expiresAt` when `expiry` is None
    expiry_from_jwt: bool = False                        # read the current expiry from the JWT when `expiresAt` is missing
    refresh_token_fallback_to_access: bool = False       # no new refresh token -> reuse the new access token
    # Retry the renewal once with these fields merged in (e.g. a narrower scope).
    retry_fields: Optional[Callable[[HTTPResponse], Optional[Dict[str, str]]]] = None
    # Provider-specific "sign in again" signals, checked before the terminal OAuth codes.
    is_terminal: Optional[Callable[[HTTPResponse, Any], bool]] = None

    @property
    def expired_copy(self) -> str:
        return _copy(self.expired_message) or tr(
            "error.signInExpiredKeepReading", "%1 sign-in expired. Sign in again to keep reading usage.", self.provider_name
        )

    @property
    def invalid_copy(self) -> str:
        return _copy(self.invalid_message) or tr("error.invalidTokenResponse", "%1 returned an invalid token response.", self.provider_name)


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------


def access_token(credential: Dict[str, Any]) -> str:
    return str(credential.get("accessToken") or "").strip()


def _refresh_token(credential: Dict[str, Any]) -> str:
    return str(credential.get("refreshToken") or "").strip()


def can_renew(ctx: FetchContext, credential: Optional[Dict[str, Any]] = None) -> bool:
    """Whether the engine may renew this account's token (never for hub accounts)."""
    cred = ctx.credential if credential is None else credential
    return not ctx.account.is_hub_account and bool(_refresh_token(cred))


def current_expiry(credential: Dict[str, Any], spec: OAuthRefresh) -> Optional[datetime]:
    expires = parse_iso8601(credential.get("expiresAt"))
    if expires is None and spec.expiry_from_jwt:
        expires = jwt_expiry(access_token(credential))
    return expires


def _sign_in_expired(spec: OAuthRefresh, status_code: int = 401) -> ProviderError:
    return ProviderError(
        tr("error.signInExpired", "Sign-in expired for %1. Sign in again.", spec.provider_name),
        kind="auth_expired",
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def renew(ctx: FetchContext, spec: OAuthRefresh) -> Dict[str, Any]:
    """Trade the refresh token for a new access token and persist the merged credential.

    Returns the current credential untouched when it can't be renewed (hub
    account or no refresh token).
    """
    refresh_token = _refresh_token(ctx.credential)
    if not can_renew(ctx):
        return ctx.credential

    fields: Dict[str, str] = {
        "grant_type": "refresh_token",
        "client_id": spec.client_id,
        "refresh_token": refresh_token,
        **spec.extra_fields,
    }

    def post(body: Dict[str, str]) -> HTTPResponse:
        kwargs: Dict[str, Any] = {"form_body" if spec.form else "json_body": body, "timeout": 15.0}
        if spec.headers:
            kwargs["headers"] = dict(spec.headers)
        return send_mapped(lambda: ctx.http.post(spec.token_url, **kwargs))

    resp = post(fields)
    retry = spec.retry_fields(resp) if spec.retry_fields is not None else None
    if retry:
        resp = post({**fields, **retry})

    try:
        payload = resp.json()
    except Exception:
        payload = None
    if spec.is_terminal is not None and spec.is_terminal(resp, payload):
        raise ProviderError(spec.expired_copy, kind="auth_expired", status_code=resp.status)
    if resp.status in (400, 401, 403) and oauth_error_code(resp) in TERMINAL_REFRESH_ERRORS:
        raise ProviderError(spec.expired_copy, kind="auth_expired", status_code=resp.status)
    raise_for_http_error(resp, spec.provider_name)

    new_access = str(payload.get("access_token") or "").strip() if isinstance(payload, dict) else ""
    if not new_access:
        raise ProviderError(spec.invalid_copy, kind="refused")

    updated = dict(ctx.credential)
    updated["accessToken"] = new_access
    new_refresh = payload.get("refresh_token")
    if isinstance(new_refresh, str) and new_refresh.strip():
        updated["refreshToken"] = new_refresh.strip()
    elif spec.refresh_token_fallback_to_access:
        updated["refreshToken"] = new_access
    expires = spec.expiry(payload, new_access, ctx.now)
    if expires is not None:
        updated["expiresAt"] = format_iso8601(expires)
    elif spec.fallback_ttl_sec is not None and spec.fallback_ttl_sec > 0:
        updated["expiresAt"] = format_iso8601(ctx.now + timedelta(seconds=spec.fallback_ttl_sec))
    elif spec.drop_unknown_expiry:
        updated.pop("expiresAt", None)
    ctx.persist_credential(updated)
    return updated


def fresh_credential(ctx: FetchContext, spec: OAuthRefresh) -> Dict[str, Any]:
    """The credential to read with: renewed early, or kept while still valid if renewal fails."""
    cred = ctx.credential
    if not can_renew(ctx, cred):
        return cred
    expires = current_expiry(cred, spec)
    if expires is None or expires > ctx.now + timedelta(seconds=spec.margin_sec):
        return cred
    try:
        return renew(ctx, spec)
    except ProviderError:
        if expires <= ctx.now:
            raise
        return cred


def is_unauthorized(resp: HTTPResponse) -> bool:
    return resp.status == 401


def authorized(
    ctx: FetchContext,
    spec: OAuthRefresh,
    send: Callable[[str], HTTPResponse],
    *,
    retry_when: Callable[[HTTPResponse], bool] = is_unauthorized,
) -> Tuple[Dict[str, Any], HTTPResponse]:
    """Send with a fresh access token; on `retry_when` (401) renew once and resend.

    `send(access_token)` performs the request; transport failures are mapped to
    `offline` / `unreachable`. Returns the credential used and the last response,
    which the caller still checks for errors.
    """
    return send_with_retry(ctx, spec, fresh_credential(ctx, spec), send, retry_when=retry_when)


def send_with_retry(
    ctx: FetchContext,
    spec: OAuthRefresh,
    credential: Dict[str, Any],
    send: Callable[[str], HTTPResponse],
    *,
    retry_when: Callable[[HTTPResponse], bool] = is_unauthorized,
) -> Tuple[Dict[str, Any], HTTPResponse]:
    """`authorized` with a credential already made fresh (several requests, one renewal check)."""
    resp = send_mapped(lambda: send(access_token(credential)))
    if retry_when(resp) and can_renew(ctx, credential):
        credential = renew(ctx, spec)
        resp = send_mapped(lambda: send(access_token(credential)))
    return credential, resp


class TokenRejected(Exception):
    """Raised by a `with_fresh_token` reader when the server refused the access token."""


def with_fresh_token(ctx: FetchContext, spec: OAuthRefresh, read: Callable[[str], T]) -> T:
    """`authorized` for multi-request reads: `read(token)` raises `TokenRejected` on a refused token.

    Renews once and reads again; a second rejection (or one that can't be
    renewed) is an expired sign-in.
    """
    cred = fresh_credential(ctx, spec)
    try:
        return read(access_token(cred))
    except TokenRejected:
        if not can_renew(ctx, cred):
            raise _sign_in_expired(spec) from None
    cred = renew(ctx, spec)
    try:
        return read(access_token(cred))
    except TokenRejected:
        raise _sign_in_expired(spec) from None
