"""CLIProxyAPI hub address validation, api-call HTTP tunnel, auth-file listing, and account mirror sync."""

from __future__ import annotations

import ipaddress
import json
import threading
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.http_client import HTTPClient, HTTPResponse, HTTPTransportError
from backend.i18n import tr
from backend.models import (
    Account,
    CLIProxyHub,
    default_account_label,
    format_iso8601,
    utc_now,
)
from backend.providers.base import ProviderError
from backend.store import OmameterStore
from backend.vault import CredentialVault


TOKEN_PLACEHOLDER = "$TOKEN$"

_STRIP_PATH_SUFFIXES: Tuple[str, ...] = (
    "/",
    "/management.html",
    "/v0/management",
    "/v8/management",
    "/v1",
)

_TAILSCALE_IPV4_NET = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_IPV6_NET = ipaddress.ip_network("fd7a:115c:a1e0::/48")
_ULA_IPV6_NET = ipaddress.ip_network("fc00::/7")
_LINK_LOCAL_IPV6_NET = ipaddress.ip_network("fe80::/10")
_PRIVATE_IPV4_NETS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    _TAILSCALE_IPV4_NET,
)

# Maps CLIProxyAPI provider names to Omameter provider kinds.
_HUB_PROVIDER_MAP: Dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
    "antigravity": "antigravity",
    "devin": "devin",
    "xai": "grok",
}


# ---------------------------------------------------------------------------
# Address normalization & transport-security validation
# ---------------------------------------------------------------------------

