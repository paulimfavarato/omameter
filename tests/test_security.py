"""Security hardening tests for HTTP transport, file permissions, OAuth callbacks, and input validation."""

from __future__ import annotations

import errno
import io
import os
import shutil
import socket
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from backend.accounts import API_KEY, AccountLifecycle
from backend.auth import LoginError, LoopbackCallbackServer, parse_pasted_authorization_code
from backend.cliproxy import HubTunnelHTTPClient
from backend.daemon import OmameterDaemon
from backend.http_client import (
    HTTPClient,
    HTTPTransportError,
    ProxyConfig,
    _SafeRedirectHandler,
    redact_secrets,
)
from backend.models import Account, CLIProxyHub, ProviderSnapshot
from backend.pace import send_desktop_notification
from backend.providers.devin import _resolve_api_server_url
from backend.store import OmameterStore
from backend.vault import CredentialVault


class TestSafeRedirectHandler(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = _SafeRedirectHandler()

    def test_cross_origin_redirect_strips_sensitive_headers(self) -> None:
        req = urllib.request.Request(
            "https://api.example.com/v1/usage",
            headers={
                "Authorization": "Bearer super-secret",
                "Cookie": "session=secret-cookie",
                "Proxy-Authorization": "Basic dXNlcjpwYXNz",
                "X-Api-Key": "sk-secret-key",
                "Accept": "application/json",
            },
        )
        new_req = self.handler.redirect_request(req, None, 302, "Found", {}, "https://other.example.net/target")
        self.assertIsNotNone(new_req)
        lower_headers = {k.lower(): v for k, v in new_req.header_items()}
        self.assertNotIn("authorization", lower_headers)
        self.assertNotIn("cookie", lower_headers)
        self.assertNotIn("proxy-authorization", lower_headers)
        self.assertNotIn("x-api-key", lower_headers)
        self.assertEqual(lower_headers.get("accept"), "application/json")

    def test_same_origin_redirect_preserves_headers(self) -> None:
        req = urllib.request.Request(
            "https://api.example.com/v1/usage",
            headers={"Authorization": "Bearer keep-me", "Accept": "application/json"},
        )
        new_req = self.handler.redirect_request(req, None, 307, "Temporary Redirect", {}, "https://api.example.com/v2/usage")
        self.assertIsNotNone(new_req)
        lower_headers = {k.lower(): v for k, v in new_req.header_items()}
        self.assertEqual(lower_headers.get("authorization"), "Bearer keep-me")

    def test_blocks_https_to_http_downgrade(self) -> None:
        req = urllib.request.Request("https://api.example.com/v1/usage")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.handler.redirect_request(req, None, 302, "Found", {}, "http://api.example.com/v1/usage")
        ctx.exception.close()

    def test_blocks_non_http_scheme_redirect(self) -> None:
        req = urllib.request.Request("https://api.example.com/v1/usage")
        with self.assertRaises(urllib.error.HTTPError) as ctx1:
            self.handler.redirect_request(req, None, 302, "Found", {}, "file:///etc/passwd")
        ctx1.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as ctx2:
            self.handler.redirect_request(req, None, 302, "Found", {}, "ftp://example.com/secret")
        ctx2.exception.close()

    def test_http_client_rejects_non_http_schemes(self) -> None:
        client = HTTPClient()
        with self.assertRaises(HTTPTransportError):
            client.request("GET", "file:///etc/passwd")
        with self.assertRaises(HTTPTransportError):
            client.request("GET", "ftp://example.com/file")


class TestCurlSecretIsolation(unittest.TestCase):
    def test_curl_never_passes_secrets_on_argv(self) -> None:
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            captured["pass_fds"] = kwargs.get("pass_fds", ())
            # Read the config passed via -K to verify secrets are inside the private stream
            k_idx = cmd.index("-K")
            cfg_target = cmd[k_idx + 1]
            if cfg_target.startswith("/dev/fd/"):
                fd = int(cfg_target.split("/")[-1])
                captured["cfg"] = os.read(fd, 65536).decode("utf-8")
            else:
                captured["cfg"] = Path(cfg_target).read_text(encoding="utf-8")
            return mock.Mock(
                returncode=0,
                stdout=b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"ok\":true}",
                stderr=b"",
            )

        client = HTTPClient(proxy=ProxyConfig(enabled=True, url="socks5://alice:proxypass@127.0.0.1:1080"))
        with mock.patch("backend.http_client.subprocess.run", side_effect=fake_run):
            resp = client._request_curl(
                "POST",
                "https://api.example.com/v1/test?verifier=secret_verifier_123",
                headers={
                    "Authorization": "Bearer secret_bearer_token",
                    "Cookie": "WorkosCursorSessionToken=secret_cookie_val",
                },
                body=b'{"a":1}',
                timeout=10.0,
            )

        self.assertEqual(resp.status, 200)
        argv_joined = " ".join(captured["cmd"])
        self.assertIn("-K", captured["cmd"])
        self.assertNotIn("secret_bearer_token", argv_joined)
        self.assertNotIn("secret_cookie_val", argv_joined)
        self.assertNotIn("proxypass", argv_joined)
        self.assertNotIn("secret_verifier_123", argv_joined)
        self.assertIn("Authorization: Bearer secret_bearer_token", captured["cfg"])
        self.assertIn("proxy = \"socks5://alice:proxypass@127.0.0.1:1080\"", captured["cfg"])

    def test_curl_rejects_header_newline_injection(self) -> None:
        client = HTTPClient()
        with self.assertRaises(HTTPTransportError):
            client._request_curl(
                "GET",
                "https://api.example.com/v1/test",
                headers={"X-Test": "ok\nheader = \"Injected: yes\""},
                body=None,
                timeout=5.0,
            )

    @unittest.skipUnless(shutil.which("curl"), "curl binary not installed")
    def test_curl_round_trip_against_local_server(self) -> None:
        received: dict = {}

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002
                return

            def do_GET(self):  # noqa: N802
                received["auth"] = self.headers.get("Authorization")
                body = b'{"status":"ok"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = HTTPServer(("127.0.0.1", 0), _Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            port = srv.server_address[1]
            client = HTTPClient()
            resp = client.request(
                "GET",
                f"http://127.0.0.1:{port}/ping",
                headers={"Authorization": "Bearer local-curl-secret"},
                use_curl=True,
                timeout=5.0,
            )
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.json(), {"status": "ok"})
            self.assertEqual(received.get("auth"), "Bearer local-curl-secret")
        finally:
            srv.shutdown()
            srv.server_close()


