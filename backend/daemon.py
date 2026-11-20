#!/usr/bin/env python3
"""Long-lived NDJSON RPC daemon for the Omameter Omarchy shell service."""

from __future__ import annotations

import concurrent.futures
import functools
import json
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, TextIO, Tuple

# Ensure plugin root is on sys.path when invoked from any CWD
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from backend.accounts import API_KEY, LOCAL_IMPORT, OAUTH_LOGIN, AccountLifecycle
from backend.auth import AuthManager, normalize_github_domain
from backend.backoff import RefreshBackoff, SECONDARY_READ_INTERVAL_SEC
from backend.cliproxy import (
    CLIProxyHubGate,
    HubTunnelHTTPClient,
    list_hub_auth_files,
    normalize_hub_address,
)
from backend.http_client import HTTPClient, ProxyConfig, redact_secrets
from backend.i18n import data_text, metric_title, set_language, tr
from backend.local_detect import (
    candidate_source_name,
    load_local_credential_payload,
    scan_local_credentials,
)
from backend.models import (
    Account,
    CLIProxyHub,
    PROVIDER_CATALOG,
    api_key_identity,
    default_account_label,
    format_iso8601,
    parse_iso8601,
    utc_now,
)
from backend.pace import BurningFastTracker, QuotaNotificationEvaluator
from backend.priority import build_dashboard_state
from backend.providers import fetch_provider
from backend.providers.base import FetchContext
from backend.refresh import AccountRefresher, SystemClock, refresh_interval_sec
from backend.store import OmameterStore
from backend.vault import CredentialVault


BACKGROUND_TICK_SEC = 30.0


def command_id(msg: Dict[str, Any]) -> Optional[str]:
    """The correlation id a command carries (non-empty string or number), else None."""
    raw = msg.get("id")
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        return None
    text = str(raw).strip()
    return text[:128] if text else None


class AfterAll:
    """Command outcome "ok once these follow-up futures finish" (e.g. refresh_all's
    per-account refreshes). Non-future items (an inline executor's return values)
    count as already done. Waits by callback, never by blocking a pool worker."""

    def __init__(self, futures: Any) -> None:
        self.futures = [f for f in (futures or []) if isinstance(f, concurrent.futures.Future)]

    def then(self, callback: Any) -> None:
        if not self.futures:
            callback()
            return
        remaining = [len(self.futures)]
        lock = threading.Lock()

        def done(_future: Any) -> None:
            with lock:
                remaining[0] -= 1
                last = remaining[0] == 0
            if last:
                callback()

        for f in self.futures:
            f.add_done_callback(done)


