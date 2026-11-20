"""Unit tests for all 13 provider mappers, CLIProxyAPI hub sync, resets, daemon NDJSON RPC, and CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.auth import generate_pkce, normalize_github_domain, parse_pasted_authorization_code
from backend.cliproxy import (
    CLIProxyHubGate,
    HubTunnelHTTPClient,
    normalize_hub_address,
    parse_hub_auth_files,
    sync_hub,
)
from backend.http_client import HTTPResponse
from backend.models import Account, CLIProxyHub, PROVIDER_CATALOG, format_iso8601
from backend.providers import SUPPORTED_PROVIDERS, get_fetcher
from backend.providers.amp import map_amp_usage
from backend.providers.antigravity import map_antigravity_quota
from backend.providers.claude import map_claude_usage
from backend.providers.codex import map_codex_usage
from backend.providers.copilot import map_copilot_org_billing, map_copilot_usage
from backend.providers.cursor import map_cursor_summary, map_cursor_usage
from backend.providers.devin import map_devin_usage
from backend.providers.grok import map_grok_credits
from backend.providers.muse import map_muse_usage
from backend.providers.ollama import map_ollama_balance
from backend.providers.opencode import map_opencode_usage
from backend.providers.openrouter import map_openrouter_credits, map_openrouter_key
from backend.providers.zai import map_zai_quota
from backend.resets import map_claude_claim_response, map_codex_claim_response, map_zai_claim_response
from backend.store import OmameterStore
from backend.vault import CredentialVault
from tests.fakes import FakeHTTP


NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)
REPO_ROOT = Path(__file__).resolve().parent.parent


class TestProviderRegistryAndMappers(unittest.TestCase):
    def test_all_13_providers_registered(self) -> None:
        self.assertEqual(set(SUPPORTED_PROVIDERS), set(PROVIDER_CATALOG.keys()))
        self.assertEqual(len(SUPPORTED_PROVIDERS), 13)
        for kind in PROVIDER_CATALOG:
            fn = get_fetcher(kind)
            self.assertTrue(callable(fn))

    def test_claude_mapper(self) -> None:
        payload = {
            "five_hour": {"utilization": 35.0, "resets_at": "2026-07-13T16:00:00Z"},
            "seven_day": {"utilization": 50.0, "resets_at": "2026-07-18T00:00:00Z"},
            "seven_day_sonnet": {"utilization": 12.0, "resets_at": "2026-07-18T00:00:00Z"},
            "extra_usage": {"is_enabled": True, "used_credits": 1250, "monthly_limit": 5000, "currency": "USD"},
        }
        lines = map_claude_usage(payload, plan="Max", now=NOW)
        keys = {l.resource_key for l in lines if l.resource_key}
        self.assertTrue({"session", "weekly", "sonnet", "extraUsage"}.issubset(keys))

    def test_codex_mapper(self) -> None:
        payload = {
            "plan_type": "pro",
            "rate_limit": {
                "primary_window": {"used_percent": 42, "limit_window_seconds": 18000, "reset_at": int((NOW + timedelta(hours=3)).timestamp())},
                "secondary_window": {"used_percent": 15, "limit_window_seconds": 604800, "reset_at": int((NOW + timedelta(days=5)).timestamp())},
            },
            "credits": {"has_credits": True, "unlimited": False, "balance": "250"},
        }
        plan, lines = map_codex_usage(payload, now=NOW)
        self.assertIsNotNone(plan)
        keys = {l.resource_key for l in lines if l.resource_key}
        self.assertTrue({"session", "weekly", "credits"}.issubset(keys))

    def test_cursor_mapper(self) -> None:
        summary = {
            "membershipType": "pro",
            "billingCycleEnd": "2026-08-01T00:00:00Z",
            "individualUsage": {
                "plan": {"enabled": True, "used": 2500, "limit": 5000, "totalPercentUsed": 50.0},
                "onDemand": {"enabled": True, "used": 400, "limit": 2000},
            },
        }
        plan, lines = map_cursor_summary(
            summary=summary,
            request_usage=None,
            plan_name="Pro",
            unavailable_message="No usage",
        )
        self.assertIsNotNone(plan)
        keys = {l.resource_key for l in lines if l.resource_key}
        self.assertTrue({"totalUsage", "onDemand"}.issubset(keys))
        self.assertTrue(callable(map_cursor_usage))

    def test_amp_mapper(self) -> None:
        body = {
            "ok": True,
            "result": {
                "displayText": "Signed in as dev@example.com\nPro Plan: $12.50 / $50.00 spent (renews Aug 1, 2026)\nIndividual credits: $20.00 remaining"
            },
        }
        plan, lines, email = map_amp_usage(body)
        self.assertEqual(email, "dev@example.com")
        self.assertTrue(len(lines) >= 1)

    def test_antigravity_mapper(self) -> None:
        payload = {
            "groups": [
                {
                    "displayName": "Gemini",
                    "buckets": [
                        {"bucketId": "gemini_five_hour", "remainingFraction": 0.7, "resetTime": "2026-07-13T16:00:00Z"},
                        {"bucketId": "gemini_weekly", "remainingFraction": 0.8, "resetTime": "2026-07-19T00:00:00Z"},
                    ],
                },
                {
                    "displayName": "Claude",
                    "buckets": [
                        {"bucketId": "non_gemini_five_hour", "remainingFraction": 0.6, "resetTime": "2026-07-13T16:00:00Z"},
                        {"bucketId": "non_gemini_weekly", "remainingFraction": 0.75, "resetTime": "2026-07-19T00:00:00Z"},
                    ],
                },
            ]
        }
        plan, lines = map_antigravity_quota([payload])
        self.assertTrue(isinstance(lines, list))

    def test_copilot_personal_and_org_mappers(self) -> None:
        personal = {
            "copilot_plan": "individual_pro",
            "quota_reset_date_utc": "2026-08-01T00:00:00Z",
            "quota_snapshots": {
                "premium_interactions": {"entitlement": 300, "remaining": 210, "percent_remaining": 70.0, "unlimited": False, "overage_count": 0, "overage_permitted": False},
                "chat": {"entitlement": 100, "remaining": 80, "percent_remaining": 80.0, "unlimited": False},
            },
        }
        plan, lines = map_copilot_usage(personal)
        self.assertIsNotNone(plan)
        keys = {l.resource_key for l in lines if l.resource_key}
        self.assertIn("premiumCredits", keys)

        org = {"usageItems": [{"product": "Copilot", "grossQuantity": 120.5, "netAmount": 14.25}]}
        org_lines = map_copilot_org_billing(org)
        org_keys = {l.resource_key for l in org_lines if l.resource_key}
        self.assertTrue({"orgCredits", "orgSpend"}.issubset(org_keys))

    def test_remaining_7_mappers(self) -> None:
        # Devin
        devin_plan, devin_lines, devin_email = map_devin_usage({
            "userStatus": {
                "email": "d@example.com",
                "planStatus": {
                    "planInfo": {"planName": "Teams"},
                    "dailyQuotaRemainingPercent": 80,
                    "dailyQuotaResetAtUnix": int((NOW + timedelta(hours=12)).timestamp()),
                    "weeklyQuotaRemainingPercent": 65,
                    "weeklyQuotaResetAtUnix": int((NOW + timedelta(days=4)).timestamp()),
                },
            }
        })
        self.assertEqual(devin_plan, "Teams")
        self.assertEqual(devin_email, "d@example.com")
        self.assertEqual(len(devin_lines), 2)
        # Grok
        grok_lines, grok_notice = map_grok_credits({
            "config": {
                "currentPeriod": {
                    "type": "USAGE_PERIOD_TYPE_WEEKLY",
                    "start": "2026-07-06T00:00:00Z",
                    "end": "2026-07-13T00:00:00Z",
                },
                "creditUsagePercent": 25.0,
            }
        })
        self.assertIsNone(grok_notice)
        self.assertEqual(len(grok_lines), 1)
        # Muse
        muse_res = map_muse_usage(
            {
                "is_subs_active": True,
                "subs_usage": {
                    "window": {"used_percent": 10},
                    "weekly": {"used_percent": 20},
                },
            },
            now=NOW,
        )
        self.assertIsNotNone(muse_res)
        # Ollama
        ollama_res = map_ollama_balance(
            {
                "included": {
                    "session": {"remaining_percent": 90.0},
                    "allowance_usd": 20.0,
                    "balance_usd": 15.0,
                }
            }
        )
        self.assertIsNotNone(ollama_res)
        # OpenCode
        opencode_res = map_opencode_usage(
            {
                "usage": {
                    "rolling": {"percent": 20, "resetsAt": "2026-07-01T15:00:00Z"},
                    "weekly": {"percent": 40, "resetsAt": "2026-07-08T00:00:00Z"},
                    "monthly": {"percent": 15, "resetsAt": "2026-08-01T00:00:00Z"},
                }
            },
            captured_at=NOW,
        )
        self.assertIsNotNone(opencode_res)
        # OpenRouter
        or_key = map_openrouter_key(
            {
                "data": {
                    "is_management_key": False,
                    "is_free_tier": False,
                    "usage_daily": 1.5,
                    "usage_weekly": 4.0,
                    "usage_monthly": 10.0,
                    "limit": 50.0,
                    "limit_remaining": 40.0,
                }
            }
        )
        self.assertIsNotNone(or_key)
        or_cred = map_openrouter_credits({"data": {"total_credits": 100.0, "total_usage": 25.0}})
        self.assertIsNotNone(or_cred)
        # Z.ai
        zai_res = map_zai_quota({"data": {"limits": [{"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 30}]}})
        self.assertIsNotNone(zai_res)

    def test_resets_mappers(self) -> None:
        self.assertTrue(map_claude_claim_response(HTTPResponse(200, {}, b'{"result":"reset"}'))["ok"])
        self.assertTrue(map_codex_claim_response(HTTPResponse(200, {}, b'{"code":"reset"}'))["ok"])
        self.assertTrue(map_zai_claim_response(HTTPResponse(200, {}, b'{"code":200,"success":true}'))["ok"])


class TestAuthAndCLIProxy(unittest.TestCase):
    def test_auth_helpers(self) -> None:
        pkce = generate_pkce()
        self.assertGreater(len(pkce.verifier), 30)
        self.assertGreater(len(pkce.challenge), 30)
        self.assertEqual(normalize_github_domain(""), "github.com")
        self.assertEqual(normalize_github_domain("octocorp"), "octocorp.ghe.com")
        self.assertEqual(normalize_github_domain("https://octocorp.ghe.com/"), "octocorp.ghe.com")
        code = parse_pasted_authorization_code("abc123#state1", "state1")
        self.assertEqual(code, "abc123")

    def test_cliproxy_normalize_and_sync(self) -> None:
        base, host = normalize_hub_address("http://127.0.0.1:8317/management.html")
        self.assertEqual(base, "http://127.0.0.1:8317")
        self.assertEqual(host, "127.0.0.1:8317")
        with self.assertRaises(ValueError):
            normalize_hub_address("http://public.example.com:8317")

        auth_files_payload = json.dumps({
            "files": [
                {"id": "claude-alice.json", "provider": "claude", "email": "alice@example.com", "auth_index": "idx1", "disabled": False},
                {"id": "codex-bob.json", "provider": "codex", "email": "bob@example.com", "auth_index": "idx2", "disabled": False, "id_token": {"chatgpt_account_id": "acct_bob"}},
                {"id": "cursor-skip.json", "provider": "cursor", "email": "c@example.com", "auth_index": "idx3", "disabled": False},
            ]
        }).encode("utf-8")

        supported, unsupported = parse_hub_auth_files(auth_files_payload)
        self.assertEqual(len(supported), 2)
        self.assertEqual(unsupported, 1)

        http = FakeHTTP(lambda m, u, kw: HTTPResponse(status=200, headers={}, body=auth_files_payload))
        with tempfile.TemporaryDirectory() as tmp:
            store = OmameterStore(config_dir=Path(tmp) / "cfg", cache_dir=Path(tmp) / "cache")
            vault = CredentialVault(fallback_path=Path(tmp) / "vault.json", use_secret_tool=False)
            hub = CLIProxyHub(id="hub-1", base_url="http://127.0.0.1:8317", host="127.0.0.1:8317")
            store.add_hub(hub)
            res = sync_hub(hub, "secret-key", store, http, vault=vault, gate=CLIProxyHubGate())
            self.assertEqual(res["added"], 2)
            self.assertEqual(len(store.accounts), 2)
            # Tunnel test
            tunnel_resp = json.dumps({"status_code": 200, "header": {"content-type": "application/json"}, "body": "{\"ok\":true}"}).encode("utf-8")
            tunnel_http = HubTunnelHTTPClient(
                hub_id=hub.id,
                base_url=hub.base_url,
                management_key="secret-key",
                auth_index="idx1",
                base_http=FakeHTTP(lambda m, u, kw: HTTPResponse(status=200, headers={}, body=tunnel_resp)),
            )
            resp = tunnel_http.get("https://api.anthropic.com/api/oauth/usage")
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.json(), {"ok": True})


class TestCLIAndDaemon(unittest.TestCase):
    def test_cli_exit_codes_and_json_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["XDG_CONFIG_HOME"] = tmp
            env["XDG_CACHE_HOME"] = tmp
            # Empty store -> exit 4, valid empty envelope
            p = subprocess.run([sys.executable, str(REPO_ROOT / "bin" / "omameter")], capture_output=True, text=True, env=env)
            self.assertEqual(p.returncode, 4)
            data = json.loads(p.stdout)
            self.assertEqual(data["schema"], "omameter.limits.v2")
            self.assertEqual(data["providers"], {})

            # Invalid arg -> exit 2
            p2 = subprocess.run([sys.executable, str(REPO_ROOT / "bin" / "omameter"), "not-a-provider"], capture_output=True, text=True, env=env)
            self.assertEqual(p2.returncode, 2)

            # Valid snapshot -> exit 0
            store = OmameterStore(config_dir=Path(tmp) / "omameter", cache_dir=Path(tmp) / "omameter")
            acc = Account.create("claude", "Personal")
            store.add_account(acc)
            from backend.models import MetricLine, ProviderSnapshot
            store.snapshots[acc.id] = ProviderSnapshot(
                account_id=acc.id,
                card_id=acc.card_id,
                kind="claude",
                plan="Max",
                lines=[MetricLine(key="session", title="Session", line_type="progress", kind="percent", used=40, limit=100, resource_key="session", resource_kind="consumption", resource_unit="percent")],
                refreshed_at=format_iso8601(NOW) or "",
                stale_at=format_iso8601(NOW + timedelta(hours=1)) or "",
            )
            store.save_snapshots()
            p3 = subprocess.run([sys.executable, str(REPO_ROOT / "bin" / "omameter"), "claude"], capture_output=True, text=True, env=env)
            self.assertEqual(p3.returncode, 0)
            data3 = json.loads(p3.stdout)
            self.assertIn(acc.card_id, data3["providers"])
            self.assertEqual(data3["providers"][acc.card_id]["resources"]["session"]["remaining"], 60.0)

    def test_daemon_ndjson_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["XDG_CONFIG_HOME"] = tmp
            env["XDG_CACHE_HOME"] = tmp
            env["XDG_DATA_HOME"] = tmp
            proc = subprocess.Popen(
                [sys.executable, "-u", str(REPO_ROOT / "backend" / "daemon.py")],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            try:
                assert proc.stdout is not None and proc.stdin is not None
                first_line = proc.stdout.readline()
                first_msg = json.loads(first_line)
                self.assertEqual(first_msg["type"], "state")
                self.assertEqual(len(first_msg["providerCatalog"]), 13)
                proc.stdin.write(json.dumps({"cmd": "update_settings", "patch": {"maxBarItems": 5}}) + "\n")
                proc.stdin.flush()
                # Read until we see updated settings
                found = False
                for _ in range(5):
                    line = proc.stdout.readline()
                    if not line:
                        break
                    msg = json.loads(line)
                    if msg.get("type") == "state" and msg.get("settings", {}).get("maxBarItems") == 5:
                        found = True
                        break
                self.assertTrue(found)
            finally:
                proc.terminate()
                proc.wait(timeout=3)
                for pipe in (proc.stdin, proc.stdout, proc.stderr):
                    if pipe is not None:
                        pipe.close()


class TestOAuthFixes(unittest.TestCase):
    def test_http_client_default_and_custom_user_agent(self) -> None:
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading
        from backend.http_client import HTTPClient

        seen_uas: list[str] = []

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def do_GET(self) -> None:  # noqa: N802
                seen_uas.append(self.headers.get("User-Agent", ""))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

        srv = HTTPServer(("127.0.0.1", 0), _Handler)
        t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        t.start()
        try:
            port = srv.server_address[1]
            client = HTTPClient()
            r1 = client.get(f"http://127.0.0.1:{port}/default")
            self.assertEqual(r1.status, 200)
            r2 = client.get(f"http://127.0.0.1:{port}/custom", headers={"user-agent": "custom-cli/2.0"})
            self.assertEqual(r2.status, 200)
            self.assertEqual(seen_uas, ["Omameter/1.0", "custom-cli/2.0"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_loopback_callback_server_ipv4_and_ipv6(self) -> None:
        import urllib.request
        from backend.auth import LoopbackCallbackServer

        # IPv4 callback
        srv4 = LoopbackCallbackServer(state="st-v4", port=0, path="/callback", redirect_host="localhost")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{srv4.port}/callback?code=abc4&state=st-v4", timeout=3) as resp:
                self.assertEqual(resp.status, 200)
            self.assertEqual(srv4.wait_for_code(timeout=2.0), "abc4")
        finally:
            srv4.cancel()

        # IPv6 callback when IPv6 loopback is available
        srv6 = LoopbackCallbackServer(state="st-v6", port=0, path="/callback", redirect_host="localhost")
        try:
            if srv6._server6 is not None:
                with urllib.request.urlopen(f"http://[::1]:{srv6.port}/callback?code=abc6&state=st-v6", timeout=3) as resp:
                    self.assertEqual(resp.status, 200)
                self.assertEqual(srv6.wait_for_code(timeout=2.0), "abc6")
        finally:
            srv6.cancel()

    def test_auth_manager_cancel_clears_error_session(self) -> None:
        from backend.auth import AuthManager

        updates: list[dict[str, Any]] = []
        mgr = AuthManager(on_update=lambda s: updates.append(dict(s)))
        with mgr._lock:
            mgr._session = {
                "active": False,
                "kind": "claude",
                "method": "oauth",
                "accountId": None,
                "status": "error",
                "verificationUrl": "",
                "userCode": "",
                "acceptsManualCode": False,
                "message": "",
                "error": "HTTP 403",
            }
        mgr.cancel()
        cur = mgr.current_session()
        self.assertEqual(cur["status"], "cancelled")
        self.assertEqual(cur["error"], "")
        self.assertFalse(cur["active"])
        self.assertTrue(len(updates) >= 1 and updates[-1]["status"] == "cancelled")


if __name__ == "__main__":
    unittest.main()
