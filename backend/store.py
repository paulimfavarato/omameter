"""Persistent stores for Accounts, CLIProxyAPI Hubs, Layout (with 40-step undo), Snapshots, and Settings."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.models import Account, CLIProxyHub, ProviderSnapshot, UNIFIABLE_KINDS
from backend.settings import DEFAULT_SETTINGS, apply_patch, normalize_settings


CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "omameter"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "omameter"


def _atomic_write_json(path: Path, data: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, mode)
    except OSError:
        pass
    tmp.replace(path)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return copy.deepcopy(default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return copy.deepcopy(default)


DEFAULT_LAYOUT: Dict[str, Any] = {
    "hiddenMetricIDs": [],
    "priorityPinIDs": [],
    "providerOrder": [],
    "metricOrderByProvider": {},
    "unifiedProviderKinds": [],
}


class OmameterStore:
    """Unified persistence layer for accounts, hubs, layout (with undo), settings, and snapshot cache."""

    MAX_UNDO_DEPTH = 40

    def __init__(self, config_dir: Path = CONFIG_DIR, cache_dir: Path = CACHE_DIR) -> None:
        self.config_dir = config_dir
        self.cache_dir = cache_dir
        self.accounts_path = config_dir / "accounts.json"
        self.layout_path = config_dir / "layout.json"
        self.settings_path = config_dir / "settings.json"
        self.snapshots_path = cache_dir / "snapshots.json"

        self.accounts: List[Account] = []
        self.hubs: List[CLIProxyHub] = []
        self.layout: Dict[str, Any] = copy.deepcopy(DEFAULT_LAYOUT)
        self.settings: Dict[str, Any] = copy.deepcopy(DEFAULT_SETTINGS)
        self.snapshots: Dict[str, ProviderSnapshot] = {}
        self._undo_stack: List[Dict[str, Any]] = []

        self.load()

    def load(self) -> None:
        raw_acc = _read_json(self.accounts_path, {"accounts": [], "hubs": []})
        if isinstance(raw_acc, dict):
            self.accounts = [Account.from_dict(a) for a in (raw_acc.get("accounts") or []) if isinstance(a, dict)]
            self.hubs = [CLIProxyHub.from_dict(h) for h in (raw_acc.get("hubs") or []) if isinstance(h, dict)]

        raw_layout = _read_json(self.layout_path, DEFAULT_LAYOUT)
        if isinstance(raw_layout, dict):
            merged = copy.deepcopy(DEFAULT_LAYOUT)
            merged.update(raw_layout)
            self.layout = merged

        raw_settings = _read_json(self.settings_path, DEFAULT_SETTINGS)
        if isinstance(raw_settings, dict):
            self.settings = normalize_settings(raw_settings)

        raw_snaps = _read_json(self.snapshots_path, {})
        if isinstance(raw_snaps, dict):
            self.snapshots = {}
            for acc_id, sdata in raw_snaps.items():
                if isinstance(sdata, dict):
                    try:
                        self.snapshots[acc_id] = ProviderSnapshot.from_dict(sdata)
                    except Exception:
                        continue

    def save_accounts(self) -> None:
        _atomic_write_json(
            self.accounts_path,
            {
                "accounts": [a.to_dict() for a in self.accounts],
                "hubs": [h.to_dict() for h in self.hubs],
            },
        )

    def save_layout(self) -> None:
        _atomic_write_json(self.layout_path, self.layout)

    def save_settings(self) -> None:
        _atomic_write_json(self.settings_path, self.settings)

    def save_snapshots(self) -> None:
        _atomic_write_json(
            self.snapshots_path,
            {acc_id: snap.to_dict() for acc_id, snap in self.snapshots.items()},
            mode=0o644,
        )

    # -----------------------------------------------------------------------
    # Account CRUD
    # -----------------------------------------------------------------------

    def get_account(self, account_id: str) -> Optional[Account]:
        for acc in self.accounts:
            if acc.id == account_id or acc.card_id == account_id:
                return acc
        return None

    def find_duplicate_account(self, kind: str, identity: Optional[str], organization: Optional[str] = None, domain: Optional[str] = None) -> Optional[Account]:
        if not identity:
            return None
        for acc in self.accounts:
            if acc.is_hub_account:
                continue
            if acc.kind == kind and acc.identity == identity and acc.organization == organization and acc.domain == domain:
                return acc
        return None

    def add_account(self, account: Account) -> Account:
        self.accounts.append(account)
        if account.kind not in self.layout["providerOrder"]:
            self.layout["providerOrder"].append(account.kind)
        self._undo_stack.clear()
        self.save_accounts()
        self.save_layout()
        return account

    def update_account(self, account_id: str, **fields: Any) -> Optional[Account]:
        """Set fields on one account and save; None when it does not exist."""
        acc = self.get_account(account_id)
        if acc is None:
            return None
        for name, value in fields.items():
            if not hasattr(acc, name):
                raise AttributeError(f"Account has no field {name!r}")
            setattr(acc, name, value)
        self.save_accounts()
        return acc

    def put_snapshot(self, account_id: str, snapshot: ProviderSnapshot) -> None:
        """Store one account's latest snapshot and save the snapshot cache."""
        self.snapshots[account_id] = snapshot
        self.save_snapshots()

    def rename_account(self, account_id: str, new_label: str) -> bool:
        acc = self.get_account(account_id)
        if not acc:
            return False
        cleaned = new_label.strip()
        if not cleaned:
            return False
        acc.label = cleaned
        self.save_accounts()
        return True

    def remove_account(self, account_id: str) -> Optional[Account]:
        acc = self.get_account(account_id)
        if not acc:
            return None
        self.accounts = [a for a in self.accounts if a.id != acc.id]
        self.snapshots.pop(acc.id, None)
        # Clean up layout references
        prefix = f"{acc.id}."
        self.layout["hiddenMetricIDs"] = [m for m in self.layout["hiddenMetricIDs"] if not m.startswith(prefix)]
        self.layout["priorityPinIDs"] = [m for m in self.layout["priorityPinIDs"] if not m.startswith(prefix)]
        self.layout["metricOrderByProvider"].pop(acc.id, None)
        if not any(a.kind == acc.kind for a in self.accounts):
            self.layout["providerOrder"] = [k for k in self.layout["providerOrder"] if k != acc.kind]
            self.layout["unifiedProviderKinds"] = [k for k in self.layout["unifiedProviderKinds"] if k != acc.kind]
        self._undo_stack.clear()
        self.save_accounts()
        self.save_layout()
        self.save_snapshots()
        return acc

    # -----------------------------------------------------------------------
    # Hub CRUD
    # -----------------------------------------------------------------------

    def get_hub(self, hub_id: str) -> Optional[CLIProxyHub]:
        for hub in self.hubs:
            if hub.id == hub_id:
                return hub
        return None

    def add_hub(self, hub: CLIProxyHub) -> CLIProxyHub:
        self.hubs.append(hub)
        self.save_accounts()
        return hub

    def remove_hub(self, hub_id: str) -> Tuple[Optional[CLIProxyHub], List[Account]]:
        hub = self.get_hub(hub_id)
        if not hub:
            return None, []
        removed_accounts = [a for a in self.accounts if a.hub_id == hub_id]
        for acc in removed_accounts:
            self.remove_account(acc.id)
        self.hubs = [h for h in self.hubs if h.id != hub_id]
        self.save_accounts()
        return hub, removed_accounts

    # -----------------------------------------------------------------------
    # Layout mutations (with 40-step undo)
    # -----------------------------------------------------------------------

    def _push_undo(self) -> None:
        self._undo_stack.append(copy.deepcopy(self.layout))
        if len(self._undo_stack) > self.MAX_UNDO_DEPTH:
            self._undo_stack.pop(0)

    def can_undo(self) -> bool:
        return len(self._undo_stack) > 0

    def undo_layout(self) -> bool:
        if not self._undo_stack:
            return False
        self.layout = self._undo_stack.pop()
        self.save_layout()
        return True

    def set_metric_hidden(self, metric_ids: List[str], hidden: bool) -> None:
        self._push_undo()
        hidden_set = set(self.layout["hiddenMetricIDs"])
        pin_order = list(self.layout["priorityPinIDs"])
        for mid in metric_ids:
            if hidden:
                hidden_set.add(mid)
                pin_order = [m for m in pin_order if m != mid]  # Invariant: hiding unpins
            else:
                hidden_set.discard(mid)
        self.layout["hiddenMetricIDs"] = sorted(hidden_set)
        self.layout["priorityPinIDs"] = pin_order
        self.save_layout()

    def set_metric_pinned(self, metric_ids: List[str], pinned: bool) -> None:
        self._push_undo()
        hidden_set = set(self.layout["hiddenMetricIDs"])
        pin_order = list(self.layout["priorityPinIDs"])
        pin_set = set(pin_order)
        for mid in metric_ids:
            if pinned:
                if mid not in hidden_set and mid not in pin_set:
                    pin_order.append(mid)
                    pin_set.add(mid)
            else:
                if mid in pin_set:
                    pin_order = [m for m in pin_order if m != mid]
                    pin_set.discard(mid)
        self.layout["priorityPinIDs"] = pin_order
        self.save_layout()

    def set_provider_unified(self, kind: str, unified: bool) -> None:
        if kind not in UNIFIABLE_KINDS:
            return
        self._push_undo()
        u_set = set(self.layout["unifiedProviderKinds"])
        if unified:
            u_set.add(kind)
        else:
            u_set.discard(kind)
        self.layout["unifiedProviderKinds"] = sorted(u_set)
        self.save_layout()

    def move_provider(self, kind: str, direction: int) -> None:
        order = [k for k in self.layout["providerOrder"] if any(a.kind == k for a in self.accounts)]
        for a in self.accounts:
            if a.kind not in order:
                order.append(a.kind)
        if kind not in order:
            return
        idx = order.index(kind)
        target = idx + (-1 if direction < 0 else 1)
        if target < 0 or target >= len(order):
            return
        self._push_undo()
        order[idx], order[target] = order[target], order[idx]
        self.layout["providerOrder"] = order
        self.save_layout()

    def move_metric(self, account_id: str, key: str, direction: int) -> None:
        snap = self.snapshots.get(account_id)
        if not snap:
            return
        keys = [l.key for l in snap.lines]
        saved = self.layout["metricOrderByProvider"].get(account_id) or []
        order = [k for k in saved if k in keys] + [k for k in keys if k not in saved]
        if key not in order:
            return
        idx = order.index(key)
        target = idx + (-1 if direction < 0 else 1)
        if target < 0 or target >= len(order):
            return
        self._push_undo()
        order[idx], order[target] = order[target], order[idx]
        self.layout["metricOrderByProvider"][account_id] = order
        self.save_layout()

    def update_settings(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Applies a patch (linked thresholds included) and persists; see backend.settings.apply_patch."""
        self.settings = apply_patch(self.settings, patch or {})
        self.save_settings()
        return dict(self.settings)
