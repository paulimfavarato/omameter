"""Credential freshness, 401 retry, and error mapping of the OAuth providers.

The first half characterizes every OAuth provider through its public
`fetch(ctx)` (and the reset claims through `claim_reset`), so the behavior is
pinned independently of how the providers implement it. The second half tests
the shared session engine directly with a test spec.

Fake HTTP and temp-free contexts only: no network, no vault, no real sleeps.
"""

from __future__ import annotations

import base64
import json
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.cliproxy import hub_account_credential
from backend.http_client import HTTPResponse, HTTPTransportError
from backend.models import Account, format_iso8601, parse_iso8601
from backend.providers import amp, antigravity, claude, codex, cursor, grok, opencode
from backend.jwt_claims import decode_jwt_payload, jwt_expiry
from backend.providers.base import (
    FetchContext,
    ProviderError,
    is_auth_failure,
    oauth_error_code,
    raise_for_auth_failure,
    read_json,
    send_mapped,
)
from backend.providers.session import (
    OAuthRefresh,
    TokenRejected,
    authorized,
    can_renew,
    fresh_credential,
    jwt_only_expiry,
    jwt_or_expires_in_expiry,
    renew,
    send_with_retry,
    with_fresh_token,
)
from backend.resets import claim_reset
from backend.secondary import SecondaryCache
from tests.fakes import FakeHTTP


NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return format_iso8601(dt) or ""


def _resp(status: int = 200, body: Any = None, headers: Optional[Dict[str, str]] = None) -> HTTPResponse:
    raw = b"" if body is None else json.dumps(body).encode("utf-8")
    return HTTPResponse(status=status, headers=headers or {}, body=raw)


def _jwt(claims: Dict[str, Any]) -> str:
    def seg(obj: Any) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode("utf-8")).decode("ascii").rstrip("=")

    return f"{seg({'alg': 'none'})}.{seg(claims)}.sig"


OFFLINE = HTTPTransportError("network down", offline=True)
UNREACHABLE = HTTPTransportError("tls handshake failed")


class Server:
    """Scripted fake server: `(method, url) -> [response | exception | callable]`.

    Each request pops the next scripted item; the last item repeats. Unknown
    routes answer 404. Every request is logged in order.
    """

    def __init__(self, routes: Optional[Dict[Tuple[str, str], List[Any]]] = None) -> None:
        self.routes: Dict[Tuple[str, str], List[Any]] = {k: list(v) for k, v in (routes or {}).items()}
        self.log: List[Tuple[str, str, Dict[str, Any]]] = []

    def __call__(self, method, url, kwargs):
        self.log.append((method, url, kwargs))
        script = self.routes.get((method, url))
        if not script:
            return _resp(404, {})
        item = script.pop(0) if len(script) > 1 else script[0]
        if callable(item) and not isinstance(item, HTTPResponse):
            item = item(kwargs)
        if isinstance(item, BaseException):
            raise item
        return item

    def calls(self, method: str, url: str) -> List[Dict[str, Any]]:
        return [kw for m, u, kw in self.log if m == method and u == url]


def _bearer(kwargs: Dict[str, Any]) -> str:
    auth = (kwargs.get("headers") or {}).get("Authorization", "")
    return auth[len("Bearer "):] if auth.startswith("Bearer ") else auth


# ---------------------------------------------------------------------------
# Per-provider cases
# ---------------------------------------------------------------------------


@dataclass
class Case:
    kind: str
    module: Any
    token_url: str
    usage: List[Tuple[str, str]]          # usage endpoints scripted alike
    usage_ok: Callable[[], HTTPResponse]
    refresh_body: Callable[[str], Dict[str, Any]]  # expected POST body for a refresh token
    form: bool                            # form_body vs json_body
    renewed_expiry: datetime              # expiresAt after a refresh answering TOKEN_RESPONSE
    margin_sec: float = 300.0
    auth_mode: Optional[str] = None
    extra: Dict[Tuple[str, str], List[Any]] = field(default_factory=dict)


NEW_ACCESS_EXP = NOW + timedelta(hours=2)
NEW_ACCESS = _jwt({"exp": NEW_ACCESS_EXP.timestamp(), "sub": "auth0|user_123"})
TOKEN_RESPONSE = {"access_token": NEW_ACCESS, "refresh_token": "new-refresh", "expires_in": 3600}
OLD_ACCESS = "old-access"


def _claude_usage() -> HTTPResponse:
    return _resp(200, {"five_hour": {"utilization": 10.0, "resets_at": "2026-07-13T16:00:00Z"},
                       "seven_day": {"utilization": 20.0, "resets_at": "2026-07-18T00:00:00Z"}})


def _antigravity_summary() -> HTTPResponse:
    buckets = [{"bucketId": b[0], "remainingFraction": 0.5, "resetTime": "2026-07-14T00:00:00Z"}
               for b in antigravity.SUMMARY_BUCKETS]
    return _resp(200, {"response": {"groups": [{"displayName": "Gemini", "buckets": buckets}]}})


def _opencode_usage() -> HTTPResponse:
    window = {"percent": 10, "resetsAt": "2026-07-14T00:00:00Z"}
    return _resp(200, {"usage": {"rolling": window, "weekly": window, "monthly": window}})


def _grok_usage() -> HTTPResponse:
    return _resp(200, {"config": {"currentPeriod": {
        "type": grok.WEEKLY_PERIOD_TYPE, "start": "2026-07-10T00:00:00Z", "end": "2026-07-17T00:00:00Z"}}})


