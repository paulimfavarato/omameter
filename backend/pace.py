"""Exact Pace, BurningFast (90% hysteresis), UseItOrLoseIt, and QuotaNotificationEvaluator logic."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional, Set, Tuple

from backend.i18n import metric_title, tr
from backend.models import (
    MetricLine,
    MetricPeriod,
    PriorityLimits,
    format_duration_seconds,
    parse_iso8601,
)


USE_IT_OR_LOSE_IT_LEAD_SEC = 24 * 3600  # 86_400s
RESET_WINDOW_JITTER_TOLERANCE_SEC = 1.0


@dataclass(frozen=True)
class PaceResult:
    status: str  # "ahead" | "on_track" | "behind"
    projected_fraction: float  # projectedUsage / limit
    elapsed_share: float


def elapsed_share(resets_at: Optional[datetime], period_ms: Optional[int], now: datetime) -> Optional[float]:
    if resets_at is None or period_ms is None or period_ms <= 0:
        return None
    period = period_ms / 1000.0
    if now >= resets_at:
        return None
    window_start = resets_at.timestamp() - period
    elapsed = now.timestamp() - window_start
    min_elapsed = max(60.0, period * 0.01)
    if elapsed < min_elapsed:
        return None
    share = elapsed / period
    if share <= 0.0 or share >= 1.0:
        return None
    return share


def is_fresh_session_window(line: MetricLine, now: datetime) -> bool:
    if not line.session_start_signal or not line.is_bounded or line.used > 0:
        return False
    resets = parse_iso8601(line.resets_at)
    if line.session_start_signal == "zero_usage":
        return resets is not None and now < resets
    if line.session_start_signal == "missing_reset_date":
        return resets is None
    return False


def evaluate_pace(line: MetricLine, now: datetime) -> Optional[PaceResult]:
    if is_fresh_session_window(line, now):
        return None
    if not line.is_bounded or not line.limit or line.limit <= 0 or line.used <= 0:
        return None
    resets = parse_iso8601(line.resets_at)
    share = elapsed_share(resets, line.period_duration_ms, now)
    if share is None:
        return None
    projected_usage = line.used / share
    projected_fraction = projected_usage / line.limit
    if line.used >= line.limit:
        status = "behind"
    elif projected_usage <= line.limit * 0.9:
        status = "ahead"
    elif projected_usage <= line.limit:
        status = "on_track"
    else:
        status = "behind"
    # 5% usage floor for non-ahead statuses
    if status != "ahead" and (line.used / line.limit) < 0.05:
        return None
    return PaceResult(status=status, projected_fraction=projected_fraction, elapsed_share=share)


def meter_state(line: MetricLine, now: datetime) -> str:
    """Returns: 'no_data' | 'unpaced' | 'spent' | 'healthy' | 'close_to_limit' | 'running_out'."""
    if not line.is_bounded or not line.limit or line.limit <= 0:
        return "unpaced"
    if line.is_spent:
        return "spent"
    pace = evaluate_pace(line, now)
    if pace is None:
        return "unpaced"
    if pace.status == "ahead":
        return "healthy"
    if pace.status == "on_track":
        spare = int(round((1.0 - pace.projected_fraction) * 100.0))
        return "close_to_limit" if spare >= 1 else "running_out"
    return "running_out"


def pace_mark(line: MetricLine, now: datetime, display_mode: str = "remaining", shows_pace_marks: bool = True) -> Optional[float]:
    if not shows_pace_marks or not line.is_bounded or line.is_spent:
        return None
    if is_fresh_session_window(line, now):
        return None
    resets = parse_iso8601(line.resets_at)
    share = elapsed_share(resets, line.period_duration_ms, now)
    if share is None:
        return None
    return (1.0 - share) if display_mode == "remaining" else share


def runway_seconds(line: MetricLine, now: datetime) -> Optional[float]:
    pace = evaluate_pace(line, now)
    if pace is None or not line.period_duration_ms or line.period_duration_ms <= 0 or not line.limit:
        return None
    if pace.projected_fraction <= 0:
        return None
    period = line.period_duration_ms / 1000.0
    rem_frac = max(0.0, 1.0 - (line.used / line.limit))
    return rem_frac * period / pace.projected_fraction


def pace_tooltip(line: MetricLine, now: datetime) -> Optional[str]:
    st = meter_state(line, now)
    if st == "spent":
        return tr("pace.limitReached", "Limit reached")
    pace = evaluate_pace(line, now)
    if pace is None:
        return None
    if pace.status == "ahead":
        left_pct = int(round((1.0 - pace.projected_fraction) * 100.0))
        return tr("pace.leftAtReset", "~%1% left at reset", max(0, left_pct))
    if pace.status == "on_track":
        used_pct = int(round(pace.projected_fraction * 100.0))
        return tr("pace.usedAtReset", "~%1% used at reset", min(100, used_pct))
    over_pct = max(1, int(round((pace.projected_fraction - 1.0) * 100.0)))
    return tr("pace.overAtReset", "~%1% over limit at reset", over_pct)


def reset_window_advanced(
    resets_at: Optional[datetime],
    previous_reset: Optional[datetime],
    now: datetime,
) -> bool:
    if resets_at is None:
        return False
    if previous_reset is None:
        return True
    return previous_reset <= now and (resets_at - previous_reset).total_seconds() > RESET_WINDOW_JITTER_TOLERANCE_SEC


def is_unused_near_reset(line: MetricLine, now: datetime) -> bool:
    """Use It or Lose It: multi-day non-dollar bounded limit resetting within 24h with > 50% left."""
    if not line.is_bounded:
        return False
    if line.kind == "dollars":
        return False
    if not line.period_duration_ms or line.period_duration_ms <= MetricPeriod.DAY_MS:
        return False
    resets = parse_iso8601(line.resets_at)
    if resets is None:
        return False
    remaining_sec = (resets - now).total_seconds()
    if remaining_sec <= 0 or remaining_sec > USE_IT_OR_LOSE_IT_LEAD_SEC:
        return False
    pct_left = int(round(line.remaining_fraction * 100.0))
    return pct_left > 50


# ---------------------------------------------------------------------------
# BurningFast Hysteresis (90% projected usage recovery)
# ---------------------------------------------------------------------------

class BurningFastTracker:
    """Tracks latched burning-fast metric IDs per reset window."""

    def __init__(self) -> None:
        self._latched: Dict[str, Optional[datetime]] = {}

    def was_burning(self, metric_id: str, line: MetricLine, now: datetime) -> bool:
        if metric_id not in self._latched:
            return False
        prev_reset = self._latched[metric_id]
        resets = parse_iso8601(line.resets_at)
        if reset_window_advanced(resets, prev_reset, now):
            return False
        return True

    def is_burning(self, metric_id: str, line: MetricLine, now: datetime) -> bool:
        st = meter_state(line, now)
        if st == "running_out":
            return True
        if st == "close_to_limit":
            return self.was_burning(metric_id, line, now)
        return False

    def forget_account(self, account_id: str) -> None:
        """Drop every latch of one account (metric ids "<accountId>.<lineKey>")."""
        prefix = f"{account_id}."
        for mid in [m for m in self._latched if m.startswith(prefix)]:
            del self._latched[mid]

    def latch(self, metric_id: str, line: MetricLine, now: datetime) -> bool:
        burning = self.is_burning(metric_id, line, now)
        if burning:
            self._latched[metric_id] = parse_iso8601(line.resets_at)
        else:
            self._latched.pop(metric_id, None)
        return burning


# ---------------------------------------------------------------------------
# QuotaNotificationEvaluator
# ---------------------------------------------------------------------------

BUCKET_ORDER = {"untracked": -1, "healthy": 0, "close": 1, "running_out": 2}


def _pace_bucket(st: str) -> str:
    if st in ("no_data", "unpaced"):
        return "untracked"
    if st == "healthy":
        return "healthy"
    if st == "close_to_limit":
        return "close"
    return "running_out"


@dataclass
class MetricNotificationState:
    primed: bool = False
    resets_at: Optional[datetime] = None
    previous_severity: str = "normal"
    previous_bucket: str = "untracked"
    fired_milestones: Set[str] = field(default_factory=set)


def milestone_title_and_body(milestone: str, limits: PriorityLimits) -> Tuple[str, str]:
    if milestone == "critical":
        return (
            tr("notify.critical", "Critical"),
            tr("notify.thresholdBody", "%1% or less left for this window.", limits.critical),
        )
    if milestone == "warning":
        return (
            tr("notify.warning", "Warning"),
            tr("notify.thresholdBody", "%1% or less left for this window.", limits.warning),
        )
    if milestone == "burningFast":
        return tr("notify.burningFast", "Burning Fast"), tr("notify.burningFastBody", "May run out before the limit resets.")
    return tr("notify.useItOrLoseIt", "Use It or Lose It"), tr("notify.useItOrLoseItBody", "Over 50% left, resets in under 24 hours.")


def send_desktop_notification(title: str, subtitle: str, body: str, *, urgency: str = "normal") -> bool:
    if shutil.which("notify-send") is None:
        return False
    try:
        proc = subprocess.run(
            [
                "notify-send",
                "-a",
                "Omameter",
                "-u",
                urgency,
                f"{title} — {subtitle}",
                body,
            ],
            capture_output=True,
            timeout=3.0,
            check=False,
        )
        return proc.returncode == 0
    except Exception:
        return False


class QuotaNotificationEvaluator:
    """State machine for QuotaNotificationEvaluator & PaceNotificationLogic."""

    CANDIDATE_ORDER = ("burningFast", "critical", "warning", "useItOrLoseIt")

    def __init__(
        self,
        post_fn: Optional[Callable[[str, str, str, str], bool]] = None,
    ) -> None:
        self._states: Dict[str, MetricNotificationState] = {}
        self._fired_reset_expiries: Set[str] = set()
        self._post_fn = post_fn or (lambda title, sub, body, urg: send_desktop_notification(title, sub, body, urgency=urg))

    def forget_account(self, account_id: str) -> None:
        """Drop one account's milestone states ("<accountId>.<lineKey>") and
        fired reset-expiry keys ("<accountId>:<itemId>")."""
        metric_prefix = f"{account_id}."
        reset_prefix = f"{account_id}:"
        for mid in [m for m in self._states if m.startswith(metric_prefix)]:
            del self._states[mid]
        self._fired_reset_expiries = {k for k in self._fired_reset_expiries if not k.startswith(reset_prefix)}

    def evaluate_reset_expiries(
        self,
        *,
        account_id: str,
        provider_name: str,
        line: MetricLine,
        now: datetime,
        enabled: bool,
    ) -> List[str]:
        """Fire one-shot notifications for reset items expiring within 48 hours."""
        if not enabled or not line.reset_items:
            return []
        fired: List[str] = []
        for item in line.reset_items:
            exp = parse_iso8601(item.expires_at)
            if exp is None:
                continue
            remaining_sec = (exp - now).total_seconds()
            if remaining_sec <= 0 or remaining_sec > 48 * 3600:
                continue
            key = f"{account_id}:{item.id}"
            if key in self._fired_reset_expiries:
                continue
            body = tr("notify.resetExpiringBody", "%1 expires in %2.", item.title, format_duration_seconds(remaining_sec))
            if self._post_fn(tr("notify.resetExpiring", "Limit Reset Expiring"), provider_name, body, "normal"):
                self._fired_reset_expiries.add(key)
                fired.append(item.id)
        return fired

    def evaluate(
        self,
        metric_id: str,
        provider_name: str,
        line: MetricLine,
        now: datetime,
        limits: PriorityLimits,
        enabled_milestones: Dict[str, bool],
    ) -> List[str]:
        if not line.is_bounded:
            return []

        prev = self._states.get(metric_id, MetricNotificationState())
        resets = parse_iso8601(line.resets_at)
        st = meter_state(line, now)
        sev = line.severity(limits)
        bucket = _pace_bucket(st)
        unused = is_unused_near_reset(line, now)

        # Carry over / detect reset window advancement
        if reset_window_advanced(resets, prev.resets_at, now):
            next_state = MetricNotificationState(
                primed=prev.primed,
                resets_at=resets,
                previous_severity="normal",
                previous_bucket="untracked",
                fired_milestones=set(),
            )
        else:
            next_state = MetricNotificationState(
                primed=prev.primed,
                resets_at=resets or prev.resets_at,
                previous_severity=prev.previous_severity,
                previous_bucket=prev.previous_bucket,
                fired_milestones=set(prev.fired_milestones),
            )

        if st == "no_data":
            self._states[metric_id] = next_state
            return []

        if not next_state.primed:
            next_state.primed = True
            next_state.previous_bucket = bucket
            next_state.previous_severity = sev
            next_state.fired_milestones = {"useItOrLoseIt"} if unused else set()
            self._states[metric_id] = next_state
            return []

        held_bucket = next_state.previous_bucket
        held_severity = next_state.previous_severity
        fire: List[str] = []

        # 1. burningFast
        if bucket != "untracked":
            if BUCKET_ORDER[bucket] >= BUCKET_ORDER["running_out"] and BUCKET_ORDER[next_state.previous_bucket] < BUCKET_ORDER["running_out"]:
                if "burningFast" not in next_state.fired_milestones:
                    fire.append("burningFast")
            if bucket == "healthy":
                next_state.fired_milestones.discard("burningFast")
            next_state.previous_bucket = bucket

        # 2. critical & warning
        if sev == "critical" and next_state.previous_severity != "critical":
            if "critical" not in next_state.fired_milestones:
                fire.append("critical")
        if sev != "normal" and next_state.previous_severity == "normal":
            if "warning" not in next_state.fired_milestones:
                fire.append("warning")

        if sev != "critical":
            next_state.fired_milestones.discard("critical")
        if sev == "normal":
            next_state.fired_milestones.discard("warning")
        next_state.previous_severity = sev

        # 3. useItOrLoseIt (never re-arms within same window)
        if unused and "useItOrLoseIt" not in next_state.fired_milestones:
            fire.append("useItOrLoseIt")

        delivered: List[str] = []
        pace_delivered = False
        level_delivered = False

        for m in self.CANDIDATE_ORDER:
            if m not in fire:
                continue
            if m == "warning" and level_delivered:
                # Multi-level drop suppression: critical already delivered
                next_state.fired_milestones.add("warning")
                continue
            if not enabled_milestones.get(m, False):
                continue
            title, body = milestone_title_and_body(m, limits)
            subtitle = f"{provider_name} {metric_title(line.title)}"
            urgency = "critical" if m == "critical" else "normal"
            if self._post_fn(title, subtitle, body, urgency):
                next_state.fired_milestones.add(m)
                delivered.append(m)
                if m == "burningFast":
                    pace_delivered = True
                if m in ("critical", "warning"):
                    level_delivered = True

        # Undelivered state rollback so toggling a switch on later doesn't miss edge
        if "burningFast" in fire and not pace_delivered:
            next_state.previous_bucket = held_bucket
        if ("critical" in fire or "warning" in fire) and not level_delivered:
            next_state.previous_severity = held_severity

        self._states[metric_id] = next_state
        return delivered
