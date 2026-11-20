"""Account lifecycle: every way an account arrives, gets a new credential, or goes.

An account's state lives in many places keyed by its id: the store (accounts,
snapshots, layout's `hiddenMetricIDs` / `priorityPinIDs` /
`metricOrderByProvider`), the credential vault, the refresher's error map and
backoff, the per-account secondary-read cache, the burning-fast latches and the
quota-notification state. `AccountLifecycle` is the one place that adds to and
clears all of them, so no caller has to remember the list.

Arrivals go through `admit(...)`. The duplicate rule differs per caller on
purpose (an OAuth login updates the matching account, a repeated API key is
rejected), so each caller states its rule explicitly with an `AdmissionRule`.

The lifecycle never emits toasts or state and takes no lock: the daemon calls it
under its own lock and reports the outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from backend.cliproxy import sync_hub as reconcile_hub
from backend.models import Account, CLIProxyHub, ProviderSnapshot


_MATCHABLE_FIELDS = ("organization", "domain")


@dataclass(frozen=True)
class AdmissionRule:
    """How `admit` treats an existing account.

    `match`: arrival fields that, with kind and identity, identify a duplicate.
    Fields left out compare against None, exactly like
    `OmameterStore.find_duplicate_account` with that argument omitted. Hub
    mirrors and arrivals without an identity never match.

    `on_duplicate`: "update" stores the new credential on the duplicate,
    "reject" leaves everything untouched.

    `on_missing_target`: when `target_id` names no account, "skip" does nothing
    (outcome "missing"), "admit" falls back to the duplicate rule / creation.
    """

    match: Tuple[str, ...] = ()
    on_duplicate: str = "update"
    on_missing_target: str = "skip"

    def __post_init__(self) -> None:
        unknown = [f for f in self.match if f not in _MATCHABLE_FIELDS]
        if unknown:
            raise ValueError(f"AdmissionRule cannot match on {unknown}")
        if self.on_duplicate not in ("update", "reject"):
            raise ValueError(f"on_duplicate must be 'update' or 'reject', not {self.on_duplicate!r}")
        if self.on_missing_target not in ("skip", "admit"):
            raise ValueError(f"on_missing_target must be 'skip' or 'admit', not {self.on_missing_target!r}")


# The rules each arrival path uses today.
OAUTH_LOGIN = AdmissionRule(match=("organization", "domain"))
LOCAL_IMPORT = AdmissionRule()
API_KEY = AdmissionRule(match=("organization",), on_duplicate="reject", on_missing_target="admit")


@dataclass
class Admission:
    outcome: str  # "created" | "updated" | "duplicate_updated" | "rejected" | "missing"
    account: Optional[Account] = None

    @property
    def admitted(self) -> bool:
        return self.outcome in ("created", "updated", "duplicate_updated")


class AccountLifecycle:
    def __init__(self, *, store: Any, vault: Any, refresher: Any, burning_tracker: Any, notifier: Any) -> None:
        self.store = store
        self.vault = vault
        self.refresher = refresher
        self.burning_tracker = burning_tracker
        self.notifier = notifier

    # -- arrivals ------------------------------------------------------------

    def admit(
        self,
        kind: str,
        credential: Dict[str, Any],
        *,
        label: str,
        rule: AdmissionRule,
        identity: Optional[str] = None,
        organization: Optional[str] = None,
        domain: Optional[str] = None,
        auth_mode: Optional[str] = None,
        target_id: Optional[str] = None,
        snapshot: Optional[ProviderSnapshot] = None,
    ) -> Admission:
        """Store a credential that just arrived, on the right account.

        - `target_id` given and found: the credential replaces that account's,
          `identity` / `domain` overwrite its own when given ("updated").
        - otherwise a duplicate per `rule` gets the credential
          ("duplicate_updated") or blocks the arrival ("rejected");
        - otherwise a new account is created ("created").

        Updated accounts lose their error and backoff. `snapshot`, when given,
        is re-stamped for the admitted account and stored.
        """
        if target_id:
            target = self.store.get_account(target_id)
            if target is not None:
                if target.kind != kind or target.is_hub_account:
                    return Admission("missing")
                self.vault.set_account_credential(target.id, credential)
                fields: Dict[str, Any] = {}
                if domain:
                    fields["domain"] = domain
                if identity:
                    fields["identity"] = identity
                self.store.update_account(target.id, **fields)
                self._put_snapshot(target, snapshot)
                self.refresher.mark_healthy(target.id)
                return Admission("updated", target)
            if rule.on_missing_target == "skip":
                return Admission("missing")

        arrival = {"organization": organization, "domain": domain}
        dup = self.store.find_duplicate_account(
            kind,
            identity,
            organization=arrival["organization"] if "organization" in rule.match else None,
            domain=arrival["domain"] if "domain" in rule.match else None,
        )
        if dup is not None:
            if rule.on_duplicate == "reject":
                return Admission("rejected", dup)
            self.vault.set_account_credential(dup.id, credential)
            self._put_snapshot(dup, snapshot)
            self.refresher.mark_healthy(dup.id)
            return Admission("duplicate_updated", dup)

        acc = Account.create(
            kind=kind,
            label=label,
            organization=organization,
            domain=domain,
            identity=identity,
            auth_mode=auth_mode,
        )
        self.store.add_account(acc)
        self.vault.set_account_credential(acc.id, credential)
        self._put_snapshot(acc, snapshot)
        return Admission("created", acc)

    def _put_snapshot(self, account: Account, snapshot: Optional[ProviderSnapshot]) -> None:
        if snapshot is None:
            return
        snapshot.account_id = account.id
        snapshot.card_id = account.card_id
        self.store.put_snapshot(account.id, snapshot)

    # -- removal -------------------------------------------------------------

    def remove(self, account_id: str) -> Optional[Account]:
        """The only way an account goes: clears everything keyed by its id."""
        acc = self.store.remove_account(account_id)  # account, snapshot, layout references
        if acc is None:
            return None
        self.vault.delete_account_credential(acc.id)
        self.refresher.mark_healthy(acc.id)  # error + backoff
        self.refresher.secondary_reads.forget(acc.id)
        self.burning_tracker.forget_account(acc.id)
        self.notifier.forget_account(acc.id)
        return acc

    def remove_hub(self, hub_id: str) -> Optional[CLIProxyHub]:
        """Remove a hub, each of its mirrored accounts (via `remove`) and its key."""
        hub = self.store.get_hub(hub_id)
        if hub is None:
            return None
        for acc in [a for a in self.store.accounts if a.hub_id == hub.id]:
            self.remove(acc.id)
        self.store.remove_hub(hub.id)
        self.vault.delete_hub_key(hub.id)
        return hub

    def sync_hub(self, hub: CLIProxyHub, management_key: str, http: Any, *, gate: Any = None) -> Dict[str, Any]:
        """Reconcile a hub's mirrored accounts; vanished logins go through `remove`."""
        return reconcile_hub(
            hub,
            management_key,
            self.store,
            http,
            vault=self.vault,
            gate=gate,
            remove_account=self.remove,
        )
