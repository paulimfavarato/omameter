"""Unit tests for Backend UX improvements: local credential detection, reset expiry notifications, paceInlineKind/hiddenMetrics/bar.allItems, partial window badges, and idle re-emit suppression."""

from __future__ import annotations

import base64
import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from backend.daemon import OmameterDaemon
from backend.local_detect import (
    load_local_credential_payload,
    scan_local_credentials,
)
from backend.models import (
    Account,
    MetricLine,
    MetricPeriod,
    MetricValue,
    ProviderSnapshot,
    ResetItem,
    api_key_identity,
    format_iso8601,
)
from backend.pace import BurningFastTracker, QuotaNotificationEvaluator
from backend.priority import build_dashboard_state
from backend.providers.claude import map_claude_usage
from backend.providers.codex import map_codex_usage
from backend.store import OmameterStore
from backend.vault import CredentialVault


NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)


def _make_jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode("utf-8")).decode("ascii").rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    return f"{header}.{body}.signature"


def _make_pct_line(
    key: str,
    title: str,
    used: float,
    *,
    resets_in_sec: float = 3600 * 3,
    period_ms: int = MetricPeriod.SESSION_MS,
) -> MetricLine:
    return MetricLine(
        key=key,
        title=title,
        line_type="progress",
        kind="percent",
        used=used,
        limit=100.0,
        resets_at=format_iso8601(NOW + timedelta(seconds=resets_in_sec)),
        period_duration_ms=period_ms,
        resource_key=key,
        resource_kind="consumption",
        resource_unit="percent",
    )


