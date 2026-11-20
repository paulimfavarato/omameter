"""Exact RefreshBackoff schedule (5m -> 10m -> 20m -> 40m -> 60m)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Optional


REFRESH_INTERVAL_SEC = 300        # 5 minutes
STALENESS_THRESHOLD_SEC = 600     # 10 minutes
BACKOFF_BASE_SEC = 300            # 5 minutes
BACKOFF_CAP_SEC = 3600            # 60 minutes
OFFLINE_RETRY_DELAYS = (2, 4, 8, 16, 32)
SECONDARY_READ_INTERVAL_SEC = 3600
FORCED_RESET_GRANTS_MIN_AGE_SEC = 900
# Slack under the refresh interval before a non-forced refresh counts as "too recent":
# covers the 30 s background tick plus up to 62 s of offline retries, so a fetch that
# started late in one cycle is not skipped by the next one.
REFRESH_GRACE_SEC = 90


def backoff_delay_seconds(failures: int) -> int:
    exp = min(max(failures - 1, 0), 16)
    return min(BACKOFF_BASE_SEC * (2 ** exp), BACKOFF_CAP_SEC)


@dataclass
class BackoffEntry:
    failures: int
    retry_at: datetime
    provider_asked: bool
    reason: str


class RefreshBackoff:
    def __init__(self) -> None:
        self._entries: Dict[str, BackoffEntry] = {}

    def record_failure(
        self,
        account_id: str,
        *,
        kind: str,
        reason: str,
        now: datetime,
        retry_after: Optional[float] = None,
    ) -> BackoffEntry:
        prev = self._entries.get(account_id)
        prev_failures = prev.failures if prev else 0

        if kind in ("offline", "unreachable"):
            failures = prev_failures
            delay = BACKOFF_BASE_SEC
            provider_asked = False
        elif kind == "rate_limited":
            failures = prev_failures + 1
            delay = int(retry_after) if retry_after is not None else backoff_delay_seconds(failures)
            provider_asked = True
        else:
            failures = prev_failures + 1
            delay = backoff_delay_seconds(failures)
            provider_asked = False

        entry = BackoffEntry(
            failures=failures,
            retry_at=now + timedelta(seconds=max(1, delay)),
            provider_asked=provider_asked,
            reason=reason,
        )
        self._entries[account_id] = entry
        return entry

    def record_success(self, account_id: str) -> None:
        self._entries.pop(account_id, None)

    def holds(self, account_id: str, now: datetime, *, forced: bool = False) -> bool:
        entry = self._entries.get(account_id)
        if entry is None:
            return False
        return now < entry.retry_at and (entry.provider_asked or not forced)
