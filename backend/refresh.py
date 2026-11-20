"""Single-account refresh: gate, fetch with offline retries, and result recording.

`AccountRefresher` owns the decision of whether an account should refresh now,
the network fetch (with offline retries paced by an injectable `Clock`), the
error classification, and how a result lands in the store, the backoff, the
per-account error map, and the notification/burning-fast trackers.

It has three phases so the daemon can hold its own lock around `plan` and
`record` while running `fetch` (network) without it:

    acc = refresher.plan(account_id, force=force, in_flight=ids)   # under lock
    outcome = refresher.fetch(acc, force=force)                      # no lock
    refresher.record(acc, outcome)                                   # under lock

The refresher never writes to stdout, starts threads, or sleeps on its own; all
waiting goes through `Clock.wait`, which tests replace with a fake.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Set, Tuple

from backend.backoff import (
    OFFLINE_RETRY_DELAYS,
    REFRESH_GRACE_SEC,
    RefreshBackoff,
)
from backend.cliproxy import hub_account_credential
from backend.http_client import HTTPClient, HTTPTransportError, redact_secrets
from backend.i18n import tr
from backend.models import (
    Account,
    PriorityLimits,
    ProviderSnapshot,
    is_default_label,
    parse_iso8601,
    utc_now,
)
from backend.providers import fetch_provider
from backend.providers.base import FetchContext, ProviderError
from backend.resets import claim_reset
from backend.secondary import SecondaryReads
from backend.settings import normalize_settings


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------


class SystemClock:
    """Production clock: wall time plus waits that a stop event can interrupt.

    Any clock passed to the refresher or daemon needs the same three methods:
    `now()`, `wait(seconds) -> bool` (True means a stop was requested and the
    caller must abort), and `stopped() -> bool`.
    """

    def __init__(self, stop_event: Optional[threading.Event] = None) -> None:
        self.stop_event = stop_event or threading.Event()

    def now(self) -> datetime:
        return utc_now()

    def wait(self, seconds: float) -> bool:
        return self.stop_event.wait(seconds)

    def stopped(self) -> bool:
        return self.stop_event.is_set()

    def stop(self) -> None:
        self.stop_event.set()


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------


def refresh_interval_sec(settings: Dict[str, Any]) -> int:
    return int(normalize_settings(settings)["refreshIntervalSec"])


def load_credential(account: Account, vault: Any) -> Dict[str, Any]:
    """Credential to fetch with: hub accounts fall back to the hub placeholder."""
    stored = vault.get_account_credential(account.id)
    if account.is_hub_account:
        return stored if isinstance(stored, dict) else hub_account_credential()
    return stored or {}


def classify_error(err: BaseException) -> Tuple[str, str, Optional[float]]:
    """Map a fetch failure to `(kind, redacted message, retry_after)`."""
    if isinstance(err, ProviderError):
        return err.kind, redact_secrets(str(err)), err.retry_after
    if isinstance(err, HTTPTransportError):
        return ("offline" if err.offline else "unreachable"), redact_secrets(str(err)), None
    return "refused", redact_secrets(str(err)), None


@dataclass
class RefreshOutcome:
    status: str  # "skipped" | "ok" | "failed" | "stopped"
    snapshot: Optional[ProviderSnapshot] = None
    error_kind: Optional[str] = None
    message: str = ""
    retry_after: Optional[float] = None


# ---------------------------------------------------------------------------
# AccountRefresher
# ---------------------------------------------------------------------------


class AccountRefresher:
    def __init__(
        self,
        *,
        store: Any,
        vault: Any,
        backoff: RefreshBackoff,
        burning_tracker: Any,
        notifier: Any,
        make_http: Callable[[Account], HTTPClient],
        clock: Any,
        secondary_reads: Optional[SecondaryReads] = None,
    ) -> None:
        self.store = store
        self.vault = vault
        self.backoff = backoff
        self.burning_tracker = burning_tracker
        self.notifier = notifier
        self.make_http = make_http
        self.clock = clock
        self.secondary_reads = secondary_reads if secondary_reads is not None else SecondaryReads()
        self._errors: Dict[str, Tuple[str, str]] = {}  # account id -> (kind, message)

    # -- per-account error map ---------------------------------------------

    def errors(self) -> Dict[str, str]:
        """Account id -> redacted message of its last failed refresh."""
        return {acc_id: msg for acc_id, (_, msg) in self._errors.items()}

    def error_kinds(self) -> Dict[str, str]:
        """Account id -> `ProviderError` kind of its last failed refresh."""
        return {acc_id: kind for acc_id, (kind, _) in self._errors.items()}

    def error_for(self, account_id: str) -> str:
        return self._errors.get(account_id, ("", ""))[1]

    def mark_healthy(self, account_id: str) -> None:
        self._errors.pop(account_id, None)
        self.backoff.record_success(account_id)

    # -- phase 1: gate -------------------------------------------------------

    def plan(self, account_id: str, *, force: bool, in_flight: Set[str]) -> Optional[Account]:
        acc = self.store.get_account(account_id)
        if acc is None:
            return None
        now = self.clock.now()
        if self.backoff.holds(acc.id, now, forced=force):
            return None
        if not force:
            snap = self.store.snapshots.get(acc.id)
            if snap is not None:
                ref_dt = parse_iso8601(snap.refreshed_at)
                interval = refresh_interval_sec(self.store.settings)
                grace = min(REFRESH_GRACE_SEC, interval // 2)
                min_age = max(0, interval - grace)
                if ref_dt is not None and (now - ref_dt).total_seconds() < min_age:
                    return None
        if acc.id in in_flight:
            return None
        return acc

    # -- phase 2: network ----------------------------------------------------

    def context_for(self, account: Account, *, force: bool) -> FetchContext:
        """Everything a provider fetch or a reset claim needs for one account.

        The secondary cache always comes from `secondary_reads`, so fetch and
        claim share it by construction.
        """
        def save(updated: Dict[str, Any]) -> None:
            self.vault.set_account_credential(account.id, updated)

        return FetchContext(
            account=account,
            credential=load_credential(account, self.vault),
            http=self.make_http(account),
            now=self.clock.now(),
            force=force,
            previous_snapshot=self.store.snapshots.get(account.id),
            secondary_cache=self.secondary_reads.for_account(account.id),
            save_credential=None if account.is_hub_account else save,
        )

    def fetch(self, account: Account, *, force: bool) -> RefreshOutcome:
        try:
            ctx = self.context_for(account, force=force)
            if not account.is_hub_account and not ctx.credential:
                raise ProviderError(tr("error.missingCredentials", "Missing credentials. Sign in again."), kind="auth_expired")
        except Exception as err:
            return self._failed(err)

        attempt = 0
        while True:
            if self.clock.stopped():
                return RefreshOutcome(status="stopped")
            ctx.now = self.clock.now()
            try:
                return RefreshOutcome(status="ok", snapshot=fetch_provider(ctx))
            except Exception as err:
                # Providers may refresh tokens mid-fetch; `ctx.credential` keeps the
                # newest one, so retries reuse the same context.
                kind, _, _ = classify_error(err)
                if kind == "offline" and attempt < len(OFFLINE_RETRY_DELAYS):
                    if self.clock.wait(OFFLINE_RETRY_DELAYS[attempt]):
                        return RefreshOutcome(status="stopped")
                    attempt += 1
                    continue
                return self._failed(err)

    # -- reset claims ---------------------------------------------------------

    def claim(self, account_id: str, reset_item: Any) -> Optional[Dict[str, Any]]:
        """Spend a rate-limit reset; None when the account does not exist.

        Runs with the same per-account secondary cache as `fetch`, so claim
        idempotency ids persist across retries and the post-claim invalidation
        reaches the next refresh.
        """
        acc = self.store.get_account(account_id)
        if acc is None:
            return None
        return claim_reset(self.context_for(acc, force=True), reset_item)

    @staticmethod
    def _failed(err: BaseException) -> RefreshOutcome:
        kind, msg, retry_after = classify_error(err)
        return RefreshOutcome(status="failed", error_kind=kind, message=msg, retry_after=retry_after)

    # -- phase 3: apply result ----------------------------------------------

    def record(self, account: Account, outcome: RefreshOutcome) -> None:
        now = self.clock.now()
        if self.store.get_account(account.id) is None:
            # Removed while the fetch ran: recording would leave an orphan
            # snapshot on disk and an error/backoff entry for a gone account.
            return
        if outcome.status == "ok" and outcome.snapshot is not None:
            snapshot = outcome.snapshot
            self.store.put_snapshot(account.id, snapshot)
            self.mark_healthy(account.id)
            if snapshot.account_label_discovered and is_default_label(account.kind, account.label):
                self.store.update_account(account.id, label=snapshot.account_label_discovered)
            self._evaluate_notifications_and_burning(account, snapshot, now)
        elif outcome.status == "failed":
            kind = outcome.error_kind or "refused"
            self._errors[account.id] = (kind, outcome.message)
            self.backoff.record_failure(
                account.id,
                kind=kind,
                reason=outcome.message,
                now=now,
                retry_after=outcome.retry_after,
            )

    def _evaluate_notifications_and_burning(self, acc: Account, snapshot: ProviderSnapshot, now: datetime) -> None:
        settings = normalize_settings(self.store.settings)
        limits = PriorityLimits.from_settings(settings)
        enabled = {
            "warning": settings["notifyWarning"],
            "critical": settings["notifyCritical"],
            "burningFast": settings["notifyBurningFast"],
            "useItOrLoseIt": settings["notifyUseItOrLoseIt"],
        }
        reset_expiry_enabled = settings["notifyUseItOrLoseIt"] or settings["notifyWarning"]
        hidden_set = set(self.store.layout.get("hiddenMetricIDs") or [])
        for ln in snapshot.lines:
            mid = f"{acc.id}.{ln.key}"
            if mid in hidden_set:
                continue
            self.burning_tracker.latch(mid, ln, now)
            self.notifier.evaluate(
                metric_id=mid,
                provider_name=acc.full_name,
                line=ln,
                now=now,
                limits=limits,
                enabled_milestones=enabled,
            )
            self.notifier.evaluate_reset_expiries(
                account_id=acc.id,
                provider_name=acc.full_name,
                line=ln,
                now=now,
                enabled=reset_expiry_enabled,
            )