class TestLocalCredentialDetection(unittest.TestCase):
    def test_scan_and_load_cli_files_env_and_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)

            # 1. Mock ~/.claude/.credentials.json (epoch ms expiresAt)
            claude_dir = home / ".claude"
            claude_dir.mkdir(parents=True)
            exp_dt = NOW + timedelta(hours=4)
            claude_secret = "sk-ant-oat01-SUPERSECRETCLAUDEACCESS12345"
            claude_refresh = "sk-ant-ort01-SUPERSECRETCLAUDEREFRESH67890"
            (claude_dir / ".credentials.json").write_text(
                json.dumps({
                    "claudeAiOauth": {
                        "accessToken": claude_secret,
                        "refreshToken": claude_refresh,
                        "expiresAt": int(exp_dt.timestamp() * 1000),
                        "email": "alice@anthropic.example",
                    }
                }),
                encoding="utf-8",
            )

            # 2. Mock ~/.codex/auth.json with JWTs
            codex_dir = home / ".codex"
            codex_dir.mkdir(parents=True)
            codex_exp = int((NOW + timedelta(hours=6)).timestamp())
            codex_access = _make_jwt({
                "exp": codex_exp,
                "https://api.openai.com/auth": {"chatgpt_account_id": "acct_codex_99"},
            })
            codex_id = _make_jwt({"email": "bob@openai.example"})
            codex_refresh = "rt_SUPERSECRETCODEXREFRESH"
            (codex_dir / "auth.json").write_text(
                json.dumps({
                    "tokens": {
                        "access_token": codex_access,
                        "refresh_token": codex_refresh,
                        "id_token": codex_id,
                        "account_id": "acct_codex_99",
                    }
                }),
                encoding="utf-8",
            )

            # 3. Mock env vars (including duplicate ZAI/ZHIPU key)
            or_key = "sk-or-v1-SUPERSECRETOPENROUTERKEY999"
            ol_key = "ollama_SUPERSECRETKEY888"
            zai_key = "zai_SUPERSECRETKEY777"
            oc_key = "oc_SUPERSECRETKEY666"
            env = {
                "OPENROUTER_API_KEY": or_key,
                "OLLAMA_API_KEY": ol_key,
                "ZAI_API_KEY": zai_key,
                "ZHIPU_API_KEY": zai_key,  # same key -> deduplicated
                "OPENCODE_API_KEY": oc_key,
            }

            items = scan_local_credentials([], home_dir=home, environ=env, check_gh=False)
            by_id = {it["id"]: it for it in items}
            self.assertEqual(
                set(by_id.keys()),
                {
                    "claude:cli",
                    "codex:cli",
                    "env:OPENROUTER_API_KEY",
                    "env:OLLAMA_API_KEY",
                    "env:ZAI_API_KEY",
                    "env:OPENCODE_API_KEY",
                },
            )

            # Strictly NO raw secrets leaked anywhere in scan output
            serialized = json.dumps(items)
            for secret in (claude_secret, claude_refresh, codex_access, codex_refresh, codex_id, or_key, ol_key, zai_key, oc_key):
                self.assertNotIn(secret, serialized)

            # Verify safe metadata schema
            self.assertEqual(by_id["claude:cli"]["kind"], "claude")
            self.assertEqual(by_id["claude:cli"]["displayName"], "Claude")
            self.assertEqual(by_id["claude:cli"]["label"], "alice@anthropic.example")
            self.assertFalse(by_id["claude:cli"]["alreadyAdded"])

            self.assertEqual(by_id["codex:cli"]["kind"], "codex")
            self.assertEqual(by_id["codex:cli"]["displayName"], "ChatGPT")
            self.assertEqual(by_id["codex:cli"]["label"], "bob@openai.example")
            self.assertFalse(by_id["codex:cli"]["alreadyAdded"])

            # Load payloads and verify formats
            c_kind, c_method, c_label, c_cred, c_ident = load_local_credential_payload("claude:cli", home_dir=home, environ=env)
            self.assertEqual((c_kind, c_method, c_label), ("claude", "oauth", "alice@anthropic.example"))
            self.assertEqual(c_cred["accessToken"], claude_secret)
            self.assertEqual(c_cred["refreshToken"], claude_refresh)
            self.assertEqual(c_cred["expiresAt"], format_iso8601(exp_dt))

            x_kind, x_method, x_label, x_cred, x_ident = load_local_credential_payload("codex:cli", home_dir=home, environ=env)
            self.assertEqual((x_kind, x_method, x_label), ("codex", "oauth", "bob@openai.example"))
            self.assertEqual(x_ident, "acct_codex_99/bob@openai.example")
            self.assertEqual(x_cred["extras"]["chatgpt_account_id"], "acct_codex_99")
            self.assertEqual(x_cred["expiresAt"], format_iso8601(datetime.fromtimestamp(codex_exp, tz=timezone.utc)))

            o_kind, o_method, _o_label, o_cred, o_ident = load_local_credential_payload("env:OPENROUTER_API_KEY", home_dir=home, environ=env)
            self.assertEqual((o_kind, o_method), ("openrouter", "api_key"))
            self.assertEqual(o_cred["apiKey"], or_key)
            self.assertEqual(o_ident, api_key_identity(or_key))

            # Verify alreadyAdded detection
            existing = [
                Account.create("claude", "alice@anthropic.example", identity=c_ident),
                Account.create("codex", "Work", identity="acct_codex_99/bob@openai.example"),
                Account.create("openrouter", "OR", identity=api_key_identity(or_key)),
                Account.create("ollama", "Other Ollama", identity=api_key_identity("different-key")),
            ]
            items2 = scan_local_credentials(existing, home_dir=home, environ=env, check_gh=False)
            by_id2 = {it["id"]: it for it in items2}
            self.assertTrue(by_id2["claude:cli"]["alreadyAdded"])
            self.assertTrue(by_id2["codex:cli"]["alreadyAdded"])
            self.assertTrue(by_id2["env:OPENROUTER_API_KEY"]["alreadyAdded"])
            self.assertFalse(by_id2["env:OLLAMA_API_KEY"]["alreadyAdded"])
            self.assertFalse(by_id2["env:ZAI_API_KEY"]["alreadyAdded"])

            # Unknown candidate raises ValueError
            with self.assertRaises(ValueError):
                load_local_credential_payload("does:not:exist", home_dir=home, environ=env)

    def test_copilot_gh_detection_mocked(self) -> None:
        gh_secret = "gho_SUPERSECRETGITHUBTOKEN123456"
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: object) -> mock.Mock:
            calls.append(list(cmd))
            if cmd[1:] == ["auth", "status"]:
                return mock.Mock(
                    returncode=0,
                    stdout="github.com\n  ✓ Logged in to github.com account octocat (keyring)\n",
                    stderr="",
                )
            if cmd[1:] == ["auth", "token"]:
                return mock.Mock(returncode=0, stdout=gh_secret + "\n", stderr="")
            return mock.Mock(returncode=1, stdout="", stderr="unexpected")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("backend.local_detect.shutil.which", return_value="/usr/bin/gh"), \
                 mock.patch("backend.local_detect.subprocess.run", side_effect=fake_run):
                items = scan_local_credentials([], home_dir=Path(tmp), environ={}, check_gh=True)
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0]["id"], "copilot:gh")
                self.assertEqual(items[0]["kind"], "copilot")
                self.assertEqual(items[0]["label"], "octocat")
                self.assertNotIn(gh_secret, json.dumps(items))
                # Scanning must never read the raw GitHub token.
                self.assertEqual(calls, [["/usr/bin/gh", "auth", "status"]])

                kind, method, label, cred, ident = load_local_credential_payload("copilot:gh", home_dir=Path(tmp), environ={})
                self.assertEqual((kind, method, label), ("copilot", "oauth", "GitHub CLI"))
                self.assertEqual(cred["accessToken"], gh_secret)
                self.assertEqual(ident, api_key_identity(gh_secret))
                self.assertEqual(calls[-1], ["/usr/bin/gh", "auth", "token"])


