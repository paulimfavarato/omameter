"""Secondary reads: the per-account cache, its owner, and the fetch/claim sharing it.

Driven by fake HTTP, a fake clock and temp dirs; no network, no real sleeps.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest import mock

from backend.backoff import FORCED_RESET_GRANTS_MIN_AGE_SEC, RefreshBackoff, SECONDARY_READ_INTERVAL_SEC
from backend.daemon import OmameterDaemon
from backend.http_client import HTTPResponse, ProxyConfig
from backend.models import Account, format_iso8601
from backend.pace import BurningFastTracker, QuotaNotificationEvaluator
from backend.providers import opencode
from backend.providers.base import FetchContext
from backend.providers.claude import CLAUDE_PROFILE_URL, CLAUDE_RESET_GRANTS_URL, CLAUDE_USAGE_URL
from backend.providers.codex import CODEX_CONSUME_RESET_CREDIT_URL, CODEX_RESET_CREDITS_URL
from backend.refresh import AccountRefresher
from backend.secondary import SecondaryCache, SecondaryReads
from backend.store import OmameterStore
from backend.vault import CredentialVault
from tests.fakes import FakeClock, FakeHTTP, InlineExecutor


NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)
ORG_UUID = "6f1c3f0e-6b7a-4b0e-9a55-1d2f3c4b5a69"


def _json_response(status: int, payload: Any) -> HTTPResponse:
    return HTTPResponse(status=status, headers={}, body=json.dumps(payload).encode("utf-8"))


class Router:
    """Fake server: `(method, url) -> handler(kwargs)`; records every request in order."""

    def __init__(self, routes: Dict[Tuple[str, str], Callable[[Dict[str, Any]], HTTPResponse]]) -> None:
        self.routes = routes
        self.log: List[Tuple[str, str, Dict[str, Any]]] = []

    def __call__(self, method, url, kwargs):
        self.log.append((method, url, kwargs))
        handler = self.routes.get((method, url))
        if handler is None:
            return _json_response(404, {})
        return handler(kwargs)

    def calls(self, method: str, url: str) -> List[Dict[str, Any]]:
        return [kw for m, u, kw in self.log if m == method and u == url]


# ---------------------------------------------------------------------------
# SecondaryCache / SecondaryReads
# ---------------------------------------------------------------------------


class TestSecondaryCache(unittest.TestCase):
    def test_is_due(self) -> None:
        cache = SecondaryCache()
        self.assertTrue(cache.is_due("k", NOW))  # missing
        cache["k"] = {"readAt": None, "value": 1}
        self.assertTrue(cache.is_due("k", NOW))  # null readAt
        cache["k"] = {"value": 1}
        self.assertTrue(cache.is_due("k", NOW))  # no readAt
        cache["k"] = {"readAt": format_iso8601(NOW), "value": 1}
        self.assertFalse(cache.is_due("k", NOW + timedelta(seconds=SECONDARY_READ_INTERVAL_SEC - 1)))
        self.assertTrue(cache.is_due("k", NOW + timedelta(seconds=SECONDARY_READ_INTERVAL_SEC)))
        self.assertTrue(cache.is_due("k", NOW + timedelta(seconds=60), interval_sec=60))

    def test_read_hit_miss_and_force(self) -> None:
        cache = SecondaryCache()
        calls: List[int] = []

        def fetch() -> str:
            calls.append(1)
            return f"v{len(calls)}"

        self.assertEqual(cache.read("k", NOW, fetch), "v1")
        self.assertEqual(cache["k"], {"readAt": format_iso8601(NOW), "value": "v1"})
        # Inside the TTL: cached, no fetch
        self.assertEqual(cache.read("k", NOW + timedelta(seconds=10), fetch), "v1")
        self.assertEqual(len(calls), 1)
        # Forced: ignores the TTL
        self.assertEqual(cache.read("k", NOW + timedelta(seconds=20), fetch, force=True), "v2")
        # Due again after the TTL
        later = NOW + timedelta(seconds=20 + SECONDARY_READ_INTERVAL_SEC)
        self.assertEqual(cache.read("k", later, fetch), "v3")
        self.assertEqual(cache["k"]["readAt"], format_iso8601(later))
        self.assertEqual(cache.value("k"), "v3")
        self.assertIsNone(cache.value("missing"))

    def test_read_failure_keep_and_clear(self) -> None:
        def boom() -> Any:
            raise RuntimeError("offline")

        for failing in (lambda: None, boom):
            keep = SecondaryCache()
            keep.read("k", NOW, lambda: "old")
            later = NOW + timedelta(seconds=SECONDARY_READ_INTERVAL_SEC)
            self.assertEqual(keep.read("k", later, failing), "old")
            self.assertEqual(keep["k"], {"readAt": format_iso8601(later), "value": "old"})

            clear = SecondaryCache()
            clear.read("k", NOW, lambda: "old", on_failure="clear")
            self.assertIsNone(clear.read("k", later, failing, on_failure="clear"))
            self.assertEqual(clear["k"], {"readAt": format_iso8601(later), "value": None})

        with self.assertRaises(ValueError):
            SecondaryCache().read("k", NOW, lambda: 1, on_failure="nope")

    def test_invalidate_and_drop(self) -> None:
        cache = SecondaryCache()
        cache.read("k", NOW, lambda: "v")
        cache.invalidate("k")
        self.assertTrue(cache.is_due("k", NOW, interval_sec=10_000))
        self.assertEqual(cache.value("k"), "v")
        cache.invalidate("missing")  # no-op
        cache.drop("k")
        self.assertNotIn("k", cache)
        cache.drop("k")  # no-op

    def test_raw_state_still_works_as_dict(self) -> None:
        cache = SecondaryCache()
        cache.setdefault("ledger", {})["a"] = 1
        self.assertEqual(cache["ledger"], {"a": 1})
        self.assertIsInstance(FetchContext(account=Account.create("grok", "G"), credential={}, http=None).secondary_cache, SecondaryCache)  # type: ignore[arg-type]

    def test_secondary_reads_owner(self) -> None:
        reads = SecondaryReads()
        a = reads.for_account("a")
        self.assertIs(reads.for_account("a"), a)
        self.assertIsNot(reads.for_account("b"), a)
        a["x"] = 1
        reads.forget("a")
        fresh = reads.for_account("a")
        self.assertIsNot(fresh, a)
        self.assertEqual(fresh, {})
        reads.forget("never-seen")  # no-op


# ---------------------------------------------------------------------------
# Provider reads through fetch(ctx)
# ---------------------------------------------------------------------------


class TestOpenCodePlanRead(unittest.TestCase):
    def test_plan_read_respects_ttl_force_and_clears_on_failure(self) -> None:
        usage = {"usage": {"rolling": {"percent": 20, "resetsAt": "2026-07-13T15:00:00Z"},
                           "weekly": {"percent": 40, "resetsAt": "2026-07-18T00:00:00Z"},
                           "monthly": {"percent": 15, "resetsAt": "2026-08-01T00:00:00Z"}}}
        plan_responses: List[HTTPResponse] = [
            _json_response(200, {"product": "go"}),
            _json_response(200, {"product": "go-plus"}),
            _json_response(500, {}),
        ]
        router = Router({
            ("GET", opencode.USAGE_URL): lambda kw: _json_response(200, usage),
            ("GET", opencode.GO_STATUS_URL): lambda kw: plan_responses.pop(0),
        })
        acc = Account.create("opencode", "OpenCode")
        cache = SecondaryCache()

        def run(at: datetime, *, force: bool = False) -> Optional[str]:
            ctx = FetchContext(
                account=acc,
                credential={"accessToken": "tok-opencode"},
                http=FakeHTTP(router),
                now=at,
                force=force,
                secondary_cache=cache,
            )
            return opencode.fetch(ctx).plan

        self.assertEqual(run(NOW), "Go")
        self.assertEqual(run(NOW + timedelta(seconds=60)), "Go")  # inside TTL
        self.assertEqual(len(router.calls("GET", opencode.GO_STATUS_URL)), 1)
        self.assertEqual(run(NOW + timedelta(seconds=120), force=True), "Go Plus")
        self.assertEqual(len(router.calls("GET", opencode.GO_STATUS_URL)), 2)
        # A failed hourly read clears the plan instead of keeping the stale one
        self.assertIsNone(run(NOW + timedelta(seconds=120 + SECONDARY_READ_INTERVAL_SEC)))
        self.assertEqual(len(router.calls("GET", opencode.GO_STATUS_URL)), 3)


# ---------------------------------------------------------------------------
# Claim and fetch share one cache (daemon + refresher)
# ---------------------------------------------------------------------------


def _grants_payload(resets_left: int) -> Dict[str, Any]:
    return {
        "cedar_ember": {
            "eligible": True,
            "grants": [{
                "id": "g1",
                "resets_left": resets_left,
                "ends_at": "2026-08-01T00:00:00Z",
                "clears": ["five_hour"],
                "type": "session",
                "usable_now": True,
            }],
            "next_grant_id": "g1",
        }
    }


CLAUDE_RESET_ITEM = {"providerKind": "claude", "id": "g1", "kind": "session", "claimPayload": {"grant_id": "g1"}}
CLAIM_URL = f"https://api.anthropic.com/api/organizations/{ORG_UUID}/reset_rate_limits"


class ClaudeServer(Router):
    def __init__(self, claim_responses: List[HTTPResponse]) -> None:
        self.claim_responses = claim_responses
        super().__init__({
            ("GET", CLAUDE_USAGE_URL): lambda kw: _json_response(200, {
                "five_hour": {"utilization": 100.0, "resets_at": "2026-07-13T16:00:00Z"},
                "seven_day": {"utilization": 50.0, "resets_at": "2026-07-18T00:00:00Z"},
            }),
            ("GET", CLAUDE_RESET_GRANTS_URL): lambda kw: _json_response(200, _grants_payload(1)),
            ("GET", CLAUDE_PROFILE_URL): lambda kw: _json_response(200, {
                "account": {"email": "someone@example.com"},
                "organization": {"uuid": ORG_UUID, "organization_type": "claude_max"},
            }),
            ("POST", CLAIM_URL): lambda kw: self.claim_responses.pop(0),
        })

    def claim_request_ids(self) -> List[str]:
        return [kw["json_body"]["request_id"] for kw in self.calls("POST", CLAIM_URL)]


class TestClaimSharesFetchCache(unittest.TestCase):
    def _daemon(self, tmp: str) -> Tuple[OmameterDaemon, FakeClock, io.StringIO]:
        clock = FakeClock(NOW)
        out = io.StringIO()
        daemon = OmameterDaemon(
            store=OmameterStore(config_dir=Path(tmp) / "cfg", cache_dir=Path(tmp) / "cache"),
            vault=CredentialVault(fallback_path=Path(tmp) / "vault.json", use_secret_tool=False),
            proxy=ProxyConfig(enabled=False, url=""),
            notifier=QuotaNotificationEvaluator(post_fn=lambda *a: True),
            clock=clock,
            executor=InlineExecutor(),
            out=out,
        )
        return daemon, clock, out

    @staticmethod
    def _toasts(out: io.StringIO) -> List[Dict[str, Any]]:
        events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        return [e for e in events if e["type"] == "toast"]

    def _claude_account(self, daemon: OmameterDaemon) -> Account:
        acc = Account.create("claude", "Personal")
        daemon.store.add_account(acc)
        daemon.vault.set_account_credential(acc.id, {
            "accessToken": "tok-claude-access",
            "refreshToken": "tok-claude-refresh",
            "expiresAt": format_iso8601(NOW + timedelta(days=1)),
        })
        return acc

    def test_claim_invalidates_grants_for_the_next_forced_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemon, clock, out = self._daemon(tmp)
            acc = self._claude_account(daemon)
            server = ClaudeServer([_json_response(200, {"result": "reset", "resets_left": 0})])
            with mock.patch.object(daemon, "_make_http_client", side_effect=lambda acc=None: FakeHTTP(server)):
                daemon._refresh_single_account(acc.id, True)
                self.assertEqual(len(server.calls("GET", CLAUDE_RESET_GRANTS_URL)), 1)

                # Within the TTL a normal refresh re-reads neither grants nor the profile
                clock.advance(SECONDARY_READ_INTERVAL_SEC - 120)
                daemon._refresh_single_account(acc.id, False)
                self.assertEqual(len(server.calls("GET", CLAUDE_RESET_GRANTS_URL)), 1)
                self.assertEqual(len(server.calls("GET", CLAUDE_PROFILE_URL)), 1)

                # Claim (well inside the forced minimum age) then the scheduled forced refresh
                clock.advance(60)
                before = len(server.log)
                daemon.handle_command({"cmd": "claim_reset", "accountId": acc.id, "resetItem": CLAUDE_RESET_ITEM})

            after_claim = server.log[before:]
            methods_urls = [(m, u) for m, u, _ in after_claim]
            # The claim reused cached grants and org UUID: its first request is the POST
            self.assertEqual(methods_urls[0], ("POST", CLAIM_URL))
            # ...and the refresh it scheduled re-read grants despite the 900 s minimum age
            self.assertIn(("GET", CLAUDE_RESET_GRANTS_URL), methods_urls[1:])
            self.assertEqual(self._toasts(out)[-1], {"type": "toast", "message": "Reset claimed. Enjoy!", "error": False})
            self.assertNotIn("tok-claude", out.getvalue())

    def test_forced_refresh_without_claim_keeps_grants_min_age(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemon, clock, _ = self._daemon(tmp)
            acc = self._claude_account(daemon)
            server = ClaudeServer([])
            with mock.patch.object(daemon, "_make_http_client", side_effect=lambda acc=None: FakeHTTP(server)):
                daemon._refresh_single_account(acc.id, True)
                clock.advance(FORCED_RESET_GRANTS_MIN_AGE_SEC - 1)
                daemon._refresh_single_account(acc.id, True)
                self.assertEqual(len(server.calls("GET", CLAUDE_RESET_GRANTS_URL)), 1)
                clock.advance(1)
                daemon._refresh_single_account(acc.id, True)
                self.assertEqual(len(server.calls("GET", CLAUDE_RESET_GRANTS_URL)), 2)

    def test_claude_claim_retry_reuses_request_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemon, _, out = self._daemon(tmp)
            acc = self._claude_account(daemon)
            server = ClaudeServer([
                _json_response(503, {}),
                _json_response(200, {"result": "reset", "resets_left": 0}),
            ])
            with mock.patch.object(daemon, "_make_http_client", side_effect=lambda acc=None: FakeHTTP(server)):
                daemon.handle_command({"cmd": "claim_reset", "accountId": acc.id, "resetItem": CLAUDE_RESET_ITEM})
                daemon.handle_command({"cmd": "claim_reset", "accountId": acc.id, "resetItem": CLAUDE_RESET_ITEM})

            ids = server.claim_request_ids()
            self.assertEqual(len(ids), 2)
            self.assertEqual(ids[0], ids[1])
            self.assertEqual(self._toasts(out)[-1]["message"], "Reset claimed. Enjoy!")

    def test_claim_unknown_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemon, _, out = self._daemon(tmp)
            daemon.handle_command({"cmd": "claim_reset", "accountId": "nope", "resetItem": CLAUDE_RESET_ITEM})
            self.assertEqual(self._toasts(out), [{"type": "toast", "message": "Account not found.", "error": True}])
            self.assertIsNone(daemon.refresher.claim("nope", CLAUDE_RESET_ITEM))

    def test_codex_claim_retry_reuses_matched_credit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = OmameterStore(config_dir=Path(tmp) / "cfg", cache_dir=Path(tmp) / "cache")
            vault = CredentialVault(fallback_path=Path(tmp) / "vault.json", use_secret_tool=False)
            expiry = format_iso8601(NOW + timedelta(days=3))
            consume = [_json_response(503, {}), _json_response(200, {"code": "reset"})]
            router = Router({
                ("GET", CODEX_RESET_CREDITS_URL): lambda kw: _json_response(200, {
                    "available_count": 1,
                    "credits": [{"id": "credit-1", "status": "available", "expires_at": expiry}],
                }),
                ("POST", CODEX_CONSUME_RESET_CREDIT_URL): lambda kw: consume.pop(0),
            })
            refresher = AccountRefresher(
                store=store,
                vault=vault,
                backoff=RefreshBackoff(),
                burning_tracker=BurningFastTracker(),
                notifier=QuotaNotificationEvaluator(post_fn=lambda *a: True),
                make_http=lambda acc: FakeHTTP(router),
                clock=FakeClock(NOW),
            )
            acc = Account.create("codex", "ChatGPT")
            store.add_account(acc)
            vault.set_account_credential(acc.id, {
                "accessToken": "tok-codex-access",
                "refreshToken": "tok-codex-refresh",
                "expiresAt": format_iso8601(NOW + timedelta(days=1)),
                "chatgpt_account_id": "acct-1",
            })
            item = {"providerKind": "codex", "requestId": "req-1", "expiresAt": expiry, "claimPayload": {"expires_at": expiry}}

            first = refresher.claim(acc.id, item)
            second = refresher.claim(acc.id, item)

            self.assertEqual(first["outcome"], "refused")
            self.assertEqual(second["outcome"], "success")
            self.assertEqual(len(router.calls("GET", CODEX_RESET_CREDITS_URL)), 1)
            bodies = [kw["json_body"] for kw in router.calls("POST", CODEX_CONSUME_RESET_CREDIT_URL)]
            self.assertEqual(bodies, [{"credit_id": "credit-1", "redeem_request_id": "req-1"}] * 2)
            self.assertIs(
                refresher.context_for(acc, force=False).secondary_cache,
                refresher.secondary_reads.for_account(acc.id),
            )


if __name__ == "__main__":
    unittest.main()