CASES: List[Case] = [
    Case(
        kind="claude", module=claude, token_url=claude.CLAUDE_TOKEN_URL,
        usage=[("GET", claude.CLAUDE_USAGE_URL)], usage_ok=_claude_usage,
        refresh_body=lambda rt: {"grant_type": "refresh_token", "refresh_token": rt,
                                 "client_id": claude.CLAUDE_CLIENT_ID, "scope": "user:profile user:inference"},
        form=False, renewed_expiry=NOW + timedelta(seconds=3600),
    ),
    Case(
        kind="codex", module=codex, token_url=codex.CODEX_TOKEN_URL,
        usage=[("GET", codex.CODEX_USAGE_URL)], usage_ok=lambda: _resp(200, {"plan_type": "plus"}),
        refresh_body=lambda rt: {"grant_type": "refresh_token", "client_id": codex.CODEX_CLIENT_ID, "refresh_token": rt},
        form=True, renewed_expiry=NEW_ACCESS_EXP,
    ),
    Case(
        kind="amp", module=amp, token_url=amp.AMP_AUTHENTICATE_URL,
        usage=[("POST", amp.AMP_BALANCE_URL)],
        usage_ok=lambda: _resp(200, {"result": {"displayText": "Amp Free: $5/$10 remaining"}}),
        refresh_body=lambda rt: {"client_id": amp.AMP_CLIENT_ID, "grant_type": "refresh_token", "refresh_token": rt},
        form=False, renewed_expiry=NEW_ACCESS_EXP,
    ),
    Case(
        kind="antigravity", module=antigravity, token_url=antigravity.ANTIGRAVITY_TOKEN_URL,
        usage=[("POST", base + antigravity.QUOTA_SUMMARY_PATH) for base in antigravity.QUOTA_SUMMARY_URLS],
        usage_ok=_antigravity_summary,
        refresh_body=lambda rt: {"client_id": antigravity.ANTIGRAVITY_CLIENT_ID,
                                 "client_secret": antigravity.ANTIGRAVITY_CLIENT_SECRET,
                                 "refresh_token": rt, "grant_type": "refresh_token"},
        form=True, renewed_expiry=NOW + timedelta(seconds=3600),
    ),
    Case(
        kind="grok", module=grok, token_url=grok.TOKEN_URL,
        usage=[("GET", grok.CREDITS_CONFIG_URL)], usage_ok=_grok_usage,
        refresh_body=lambda rt: {"grant_type": "refresh_token", "client_id": grok.CLIENT_ID, "refresh_token": rt},
        form=True, renewed_expiry=NOW + timedelta(seconds=3600),
    ),
    Case(
        kind="opencode", module=opencode, token_url=opencode.TOKEN_URL,
        usage=[("GET", opencode.USAGE_URL)], usage_ok=_opencode_usage,
        refresh_body=lambda rt: {"client_id": opencode.CLIENT_ID, "grant_type": "refresh_token", "refresh_token": rt},
        form=True, renewed_expiry=NOW + timedelta(seconds=3600), auth_mode="oauth",
    ),
    Case(
        kind="cursor", module=cursor, token_url=cursor.CURSOR_REFRESH_URL,
        usage=[("POST", cursor.CURSOR_USAGE_URL)],
        usage_ok=lambda: _resp(200, {"planUsage": {"limit": 2000, "remaining": 1500, "totalPercentUsed": 25}}),
        refresh_body=lambda rt: {"grant_type": "refresh_token", "client_id": cursor.CURSOR_CLIENT_ID, "refresh_token": rt},
        form=False, renewed_expiry=NEW_ACCESS_EXP, margin_sec=7 * 86400.0,
    ),
]


class Run:
    """One provider fetch against a scripted server."""

    def __init__(self, case: Case, *, credential: Dict[str, Any], usage: List[Any],
                 token: Optional[List[Any]] = None, hub: bool = False) -> None:
        self.case = case
        routes: Dict[Tuple[str, str], List[Any]] = dict(case.extra)
        for route in case.usage:
            routes[route] = list(usage)
        routes[("POST", case.token_url)] = list(token or [_resp(200, TOKEN_RESPONSE)])
        self.server = Server(routes)
        if hub:
            account = Account.create(case.kind, "Hub", hub_id="hub-1", auth_mode="hub")
        else:
            account = Account.create(case.kind, "Personal", auth_mode=case.auth_mode)
        self.saved: List[Dict[str, Any]] = []
        self.ctx = FetchContext(
            account=account,
            credential=dict(credential),
            http=FakeHTTP(self.server),
            now=NOW,
            secondary_cache=SecondaryCache(),
            save_credential=None if hub else self.saved.append,
        )

    def fetch(self):
        return self.case.module.fetch(self.ctx)

    def refresh_posts(self) -> List[Dict[str, Any]]:
        return self.server.calls("POST", self.case.token_url)

    def usage_tokens(self) -> List[str]:
        return [_bearer(kw) for m, u, kw in self.server.log if (m, u) == self.case.usage[0]]


def _credential(expires_in_sec: float, *, refresh: Optional[str] = "old-refresh", access: str = OLD_ACCESS) -> Dict[str, Any]:
    cred: Dict[str, Any] = {"type": "oauth", "accessToken": access, "expiresAt": _iso(NOW + timedelta(seconds=expires_in_sec))}
    if refresh is not None:
        cred["refreshToken"] = refresh
    return cred


def _far(case: Case) -> float:
    """Seconds to expiry well outside the provider's renewal margin."""
    return case.margin_sec + 3600.0