class TestResetExpiryNotifications(unittest.TestCase):
    def test_evaluate_reset_expiries_window_dedup_and_toggle(self) -> None:
        posted: list[tuple[str, str, str, str]] = []
        ev = QuotaNotificationEvaluator(post_fn=lambda t, s, b, u: (posted.append((t, s, b, u)) or True))

        line = MetricLine(
            key="rateLimitResets",
            title="Limit Resets",
            line_type="values",
            kind="count",
            values=[MetricValue(number=3.0, kind="count", label="available")],
            reset_items=[
                ResetItem(
                    id="soon-1",
                    provider_kind="claude",
                    kind="session",
                    title="Session Reset",
                    expires_at=format_iso8601(NOW + timedelta(hours=18)),
                ),
                ResetItem(
                    id="far-1",
                    provider_kind="claude",
                    kind="weekly",
                    title="Weekly Reset",
                    expires_at=format_iso8601(NOW + timedelta(hours=72)),
                ),
                ResetItem(
                    id="expired-1",
                    provider_kind="claude",
                    kind="full",
                    title="Full Reset",
                    expires_at=format_iso8601(NOW - timedelta(hours=1)),
                ),
            ],
        )

        # Disabled -> fires nothing and does not suppress later enablement
        self.assertEqual(
            ev.evaluate_reset_expiries(account_id="acc1", provider_name="Claude", line=line, now=NOW, enabled=False),
            [],
        )
        self.assertEqual(posted, [])

        # Enabled -> fires ONLY soon-1 (within 48h and > 0)
        fired = ev.evaluate_reset_expiries(account_id="acc1", provider_name="Claude", line=line, now=NOW, enabled=True)
        self.assertEqual(fired, ["soon-1"])
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0][0], "Limit Reset Expiring")
        self.assertEqual(posted[0][1], "Claude")
        self.assertIn("Session Reset expires in 18h.", posted[0][2])
        self.assertEqual(posted[0][3], "normal")

        # Repeated evaluation -> deduplicated
        fired2 = ev.evaluate_reset_expiries(account_id="acc1", provider_name="Claude", line=line, now=NOW, enabled=True)
        self.assertEqual(fired2, [])
        self.assertEqual(len(posted), 1)


