"""Command outcomes over the NDJSON seam: every command with an `id` gets exactly one `result`."""

from __future__ import annotations

import concurrent.futures
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

from backend.daemon import OmameterDaemon
from backend.http_client import HTTPResponse, ProxyConfig
from backend.models import Account
from backend.pace import QuotaNotificationEvaluator
from backend.store import OmameterStore
from backend.vault import CredentialVault
from tests.fakes import FakeClock, FakeHTTP, InlineExecutor, RecordingExecutor


NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)
API_KEY_VALUE = "sk-or-v1-COMMANDSTESTKEY0123456789"
HUB_KEY = "secret-management-key-123"
HUB_BASE = "http://127.0.0.1:8317"
# cliproxy.DEFAULT_HUB_GATE remembers a rejected key per address for the whole process,
# so the rejection tests use their own key and never poison HUB_KEY.
WRONG_HUB_KEY = "wrong-management-key-456"


def openrouter_ok(method, url, kwargs):
    payload = {"data": {"is_management_key": False, "is_free_tier": False, "limit": 100,
                        "limit_remaining": 90, "limit_reset": "monthly", "usage_daily": 1,
                        "usage_weekly": 2, "usage_monthly": 3}}
    return HTTPResponse(status=200, headers={}, body=json.dumps(payload).encode("utf-8"))


def rejected(method, url, kwargs):
    return HTTPResponse(status=401, headers={}, body=b'{"error": "invalid key"}')


def hub_ok(method, url, kwargs):
    login = {"id": "claude-alice.json", "provider": "claude", "email": "alice@example.com",
             "auth_index": "idx1", "disabled": False}
    return HTTPResponse(status=200, headers={}, body=json.dumps({"files": [login]}).encode("utf-8"))


class DeferredExecutor:
    """Returns a real, unfinished Future per submit; the test decides when work runs."""

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    def submit(self, fn, *args):
        future: concurrent.futures.Future = concurrent.futures.Future()
        self.calls.append((fn, args, future))
        return future

    def shutdown(self, wait: bool = False) -> None:
        pass


class CommandOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def daemon(self, executor=None, responder=openrouter_ok) -> OmameterDaemon:
        tmp = Path(self._tmp.name)
        self.out = io.StringIO()
        d = OmameterDaemon(
            store=OmameterStore(config_dir=tmp / "cfg", cache_dir=tmp / "cache"),
            vault=CredentialVault(fallback_path=tmp / "vault.json", use_secret_tool=False),
            proxy=ProxyConfig(enabled=False, url=""),
            notifier=QuotaNotificationEvaluator(post_fn=lambda *a: True),
            clock=FakeClock(NOW),
            executor=executor if executor is not None else InlineExecutor(),
            out=self.out,
        )
        patcher = mock.patch.object(d, "_make_http_client", side_effect=lambda acc=None: FakeHTTP(self.responder))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.responder = responder
        return d

    def events(self) -> List[Dict[str, Any]]:
        events = [json.loads(line) for line in self.out.getvalue().splitlines() if line.strip()]
        self.out.truncate(0)
        self.out.seek(0)
        return events

    def results(self, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [e for e in events if e["type"] == "result"]

    def assert_one_result_last(self, events, cmd_id, ok) -> Dict[str, Any]:
        results = self.results(events)
        self.assertEqual(len(results), 1, events)
        self.assertEqual(events[-1], results[0], "result comes after the work's toasts and state")
        self.assertEqual(results[0]["id"], cmd_id)
        self.assertIs(results[0]["ok"], ok)
        if ok:
            self.assertNotIn("error", results[0])
        else:
            self.assertTrue(results[0]["error"])
        return results[0]

    # ---- success paths ----

    def test_inline_and_pool_commands_answer_once_after_their_work(self) -> None:
        d = self.daemon()
        d.handle_command({"cmd": "get_state", "id": "c1"})
        events = self.events()
        self.assertEqual([e["type"] for e in events], ["state", "result"])
        self.assert_one_result_last(events, "c1", True)

        d.handle_command({"cmd": "add_api_key_account", "id": "c2", "kind": "openrouter", "apiKey": API_KEY_VALUE})
        events = self.events()
        self.assertIn("toast", [e["type"] for e in events])
        self.assertIn("state", [e["type"] for e in events])
        self.assert_one_result_last(events, "c2", True)

        d.handle_command({"cmd": "set_metric_pinned", "id": 7, "metricIds": ["x.y"], "pinned": True})
        self.assert_one_result_last(self.events(), "7", True)

    def test_command_without_id_gets_no_result(self) -> None:
        d = self.daemon()
        for msg in ({"cmd": "get_state"}, {"cmd": "get_state", "id": ""}, {"cmd": "get_state", "id": True},
                    {"cmd": "add_api_key_account", "kind": "openrouter", "apiKey": API_KEY_VALUE},
                    {"cmd": "nope"}, {}):
            d.handle_command(msg)
        self.assertEqual(self.results(self.events()), [])

    def test_unknown_command_with_id_fails(self) -> None:
        d = self.daemon()
        d.handle_command({"cmd": "frobnicate", "id": "u1"})
        result = self.assert_one_result_last(self.events(), "u1", False)
        self.assertIn("frobnicate", result["error"])

    def test_pool_command_answers_only_when_its_work_runs(self) -> None:
        executor = RecordingExecutor()
        d = self.daemon(executor)
        d.handle_command({"cmd": "add_hub", "id": "h1", "address": HUB_BASE, "managementKey": HUB_KEY})
        self.assertEqual(self.results(self.events()), [])
        self.responder = hub_ok
        fn, args = executor.calls.pop(0)
        fn(*args)
        events = self.events()
        self.assert_one_result_last(events, "h1", True)
        self.assertEqual(len(d.store.hubs), 1)
        self.assertNotIn(HUB_KEY, json.dumps(events))

    def test_refresh_all_answers_after_every_scheduled_refresh_finishes(self) -> None:
        executor = DeferredExecutor()
        d = self.daemon(executor)
        for label in ("A", "B"):
            d.store.add_account(Account.create("claude", label))
        d.handle_command({"cmd": "refresh_all", "id": "r1", "force": True})
        run, args, _ = executor.calls.pop(0)
        run(*args)  # refresh_all itself: syncs hubs, schedules two account refreshes
        self.assertEqual(len(executor.calls), 2)
        self.assertEqual(self.results(self.events()), [])
        executor.calls[0][2].set_result(None)
        self.assertEqual(self.results(self.events()), [])
        executor.calls[1][2].set_result(None)
        self.assert_one_result_last(self.events(), "r1", True)

    def test_detect_local_credentials_ok_after_items(self) -> None:
        d = self.daemon()
        with mock.patch("backend.daemon.scan_local_credentials", return_value=[]):
            d.handle_command({"cmd": "detect_local_credentials", "id": "d1"})
        events = self.events()
        self.assertEqual([e["type"] for e in events], ["local_credentials", "result"])
        self.assert_one_result_last(events, "d1", True)

    def test_copy_text_ok_with_wl_copy(self) -> None:
        d = self.daemon()
        with mock.patch("backend.daemon.shutil.which", return_value="/usr/bin/wl-copy"), \
             mock.patch("backend.daemon.subprocess.run") as run:
            d.handle_command({"cmd": "copy_text", "id": "k1", "text": "hello"})
        run.assert_called_once()
        self.assert_one_result_last(self.events(), "k1", True)

    # ---- failure paths ----

    def test_rejected_api_key_fails_with_redacted_error(self) -> None:
        d = self.daemon(responder=rejected)
        d.handle_command({"cmd": "add_api_key_account", "id": "a1", "kind": "openrouter", "apiKey": API_KEY_VALUE})
        events = self.events()
        toasts = [e for e in events if e["type"] == "toast"]
        self.assertEqual(len(toasts), 1)
        self.assertTrue(toasts[0]["error"])
        result = self.assert_one_result_last(events, "a1", False)
        self.assertEqual(result["error"], toasts[0]["message"])
        self.assertEqual(d.store.accounts, [])
        self.assertNotIn(API_KEY_VALUE, self.out.getvalue() + json.dumps(events))

        d.handle_command({"cmd": "add_api_key_account", "id": "a2", "kind": "openrouter", "apiKey": ""})
        self.assertEqual(self.assert_one_result_last(self.events(), "a2", False)["error"], "API key cannot be empty.")

    def test_duplicate_api_key_fails(self) -> None:
        d = self.daemon()
        d.handle_command({"cmd": "add_api_key_account", "kind": "openrouter", "apiKey": API_KEY_VALUE})
        self.events()
        d.handle_command({"cmd": "add_api_key_account", "id": "a3", "kind": "openrouter", "apiKey": API_KEY_VALUE})
        result = self.assert_one_result_last(self.events(), "a3", False)
        self.assertEqual(result["error"], "This API key is already added.")

    def test_add_hub_bad_address_or_key_fails(self) -> None:
        d = self.daemon(responder=rejected)
        d.handle_command({"cmd": "add_hub", "id": "h2", "address": "ftp://nowhere", "managementKey": HUB_KEY})
        self.assertIn("hub's address", self.assert_one_result_last(self.events(), "h2", False)["error"])

        d.handle_command({"cmd": "add_hub", "id": "h3", "address": HUB_BASE, "managementKey": WRONG_HUB_KEY})
        events = self.events()
        self.assertIn("rejected the management key", self.assert_one_result_last(events, "h3", False)["error"])
        self.assertEqual(d.store.hubs, [])
        self.assertNotIn(WRONG_HUB_KEY, json.dumps(events))

        d.handle_command({"cmd": "replace_hub_key", "id": "h4", "hubId": "missing", "managementKey": HUB_KEY})
        self.assertEqual(self.assert_one_result_last(self.events(), "h4", False)["error"], "Hub or key missing.")

    def test_detect_local_credentials_scan_error_fails(self) -> None:
        d = self.daemon()
        boom = RuntimeError("disk on fire token=" + API_KEY_VALUE)
        with mock.patch("backend.daemon.scan_local_credentials", side_effect=boom):
            d.handle_command({"cmd": "detect_local_credentials", "id": "d2"})
        events = self.events()
        self.assertNotIn("local_credentials", [e["type"] for e in events])
        result = self.assert_one_result_last(events, "d2", False)
        self.assertIn("Could not scan local credentials", result["error"])
        self.assertNotIn(API_KEY_VALUE, json.dumps(events))

    def test_copy_text_without_wl_copy_fails(self) -> None:
        d = self.daemon()
        with mock.patch("backend.daemon.shutil.which", return_value=None):
            d.handle_command({"cmd": "copy_text", "id": "k2", "text": "hello"})
        events = self.events()
        self.assertEqual([(e["message"], e["error"]) for e in events if e["type"] == "toast"],
                         [("wl-copy not available.", True)])
        self.assertEqual(self.assert_one_result_last(events, "k2", False)["error"], "wl-copy not available.")

    def test_auth_commands_report_failures(self) -> None:
        d = self.daemon()
        d.handle_command({"cmd": "submit_auth_code", "id": "m1", "code": "abc"})
        self.assert_one_result_last(self.events(), "m1", False)
        d.handle_command({"cmd": "start_auth", "id": "s1", "kind": "nope"})
        self.assert_one_result_last(self.events(), "s1", False)
        d.handle_command({"cmd": "claim_reset", "id": "x1", "accountId": "gone", "resetItem": {}})
        self.assertEqual(self.assert_one_result_last(self.events(), "x1", False)["error"], "Account not found.")

    def test_unexpected_exception_fails_the_command_and_keeps_going(self) -> None:
        d = self.daemon()
        d.handle_command({"cmd": "move_provider", "id": "e1", "kind": "claude", "direction": "sideways"})
        events = self.events()
        self.assertTrue([e for e in events if e["type"] == "toast" and e["error"]])
        self.assertIn("Command failed", self.assert_one_result_last(events, "e1", False)["error"])

        with mock.patch.object(d, "_handle_add_hub", side_effect=RuntimeError("kaput")):
            d.handle_command({"cmd": "add_hub", "id": "e2", "address": HUB_BASE, "managementKey": HUB_KEY})
        self.assertIn("kaput", self.assert_one_result_last(self.events(), "e2", False)["error"])

        d.handle_command({"cmd": "get_state", "id": "e3"})
        self.assert_one_result_last(self.events(), "e3", True)

    def test_proxy_credentials_are_redacted_in_state_and_preserved_on_toggle(self) -> None:
        d = self.daemon()
        secret_url = "http://alice:super-secret-pass@proxy.example:8080"
        d.handle_command({"cmd": "update_proxy", "id": "p1", "enabled": True, "url": secret_url})
        events = self.events()
        self.assert_one_result_last(events, "p1", True)
        state = [e for e in events if e["type"] == "state"][-1]
        self.assertNotIn("super-secret-pass", json.dumps(events))
        self.assertEqual(state["proxy"]["url"], "http://alice:[REDACTED]@proxy.example:8080")
        self.assertEqual(d.proxy.url, secret_url)

        # Toggling enabled off while sending back the redacted URL keeps the saved credentials
        d.handle_command({"cmd": "update_proxy", "id": "p2", "enabled": False, "url": state["proxy"]["url"]})
        events2 = self.events()
        self.assert_one_result_last(events2, "p2", True)
        self.assertEqual(d.proxy.url, secret_url)
        self.assertFalse(d.proxy.enabled)
        self.assertNotIn("super-secret-pass", json.dumps(events2))

    def test_sync_hubs_honors_force_flag(self) -> None:
        d = self.daemon(responder=hub_ok)
        d.handle_command({"cmd": "add_hub", "id": "h1", "address": HUB_BASE, "managementKey": HUB_KEY})
        self.assert_one_result_last(self.events(), "h1", True)
        with mock.patch.object(d.accounts, "sync_hub", wraps=d.accounts.sync_hub) as spy:
            d.handle_command({"cmd": "sync_hubs", "id": "s1", "force": False})
            self.assert_one_result_last(self.events(), "s1", True)
            self.assertEqual(spy.call_count, 1)

            # Second non-forced sync within cooldown is skipped
            d.handle_command({"cmd": "sync_hubs", "id": "s2", "force": False})
            self.assert_one_result_last(self.events(), "s2", True)
            self.assertEqual(spy.call_count, 1)

            # Forced sync bypasses cooldown
            d.handle_command({"cmd": "sync_hubs", "id": "s3", "force": True})
            self.assert_one_result_last(self.events(), "s3", True)
            self.assertEqual(spy.call_count, 2)


if __name__ == "__main__":
    unittest.main()
