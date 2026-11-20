"""Provider authentication flows (OAuth + PKCE, loopback callback, device code, API key) for all 13 providers."""

from __future__ import annotations

import base64
import hashlib
import html
import queue
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any, Callable, Dict, Optional, Tuple

from backend.http_client import HTTPClient, HTTPResponse, HTTPTransportError, redact_secrets
from backend.i18n import language, tr
from backend.models import (
    PROVIDER_CATALOG,
    api_key_identity,
    format_iso8601,
    utc_now,
)
from backend.jwt_claims import decode_jwt_payload, jwt_expiry
from backend.providers.base import ProviderError, raise_for_http_error
from backend.providers.claude import (
    CLAUDE_CLIENT_ID,
    CLAUDE_TOKEN_URL,
    CLAUDE_PROFILE_URL,
    CLAUDE_SCOPES,
)
from backend.providers.codex import (
    CODEX_CLIENT_ID,
    CODEX_TOKEN_URL,
)
from backend.providers.antigravity import (
    ANTIGRAVITY_CLIENT_ID,
    ANTIGRAVITY_CLIENT_SECRET,
    ANTIGRAVITY_TOKEN_URL,
)
from backend.providers.grok import (
    CLIENT_ID as GROK_CLIENT_ID,
    TOKEN_URL as GROK_TOKEN_URL,
)
from backend.providers.amp import (
    AMP_CLIENT_ID,
    AMP_AUTHENTICATE_URL,
    AMP_BALANCE_URL,
)
from backend.providers.opencode import (
    CLIENT_ID as OPENCODE_CLIENT_ID,
    TOKEN_URL as OPENCODE_TOKEN_URL,
    USAGE_URL as OPENCODE_USAGE_URL,
    ORG_KEY as OPENCODE_ORG_KEY,
)
from backend.providers.cursor import (
    CURSOR_PROFILE_URL,
)
from backend.providers.devin import (
    DEFAULT_API_SERVER_URL as DEVIN_DEFAULT_API_SERVER_URL,
    CLOUD_COMPAT_VERSION as DEVIN_CLOUD_COMPAT_VERSION,
)
from backend.providers.muse import (
    KEY_URL as MUSE_KEY_URL,
)
from backend.providers.openrouter import (
    KEY_URL as OPENROUTER_KEY_URL,
)
from backend.providers.zai import (
    QUOTA_URL as ZAI_QUOTA_URL,
)


# ---------------------------------------------------------------------------
# Provider OAuth / Device-Code Endpoints & Public Client IDs
# ---------------------------------------------------------------------------

ACCOUNT_IDENTITY_KEY = "account_identity"

CLAUDE_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"

CODEX_AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
CODEX_SCOPES = "openid profile email offline_access"
CODEX_CALLBACK_PORT = 1455
CODEX_CALLBACK_PATH = "/auth/callback"
CODEX_DEVICE_USERCODE_URL = "https://auth.openai.com/api/accounts/deviceauth/usercode"
CODEX_DEVICE_VERIFY_URL = "https://auth.openai.com/codex/device"
CODEX_DEVICE_TOKEN_URL = "https://auth.openai.com/api/accounts/deviceauth/token"
CODEX_DEVICE_REDIRECT_URI = "https://auth.openai.com/deviceauth/callback"

ANTIGRAVITY_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
ANTIGRAVITY_SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform "
    "https://www.googleapis.com/auth/userinfo.email"
)
ANTIGRAVITY_USERINFO_URL = "https://www.googleapis.com/oauth2/v1/userinfo?alt=json"
ANTIGRAVITY_CALLBACK_PORT = 51121
ANTIGRAVITY_CALLBACK_PATH = "/oauth-callback"

COPILOT_CLIENT_ID = base64.b64decode("SXYxLmI1MDdhMDhjODdlY2ZlOTg=").decode("utf-8")
COPILOT_SCOPES = "read:user"


def _github_domain_invalid_msg() -> str:
    return tr(
        "auth.githubDomainInvalid",
        "Omameter works with GitHub Enterprise on GHE.com. Enter an address like octocorp.ghe.com.",
    )


GROK_DEVICE_START_URL = "https://auth.x.ai/oauth2/device/code"
GROK_SCOPES = "openid email offline_access grok-cli:access"

MUSE_CLIENT_ID = "1031625952748946"
MUSE_DEVICE_START_URL = "https://auth.meta.com/oidc/device/authorization/"
MUSE_TOKEN_URL = "https://auth.meta.com/oidc/device/token/"

AMP_DEVICE_START_URL = "https://authapi.ampcode.com/user_management/authorize/device"
AMP_VERIFICATION_PAGE = "https://ampcode.com/auth/cli/device"

OPENCODE_DEVICE_START_URL = "https://opencode.ai/console/auth/device/code"
OPENCODE_USER_URL = "https://opencode.ai/console/api/user"

CURSOR_AUTHORIZE_URL = "https://cursor.com/loginDeepControl"
CURSOR_POLL_URL = "https://api2.cursor.sh/auth/poll"

DEVIN_AUTHORIZE_URL = "https://app.devin.ai/auth/cli/continue"
DEVIN_TOKEN_URL = "https://api.devin.ai/auth/cli/token"
DEVIN_SESSION_TOKEN_PREFIX = "devin-session-token$"

OLLAMA_BALANCE_URL = "https://ollama.com/api/balance"
OLLAMA_ACCOUNT_URL = "https://ollama.com/api/me"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class LoginError(Exception):
    """Typed error raised during an interactive sign-in flow."""

    def __init__(self, message: str, *, code: str = "failed") -> None:
        super().__init__(redact_secrets(message))
        self.code = code


class LoginCancelled(LoginError):
    def __init__(self, message: Optional[str] = None) -> None:
        super().__init__(message or tr("auth.cancelled", "Sign-in was cancelled."), code="cancelled")


# ---------------------------------------------------------------------------
# PKCE & JWT helpers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PKCEPair:
    verifier: str
    challenge: str


def _b64url_no_pad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return _b64url_no_pad(digest)


def generate_pkce() -> PKCEPair:
    verifier = _b64url_no_pad(secrets.token_bytes(32))
    return PKCEPair(verifier=verifier, challenge=pkce_challenge(verifier))


def jwt_expiry_iso(token: str) -> Optional[str]:
    return format_iso8601(jwt_expiry(token))


def _expires_at_from_seconds(expires_in: Any) -> Optional[str]:
    if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool) and expires_in > 0:
        return format_iso8601(utc_now() + timedelta(seconds=float(expires_in)))
    return None