class TestOAuthProvidersCharacterization(unittest.TestCase):
    def test_token_inside_margin_is_renewed_before_reading(self) -> None:
        for case in CASES:
            with self.subTest(provider=case.kind):
                run = Run(case, credential=_credential(60), usage=[case.usage_ok()])
                snap = run.fetch()
                self.assertEqual(snap.account_id, run.ctx.account.id)
                posts = run.refresh_posts()
                self.assertEqual(len(posts), 1)
                body_key = "form_body" if case.form else "json_body"
                self.assertEqual(posts[0][body_key], case.refresh_body("old-refresh"))
                self.assertEqual(len(run.saved), 1)
                saved = run.saved[0]
                self.assertEqual(saved["accessToken"], NEW_ACCESS)
                self.assertEqual(saved["refreshToken"], "new-refresh")
                self.assertEqual(parse_iso8601(saved["expiresAt"]), case.renewed_expiry)
                self.assertEqual(run.ctx.credential, saved)
                self.assertEqual(run.usage_tokens(), [NEW_ACCESS])

    def test_token_outside_margin_is_not_renewed(self) -> None:
        for case in CASES:
            with self.subTest(provider=case.kind):
                run = Run(case, credential=_credential(_far(case)), usage=[case.usage_ok()])
                run.fetch()
                self.assertEqual(run.refresh_posts(), [])
                self.assertEqual(run.saved, [])
                self.assertEqual(run.usage_tokens(), [OLD_ACCESS])

    def test_failed_renewal_keeps_unexpired_token(self) -> None:
        for case in CASES:
            for failure in (OFFLINE, _resp(503, {})):
                with self.subTest(provider=case.kind, failure=repr(failure)):
                    run = Run(case, credential=_credential(60), usage=[case.usage_ok()], token=[failure])
                    run.fetch()
                    self.assertEqual(len(run.refresh_posts()), 1)
                    self.assertEqual(run.saved, [])
                    self.assertEqual(run.usage_tokens(), [OLD_ACCESS])

    def test_failed_renewal_of_expired_token_raises(self) -> None:
        for case in CASES:
            for failure, kind in ((OFFLINE, "offline"), (UNREACHABLE, "unreachable"), (_resp(503, {}), "refused")):
                with self.subTest(provider=case.kind, kind=kind):
                    run = Run(case, credential=_credential(-60), usage=[case.usage_ok()], token=[failure])
                    with self.assertRaises(ProviderError) as cm:
                        run.fetch()
                    self.assertEqual(cm.exception.kind, kind)
                    self.assertEqual(run.usage_tokens(), [])

    def test_terminal_oauth_code_is_auth_expired(self) -> None:
        for case in CASES:
            for code in ("invalid_grant", "refresh_token_expired", "refresh_token_reused", "refresh_token_invalidated"):
                with self.subTest(provider=case.kind, code=code):
                    run = Run(case, credential=_credential(-60), usage=[case.usage_ok()],
                              token=[_resp(400, {"error": code})])
                    with self.assertRaises(ProviderError) as cm:
                        run.fetch()
                    self.assertEqual(cm.exception.kind, "auth_expired")
                    self.assertEqual(cm.exception.status_code, 400)

    def test_non_terminal_400_is_refused(self) -> None:
        for case in CASES:
            with self.subTest(provider=case.kind):
                run = Run(case, credential=_credential(-60), usage=[case.usage_ok()],
                          token=[_resp(400, {"error": "temporarily_unavailable"})])
                with self.assertRaises(ProviderError) as cm:
                    run.fetch()
                self.assertEqual(cm.exception.kind, "refused")

    def test_invalid_token_response_is_refused(self) -> None:
        for case in CASES:
            for body in ({"token_type": "bearer"}, ["x"]):
                with self.subTest(provider=case.kind, body=body):
                    run = Run(case, credential=_credential(-60), usage=[case.usage_ok()], token=[_resp(200, body)])
                    with self.assertRaises(ProviderError) as cm:
                        run.fetch()
                    self.assertEqual(cm.exception.kind, "refused")
                    self.assertEqual(run.saved, [])

    def test_unauthorized_read_renews_once_and_retries(self) -> None:
        for case in CASES:
            with self.subTest(provider=case.kind):
                run = Run(case, credential=_credential(_far(case)), usage=[_resp(401, {}), case.usage_ok()])
                run.fetch()
                self.assertEqual(len(run.refresh_posts()), 1)
                self.assertEqual(run.usage_tokens(), [OLD_ACCESS, NEW_ACCESS])
                self.assertEqual(run.saved[0]["accessToken"], NEW_ACCESS)

    def test_unauthorized_twice_is_auth_expired(self) -> None:
        for case in CASES:
            with self.subTest(provider=case.kind):
                run = Run(case, credential=_credential(_far(case)), usage=[_resp(401, {})])
                with self.assertRaises(ProviderError) as cm:
                    run.fetch()
                self.assertEqual(cm.exception.kind, "auth_expired")
                self.assertEqual(cm.exception.status_code, 401)
                self.assertEqual(len(run.refresh_posts()), 1)
                self.assertEqual(run.usage_tokens(), [OLD_ACCESS, NEW_ACCESS])

    def test_unauthorized_without_refresh_token_is_auth_expired(self) -> None:
        for case in CASES:
            with self.subTest(provider=case.kind):
                run = Run(case, credential=_credential(_far(case), refresh=None), usage=[_resp(401, {})])
                with self.assertRaises(ProviderError) as cm:
                    run.fetch()
                self.assertEqual(cm.exception.kind, "auth_expired")
                self.assertEqual(run.refresh_posts(), [])
                self.assertEqual(run.usage_tokens(), [OLD_ACCESS])

    def test_expiring_token_without_refresh_token_is_used_as_is(self) -> None:
        for case in CASES:
            with self.subTest(provider=case.kind):
                run = Run(case, credential=_credential(60, refresh=None), usage=[case.usage_ok()])
                run.fetch()
                self.assertEqual(run.refresh_posts(), [])
                self.assertEqual(run.usage_tokens(), [OLD_ACCESS])

    def test_transport_error_on_read_maps_to_offline_or_unreachable(self) -> None:
        for case in CASES:
            for exc, kind in ((OFFLINE, "offline"), (UNREACHABLE, "unreachable")):
                with self.subTest(provider=case.kind, kind=kind):
                    run = Run(case, credential=_credential(_far(case)), usage=[exc])
                    with self.assertRaises(ProviderError) as cm:
                        run.fetch()
                    self.assertEqual(cm.exception.kind, kind)

    def test_hub_account_never_renews(self) -> None:
        for case in CASES:
            with self.subTest(provider=case.kind):
                run = Run(case, credential=hub_account_credential(), usage=[case.usage_ok()], hub=True)
                run.fetch()
                self.assertEqual(run.refresh_posts(), [])

                run = Run(case, credential=hub_account_credential(), usage=[_resp(401, {})], hub=True)
                with self.assertRaises(ProviderError) as cm:
                    run.fetch()
                self.assertEqual(cm.exception.kind, "auth_expired")
                self.assertEqual(run.refresh_posts(), [])

    def test_hub_account_with_expiring_credential_never_renews(self) -> None:
        for case in CASES:
            with self.subTest(provider=case.kind):
                run = Run(case, credential=_credential(60), usage=[case.usage_ok()], hub=True)
                run.fetch()
                self.assertEqual(run.refresh_posts(), [])
                self.assertEqual(run.usage_tokens(), [OLD_ACCESS])


