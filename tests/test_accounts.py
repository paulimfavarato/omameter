"""AccountLifecycle: arrivals, removal of every per-account state, hub mirrors, default labels."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

from backend.accounts import API_KEY, LOCAL_IMPORT, OAUTH_LOGIN, AccountLifecycle, AdmissionRule
from backend.backoff import RefreshBackoff
from backend.cliproxy import CLIProxyHubGate
from backend.daemon import OmameterDaemon
from backend.http_client import HTTPResponse, ProxyConfig
from backend.models import (
    Account,
    CLIProxyHub,
    MetricLine,
    PriorityLimits,
    ProviderSnapshot,
    ResetItem,
    default_account_label,
    format_iso8601,
    is_default_label,
)
from backend.pace import BurningFastTracker, QuotaNotificationEvaluator
from backend.refresh import AccountRefresher, RefreshOutcome
from backend.store import OmameterStore
from backend.vault import CredentialVault
from tests.fakes import FakeClock, FakeHTTP, InlineExecutor, RecordingExecutor


NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)
FIVE_HOURS_MS = 5 * 3600 * 1000
API_KEY_VALUE = "sk-or-v1-ACCOUNTSTESTKEY0123456789"
HUB_BASE = "http://127.0.0.1:8317"


def _line(key: str, used: float, *, reset_items: Optional[List[ResetItem]] = None) -> MetricLine:
    # 20% of a 5h window elapsed: 10 used is healthy, 90 used is running out.
    return MetricLine(
        key=key,
        title=key.title(),
        line_type="progress",
        kind="percent",
        used=used,
        limit=100,
        resets_at=format_iso8601(NOW + timedelta(hours=4)),
        period_duration_ms=FIVE_HOURS_MS,
        reset_items=list(reset_items or []),
    )


def _reset_item(item_id: str = "grant-1") -> ResetItem:
    return ResetItem(
        id=item_id,
        provider_kind="claude",
        kind="session",
        title="Session Reset",
        expires_at=format_iso8601(NOW + timedelta(hours=10)),
    )


def _snapshot(acc: Account, lines: List[MetricLine]) -> ProviderSnapshot:
    return ProviderSnapshot(
        account_id=acc.id,
        card_id=acc.card_id,
        kind=acc.kind,
        plan=None,
        lines=lines,
        refreshed_at=format_iso8601(NOW) or "",
        stale_at=format_iso8601(NOW + timedelta(minutes=15)) or "",
    )


def _hub_listing(*logins: Dict[str, Any]) -> bytes:
    return json.dumps({"files": list(logins)}).encode("utf-8")


ALICE = {"id": "claude-alice.json", "provider": "claude", "email": "alice@example.com", "auth_index": "idx1", "disabled": False}
BOB = {"id": "claude-bob.json", "provider": "claude", "email": "bob@example.com", "auth_index": "idx2", "disabled": False}


class Harness:
    """Real store/vault/refresher/trackers in a temp dir; no network, no real notifications."""

    def __init__(self, tmp: str) -> None:
        self.clock = FakeClock(NOW)
        self.store = OmameterStore(config_dir=Path(tmp) / "cfg", cache_dir=Path(tmp) / "cache")
        self.store.update_settings({"notifyWarning": True, "notifyCritical": True, "notifyBurningFast": True})
        self.vault = CredentialVault(fallback_path=Path(tmp) / "vault.json", use_secret_tool=False)
        self.posts: List[tuple] = []
        self.notifier = QuotaNotificationEvaluator(post_fn=lambda *a: self.posts.append(a) or True)
        self.burning = BurningFastTracker()
        self.backoff = RefreshBackoff()
        self.refresher = AccountRefresher(
            store=self.store,
            vault=self.vault,
            backoff=self.backoff,
            burning_tracker=self.burning,
            notifier=self.notifier,
            make_http=lambda acc: FakeHTTP(lambda m, u, kw: (_ for _ in ()).throw(AssertionError("no network"))),
            clock=self.clock,
        )
        self.lifecycle = AccountLifecycle(
            store=self.store,
            vault=self.vault,
            refresher=self.refresher,
            burning_tracker=self.burning,
            notifier=self.notifier,
        )

    def add(self, kind: str = "claude", label: str = "Work", **fields: Any) -> Account:
        acc = Account.create(kind, label, **fields)
        self.store.add_account(acc)
        return acc

    def fail(self, acc: Account) -> None:
        self.refresher.record(acc, RefreshOutcome(status="failed", error_kind="auth_expired", message="Sign in again."))

    def assert_unhealthy(self, tc: unittest.TestCase, acc: Account) -> None:
        tc.assertIn(acc.id, self.refresher.errors())
        tc.assertTrue(self.backoff.holds(acc.id, NOW))

    def assert_healthy(self, tc: unittest.TestCase, acc_id: str) -> None:
        tc.assertNotIn(acc_id, self.refresher.errors())
        tc.assertNotIn(acc_id, self.refresher.error_kinds())
        tc.assertFalse(self.backoff.holds(acc_id, NOW))

    def populate_everything(self, acc: Account) -> Dict[str, Any]:
        """Fill each per-account container for `acc` through public paths."""
        healthy = _line("a", 10, reset_items=[_reset_item()])
        hidden = _line("b", 10)
        burning = _line("c", 90)
        self.vault.set_account_credential(acc.id, {"type": "oauth", "accessToken": "tok"})
        self.refresher.record(acc, RefreshOutcome(status="ok", snapshot=_snapshot(acc, [healthy, burning, hidden])))
        self.store.set_metric_hidden([f"{acc.id}.b"], True)
        self.store.set_metric_pinned([f"{acc.id}.a"], True)
        self.store.move_metric(acc.id, "c", -1)
        self.refresher.secondary_reads.for_account(acc.id)["plan"] = {"readAt": None, "value": "max"}
        self.fail(acc)
        return {"healthy": healthy, "burning": burning}


class TestAdmission(unittest.TestCase):
    def test_target_update_replaces_credential_fields_and_clears_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            acc = h.add("copilot", "Work", domain="old.ghe.com", identity="old")
            h.fail(acc)
            h.assert_unhealthy(self, acc)

            res = h.lifecycle.admit(
                "copilot", {"type": "oauth", "accessToken": "new"}, label="ignored",
                rule=OAUTH_LOGIN, identity="new-id", domain="new.ghe.com", target_id=acc.id,
            )
            self.assertEqual(res.outcome, "updated")
            self.assertIs(res.account, acc)
            self.assertEqual(h.vault.get_account_credential(acc.id)["accessToken"], "new")
            reloaded = OmameterStore(config_dir=h.store.config_dir, cache_dir=h.store.cache_dir).get_account(acc.id)
            self.assertEqual((reloaded.domain, reloaded.identity, reloaded.label), ("new.ghe.com", "new-id", "Work"))
            h.assert_healthy(self, acc.id)

            # Without identity/domain the existing values stay.
            h.lifecycle.admit("copilot", {"type": "oauth"}, label="x", rule=OAUTH_LOGIN, target_id=acc.id)
            self.assertEqual((acc.domain, acc.identity), ("new.ghe.com", "new-id"))

    def test_missing_target_is_skipped_for_oauth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            res = h.lifecycle.admit("claude", {"type": "oauth"}, label="Work", rule=OAUTH_LOGIN, identity="a", target_id="gone")
            self.assertEqual(res.outcome, "missing")
            self.assertIsNone(res.account)
            self.assertEqual(h.store.accounts, [])

    def test_oauth_without_target_updates_duplicate_or_creates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            acc = h.add("codex", "Work", identity="alice", organization="org-1")
            h.fail(acc)

            dup = h.lifecycle.admit("codex", {"accessToken": "t2"}, label="Other", rule=OAUTH_LOGIN,
                                    identity="alice", organization="org-1")
            self.assertEqual((dup.outcome, dup.account.id), ("duplicate_updated", acc.id))
            self.assertEqual(h.vault.get_account_credential(acc.id)["accessToken"], "t2")
            self.assertEqual(acc.label, "Work")
            h.assert_healthy(self, acc.id)

            # A different organization is a different account for OAuth logins.
            new = h.lifecycle.admit("codex", {"accessToken": "t3"}, label="", rule=OAUTH_LOGIN,
                                    identity="alice", organization="org-2", auth_mode="oauth")
            self.assertEqual(new.outcome, "created")
            self.assertEqual(len(h.store.accounts), 2)
            self.assertEqual(new.account.label, "ChatGPT")
            self.assertEqual(new.account.auth_mode, "oauth")
            self.assertEqual(h.vault.get_account_credential(new.account.id)["accessToken"], "t3")

    def test_local_import_matches_on_kind_and_identity_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            acc = h.add("claude", "Work", identity="alice")
            h.fail(acc)
            # Arrival organization/domain are not compared (store default of None).
            res = h.lifecycle.admit("claude", {"accessToken": "imported"}, label="Claude", rule=LOCAL_IMPORT,
                                    identity="alice", organization="Acme", domain="x")
            self.assertEqual((res.outcome, res.account.id), ("duplicate_updated", acc.id))
            h.assert_healthy(self, acc.id)

            other = h.lifecycle.admit("claude", {"accessToken": "b"}, label="Claude", rule=LOCAL_IMPORT, identity="bob")
            self.assertEqual(other.outcome, "created")
            # Without identity nothing is ever a duplicate.
            anon = h.lifecycle.admit("claude", {"accessToken": "c"}, label="Claude", rule=LOCAL_IMPORT)
            self.assertEqual(anon.outcome, "created")
            self.assertEqual(len(h.store.accounts), 3)

    def test_api_key_target_updates_credential_identity_and_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            acc = h.add("openrouter", "OpenRouter Account", identity="old-key", auth_mode="api_key")
            h.fail(acc)
            temp = Account.create("openrouter", "OpenRouter Account")
            snap = _snapshot(temp, [_line("credits", 10)])

            res = h.lifecycle.admit("openrouter", {"type": "api_key", "apiKey": "k2"}, label="ignored", rule=API_KEY,
                                    identity="new-key", target_id=acc.id, snapshot=snap)
            self.assertEqual(res.outcome, "updated")
            self.assertEqual(acc.identity, "new-key")
            self.assertEqual(h.vault.get_account_credential(acc.id)["apiKey"], "k2")
            stored = h.store.snapshots[acc.id]
            self.assertEqual((stored.account_id, stored.card_id), (acc.id, acc.card_id))
            self.assertNotIn(temp.id, h.store.snapshots)
            h.assert_healthy(self, acc.id)

    def test_api_key_missing_target_falls_back_to_admission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            res = h.lifecycle.admit("openrouter", {"apiKey": "k"}, label="OpenRouter Account", rule=API_KEY,
                                    identity="kid", target_id="gone")
            self.assertEqual(res.outcome, "created")

    def test_api_key_duplicate_is_rejected_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            acc = h.add("openrouter", "OpenRouter Account", identity="kid", auth_mode="api_key")
            h.vault.set_account_credential(acc.id, {"apiKey": "original"})
            h.fail(acc)
            temp = Account.create("openrouter", "x")
            res = h.lifecycle.admit("openrouter", {"apiKey": "again"}, label="OpenRouter Account", rule=API_KEY,
                                    identity="kid", snapshot=_snapshot(temp, []))
            self.assertEqual((res.outcome, res.account.id), ("rejected", acc.id))
            self.assertEqual(len(h.store.accounts), 1)
            self.assertEqual(h.vault.get_account_credential(acc.id)["apiKey"], "original")
            self.assertEqual(h.store.snapshots, {})
            h.assert_unhealthy(self, acc)

    def test_api_key_new_account_gets_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            temp = Account.create("openrouter", "x")
            res = h.lifecycle.admit("openrouter", {"apiKey": "k"}, label="OpenRouter Account", rule=API_KEY,
                                    identity="kid", auth_mode="api_key", snapshot=_snapshot(temp, [_line("credits", 1)]))
            self.assertEqual(res.outcome, "created")
            acc = res.account
            self.assertEqual(acc.label, "OpenRouter Account")
            self.assertEqual(h.store.snapshots[acc.id].card_id, acc.card_id)
            self.assertEqual(h.vault.get_account_credential(acc.id), {"apiKey": "k"})
            self.assertIn("openrouter", h.store.layout["providerOrder"])

    def test_rule_validation(self) -> None:
        with self.assertRaises(ValueError):
            AdmissionRule(match=("identity",))
        with self.assertRaises(ValueError):
            AdmissionRule(on_duplicate="merge")
        with self.assertRaises(ValueError):
            AdmissionRule(on_missing_target="create")


class TestRemoval(unittest.TestCase):
    def test_remove_clears_every_per_account_container(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            acc = h.add("claude", "Work")
            keep = h.add("claude", "Personal")
            lines = h.populate_everything(acc)
            keep_lines = h.populate_everything(keep)
            mid = f"{acc.id}.a"
            critical_probe = _line("a", 95)
            limits = PriorityLimits(warning=30, critical=10)

            # Preconditions: everything is populated.
            self.assertIn(acc.id, h.store.snapshots)
            self.assertIn(f"{acc.id}.b", h.store.layout["hiddenMetricIDs"])
            self.assertIn(mid, h.store.layout["priorityPinIDs"])
            self.assertIn(acc.id, h.store.layout["metricOrderByProvider"])
            self.assertIsNotNone(h.vault.get_account_credential(acc.id))
            h.assert_unhealthy(self, acc)
            self.assertIn("plan", h.refresher.secondary_reads.for_account(acc.id))
            self.assertTrue(h.burning.was_burning(f"{acc.id}.c", lines["burning"], NOW))
            self.assertTrue(any(p[0] == "Limit Reset Expiring" for p in h.posts))

            removed = h.lifecycle.remove(acc.id)
            self.assertIs(removed, acc)

            # Store: account, snapshot, three layout keys (persisted too).
            reloaded = OmameterStore(config_dir=h.store.config_dir, cache_dir=h.store.cache_dir)
            for store in (h.store, reloaded):
                self.assertIsNone(store.get_account(acc.id))
                self.assertNotIn(acc.id, store.snapshots)
                self.assertFalse([m for m in store.layout["hiddenMetricIDs"] if m.startswith(acc.id)])
                self.assertFalse([m for m in store.layout["priorityPinIDs"] if m.startswith(acc.id)])
                self.assertNotIn(acc.id, store.layout["metricOrderByProvider"])
            # Vault, error, backoff, secondary cache.
            self.assertIsNone(h.vault.get_account_credential(acc.id))
            h.assert_healthy(self, acc.id)
            self.assertEqual(h.refresher.secondary_reads.for_account(acc.id), {})
            # Burning latch.
            self.assertFalse(h.burning.was_burning(f"{acc.id}.c", lines["burning"], NOW))
            # Notifier milestone state: a forgotten metric primes again instead of firing.
            self.assertEqual(
                h.notifier.evaluate(mid, "Claude", critical_probe, NOW, limits, {"critical": True, "warning": True}),
                [],
            )
            # Notifier fired reset expiries: the same grant may notify again.
            self.assertEqual(
                h.notifier.evaluate_reset_expiries(account_id=acc.id, provider_name="Claude",
                                                   line=lines["healthy"], now=NOW, enabled=True),
                ["grant-1"],
            )

            # The other account keeps all of its state.
            self.assertIsNotNone(h.store.get_account(keep.id))
            self.assertIsNotNone(h.vault.get_account_credential(keep.id))
            h.assert_unhealthy(self, keep)
            self.assertIn("plan", h.refresher.secondary_reads.for_account(keep.id))
            self.assertTrue(h.burning.was_burning(f"{keep.id}.c", keep_lines["burning"], NOW))
            self.assertEqual(
                h.notifier.evaluate(f"{keep.id}.a", "Claude", critical_probe, NOW, limits, {"critical": True}),
                ["critical"],
            )
            self.assertEqual(
                h.notifier.evaluate_reset_expiries(account_id=keep.id, provider_name="Claude",
                                                   line=keep_lines["healthy"], now=NOW, enabled=True),
                [],
            )
            self.assertIn("claude", h.store.layout["providerOrder"])

    def test_removing_last_account_of_a_kind_clears_provider_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            acc = h.add("claude", "Work")
            other = h.add("openrouter", "OpenRouter Account")
            h.store.set_provider_unified("claude", True)
            self.assertIn("claude", h.store.layout["unifiedProviderKinds"])

            h.lifecycle.remove(acc.id)
            self.assertNotIn("claude", h.store.layout["providerOrder"])
            self.assertNotIn("claude", h.store.layout["unifiedProviderKinds"])
            self.assertIn("openrouter", h.store.layout["providerOrder"])
            self.assertIsNotNone(h.store.get_account(other.id))

    def test_remove_unknown_account_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            self.assertIsNone(h.lifecycle.remove("nope"))


class TestHubMirrors(unittest.TestCase):
    def _hub(self, h: Harness) -> CLIProxyHub:
        hub = CLIProxyHub(id="hub-1", base_url=HUB_BASE, host="127.0.0.1:8317")
        h.store.add_hub(hub)
        h.vault.set_hub_key(hub.id, HUB_BASE, "secret-key")
        return hub

    @staticmethod
    def _http(*logins: Dict[str, Any]) -> FakeHTTP:
        body = _hub_listing(*logins)
        return FakeHTTP(lambda m, u, kw: HTTPResponse(status=200, headers={}, body=body))

    def test_remove_hub_deletes_mirror_credentials_and_hub_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            hub = self._hub(h)
            res = h.lifecycle.sync_hub(hub, "secret-key", self._http(ALICE, BOB), gate=CLIProxyHubGate())
            self.assertEqual(res["added"], 2)
            mirrors = [a for a in h.store.accounts if a.hub_id == hub.id]
            self.assertEqual([a.label for a in mirrors], ["alice@example.com", "bob@example.com"])
            for acc in mirrors:
                self.assertIsNotNone(h.vault.get_account_credential(acc.id))
            h.populate_everything(mirrors[0])

            removed = h.lifecycle.remove_hub(hub.id)
            self.assertIs(removed, hub)
            self.assertEqual(h.store.hubs, [])
            self.assertEqual(h.store.accounts, [])
            self.assertIsNone(h.vault.get_hub_key(hub.id))
            for acc in mirrors:
                self.assertIsNone(h.vault.get_account_credential(acc.id))
                h.assert_healthy(self, acc.id)
            self.assertEqual(h.refresher.secondary_reads.for_account(mirrors[0].id), {})
            self.assertIsNone(h.lifecycle.remove_hub(hub.id))

    def test_sync_removes_vanished_login_with_all_its_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            hub = self._hub(h)
            h.lifecycle.sync_hub(hub, "secret-key", self._http(ALICE, BOB))
            alice, bob = [a for a in h.store.accounts if a.hub_id == hub.id]
            lines = h.populate_everything(bob)

            res = h.lifecycle.sync_hub(hub, "secret-key", self._http(ALICE))
            self.assertEqual(res["removed"], 1)
            self.assertIsNone(h.store.get_account(bob.id))
            self.assertIsNotNone(h.store.get_account(alice.id))
            self.assertIsNone(h.vault.get_account_credential(bob.id))
            h.assert_healthy(self, bob.id)
            self.assertEqual(h.refresher.secondary_reads.for_account(bob.id), {})
            self.assertFalse(h.burning.was_burning(f"{bob.id}.c", lines["burning"], NOW))
            self.assertEqual(
                h.notifier.evaluate_reset_expiries(account_id=bob.id, provider_name="Claude",
                                                   line=lines["healthy"], now=NOW, enabled=True),
                ["grant-1"],
            )


class TestDefaultLabels(unittest.TestCase):
    def test_default_account_label(self) -> None:
        # API-key accounts and hub mirrors (former cliproxy `_default_account_label`).
        table = [
            ("claude", None, "Claude Account"),
            ("claude", "", "Claude Account"),
            ("claude", "   ", "Claude Account"),
            ("claude", "Claude", "Claude Account"),
            ("claude", " cLaUdE ", "Claude Account"),
            ("claude", "Claude Account", "Claude Account"),
            ("claude", "claude account", "claude account"),
            ("claude", " alice@example.com ", "alice@example.com"),
            ("codex", "ChatGPT", "ChatGPT Account"),
            ("codex", "codex", "codex"),
        ]
        for kind, label, expected in table:
            with self.subTest(kind=kind, label=label):
                self.assertEqual(default_account_label(kind, label), expected)

    def test_is_default_label(self) -> None:
        table = [
            ("", True),
            ("Claude", True),
            ("Claude Account", True),
            ("claude", False),
            (" Claude", False),
            ("claude account", False),
            ("Work", False),
        ]
        for label, expected in table:
            with self.subTest(label=label):
                self.assertEqual(is_default_label("claude", label), expected)

    def test_account_create_empty_label_is_bare_display_name(self) -> None:
        for label, expected in [("", "Claude"), ("   ", "Claude"), (" Work ", "Work"), ("Claude Account", "Claude Account")]:
            with self.subTest(label=label):
                self.assertEqual(Account.create("claude", label).label, expected)

    def test_discovered_label_replaces_only_default_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            cases = [("", True), ("Claude", True), ("Claude Account", True), ("claude", False), ("Work", False)]
            for label, replaced in cases:
                with self.subTest(label=label):
                    acc = Account(id=f"id-{len(h.store.accounts)}", kind="claude", label=label)
                    h.store.add_account(acc)
                    snap = _snapshot(acc, [])
                    snap.account_label_discovered = "alice@example.com"
                    h.refresher.record(acc, RefreshOutcome(status="ok", snapshot=snap))
                    self.assertEqual(acc.label, "alice@example.com" if replaced else label)


class OpenRouterServer:
    def __init__(self) -> None:
        self.requests: List[str] = []

    def __call__(self, method, url, kwargs):
        self.requests.append(url)
        payload = {"data": {"is_management_key": False, "is_free_tier": False, "limit": 100,
                            "limit_remaining": 90, "limit_reset": "monthly", "usage_daily": 1,
                            "usage_weekly": 2, "usage_monthly": 3}}
        return HTTPResponse(status=200, headers={}, body=json.dumps(payload).encode("utf-8"))


class TestDaemonAccountCommands(unittest.TestCase):
    def _daemon(self, tmp: str, executor) -> tuple:
        out = io.StringIO()
        daemon = OmameterDaemon(
            store=OmameterStore(config_dir=Path(tmp) / "cfg", cache_dir=Path(tmp) / "cache"),
            vault=CredentialVault(fallback_path=Path(tmp) / "vault.json", use_secret_tool=False),
            proxy=ProxyConfig(enabled=False, url=""),
            notifier=QuotaNotificationEvaluator(post_fn=lambda *a: True),
            clock=FakeClock(NOW),
            executor=executor,
            out=out,
        )
        return daemon, out

    @staticmethod
    def _events(out: io.StringIO) -> List[Dict[str, Any]]:
        events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        out.truncate(0)
        out.seek(0)
        return events

    @staticmethod
    def _toasts(events: List[Dict[str, Any]]) -> List[tuple]:
        return [(e["message"], e["error"]) for e in events if e["type"] == "toast"]

    def test_auth_complete_adds_updates_and_schedules_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executor = RecordingExecutor()
            daemon, out = self._daemon(tmp, executor)
            result = {"kind": "claude", "label": "Work", "method": "oauth", "identity": "alice",
                      "credential": {"type": "oauth", "accessToken": "secret-access-token"}}

            daemon._on_auth_complete(dict(result))
            events = self._events(out)
            self.assertEqual(self._toasts(events), [("Added Claude · Work.", False)])
            self.assertIn({"type": "auth_update", "session": None}, events)
            self.assertEqual(events[-1]["type"], "state")
            acc = daemon.store.accounts[0]
            self.assertEqual(executor.calls, [(daemon._refresh_single_account, (acc.id, True))])

            daemon._on_auth_complete(dict(result, credential={"type": "oauth", "accessToken": "second-token"}))
            self.assertEqual(self._toasts(self._events(out)), [("Updated existing Claude · Work account.", False)])
            self.assertEqual(len(daemon.store.accounts), 1)
            self.assertEqual(daemon.vault.get_account_credential(acc.id)["accessToken"], "second-token")

            # Re-auth of a specific account: no toast, still refreshed.
            daemon._on_auth_complete(dict(result, accountId=acc.id))
            events = self._events(out)
            self.assertEqual(self._toasts(events), [])
            self.assertEqual(executor.calls[-1], (daemon._refresh_single_account, (acc.id, True)))

            # Re-auth of an account removed meanwhile: nothing created, nothing scheduled.
            calls = len(executor.calls)
            daemon._on_auth_complete(dict(result, accountId="gone"))
            events = self._events(out)
            self.assertEqual(self._toasts(events), [])
            self.assertIn({"type": "auth_update", "session": None}, events)
            self.assertEqual(len(executor.calls), calls)
            self.assertEqual(len(daemon.store.accounts), 1)
            self.assertNotIn("secret-access-token", out.getvalue())

    def test_add_api_key_creates_rejects_duplicate_and_updates_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemon, out = self._daemon(tmp, InlineExecutor())
            server = OpenRouterServer()
            with mock.patch.object(daemon, "_make_http_client", side_effect=lambda acc=None: FakeHTTP(server)):
                daemon.handle_command({"cmd": "add_api_key_account", "kind": "openrouter", "apiKey": API_KEY_VALUE})
                events = self._events(out)
                self.assertEqual(self._toasts(events), [("Added OpenRouter · OpenRouter Account.", False)])
                self.assertEqual(len(daemon.store.accounts), 1)
                acc = daemon.store.accounts[0]
                self.assertEqual(daemon.store.snapshots[acc.id].account_id, acc.id)
                self.assertEqual(daemon.vault.get_account_credential(acc.id)["apiKey"], API_KEY_VALUE)

                daemon.handle_command({"cmd": "add_api_key_account", "kind": "openrouter", "apiKey": API_KEY_VALUE})
                events = self._events(out)
                self.assertEqual(self._toasts(events), [("This API key is already added.", True)])
                self.assertFalse([e for e in events if e["type"] == "state"])
                self.assertEqual(len(daemon.store.accounts), 1)

                rotated = API_KEY_VALUE + "ROTATED"
                daemon.handle_command({"cmd": "add_api_key_account", "kind": "openrouter", "apiKey": rotated,
                                       "accountId": acc.id})
                events = self._events(out)
                self.assertEqual(self._toasts(events), [("Updated key for OpenRouter · OpenRouter Account.", False)])
                self.assertEqual(daemon.vault.get_account_credential(acc.id)["apiKey"], rotated)
                self.assertEqual(len(daemon.store.accounts), 1)
            self.assertEqual(len([u for u in server.requests if u.endswith("/api/v1/key")]), 3)
            self.assertNotIn(API_KEY_VALUE, out.getvalue())

    def test_remove_account_command_clears_lifecycle_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemon, out = self._daemon(tmp, InlineExecutor())
            acc = Account.create("claude", "Work")
            daemon.store.add_account(acc)
            daemon.vault.set_account_credential(acc.id, {"accessToken": "t"})
            daemon.refresher.secondary_reads.for_account(acc.id)["plan"] = {"readAt": None, "value": "max"}

            daemon.handle_command({"cmd": "remove_account", "accountId": acc.id})
            events = self._events(out)
            self.assertEqual(self._toasts(events), [("Removed Claude · Work.", False)])
            self.assertEqual(events[-1]["accounts"], [])
            self.assertIsNone(daemon.vault.get_account_credential(acc.id))
            self.assertEqual(daemon.refresher.secondary_reads.for_account(acc.id), {})

            daemon.handle_command({"cmd": "remove_account", "accountId": acc.id})
            events = self._events(out)
            self.assertEqual(self._toasts(events), [])
            self.assertEqual(events[-1]["type"], "state")

    def test_remove_hub_command_removes_mirrors_and_their_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemon, out = self._daemon(tmp, RecordingExecutor())
            body = _hub_listing(ALICE, BOB)
            http = FakeHTTP(lambda m, u, kw: HTTPResponse(status=200, headers={}, body=body))
            with mock.patch.object(daemon, "_make_http_client", side_effect=lambda acc=None: http):
                daemon._handle_add_hub(HUB_BASE, "secret-management-key")
            hub = daemon.store.hubs[0]
            mirrors = list(daemon.store.accounts)
            self.assertEqual(len(mirrors), 2)
            self._events(out)

            daemon.handle_command({"cmd": "remove_hub", "hubId": hub.id})
            events = self._events(out)
            self.assertEqual(self._toasts(events), [("Removed CLIProxyAPI hub 127.0.0.1:8317.", False)])
            self.assertEqual(events[-1]["hubs"], [])
            self.assertEqual(events[-1]["accounts"], [])
            self.assertIsNone(daemon.vault.get_hub_key(hub.id))
            for acc in mirrors:
                self.assertIsNone(daemon.vault.get_account_credential(acc.id))
            self.assertNotIn("secret-management-key", out.getvalue())


if __name__ == "__main__":
    unittest.main()