class TestPriorityUxMetadata(unittest.TestCase):
    def test_pace_inline_hidden_metrics_and_bar_all_items(self) -> None:
        acc = Account.create("claude", "Personal")
        # 50% elapsed (9000s left of 18000s), 60% used -> projected 120% -> running_out, runway 6000s (< 9000s) -> "Out in 1h 40m"
        burning = _make_pct_line("session", "Session", 60.0, resets_in_sec=9000)
        # 50% elapsed, 30% used -> projected 60% -> healthy -> "~40% left at reset"
        healthy = _make_pct_line("weekly", "Weekly", 30.0, resets_in_sec=MetricPeriod.WEEK_MS / 2000.0, period_ms=MetricPeriod.WEEK_MS)
        # Hidden metric
        hidden_ln = _make_pct_line("sonnet", "Sonnet", 50.0, resets_in_sec=3600 * 10, period_ms=MetricPeriod.WEEK_MS)
        # Used up metric (for bar.allItems)
        spent_ln = _make_pct_line("fable", "Fable", 100.0, resets_in_sec=3600 * 12, period_ms=MetricPeriod.WEEK_MS)
        # Non-bounded badge metric
        badge_ln = MetricLine(key="info", title="Info", line_type="badge", kind="count", badge_text="Not reported")

        snap = ProviderSnapshot(
            account_id=acc.id,
            card_id=acc.card_id,
            kind="claude",
            plan="Max",
            lines=[burning, healthy, hidden_ln, spent_ln, badge_ln],
            refreshed_at=format_iso8601(NOW) or "",
            stale_at=format_iso8601(NOW + timedelta(minutes=10)) or "",
        )
        tracker = BurningFastTracker()
        tracker.latch(f"{acc.id}.session", burning, NOW)

        dash = build_dashboard_state(
            accounts=[acc],
            snapshots={acc.id: snap},
            errors={},
            refreshing_ids=set(),
            layout={
                "hiddenMetricIDs": [f"{acc.id}.sonnet"],
                "priorityPinIDs": [f"{acc.id}.weekly"],
                "providerOrder": ["claude"],
                "metricOrderByProvider": {},
                "unifiedProviderKinds": [],
            },
            settings={
                "showPaceMarks": True,
                "maxBarItems": 1,
                "alwaysShowInBar": True,
                "warningThreshold": 30,
                "criticalThreshold": 10,
            },
            burning_tracker=tracker,
            now=NOW,
        )

        # Inline pace is data (kind + instant), never a frozen duration string
        lines_by_key = {l["key"]: l for l in dash["allUsage"][0]["accounts"][0]["lines"]}
        self.assertNotIn("sonnet", lines_by_key)
        self.assertEqual(lines_by_key["session"]["meterState"], "running_out")
        self.assertEqual(lines_by_key["session"]["paceInlineKind"], "outIn")
        self.assertEqual(lines_by_key["session"]["runoutAt"], format_iso8601(NOW + timedelta(seconds=6000)))
        self.assertEqual(lines_by_key["weekly"]["meterState"], "healthy")
        self.assertEqual(lines_by_key["weekly"]["paceInlineKind"], "pace")
        self.assertEqual(lines_by_key["weekly"]["paceTooltip"], "~40% left at reset")
        self.assertEqual(lines_by_key["weekly"]["runoutAt"], "")
        self.assertEqual(lines_by_key["info"]["meterState"], "")
        self.assertEqual(lines_by_key["info"]["paceInlineKind"], "")
        for ln in lines_by_key.values():
            self.assertNotIn("resetText", ln)
            self.assertNotIn("paceInline", ln)

        # Priority notes are kinds; the burning session counts down to runoutAt, the pinned weekly follows resetDisplayMode
        prio = dash["priority"]
        self.assertEqual([(it["key"], it["priorityReason"], it["noteKind"]) for it in prio["runningOut"]],
                         [("session", "burningFast", "outIn")])
        self.assertEqual([(it["key"], it["noteKind"]) for it in prio["pinned"]], [("weekly", "reset")])
        self.assertEqual([(it["key"], it["noteKind"]) for it in prio["usedUp"]], [("fable", "resetCompact")])
        self.assertNotIn("priorityNote", prio["runningOut"][0])

        # Check hiddenMetrics metadata
        self.assertEqual(len(dash["hiddenMetrics"]), 1)
        hm = dash["hiddenMetrics"][0]
        self.assertEqual(hm["metricId"], f"{acc.id}.sonnet")
        self.assertEqual(hm["accountId"], acc.id)
        self.assertEqual(hm["key"], "sonnet")
        self.assertEqual(hm["kind"], "claude")
        self.assertEqual(hm["providerName"], "Claude")
        self.assertEqual(hm["accountName"], "Personal")
        self.assertEqual(hm["title"], "Sonnet")

        # Check bar.items vs bar.allItems (maxBarItems=1, but 3 eligible: pinned weekly + burning session + usedUp fable)
        self.assertEqual(len(dash["bar"]["items"]), 1)
        self.assertEqual(len(dash["bar"]["allItems"]), 3)
        self.assertEqual(dash["bar"]["overflow"], 1)  # 2 counted (weekly + session) - 1 shown = 1

        # When showPaceMarks=False, there is no inline pace text
        dash_no_pace = build_dashboard_state(
            accounts=[acc],
            snapshots={acc.id: snap},
            errors={},
            refreshing_ids=set(),
            layout={"hiddenMetricIDs": [], "priorityPinIDs": [], "providerOrder": ["claude"], "metricOrderByProvider": {}, "unifiedProviderKinds": []},
            settings={"showPaceMarks": False, "maxBarItems": 3},
            burning_tracker=tracker,
            now=NOW,
        )
        lines_no_pace = {l["key"]: l for l in dash_no_pace["allUsage"][0]["accounts"][0]["lines"]}
        self.assertEqual(lines_no_pace["session"]["paceInlineKind"], "")
        self.assertEqual(lines_no_pace["session"]["meterState"], "running_out")