class TestProviderSpecificRenewal(unittest.TestCase):
    def _case(self, kind: str) -> Case:
        return next(c for c in CASES if c.kind == kind)

    def test_claude_invalid_scope_falls_back_to_profile_scope(self) -> None:
        case = self._case("claude")
        run = Run(case, credential=_credential(60), usage=[case.usage_ok()],
                  token=[_resp(400, {"error": "invalid_scope"}), _resp(200, TOKEN_RESPONSE)])
        run.fetch()
        scopes = [kw["json_body"]["scope"] for kw in run.refresh_posts()]
        self.assertEqual(scopes, ["user:profile user:inference", "user:profile"])
        self.assertEqual(run.usage_tokens(), [NEW_ACCESS])

    def test_claude_codex_amp_renewal_without_expiry_uses_one_hour_fallback(self) -> None:
        for kind in ("claude", "codex", "amp"):
            with self.subTest(provider=kind):
                case = self._case(kind)
                cred = _credential(60)
                run = Run(case, credential=cred, usage=[case.usage_ok()], token=[_resp(200, {"access_token": "plain"})])
                run.fetch()
                self.assertEqual(parse_iso8601(run.saved[0]["expiresAt"]), NOW + timedelta(seconds=3600))
                self.assertEqual(run.saved[0]["refreshToken"], "old-refresh")

    def test_grok_and_opencode_drop_unknown_expiry(self) -> None:
        for kind in ("grok", "opencode"):
            with self.subTest(provider=kind):
                case = self._case(kind)
                run = Run(case, credential=_credential(60), usage=[case.usage_ok()],
                          token=[_resp(200, {"access_token": " plain "})])
                run.fetch()
                self.assertNotIn("expiresAt", run.saved[0])
                self.assertEqual(run.saved[0]["accessToken"], "plain")

    def test_grok_and_opencode_without_access_token_are_auth_expired(self) -> None:
        for kind in ("grok", "opencode"):
            with self.subTest(provider=kind):
                case = self._case(kind)
                run = Run(case, credential=_credential(_far(case), access="  "), usage=[case.usage_ok()])
                with self.assertRaises(ProviderError) as cm:
                    run.fetch()
                self.assertEqual(cm.exception.kind, "auth_expired")
                self.assertEqual(run.usage_tokens(), [])

    def test_codex_and_amp_fall_back_to_expires_in_without_jwt(self) -> None:
        for kind in ("codex", "amp"):
            with self.subTest(provider=kind):
                case = self._case(kind)
                run = Run(case, credential=_credential(60), usage=[case.usage_ok()],
                          token=[_resp(200, {"access_token": "plain", "expires_in": 600})])
                run.fetch()
                self.assertEqual(parse_iso8601(run.saved[0]["expiresAt"]), NOW + timedelta(seconds=600))

    def test_antigravity_defaults_expires_in_to_one_hour(self) -> None:
        case = self._case("antigravity")
        run = Run(case, credential=_credential(60), usage=[case.usage_ok()], token=[_resp(200, {"access_token": "plain"})])
        run.fetch()
        self.assertEqual(parse_iso8601(run.saved[0]["expiresAt"]), NOW + timedelta(seconds=3600))

    def test_antigravity_quota_falls_back_across_urls(self) -> None:
        case = self._case("antigravity")
        run = Run(case, credential=_credential(_far(case)), usage=[case.usage_ok()])
        first, second = case.usage
        run.server.routes[first] = [OFFLINE]
        snap = run.fetch()
        self.assertTrue(snap.lines)
        self.assertEqual(len(run.server.calls(*second)), 1)

    def test_cursor_renews_seven_days_early(self) -> None:
        case = self._case("cursor")
        run = Run(case, credential=_credential(3 * 86400), usage=[case.usage_ok()])
        run.fetch()
        self.assertEqual(len(run.refresh_posts()), 1)
        claude_case = self._case("claude")
        run = Run(claude_case, credential=_credential(3 * 86400), usage=[claude_case.usage_ok()])
        run.fetch()
        self.assertEqual(run.refresh_posts(), [])

    def test_cursor_reads_current_expiry_from_jwt(self) -> None:
        case = self._case("cursor")
        access = _jwt({"exp": (NOW + timedelta(days=1)).timestamp(), "sub": "auth0|user_1"})
        run = Run(case, credential={"accessToken": access, "refreshToken": "old-refresh"}, usage=[case.usage_ok()])
        run.fetch()
        self.assertEqual(len(run.refresh_posts()), 1)
        # Other providers need `expiresAt` to renew proactively
        codex_case = self._case("codex")
        run = Run(codex_case, credential={"accessToken": access, "refreshToken": "old-refresh"}, usage=[codex_case.usage_ok()])
        run.fetch()
        self.assertEqual(run.refresh_posts(), [])

    def test_cursor_should_logout_is_auth_expired(self) -> None:
        case = self._case("cursor")
        run = Run(case, credential=_credential(-60), usage=[case.usage_ok()],
                  token=[_resp(200, {"shouldLogout": True, "access_token": NEW_ACCESS})])
        with self.assertRaises(ProviderError) as cm:
            run.fetch()
        self.assertEqual(cm.exception.kind, "auth_expired")
        self.assertEqual(cm.exception.status_code, 200)

    def test_cursor_refresh_token_falls_back_to_access_token(self) -> None:
        case = self._case("cursor")
        run = Run(case, credential=_credential(60), usage=[case.usage_ok()], token=[_resp(200, {"access_token": NEW_ACCESS})])
        run.fetch()
        self.assertEqual(run.saved[0]["refreshToken"], NEW_ACCESS)

    def test_cursor_renewal_without_jwt_expiry_drops_stale_expiry(self) -> None:
        case = self._case("cursor")
        cred = _credential(60)
        run = Run(case, credential=cred, usage=[case.usage_ok()],
                  token=[_resp(200, {"access_token": "plain", "expires_in": 600})])
        run.fetch()
        self.assertNotIn("expiresAt", run.saved[0])

    def test_cursor_renews_on_auth_failure_403(self) -> None:
        case = self._case("cursor")
        run = Run(case, credential=_credential(_far(case)), usage=[_resp(403, {}), case.usage_ok()])
        run.fetch()
        self.assertEqual(run.usage_tokens(), [OLD_ACCESS, NEW_ACCESS])

    def test_rate_limited_403_is_not_an_auth_failure(self) -> None:
        for kind in ("codex", "amp", "cursor"):
            with self.subTest(provider=kind):
                case = self._case(kind)
                run = Run(case, credential=_credential(_far(case)), usage=[_resp(403, {}, {"retry-after": "30"})])
                with self.assertRaises(ProviderError) as cm:
                    run.fetch()
                self.assertEqual(cm.exception.kind, "refused")
                self.assertEqual(run.refresh_posts(), [])

    def test_auth_failure_403_is_auth_expired_without_renewal(self) -> None:
        for kind in ("claude", "codex", "amp"):
            with self.subTest(provider=kind):
                case = self._case(kind)
                run = Run(case, credential=_credential(_far(case)), usage=[_resp(403, {})])
                with self.assertRaises(ProviderError) as cm:
                    run.fetch()
                self.assertEqual(cm.exception.kind, "auth_expired")
                self.assertEqual(cm.exception.status_code, 403)
                self.assertEqual(run.refresh_posts(), [])

    def test_invalid_usage_json_is_refused(self) -> None:
        for case in CASES:
            with self.subTest(provider=case.kind):
                run = Run(case, credential=_credential(_far(case)),
                          usage=[HTTPResponse(status=200, headers={}, body=b"not json")])
                with self.assertRaises(ProviderError) as cm:
                    run.fetch()
                self.assertEqual(cm.exception.kind, "refused")


