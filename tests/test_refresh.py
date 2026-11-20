"""AccountRefresher and daemon refresh glue, driven by a fake clock, fake HTTP and temp dirs."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

from backend.backoff import OFFLINE_RETRY_DELAYS, RefreshBackoff
from backend.cliproxy import TOKEN_PLACEHOLDER
from backend.daemon import OmameterDaemon
from backend.http_client import HTTPResponse, HTTPTransportError, ProxyConfig
from backend.models import Account, ProviderSnapshot, format_iso8601
from backend.pace import BurningFastTracker, QuotaNotificationEvaluator
from backend.providers.base import ProviderError, make_snapshot
from backend.refresh import AccountRefresher, RefreshOutcome, classify_error, load_credential, refresh_interval_sec
from backend.store import OmameterStore
from backend.vault import CredentialVault
from tests.fakes import FakeClock, FakeHTTP, InlineExecutor, RecordingExecutor


NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)
API_KEY = "sk-or-v1-REFRESHTESTKEY0123456789"


def _key_payload(*, key_used: float = 10.0, free_used: float = 5.0) -> Dict[str, Any]:
    return {
        "data": {
            "is_management_key": False,
            "is_free_tier": False,
            "limit": 100,
            "limit_remaining": 100 - key_used,
            "limit_reset": "monthly",
            "usage_daily": 1,
            "usage_weekly": 2,
            "usage_monthly": 3,
            "free_model_daily_requests": {"limit": 100, "used": free_used},
        }
    }


class OpenRouterServer:
    """Scripted responses for `GET /api/v1/key`; each entry is a payload or an exception."""

    def __init__(self, script: List[Any]) -> None:
        self.script = list(script)
        self.requests: List[str] = []

    def __call__(self, method, url, kwargs):
        self.requests.append(url)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, BaseException):
            raise item
        return HTTPResponse(status=200, headers={}, body=json.dumps(item).encode("utf-8"))


class Harness:
    def __init__(self, tmp: str, *, clock: Optional[FakeClock] = None, server: Optional[OpenRouterServer] = None) -> None:
        self.dirs = (Path(tmp) / "cfg", Path(tmp) / "cache")
        self.store = OmameterStore(config_dir=self.dirs[0], cache_dir=self.dirs[1])
        self.vault = CredentialVault(fallback_path=Path(tmp) / "vault.json", use_secret_tool=False)
        self.backoff = RefreshBackoff()
        self.posted: List[tuple] = []
        self.notifier = QuotaNotificationEvaluator(post_fn=lambda *a: self.posted.append(a) or True)
        self.clock = clock or FakeClock(NOW)
        self.server = server or OpenRouterServer([_key_payload()])
        self.refresher = AccountRefresher(
            store=self.store,
            vault=self.vault,
            backoff=self.backoff,
            burning_tracker=BurningFastTracker(),
            notifier=self.notifier,
            make_http=lambda acc: FakeHTTP(self.server),
            clock=self.clock,
        )

    def add_openrouter(self, *, with_key: bool = True, label: str = "OpenRouter Account") -> Account:
        acc = Account.create("openrouter", label, auth_mode="api_key")
        self.store.add_account(acc)
        if with_key:
            self.vault.set_account_credential(acc.id, {"type": "api_key", "apiKey": API_KEY})
        return acc

    def refresh(self, acc: Account, *, force: bool = True) -> RefreshOutcome:
        planned = self.refresher.plan(acc.id, force=force, in_flight=set())
        assert planned is not None
        outcome = self.refresher.fetch(planned, force=force)
        self.refresher.record(planned, outcome)
        return outcome

    def put_snapshot(self, acc: Account, age_sec: float) -> None:
        self.store.snapshots[acc.id] = ProviderSnapshot(
            account_id=acc.id,
            card_id=acc.card_id,
            kind=acc.kind,
            refreshed_at=format_iso8601(self.clock.now() - timedelta(seconds=age_sec)) or "",
        )


class TestRefreshRules(unittest.TestCase):
    def test_refresh_interval_is_clamped(self) -> None:
        self.assertEqual(refresh_interval_sec({}), 300)
        self.assertEqual(refresh_interval_sec({"refreshIntervalSec": 5}), 60)
        self.assertEqual(refresh_interval_sec({"refreshIntervalSec": 99999}), 3600)

    def test_classify_error(self) -> None:
        self.assertEqual(classify_error(ProviderError("slow down", kind="rate_limited", retry_after=42.0)), ("rate_limited", "slow down", 42.0))
        self.assertEqual(classify_error(HTTPTransportError("dns", offline=True))[0], "offline")
        self.assertEqual(classify_error(HTTPTransportError("tls handshake"))[0], "unreachable")
        kind, msg, _ = classify_error(RuntimeError(f"boom Authorization: Bearer {API_KEY}"))
        self.assertEqual(kind, "refused")
        self.assertNotIn(API_KEY, msg)

    def test_load_credential_hub_and_plain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = CredentialVault(fallback_path=Path(tmp) / "vault.json", use_secret_tool=False)
            plain = Account.create("openrouter", "x")
            self.assertEqual(load_credential(plain, vault), {})
            vault.set_account_credential(plain.id, {"type": "api_key", "apiKey": "k"})
            self.assertEqual(load_credential(plain, vault)["apiKey"], "k")
            hub = Account.create("codex", "h", hub_id="hub-1", auth_mode="hub")
            self.assertEqual(load_credential(hub, vault)["accessToken"], TOKEN_PLACEHOLDER)


class TestRefreshGate(unittest.TestCase):
    def test_plan_branches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            acc = h.add_openrouter()
            r = h.refresher

            # Unknown account
            self.assertIsNone(r.plan("missing", force=True, in_flight=set()))
            # No snapshot yet -> refresh
            self.assertIs(r.plan(acc.id, force=False, in_flight=set()), acc)
            # Already in flight
            self.assertIsNone(r.plan(acc.id, force=True, in_flight={acc.id}))

            # Too recent (non-forced), forced ignores recency
            h.put_snapshot(acc, age_sec=60)
            self.assertIsNone(r.plan(acc.id, force=False, in_flight=set()))
            self.assertIs(r.plan(acc.id, force=True, in_flight=set()), acc)

            # Our own backoff holds non-forced refreshes, forced ignores it
            h.backoff.record_failure(acc.id, kind="refused", reason="x", now=NOW)
            h.put_snapshot(acc, age_sec=10_000)
            self.assertIsNone(r.plan(acc.id, force=False, in_flight=set()))
            self.assertIs(r.plan(acc.id, force=True, in_flight=set()), acc)

            # Provider-requested backoff (rate limit) holds even forced refreshes
            h.backoff.record_failure(acc.id, kind="rate_limited", reason="x", now=NOW, retry_after=600)
            self.assertIsNone(r.plan(acc.id, force=True, in_flight=set()))
            h.clock.advance(601)
            self.assertIs(r.plan(acc.id, force=True, in_flight=set()), acc)

    def test_late_fetch_is_not_skipped_by_next_cycle(self) -> None:
        # Regression: a snapshot stamped some seconds after the cycle started is
        # younger than the interval at the next cycle; it must still refresh.
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            h.store.settings["refreshIntervalSec"] = 300
            acc = h.add_openrouter()
            h.put_snapshot(acc, age_sec=250)
            self.assertIs(h.refresher.plan(acc.id, force=False, in_flight=set()), acc)
            h.put_snapshot(acc, age_sec=200)
            self.assertIsNone(h.refresher.plan(acc.id, force=False, in_flight=set()))

    def test_short_interval_caps_grace_period(self) -> None:
        # With a 60s interval, grace is capped at 30s (half interval) so snapshots
        # younger than 30s are still gated instead of disabling recency checks.
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            h.store.settings["refreshIntervalSec"] = 60
            acc = h.add_openrouter()
            h.put_snapshot(acc, age_sec=10)
            self.assertIsNone(h.refresher.plan(acc.id, force=False, in_flight=set()))
            h.put_snapshot(acc, age_sec=35)
            self.assertIs(h.refresher.plan(acc.id, force=False, in_flight=set()), acc)


class TestRefreshFetchAndRecord(unittest.TestCase):
    def test_success_persists_snapshot_and_clears_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            acc = h.add_openrouter()
            h.refresher.record(acc, RefreshOutcome(status="failed", error_kind="refused", message="old error"))
            self.assertEqual(h.refresher.error_for(acc.id), "old error")

            outcome = h.refresh(acc)
            self.assertEqual(outcome.status, "ok")
            self.assertEqual(h.server.requests, ["https://openrouter.ai/api/v1/key"])
            self.assertEqual(h.refresher.errors(), {})
            self.assertEqual(h.refresher.error_kinds(), {})
            self.assertFalse(h.backoff.holds(acc.id, h.clock.now()))

            reloaded = OmameterStore(config_dir=h.dirs[0], cache_dir=h.dirs[1])
            snap = reloaded.snapshots[acc.id]
            self.assertEqual(snap.refreshed_at, format_iso8601(NOW))
            self.assertIn("keyLimit", {ln.key for ln in snap.lines})

    def test_account_removed_during_fetch_records_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            acc = h.add_openrouter()
            planned = h.refresher.plan(acc.id, force=True, in_flight=set())
            outcome = h.refresher.fetch(planned, force=True)
            self.assertEqual(outcome.status, "ok")
            h.store.remove_account(acc.id)
            h.refresher.record(planned, outcome)
            self.assertNotIn(acc.id, h.store.snapshots)
            reloaded = OmameterStore(config_dir=h.dirs[0], cache_dir=h.dirs[1])
            self.assertNotIn(acc.id, reloaded.snapshots)

            h.refresher.record(planned, RefreshOutcome(status="failed", error_kind="refused", message="gone"))
            self.assertEqual(h.refresher.errors(), {})
            self.assertFalse(h.backoff.holds(acc.id, h.clock.now()))

    def test_discovered_label_only_replaces_default_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            default_acc = h.add_openrouter(label="OpenRouter Account")
            custom_acc = h.add_openrouter(label="Work")

            def fake_fetch(ctx):
                return make_snapshot(ctx, plan=None, lines=[], account_label_discovered="me@example.com")

            with mock.patch("backend.refresh.fetch_provider", side_effect=fake_fetch):
                h.refresh(default_acc)
                h.refresh(custom_acc)
            reloaded = OmameterStore(config_dir=h.dirs[0], cache_dir=h.dirs[1])
            self.assertEqual(reloaded.get_account(default_acc.id).label, "me@example.com")
            self.assertEqual(reloaded.get_account(custom_acc.id).label, "Work")

    def test_notifications_skip_hidden_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = OpenRouterServer([_key_payload(key_used=10, free_used=5), _key_payload(key_used=95, free_used=95)])
            h = Harness(tmp, server=server)
            h.store.settings["notifyCritical"] = True
            acc = h.add_openrouter()
            h.store.layout["hiddenMetricIDs"] = [f"{acc.id}.keyLimit"]

            h.refresh(acc)  # primes the evaluator at normal severity
            self.assertEqual(h.posted, [])
            h.clock.advance(60)
            h.refresh(acc)  # both metrics cross to critical; only the visible one notifies
            subtitles = [p[1] for p in h.posted]
            self.assertEqual(len(subtitles), 1)
            self.assertIn("Free Requests", subtitles[0])

    def test_offline_retries_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            offline = HTTPTransportError("network down", offline=True)
            server = OpenRouterServer([offline, offline, _key_payload()])
            h = Harness(tmp, server=server)
            acc = h.add_openrouter()
            outcome = h.refresh(acc)
            self.assertEqual(outcome.status, "ok")
            self.assertEqual(h.clock.waits, [2, 4])
            self.assertEqual(len(server.requests), 3)
            # Each attempt stamps its own time
            self.assertEqual(outcome.snapshot.refreshed_at, format_iso8601(NOW + timedelta(seconds=6)))

    def test_offline_exhausts_retries_and_fails_soft(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = OpenRouterServer([HTTPTransportError("network down", offline=True)])
            h = Harness(tmp, server=server)
            acc = h.add_openrouter()
            outcome = h.refresh(acc)
            self.assertEqual(outcome.status, "failed")
            self.assertEqual(outcome.error_kind, "offline")
            self.assertEqual(h.clock.waits, list(OFFLINE_RETRY_DELAYS))
            self.assertEqual(len(server.requests), len(OFFLINE_RETRY_DELAYS) + 1)
            self.assertEqual(h.refresher.error_for(acc.id), "network down")
            self.assertEqual(h.refresher.error_kinds(), {acc.id: "offline"})

    def test_stop_during_wait_records_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = OpenRouterServer([HTTPTransportError("network down", offline=True)])
            h = Harness(tmp, clock=FakeClock(NOW, stop_after_waits=1), server=server)
            acc = h.add_openrouter()
            outcome = h.refresh(acc)
            self.assertEqual(outcome.status, "stopped")
            self.assertEqual(h.clock.waits, [2])
            self.assertEqual(h.refresher.errors(), {})
            self.assertNotIn(acc.id, h.store.snapshots)
            self.assertFalse(h.backoff.holds(acc.id, h.clock.now()))

    def test_non_offline_transport_error_is_unreachable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = OpenRouterServer([HTTPTransportError("tls handshake failed")])
            h = Harness(tmp, server=server)
            acc = h.add_openrouter()
            outcome = h.refresh(acc)
            self.assertEqual((outcome.status, outcome.error_kind), ("failed", "unreachable"))
            self.assertEqual(h.clock.waits, [])
            # "unreachable" keeps the failure count at zero (soft backoff), unlike "refused"
            self.assertEqual(h.backoff.record_failure(acc.id, kind="refused", reason="x", now=NOW).failures, 1)

            # Raw transport errors (not wrapped by a provider) classify the same way
            with mock.patch("backend.refresh.fetch_provider", side_effect=HTTPTransportError("reset by peer")):
                outcome = h.refresh(acc)
            self.assertEqual(outcome.error_kind, "unreachable")

    def test_generic_exception_is_refused_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = OpenRouterServer([RuntimeError(f"exploded with Authorization: Bearer {API_KEY}")])
            h = Harness(tmp, server=server)
            acc = h.add_openrouter()
            outcome = h.refresh(acc)
            self.assertEqual((outcome.status, outcome.error_kind), ("failed", "refused"))
            self.assertNotIn(API_KEY, outcome.message)
            self.assertNotIn(API_KEY, h.refresher.error_for(acc.id))
            self.assertTrue(h.backoff.holds(acc.id, h.clock.now()))

    def test_missing_credential_is_auth_expired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            acc = h.add_openrouter(with_key=False)
            outcome = h.refresh(acc)
            self.assertEqual(outcome.error_kind, "auth_expired")
            self.assertEqual(outcome.message, "Missing credentials. Sign in again.")
            self.assertEqual(h.server.requests, [])

    def test_hub_account_uses_placeholder_and_never_writes_vault(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            hub_acc = Account.create("codex", "Hub login", hub_id="hub-1", auth_mode="hub")
            h.store.add_account(hub_acc)
            seen: Dict[str, Any] = {}

            def fake_fetch(ctx):
                seen["token"] = ctx.credential.get("accessToken")
                ctx.persist_credential({"type": "oauth", "accessToken": "rotated"})
                return make_snapshot(ctx, plan=None, lines=[])

            with mock.patch("backend.refresh.fetch_provider", side_effect=fake_fetch):
                self.assertEqual(h.refresh(hub_acc).status, "ok")
            self.assertEqual(seen["token"], TOKEN_PLACEHOLDER)
            self.assertIsNone(h.vault.get_account_credential(hub_acc.id))

    def test_token_refresh_is_persisted_for_regular_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            acc = h.add_openrouter()

            def fake_fetch(ctx):
                ctx.persist_credential({"type": "api_key", "apiKey": "rotated-key"})
                return make_snapshot(ctx, plan=None, lines=[])

            with mock.patch("backend.refresh.fetch_provider", side_effect=fake_fetch):
                self.assertEqual(h.refresh(acc).status, "ok")
            self.assertEqual(h.vault.get_account_credential(acc.id)["apiKey"], "rotated-key")


class TestDaemonRefreshGlue(unittest.TestCase):
    def _daemon(self, tmp: str, executor) -> tuple:
        clock = FakeClock(NOW)
        out = io.StringIO()
        store = OmameterStore(config_dir=Path(tmp) / "cfg", cache_dir=Path(tmp) / "cache")
        vault = CredentialVault(fallback_path=Path(tmp) / "vault.json", use_secret_tool=False)
        daemon = OmameterDaemon(
            store=store,
            vault=vault,
            proxy=ProxyConfig(enabled=False, url=""),
            notifier=QuotaNotificationEvaluator(post_fn=lambda *a: True),
            clock=clock,
            executor=executor,
            out=out,
        )
        return daemon, clock, out

    @staticmethod
    def _events(out: io.StringIO) -> List[Dict[str, Any]]:
        return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]

    def test_refresh_single_account_emits_refreshing_on_then_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemon, _, out = self._daemon(tmp, InlineExecutor())
            acc = Account.create("openrouter", "OpenRouter Account", auth_mode="api_key")
            daemon.store.add_account(acc)
            daemon.vault.set_account_credential(acc.id, {"type": "api_key", "apiKey": API_KEY})
            server = OpenRouterServer([_key_payload()])

            with mock.patch.object(daemon, "_make_http_client", side_effect=lambda acc=None: FakeHTTP(server)):
                daemon._refresh_single_account(acc.id, True)

            states = [e for e in self._events(out) if e["type"] == "state"]
            self.assertEqual([s["refreshing"] for s in states], [True, False])
            self.assertTrue(states[0]["accounts"][0]["refreshing"])
            final = states[-1]
            self.assertEqual(final["accounts"][0]["error"], "")
            self.assertEqual(final["accounts"][0]["errorKind"], "")
            self.assertIn(acc.id, daemon.store.snapshots)
            self.assertNotIn(API_KEY, out.getvalue())

            # A second non-forced refresh right away is gated: no new events
            before = len(self._events(out))
            daemon._refresh_single_account(acc.id, False)
            self.assertEqual(len(self._events(out)), before)

    def test_refresh_failure_surfaces_error_in_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemon, _, out = self._daemon(tmp, InlineExecutor())
            acc = Account.create("openrouter", "OpenRouter Account", auth_mode="api_key")
            daemon.store.add_account(acc)  # no credential stored
            daemon._refresh_single_account(acc.id, True)
            final = [e for e in self._events(out) if e["type"] == "state"][-1]
            self.assertFalse(final["refreshing"])
            self.assertEqual(final["accounts"][0]["error"], "Missing credentials. Sign in again.")
            self.assertEqual(final["accounts"][0]["errorKind"], "auth_expired")

            # remove_account clears error and backoff through mark_healthy
            daemon.handle_command({"cmd": "remove_account", "accountId": acc.id})
            self.assertEqual(daemon.refresher.errors(), {})
            self.assertEqual(daemon.refresher.error_kinds(), {})
            self.assertFalse(daemon.backoff.holds(acc.id, NOW))

    def test_refresh_all_submits_each_account_and_reschedules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executor = RecordingExecutor()
            daemon, clock, out = self._daemon(tmp, executor)
            a = Account.create("openrouter", "A", auth_mode="api_key")
            b = Account.create("openrouter", "B", auth_mode="api_key")
            daemon.store.add_account(a)
            daemon.store.add_account(b)
            clock.advance(1000)
            daemon.refresh_all(False)
            self.assertEqual(
                [(fn, args) for fn, args in executor.calls],
                [(daemon._refresh_single_account, (a.id, False)), (daemon._refresh_single_account, (b.id, False))],
            )
            state = self._events(out)[-1]
            self.assertEqual(state["nextRefreshAt"], format_iso8601(NOW + timedelta(seconds=1300)))

    def test_background_tick(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executor = RecordingExecutor()
            daemon, clock, out = self._daemon(tmp, executor)
            acc = Account.create("claude", "Personal")
            daemon.store.add_account(acc)
            from backend.models import MetricLine

            daemon.store.snapshots[acc.id] = ProviderSnapshot(
                account_id=acc.id,
                card_id=acc.card_id,
                kind="claude",
                lines=[MetricLine(key="session", title="Session", line_type="progress", kind="percent",
                                  used=40.0, limit=100.0, resets_at=format_iso8601(NOW + timedelta(seconds=120)))],
                refreshed_at=format_iso8601(NOW) or "",
            )

            # Idle, nothing crossed: no submit, no emission
            clock.advance(30)
            daemon._background_tick()
            self.assertEqual(executor.calls, [])
            self.assertEqual(out.getvalue(), "")

            # A window crossed resetsAt: re-emit once, then stay quiet
            clock.advance(100)
            daemon._background_tick()
            self.assertEqual(len(self._events(out)), 1)
            daemon._background_tick()
            self.assertEqual(len(self._events(out)), 1)
            self.assertEqual(executor.calls, [])

            # Refresh due: submit refresh_all
            clock.advance(300)
            daemon._background_tick()
            self.assertEqual(executor.calls, [(daemon.refresh_all, (False,))])


if __name__ == "__main__":
    unittest.main()