class TestPartialMultiWindowNotReported(unittest.TestCase):
    def test_codex_partial_windows(self) -> None:
        # Only primary (session) -> adds weekly Not reported badge
        _, lines_only_session = map_codex_usage(
            {
                "rate_limit": {
                    "primary_window": {"used_percent": 25, "limit_window_seconds": 18000, "reset_at": int((NOW + timedelta(hours=2)).timestamp())},
                    "secondary_window": None,
                }
            },
            now=NOW,
        )
        self.assertEqual([l.key for l in lines_only_session], ["session", "weekly"])
        self.assertEqual(lines_only_session[0].line_type, "progress")
        self.assertEqual(lines_only_session[1].line_type, "badge")
        self.assertEqual(lines_only_session[1].badge_text, "Not reported")

        # Only secondary (weekly) -> adds session Not reported badge
        _, lines_only_weekly = map_codex_usage(
            {
                "rate_limit": {
                    "primary_window": None,
                    "secondary_window": {"used_percent": 40, "limit_window_seconds": 604800, "reset_at": int((NOW + timedelta(days=3)).timestamp())},
                }
            },
            now=NOW,
        )
        self.assertEqual([l.key for l in lines_only_weekly], ["session", "weekly"])
        self.assertEqual(lines_only_weekly[0].line_type, "badge")
        self.assertEqual(lines_only_weekly[0].badge_text, "Not reported")
        self.assertEqual(lines_only_weekly[1].line_type, "progress")

        # Completely empty -> unchanged (no Not reported badges)
        _, lines_empty = map_codex_usage({}, now=NOW)
        self.assertEqual(lines_empty, [])

    def test_claude_partial_windows(self) -> None:
        # Only five_hour -> adds weekly Not reported badge
        lines_only_session = map_claude_usage(
            {"five_hour": {"utilization": 20.0, "resets_at": "2026-07-13T15:00:00Z"}},
            now=NOW,
        )
        self.assertEqual([l.key for l in lines_only_session], ["session", "weekly"])
        self.assertEqual(lines_only_session[0].line_type, "progress")
        self.assertEqual(lines_only_session[1].line_type, "badge")
        self.assertEqual(lines_only_session[1].badge_text, "Not reported")

        # Only seven_day -> adds session Not reported badge
        lines_only_weekly = map_claude_usage(
            {"seven_day": {"utilization": 55.0, "resets_at": "2026-07-18T00:00:00Z"}},
            now=NOW,
        )
        self.assertEqual([l.key for l in lines_only_weekly], ["session", "weekly"])
        self.assertEqual(lines_only_weekly[0].line_type, "badge")
        self.assertEqual(lines_only_weekly[0].badge_text, "Not reported")
        self.assertEqual(lines_only_weekly[1].line_type, "progress")

        # Completely empty -> unchanged
        lines_empty = map_claude_usage({}, now=NOW)
        self.assertEqual(lines_empty, [])