class TestRedactSecrets(unittest.TestCase):
    def test_redacts_new_secret_patterns(self) -> None:
        samples = [
            ("Authorization: token ghp_1234567890abcdef", "ghp_1234567890abcdef"),
            ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
            ("Cookie: WorkosCursorSessionToken=user_01ABC%3A%3Asecretval; foo=bar", "user_01ABC%3A%3Asecretval"),
            ("Bearer devin-session-token$supersecretvalue", "supersecretvalue"),
            ("token github_pat_11ABCDEF0123456789_abcdefghijklmnop", "github_pat_11ABCDEF0123456789_abcdefghijklmnop"),
            ("https://api2.cursor.sh/auth/poll?uuid=123&verifier=my_pkce_verifier", "my_pkce_verifier"),
        ]
        for raw, secret in samples:
            redacted = redact_secrets(raw)
            self.assertNotIn(secret, redacted, f"Failed to redact secret in: {raw!r} -> {redacted!r}")
            self.assertIn("[REDACTED]", redacted)


class TestFilePermissions(unittest.TestCase):
    def test_vault_proxy_and_store_use_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            vault = CredentialVault(fallback_path=root / "vault_dir" / "vault.json", use_secret_tool=False)
            vault.set_account_credential("acc1", {"apiKey": "sk-secret"})
            self.assertEqual(stat.S_IMODE((root / "vault_dir").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((root / "vault_dir" / "vault.json").stat().st_mode), 0o600)

            proxy_path = root / "proxy_dir" / "proxy.json"
            ok, err = ProxyConfig.save(True, "socks5://127.0.0.1:1080", path=proxy_path)
            self.assertTrue(ok, err)
            self.assertEqual(stat.S_IMODE(proxy_path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(proxy_path.stat().st_mode), 0o600)

            config_dir = root / "store_cfg"
            cache_dir = root / "store_cache"
            store = OmameterStore(config_dir=config_dir, cache_dir=cache_dir)
            acc = Account.create("claude", "Work")
            store.accounts.append(acc)
            store.snapshots[acc.id] = ProviderSnapshot(
                account_id=acc.id,
                card_id=acc.card_id,
                kind="claude",
            )
            store.save_accounts()
            store.save_snapshots()
            self.assertEqual(stat.S_IMODE(config_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(cache_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(store.accounts_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(store.snapshots_path.stat().st_mode), 0o600)


class TestHubBaseUrlBinding(unittest.TestCase):
    def test_make_http_client_refuses_mismatched_hub_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = OmameterStore(config_dir=root / "cfg", cache_dir=root / "cache")
            vault = CredentialVault(fallback_path=root / "vault.json", use_secret_tool=False)
            hub = CLIProxyHub(id="hub-1", base_url="http://127.0.0.1:8317", host="127.0.0.1:8317")
            store.add_hub(hub)
            vault.set_hub_key(hub.id, "http://127.0.0.1:8317", "mgmt-secret")

            acc = Account.create("claude", "Mirrored", hub_id=hub.id, hub_auth_index="idx-1")
            store.accounts.append(acc)

            daemon = OmameterDaemon(store=store, vault=vault, out=io.StringIO())
            try:
                # Matching baseUrl returns HubTunnelHTTPClient
                client_ok = daemon._make_http_client(acc)
                self.assertIsInstance(client_ok, HubTunnelHTTPClient)

                # Tampered hub.base_url must NOT return HubTunnelHTTPClient
                hub.base_url = "https://attacker.example.com"
                client_bad = daemon._make_http_client(acc)
                self.assertNotIsInstance(client_bad, HubTunnelHTTPClient)
            finally:
                daemon._pool.shutdown(wait=False)


class TestOAuthCallbackHardening(unittest.TestCase):
    def test_callback_headers_and_state_check(self) -> None:
        cb = LoopbackCallbackServer(port=0, path="/callback", state="expected-state-token", redirect_host="127.0.0.1")
        try:
            with urllib.request.urlopen(f"{cb.redirect_uri}?code=abc123&state=expected-state-token", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers.get("Cache-Control"), "no-store")
                self.assertEqual(resp.headers.get("Referrer-Policy"), "no-referrer")
            self.assertEqual(cb.wait_for_code(timeout=2.0), "abc123")
        finally:
            cb.cancel()

    def test_manual_code_state_mismatch_raises(self) -> None:
        with self.assertRaises(LoginError):
            parse_pasted_authorization_code("code123#wrong-state", "expected-state")
        self.assertEqual(
            parse_pasted_authorization_code("code123#expected-state", "expected-state"),
            "code123",
        )

    def test_ipv6_eaddrinuse_fails_closed_on_fixed_port(self) -> None:
        if not socket.has_ipv6:
            self.skipTest("IPv6 not supported")
        sock6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            sock6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock6.bind(("::1", 0))
            sock6.listen(1)
        except OSError as exc:
            sock6.close()
            if exc.errno in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL, errno.EPROTONOSUPPORT):
                self.skipTest("IPv6 loopback unavailable")
            raise
        occupied_port = sock6.getsockname()[1]
        try:
            with self.assertRaises(LoginError) as ctx:
                LoopbackCallbackServer(port=occupied_port, path="/callback", state="st", redirect_host="localhost")
            self.assertEqual(ctx.exception.code, "listener_failed")
        finally:
            sock6.close()


class TestMiscHardening(unittest.TestCase):
    def test_devin_api_server_url_validation(self) -> None:
        self.assertEqual(_resolve_api_server_url({"api_server_url": "https://api.devin.ai/v1"}), "https://api.devin.ai/v1")
        self.assertEqual(_resolve_api_server_url({"api_server_url": "http://127.0.0.1:8080"}), "http://127.0.0.1:8080")
        self.assertEqual(_resolve_api_server_url({"api_server_url": "http://localhost:8080"}), "http://localhost:8080")
        # Remote cleartext HTTP or non-HTTP schemes must fall back to default HTTPS endpoint
        default_url = _resolve_api_server_url({})
        self.assertEqual(_resolve_api_server_url({"api_server_url": "http://evil.example.com"}), default_url)
        self.assertEqual(_resolve_api_server_url({"api_server_url": "file:///etc/passwd"}), default_url)

    def test_notify_send_uses_end_of_options_and_escapes_markup(self) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            calls.append(list(cmd))
            return mock.Mock(returncode=0)

        with mock.patch("backend.pace.shutil.which", return_value="/usr/bin/notify-send"), \
             mock.patch("backend.pace.subprocess.run", side_effect=fake_run):
            ok = send_desktop_notification(
                title="--evil-flag",
                subtitle="sub",
                body="<a href='http://evil'>click</a> & more",
                urgency="normal",
            )
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        cmd = calls[0]
        self.assertIn("--", cmd)
        sep_idx = cmd.index("--")
        self.assertEqual(cmd[sep_idx + 1], "--evil-flag — sub")
        self.assertEqual(cmd[sep_idx + 2], "&lt;a href='http://evil'&gt;click&lt;/a&gt; &amp; more")

    def test_account_links_encodes_organization(self) -> None:
        acc = Account.create("copilot", "Org", organization="my org/evil?x=1#frag")
        by_id = {lnk["id"]: lnk["url"] for lnk in acc.links()}
        self.assertEqual(
            by_id["orgBilling"],
            "https://github.com/organizations/my%20org%2Fevil%3Fx%3D1%23frag/settings/billing",
        )

    def test_admit_rejects_mismatched_kind_or_hub_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = OmameterStore(config_dir=root / "cfg", cache_dir=root / "cache")
            vault = CredentialVault(fallback_path=root / "vault.json", use_secret_tool=False)
            lifecycle = AccountLifecycle(
                store=store,
                vault=vault,
                refresher=mock.Mock(),
                burning_tracker=mock.Mock(),
                notifier=mock.Mock(),
            )

            other = Account.create("claude", "Claude Account")
            hub_acc = Account.create("openrouter", "Hub Account", hub_id="hub-1", hub_auth_index="idx-1")
            store.accounts.extend([other, hub_acc])

            res_wrong_kind = lifecycle.admit(
                "openrouter",
                {"type": "api_key", "apiKey": "sk-or-test"},
                label="OR",
                rule=API_KEY,
                target_id=other.id,
            )
            self.assertEqual(res_wrong_kind.outcome, "missing")
            self.assertIsNone(res_wrong_kind.account)

            res_hub = lifecycle.admit(
                "openrouter",
                {"type": "api_key", "apiKey": "sk-or-test"},
                label="OR",
                rule=API_KEY,
                target_id=hub_acc.id,
            )
            self.assertEqual(res_hub.outcome, "missing")
            self.assertIsNone(res_hub.account)


if __name__ == "__main__":
    unittest.main()