# ---------------------------------------------------------------------------
# Reset claims (Claude and Codex) through `claim_reset`
# ---------------------------------------------------------------------------

ORG_UUID = "6f1c3f0e-6b7a-4b0e-9a55-1d2f3c4b5a69"
CLAUDE_CLAIM_URL = f"https://api.anthropic.com/api/organizations/{ORG_UUID}/reset_rate_limits"
CLAUDE_ITEM = {"providerKind": "claude", "id": "g1", "kind": "session", "requestId": "req-1",
               "claimPayload": {"grant_id": "g1"}}
CODEX_EXPIRY = _iso(NOW + timedelta(days=3))
CODEX_ITEM = {"providerKind": "codex", "requestId": "req-1", "expiresAt": CODEX_EXPIRY,
              "claimPayload": {"expires_at": CODEX_EXPIRY}}


def _claude_claim_server(claim: List[Any], token: Optional[List[Any]] = None) -> Server:
    grants = {"cedar_ember": {"eligible": True, "next_grant_id": "g1", "grants": [{
        "id": "g1", "resets_left": 1, "ends_at": "2026-08-01T00:00:00Z", "clears": ["five_hour"],
        "type": "session", "usable_now": True}]}}
    return Server({
        ("GET", claude.CLAUDE_RESET_GRANTS_URL): [_resp(200, grants)],
        ("GET", claude.CLAUDE_PROFILE_URL): [_resp(200, {"organization": {"uuid": ORG_UUID}})],
        ("POST", CLAUDE_CLAIM_URL): list(claim),
        ("POST", claude.CLAUDE_TOKEN_URL): list(token or [_resp(200, TOKEN_RESPONSE)]),
    })


def _codex_claim_server(listing: List[Any], consume: List[Any], token: Optional[List[Any]] = None) -> Server:
    return Server({
        ("GET", codex.CODEX_RESET_CREDITS_URL): list(listing),
        ("POST", codex.CODEX_CONSUME_RESET_CREDIT_URL): list(consume),
        ("POST", codex.CODEX_TOKEN_URL): list(token or [_resp(200, TOKEN_RESPONSE)]),
    })


CODEX_LISTING = _resp(200, {"available_count": 1, "credits": [{"id": "credit-1", "status": "available", "expires_at": CODEX_EXPIRY}]})


def _claim_ctx(kind: str, server: Server, credential: Dict[str, Any], *, hub: bool = False) -> Tuple[FetchContext, List[Dict[str, Any]]]:
    saved: List[Dict[str, Any]] = []
    account = Account.create(kind, "Hub", hub_id="hub-1", auth_mode="hub") if hub else Account.create(kind, "Personal")
    ctx = FetchContext(account=account, credential=dict(credential), http=FakeHTTP(server), now=NOW,
                       force=True, secondary_cache=SecondaryCache(),
                       save_credential=None if hub else saved.append)
    return ctx, saved


