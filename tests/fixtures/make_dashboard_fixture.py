"""Builds tests/fixtures/dashboard.json from the real dashboard builder.

The JS tests (tests/test_qml_model.mjs) read this file so Model.js is checked
against what backend/priority.py actually emits, not a hand-written guess.
tests/test_dashboard_fixture.py fails when the file drifts from the builder.

Regenerate after changing the dashboard payload (from the repository root):

    python3 tests/fixtures/make_dashboard_fixture.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.models import (  # noqa: E402
    Account,
    MetricLine,
    MetricPeriod,
    MetricValue,
    ProviderSnapshot,
    UsageBreakdown,
    UsageBreakdownModel,
    format_iso8601,
)
from backend.pace import BurningFastTracker  # noqa: E402
from backend.priority import build_dashboard_state  # noqa: E402

FIXTURE_PATH = Path(__file__).resolve().parent / "dashboard.json"
NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)

CLAUDE_PERSONAL = "11111111-1111-4111-8111-111111111111"
CLAUDE_WORK = "22222222-2222-4222-8222-222222222222"
CODEX = "33333333-3333-4333-8333-333333333333"
CURSOR = "44444444-4444-4444-8444-444444444444"
OPENROUTER = "55555555-5555-4555-8555-555555555555"
COPILOT = "66666666-6666-4666-8666-666666666666"

SETTINGS: Dict[str, Any] = {
    "showPaceMarks": True,
    "alwaysShowInBar": True,
    "maxBarItems": 3,
    "warningThreshold": 30,
    "criticalThreshold": 10,
    "resetDisplayMode": "relative",
    "timeFormat": "auto",
}


def _iso(seconds_from_now: float) -> str:
    return format_iso8601(NOW + timedelta(seconds=seconds_from_now)) or ""


def _pct(key: str, title: str, used: float, resets_in_sec: float, period_ms: int) -> MetricLine:
    return MetricLine(
        key=key, title=title, line_type="progress", kind="percent", used=used, limit=100.0,
        resets_at=_iso(resets_in_sec), period_duration_ms=period_ms,
        resource_key=key, resource_kind="consumption", resource_unit="percent",
    )


def _account(acc_id: str, kind: str, label: str, organization: Optional[str] = None) -> Account:
    return Account(id=acc_id, kind=kind, label=label, created_at=_iso(-86400), organization=organization)


def _snapshot(acc: Account, plan: Optional[str], lines: List[MetricLine]) -> ProviderSnapshot:
    return ProviderSnapshot(
        account_id=acc.id, card_id=acc.card_id, kind=acc.kind, plan=plan, lines=lines,
        refreshed_at=_iso(0), stale_at=_iso(600),
    )


def _build(privacy_mode: bool) -> Dict[str, Any]:
    day = 86400
    week = MetricPeriod.WEEK_MS
    session = MetricPeriod.SESSION_MS

    personal = _account(CLAUDE_PERSONAL, "claude", "Personal")
    work = _account(CLAUDE_WORK, "claude", "Work")
    codex = _account(CODEX, "codex", "dev@example.com")
    cursor = _account(CURSOR, "cursor", "")
    openrouter = _account(OPENROUTER, "openrouter", '<a href="https://evil.invalid">Ops</a>')
    copilot = _account(COPILOT, "copilot", "Acme Org", organization="acme")

    burst = _pct("burst", "Burst", 60.0, 9000, session)  # running out: runway 6000s < 9000s to reset
    breakdown = UsageBreakdown(
        title="Usage Breakdown", spend=12.5, currency_code="USD", tokens=1_200_000,
        models=[UsageBreakdownModel(name=f"model-{i}", spend=float(7 - i), tokens=1000.0 * (7 - i)) for i in range(7)],
    )
    snapshots = {
        personal.id: _snapshot(personal, "Max", [
            _pct("session", "Session", 20.0, 2 * 3600, session),
            _pct("weekly", "Weekly", 10.0, 2 * day, week),
        ]),
        work.id: _snapshot(work, "Pro", [
            _pct("session", "Session", 40.0, 3 * 3600, session),
            _pct("weekly", "Weekly", 30.0, 2 * day, week),
        ]),
        codex.id: _snapshot(codex, "Plus", [
            _pct("session", "Session", 75.0, 16200, session),  # low: 25% left, resets in 4h 30m
            burst,
            _pct("weekly", "Weekly", 10.0, 3 * day, week),  # pinned, resets in days
            _pct("fable", "Fable", 100.0, 12 * 3600, week),  # used up
        ]),
        cursor.id: _snapshot(cursor, "Pro", [
            _pct("monthly", "Monthly Requests", 10.0, 20 * 3600, 30 * day * 1000),  # use it or lose it
            MetricLine(
                key="usage", title="On-Demand Usage", line_type="values", kind="dollars",
                values=[MetricValue(number=12.5, kind="dollars", label="spent", currency_code="USD")],
                subtitle="Billing cycle ends Jul 31", breakdown=breakdown,
                resource_key="usage", resource_kind="consumption", resource_unit="usd",
            ),
        ]),
        openrouter.id: _snapshot(openrouter, None, [
            MetricLine(
                key="credits", title="Credits", line_type="values", kind="dollars",
                values=[MetricValue(number=0.0, kind="dollars", label="left", currency_code="USD")],
                resource_key="credits", resource_kind="balance", resource_unit="usd",
            ),
        ]),
        copilot.id: _snapshot(copilot, "Business", [
            MetricLine(key="premium", title="Premium Requests", line_type="badge", kind="count", badge_text="Not reported"),
        ]),
    }
    return build_dashboard_state(
        accounts=[personal, work, codex, cursor, openrouter, copilot],
        snapshots=snapshots,
        errors={},
        refreshing_ids=set(),
        layout={
            "hiddenMetricIDs": [],
            "priorityPinIDs": [f"{CLAUDE_PERSONAL}.session", f"{CODEX}.weekly"],
            "providerOrder": ["claude", "codex", "cursor", "openrouter", "copilot"],
            "metricOrderByProvider": {},
            "unifiedProviderKinds": ["claude"],
        },
        settings=dict(SETTINGS, privacyMode=privacy_mode),
        burning_tracker=BurningFastTracker(),
        now=NOW,
    )


def build_fixture() -> Dict[str, Any]:
    return {
        "now": format_iso8601(NOW),
        "settings": SETTINGS,
        "dashboard": _build(privacy_mode=False),
        "privacyDashboard": _build(privacy_mode=True),
    }


def render_fixture() -> str:
    return json.dumps(build_fixture(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    FIXTURE_PATH.write_text(render_fixture(), encoding="utf-8")
    print(f"wrote {FIXTURE_PATH}")
