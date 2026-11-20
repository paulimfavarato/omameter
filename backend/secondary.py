"""Per-account secondary reads: low-frequency lookups cached in memory.

Providers read some data less often than usage (plan, Claude reset grants,
Codex reset credits, history ledgers...). Each account owns one
`SecondaryCache`; `SecondaryReads` hands out the same cache for the same account
id, so the refresh path and the reset-claim path always share it.

A TTL-backed entry has the shape `{"readAt": iso | None, "value": Any}` and is
handled through `is_due` / `read` / `value` / `invalidate` / `drop`. Raw state
that is not a TTL read (history ledgers, idempotency maps, cooldowns) keeps
using plain dict operations, which is why `SecondaryCache` is a `dict`.

Each key belongs to one module and is a named constant there. Nothing is
persisted to disk.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Callable, Dict

from backend.backoff import SECONDARY_READ_INTERVAL_SEC
from backend.models import format_iso8601, parse_iso8601


class SecondaryCache(dict):
    """One account's secondary-read cache (a dict with a TTL-read interface)."""

    def is_due(self, key: str, now: datetime, interval_sec: float = SECONDARY_READ_INTERVAL_SEC) -> bool:
        entry = self.get(key)
        if not isinstance(entry, dict):
            return True
        read_at = parse_iso8601(entry.get("readAt"))
        if read_at is None:
            return True
        return (now - read_at).total_seconds() >= interval_sec

    def value(self, key: str) -> Any:
        entry = self.get(key)
        return entry.get("value") if isinstance(entry, dict) else None

    def read(
        self,
        key: str,
        now: datetime,
        fetch: Callable[[], Any],
        *,
        force: bool = False,
        interval_sec: float = SECONDARY_READ_INTERVAL_SEC,
        on_failure: str = "keep",
    ) -> Any:
        """Return the cached value, re-reading through `fetch` when forced or due.

        `fetch()` returning `None` or raising counts as a failure: with
        `on_failure="keep"` the previous value stays, with `"clear"` it becomes
        `None`. Either way `readAt` is stamped, so a failing read also rests
        for `interval_sec`.
        """
        if on_failure not in ("keep", "clear"):
            raise ValueError(f"on_failure must be 'keep' or 'clear', not {on_failure!r}")
        if not force and not self.is_due(key, now, interval_sec):
            return self.value(key)
        try:
            fresh = fetch()
        except Exception:
            fresh = None
        if fresh is None:
            effective = self.value(key) if on_failure == "keep" else None
        else:
            effective = fresh
        self[key] = {"readAt": format_iso8601(now), "value": effective}
        return effective

    def invalidate(self, key: str) -> None:
        """Make the next read due regardless of any minimum age; keep the value."""
        entry = self.get(key)
        if isinstance(entry, dict):
            entry["readAt"] = None

    def drop(self, key: str) -> None:
        self.pop(key, None)


class SecondaryReads:
    """Owner of every account's `SecondaryCache`."""

    def __init__(self) -> None:
        self._caches: Dict[str, SecondaryCache] = {}
        self._lock = threading.Lock()

    def for_account(self, account_id: str) -> SecondaryCache:
        with self._lock:
            return self._caches.setdefault(account_id, SecondaryCache())

    def forget(self, account_id: str) -> None:
        with self._lock:
            self._caches.pop(account_id, None)