class TestResetClaimSessions(unittest.TestCase):
    def test_claude_claim_renews_expiring_token_first(self) -> None:
        server = _claude_claim_server([_resp(200, {"result": "reset"})])
        ctx, saved = _claim_ctx("claude", server, _credential(60))
        self.assertEqual(claim_reset(ctx, CLAUDE_ITEM)["outcome"], "success")
        self.assertEqual(len(server.calls("POST", claude.CLAUDE_TOKEN_URL)), 1)
        self.assertEqual([_bearer(kw) for kw in server.calls("POST", CLAUDE_CLAIM_URL)], [NEW_ACCESS])
        self.assertEqual(saved[0]["accessToken"], NEW_ACCESS)

    def test_claude_claim_keeps_unexpired_token_when_renewal_fails(self) -> None:
        server = _claude_claim_server([_resp(200, {"result": "reset"})], token=[OFFLINE])
        ctx, _ = _claim_ctx("claude", server, _credential(60))
        self.assertEqual(claim_reset(ctx, CLAUDE_ITEM)["outcome"], "success")
        self.assertEqual([_bearer(kw) for kw in server.calls("POST", CLAUDE_CLAIM_URL)], [OLD_ACCESS])

    def test_claude_claim_with_expired_token_and_failed_renewal(self) -> None:
        for failure, outcome in ((OFFLINE, "connection"), (_resp(400, {"error": "invalid_grant"}), "sign_in_expired"),
                                 (_resp(503, {}), "refused")):
            with self.subTest(outcome=outcome):
                server = _claude_claim_server([_resp(200, {"result": "reset"})], token=[failure])
                ctx, _ = _claim_ctx("claude", server, _credential(-60))
                self.assertEqual(claim_reset(ctx, CLAUDE_ITEM)["outcome"], outcome)
                self.assertEqual(server.calls("POST", CLAUDE_CLAIM_URL), [])

    def test_claude_claim_unauthorized_renews_and_retries(self) -> None:
        server = _claude_claim_server([_resp(401, {}), _resp(200, {"result": "reset"})])
        ctx, _ = _claim_ctx("claude", server, _credential(3600))
        self.assertEqual(claim_reset(ctx, CLAUDE_ITEM)["outcome"], "success")
        self.assertEqual([_bearer(kw) for kw in server.calls("POST", CLAUDE_CLAIM_URL)], [OLD_ACCESS, NEW_ACCESS])

    def test_claude_claim_unauthorized_twice_is_not_permitted(self) -> None:
        server = _claude_claim_server([_resp(401, {})])
        ctx, _ = _claim_ctx("claude", server, _credential(3600))
        self.assertEqual(claim_reset(ctx, CLAUDE_ITEM)["outcome"], "not_permitted")
        self.assertEqual(len(server.calls("POST", CLAUDE_CLAIM_URL)), 2)

    def test_claude_claim_transport_error_is_connection_and_unsettled(self) -> None:
        server = _claude_claim_server([OFFLINE])
        ctx, _ = _claim_ctx("claude", server, _credential(3600))
        self.assertEqual(claim_reset(ctx, CLAUDE_ITEM)["outcome"], "connection")

    def test_claude_hub_claim_never_renews(self) -> None:
        server = _claude_claim_server([_resp(401, {})])
        ctx, _ = _claim_ctx("claude", server, _credential(60), hub=True)
        self.assertEqual(claim_reset(ctx, CLAUDE_ITEM)["outcome"], "sign_in_expired")
        self.assertEqual(server.calls("POST", claude.CLAUDE_TOKEN_URL), [])
        self.assertEqual(len(server.calls("POST", CLAUDE_CLAIM_URL)), 1)

    def test_codex_claim_renews_expiring_token_first(self) -> None:
        server = _codex_claim_server([CODEX_LISTING], [_resp(200, {"code": "reset"})])
        ctx, _ = _claim_ctx("codex", server, _credential(60))
        self.assertEqual(claim_reset(ctx, CODEX_ITEM)["outcome"], "success")
        self.assertEqual(len(server.calls("POST", codex.CODEX_TOKEN_URL)), 1)
        self.assertEqual([_bearer(kw) for kw in server.calls("POST", codex.CODEX_CONSUME_RESET_CREDIT_URL)], [NEW_ACCESS])

    def test_codex_claim_expired_token_failed_renewal(self) -> None:
        server = _codex_claim_server([CODEX_LISTING], [_resp(200, {"code": "reset"})], token=[UNREACHABLE])
        ctx, _ = _claim_ctx("codex", server, _credential(-60))
        self.assertEqual(claim_reset(ctx, CODEX_ITEM)["outcome"], "connection")
        self.assertEqual(server.calls("GET", codex.CODEX_RESET_CREDITS_URL), [])

    def test_codex_claim_list_unauthorized_renews_and_retries(self) -> None:
        server = _codex_claim_server([_resp(401, {}), CODEX_LISTING], [_resp(200, {"code": "reset"})])
        ctx, _ = _claim_ctx("codex", server, _credential(3600))
        self.assertEqual(claim_reset(ctx, CODEX_ITEM)["outcome"], "success")
        self.assertEqual([_bearer(kw) for kw in server.calls("GET", codex.CODEX_RESET_CREDITS_URL)], [OLD_ACCESS, NEW_ACCESS])
        consume = server.calls("POST", codex.CODEX_CONSUME_RESET_CREDIT_URL)
        self.assertEqual([_bearer(kw) for kw in consume], [NEW_ACCESS])
        self.assertEqual(consume[0]["headers"]["Content-Type"], "application/json")

    def test_codex_claim_consume_unauthorized_renews_and_retries(self) -> None:
        server = _codex_claim_server([CODEX_LISTING], [_resp(401, {}), _resp(200, {"code": "reset"})])
        ctx, _ = _claim_ctx("codex", server, _credential(3600))
        self.assertEqual(claim_reset(ctx, CODEX_ITEM)["outcome"], "success")
        consume = server.calls("POST", codex.CODEX_CONSUME_RESET_CREDIT_URL)
        self.assertEqual([_bearer(kw) for kw in consume], [OLD_ACCESS, NEW_ACCESS])
        self.assertEqual(consume[1]["headers"]["Content-Type"], "application/json")
        self.assertEqual(consume[0]["json_body"], consume[1]["json_body"])

    def test_codex_claim_auth_failures_are_sign_in_expired(self) -> None:
        for listing in ([_resp(401, {})], [_resp(403, {})]):
            with self.subTest(status=listing[0].status):
                server = _codex_claim_server(listing, [_resp(200, {"code": "reset"})])
                ctx, _ = _claim_ctx("codex", server, _credential(3600))
                self.assertEqual(claim_reset(ctx, CODEX_ITEM)["outcome"], "sign_in_expired")

    def test_codex_claim_transport_error_is_connection(self) -> None:
        server = _codex_claim_server([CODEX_LISTING], [OFFLINE])
        ctx, _ = _claim_ctx("codex", server, _credential(3600))
        self.assertEqual(claim_reset(ctx, CODEX_ITEM)["outcome"], "connection")

    def test_codex_hub_claim_never_renews(self) -> None:
        server = _codex_claim_server([_resp(401, {})], [_resp(200, {"code": "reset"})])
        ctx, _ = _claim_ctx("codex", server, _credential(60), hub=True)
        self.assertEqual(claim_reset(ctx, CODEX_ITEM)["outcome"], "sign_in_expired")
        self.assertEqual(server.calls("POST", codex.CODEX_TOKEN_URL), [])


# ---------------------------------------------------------------------------
# The engine itself, with a test spec
# ---------------------------------------------------------------------------

TEST_TOKEN_URL = "https://auth.example.test/token"
TEST_READ_URL = "https://api.example.test/usage"
SPEC = OAuthRefresh(provider_name="Example", token_url=TEST_TOKEN_URL, client_id="client-1")


class Engine:
    def __init__(self, *, credential: Dict[str, Any], token: Optional[List[Any]] = None,
                 read: Optional[List[Any]] = None, hub: bool = False) -> None:
        self.server = Server({
            ("POST", TEST_TOKEN_URL): list(token or [_resp(200, TOKEN_RESPONSE)]),
            ("GET", TEST_READ_URL): list(read or [_resp(200, {})]),
        })
        account = Account.create("claude", "Hub", hub_id="hub-1", auth_mode="hub") if hub else Account.create("claude", "x")
        self.saved: List[Dict[str, Any]] = []
        self.ctx = FetchContext(account=account, credential=dict(credential), http=FakeHTTP(self.server), now=NOW,
                                secondary_cache=SecondaryCache(), save_credential=self.saved.append)

    def send(self, token: str) -> HTTPResponse:
        return self.ctx.http.get(TEST_READ_URL, headers={"Authorization": f"Bearer {token}"})

    def posts(self) -> List[Dict[str, Any]]:
        return self.server.calls("POST", TEST_TOKEN_URL)

    def read_tokens(self) -> List[str]:
        return [_bearer(kw) for kw in self.server.calls("GET", TEST_READ_URL)]