def _make_oauth_credential(
    access_token: str,
    *,
    refresh_token: Optional[str] = None,
    expires_at: Optional[str] = None,
    extras: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    clean_extras = {str(k): str(v) for k, v in (extras or {}).items() if v is not None and str(v) != ""}
    return {
        "type": "oauth",
        "accessToken": access_token,
        "refreshToken": refresh_token or None,
        "expiresAt": expires_at,
        "extras": clean_extras,
    }


def _make_api_key_credential(key: str) -> Dict[str, Any]:
    cleaned = key.strip()
    return {
        "type": "api_key",
        "apiKey": cleaned,
        "identity": api_key_identity(cleaned),
    }


# ---------------------------------------------------------------------------
# GitHub Enterprise Cloud (GHE.com) domain normalization
# ---------------------------------------------------------------------------

_GHE_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


def normalize_github_domain(raw: str) -> str:
    """Normalize a Copilot GitHub domain.

    - Blank, ``github.com``, or ``www.github.com`` -> ``"github.com"``
    - Bare tenant name ``octocorp`` -> ``"octocorp.ghe.com"``
    - Full URL or host ``https://octocorp.ghe.com/foo`` -> ``"octocorp.ghe.com"``
    - Any other host raises ``ValueError``.
    """
    host = (raw or "").strip().lower()
    if "://" in host:
        host = host.split("://", 1)[1]
    for sep in ("/", "?", "#"):
        if sep in host:
            host = host.split(sep, 1)[0]
    host = host.strip()
    if not host or host in ("github.com", "www.github.com"):
        return "github.com"
    if "." not in host:
        host = f"{host}.ghe.com"
    if not host.endswith(".ghe.com"):
        raise ValueError(_github_domain_invalid_msg())
    name = host[: -len(".ghe.com")]
    if not name or not _GHE_NAME_RE.match(name):
        raise ValueError(_github_domain_invalid_msg())
    return host


def github_saved_domain(normalized_host: str) -> Optional[str]:
    return None if normalized_host == "github.com" else normalized_host


def github_web_url(normalized_host: str, path: str) -> str:
    return f"https://{normalized_host}{path}"


def github_api_url(normalized_host: str, path: str) -> str:
    return f"https://api.{normalized_host}{path}"


# ---------------------------------------------------------------------------
# Manual / pasted OAuth authorization code parser
# ---------------------------------------------------------------------------

def parse_pasted_authorization_code(value: str, expected_state: str) -> str:
    """Extract an OAuth authorization code from a pasted callback URL, ``code#state``, or bare code."""
    trimmed = (value or "").strip()
    if not trimmed:
        raise LoginError(tr("auth.invalidCode", "The authorization code was invalid."), code="invalid_code")

    code: Optional[str] = None
    state: Optional[str] = None

    parsed_url = urllib.parse.urlsplit(trimmed)
    if parsed_url.scheme and parsed_url.netloc:
        params = urllib.parse.parse_qs(parsed_url.query, keep_blank_values=False)
        code = (params.get("code") or [None])[0]
        state = (params.get("state") or [None])[0]
    elif "#" in trimmed:
        parts = trimmed.split("#", 1)
        code = parts[0] or None
        state = parts[1] if len(parts) > 1 else None
    else:
        code = trimmed
        state = None

    if not code:
        raise LoginError(tr("auth.invalidCode", "The authorization code was invalid."), code="invalid_code")
    if state != expected_state:
        raise LoginError(tr("auth.stateMismatch", "The sign-in response did not match this request."), code="invalid_state")
    return code


# ---------------------------------------------------------------------------
# Browser launcher
# ---------------------------------------------------------------------------

def open_url_in_browser(url: str) -> bool:
    """Launch the default browser non-blockingly via ``xdg-open``."""
    if not url:
        return False
    opener = shutil.which("xdg-open")
    if not opener:
        return False
    try:
        subprocess.Popen(
            [opener, url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Loopback OAuth Callback Server
# ---------------------------------------------------------------------------

def _success_html() -> str:
    return f"""<!doctype html>
<html lang="{html.escape(language())}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(tr("auth.page.successTitle", "Omameter — Sign-in Complete"))}</title>
<style>
  :root {{ color-scheme: dark light; }}
  body {{
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #111318; color: #e8ecf2;
  }}
  .card {{
    max-width: 420px; padding: 28px 32px; border-radius: 14px;
    background: #1b1f27; border: 1px solid #2b3240; text-align: center;
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.35);
  }}
  .icon {{ font-size: 32px; margin-bottom: 12px; color: #4ade80; }}
  h1 {{ margin: 0 0 8px; font-size: 18px; font-weight: 600; }}
  p {{ margin: 0; font-size: 14px; color: #9aa5b5; line-height: 1.5; }}
</style>
</head>
<body>
  <div class="card">
    <div class="icon">&#10003;</div>
    <h1>{html.escape(tr("auth.page.successHeading", "Sign-in complete"))}</h1>
    <p>{html.escape(tr("auth.page.successBody", "You may close this tab and return to Omameter."))}</p>
  </div>
</body>
</html>
"""


def _error_html(message: str) -> str:
    safe = html.escape(message)
    return f"""<!doctype html>
<html lang="{html.escape(language())}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(tr("auth.page.errorTitle", "Omameter — Sign-in Error"))}</title>
<style>
  :root {{ color-scheme: dark light; }}
  body {{
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #111318; color: #e8ecf2;
  }}
  .card {{
    max-width: 420px; padding: 28px 32px; border-radius: 14px;
    background: #1b1f27; border: 1px solid #3d2428; text-align: center;
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.35);
  }}
  h1 {{ margin: 0 0 8px; font-size: 18px; font-weight: 600; color: #f87171; }}
  p {{ margin: 0; font-size: 14px; color: #9aa5b5; line-height: 1.5; }}
</style>
</head>
<body>
  <div class="card">
    <h1>{html.escape(tr("auth.page.errorHeading", "Sign-in failed"))}</h1>
    <p>{safe}</p>
  </div>
</body>
</html>
"""


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _ThreadingHTTPServer6(_ThreadingHTTPServer):
    address_family = socket.AF_INET6

    def server_bind(self) -> None:
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        except OSError:
            pass
        super().server_bind()


class LoopbackCallbackServer:
    """Localhost HTTP callback listener for browser PKCE OAuth flows.

    Supports both fixed ports (e.g. 1455 for ChatGPT) and ephemeral ports (port=0),
    listening on both 127.0.0.1 and ::1 when redirect_host is "localhost".
    """

    def __init__(
        self,
        *,
        state: str,
        port: int = 0,
        path: str = "/callback",
        redirect_host: str = "localhost",
    ) -> None:
        self.state = state
        self.path = path
        self.redirect_host = redirect_host
        self._lock = threading.Lock()
        self._done_event = threading.Event()
        self._code: Optional[str] = None
        self._error: Optional[Exception] = None
        self._closed = False

        owner = self

        class _CallbackHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def _send_html(self, status: int, body_html: str) -> None:
                raw = body_html.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except OSError:
                    pass

            def do_GET(self) -> None:  # noqa: N802
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path != owner.path:
                    self._send_html(404, _error_html(tr("auth.page.callbackNotFound", "Sign-in callback not found.")))
                    return
                params = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
                cb_state = (params.get("state") or [None])[0]
                if cb_state != owner.state:
                    self._send_html(400, _error_html(tr("auth.page.stateMismatch", "Sign-in state did not match.")))
                    return
                cb_error = (params.get("error") or [None])[0]
                if cb_error:
                    owner._finish(error=LoginError(tr("auth.authorizationFailed", "Authorization failed: %1", cb_error), code="authorization_failed"))
                    self._send_html(400, _error_html(tr("auth.page.failed", "Sign-in failed. You may close this page.")))
                    return
                cb_code = (params.get("code") or [None])[0]
                if not cb_code:
                    owner._finish(error=LoginError(tr("auth.invalidCallback", "The sign-in callback request was invalid."), code="invalid_callback"))
                    self._send_html(400, _error_html(tr("auth.page.codeMissing", "Authorization code was missing.")))
                    return
                owner._finish(code=cb_code)
                self._send_html(200, _success_html())

            def do_POST(self) -> None:  # noqa: N802
                self._send_html(405, _error_html(tr("auth.page.getOnly", "Only GET is supported.")))

            def do_HEAD(self) -> None:  # noqa: N802
                self._send_html(405, _error_html(tr("auth.page.getOnly", "Only GET is supported.")))

        try:
            self._server = _ThreadingHTTPServer(("127.0.0.1", int(port)), _CallbackHandler)
        except OSError as exc:
            raise LoginError(tr("auth.listenerFailed", "Could not start the sign-in callback: %1", exc), code="listener_failed") from exc

        actual_port = int(self._server.server_address[1])
        self.port = actual_port
        self.redirect_uri = f"http://{self.redirect_host}:{actual_port}{self.path}"

        self._server6: Optional[_ThreadingHTTPServer6] = None
        if self.redirect_host == "localhost" and socket.has_ipv6:
            try:
                self._server6 = _ThreadingHTTPServer6(("::1", actual_port), _CallbackHandler)
            except OSError:
                self._server6 = None

        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name=f"omameter-oauth-cb-{actual_port}",
            daemon=True,
        )
        self._thread.start()

        self._thread6: Optional[threading.Thread] = None
        if self._server6 is not None:
            self._thread6 = threading.Thread(
                target=self._server6.serve_forever,
                kwargs={"poll_interval": 0.1},
                name=f"omameter-oauth-cb6-{actual_port}",
                daemon=True,
            )
            self._thread6.start()

    def _finish(self, *, code: Optional[str] = None, error: Optional[Exception] = None) -> None:
        with self._lock:
            if self._done_event.is_set():
                return
            self._code = code
            self._error = error
            self._done_event.set()

    def wait_for_code(
        self,
        *,
        cancel_event: Optional[threading.Event] = None,
        manual_code_queue: Optional["queue.Queue[str]"] = None,
        timeout: float = 600.0,
    ) -> str:
        """Block until callback arrives, manual code is submitted, cancelled, or timed out."""
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if cancel_event is not None and cancel_event.is_set():
                    raise LoginCancelled()
                if self._done_event.wait(timeout=0.1):
                    with self._lock:
                        err = self._error
                        code = self._code
                    if err is not None:
                        raise err
                    if code:
                        return code
                    raise LoginCancelled()
                if manual_code_queue is not None:
                    try:
                        pasted = manual_code_queue.get_nowait()
                    except queue.Empty:
                        pasted = None
                    if pasted is not None:
                        return parse_pasted_authorization_code(pasted, self.state)
            raise LoginError(tr("auth.browserTimeout", "Sign-in timed out before the browser returned."), code="timeout")
        finally:
            self.cancel()

    def cancel(self) -> None:
        self._finish(error=LoginCancelled())
        with self._lock:
            if self._closed:
                return
            self._closed = True
        for srv in (self._server, self._server6):
            if srv is None:
                continue
            try:
                srv.shutdown()
            except Exception:
                pass
            try:
                srv.server_close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Device-code polling helper (DeviceCodePoller.swift)
# ---------------------------------------------------------------------------

def poll_device_code(
    *,
    interval: float,
    expires_in: float,
    cancel_event: threading.Event,
    request_fn: Callable[[], Tuple[str, Any]],
) -> Any:
    """Poll until ``request_fn`` returns ``("complete", value)`` or ``("failed", msg)``.

    Other return statuses: ``("pending", None)`` and ``("slow_down", None)``.
    Network transport blips are retried until ``expires_in`` lapses.
    """
    deadline = time.monotonic() + max(1.0, float(expires_in))
    delay = max(1.0, float(interval))
    last_net_err: Optional[HTTPTransportError] = None

    while time.monotonic() < deadline:
        if cancel_event.is_set():
            raise LoginCancelled()
        try:
            status, value = request_fn()
            last_net_err = None
        except HTTPTransportError as exc:
            last_net_err = exc
            status, value = ("pending", None)

        if status == "complete":
            return value
        if status == "failed":
            raise LoginError(str(value or tr("auth.deviceFailed", "Device sign-in failed.")))
        if status == "slow_down":
            delay += 5.0

        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            break
        wait_sec = min(delay, remaining)
        if cancel_event.wait(timeout=wait_sec):
            raise LoginCancelled()

    if last_net_err is not None:
        raise LoginError(str(last_net_err))
    raise LoginError(tr("auth.deviceExpired", "Device sign-in expired."), code="expired")


# ---------------------------------------------------------------------------
# Low-level HTTP / JSON helpers matching LoginHTTP
# ---------------------------------------------------------------------------

def _decode_json_response(resp: HTTPResponse, *, context: Optional[str] = None) -> Dict[str, Any]:
    if not (200 <= resp.status < 300):
        what = context or tr("auth.context.signIn", "Sign-in")
        raise LoginError(tr("auth.httpFailed", "%1 failed with HTTP %2.", what, resp.status), code=f"http_{resp.status}")
    try:
        parsed = resp.json()
    except Exception as exc:
        raise LoginError(tr("auth.invalidResponse", "The sign-in service returned an invalid response."), code="invalid_response") from exc
    if not isinstance(parsed, dict):
        raise LoginError(tr("auth.invalidResponse", "The sign-in service returned an invalid response."), code="invalid_response")
    return parsed


def _safe_json(resp: HTTPResponse) -> Dict[str, Any]:
    try:
        parsed = resp.json()
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _build_url(base: str, params: Dict[str, str]) -> str:
    parsed = urllib.parse.urlsplit(base)
    existing = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    merged = existing + [(k, v) for k, v in params.items() if v is not None]
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(merged), parsed.fragment)
    )