class OmameterDaemon:
    def __init__(
        self,
        *,
        store: Optional[OmameterStore] = None,
        vault: Optional[CredentialVault] = None,
        proxy: Optional[ProxyConfig] = None,
        notifier: Optional[QuotaNotificationEvaluator] = None,
        clock: Optional[Any] = None,
        executor: Optional[Any] = None,
        out: Optional[TextIO] = None,
    ) -> None:
        """Every collaborator is injectable; the defaults are the production ones.

        `clock` needs `now()`, `wait(seconds) -> bool` and `stopped()` (see
        `backend.refresh.SystemClock`); `executor` needs `submit(fn, *args)` and
        `shutdown(wait=False)`; `out` receives the NDJSON lines.
        """
        self.store = store if store is not None else OmameterStore()
        self.vault = vault if vault is not None else CredentialVault()
        self.backoff = RefreshBackoff()
        self.burning_tracker = BurningFastTracker()
        self.notifier = notifier if notifier is not None else QuotaNotificationEvaluator()
        self.proxy = proxy if proxy is not None else ProxyConfig.load()
        self._clock = clock if clock is not None else SystemClock()
        self._out: TextIO = out if out is not None else sys.stdout
        self._hub_gate = CLIProxyHubGate()

        self._lock = threading.RLock()
        self._stdout_lock = threading.Lock()
        self._refreshing_ids: Set[str] = set()
        self._last_hub_sync: Optional[datetime] = None
        self._last_state_emit_at: datetime = self._clock.now()
        self._next_refresh_at: datetime = self._clock.now() + timedelta(seconds=self._refresh_interval())
        self._auth_session: Optional[Dict[str, Any]] = None
        self._pool = executor if executor is not None else concurrent.futures.ThreadPoolExecutor(
            max_workers=6, thread_name_prefix="ou-fetch"
        )

        self.refresher = AccountRefresher(
            store=self.store,
            vault=self.vault,
            backoff=self.backoff,
            burning_tracker=self.burning_tracker,
            notifier=self.notifier,
            make_http=lambda acc: self._make_http_client(acc),
            clock=self._clock,
        )
        self.accounts = AccountLifecycle(
            store=self.store,
            vault=self.vault,
            refresher=self.refresher,
            burning_tracker=self.burning_tracker,
            notifier=self.notifier,
        )

        self.auth_manager = AuthManager(
            http=self._make_http_client(),
            on_update=self._on_auth_update,
            on_complete=self._on_auth_complete,
        )

    def _refresh_interval(self) -> int:
        return refresh_interval_sec(self.store.settings)

    def _make_http_client(self, account: Optional[Account] = None) -> HTTPClient:
        base_client = HTTPClient(proxy=self.proxy)
        if account is not None and account.is_hub_account and account.hub_id:
            hub = self.store.get_hub(account.hub_id)
            key_info = self.vault.get_hub_key(account.hub_id)
            if hub and key_info and key_info.get("managementKey"):
                saved_url = (key_info.get("baseUrl") or "").rstrip("/")
                if saved_url and saved_url == hub.base_url.rstrip("/"):
                    return HubTunnelHTTPClient(
                        hub_id=hub.id,
                        base_url=hub.base_url,
                        management_key=key_info["managementKey"],
                        auth_index=account.hub_auth_index or "",
                        upstream_name=account.display_name,
                        base_http=base_client,
                        gate=self._hub_gate,
                        on_login_missing=lambda _hid: self._pool.submit(self._sync_all_hubs, True),
                    )
        return base_client

    # -----------------------------------------------------------------------
    # NDJSON stdout emission
    # -----------------------------------------------------------------------

    def emit(self, payload: Dict[str, Any]) -> None:
        try:
            line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
            with self._stdout_lock:
                self._out.write(line + "\n")
                self._out.flush()
        except Exception:
            pass

    def emit_toast(self, message: str, *, error: bool = False) -> None:
        self.emit({"type": "toast", "message": redact_secrets(message), "error": error})

    @functools.cached_property
    def _provider_catalog_payload(self) -> List[Dict[str, Any]]:
        """Static catalog sent with every state; built once."""
        items = []
        for kind, spec in PROVIDER_CATALOG.items():
            items.append({
                "id": spec.id,
                "displayName": spec.display_name,
                "icon": spec.icon,
                "authMethods": list(spec.auth_methods),
                "keyPage": spec.key_page or "",
                "unifiable": spec.unifiable,
                "links": [lnk.to_dict() for lnk in spec.links],
            })
        return items

    def emit_state(self) -> None:
        with self._lock:
            now = self._clock.now()
            self._last_state_emit_at = now
            errors = self.refresher.errors()
            error_kinds = self.refresher.error_kinds()
            dashboard = build_dashboard_state(
                accounts=list(self.store.accounts),
                snapshots=dict(self.store.snapshots),
                errors=errors,
                error_kinds=error_kinds,
                refreshing_ids=set(self._refreshing_ids),
                layout=dict(self.store.layout),
                settings=dict(self.store.settings),
                burning_tracker=self.burning_tracker,
                now=now,
            )
            accounts_view = []
            hidden_set = set(self.store.layout.get("hiddenMetricIDs") or [])
            pin_set = set(self.store.layout.get("priorityPinIDs") or [])
            for acc in self.store.accounts:
                snap = self.store.snapshots.get(acc.id)
                saved_order = self.store.layout.get("metricOrderByProvider", {}).get(acc.id) or []
                metrics_meta = []
                if snap:
                    line_map = {l.key: l for l in snap.lines}
                    ordered_keys = [k for k in saved_order if k in line_map] + [l.key for l in snap.lines if l.key not in saved_order]
                    for k in ordered_keys:
                        ln = line_map[k]
                        mid = f"{acc.id}.{k}"
                        metrics_meta.append({
                            "key": k,
                            "metricId": mid,
                            "title": metric_title(ln.title),
                            "hidden": mid in hidden_set,
                            "pinned": mid in pin_set,
                        })
                item = acc.to_dict()
                item["displayName"] = acc.display_name
                item["accountName"] = acc.account_name
                item["icon"] = acc.spec.icon
                item["authMethod"] = acc.effective_auth_method
                item["plan"] = (snap.plan or "") if snap else ""
                item["error"] = errors.get(acc.id, "")
                item["errorKind"] = error_kinds.get(acc.id, "")
                item["notice"] = data_text(snap.notice or "") if snap else ""
                item["refreshing"] = acc.id in self._refreshing_ids
                item["links"] = acc.links()
                item["metrics"] = metrics_meta
                accounts_view.append(item)

            payload = {
                "type": "state",
                "dashboard": dashboard,
                "accounts": accounts_view,
                "hubs": [h.to_dict() for h in self.store.hubs],
                "settings": dict(self.store.settings),
                "layout": dict(self.store.layout),
                "proxy": {"enabled": self.proxy.enabled, "url": self.proxy.redacted_url()},
                "canUndo": self.store.can_undo(),
                "nextRefreshAt": format_iso8601(self._next_refresh_at),
                "refreshing": len(self._refreshing_ids) > 0,
                "authSession": self._auth_session,
                "providerCatalog": self._provider_catalog_payload,
            }
        self.emit(payload)

    # -----------------------------------------------------------------------
    # Auth callbacks
    # -----------------------------------------------------------------------

    def _on_auth_update(self, session: Optional[Dict[str, Any]]) -> None:
        clean_session = session
        if isinstance(session, dict) and not session.get("active") and session.get("status") == "cancelled":
            clean_session = None
        with self._lock:
            self._auth_session = clean_session
        self.emit({"type": "auth_update", "session": clean_session})
        if isinstance(session, dict) and session.get("status") == "error" and session.get("error"):
            self.emit_toast(str(session["error"]), error=True)

    def _on_auth_complete(self, result: Dict[str, Any]) -> None:
        kind = result["kind"]
        label = result.get("label") or PROVIDER_CATALOG[kind].display_name
        credential = result["credential"]
        domain = result.get("domain")
        organization = result.get("organization")
        identity = result.get("identity") or (credential.get("extras") or {}).get("account_identity")
        existing_id = result.get("accountId")

        with self._lock:
            admission = self.accounts.admit(
                kind,
                credential,
                label=label,
                rule=OAUTH_LOGIN,
                identity=identity,
                organization=organization,
                domain=domain,
                auth_mode=result.get("method"),
                target_id=existing_id or None,
            )
            target = admission.account
            if admission.outcome == "duplicate_updated":
                self.emit_toast(tr("toast.accountUpdated", "Updated existing %1 account.", target.full_name))
            elif admission.outcome == "created":
                self.emit_toast(tr("toast.accountAdded", "Added %1.", target.full_name))
            self._auth_session = None

        self.emit({"type": "auth_update", "session": None})
        self.emit_state()
        if target is not None:
            self._pool.submit(self._refresh_single_account, target.id, True)

    # -----------------------------------------------------------------------
    # Background refresh & hub sync
    # -----------------------------------------------------------------------

    def _refresh_single_account(self, account_id: str, force: bool = False) -> None:
        with self._lock:
            acc = self.refresher.plan(account_id, force=force, in_flight=self._refreshing_ids)
            if acc is None:
                return
            self._refreshing_ids.add(acc.id)

        self.emit_state()
        try:
            outcome = self.refresher.fetch(acc, force=force)
            with self._lock:
                self.refresher.record(acc, outcome)
        finally:
            with self._lock:
                self._refreshing_ids.discard(acc.id)
            self.emit_state()

    def _sync_all_hubs(self, force: bool = False) -> None:
        with self._lock:
            hubs = list(self.store.hubs)
            now = self._clock.now()
            if not hubs:
                return
            if not force and self._last_hub_sync is not None:
                if (now - self._last_hub_sync).total_seconds() < SECONDARY_READ_INTERVAL_SEC:
                    return
            self._last_hub_sync = now

        http = self._make_http_client()
        changed = False
        for hub in hubs:
            if hub.key_rejected and not force:
                continue
            key_info = self.vault.get_hub_key(hub.id)
            if not key_info or not key_info.get("managementKey"):
                hub.last_error = tr("hubSync.keyMissing", "The management key for the CLIProxyAPI hub at %1 is missing.", hub.host)
                self.store.save_accounts()
                continue
            if key_info.get("baseUrl") and key_info["baseUrl"].rstrip("/") != hub.base_url.rstrip("/"):
                hub.last_error = tr("hubSync.keyAddressMismatch", "%1 isn't the address its management key was saved for.", hub.host)
                self.store.save_accounts()
                continue
            try:
                with self._lock:
                    res = self.accounts.sync_hub(hub, key_info["managementKey"], http, gate=self._hub_gate)
                    if res.get("added") or res.get("removed") or res.get("relinked"):
                        changed = True
            except Exception as err:
                hub.last_error = redact_secrets(str(err))
                self.store.save_accounts()
        if changed:
            self.emit_state()

    def refresh_all(self, force: bool = False) -> List[Any]:
        """Sync hubs, schedule every account refresh; returns what the pool returned
        for each scheduled refresh (futures in production)."""
        self._sync_all_hubs(force=force)
        with self._lock:
            ids = [a.id for a in self.store.accounts]
            self._next_refresh_at = self._clock.now() + timedelta(seconds=self._refresh_interval())
        scheduled = [self._pool.submit(self._refresh_single_account, acc_id, force) for acc_id in ids]
        self.emit_state()
        return scheduled

    def _has_crossed_reset_window(self, now: datetime) -> bool:
        with self._lock:
            last_emit = self._last_state_emit_at
            for snap in self.store.snapshots.values():
                for ln in snap.lines:
                    resets_dt = parse_iso8601(ln.resets_at)
                    if resets_dt is not None and last_emit < resets_dt <= now:
                        return True
        return False

    def _background_tick(self) -> None:
        """One idle-loop step: start a refresh cycle when due, otherwise re-emit
        state only when some window crossed its `resetsAt` (no idle re-emission)."""
        now = self._clock.now()
        if now >= self._next_refresh_at:
            self._pool.submit(self.refresh_all, False)
        elif self._has_crossed_reset_window(now):
            self.emit_state()

    def _background_loop(self) -> None:
        # Initial refresh shortly after startup
        self._pool.submit(self.refresh_all, False)
        while not self._clock.stopped():
            self._background_tick()
            if self._clock.wait(BACKGROUND_TICK_SEC):
                break

    # -----------------------------------------------------------------------
    # Command handlers
    # -----------------------------------------------------------------------

    # Every command handler takes the decoded message and returns its outcome:
    # None = ok, a string = the failure (already shown to the user as a toast),
    # or AfterAll(futures) = ok once that follow-up work has finished.
    # `in_pool` handlers run on the worker pool (they touch the network or disk
    # for a while); the others run inline on the stdin thread.
    _COMMANDS: Dict[str, Tuple[bool, str]] = {
        "get_state": (False, "_cmd_get_state"),
        "set_language": (False, "_cmd_set_language"),
        "refresh_all": (True, "_cmd_refresh_all"),
        "refresh_account": (True, "_cmd_refresh_account"),
        "update_settings": (False, "_cmd_update_settings"),
        "update_proxy": (False, "_cmd_update_proxy"),
        "set_metric_pinned": (False, "_cmd_set_metric_pinned"),
        "set_metric_hidden": (False, "_cmd_set_metric_hidden"),
        "set_provider_unified": (False, "_cmd_set_provider_unified"),
        "move_provider": (False, "_cmd_move_provider"),
        "move_metric": (False, "_cmd_move_metric"),
        "undo_layout": (False, "_cmd_undo_layout"),
        "rename_account": (False, "_cmd_rename_account"),
        "remove_account": (False, "_cmd_remove_account"),
        "start_auth": (False, "_cmd_start_auth"),
        "submit_auth_code": (False, "_cmd_submit_auth_code"),
        "cancel_auth": (False, "_cmd_cancel_auth"),
        "add_api_key_account": (True, "_cmd_add_api_key_account"),
        "add_hub": (True, "_cmd_add_hub"),
        "replace_hub_key": (True, "_cmd_replace_hub_key"),
        "remove_hub": (False, "_cmd_remove_hub"),
        "sync_hubs": (True, "_cmd_sync_hubs"),
        "claim_reset": (True, "_cmd_claim_reset"),
        "detect_local_credentials": (True, "_cmd_detect_local_credentials"),
        "import_local_credential": (True, "_cmd_import_local_credential"),
        "copy_text": (False, "_cmd_copy_text"),
    }

    def handle_command(self, msg: Dict[str, Any]) -> None:
        """Route one decoded command. A command carrying an `id` gets exactly one
        `{"type": "result", "id", "ok", "error"?}` event, emitted after its work
        (and the toasts/state that work emits) is done; without `id`, none."""
        cmd = str(msg.get("cmd") or "").strip()
        cmd_id = command_id(msg)
        route = self._COMMANDS.get(cmd)
        if route is None:
            if cmd_id is not None:
                self._emit_result(cmd_id, tr("command.unknown", "Unknown command: %1", cmd) if cmd else tr("command.missing", "Missing command."))
            return
        in_pool, method_name = route
        handler = getattr(self, method_name)
        if in_pool:
            self._pool.submit(self._run, cmd_id, handler, msg)
        else:
            self._run(cmd_id, handler, msg)

    def _run(self, cmd_id: Optional[str], handler: Any, msg: Dict[str, Any]) -> None:
        """Run one handler, turn an unexpected exception into a failure (toast +
        result) instead of losing it, and answer the command when it carries an id."""
        try:
            outcome = handler(msg)
        except Exception as err:
            outcome = self._fail(tr("command.failedWith", "Command failed: %1", err))
        if cmd_id is None:
            return
        if isinstance(outcome, AfterAll):
            outcome.then(lambda: self._emit_result(cmd_id, None))
        else:
            self._emit_result(cmd_id, outcome)

    def _emit_result(self, cmd_id: str, failure: Optional[str]) -> None:
        payload: Dict[str, Any] = {"type": "result", "id": cmd_id, "ok": failure is None}
        if failure is not None:
            payload["error"] = redact_secrets(str(failure)) or tr("command.failed", "Command failed.")
        self.emit(payload)

    def _fail(self, message: str) -> str:
        """Show a failure to the user (error toast) and return it as the command outcome."""
        text = redact_secrets(str(message))
        self.emit_toast(text, error=True)
        return text

    # ---- inline commands ----

    def _cmd_get_state(self, msg: Dict[str, Any]) -> Optional[str]:
        self.emit_state()
        return None

    def _cmd_set_language(self, msg: Dict[str, Any]) -> Optional[str]:
        # Omarchy Setup > Language, pushed by the QML; text the daemon writes follows it.
        if set_language(msg.get("language")):
            self.emit_state()
        return None

    def _cmd_update_settings(self, msg: Dict[str, Any]) -> Optional[str]:
        patch = msg.get("patch")
        if not isinstance(patch, dict):
            return tr("command.invalidSettingsPatch", "Invalid settings patch.")
        with self._lock:
            self.store.update_settings(patch)
        self.emit_state()
        return None

    def _cmd_update_proxy(self, msg: Dict[str, Any]) -> Optional[str]:
        enabled = bool(msg.get("enabled", False))
        url = str(msg.get("url") or "").strip()
        proxy_path = self.store.config_dir / "proxy.json"
        with self._lock:
            if url and url == self.proxy.redacted_url() and self.proxy.url:
                url = self.proxy.url
        ok, err = ProxyConfig.save(enabled, url, path=proxy_path)
        if not ok:
            return self._fail(err or tr("command.invalidProxyUrl", "Invalid proxy URL."))
        with self._lock:
            self.proxy = ProxyConfig.load(path=proxy_path)
            self.auth_manager._http = self._make_http_client()
        self.emit_toast(tr("toast.proxySaved", "Proxy settings saved."))
        self.emit_state()
        return None

    def _cmd_set_metric_pinned(self, msg: Dict[str, Any]) -> Optional[str]:
        mids = [str(x) for x in (msg.get("metricIds") or [])]
        with self._lock:
            self.store.set_metric_pinned(mids, bool(msg.get("pinned", True)))
        self.emit_state()
        return None

    def _cmd_set_metric_hidden(self, msg: Dict[str, Any]) -> Optional[str]:
        mids = [str(x) for x in (msg.get("metricIds") or [])]
        with self._lock:
            self.store.set_metric_hidden(mids, bool(msg.get("hidden", True)))
        self.emit_state()
        return None

    def _cmd_set_provider_unified(self, msg: Dict[str, Any]) -> Optional[str]:
        with self._lock:
            self.store.set_provider_unified(str(msg.get("kind") or ""), bool(msg.get("unified", False)))
        self.emit_state()
        return None

    def _cmd_move_provider(self, msg: Dict[str, Any]) -> Optional[str]:
        direction = int(msg.get("direction", 0))
        with self._lock:
            self.store.move_provider(str(msg.get("kind") or ""), direction)
        self.emit_state()
        return None

    def _cmd_move_metric(self, msg: Dict[str, Any]) -> Optional[str]:
        direction = int(msg.get("direction", 0))
        with self._lock:
            self.store.move_metric(str(msg.get("accountId") or ""), str(msg.get("key") or ""), direction)
        self.emit_state()
        return None

    def _cmd_undo_layout(self, msg: Dict[str, Any]) -> Optional[str]:
        with self._lock:
            undone = self.store.undo_layout()
        if undone:
            self.emit_state()
        return None

    def _cmd_rename_account(self, msg: Dict[str, Any]) -> Optional[str]:
        with self._lock:
            self.store.rename_account(str(msg.get("accountId") or ""), str(msg.get("label") or "").strip())
        self.emit_state()
        return None

    def _cmd_remove_account(self, msg: Dict[str, Any]) -> Optional[str]:
        with self._lock:
            removed = self.accounts.remove(str(msg.get("accountId") or ""))
        if removed:
            self.emit_toast(tr("toast.accountRemoved", "Removed %1.", removed.full_name))
        self.emit_state()
        return None

    def _cmd_start_auth(self, msg: Dict[str, Any]) -> Optional[str]:
        kind = str(msg.get("kind") or "")
        method = str(msg.get("method") or "oauth")
        acc_id = msg.get("accountId")
        domain = msg.get("domain")
        if acc_id and not domain:
            existing = self.store.get_account(str(acc_id))
            if existing and existing.domain:
                domain = existing.domain
        if kind == "copilot" and domain:
            try:
                domain = normalize_github_domain(str(domain))
            except ValueError as err:
                return self._fail(str(err))
        try:
            self.auth_manager.start_login(
                kind,
                method=method,
                account_id=str(acc_id) if acc_id else None,
                domain=str(domain) if domain else None,
            )
        except Exception as err:
            return self._fail(str(err))
        return None

    def _cmd_submit_auth_code(self, msg: Dict[str, Any]) -> Optional[str]:
        # The exchange itself reports through auth_update; ok = the code reached a waiting sign-in.
        if not self.auth_manager.submit_manual_code(str(msg.get("code") or "").strip()):
            return tr("command.noSignInWaiting", "No sign-in is waiting for a code.")
        return None

    def _cmd_cancel_auth(self, msg: Dict[str, Any]) -> Optional[str]:
        self.auth_manager.cancel()
        return None

    def _cmd_remove_hub(self, msg: Dict[str, Any]) -> Optional[str]:
        with self._lock:
            hub = self.accounts.remove_hub(str(msg.get("hubId") or "").strip())
        if hub:
            self.emit_toast(tr("toast.hubRemoved", "Removed CLIProxyAPI hub %1.", hub.host))
        self.emit_state()
        return None

    def _cmd_copy_text(self, msg: Dict[str, Any]) -> Optional[str]:
        return self._copy_clipboard(str(msg.get("text") or ""))

    # ---- pool commands ----

    def _cmd_refresh_all(self, msg: Dict[str, Any]) -> "AfterAll":
        return AfterAll(self.refresh_all(bool(msg.get("force", True))))

    def _cmd_refresh_account(self, msg: Dict[str, Any]) -> Optional[str]:
        acc_id = str(msg.get("accountId") or "")
        if acc_id:
            self._refresh_single_account(acc_id, bool(msg.get("force", True)))
        return None

    def _cmd_add_api_key_account(self, msg: Dict[str, Any]) -> Optional[str]:
        return self._handle_add_api_key(
            str(msg.get("kind") or ""),
            str(msg.get("apiKey") or "").strip(),
            (str(msg.get("organization") or "").strip() or None),
            (str(msg.get("accountId") or "").strip() or None),
        )

    def _cmd_add_hub(self, msg: Dict[str, Any]) -> Optional[str]:
        return self._handle_add_hub(
            str(msg.get("address") or "").strip(),
            str(msg.get("managementKey") or "").strip(),
        )

    def _cmd_replace_hub_key(self, msg: Dict[str, Any]) -> Optional[str]:
        return self._handle_replace_hub_key(
            str(msg.get("hubId") or "").strip(),
            str(msg.get("managementKey") or "").strip(),
        )

    def _cmd_sync_hubs(self, msg: Dict[str, Any]) -> Optional[str]:
        self._sync_all_hubs(bool(msg.get("force", True)))
        return None

    def _cmd_claim_reset(self, msg: Dict[str, Any]) -> Optional[str]:
        return self._handle_claim_reset(str(msg.get("accountId") or ""), dict(msg.get("resetItem") or {}))

    def _cmd_detect_local_credentials(self, msg: Dict[str, Any]) -> Optional[str]:
        return self._handle_detect_local_credentials()

    def _cmd_import_local_credential(self, msg: Dict[str, Any]) -> Optional[str]:
        return self._handle_import_local_credential(str(msg.get("candidateId") or "").strip())

    def _handle_detect_local_credentials(self) -> Optional[str]:
        try:
            with self._lock:
                accounts = list(self.store.accounts)
            items = scan_local_credentials(accounts)
        except Exception as err:
            return self._fail(tr("toast.localScanFailed", "Could not scan local credentials: %1", err))
        self.emit({"type": "local_credentials", "items": items})
        return None

    def _handle_import_local_credential(self, candidate_id: str) -> Optional[str]:
        if not candidate_id:
            return self._fail(tr("toast.noCredentialSelected", "No credential selected to import."))
        try:
            kind, method, label, credential, identity = load_local_credential_payload(candidate_id)
            if kind not in PROVIDER_CATALOG:
                raise ValueError(tr("command.unknownProviderNamed", "Unknown provider: %1", kind))
        except Exception as err:
            return self._fail(str(err))

        spec = PROVIDER_CATALOG[kind]
        source = candidate_source_name(candidate_id)
        try:
            with self._lock:
                target = self.accounts.admit(
                    kind,
                    credential,
                    label=label,
                    rule=LOCAL_IMPORT,
                    identity=identity,
                    auth_mode=method,
                ).account
                accounts_copy = list(self.store.accounts)
        except Exception as err:
            return self._fail(tr("toast.importFailed", "Failed to import %1: %2", spec.display_name, err))

        self.emit_toast(tr("toast.imported", "Imported %1 from %2", spec.display_name, source))
        self.emit_state()
        try:
            items = scan_local_credentials(accounts_copy)
            self.emit({"type": "local_credentials", "items": items})
        except Exception:
            pass
        self._pool.submit(self._refresh_single_account, target.id, True)
        return None

    def _handle_add_api_key(
        self,
        kind: str,
        api_key: str,
        organization: Optional[str],
        existing_account_id: Optional[str],
    ) -> Optional[str]:
        if kind not in PROVIDER_CATALOG:
            return self._fail(tr("command.unknownProvider", "Unknown provider."))
        if not api_key:
            return self._fail(tr("command.apiKeyEmpty", "API key cannot be empty."))
        if kind == "copilot" and not organization:
            return self._fail(tr("command.organizationRequired", "Organization name is required for Copilot Organization Billing."))

        label = organization if (kind == "copilot" and organization) else default_account_label(kind)
        temp_acc = Account.create(
            kind=kind,
            label=label,
            organization=organization,
            identity=api_key_identity(api_key),
            auth_mode="org_token" if (kind == "copilot" and organization) else "api_key",
        )
        cred = {"type": "api_key", "apiKey": api_key}
        http = self._make_http_client()
        ctx = FetchContext(account=temp_acc, credential=cred, http=http, now=utc_now(), force=True)

        # Validate with 1 live usage read before saving
        try:
            snap = fetch_provider(ctx)
        except Exception as err:
            return self._fail(str(err))

        if snap.account_label_discovered:
            temp_acc.label = snap.account_label_discovered

        with self._lock:
            admission = self.accounts.admit(
                kind,
                cred,
                label=temp_acc.label,
                rule=API_KEY,
                identity=temp_acc.identity,
                organization=organization,
                auth_mode=temp_acc.auth_mode,
                target_id=existing_account_id,
                snapshot=snap,
            )
            if admission.outcome == "rejected":
                return self._fail(tr("command.apiKeyDuplicate", "This API key is already added."))
            if admission.account is None:
                return self._fail(tr("command.accountNotFound", "Account not found."))
            if admission.outcome == "updated":
                self.emit_toast(tr("toast.keyUpdated", "Updated key for %1.", admission.account.full_name))
            else:
                self.emit_toast(tr("toast.accountAdded", "Added %1.", admission.account.full_name))
        self.emit_state()
        return None

    def _handle_add_hub(self, address: str, management_key: str) -> Optional[str]:
        if not address or not management_key:
            return self._fail(tr("command.hubFieldsRequired", "Hub address and management key are required."))
        try:
            base_url, host = normalize_hub_address(address)
        except ValueError as err:
            return self._fail(str(err))

        http = self._make_http_client()
        try:
            list_hub_auth_files(base_url, management_key, http)
        except Exception as err:
            return self._fail(str(err))

        hub = CLIProxyHub(id=str(uuid.uuid4()).lower(), base_url=base_url, host=host)
        with self._lock:
            self.store.add_hub(hub)
            self.vault.set_hub_key(hub.id, base_url, management_key)
            res = self.accounts.sync_hub(hub, management_key, http, gate=self._hub_gate)
            hub_accounts = [a for a in self.store.accounts if a.hub_id == hub.id]
        self.emit_toast(tr("toast.hubConnected", "Connected CLIProxyAPI hub %1 (%2 logins).", host, res.get("total", len(hub_accounts))))
        self.emit_state()
        for acc in hub_accounts:
            self._pool.submit(self._refresh_single_account, acc.id, True)
        return None

    def _handle_replace_hub_key(self, hub_id: str, management_key: str) -> Optional[str]:
        hub = self.store.get_hub(hub_id)
        if not hub or not management_key:
            return self._fail(tr("command.hubOrKeyMissing", "Hub or key missing."))
        http = self._make_http_client()
        try:
            list_hub_auth_files(hub.base_url, management_key, http)
        except Exception as err:
            return self._fail(str(err))
        with self._lock:
            self.vault.set_hub_key(hub.id, hub.base_url, management_key)
            self._hub_gate.reopen(hub.id)
            hub.key_rejected = False
            hub.last_error = None
            self.accounts.sync_hub(hub, management_key, http, gate=self._hub_gate)
        self.emit_toast(tr("toast.hubKeyUpdated", "Updated management key for %1.", hub.host))
        self.emit_state()
        return None

    def _handle_claim_reset(self, account_id: str, reset_item: Dict[str, Any]) -> Optional[str]:
        res = self.refresher.claim(account_id, reset_item)
        if res is None:
            return self._fail(tr("command.accountNotFound", "Account not found."))
        message = res.get("message") or (tr("toast.resetClaimed", "Reset claimed.") if res.get("ok") else tr("toast.resetFailed", "Reset failed."))
        self.emit_toast(message, error=not res.get("ok"))
        if res.get("outcome") != "failed":
            self._pool.submit(self._refresh_single_account, account_id, True)
        return None if res.get("ok") else redact_secrets(message)

    def _copy_clipboard(self, text: str) -> Optional[str]:
        if not text:
            return None
        if shutil.which("wl-copy"):
            try:
                subprocess.run(["wl-copy"], input=text.encode("utf-8"), timeout=2.0, check=False)
                self.emit_toast(tr("toast.copied", "Copied to clipboard."))
                return None
            except Exception:
                pass
        return self._fail(tr("toast.clipboardUnavailable", "wl-copy not available."))

    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------

    def run(self) -> None:
        self.emit_state()
        bg = threading.Thread(target=self._background_loop, name="ou-bg", daemon=True)
        bg.start()

        try:
            for raw_line in sys.stdin:
                line = raw_line.strip()
                if not line:
                    continue
                if len(line) > 512 * 1024:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict):
                    try:
                        self.handle_command(msg)
                    except Exception as err:  # safety net; handlers already answer via _run
                        self.emit_toast(redact_secrets(tr("command.failedWith", "Command failed: %1", err)), error=True)
        except KeyboardInterrupt:
            pass
        finally:
            stop = getattr(self._clock, "stop", None)
            if callable(stop):
                stop()
            self.auth_manager.cancel()
            self._pool.shutdown(wait=False)


if __name__ == "__main__":
    OmameterDaemon().run()