class TestSessionEngine(unittest.TestCase):
    def test_renew_posts_spec_fields_and_persists(self) -> None:
        spec = OAuthRefresh(provider_name="Example", token_url=TEST_TOKEN_URL, client_id="client-1", form=True,
                            extra_fields={"client_secret": "s"}, headers={"Accept": "application/json"})
        eng = Engine(credential=_credential(60, refresh=" old-refresh "))
        updated = renew(eng.ctx, spec)
        post = eng.posts()[0]
        self.assertEqual(post["form_body"], {"grant_type": "refresh_token", "client_id": "client-1",
                                             "refresh_token": "old-refresh", "client_secret": "s"})
        self.assertEqual(post["headers"], {"Accept": "application/json"})
        self.assertNotIn("json_body", post)
        self.assertEqual(updated["accessToken"], NEW_ACCESS)
        self.assertEqual(updated["refreshToken"], "new-refresh")
        self.assertEqual(parse_iso8601(updated["expiresAt"]), NOW + timedelta(seconds=3600))
        self.assertEqual(eng.saved, [updated])
        self.assertIs(eng.ctx.credential, updated)

    def test_renew_without_refresh_token_or_for_hub_is_a_no_op(self) -> None:
        for cred, hub in ((_credential(60, refresh=None), False), (_credential(60), True)):
            eng = Engine(credential=cred, hub=hub)
            self.assertEqual(renew(eng.ctx, SPEC), cred)
            self.assertEqual(eng.posts(), [])
            self.assertFalse(can_renew(eng.ctx))

    def test_fresh_credential_respects_margin(self) -> None:
        spec = OAuthRefresh(provider_name="Example", token_url=TEST_TOKEN_URL, client_id="c", margin_sec=600)
        for seconds, renews in ((599, True), (601, False), (-5, True)):
            with self.subTest(seconds=seconds):
                eng = Engine(credential=_credential(seconds))
                cred = fresh_credential(eng.ctx, spec)
                self.assertEqual(len(eng.posts()), 1 if renews else 0)
                self.assertEqual(cred["accessToken"], NEW_ACCESS if renews else OLD_ACCESS)

    def test_fresh_credential_without_expiry_is_kept(self) -> None:
        eng = Engine(credential={"accessToken": OLD_ACCESS, "refreshToken": "r"})
        self.assertEqual(fresh_credential(eng.ctx, SPEC)["accessToken"], OLD_ACCESS)
        self.assertEqual(eng.posts(), [])

    def test_expiry_from_jwt_reads_the_current_token(self) -> None:
        spec = OAuthRefresh(provider_name="Example", token_url=TEST_TOKEN_URL, client_id="c", expiry_from_jwt=True)
        access = _jwt({"exp": (NOW + timedelta(seconds=60)).timestamp()})
        eng = Engine(credential={"accessToken": access, "refreshToken": "r"})
        self.assertEqual(fresh_credential(eng.ctx, spec)["accessToken"], NEW_ACCESS)

    def test_failed_renewal_keeps_unexpired_token_and_raises_for_expired(self) -> None:
        for failure, kind in ((OFFLINE, "offline"), (UNREACHABLE, "unreachable"), (_resp(500, {}), "refused"),
                              (_resp(400, {"error": "invalid_grant"}), "auth_expired")):
            with self.subTest(kind=kind):
                eng = Engine(credential=_credential(60), token=[failure])
                self.assertEqual(fresh_credential(eng.ctx, SPEC)["accessToken"], OLD_ACCESS)
                eng = Engine(credential=_credential(0), token=[failure])
                with self.assertRaises(ProviderError) as cm:
                    fresh_credential(eng.ctx, SPEC)
                self.assertEqual(cm.exception.kind, kind)
                self.assertEqual(eng.saved, [])

    def test_terminal_codes_and_hook_are_auth_expired(self) -> None:
        spec = OAuthRefresh(provider_name="Example", token_url=TEST_TOKEN_URL, client_id="c",
                            expired_message="Gone.", is_terminal=lambda resp, body: isinstance(body, dict) and body.get("logout") is True)
        cases = [_resp(401, {"error": {"code": " refresh_token_reused "}}), _resp(403, {"code": "refresh_token_expired"}),
                 _resp(200, {"logout": True, "access_token": "x"})]
        for resp in cases:
            with self.subTest(status=resp.status):
                eng = Engine(credential=_credential(-1), token=[resp])
                with self.assertRaises(ProviderError) as cm:
                    renew(eng.ctx, spec)
                self.assertEqual((cm.exception.kind, str(cm.exception), cm.exception.status_code), ("auth_expired", "Gone.", resp.status))
        eng = Engine(credential=_credential(-1), token=[_resp(400, {"error": "invalid_grant"})])
        with self.assertRaises(ProviderError) as cm:
            renew(eng.ctx, SPEC)
        self.assertEqual(str(cm.exception), "Example sign-in expired. Sign in again to keep reading usage.")

    def test_invalid_token_responses_are_refused(self) -> None:
        bodies = [HTTPResponse(200, {}, b"not json"), _resp(200, []), _resp(200, {"access_token": "  "})]
        for resp in bodies:
            with self.subTest(body=resp.body):
                eng = Engine(credential=_credential(-1), token=[resp])
                with self.assertRaises(ProviderError) as cm:
                    renew(eng.ctx, SPEC)
                self.assertEqual((cm.exception.kind, str(cm.exception)), ("refused", "Example returned an invalid token response."))

    def test_retry_fields_resend_once(self) -> None:
        spec = OAuthRefresh(provider_name="Example", token_url=TEST_TOKEN_URL, client_id="c", extra_fields={"scope": "a b"},
                            retry_fields=lambda resp: {"scope": "a"} if resp.status == 400 else None)
        eng = Engine(credential=_credential(-1), token=[_resp(400, {"error": "invalid_scope"}), _resp(200, TOKEN_RESPONSE)])
        renew(eng.ctx, spec)
        self.assertEqual([kw["json_body"]["scope"] for kw in eng.posts()], ["a b", "a"])

    def test_expiry_policies(self) -> None:
        plain = {"access_token": "plain", "expires_in": 600}
        jwt_payload = {"access_token": NEW_ACCESS, "expires_in": 600}
        self.assertEqual(jwt_or_expires_in_expiry(plain, "plain", NOW), NOW + timedelta(seconds=600))
        self.assertEqual(jwt_or_expires_in_expiry(jwt_payload, NEW_ACCESS, NOW), NEW_ACCESS_EXP)
        self.assertIsNone(jwt_only_expiry(plain, "plain", NOW))

        cred = _credential(60)
        for drop, expected in ((False, cred["expiresAt"]), (True, None)):
            spec = OAuthRefresh(provider_name="Example", token_url=TEST_TOKEN_URL, client_id="c", drop_unknown_expiry=drop)
            eng = Engine(credential=cred, token=[_resp(200, {"access_token": "plain"})])
            self.assertEqual(renew(eng.ctx, spec).get("expiresAt"), expected)

    def test_refresh_token_fallback_to_access(self) -> None:
        for fallback, expected in ((False, "old-refresh"), (True, "plain")):
            spec = OAuthRefresh(provider_name="Example", token_url=TEST_TOKEN_URL, client_id="c",
                                refresh_token_fallback_to_access=fallback)
            eng = Engine(credential=_credential(60), token=[_resp(200, {"access_token": "plain"})])
            self.assertEqual(renew(eng.ctx, spec)["refreshToken"], expected)

    def test_authorized_renews_once_on_401(self) -> None:
        eng = Engine(credential=_credential(3600), read=[_resp(401, {}), _resp(200, {})])
        cred, resp = authorized(eng.ctx, SPEC, eng.send)
        self.assertEqual((resp.status, cred["accessToken"]), (200, NEW_ACCESS))
        self.assertEqual(eng.read_tokens(), [OLD_ACCESS, NEW_ACCESS])

        eng = Engine(credential=_credential(3600), read=[_resp(401, {})])
        _, resp = authorized(eng.ctx, SPEC, eng.send)
        self.assertEqual(resp.status, 401)
        self.assertEqual(len(eng.posts()), 1)
        self.assertEqual(len(eng.read_tokens()), 2)

    def test_authorized_retry_when(self) -> None:
        eng = Engine(credential=_credential(3600), read=[_resp(403, {}), _resp(200, {})])
        _, resp = authorized(eng.ctx, SPEC, eng.send)
        self.assertEqual((resp.status, eng.posts()), (403, []))
        eng = Engine(credential=_credential(3600), read=[_resp(403, {}), _resp(200, {})])
        _, resp = authorized(eng.ctx, SPEC, eng.send, retry_when=is_auth_failure)
        self.assertEqual(resp.status, 200)

    def test_authorized_never_renews_hub_accounts(self) -> None:
        eng = Engine(credential=_credential(-60), read=[_resp(401, {})], hub=True)
        _, resp = authorized(eng.ctx, SPEC, eng.send)
        self.assertEqual((resp.status, eng.posts()), (401, []))

    def test_authorized_maps_transport_errors(self) -> None:
        for exc, kind in ((OFFLINE, "offline"), (UNREACHABLE, "unreachable")):
            eng = Engine(credential=_credential(3600), read=[exc])
            with self.assertRaises(ProviderError) as cm:
                authorized(eng.ctx, SPEC, eng.send)
            self.assertEqual(cm.exception.kind, kind)

    def test_send_with_retry_skips_the_freshness_check(self) -> None:
        eng = Engine(credential=_credential(60), read=[_resp(200, {})])
        cred, _ = send_with_retry(eng.ctx, SPEC, eng.ctx.credential, eng.send)
        self.assertEqual((eng.posts(), cred["accessToken"]), ([], OLD_ACCESS))

    def test_with_fresh_token(self) -> None:
        def reader(results: List[Any]):
            def read(token: str) -> str:
                item = results.pop(0)
                if item is TokenRejected:
                    raise TokenRejected()
                return f"{item}:{token}"
            return read

        eng = Engine(credential=_credential(3600))
        self.assertEqual(with_fresh_token(eng.ctx, SPEC, reader([TokenRejected, "ok"])), f"ok:{NEW_ACCESS}")
        eng = Engine(credential=_credential(3600))
        with self.assertRaises(ProviderError) as cm:
            with_fresh_token(eng.ctx, SPEC, reader([TokenRejected, TokenRejected]))
        self.assertEqual((cm.exception.kind, cm.exception.status_code, str(cm.exception)),
                         ("auth_expired", 401, "Sign-in expired for Example. Sign in again."))
        self.assertEqual(len(eng.posts()), 1)
        eng = Engine(credential=_credential(3600, refresh=None))
        with self.assertRaises(ProviderError):
            with_fresh_token(eng.ctx, SPEC, reader([TokenRejected]))
        self.assertEqual(eng.posts(), [])