class TestDaemonUxBehaviors(unittest.TestCase):
    def test_idle_reset_window_crossing_and_local_import_handlers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from backend.http_client import ProxyConfig
            from tests.fakes import FakeClock, RecordingExecutor

            emitted: list[dict] = []

            class _Out:
                def write(self, text: str) -> None:
                    for line in text.splitlines():
                        if line.strip():
                            emitted.append(json.loads(line))

                def flush(self) -> None:
                    pass

            executor = RecordingExecutor()
            submitted = executor.calls
            daemon = OmameterDaemon(
                store=OmameterStore(config_dir=Path(tmp) / "cfg", cache_dir=Path(tmp) / "cache"),
                vault=CredentialVault(fallback_path=Path(tmp) / "vault.json", use_secret_tool=False),
                proxy=ProxyConfig(enabled=False, url=""),
                notifier=QuotaNotificationEvaluator(post_fn=lambda *a: True),
                clock=FakeClock(NOW),
                executor=executor,
                out=_Out(),
            )

            # 1. Idle reset window crossing check
            acc = Account.create("claude", "Personal")
            daemon.store.add_account(acc)
            daemon.store.snapshots[acc.id] = ProviderSnapshot(
                account_id=acc.id,
                card_id=acc.card_id,
                kind="claude",
                lines=[_make_pct_line("session", "Session", 40.0, resets_in_sec=120)],
                refreshed_at=format_iso8601(NOW) or "",
                stale_at=format_iso8601(NOW + timedelta(minutes=10)) or "",
            )
            self.assertFalse(daemon._has_crossed_reset_window(NOW + timedelta(seconds=60)))
            self.assertTrue(daemon._has_crossed_reset_window(NOW + timedelta(seconds=125)))

            # 2. Local credential detect + import roundtrip (no network)
            fake_key = "sk-or-v1-LOCALIMPORTTESTKEY"
            fake_ident = api_key_identity(fake_key)
            fake_items_before = [{
                "id": "env:OPENROUTER_API_KEY",
                "kind": "openrouter",
                "displayName": "OpenRouter",
                "icon": "openrouter.svg",
                "source": "OPENROUTER_API_KEY",
                "method": "api_key",
                "label": "OpenRouter (OPENROUTER_API_KEY)",
                "alreadyAdded": False,
            }]
            fake_items_after = [dict(fake_items_before[0], alreadyAdded=True)]

            with mock.patch("backend.daemon.scan_local_credentials", side_effect=[fake_items_before, fake_items_after]), \
                 mock.patch(
                     "backend.daemon.load_local_credential_payload",
                     return_value=("openrouter", "api_key", "OpenRouter (OPENROUTER_API_KEY)", {"type": "api_key", "apiKey": fake_key, "identity": fake_ident}, fake_ident),
                 ):
                daemon._handle_detect_local_credentials()
                self.assertEqual(emitted[-1]["type"], "local_credentials")
                self.assertEqual(len(emitted[-1]["items"]), 1)

                emitted.clear()
                daemon._handle_import_local_credential("env:OPENROUTER_API_KEY")
                types = [e["type"] for e in emitted]
                self.assertIn("toast", types)
                self.assertIn("state", types)
                self.assertIn("local_credentials", types)
                toast = next(e for e in emitted if e["type"] == "toast")
                self.assertEqual(toast["message"], "Imported OpenRouter from OPENROUTER_API_KEY")
                self.assertFalse(toast["error"])
                # Refresh scheduled for imported account
                self.assertEqual(len(submitted), 1)
                self.assertEqual(submitted[0][0], daemon._refresh_single_account)
                self.assertTrue(submitted[0][1][1])


class SvgIconAssetTests(unittest.TestCase):
    def test_svg_icons_use_white_fill_for_multieffect_tinting(self):
        # Qt Quick MultiEffect colorization preserves source luminance: currentColor
        # or black renders as #000000 and stays black on dark bars/popups.
        icons_dir = Path(__file__).resolve().parent.parent / "icons"
        svgs = sorted(icons_dir.glob("*.svg"))
        self.assertGreaterEqual(len(svgs), 15)
        for svg in svgs:
            content = svg.read_text(encoding="utf-8")
            fills = re.findall(r'fill="([^"]+)"', content)
            painted = [f for f in fills if f != "none"]
            self.assertTrue(painted, f"{svg.name} must have at least one painted fill")
            for f in painted:
                self.assertEqual(
                    f.lower(),
                    "#ffffff",
                    f"{svg.name} has non-white fill={f!r}; MultiEffect requires #ffffff",
                )