def _device_authorization(
    *,
    provider: str,
    start_url: str,
    token_url: str,
    client_id: str,
    scopes: Optional[str],
    denied_message: str,
    http: HTTPClient,
    cancel_event: threading.Event,
    on_device_prompt: Callable[[str, str], None],
    start_fields: Optional[Dict[str, str]] = None,
    headers: Optional[Dict[str, str]] = None,
    verification_page: Optional[str] = None,
) -> Dict[str, Any]:
    fields: Dict[str, str] = dict(start_fields or {})
    fields["client_id"] = client_id
    if scopes:
        fields["scope"] = scopes

    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)

    start_resp = http.post(start_url, form_body=fields, headers=req_headers)
    device = _decode_json_response(start_resp, context=tr("auth.context.deviceSignIn", "%1 device sign-in", provider))

    device_code = str(device.get("device_code") or "").strip()
    user_code = str(device.get("user_code") or "").strip()
    if not device_code or not user_code:
        raise LoginError(tr("auth.invalidResponse", "The sign-in service returned an invalid response."), code="invalid_response")

    expires_in = float(device.get("expires_in") or 900)
    interval = float(device.get("interval") or 5)

    candidates = []
    if verification_page:
        candidates.append(_build_url(verification_page, {"user_code": user_code}))
    for raw_uri in (device.get("verification_uri_complete"), device.get("verification_uri")):
        if isinstance(raw_uri, str) and raw_uri.strip():
            candidates.append(urllib.parse.urljoin(start_url, raw_uri.strip()))

    verification_url = ""
    for cand in candidates:
        parts = urllib.parse.urlsplit(cand)
        if parts.scheme == "https" and parts.netloc:
            verification_url = cand
            break
    if not verification_url:
        raise LoginError(tr("auth.invalidResponse", "The sign-in service returned an invalid response."), code="invalid_response")

    on_device_prompt(user_code, verification_url)

    def _poll_once() -> Tuple[str, Any]:
        resp = http.post(
            token_url,
            form_body={
                "client_id": client_id,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers=req_headers,
        )
        body = _safe_json(resp)
        if isinstance(body.get("access_token"), str) and body["access_token"].strip():
            return ("complete", body)
        err = str(body.get("error") or "")
        if err == "authorization_pending":
            return ("pending", None)
        if err == "slow_down":
            return ("slow_down", None)
        if err == "expired_token":
            return ("failed", tr("auth.deviceExpiredFor", "%1 device sign-in expired.", provider))
        if err == "access_denied":
            return ("failed", denied_message)
        return ("failed", tr("auth.deviceFailedFor", "%1 device sign-in failed.", provider))

    return poll_device_code(
        interval=interval,
        expires_in=expires_in,
        cancel_event=cancel_event,
        request_fn=_poll_once,
    )


# ---------------------------------------------------------------------------
# API-key / Organization-token validation (also usable synchronously)
# ---------------------------------------------------------------------------

def _extract_ollama_email(body_dict: Dict[str, Any]) -> Optional[str]:
    raw = body_dict.get("Email") or body_dict.get("email")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def validate_api_key_sync(
    kind: str,
    api_key: str,
    *,
    organization: Optional[str] = None,
    http: Optional[HTTPClient] = None,
) -> Tuple[str, Dict[str, Any], str]:
    """Validate an API key or Copilot org token and return ``(label, credential, identity)``."""
    client = http or HTTPClient()
    key = (api_key or "").strip()
    if not key:
        raise LoginError(tr("auth.enterApiKey", "Please enter an API key."), code="invalid_code")

    if kind == "copilot" and organization:
        org = organization.strip()
        if not org:
            raise LoginError(tr("auth.enterOrganization", "Please enter a GitHub organization name."), code="invalid_code")
        url = f"https://api.github.com/organizations/{urllib.parse.quote(org, safe='')}/settings/billing/ai_credit/usage"
        resp = client.get(
            url,
            headers={
                "Authorization": f"Bearer {key}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "Omameter",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        if resp.status in (401, 403):
            raise LoginError(tr("auth.githubTokenRejected", "GitHub rejected the token."), code="api_key_rejected")
        if resp.status == 404:
            raise LoginError(tr("auth.githubOrgNotFound", 'GitHub has no organization "%1" this token can read.', org), code="not_found")
        raise_for_http_error(resp, "Copilot", is_api_key=True)
        cred = _make_api_key_credential(key)
        return org, cred, cred["identity"]

    if kind == "ollama":
        resp = client.get(
            OLLAMA_BALANCE_URL,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        )
        raise_for_http_error(resp, "Ollama", is_api_key=True)
        label = "Ollama"
        try:
            acct = client.post(
                OLLAMA_ACCOUNT_URL,
                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            )
            if 200 <= acct.status < 300:
                email = _extract_ollama_email(_safe_json(acct))
                if email:
                    label = email
        except Exception:
            pass
        cred = _make_api_key_credential(key)
        return label, cred, cred["identity"]

    if kind == "openrouter":
        resp = client.get(
            OPENROUTER_KEY_URL,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        )
        raise_for_http_error(resp, "OpenRouter", is_api_key=True)
        cred = _make_api_key_credential(key)
        return "OpenRouter", cred, cred["identity"]

    if kind == "zai":
        resp = client.get(
            ZAI_QUOTA_URL,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        )
        raise_for_http_error(resp, "Z.ai", is_api_key=True)
        cred = _make_api_key_credential(key)
        return "Z.ai", cred, cred["identity"]

    if kind == "opencode":
        resp = client.get(
            OPENCODE_USAGE_URL,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        )
        raise_for_http_error(resp, "OpenCode", is_api_key=True)
        cred = _make_api_key_credential(key)
        return "OpenCode", cred, cred["identity"]

    raise LoginError(tr("auth.apiKeyUnsupported", "API key sign-in is not supported for %1.", kind))


# ---------------------------------------------------------------------------
# AuthManager (single concurrent interactive session on background thread)
# ---------------------------------------------------------------------------

def _empty_session() -> Dict[str, Any]:
    return {
        "active": False,
        "kind": "",
        "method": "",
        "accountId": None,
        "status": "cancelled",
        "verificationUrl": "",
        "userCode": "",
        "acceptsManualCode": False,
        "message": "",
        "error": "",
    }


class AuthManager:
    """Manages at most one interactive provider sign-in flow at a time on a background thread."""

    def __init__(
        self,
        *,
        http: Optional[HTTPClient] = None,
        on_update: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_complete: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self._http = http or HTTPClient()
        self._on_update = on_update
        self._on_complete = on_complete

        self._lock = threading.Lock()
        self._session: Dict[str, Any] = _empty_session()
        self._generation: int = 0
        self._cancel_event: Optional[threading.Event] = None
        self._manual_queue: Optional["queue.Queue[str]"] = None
        self._active_server: Optional[LoopbackCallbackServer] = None
        self._worker: Optional[threading.Thread] = None

    def current_session(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._session)

    def start_login(
        self,
        kind: str,
        *,
        method: Optional[str] = None,
        account_id: Optional[str] = None,
        domain: Optional[str] = None,
        organization: Optional[str] = None,
        api_key: Optional[str] = None,
        open_browser: bool = True,
    ) -> Dict[str, Any]:
        if kind not in PROVIDER_CATALOG:
            raise ValueError(f"Unknown provider kind: {kind}")

        spec = PROVIDER_CATALOG[kind]
        eff_method = (method or spec.auth_methods[0]).strip().lower()
        if eff_method not in spec.auth_methods:
            # Allow "oauth" alias for device providers or vice versa when unambiguous
            if eff_method == "oauth" and "device" in spec.auth_methods:
                eff_method = "device"
            elif eff_method not in ("oauth", "device", "api_key", "org_token"):
                raise ValueError(f"Unsupported auth method '{eff_method}' for {kind}")

        # Cancel any currently running interactive flow cleanly before starting a new one.
        self._cancel_active_internal(emit_update=False)

        cancel_event = threading.Event()
        manual_queue: "queue.Queue[str]" = queue.Queue()

        with self._lock:
            self._generation += 1
            gen = self._generation
            self._cancel_event = cancel_event
            self._manual_queue = manual_queue
            self._active_server = None
            self._session = {
                "active": True,
                "kind": kind,
                "method": eff_method,
                "accountId": account_id,
                "status": "starting",
                "verificationUrl": "",
                "userCode": "",
                "acceptsManualCode": False,
                "message": tr("auth.starting", "Starting %1 sign-in...", spec.display_name),
                "error": "",
            }
            snapshot = dict(self._session)

        self._emit_update(snapshot)

        worker = threading.Thread(
            target=self._run_worker,
            kwargs={
                "gen": gen,
                "kind": kind,
                "method": eff_method,
                "account_id": account_id,
                "domain": domain,
                "organization": organization,
                "api_key": api_key,
                "open_browser": open_browser,
                "cancel_event": cancel_event,
                "manual_queue": manual_queue,
            },
            name=f"omameter-auth-{kind}-{gen}",
            daemon=True,
        )
        with self._lock:
            self._worker = worker
        worker.start()
        return snapshot

    def submit_manual_code(self, code: str) -> bool:
        text = (code or "").strip()
        if not text:
            return False
        with self._lock:
            if not self._session.get("active") or self._manual_queue is None:
                return False
            q = self._manual_queue
        q.put(text)
        return True

    def cancel(self) -> None:
        self._cancel_active_internal(emit_update=True)

    def _cancel_active_internal(self, *, emit_update: bool) -> None:
        with self._lock:
            was_active = bool(self._session.get("active"))
            had_error = self._session.get("status") == "error"
            cancel_event = self._cancel_event
            server = self._active_server
            self._active_server = None
            if was_active or had_error:
                self._session["active"] = False
                self._session["status"] = "cancelled"
                self._session["message"] = tr("auth.cancelled", "Sign-in was cancelled.") if was_active else ""
                self._session["error"] = ""
            snapshot = dict(self._session)

        if cancel_event is not None:
            cancel_event.set()
        if server is not None:
            server.cancel()
        if emit_update and (was_active or had_error):
            self._emit_update(snapshot)

    def _set_active_server(self, gen: int, server: Optional[LoopbackCallbackServer]) -> None:
        with self._lock:
            if gen == self._generation:
                self._active_server = server

    def _update_session(self, gen: int, **changes: Any) -> Optional[Dict[str, Any]]:
        with self._lock:
            if gen != self._generation:
                return None
            self._session.update(changes)
            snapshot = dict(self._session)
        self._emit_update(snapshot)
        return snapshot

    def _emit_update(self, session: Dict[str, Any]) -> None:
        if self._on_update is not None:
            try:
                self._on_update(session)
            except Exception:
                pass

    def _emit_complete(self, result: Dict[str, Any]) -> None:
        if self._on_complete is not None:
            try:
                self._on_complete(result)
            except Exception:
                pass

    def _run_worker(
        self,
        *,
        gen: int,
        kind: str,
        method: str,
        account_id: Optional[str],
        domain: Optional[str],
        organization: Optional[str],
        api_key: Optional[str],
        open_browser: bool,
        cancel_event: threading.Event,
        manual_queue: "queue.Queue[str]",
    ) -> None:
        try:
            result = self._execute_flow(
                gen=gen,
                kind=kind,
                method=method,
                account_id=account_id,
                domain=domain,
                organization=organization,
                api_key=api_key,
                open_browser=open_browser,
                cancel_event=cancel_event,
                manual_queue=manual_queue,
            )
            if cancel_event.is_set():
                raise LoginCancelled()
            self._update_session(
                gen,
                active=False,
                status="completed",
                message=tr("auth.signedInAs", "Signed in as %1.", result["label"]),
                error="",
            )
            self._emit_complete(result)
        except LoginCancelled:
            self._update_session(
                gen,
                active=False,
                status="cancelled",
                message=tr("auth.cancelled", "Sign-in was cancelled."),
                error="",
            )
        except (LoginError, ProviderError, HTTPTransportError, ValueError) as exc:
            if cancel_event.is_set():
                self._update_session(
                    gen,
                    active=False,
                    status="cancelled",
                    message=tr("auth.cancelled", "Sign-in was cancelled."),
                    error="",
                )
            else:
                self._update_session(
                    gen,
                    active=False,
                    status="error",
                    message="",
                    error=redact_secrets(str(exc)),
                )
        except Exception as exc:
            self._update_session(
                gen,
                active=False,
                status="error",
                message="",
                error=redact_secrets(tr("auth.failedWith", "Sign-in failed: %1", exc)),
            )
        finally:
            self._set_active_server(gen, None)

    def _execute_flow(
        self,
        *,
        gen: int,
        kind: str,
        method: str,
        account_id: Optional[str],
        domain: Optional[str],
        organization: Optional[str],
        api_key: Optional[str],
        open_browser: bool,
        cancel_event: threading.Event,
        manual_queue: "queue.Queue[str]",
    ) -> Dict[str, Any]:
        # API key & Copilot org token flows
        if method in ("api_key", "org_token"):
            key_val = (api_key or "").strip()
            if not key_val:
                self._update_session(
                    gen,
                    status="browser",
                    acceptsManualCode=True,
                    message=tr("auth.pasteApiKey", "Paste your API key to continue."),
                )
                key_val = self._wait_for_manual_value(cancel_event, manual_queue)
            self._update_session(gen, status="exchanging", message=tr("auth.verifyingKey", "Verifying key..."))
            label, credential, identity = validate_api_key_sync(
                kind,
                key_val,
                organization=organization,
                http=self._http,
            )
            return {
                "kind": kind,
                "method": method,
                "accountId": account_id,
                "label": label,
                "credential": credential,
                "identity": identity,
                "domain": None,
                "organization": organization.strip() if organization else None,
            }

        if kind == "claude":
            return self._login_claude(gen, account_id, open_browser, cancel_event, manual_queue)
        if kind == "codex":
            return self._login_codex(gen, method, account_id, open_browser, cancel_event, manual_queue)
        if kind == "cursor":
            return self._login_cursor(gen, account_id, open_browser, cancel_event)
        if kind == "antigravity":
            return self._login_antigravity(gen, account_id, open_browser, cancel_event, manual_queue)
        if kind == "devin":
            return self._login_devin(gen, account_id, open_browser, cancel_event)
        if kind == "copilot":
            return self._login_copilot(gen, account_id, domain, open_browser, cancel_event)
        if kind == "grok":
            return self._login_grok(gen, account_id, open_browser, cancel_event)
        if kind == "muse":
            return self._login_muse(gen, account_id, open_browser, cancel_event)
        if kind == "amp":
            return self._login_amp(gen, account_id, open_browser, cancel_event)
        if kind == "opencode":
            return self._login_opencode(gen, account_id, open_browser, cancel_event)

        raise LoginError(tr("auth.interactiveUnsupported", "Interactive sign-in is not implemented for %1.", kind))

    def _wait_for_manual_value(
        self,
        cancel_event: threading.Event,
        manual_queue: "queue.Queue[str]",
        timeout: float = 600.0,
    ) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel_event.is_set():
                raise LoginCancelled()
            try:
                val = manual_queue.get(timeout=0.1)
                if val.strip():
                    return val.strip()
            except queue.Empty:
                continue
        raise LoginError(tr("auth.timedOut", "Sign-in timed out."), code="timeout")

    # -----------------------------------------------------------------------
    # Browser OAuth Providers
    # -----------------------------------------------------------------------

    def _prompt_browser(
        self,
        gen: int,
        url: str,
        *,
        accepts_manual_code: bool,
        open_browser: bool,
        provider_name: str,
    ) -> None:
        self._update_session(
            gen,
            status="browser",
            verificationUrl=url,
            userCode="",
            acceptsManualCode=accepts_manual_code,
            message=tr("auth.completeInBrowser", "Complete sign-in for %1 in your browser.", provider_name),
        )
        if open_browser:
            open_url_in_browser(url)

    def _prompt_device_code(
        self,
        gen: int,
        user_code: str,
        verification_url: str,
        *,
        open_browser: bool,
        provider_name: str,
    ) -> None:
        self._update_session(
            gen,
            status="device_code",
            method="device",
            verificationUrl=verification_url,
            userCode=user_code,
            acceptsManualCode=False,
            message=tr("auth.enterDeviceCode", "Enter code %1 in your browser to sign in to %2.", user_code, provider_name),
        )
        if open_browser:
            open_url_in_browser(verification_url)
        # Transition to polling state while preserving code & URL so UI shows waiting indicator
        self._update_session(
            gen,
            status="polling",
            verificationUrl=verification_url,
            userCode=user_code,
        )

    def _login_claude(
        self,
        gen: int,
        account_id: Optional[str],
        open_browser: bool,
        cancel_event: threading.Event,
        manual_queue: "queue.Queue[str]",
    ) -> Dict[str, Any]:
        pkce = generate_pkce()
        state = str(uuid.uuid4())
        server = LoopbackCallbackServer(state=state, port=0, path="/callback")
        self._set_active_server(gen, server)
        try:
            auth_url = _build_url(
                CLAUDE_AUTHORIZE_URL,
                {
                    "client_id": CLAUDE_CLIENT_ID,
                    "response_type": "code",
                    "redirect_uri": server.redirect_uri,
                    "scope": CLAUDE_SCOPES,
                    "code_challenge": pkce.challenge,
                    "code_challenge_method": "S256",
                    "state": state,
                    "code": "true",
                },
            )
            self._prompt_browser(
                gen,
                auth_url,
                accepts_manual_code=True,
                open_browser=open_browser,
                provider_name="Claude",
            )
            code = server.wait_for_code(cancel_event=cancel_event, manual_code_queue=manual_queue)
        finally:
            server.cancel()
            self._set_active_server(gen, None)

        self._update_session(gen, status="exchanging", message=tr("auth.completing", "Completing %1 sign-in...", "Claude"))
        tok_resp = self._http.post(
            CLAUDE_TOKEN_URL,
            json_body={
                "grant_type": "authorization_code",
                "client_id": CLAUDE_CLIENT_ID,
                "code": code,
                "state": state,
                "redirect_uri": server.redirect_uri,
                "code_verifier": pkce.verifier,
            },
            headers={"Accept": "application/json"},
        )
        tok = _decode_json_response(tok_resp, context=tr("auth.context.providerSignIn", "%1 sign-in", "Claude"))
        access_token = str(tok.get("access_token") or "")
        if not access_token:
            raise LoginError(tr("auth.invalidResponse", "The sign-in service returned an invalid response."), code="invalid_response")

        try:
            prof_resp = self._http.get(
                CLAUDE_PROFILE_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                    "anthropic-beta": "oauth-2025-04-20",
                },
            )
            profile = _decode_json_response(prof_resp, context=tr("auth.context.profile", "%1 profile", "Claude"))
            acct_obj = profile.get("account") or {}
            acct_uuid = str(acct_obj.get("uuid") or "")
            if not acct_uuid:
                raise LoginError("Missing account UUID")
        except Exception as exc:
            raise LoginError(tr("auth.claudeProfileMissing", "Claude didn't return your profile. Try signing in again.")) from exc

        org_obj = profile.get("organization") if isinstance(profile.get("organization"), dict) else None
        org_uuid = str(org_obj.get("uuid") or "") if org_obj else ""
        identity = f"{acct_uuid}/{org_uuid}" if org_uuid else acct_uuid
        email = acct_obj.get("email") or profile.get("email")
        label = str(email).strip() if isinstance(email, str) and email.strip() else "Claude Account"

        credential = _make_oauth_credential(
            access_token,
            refresh_token=tok.get("refresh_token"),
            expires_at=_expires_at_from_seconds(tok.get("expires_in")),
            extras={ACCOUNT_IDENTITY_KEY: identity},
        )
        return {
            "kind": "claude",
            "method": "oauth",
            "accountId": account_id,
            "label": label,
            "credential": credential,
            "identity": identity,
            "domain": None,
            "organization": None,
        }

    def _login_codex(
        self,
        gen: int,
        method: str,
        account_id: Optional[str],
        open_browser: bool,
        cancel_event: threading.Event,
        manual_queue: "queue.Queue[str]",
    ) -> Dict[str, Any]:
        if method == "device":
            return self._login_codex_device(gen, account_id, open_browser, cancel_event)

        pkce = generate_pkce()
        state = str(uuid.uuid4())
        try:
            server = LoopbackCallbackServer(
                state=state,
                port=CODEX_CALLBACK_PORT,
                path=CODEX_CALLBACK_PATH,
            )
        except LoginError:
            # Port 1455 busy (e.g. Codex CLI running): automatically fall back to device code flow.
            return self._login_codex_device(gen, account_id, open_browser, cancel_event)

        self._set_active_server(gen, server)
        try:
            auth_url = _build_url(
                CODEX_AUTHORIZE_URL,
                {
                    "client_id": CODEX_CLIENT_ID,
                    "response_type": "code",
                    "redirect_uri": server.redirect_uri,
                    "scope": CODEX_SCOPES,
                    "code_challenge": pkce.challenge,
                    "code_challenge_method": "S256",
                    "state": state,
                    "id_token_add_organizations": "true",
                    "codex_cli_simplified_flow": "true",
                    "originator": "pi",
                },
            )
            self._prompt_browser(
                gen,
                auth_url,
                accepts_manual_code=True,
                open_browser=open_browser,
                provider_name="ChatGPT",
            )
            code = server.wait_for_code(cancel_event=cancel_event, manual_code_queue=manual_queue)
        finally:
            server.cancel()
            self._set_active_server(gen, None)

        return self._exchange_codex(
            gen=gen,
            code=code,
            verifier=pkce.verifier,
            redirect_uri=server.redirect_uri,
            account_id=account_id,
            method="oauth",
        )

    def _login_codex_device(
        self,
        gen: int,
        account_id: Optional[str],
        open_browser: bool,
        cancel_event: threading.Event,
    ) -> Dict[str, Any]:
        start_resp = self._http.post(
            CODEX_DEVICE_USERCODE_URL,
            json_body={"client_id": CODEX_CLIENT_ID},
            headers={"Accept": "application/json"},
        )
        device = _decode_json_response(start_resp, context=tr("auth.context.deviceSignIn", "%1 device sign-in", "ChatGPT"))
        device_auth_id = str(device.get("device_auth_id") or "")
        user_code = str(device.get("user_code") or "")
        if not device_auth_id or not user_code:
            raise LoginError(tr("auth.invalidResponse", "The sign-in service returned an invalid response."), code="invalid_response")

        interval = float(device.get("interval") or 5)
        self._prompt_device_code(
            gen,
            user_code,
            CODEX_DEVICE_VERIFY_URL,
            open_browser=open_browser,
            provider_name="ChatGPT",
        )

        def _poll_once() -> Tuple[str, Any]:
            resp = self._http.post(
                CODEX_DEVICE_TOKEN_URL,
                json_body={"device_auth_id": device_auth_id, "user_code": user_code},
                headers={"Accept": "application/json"},
            )
            if 200 <= resp.status < 300:
                parsed = _decode_json_response(resp, context=tr("auth.context.deviceSignIn", "%1 device sign-in", "ChatGPT"))
                return ("complete", parsed)
            if resp.status in (403, 404):
                return ("pending", None)
            err = str(_safe_json(resp).get("error") or "")
            if err == "deviceauth_authorization_pending":
                return ("pending", None)
            if err == "slow_down":
                return ("slow_down", None)
            if err == "expired_token":
                return ("failed", tr("auth.deviceExpired", "Device sign-in expired."))
            return ("failed", tr("auth.deviceFailed", "Device sign-in failed."))

        dev_token = poll_device_code(
            interval=interval,
            expires_in=900.0,
            cancel_event=cancel_event,
            request_fn=_poll_once,
        )
        auth_code = str(dev_token.get("authorization_code") or "")
        code_verifier = str(dev_token.get("code_verifier") or "")
        if not auth_code or not code_verifier:
            raise LoginError(tr("auth.invalidResponse", "The sign-in service returned an invalid response."), code="invalid_response")

        return self._exchange_codex(
            gen=gen,
            code=auth_code,
            verifier=code_verifier,
            redirect_uri=CODEX_DEVICE_REDIRECT_URI,
            account_id=account_id,
            method="device",
        )

    def _exchange_codex(
        self,
        *,
        gen: int,
        code: str,
        verifier: str,
        redirect_uri: str,
        account_id: Optional[str],
        method: str,
    ) -> Dict[str, Any]:
        self._update_session(gen, status="exchanging", message=tr("auth.completing", "Completing %1 sign-in...", "ChatGPT"))
        tok_resp = self._http.post(
            CODEX_TOKEN_URL,
            form_body={
                "grant_type": "authorization_code",
                "client_id": CODEX_CLIENT_ID,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
            },
            headers={"Accept": "application/json"},
        )
        tok = _decode_json_response(tok_resp, context=tr("auth.context.providerSignIn", "%1 sign-in", "ChatGPT"))
        access_token = str(tok.get("access_token") or "")
        if not access_token:
            raise LoginError(tr("auth.invalidResponse", "The sign-in service returned an invalid response."), code="invalid_response")

        access_claims = decode_jwt_payload(access_token) or {}
        auth_claim = access_claims.get("https://api.openai.com/auth")
        chatgpt_account_id = (
            str(auth_claim.get("chatgpt_account_id"))
            if isinstance(auth_claim, dict) and auth_claim.get("chatgpt_account_id")
            else None
        )
        id_claims = decode_jwt_payload(str(tok.get("id_token") or "")) or {}
        email = id_claims.get("email") if isinstance(id_claims.get("email"), str) else None

        extras: Dict[str, str] = {}
        if chatgpt_account_id:
            extras["chatgpt_account_id"] = chatgpt_account_id
        identity: Optional[str] = None
        if chatgpt_account_id and email:
            identity = f"{chatgpt_account_id}/{email}"
            extras[ACCOUNT_IDENTITY_KEY] = identity

        credential = _make_oauth_credential(
            access_token,
            refresh_token=tok.get("refresh_token"),
            expires_at=_expires_at_from_seconds(tok.get("expires_in")),
            extras=extras,
        )
        return {
            "kind": "codex",
            "method": method,
            "accountId": account_id,
            "label": email or "ChatGPT Account",
            "credential": credential,
            "identity": identity,
            "domain": None,
            "organization": None,
        }

    def _login_antigravity(
        self,
        gen: int,
        account_id: Optional[str],
        open_browser: bool,
        cancel_event: threading.Event,
        manual_queue: "queue.Queue[str]",
    ) -> Dict[str, Any]:
        pkce = generate_pkce()
        state = str(uuid.uuid4())
        try:
            server = LoopbackCallbackServer(
                state=state,
                port=ANTIGRAVITY_CALLBACK_PORT,
                path=ANTIGRAVITY_CALLBACK_PATH,
            )
        except LoginError:
            server = LoopbackCallbackServer(
                state=state,
                port=0,
                path=ANTIGRAVITY_CALLBACK_PATH,
            )
        self._set_active_server(gen, server)
        try:
            auth_url = _build_url(
                ANTIGRAVITY_AUTHORIZE_URL,
                {
                    "client_id": ANTIGRAVITY_CLIENT_ID,
                    "response_type": "code",
                    "redirect_uri": server.redirect_uri,
                    "scope": ANTIGRAVITY_SCOPES,
                    "code_challenge": pkce.challenge,
                    "code_challenge_method": "S256",
                    "state": state,
                    "access_type": "offline",
                    "prompt": "consent",
                },
            )
            self._prompt_browser(
                gen,
                auth_url,
                accepts_manual_code=True,
                open_browser=open_browser,
                provider_name="Antigravity",
            )
            code = server.wait_for_code(cancel_event=cancel_event, manual_code_queue=manual_queue)
        finally:
            server.cancel()
            self._set_active_server(gen, None)

        self._update_session(gen, status="exchanging", message=tr("auth.completing", "Completing %1 sign-in...", "Antigravity"))
        tok_resp = self._http.post(
            ANTIGRAVITY_TOKEN_URL,
            form_body={
                "client_id": ANTIGRAVITY_CLIENT_ID,
                "client_secret": ANTIGRAVITY_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": server.redirect_uri,
                "code_verifier": pkce.verifier,
            },
            headers={"Accept": "application/json"},
        )
        tok = _decode_json_response(tok_resp, context=tr("auth.context.providerSignIn", "%1 sign-in", "Antigravity"))
        access_token = str(tok.get("access_token") or "")
        if not access_token:
            raise LoginError(tr("auth.invalidResponse", "The sign-in service returned an invalid response."), code="invalid_response")

        email: Optional[str] = None
        user_id: Optional[str] = None
        try:
            info_resp = self._http.get(
                ANTIGRAVITY_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
            if 200 <= info_resp.status < 300:
                user = _safe_json(info_resp)
                if isinstance(user.get("email"), str) and user["email"].strip():
                    email = user["email"].strip()
                if isinstance(user.get("id"), str) and user["id"].strip():
                    user_id = user["id"].strip()
        except Exception:
            pass

        identity = user_id or email
        extras = {ACCOUNT_IDENTITY_KEY: identity} if identity else {}
        credential = _make_oauth_credential(
            access_token,
            refresh_token=tok.get("refresh_token"),
            expires_at=_expires_at_from_seconds(tok.get("expires_in")),
            extras=extras,
        )
        return {
            "kind": "antigravity",
            "method": "oauth",
            "accountId": account_id,
            "label": email or "Google Account",
            "credential": credential,
            "identity": identity,
            "domain": None,
            "organization": None,
        }

    def _login_cursor(
        self,
        gen: int,
        account_id: Optional[str],
        open_browser: bool,
        cancel_event: threading.Event,
    ) -> Dict[str, Any]:
        pkce = generate_pkce()
        login_uuid = str(uuid.uuid4())
        auth_url = _build_url(
            CURSOR_AUTHORIZE_URL,
            {
                "challenge": pkce.challenge,
                "uuid": login_uuid,
                "mode": "login",
                "redirectTarget": "cli",
            },
        )
        self._prompt_browser(
            gen,
            auth_url,
            accepts_manual_code=False,
            open_browser=open_browser,
            provider_name="Cursor",
        )
        self._update_session(
            gen,
            status="polling",
            verificationUrl=auth_url,
            message=tr("auth.waitingInBrowser", "Waiting for %1 sign-in in your browser...", "Cursor"),
        )

        poll_url = _build_url(CURSOR_POLL_URL, {"uuid": login_uuid, "verifier": pkce.verifier})

        def _poll_once() -> Tuple[str, Any]:
            resp = self._http.get(poll_url, headers={"Accept": "application/json"})
            if 200 <= resp.status < 300:
                body = _safe_json(resp)
                at = str(body.get("accessToken") or "")
                rt = str(body.get("refreshToken") or "")
                if at and rt:
                    return ("complete", body)
            if resp.status in (404, 202):
                return ("pending", None)
            return ("failed", tr("auth.signInFailedFor", "%1 sign-in failed.", "Cursor"))

        tok = poll_device_code(
            interval=2.0,
            expires_in=300.0,
            cancel_event=cancel_event,
            request_fn=_poll_once,
        )
        access_token = str(tok["accessToken"])
        refresh_token = str(tok["refreshToken"])

        self._update_session(gen, status="exchanging", message=tr("auth.fetchingProfile", "Fetching %1 profile...", "Cursor"))
        user_id: Optional[str] = None
        payload = decode_jwt_payload(access_token)
        sub = payload.get("sub") if isinstance(payload, dict) else None
        if isinstance(sub, str) and sub.strip():
            parts = sub.strip().split("|")
            candidate = parts[1] if len(parts) > 1 else parts[0]
            if candidate:
                user_id = candidate

        label = "Cursor Account"
        if user_id:
            try:
                cookie = f"WorkosCursorSessionToken={user_id}%3A%3A{access_token}"
                prof_resp = self._http.get(
                    CURSOR_PROFILE_URL,
                    headers={"Cookie": cookie, "Accept": "application/json"},
                )
                if 200 <= prof_resp.status < 300:
                    prof = _safe_json(prof_resp)
                    for key in ("email", "name"):
                        val = prof.get(key)
                        if isinstance(val, str) and val.strip():
                            label = val.strip()
                            break
            except Exception:
                pass

        identity = f"cursor:{user_id}" if user_id else None
        extras = {ACCOUNT_IDENTITY_KEY: identity} if identity else {}
        credential = _make_oauth_credential(
            access_token,
            refresh_token=refresh_token,
            expires_at=jwt_expiry_iso(access_token),
            extras=extras,
        )
        return {
            "kind": "cursor",
            "method": "oauth",
            "accountId": account_id,
            "label": label,
            "credential": credential,
            "identity": identity,
            "domain": None,
            "organization": None,
        }

    def _login_devin(
        self,
        gen: int,
        account_id: Optional[str],
        open_browser: bool,
        cancel_event: threading.Event,
    ) -> Dict[str, Any]:
        pkce = generate_pkce()
        state = str(uuid.uuid4())
        # Devin refuses `localhost` and accepts only `http://127.0.0.1:<port>/callback`.
        server = LoopbackCallbackServer(state=state, port=0, path="/callback", redirect_host="127.0.0.1")
        self._set_active_server(gen, server)
        try:
            auth_url = _build_url(
                DEVIN_AUTHORIZE_URL,
                {
                    "redirect_uri": server.redirect_uri,
                    "state": state,
                    "prompt": "select_account",
                    "code_challenge": pkce.challenge,
                    "code_challenge_method": "S256",
                },
            )
            self._prompt_browser(
                gen,
                auth_url,
                accepts_manual_code=False,
                open_browser=open_browser,
                provider_name="Devin",
            )
            code = server.wait_for_code(cancel_event=cancel_event)
        finally:
            server.cancel()
            self._set_active_server(gen, None)

        self._update_session(gen, status="exchanging", message=tr("auth.completing", "Completing %1 sign-in...", "Devin"))
        exch_resp = self._http.post(
            DEVIN_TOKEN_URL,
            json_body={"code": code, "code_verifier": pkce.verifier},
            headers={"Accept": "application/json"},
        )
        if not (200 <= exch_resp.status < 300):
            raise LoginError(tr("auth.devinRejected", "Devin didn't accept the sign-in. Try signing in again."))
        exch = _decode_json_response(exch_resp, context=tr("auth.context.providerSignIn", "%1 sign-in", "Devin"))
        raw_token = str(exch.get("token") or "")
        if not raw_token:
            raise LoginError(tr("auth.invalidResponse", "The sign-in service returned an invalid response."), code="invalid_response")
        session_token = (
            raw_token
            if raw_token.startswith(DEVIN_SESSION_TOKEN_PREFIX)
            else DEVIN_SESSION_TOKEN_PREFIX + raw_token
        )

        status_url = f"{DEVIN_DEFAULT_API_SERVER_URL}/exa.seat_management_pb.SeatManagementService/GetUserStatus"
        status_resp = self._http.post(
            status_url,
            json_body={
                "metadata": {
                    "apiKey": session_token,
                    "ideName": "devin",
                    "ideVersion": DEVIN_CLOUD_COMPAT_VERSION,
                    "extensionName": "devin",
                    "extensionVersion": DEVIN_CLOUD_COMPAT_VERSION,
                    "locale": "en",
                }
            },
            headers={
                "Content-Type": "application/json",
                "Connect-Protocol-Version": "1",
            },
        )
        if not (200 <= status_resp.status < 300):
            raise LoginError(
                tr("auth.devinAccountMissing", "Devin signed you in but didn't return your account. Try signing in again.")
            )
        status_json = _decode_json_response(status_resp, context=tr("auth.context.userStatus", "%1 user status", "Devin"))
        user_status = status_json.get("userStatus") if isinstance(status_json.get("userStatus"), dict) else {}
        email = user_status.get("email") if isinstance(user_status.get("email"), str) else None
        team_id = user_status.get("teamId") if isinstance(user_status.get("teamId"), str) else None

        identity: Optional[str] = None
        if email:
            identity = f"devin:{email}/{team_id}" if team_id else f"devin:{email}"
        extras = {ACCOUNT_IDENTITY_KEY: identity} if identity else {}

        credential = _make_oauth_credential(session_token, extras=extras)
        return {
            "kind": "devin",
            "method": "oauth",
            "accountId": account_id,
            "label": email or "Devin Account",
            "credential": credential,
            "identity": identity,
            "domain": None,
            "organization": None,
        }

    # -----------------------------------------------------------------------
    # Device-Code Providers
    # -----------------------------------------------------------------------

    def _login_copilot(
        self,
        gen: int,
        account_id: Optional[str],
        domain: Optional[str],
        open_browser: bool,
        cancel_event: threading.Event,
    ) -> Dict[str, Any]:
        norm_host = normalize_github_domain(domain or "")
        saved_domain = github_saved_domain(norm_host)
        try:
            tok = _device_authorization(
                provider="GitHub",
                start_url=github_web_url(norm_host, "/login/device/code"),
                token_url=github_web_url(norm_host, "/login/oauth/access_token"),
                client_id=COPILOT_CLIENT_ID,
                scopes=COPILOT_SCOPES,
                headers={"User-Agent": "Omameter"},
                denied_message=tr("auth.deviceDenied", "%1 device sign-in was denied.", "GitHub"),
                http=self._http,
                cancel_event=cancel_event,
                on_device_prompt=lambda code, url: self._prompt_device_code(
                    gen, code, url, open_browser=open_browser, provider_name="Copilot"
                ),
            )
            access_token = str(tok.get("access_token") or "")
            if not access_token:
                raise LoginError(tr("auth.invalidResponse", "The sign-in service returned an invalid response."), code="invalid_response")

            self._update_session(gen, status="exchanging", message=tr("auth.fetchingProfile", "Fetching %1 profile...", "GitHub"))
            prof_resp = self._http.get(
                github_api_url(norm_host, "/user"),
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "Omameter",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            user = _decode_json_response(prof_resp, context=tr("auth.context.profile", "%1 profile", "GitHub"))
            login = str(user.get("login") or "").strip()
            if not login:
                raise LoginError(tr("auth.invalidResponse", "The sign-in service returned an invalid response."), code="invalid_response")
            user_id = str(user["id"]) if user.get("id") is not None else login

            if saved_domain is not None:
                # Enterprise sign-in verifies that the user actually has a Copilot seat on this GHE.com host.
                seat_resp = self._http.get(
                    github_api_url(norm_host, "/copilot_internal/user"),
                    headers={
                        "Authorization": f"token {access_token}",
                        "Accept": "application/json",
                        "Editor-Version": "vscode/1.96.2",
                        "Editor-Plugin-Version": "copilot-chat/0.26.7",
                        "User-Agent": "GitHubCopilotChat/0.26.7",
                        "X-Github-Api-Version": "2025-04-01",
                    },
                )
                if not (200 <= seat_resp.status < 300):
                    if seat_resp.status in (401, 403, 404):
                        raise LoginError(
                            tr(
                                "auth.noCopilotSeat",
                                "%1 has no Copilot seat on %2. Ask your GitHub admin for one, then try again.",
                                login,
                                norm_host,
                            )
                        )
                    raise_for_http_error(seat_resp, "Copilot")
                identity = f"github:{saved_domain}:{user_id}"
            else:
                identity = f"github:{user_id}"
        except HTTPTransportError as exc:
            if saved_domain is not None:
                raise LoginError(
                    tr("auth.hostUnreachable", "Couldn't reach %1. Check the address and your connection.", norm_host)
                ) from exc
            raise

        credential = _make_oauth_credential(
            access_token,
            extras={ACCOUNT_IDENTITY_KEY: identity},
        )
        return {
            "kind": "copilot",
            "method": "device",
            "accountId": account_id,
            "label": login,
            "credential": credential,
            "identity": identity,
            "domain": saved_domain,
            "organization": None,
        }

    def _login_grok(
        self,
        gen: int,
        account_id: Optional[str],
        open_browser: bool,
        cancel_event: threading.Event,
    ) -> Dict[str, Any]:
        tok = _device_authorization(
            provider="xAI",
            start_url=GROK_DEVICE_START_URL,
            token_url=GROK_TOKEN_URL,
            client_id=GROK_CLIENT_ID,
            scopes=GROK_SCOPES,
            denied_message=tr("auth.deviceFailedFor", "%1 device sign-in failed.", "xAI"),
            http=self._http,
            cancel_event=cancel_event,
            on_device_prompt=lambda code, url: self._prompt_device_code(
                gen, code, url, open_browser=open_browser, provider_name="Grok"
            ),
        )
        access_token = str(tok.get("access_token") or "")
        if not access_token:
            raise LoginError(tr("auth.invalidResponse", "The sign-in service returned an invalid response."), code="invalid_response")

        claims = decode_jwt_payload(access_token) or {}
        email = claims.get("email") if isinstance(claims.get("email"), str) else None
        sub = claims.get("sub") if isinstance(claims.get("sub"), str) else None
        identity = sub or email
        extras = {ACCOUNT_IDENTITY_KEY: identity} if identity else {}

        credential = _make_oauth_credential(
            access_token,
            refresh_token=tok.get("refresh_token"),
            expires_at=_expires_at_from_seconds(tok.get("expires_in")),
            extras=extras,
        )
        return {
            "kind": "grok",
            "method": "device",
            "accountId": account_id,
            "label": email or "Grok Account",
            "credential": credential,
            "identity": identity,
            "domain": None,
            "organization": None,
        }

    def _login_muse(
        self,
        gen: int,
        account_id: Optional[str],
        open_browser: bool,
        cancel_event: threading.Event,
    ) -> Dict[str, Any]:
        tok = _device_authorization(
            provider="Meta",
            start_url=MUSE_DEVICE_START_URL,
            token_url=MUSE_TOKEN_URL,
            client_id=MUSE_CLIENT_ID,
            scopes=None,
            denied_message=tr("auth.deviceDenied", "%1 device sign-in was denied.", "Meta"),
            http=self._http,
            cancel_event=cancel_event,
            on_device_prompt=lambda code, url: self._prompt_device_code(
                gen, code, url, open_browser=open_browser, provider_name="Muse Code"
            ),
        )
        access_token = str(tok.get("access_token") or "")
        if not access_token:
            raise LoginError(tr("auth.invalidResponse", "The sign-in service returned an invalid response."), code="invalid_response")

        self._update_session(gen, status="exchanging", message=tr("auth.fetchingAccount", "Fetching %1 account...", "Muse Code"))
        key_resp = self._http.post(
            MUSE_KEY_URL,
            json_body={"onboard": False},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "x-api-version": "1.0.0",
            },
        )
        raise_for_http_error(key_resp, "Muse Code")
        acct = _safe_json(key_resp)
        raw_email = acct.get("user_email")
        email = raw_email.strip() if isinstance(raw_email, str) and raw_email.strip() else None

        identity = f"muse:{email}" if email else None
        extras = {ACCOUNT_IDENTITY_KEY: identity} if identity else {}
        credential = _make_oauth_credential(access_token, extras=extras)
        return {
            "kind": "muse",
            "method": "device",
            "accountId": account_id,
            "label": email or "Muse Code",
            "credential": credential,
            "identity": identity,
            "domain": None,
            "organization": None,
        }

    def _login_amp(
        self,
        gen: int,
        account_id: Optional[str],
        open_browser: bool,
        cancel_event: threading.Event,
    ) -> Dict[str, Any]:
        tok = _device_authorization(
            provider="Amp",
            start_url=AMP_DEVICE_START_URL,
            token_url=AMP_AUTHENTICATE_URL,
            client_id=AMP_CLIENT_ID,
            scopes=None,
            verification_page=AMP_VERIFICATION_PAGE,
            denied_message=tr("auth.deviceDenied", "%1 device sign-in was denied.", "Amp"),
            http=self._http,
            cancel_event=cancel_event,
            on_device_prompt=lambda code, url: self._prompt_device_code(
                gen, code, url, open_browser=open_browser, provider_name="Amp"
            ),
        )
        access_token = str(tok.get("access_token") or "")
        if not access_token:
            raise LoginError(tr("auth.invalidResponse", "The sign-in service returned an invalid response."), code="invalid_response")

        self._update_session(gen, status="exchanging", message=tr("auth.verifyingAccount", "Verifying %1 account...", "Amp"))
        bal_resp = self._http.post(
            AMP_BALANCE_URL,
            json_body={"method": "userDisplayBalanceInfo", "params": {}},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        raise_for_http_error(bal_resp, "Amp")
        bal_json = _safe_json(bal_resp)
        result_obj = bal_json.get("result") if isinstance(bal_json.get("result"), dict) else {}
        display_text = str(result_obj.get("displayText") or "").replace("**", "")
        match = re.search(r"Signed in as ([^\s(]+)", display_text)
        email = match.group(1).strip() if match else None

        claims = decode_jwt_payload(access_token) or {}
        sub = claims.get("sub") if isinstance(claims.get("sub"), str) else None
        identity = f"amp:{sub}" if sub else None
        extras = {ACCOUNT_IDENTITY_KEY: identity} if identity else {}

        credential = _make_oauth_credential(
            access_token,
            refresh_token=tok.get("refresh_token"),
            expires_at=jwt_expiry_iso(access_token),
            extras=extras,
        )
        return {
            "kind": "amp",
            "method": "device",
            "accountId": account_id,
            "label": email or "Amp",
            "credential": credential,
            "identity": identity,
            "domain": None,
            "organization": None,
        }

    def _login_opencode(
        self,
        gen: int,
        account_id: Optional[str],
        open_browser: bool,
        cancel_event: threading.Event,
    ) -> Dict[str, Any]:
        tok = _device_authorization(
            provider="OpenCode",
            start_url=OPENCODE_DEVICE_START_URL,
            token_url=OPENCODE_TOKEN_URL,
            client_id=OPENCODE_CLIENT_ID,
            scopes=None,
            start_fields={"supports_org_scope": "true"},
            denied_message=tr("auth.deviceDenied", "%1 device sign-in was denied.", "OpenCode"),
            http=self._http,
            cancel_event=cancel_event,
            on_device_prompt=lambda code, url: self._prompt_device_code(
                gen, code, url, open_browser=open_browser, provider_name="OpenCode"
            ),
        )
        access_token = str(tok.get("access_token") or "")
        org_id = str(tok.get("org_id") or "").strip()
        if not access_token or not org_id:
            raise LoginError(tr("auth.invalidResponse", "The sign-in service returned an invalid response."), code="invalid_response")

        self._update_session(gen, status="exchanging", message=tr("auth.fetchingProfile", "Fetching %1 profile...", "OpenCode"))
        user_resp = self._http.get(
            OPENCODE_USER_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "x-org-id": org_id,
            },
        )
        user = _decode_json_response(user_resp, context=tr("auth.context.profile", "%1 profile", "OpenCode"))
        user_id = str(user.get("id") or "").strip()
        email = str(user.get("email") or "").strip()
        if not user_id or not email:
            raise LoginError(tr("auth.invalidResponse", "The sign-in service returned an invalid response."), code="invalid_response")

        identity = f"opencode:{user_id}:{org_id}"
        extras = {
            ACCOUNT_IDENTITY_KEY: identity,
            OPENCODE_ORG_KEY: org_id,
        }
        credential = _make_oauth_credential(
            access_token,
            refresh_token=tok.get("refresh_token"),
            expires_at=_expires_at_from_seconds(tok.get("expires_in")),
            extras=extras,
        )
        return {
            "kind": "opencode",
            "method": "device",
            "accountId": account_id,
            "label": email,
            "credential": credential,
            "identity": identity,
            "domain": None,
            "organization": None,
        }