def format_hub_host(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    hostname = parsed.hostname
    if not hostname:
        return url
    try:
        port = parsed.port
    except ValueError:
        port = None
    display_host = f"[{hostname}]" if ":" in hostname else hostname
    return f"{display_host}:{port}" if port is not None else display_host


def allows_hub_key(url: str) -> bool:
    """Return True if the management key may be sent to ``url``.

    - ``https://`` is allowed for any host.
    - ``http://`` is allowed ONLY for localhost, private/link-local LAN addresses,
      ``.local`` names, or Tailscale peers (``*.ts.net``, ``100.64.0.0/10``, ``fd7a:115c:a1e0::/48``).
    """
    parsed = urllib.parse.urlsplit(url)
    scheme = (parsed.scheme or "").lower()
    if scheme == "https":
        return bool(parsed.hostname)
    if scheme != "http":
        return False

    raw_host = parsed.hostname
    if not raw_host:
        return False
    host = raw_host.lower().strip("[]")
    if not host:
        return False

    if host == "localhost" or host.endswith(".local") or host.endswith(".ts.net"):
        return True

    # Strip IPv6 zone identifier if present (e.g. fe80::1%eth0)
    addr_str = host.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(addr_str)
    except ValueError:
        return False

    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        else:
            return (
                ip.is_loopback
                or ip in _LINK_LOCAL_IPV6_NET
                or ip in _TAILSCALE_IPV6_NET
                or ip in _ULA_IPV6_NET
            )

    return any(ip in net for net in _PRIVATE_IPV4_NETS)


def normalize_hub_address(raw: str) -> Tuple[str, str]:
    """Normalize a user-entered CLIProxyAPI hub address into ``(base_url, host)``.

    - Defaults scheme to ``https://`` when omitted.
    - Strips query, fragment, userinfo, and trailing ``/management.html``, ``/v0/management``,
      ``/v8/management``, ``/v1``, and ``/`` segments (while preserving any reverse-proxy prefix).
    - Validates that plain ``http://`` is used only on localhost, private/link-local LAN,
      ``.local``, or Tailscale addresses.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError(
            tr(
                "hub.addressInvalid",
                "Enter the hub's address, like https://hub.example.com or http://192.168.1.20:8317.",
            )
        )
    if "://" not in text:
        text = "https://" + text

    parsed = urllib.parse.urlsplit(text)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(
            tr(
                "hub.addressInvalid",
                "Enter the hub's address, like https://hub.example.com or http://192.168.1.20:8317.",
            )
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(tr("hub.portInvalid", "The hub address has an invalid port.")) from exc

    hostname = parsed.hostname
    netloc_host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = f"{netloc_host}:{port}" if port is not None else netloc_host

    path = parsed.path or ""
    changed = True
    while changed and path:
        changed = False
        lowered = path.lower()
        for suffix in _STRIP_PATH_SUFFIXES:
            if lowered.endswith(suffix):
                path = path[: -len(suffix)]
                lowered = path.lower()
                changed = True

    base_url = urllib.parse.urlunsplit((scheme, netloc, path, "", ""))
    host = format_hub_host(base_url)

    if not allows_hub_key(base_url):
        raise ValueError(
            tr(
                "hub.httpsRequired",
                "Use https for %1. Omameter uses http only for a hub on this machine, "
                "your local network, or Tailscale.",
                host,
            )
        )
    return base_url, host


def hub_account_credential(chatgpt_account_id: Optional[str] = None) -> Dict[str, Any]:
    """Build the placeholder OAuth credential stored for a hub-mirrored account."""
    extras: Dict[str, str] = {}
    if chatgpt_account_id:
        extras["chatgpt_account_id"] = chatgpt_account_id
    return {
        "type": "oauth",
        "accessToken": TOKEN_PLACEHOLDER,
        "refreshToken": None,
        "expiresAt": None,
        "extras": extras,
    }


# ---------------------------------------------------------------------------
# Hub error classification & per-hub IP-ban gate
# ---------------------------------------------------------------------------

def _classify_hub_refusal(
    resp: HTTPResponse,
    *,
    host: str,
    upstream: Optional[str] = None,
) -> Optional[Tuple[str, ProviderError, bool]]:
    """Classify a non-2xx management API response into ``(reason, ProviderError, closes_gate)``."""
    if 200 <= resp.status < 300:
        return None

    msg = ""
    try:
        parsed = resp.json()
        if isinstance(parsed, dict) and isinstance(parsed.get("error"), str):
            msg = parsed["error"].lower()
    except Exception:
        msg = resp.text.lower()

    if resp.status == 401:
        return (
            "key_rejected",
            ProviderError(
                tr(
                    "hub.keyRejected",
                    "The CLIProxyAPI hub at %1 rejected the management key. Replace it in Accounts.",
                    host,
                ),
                kind="api_key_rejected",
                status_code=401,
            ),
            True,
        )
    if resp.status == 403 and msg.startswith("ip banned"):
        return (
            "banned",
            ProviderError(
                tr(
                    "hub.ipBanned",
                    "The CLIProxyAPI hub at %1 blocked this machine after too many wrong keys. "
                    "Wait 30 minutes, then replace the management key in Accounts.",
                    host,
                ),
                kind="refused",
                status_code=403,
            ),
            True,
        )
    if resp.status == 403 and "remote management disabled" in msg:
        return (
            "remote_management_off",
            ProviderError(
                tr(
                    "hub.remoteManagementOff",
                    "The CLIProxyAPI hub at %1 only allows management from its own machine. "
                    "Turn on allow-remote in its config.",
                    host,
                ),
                kind="refused",
                status_code=403,
            ),
            False,
        )
    if resp.status == 404:
        return (
            "management_off",
            ProviderError(
                tr(
                    "hub.managementOff",
                    "The CLIProxyAPI hub at %1 has its management API turned off. "
                    "Set a management key in its config.",
                    host,
                ),
                kind="refused",
                status_code=404,
            ),
            False,
        )
    if resp.status == 400 and (
        "auth credential not found" in msg
        or "auth_file_not_found" in msg
        or "auth file not found" in msg
    ):
        return (
            "login_missing",
            ProviderError(
                tr("hub.loginMissing", "This login is no longer on the CLIProxyAPI hub at %1.", host),
                kind="auth_expired",
                status_code=400,
            ),
            False,
        )
    if resp.status == 400 and "token" in msg:
        return (
            "no_token",
            ProviderError(
                tr(
                    "hub.noToken",
                    "The CLIProxyAPI hub at %1 has no working sign-in for this login. "
                    "Sign it in again on the hub.",
                    host,
                ),
                kind="auth_expired",
                status_code=400,
            ),
            False,
        )
    if resp.status == 502:
        return (
            "upstream_unreachable",
            ProviderError(
                tr("hub.upstreamUnreachable", "The CLIProxyAPI hub at %1 couldn't reach %2.", host, upstream)
                if upstream
                else tr("hub.providerUnreachable", "The CLIProxyAPI hub at %1 couldn't reach the provider.", host),
                kind="unreachable",
                status_code=502,
            ),
            False,
        )
    return (
        f"http_{resp.status}",
        ProviderError(
            tr("hub.httpError", "The CLIProxyAPI hub at %1 answered with an error (HTTP %2).", host, resp.status),
            kind="refused",
            status_code=resp.status,
        ),
        False,
    )


class CLIProxyHubGate:
    """Serializes requests per hub and stops sending a rejected key to avoid IP bans."""

    def __init__(self) -> None:
        self._meta_lock = threading.Lock()
        self._hub_locks: Dict[str, threading.Lock] = {}
        self._refused: Dict[str, Tuple[str, ProviderError]] = {}

    def _lock_for(self, hub_id: str) -> threading.Lock:
        with self._meta_lock:
            lock = self._hub_locks.get(hub_id)
            if lock is None:
                lock = threading.Lock()
                self._hub_locks[hub_id] = lock
            return lock

    def _check_refused(self, hub_id: str, key: str) -> Optional[ProviderError]:
        with self._meta_lock:
            entry = self._refused.get(hub_id)
            if entry is not None and entry[0] == key:
                return entry[1]
            return None

    def send(
        self,
        *,
        hub_id: str,
        host: str,
        key: str,
        fn: Callable[[], HTTPResponse],
    ) -> HTTPResponse:
        cached = self._check_refused(hub_id, key)
        if cached is not None:
            raise cached

        lock = self._lock_for(hub_id)
        with lock:
            cached = self._check_refused(hub_id, key)
            if cached is not None:
                raise cached
            resp = fn()
            refusal = _classify_hub_refusal(resp, host=host)
            if refusal is not None and refusal[2]:
                with self._meta_lock:
                    self._refused[hub_id] = (key, refusal[1])
                raise refusal[1]
            return resp

    def reopen(self, hub_id: str) -> None:
        with self._meta_lock:
            self._refused.pop(hub_id, None)


DEFAULT_HUB_GATE = CLIProxyHubGate()


def _management_request(
    *,
    method: str,
    path: str,
    base_url: str,
    management_key: str,
    http: HTTPClient,
    hub_id: Optional[str] = None,
    gate: Optional[CLIProxyHubGate] = None,
    json_body: Any = None,
    timeout: float = 15.0,
    upstream: Optional[str] = None,
    on_login_missing: Optional[Callable[[str], None]] = None,
) -> HTTPResponse:
    host = format_hub_host(base_url)
    if not allows_hub_key(base_url):
        raise ProviderError(
            tr(
                "hub.httpsRequired",
                "Use https for %1. Omameter uses http only for a hub on this machine, "
                "your local network, or Tailscale.",
                host,
            ),
            kind="refused",
        )

    url = f"{base_url.rstrip('/')}/v0/management/{path.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {management_key}",
        "Accept": "application/json",
    }
    if json_body is not None:
        headers["Content-Type"] = "application/json"

    def _do_send() -> HTTPResponse:
        return http.request(
            method,
            url,
            headers=headers,
            json_body=json_body,
            timeout=timeout,
        )

    eff_gate = gate if gate is not None else DEFAULT_HUB_GATE
    eff_hub_id = hub_id or base_url

    try:
        resp = eff_gate.send(hub_id=eff_hub_id, host=host, key=management_key, fn=_do_send)
    except ProviderError:
        raise
    except HTTPTransportError as exc:
        raise ProviderError(
            tr("hub.unreachable", "Can't reach the CLIProxyAPI hub at %1.", host),
            kind="offline" if exc.offline else "unreachable",
        ) from exc

    refusal = _classify_hub_refusal(resp, host=host, upstream=upstream)
    if refusal is not None:
        reason, err, _ = refusal
        if reason == "login_missing" and on_login_missing is not None and hub_id:
            try:
                on_login_missing(hub_id)
            except Exception:
                pass
        raise err

    return resp


# ---------------------------------------------------------------------------
# HubTunnelHTTPClient (subclasses HTTPClient to tunnel provider requests)
# ---------------------------------------------------------------------------

def unwrap_api_call_response(raw_body: bytes) -> Optional[HTTPResponse]:
    """Decode CLIProxyAPI ``api-call`` envelope: ``{status_code, header: {k: [v]}, body}``."""
    try:
        envelope = json.loads(raw_body.decode("utf-8", errors="replace"))
    except Exception:
        return None
    if not isinstance(envelope, dict):
        return None
    status_code = envelope.get("status_code")
    if not isinstance(status_code, int) or isinstance(status_code, bool):
        return None

    headers: Dict[str, str] = {}
    raw_headers = envelope.get("header")
    if isinstance(raw_headers, dict):
        for name, values in raw_headers.items():
            if isinstance(values, list):
                cleaned = [str(v) for v in values if v is not None]
                if cleaned:
                    headers[str(name).lower()] = ", ".join(cleaned)
            elif isinstance(values, str) and values:
                headers[str(name).lower()] = values

    body_text = envelope.get("body")
    body_bytes = body_text.encode("utf-8") if isinstance(body_text, str) else b""
    return HTTPResponse(status=status_code, headers=headers, body=body_bytes)


class HubTunnelHTTPClient(HTTPClient):
    """HTTPClient that tunnels provider HTTP calls through ``<hub>/v0/management/api-call``."""

    def __init__(
        self,
        *,
        hub_id: str,
        base_url: str,
        management_key: str,
        auth_index: str,
        upstream_name: str = "",
        base_http: Optional[HTTPClient] = None,
        gate: Optional[CLIProxyHubGate] = None,
        on_login_missing: Optional[Callable[[str], None]] = None,
    ) -> None:
        base = base_http or HTTPClient()
        super().__init__(proxy=base.proxy, timeout=base.timeout)
        self.hub_id = hub_id
        self.base_url = base_url.rstrip("/")
        self.management_key = management_key
        self.auth_index = auth_index
        self.upstream_name = upstream_name
        self._base_http = base
        self._gate = gate if gate is not None else DEFAULT_HUB_GATE
        self._on_login_missing = on_login_missing

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        body: Optional[bytes] = None,
        json_body: Any = None,
        form_body: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        use_curl: bool = False,
    ) -> HTTPResponse:
        req_headers = dict(headers or {})
        payload = body
        if json_body is not None:
            payload = json.dumps(json_body).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        elif form_body is not None:
            payload = urllib.parse.urlencode(form_body).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

        api_call_body: Dict[str, Any] = {
            "auth_index": self.auth_index,
            "method": method.upper(),
            "url": url,
            "header": req_headers,
        }
        if payload is not None:
            api_call_body["data"] = payload.decode("utf-8", errors="replace")

        eff_timeout = (timeout or self.timeout) + 10.0
        resp = _management_request(
            method="POST",
            path="api-call",
            base_url=self.base_url,
            management_key=self.management_key,
            http=self._base_http,
            hub_id=self.hub_id,
            gate=self._gate,
            json_body=api_call_body,
            timeout=eff_timeout,
            upstream=self.upstream_name,
            on_login_missing=self._on_login_missing,
        )
        unwrapped = unwrap_api_call_response(resp.body)
        if unwrapped is None:
            raise ProviderError(
                tr(
                    "hub.apiCallUnreadable",
                    "The CLIProxyAPI hub at %1 sent an api-call answer Omameter can't read.",
                    format_hub_host(self.base_url),
                ),
                kind="refused",
            )
        return unwrapped


# ---------------------------------------------------------------------------
# Auth-file listing (GET /v0/management/auth-files)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HubLogin:
    """One enabled entry from ``GET /v0/management/auth-files``."""

    id: str
    auth_index: Optional[str]
    provider: str
    kind: Optional[str]
    label: str
    is_oauth: bool
    is_disabled: bool
    chatgpt_account_id: Optional[str] = None

    @property
    def is_unsupported(self) -> bool:
        return (not self.is_disabled) and (self.kind is None) and (self.provider != "unknown")


def parse_hub_auth_files(raw_body: bytes) -> Tuple[List[HubLogin], int]:
    """Parse ``auth-files`` response into ``(supported_enabled_logins, unsupported_count)``."""
    try:
        payload = json.loads(raw_body.decode("utf-8", errors="replace"))
    except Exception as exc:
        raise ValueError("Invalid JSON in auth-files listing") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        raise ValueError("Missing files array in auth-files listing")

    supported: List[HubLogin] = []
    unsupported_count = 0

    for entry in payload["files"]:
        if not isinstance(entry, dict):
            continue
        raw_id = entry.get("id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            continue
        login_id = raw_id

        raw_idx = entry.get("auth_index")
        auth_index = raw_idx if isinstance(raw_idx, str) and raw_idx else None

        provider = str(entry.get("provider") or entry.get("type") or "").strip().lower()
        is_oauth = str(entry.get("account_type") or "oauth").strip().lower() == "oauth"
        is_disabled = bool(entry.get("disabled", False))

        label = login_id
        for field_name in ("email", "account", "label", "name"):
            val = entry.get(field_name)
            if isinstance(val, str) and val.strip():
                label = val.strip()
                break

        id_token = entry.get("id_token")
        chatgpt_account_id: Optional[str] = None
        if isinstance(id_token, dict):
            raw_acct = id_token.get("chatgpt_account_id")
            if isinstance(raw_acct, str) and raw_acct.strip():
                chatgpt_account_id = raw_acct.strip()

        kind = _HUB_PROVIDER_MAP.get(provider) if is_oauth else None

        login = HubLogin(
            id=login_id,
            auth_index=auth_index,
            provider=provider,
            kind=kind,
            label=label,
            is_oauth=is_oauth,
            is_disabled=is_disabled,
            chatgpt_account_id=chatgpt_account_id,
        )

        # Skip disabled logins and unreadable/non-login catalog entries (`provider == "unknown"`).
        if login.is_disabled:
            continue
        if login.kind is not None:
            supported.append(login)
        elif login.is_unsupported:
            unsupported_count += 1

    return supported, unsupported_count


def list_hub_auth_files(
    base_url: str,
    management_key: str,
    http: HTTPClient,
    *,
    hub_id: Optional[str] = None,
    gate: Optional[CLIProxyHubGate] = None,
) -> Tuple[List[HubLogin], int]:
    """Fetch ``GET /v0/management/auth-files`` and return ``(supported_logins, unsupported_count)``."""
    resp = _management_request(
        method="GET",
        path="auth-files",
        base_url=base_url,
        management_key=management_key,
        http=http,
        hub_id=hub_id,
        gate=gate,
        timeout=15.0,
    )
    try:
        return parse_hub_auth_files(resp.body)
    except ValueError as exc:
        host = format_hub_host(base_url)
        raise ProviderError(
            tr("hub.loginListUnreadable", "The CLIProxyAPI hub at %1 sent a login list Omameter can't read.", host),
            kind="refused",
        ) from exc


# ---------------------------------------------------------------------------
# Hub account mirror reconciliation (CLIProxyHubSync + HubReconciliation)
# ---------------------------------------------------------------------------

def _create_unique_hub_account(
    store: OmameterStore,
    *,
    kind: str,
    label: str,
    hub: CLIProxyHub,
    auth_index: str,
    file_name: str,
) -> Account:
    existing_card_ids = {acc.card_id for acc in store.accounts}
    while True:
        acc = Account.create(
            kind=kind,
            label=default_account_label(kind, label),
            hub_id=hub.id,
            hub_host=hub.host,
            hub_auth_index=auth_index,
            hub_file_name=file_name,
            auth_mode="hub",
        )
        if acc.card_id not in existing_card_ids:
            return acc


def sync_hub(
    hub: CLIProxyHub,
    management_key: str,
    store: OmameterStore,
    http: HTTPClient,
    *,
    vault: Optional[CredentialVault] = None,
    gate: Optional[CLIProxyHubGate] = None,
    remove_account: Optional[Callable[[str], Any]] = None,
) -> Dict[str, Any]:
    """Reconcile ``store.accounts`` with the hub's live ``auth-files`` listing.

    - Preserves existing mirrored account IDs, custom labels, pins, hidden metrics, and ordering
      when a login is renamed/reindexed or switches ChatGPT workspace.
    - Adds newly discovered enabled logins at the bottom.
    - Removes logins that were deleted or disabled on the hub through ``remove_account(id)``
      (returns the removed account or None). The daemon passes ``AccountLifecycle.remove`` so
      every per-account state goes; without it only the store and the vault are cleaned.
    """
    if remove_account is None:
        def remove_account(acc_id: str) -> Optional[Account]:
            if vault is not None:
                vault.delete_account_credential(acc_id)
            return store.remove_account(acc_id)

    try:
        supported_logins, unsupported_count = list_hub_auth_files(
            hub.base_url,
            management_key,
            http,
            hub_id=hub.id,
            gate=gate,
        )
    except ProviderError as exc:
        hub.last_error = str(exc)
        hub.key_rejected = exc.kind == "api_key_rejected"
        store.save_accounts()
        raise

    # Index enabled supported logins by stable login ID (first wins, matching Swift).
    listed: Dict[str, HubLogin] = {}
    for login in supported_logins:
        if login.id not in listed:
            listed[login.id] = login

    mirrored_ids: set[str] = set()
    to_remove: List[str] = []
    relinked_count = 0

    for acc in list(store.accounts):
        if acc.hub_id != hub.id:
            continue
        file_name = acc.hub_file_name or ""
        login = listed.get(file_name)
        if (
            login is None
            or login.is_disabled
            or login.kind != acc.kind
            or login.id in mirrored_ids
        ):
            to_remove.append(acc.id)
            continue

        mirrored_ids.add(login.id)

        # Keep existing account as-is if listed without an auth_index.
        if not login.auth_index:
            continue

        relinked = False
        if acc.hub_auth_index != login.auth_index:
            acc.hub_auth_index = login.auth_index
            relinked = True
        if acc.hub_host != hub.host:
            acc.hub_host = hub.host
            relinked = True
        if acc.auth_mode != "hub":
            acc.auth_mode = "hub"
            relinked = True

        if vault is not None:
            existing_cred = vault.get_account_credential(acc.id)
            existing_acct_id = (
                (existing_cred.get("extras") or {}).get("chatgpt_account_id")
                if isinstance(existing_cred, dict)
                else None
            )
            if existing_cred is None or existing_acct_id != login.chatgpt_account_id:
                vault.set_account_credential(acc.id, hub_account_credential(login.chatgpt_account_id))
                relinked = True

        if relinked:
            relinked_count += 1

    removed_count = 0
    for acc_id in to_remove:
        if remove_account(acc_id) is not None:
            removed_count += 1

    added_count = 0
    for login in supported_logins:
        if login.is_disabled or login.kind is None or not login.auth_index:
            continue
        if login.id in mirrored_ids:
            continue
        mirrored_ids.add(login.id)
        new_acc = _create_unique_hub_account(
            store,
            kind=login.kind,
            label=login.label,
            hub=hub,
            auth_index=login.auth_index,
            file_name=login.id,
        )
        if vault is not None:
            vault.set_account_credential(new_acc.id, hub_account_credential(login.chatgpt_account_id))
        store.add_account(new_acc)
        added_count += 1

    hub.last_synced_at = format_iso8601(utc_now())
    hub.last_error = None
    hub.unsupported_count = unsupported_count
    hub.key_rejected = False
    store.save_accounts()

    return {
        "added": added_count,
        "removed": removed_count,
        "relinked": relinked_count,
        "unsupportedCount": unsupported_count,
        "total": len(mirrored_ids),
    }