class TopBarPinningAndWindowTagTests(unittest.TestCase):
    def test_pinned_items_lead_top_bar_even_when_always_show_in_bar_is_false(self) -> None:
        from backend.models import metric_window_info

        acc_ag = Account.create("antigravity", "Work")
        acc_cl = Account.create("claude", "Personal")
        snap_ag = ProviderSnapshot(
            account_id=acc_ag.id,
            card_id=acc_ag.card_id,
            kind="antigravity",
            lines=[
                _make_pct_line("geminiPro", "Gemini 5h", 15.0, resets_in_sec=3600, period_ms=MetricPeriod.SESSION_MS),
                _make_pct_line("geminiWeekly", "Gemini", 80.0, resets_in_sec=5 * 86400, period_ms=MetricPeriod.WEEK_MS),
            ],
            refreshed_at=format_iso8601(NOW) or "",
            stale_at=format_iso8601(NOW + timedelta(minutes=10)) or "",
        )
        snap_cl = ProviderSnapshot(
            account_id=acc_cl.id,
            card_id=acc_cl.card_id,
            kind="claude",
            lines=[
                _make_pct_line("session", "Session", 10.0, resets_in_sec=4 * 3600, period_ms=MetricPeriod.SESSION_MS),
                _make_pct_line("weekly", "Weekly", 100.0, resets_in_sec=4 * 86400, period_ms=MetricPeriod.WEEK_MS),
            ],
            refreshed_at=format_iso8601(NOW) or "",
            stale_at=format_iso8601(NOW + timedelta(minutes=10)) or "",
        )

        # Window info distinguishes 5h vs session vs weekly vs daily vs monthly vs extra
        self.assertEqual(metric_window_info(snap_ag.lines[0]), ("5h", "5h"))
        self.assertEqual(metric_window_info(snap_ag.lines[1]), ("weekly", "7d"))
        self.assertEqual(metric_window_info(snap_cl.lines[0]), ("session", "5h"))
        self.assertEqual(metric_window_info(snap_cl.lines[1]), ("weekly", "7d"))
        self.assertEqual(
            metric_window_info(MetricLine(key="daily", title="Daily", line_type="progress", period_duration_ms=MetricPeriod.DAY_MS)),
            ("daily", "1d"),
        )
        self.assertEqual(
            metric_window_info(MetricLine(key="monthly", title="Monthly", line_type="progress", period_duration_ms=MetricPeriod.MONTH_MS)),
            ("monthly", "30d"),
        )
        self.assertEqual(
            metric_window_info(MetricLine(key="extra", title="Extra Usage", line_type="values", kind="dollars")),
            ("extra", "$"),
        )
        self.assertEqual(
            metric_window_info(
                MetricLine(
                    key="rateLimitResets",
                    title="Limit Resets",
                    line_type="values",
                    kind="count",
                    resource_kind="balance",
                    resource_unit="resets",
                )
            ),
            ("", ""),
        )

        # 1. With alwaysShowInBar=False and NO pins, only threshold-low (warning/critical) limits appear
        dash_auto = build_dashboard_state(
            accounts=[acc_ag, acc_cl],
            snapshots={acc_ag.id: snap_ag, acc_cl.id: snap_cl},
            errors={},
            refreshing_ids=set(),
            layout={"hiddenMetricIDs": [], "priorityPinIDs": [], "providerOrder": ["claude", "antigravity"], "metricOrderByProvider": {}, "unifiedProviderKinds": []},
            settings={"alwaysShowInBar": False, "maxBarItems": 3, "warningThreshold": 30, "criticalThreshold": 10},
            burning_tracker=BurningFastTracker(),
            now=NOW,
        )
        self.assertEqual([it["metricId"] for it in dash_auto["bar"]["items"]], [f"{acc_ag.id}.geminiWeekly"])

        # 1b. Healthy burning-fast metrics alone do NOT populate the bar when alwaysShowInBar=False and nothing is pinned
        snap_burn = ProviderSnapshot(
            account_id=acc_ag.id,
            card_id=acc_ag.card_id,
            kind="antigravity",
            lines=[
                # 10% used in first ~10h of a 7d window => burningFast, but 90% remaining (severity == normal)
                _make_pct_line("geminiWeekly", "Gemini", 10.0, resets_in_sec=int(6.5 * 86400), period_ms=MetricPeriod.WEEK_MS),
            ],
            refreshed_at=format_iso8601(NOW) or "",
            stale_at=format_iso8601(NOW + timedelta(minutes=10)) or "",
        )
        dash_burn_off = build_dashboard_state(
            accounts=[acc_ag],
            snapshots={acc_ag.id: snap_burn},
            errors={},
            refreshing_ids=set(),
            layout={"hiddenMetricIDs": [], "priorityPinIDs": [], "providerOrder": ["antigravity"], "metricOrderByProvider": {}, "unifiedProviderKinds": []},
            settings={"alwaysShowInBar": False, "maxBarItems": 5, "warningThreshold": 30, "criticalThreshold": 10},
            burning_tracker=BurningFastTracker(),
            now=NOW,
        )
        self.assertEqual(dash_burn_off["bar"]["items"], [])
        self.assertTrue(dash_burn_off["bar"]["iconOnly"])

        # 2. With alwaysShowInBar=False and pinned metrics, ONLY the pinned metrics appear in exact pin order
        # (even when Antigravity is after Claude in providerOrder, pinning Gemini 5h first puts it first!)
        pins = [f"{acc_ag.id}.geminiPro", f"{acc_cl.id}.session", f"{acc_cl.id}.weekly"]
        dash_pinned = build_dashboard_state(
            accounts=[acc_ag, acc_cl],
            snapshots={acc_ag.id: snap_ag, acc_cl.id: snap_cl},
            errors={},
            refreshing_ids=set(),
            layout={"hiddenMetricIDs": [], "priorityPinIDs": pins, "providerOrder": ["claude", "antigravity"], "metricOrderByProvider": {}, "unifiedProviderKinds": []},
            settings={"alwaysShowInBar": False, "maxBarItems": 5, "warningThreshold": 30, "criticalThreshold": 10},
            burning_tracker=BurningFastTracker(),
            now=NOW,
        )
        self.assertEqual(
            [it["metricId"] for it in dash_pinned["bar"]["items"]],
            [f"{acc_ag.id}.geminiPro", f"{acc_cl.id}.session", f"{acc_cl.id}.weekly"],
        )
        self.assertEqual(
            [(it["windowKind"], it["windowTag"]) for it in dash_pinned["bar"]["items"]],
            [("5h", "5h"), ("session", "5h"), ("weekly", "7d")],
        )
        self.assertEqual(dash_pinned["bar"]["overflow"], 0)
        # Priority tab still puts spent pinned weekly in usedUp, not duplicated in priority.pinned,
        # and keeps pinned order in priority.pinned
        self.assertEqual(
            [it["metricId"] for it in dash_pinned["priority"]["pinned"]],
            [f"{acc_ag.id}.geminiPro", f"{acc_cl.id}.session"],
        )
        self.assertEqual([it["metricId"] for it in dash_pinned["priority"]["usedUp"]], [f"{acc_cl.id}.weekly"])

        # 3. With alwaysShowInBar=True and a spent pinned metric, pinned metrics lead in pin order,
        # unpinned urgent limits fill remaining slots, and allItems has no duplicates
        dash_always = build_dashboard_state(
            accounts=[acc_ag, acc_cl],
            snapshots={acc_ag.id: snap_ag, acc_cl.id: snap_cl},
            errors={},
            refreshing_ids=set(),
            layout={"hiddenMetricIDs": [], "priorityPinIDs": pins, "providerOrder": ["claude", "antigravity"], "metricOrderByProvider": {}, "unifiedProviderKinds": []},
            settings={"alwaysShowInBar": True, "maxBarItems": 5, "warningThreshold": 30, "criticalThreshold": 10},
            burning_tracker=BurningFastTracker(),
            now=NOW,
        )
        all_ids = [it["metricId"] for it in dash_always["bar"]["allItems"]]
        self.assertEqual(all_ids, [f"{acc_ag.id}.geminiPro", f"{acc_cl.id}.session", f"{acc_cl.id}.weekly", f"{acc_ag.id}.geminiWeekly"])
        self.assertEqual(len(all_ids), len(set(all_ids)))

        # 4. OmameterStore.set_metric_pinned preserves user selection order (not alphabetical)
        with tempfile.TemporaryDirectory() as tmp:
            store = OmameterStore(config_dir=Path(tmp) / "cfg", cache_dir=Path(tmp) / "cache")
            store.set_metric_pinned(["z.geminiPro"], True)
            store.set_metric_pinned(["a.session"], True)
            store.set_metric_pinned(["m.weekly"], True)
            self.assertEqual(store.layout["priorityPinIDs"], ["z.geminiPro", "a.session", "m.weekly"])
            store.set_metric_pinned(["a.session"], False)
            self.assertEqual(store.layout["priorityPinIDs"], ["z.geminiPro", "m.weekly"])


if __name__ == "__main__":
    unittest.main()
