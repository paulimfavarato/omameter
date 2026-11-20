"""Comprehensive unit tests for Omameter core engine (Pace, BurningFast, PriorityBoard, UnifiedAccounts, Notifications, Backoff, LimitsEnvelope)."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.backoff import RefreshBackoff, backoff_delay_seconds
from backend.http_client import ProxyConfig, redact_secrets
from backend.limits_envelope import build_limits_envelope, encode_limits_envelope_json
from backend.models import (
    Account,
    MetricLine,
    MetricPeriod,
    MetricValue,
    PriorityLimits,
    ProviderSnapshot,
    account_initials,
    format_duration_seconds,
    format_iso8601,
    short_metric_label,
)
from backend.pace import (
    BurningFastTracker,
    QuotaNotificationEvaluator,
    evaluate_pace,
    is_unused_near_reset,
    meter_state,
    pace_mark,
    runway_seconds,
)
from backend.priority import build_dashboard_state, pool_metric_lines
from backend.store import OmameterStore


NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)


def make_pct_line(
    key: str,
    title: str,
    used: float,
    *,
    resets_in_sec: float = 3600 * 3,
    period_ms: int = MetricPeriod.SESSION_MS,
    resource_key: str | None = None,
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
        resource_key=resource_key or key,
        resource_kind="consumption",
        resource_unit="percent",
    )


class TestModelsAndFormatting(unittest.TestCase):
    def test_account_initials(self) -> None:
        self.assertEqual(account_initials("Personal"), "P")
        self.assertEqual(account_initials("Attention Machine 1"), "A1")
        self.assertEqual(account_initials("Work Account"), "WA")

    def test_short_metric_label(self) -> None:
        self.assertEqual(short_metric_label("Cursor Models"), "Cursor")
        self.assertEqual(short_metric_label("Grok Bot"), "Grok")
        self.assertEqual(short_metric_label("Extra Usage"), "Extra")
        self.assertEqual(short_metric_label("Session"), "Session")

    def test_priority_limits_linked_push(self) -> None:
        lim = PriorityLimits(warning=30, critical=10)
        self.assertEqual((lim.warning, lim.critical), (30, 10))
        lim2 = lim.with_critical(25)
        self.assertEqual(lim2.critical, 25)
        self.assertEqual(lim2.warning, 30)
        lim3 = PriorityLimits(warning=20, critical=15).with_critical(20)
        self.assertEqual(lim3.critical, 20)
        self.assertEqual(lim3.warning, 25)
        lim4 = PriorityLimits(warning=25, critical=20).with_warning(20)
        self.assertEqual(lim4.warning, 20)
        self.assertEqual(lim4.critical, 15)

    def test_duration_formatting(self) -> None:
        self.assertEqual(format_duration_seconds(45 * 60), "45m")
        self.assertEqual(format_duration_seconds(4 * 3600 + 12 * 60), "4h 12m")
        self.assertEqual(format_duration_seconds(2 * 86400 + 5 * 3600), "2d 5h")
        self.assertEqual(format_duration_seconds(17 * 86400), "17d")

    def test_proxy_validation_and_redaction(self) -> None:
        ok, err = ProxyConfig.validate_url("socks5://user:secret@127.0.0.1:1080")
        self.assertTrue(ok)
        self.assertIsNone(err)
        ok2, _ = ProxyConfig.validate_url("ftp://example.com:21")
        self.assertFalse(ok2)
        redacted = redact_secrets("Bearer sk-ant-oat01-abcdefghij123456 socks5://alice:superpass@host:1080")
        self.assertNotIn("abcdefghij123456", redacted)
        self.assertNotIn("superpass", redacted)


class TestPaceAndNotifications(unittest.TestCase):
    def test_pace_minimum_elapsed_and_five_percent_floor(self) -> None:
        # 5h window = 18000s. 1% = 180s. Elapsed = 120s (< 180s) -> None
        too_early = make_pct_line("session", "Session", 40.0, resets_in_sec=18000 - 120)
        self.assertIsNone(evaluate_pace(too_early, NOW))

        # Elapsed = 50% (9000s), used = 3% (< 5% floor, projected = 6% <= 90% -> ahead IS allowed)
        low_ahead = make_pct_line("session", "Session", 3.0, resets_in_sec=9000)
        p = evaluate_pace(low_ahead, NOW)
        self.assertIsNotNone(p)
        self.assertEqual(p.status, "ahead")

        # Elapsed = 2% (360s), used = 4% (< 5% floor, projected = 200% -> behind suppressed by 5% floor!)
        tiny_spike = make_pct_line("session", "Session", 4.0, resets_in_sec=18000 - 360)
        self.assertIsNone(evaluate_pace(tiny_spike, NOW))

        # Elapsed = 50% (9000s), used = 60% -> projected 120% -> behind / running_out
        burning = make_pct_line("session", "Session", 60.0, resets_in_sec=9000)
        self.assertEqual(meter_state(burning, NOW), "running_out")
        rw = runway_seconds(burning, NOW)
        self.assertIsNotNone(rw)
        # 40% left at rate 60%/9000s = 6000s
        self.assertAlmostEqual(rw, 6000.0, delta=1.0)
        self.assertAlmostEqual(pace_mark(burning, NOW, display_mode="remaining"), 0.5, delta=0.01)
        self.assertAlmostEqual(pace_mark(burning, NOW, display_mode="used"), 0.5, delta=0.01)

    def test_burning_fast_hysteresis_90_percent_recovery(self) -> None:
        tracker = BurningFastTracker()
        # 50% elapsed, 60% used -> projected 120% -> burning
        l_burn = make_pct_line("s", "Session", 60.0, resets_in_sec=9000)
        self.assertTrue(tracker.latch("a.s", l_burn, NOW))

        # Later: 63% elapsed (6660s left), still 60% used -> projected 95.2% (close_to_limit) -> stays latched!
        l_close = make_pct_line("s", "Session", 60.0, resets_in_sec=18000 - 11340)
        self.assertEqual(meter_state(l_close, NOW), "close_to_limit")
        self.assertTrue(tracker.is_burning("a.s", l_close, NOW))

        # Later: 70% elapsed (5400s left), 60% used -> projected 85.7% (<= 90% -> healthy) -> clears!
        l_calm = make_pct_line("s", "Session", 60.0, resets_in_sec=5400)
        self.assertEqual(meter_state(l_calm, NOW), "healthy")
        self.assertFalse(tracker.latch("a.s", l_calm, NOW))

    def test_use_it_or_lose_it_rules(self) -> None:
        # Weekly (7d > 1d), 18h left, 60% remaining -> True
        wk = make_pct_line("weekly", "Weekly", 40.0, resets_in_sec=18 * 3600, period_ms=MetricPeriod.WEEK_MS)
        self.assertTrue(is_unused_near_reset(wk, NOW))

        # 5h session (<= 1d) -> False
        sess = make_pct_line("session", "Session", 10.0, resets_in_sec=3 * 3600, period_ms=MetricPeriod.SESSION_MS)
        self.assertFalse(is_unused_near_reset(sess, NOW))

        # Dollar spending cap -> False
        dollars = MetricLine(
            key="extra", title="Extra", line_type="progress", kind="dollars",
            used=10.0, limit=100.0,
            resets_at=format_iso8601(NOW + timedelta(hours=12)),
            period_duration_ms=MetricPeriod.MONTH_MS,
        )
        self.assertFalse(is_unused_near_reset(dollars, NOW))

    def test_notification_priming_and_multilevel_suppression(self) -> None:
        sent: list[tuple[str, str]] = []
        ev = QuotaNotificationEvaluator(post_fn=lambda t, s, b, u: (sent.append((t, s)) or True))
        lim = PriorityLimits(warning=30, critical=10)
        enabled = {"warning": True, "critical": True, "burningFast": False, "useItOrLoseIt": False}

        # Prime at 50% left (normal) -> no notification
        ev.evaluate("acc.s", "Claude", make_pct_line("s", "Session", 50.0), NOW, lim, enabled)
        self.assertEqual(sent, [])

        # Jump straight to 5% left (critical) -> ONLY Critical fires, Warning suppressed!
        ev.evaluate("acc.s", "Claude", make_pct_line("s", "Session", 95.0), NOW, lim, enabled)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "Critical")


class TestPriorityBoardAndUnifiedAccounts(unittest.TestCase):
    def test_unified_pooling_and_anti_false_spent(self) -> None:
        a = make_pct_line("session", "Session", 100.0, resets_in_sec=7200)
        b = make_pct_line("session", "Session", 99.4, resets_in_sec=3600)
        pooled = pool_metric_lines("session", [a, b])
        # Average = 99.7% -> would round to 100% spent, but b is NOT spent (0.6 rounds to 1), so anti-false-spent caps at 99!
        self.assertFalse(pooled.is_spent)
        self.assertEqual(pooled.resets_at, b.resets_at)
        self.assertIsNone(pooled.period_duration_ms)

    def test_menubar_overflow_excludes_used_up(self) -> None:
        acc = Account.create("claude", "Personal")
        snap = ProviderSnapshot(
            account_id=acc.id,
            card_id=acc.card_id,
            kind="claude",
            plan="Max",
            lines=[
                make_pct_line("session", "Session", 80.0, resets_in_sec=3600),  # 20% left -> warning
                make_pct_line("weekly", "Weekly", 100.0, resets_in_sec=7200, period_ms=MetricPeriod.WEEK_MS),  # used up
                make_pct_line("sonnet", "Sonnet", 100.0, resets_in_sec=7200, period_ms=MetricPeriod.WEEK_MS),  # used up
            ],
            refreshed_at=format_iso8601(NOW) or "",
            stale_at=format_iso8601(NOW + timedelta(minutes=10)) or "",
        )
        state = build_dashboard_state(
            accounts=[acc],
            snapshots={acc.id: snap},
            errors={},
            refreshing_ids=set(),
            layout={"hiddenMetricIDs": [], "priorityPinIDs": [], "providerOrder": ["claude"], "metricOrderByProvider": {}, "unifiedProviderKinds": []},
            settings={"maxBarItems": 1, "alwaysShowInBar": True, "warningThreshold": 30, "criticalThreshold": 10},
            burning_tracker=BurningFastTracker(),
            now=NOW,
        )
        # 1 runningOut item shown, 2 usedUp items excluded from overflow!
        self.assertEqual(len(state["bar"]["items"]), 1)
        self.assertEqual(state["bar"]["overflow"], 0)


class TestBackoffAndLimitsEnvelope(unittest.TestCase):
    def test_backoff_schedule_and_429_respect(self) -> None:
        self.assertEqual([backoff_delay_seconds(i) for i in range(1, 7)], [300, 600, 1200, 2400, 3600, 3600])
        bo = RefreshBackoff()
        bo.record_failure("a", kind="refused", reason="500", now=NOW)
        self.assertTrue(bo.holds("a", NOW + timedelta(seconds=60), forced=False))
        self.assertFalse(bo.holds("a", NOW + timedelta(seconds=60), forced=True))

        bo.record_failure("b", kind="rate_limited", reason="429", now=NOW, retry_after=900)
        self.assertTrue(bo.holds("b", NOW + timedelta(seconds=60), forced=True))

    def test_limits_envelope_v2_schema(self) -> None:
        acc = Account.create("codex", "Work")
        snap = ProviderSnapshot(
            account_id=acc.id,
            card_id=acc.card_id,
            kind="codex",
            plan="Pro 200",
            lines=[
                make_pct_line("session", "Session", 42.0, resets_in_sec=18000, resource_key="session"),
                MetricLine(
                    key="credits", title="Credits", line_type="values", kind="count",
                    values=[MetricValue(number=821, kind="count", label="credits")],
                    resource_key="credits", resource_kind="balance", resource_unit="credits",
                ),
            ],
            refreshed_at=format_iso8601(NOW) or "",
            stale_at=format_iso8601(NOW + timedelta(minutes=10)) or "",
        )
        env = build_limits_envelope([acc], {acc.id: snap}, now=NOW)
        self.assertEqual(env["schema"], "omameter.limits.v2")
        p = env["providers"][acc.card_id]
        self.assertEqual(p["displayName"], "ChatGPT · Work")
        self.assertEqual(p["plan"], "Pro 200")
        self.assertFalse(p["stale"])
        self.assertEqual(p["resources"]["session"]["remaining"], 58.0)
        self.assertAlmostEqual(p["resources"]["session"]["utilization"], 0.42)
        self.assertEqual(p["resources"]["credits"]["available"], 821.0)
        encoded = encode_limits_envelope_json(env)
        self.assertIn("omameter.limits.v2", encoded)

    def test_store_undo_and_hide_unpins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            st = OmameterStore(config_dir=Path(tmp) / "cfg", cache_dir=Path(tmp) / "cache")
            acc = Account.create("claude", "Personal")
            st.add_account(acc)
            mid = f"{acc.id}.session"
            st.set_metric_pinned([mid], True)
            self.assertIn(mid, st.layout["priorityPinIDs"])
            st.set_metric_hidden([mid], True)
            self.assertIn(mid, st.layout["hiddenMetricIDs"])
            self.assertNotIn(mid, st.layout["priorityPinIDs"])
            self.assertTrue(st.undo_layout())
            self.assertIn(mid, st.layout["priorityPinIDs"])


if __name__ == "__main__":
    unittest.main()