class TestBaseErrorMapping(unittest.TestCase):
    def test_send_mapped(self) -> None:
        self.assertEqual(send_mapped(lambda: 7), 7)
        for exc, kind in ((OFFLINE, "offline"), (UNREACHABLE, "unreachable")):
            with self.assertRaises(ProviderError) as cm:
                send_mapped(lambda: (_ for _ in ()).throw(exc))
            self.assertEqual(cm.exception.kind, kind)
            self.assertIs(cm.exception.__cause__, exc)

    def test_read_json(self) -> None:
        self.assertEqual(read_json(_resp(200, {"a": 1}), "X"), {"a": 1})
        self.assertEqual(read_json(_resp(200, [1]), "X", expect=None), [1])
        cases = [
            (HTTPResponse(200, {}, b"<html>"), {}, "X returned an invalid JSON response."),
            (_resp(200, [1]), {}, "X returned an unexpected response."),
            (_resp(200, [1]), {"invalid_message": "X broke."}, "X broke."),
        ]
        for resp, kwargs, message in cases:
            with self.assertRaises(ProviderError) as cm:
                read_json(resp, "X", **kwargs)
            self.assertEqual((cm.exception.kind, str(cm.exception)), ("refused", message))

    def test_auth_failure(self) -> None:
        self.assertTrue(is_auth_failure(_resp(401)))
        self.assertTrue(is_auth_failure(_resp(403)))
        self.assertFalse(is_auth_failure(_resp(403, headers={"x-ratelimit-remaining": "0"})))
        self.assertFalse(is_auth_failure(_resp(403, headers={"retry-after": "5"})))
        self.assertFalse(is_auth_failure(_resp(429)))
        raise_for_auth_failure(_resp(401), "X")  # 401s are raise_for_http_error's job
        with self.assertRaises(ProviderError) as cm:
            raise_for_auth_failure(_resp(403), "X")
        self.assertEqual((cm.exception.kind, cm.exception.status_code), ("auth_expired", 403))

    def test_oauth_error_code(self) -> None:
        self.assertEqual(oauth_error_code(_resp(400, {"error": " invalid_grant "})), "invalid_grant")
        self.assertEqual(oauth_error_code(_resp(400, {"error": {"code": "x"}})), "x")
        self.assertEqual(oauth_error_code(_resp(400, {"error": "", "code": "y"})), "y")
        self.assertIsNone(oauth_error_code(HTTPResponse(400, {}, b"nope")))

    def test_jwt_claims(self) -> None:
        token = _jwt({"exp": 1_800_000_000, "sub": "u"})
        self.assertEqual(decode_jwt_payload(token)["sub"], "u")
        self.assertEqual(jwt_expiry(token), datetime.fromtimestamp(1_800_000_000, tz=timezone.utc))
        for bad in ("", "abc", "a.!!!.c", _jwt({"exp": "soon"}), _jwt({"exp": True}), _jwt({"exp": -1})):
            self.assertIsNone(jwt_expiry(bad))


if __name__ == "__main__":
    unittest.main()
