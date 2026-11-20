"""Analytics orchestration: Phase 1 quota history + Phase 2 local token & cost reports."""

from __future__ import annotations

import math
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.analytics.db import AnalyticsDB, DEFAULT_DB_PATH, QuotaSample, ScanFileState, TokenEvent
from backend.analytics.parsers import (
    HARNESS_ORDER,
    discover_harness_roots,
    enumerate_harness_files,
    inspect_harness_coverage,
    scan_antigravity_db,
    scan_claude_file,
    scan_codex_file,
    scan_opencode_db,
    scan_opencode_legacy_json,
)
from backend.analytics.pricing import (
    ANALYTICS_CONFIG_PATH,
    BUNDLED_CATALOG_PATH,
    LITELLM_CACHE_PATH,
    PricingEngine,
    load_analytics_config,
    save_analytics_config,
)
from backend.http_client import ProxyConfig
from backend.models import (
    Account,
    MetricPeriod,
    PROVIDER_CATALOG,
    ProviderSnapshot,
    parse_iso8601,
    utc_now,
)
from backend.settings import normalize_settings
from backend.store import CACHE_DIR, CONFIG_DIR, OmameterStore

HARNESS_DISPLAY_NAMES: Dict[str, str] = {
    "claude": "Claude Code",
    "codex": "GPT Codex",
    "antigravity": "Antigravity",
    "opencode": "OpenCode",
}

SCAN_BATCH_FILES = 64


def _local_tz():
    return datetime.now().astimezone().tzinfo or timezone.utc


def _period_ms_for_window(window_kind: str, period_ms: Optional[int]) -> int:
    if isinstance(period_ms, int) and period_ms > 0:
        return period_ms
    wk = (window_kind or "").lower()
    if wk in ("session", "5h"):
        return MetricPeriod.SESSION_MS
    if wk in ("daily", "day", "24h"):
        return MetricPeriod.DAY_MS
    if wk in ("weekly", "week", "7d"):
        return MetricPeriod.WEEK_MS
    if wk in ("monthly", "month", "30d"):
        return MetricPeriod.MONTH_MS
    return MetricPeriod.WEEK_MS


def _used_pct_from_line(line: Any) -> Optional[float]:
    if not getattr(line, "is_bounded", False):
        return None
    lim = getattr(line, "limit", None)
    used = getattr(line, "used", None)
    if lim is None or used is None or lim <= 0:
        return None
    if not (math.isfinite(lim) and math.isfinite(used)):
        return None
    return round(max(0.0, min(100.0, (used / lim) * 100.0)), 2)


def _project_basename(project: str) -> str:
    p = str(project or "").strip().rstrip("/")
    if not p:
        return ""
    return os.path.basename(p) or p


def _round_usd(val: float) -> float:
    return round(val, 6)


def _line_unit(ln: Any) -> str:
    cc = getattr(ln, "currency_code", None)
    if cc:
        return str(cc)
    kind = str(getattr(ln, "kind", "") or "")
    if kind == "dollars":
        return "USD"
    if kind == "percent":
        return "%"
    return str(getattr(ln, "unit_suffix", None) or getattr(ln, "resource_unit", None) or "")


def _line_window_kind(ln: Any) -> str:
    pms = getattr(ln, "period_duration_ms", None)
    if isinstance(pms, int) and pms > 0:
        if pms <= MetricPeriod.SESSION_MS + 3_600_000:
            return "session"
        if pms <= MetricPeriod.DAY_MS + 3_600_000:
            return "daily"
        if pms <= MetricPeriod.WEEK_MS + 86_400_000:
            return "weekly"
        return "monthly"
    key_t = f"{getattr(ln, 'key', '')} {getattr(ln, 'title', '')}".lower()
    if "session" in key_t or "5h" in key_t:
        return "session"
    if "day" in key_t or "daily" in key_t or "today" in key_t:
        return "daily"
    if "week" in key_t or "7d" in key_t:
        return "weekly"
    if "month" in key_t or "30d" in key_t:
        return "monthly"
    return ""


def _account_name(acc: Any) -> str:
    return str(getattr(acc, "label", "") or getattr(acc, "id", ""))


class AnalyticsService:
    """Owns the analytics DB, pricing engine, opt-in local scanner, and report builder."""

    def __init__(
        self,
        *,
        db_path: Path = DEFAULT_DB_PATH,
        config_path: Path = ANALYTICS_CONFIG_PATH,
        omameter_config_dir: Path = CONFIG_DIR,
        omameter_cache_dir: Path = CACHE_DIR,
        bundled_catalog_path: Path = BUNDLED_CATALOG_PATH,
        litellm_cache_path: Path = LITELLM_CACHE_PATH,
        home: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> None:
        self.db = AnalyticsDB(db_path)
        self.config_path = config_path
        self.omameter_config_dir = omameter_config_dir
        self.omameter_cache_dir = omameter_cache_dir
        self.home = home
        self.env = env
        self.config = load_analytics_config(config_path)
        self.pricing = PricingEngine(
            config=self.config,
            bundled_path=bundled_catalog_path,
            litellm_cache_path=litellm_cache_path,
        )

    # ------------------------------------------------------------------
    # Phase 1: Record quota snapshots from Omameter refreshes
    # ------------------------------------------------------------------

    def record_snapshot(
        self,
        account: Account,
        snapshot: ProviderSnapshot,
        now: Optional[datetime] = None,
    ) -> int:
        ts = int((now or utc_now()).timestamp())
        samples: List[QuotaSample] = []
        acc_name = _account_name(account)
        for ln in snapshot.lines:
            used_pct = _used_pct_from_line(ln)
            used_amt = ln.used if (ln.used is not None and math.isfinite(ln.used)) else None
            lim_amt = ln.limit if (ln.limit is not None and math.isfinite(ln.limit)) else None
            if ln.line_type == "values" and getattr(ln, "values", None):
                first_dollar = next((v for v in ln.values if v.kind == "dollars" and math.isfinite(v.number)), None)
                if first_dollar is not None:
                    used_amt = first_dollar.number
            if used_pct is None and (used_amt is None or (ln.line_type != "progress" and ln.kind != "dollars")):
                continue
            samples.append(
                QuotaSample(
                    ts=ts,
                    account_id=account.id,
                    provider=account.kind,
                    account_name=acc_name,
                    metric_key=ln.key,
                    metric_title=ln.title,
                    window_kind=_line_window_kind(ln),
                    used_pct=used_pct,
                    used_amount=used_amt,
                    limit_amount=lim_amt,
                    unit=_line_unit(ln),
                    resets_at=ln.resets_at or "",
                )
            )
        return self.db.insert_quota_samples(samples)

    # ------------------------------------------------------------------
    # Configuration & pricing updates
    # ------------------------------------------------------------------

    def reload_config(self) -> Dict[str, Any]:
        self.config = load_analytics_config(self.config_path)
        self.pricing.update_config(self.config)
        return self.config

    def update_config(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        current = load_analytics_config(self.config_path)
        if not isinstance(patch, dict):
            return current
        for key in ("enabledSources", "customPaths", "priceOverrides", "modelAliases", "planMonthlyPrices"):
            if key in patch and isinstance(patch[key], dict):
                merged = dict(current.get(key) or {})
                merged.update(patch[key])
                current[key] = merged
        if "retentionDays" in patch:
            current["retentionDays"] = patch["retentionDays"]
        self.config = save_analytics_config(current, self.config_path)
        self.pricing.update_config(self.config)
        retention = int(self.config.get("retentionDays") or 0)
        if retention > 0:
            self.db.prune_older_than(retention)
        return self.config

    def set_model_price(
        self,
        model_id: str,
        input_per_m: Optional[float],
        output_per_m: Optional[float],
        cache_read_per_m: Optional[float] = None,
        cache_write_per_m: Optional[float] = None,
    ) -> Dict[str, Any]:
        current = load_analytics_config(self.config_path)
        mid = str(model_id or "").strip()
        if not mid:
            return current
        overrides = dict(current.get("priceOverrides") or {})
        if input_per_m is None or output_per_m is None:
            overrides.pop(mid, None)
        else:
            entry: Dict[str, float] = {
                "inputPerM": float(input_per_m),
                "outputPerM": float(output_per_m),
            }
            if cache_read_per_m is not None:
                entry["cacheReadPerM"] = float(cache_read_per_m)
            if cache_write_per_m is not None:
                entry["cacheWritePerM"] = float(cache_write_per_m)
            overrides[mid] = entry
        current["priceOverrides"] = overrides
        self.config = save_analytics_config(current, self.config_path)
        self.pricing.update_config(self.config)
        return self.config

    def set_model_alias(self, source_model: str, target_model: Optional[str]) -> Dict[str, Any]:
        current = load_analytics_config(self.config_path)
        src = str(source_model or "").strip()
        if not src:
            return current
        aliases = dict(current.get("modelAliases") or {})
        dst = str(target_model or "").strip()
        if not dst or dst == src:
            aliases.pop(src, None)
        else:
            aliases[src] = dst
        current["modelAliases"] = aliases
        self.config = save_analytics_config(current, self.config_path)
        self.pricing.update_config(self.config)
        return self.config

    def set_plan_price(self, provider: str, monthly_usd: Optional[float]) -> Dict[str, Any]:
        current = load_analytics_config(self.config_path)
        prov = str(provider or "").strip().lower()
        if not prov:
            return current
        plans = dict(current.get("planMonthlyPrices") or {})
        if monthly_usd is None or monthly_usd <= 0:
            plans.pop(prov, None)
        else:
            plans[prov] = float(monthly_usd)
        current["planMonthlyPrices"] = plans
        self.config = save_analytics_config(current, self.config_path)
        self.pricing.update_config(self.config)
        return self.config

    def refresh_pricing_online(self) -> Dict[str, Any]:
        proxy_path = self.omameter_config_dir / "proxy.json"
        proxy = ProxyConfig.load(proxy_path)
        ok, err, count = self.pricing.refresh_litellm_online(proxy=proxy)
        return {"ok": ok, "error": err, "count": count, "pricingInfo": self.pricing.catalog_info()}

    # ------------------------------------------------------------------
    # Phase 2: Opt-in local harness scanning
    # ------------------------------------------------------------------

    def _load_omameter_store(self) -> OmameterStore:
        return OmameterStore(
            config_dir=self.omameter_config_dir,
            cache_dir=self.omameter_cache_dir,
        )

    def is_local_analytics_enabled(self, store: Optional[OmameterStore] = None) -> bool:
        st = store if store is not None else self._load_omameter_store()
        return bool(normalize_settings(st.settings).get("localTokenAnalytics", False))

    def set_local_analytics_enabled(self, enabled: bool) -> bool:
        store = self._load_omameter_store()
        store.update_settings({"localTokenAnalytics": bool(enabled)})
        return bool(store.settings.get("localTokenAnalytics", False))

    def scan_local_harnesses(
        self,
        *,
        force: bool = False,
        only_recent_sec: Optional[int] = None,
        progress_cb: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Incrementally scan enabled local harnesses (strictly opt-in)."""
        t0 = time.monotonic()
        self.reload_config()
        if not self.is_local_analytics_enabled():
            return {
                "enabled": False,
                "scannedFiles": 0,
                "skippedFiles": 0,
                "newEvents": 0,
                "durationMs": 0,
            }

        now_ts = int(time.time())
        enabled_map = self.config.get("enabledSources") or {}
        roots_map = discover_harness_roots(
            self.config.get("customPaths"),
            home=self.home,
            env=self.env,
        )
        cached_files = {} if force else self.db.get_all_scan_files()

        # Pre-enumerate files across enabled harnesses so progress reports exact totals.
        work_plan: List[Tuple[str, List[Tuple[Path, str]]]] = []
        total_files = 0
        for provider in HARNESS_ORDER:
            if not enabled_map.get(provider, True):
                continue
            files = enumerate_harness_files(provider, roots_map.get(provider) or [])
            work_plan.append((provider, files))
            total_files += len(files)

        scanned_files = 0
        skipped_files = 0
        processed_files = 0
        new_events = 0
        batch_events: List[TokenEvent] = []
        batch_states: List[ScanFileState] = []
        last_emit = 0.0

        def _emit_progress(phase: str, prov: str, force_emit: bool = False) -> None:
            nonlocal last_emit
            if progress_cb is None:
                return
            now_m = time.monotonic()
            if not force_emit and (now_m - last_emit) < 0.20:
                return
            last_emit = now_m
            pct = round((processed_files / total_files) * 100.0, 1) if total_files > 0 else 100.0
            try:
                progress_cb(
                    {
                        "phase": phase,
                        "provider": prov,
                        "providerName": HARNESS_DISPLAY_NAMES.get(prov, prov),
                        "processedFiles": processed_files,
                        "scannedFiles": scanned_files,
                        "totalFiles": total_files,
                        "newEvents": new_events + len(batch_events),
                        "percent": min(100.0, max(0.0, pct)),
                    }
                )
            except Exception:
                pass

        def _flush() -> None:
            nonlocal new_events
            if batch_events or batch_states:
                new_events += self.db.commit_scan_batch(batch_events, batch_states)
                batch_events.clear()
                batch_states.clear()

        _emit_progress("scanning", work_plan[0][0] if work_plan else "", force_emit=True)

        for provider, files in work_plan:
            if files:
                _emit_progress("scanning", provider, force_emit=True)
            for fpath, source_dir in files:
                processed_files += 1
                if only_recent_sec is not None:
                    try:
                        if (now_ts - int(fpath.stat().st_mtime)) > only_recent_sec:
                            # Also check SQLite WAL mtime for DB files
                            wal = Path(str(fpath) + "-wal")
                            if not (wal.is_file() and (now_ts - int(wal.stat().st_mtime)) <= only_recent_sec):
                                skipped_files += 1
                                _emit_progress("scanning", provider)
                                continue
                    except OSError:
                        skipped_files += 1
                        _emit_progress("scanning", provider)
                        continue

                cached = None if force else cached_files.get(str(fpath))
                if provider == "claude":
                    res = scan_claude_file(fpath, source_dir, cached, now_ts)
                elif provider == "codex":
                    res = scan_codex_file(fpath, source_dir, cached, now_ts)
                elif provider == "opencode":
                    if fpath.name.endswith(".db"):
                        res = scan_opencode_db(fpath, source_dir, cached, now_ts)
                    else:
                        res = scan_opencode_legacy_json(fpath, source_dir, cached, now_ts)
                elif provider == "antigravity":
                    res = scan_antigravity_db(fpath, source_dir, cached, now_ts)
                else:
                    res = None

                if res is None:
                    skipped_files += 1
                    _emit_progress("scanning", provider)
                    continue
                evs, state = res
                scanned_files += 1
                batch_events.extend(evs)
                batch_states.append(state)
                if len(batch_states) >= SCAN_BATCH_FILES:
                    _flush()
                _emit_progress("scanning", provider)

        _flush()
        _emit_progress("finalizing", "", force_emit=True)
        retention = int(self.config.get("retentionDays") or 0)
        if retention > 0 and only_recent_sec is None:
            self.db.prune_older_than(retention, now_ts=now_ts)
        self.db.set_meta("last_scan_at", str(now_ts))

        return {
            "enabled": True,
            "scannedFiles": scanned_files,
            "skippedFiles": skipped_files,
            "totalFiles": total_files,
            "newEvents": new_events,
            "durationMs": int((time.monotonic() - t0) * 1000),
            "lastScanAt": now_ts,
        }

    # ------------------------------------------------------------------
    # Report builder (Phase 1 + Phase 2 unified payload)
    # ------------------------------------------------------------------

    def _resolve_period_bounds(
        self,
        period: str,
        now_ts: int,
        live_quotas: Sequence[Dict[str, Any]],
    ) -> Tuple[int, int, str, str, str]:
        """Return `(since_ts, until_ts, granularity, period_label, reset_meta)`."""
        tz = _local_tz()
        now_local = datetime.fromtimestamp(now_ts, tz=tz)
        today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

        if period.startswith("since_reset:"):
            parts = period.split(":", 2)
            if len(parts) == 3:
                acc_id, mkey = parts[1], parts[2]
                for q in live_quotas:
                    if q["accountId"] == acc_id and q["metricKey"] == mkey:
                        resets_dt = parse_iso8601(q.get("resetsAt"))
                        p_ms = int(q.get("periodMs") or MetricPeriod.WEEK_MS)
                        if resets_dt is not None:
                            win_start = int(resets_dt.timestamp()) - (p_ms // 1000)
                            # If resets_at is already in the past, shift back one period
                            if win_start > now_ts:
                                win_start = now_ts - (p_ms // 1000)
                            gran = "hourly" if (now_ts - win_start) <= 48 * 3600 else "daily"
                            lbl = f"{q['providerName']} · {q['title']}"
                            return max(0, win_start), now_ts, gran, lbl, period
            # Fallback to 7d if reset window disappeared
            period = "7d"

        if period == "today":
            return int(today_start.timestamp()), now_ts, "hourly", "today", ""
        if period == "7d":
            start = today_start - timedelta(days=6)
            return int(start.timestamp()), now_ts, "daily", "7d", ""
        if period == "30d":
            start = today_start - timedelta(days=29)
            return int(start.timestamp()), now_ts, "daily", "30d", ""
        if period == "90d":
            start = today_start - timedelta(days=89)
            return int(start.timestamp()), now_ts, "daily", "90d", ""
        # "all"
        return 0, now_ts, "daily", "all", ""

    def _build_quota_section(
        self,
        store: OmameterStore,
        *,
        since_ts: int,
        until_ts: int,
        history_account_id: str,
        history_metric_key: str,
    ) -> Dict[str, Any]:
        live_quotas: List[Dict[str, Any]] = []
        cloud_spend: List[Dict[str, Any]] = []

        for acc in store.accounts:
            snap = store.snapshots.get(acc.id)
            if snap is None:
                continue
            spec = PROVIDER_CATALOG.get(acc.kind)
            prov_name = spec.display_name if spec else acc.kind.capitalize()
            acc_name = _account_name(acc)
            for ln in snap.lines:
                wkind = _line_window_kind(ln)
                lunit = _line_unit(ln)
                used_pct = _used_pct_from_line(ln)
                if ln.is_bounded and used_pct is not None:
                    p_ms = _period_ms_for_window(wkind, getattr(ln, "period_duration_ms", None))
                    resets_dt = parse_iso8601(ln.resets_at)
                    can_sr = (
                        acc.kind in HARNESS_ORDER
                        and resets_dt is not None
                        and int(resets_dt.timestamp()) > until_ts - 86400
                    )
                    live_quotas.append(
                        {
                            "accountId": acc.id,
                            "accountName": acc_name,
                            "provider": acc.kind,
                            "providerName": prov_name,
                            "icon": spec.icon if spec else "omameter.svg",
                            "metricKey": ln.key,
                            "title": ln.title,
                            "windowKind": wkind,
                            "usedPct": used_pct,
                            "remainingPct": round(max(0.0, 100.0 - used_pct), 2),
                            "usedAmount": ln.used,
                            "limitAmount": ln.limit,
                            "unit": lunit or "%",
                            "resetsAt": ln.resets_at or "",
                            "periodMs": p_ms,
                            "canSinceReset": can_sr,
                            "resetWindowId": f"since_reset:{acc.id}:{ln.key}",
                        }
                    )
                if ln.kind == "dollars" or lunit.strip().upper() in ("USD", "$"):
                    if ln.used is not None and math.isfinite(ln.used):
                        cloud_spend.append(
                            {
                                "accountId": acc.id,
                                "accountName": acc_name,
                                "provider": acc.kind,
                                "providerName": prov_name,
                                "metricKey": ln.key,
                                "title": ln.title,
                                "used": round(float(ln.used), 4),
                                "limit": round(float(ln.limit), 4) if (ln.limit is not None and math.isfinite(ln.limit)) else None,
                                "unit": lunit or "USD",
                                "windowKind": wkind,
                            }
                        )
                elif ln.line_type == "values" and getattr(ln, "values", None):
                    for mv in ln.values:
                        if mv.kind == "dollars" and math.isfinite(mv.number):
                            cloud_spend.append(
                                {
                                    "accountId": acc.id,
                                    "accountName": acc_name,
                                    "provider": acc.kind,
                                    "providerName": prov_name,
                                    "metricKey": ln.key,
                                    "title": f"{ln.title} ({mv.label})" if mv.label else ln.title,
                                    "used": round(float(mv.number), 4),
                                    "limit": None,
                                    "unit": mv.currency_code or "USD",
                                    "windowKind": wkind,
                                }
                            )

        history_metrics = self.db.quota_sample_metrics(since_ts=max(0, until_ts - 90 * 86400))
        sel_acc = history_account_id
        sel_key = history_metric_key
        if (not sel_acc or not sel_key) and history_metrics:
            # Prefer a bounded percentage metric with the most samples
            best = max(history_metrics, key=lambda m: (m["sample_count"], m["last_ts"]))
            sel_acc = str(best["account_id"])
            sel_key = str(best["metric_key"])
        elif (not sel_acc or not sel_key) and live_quotas:
            sel_acc = live_quotas[0]["accountId"]
            sel_key = live_quotas[0]["metricKey"]

        selected_history: Dict[str, Any] = {
            "accountId": sel_acc,
            "metricKey": sel_key,
            "metricTitle": "",
            "provider": "",
            "accountName": "",
            "windowKind": "",
            "resetsAt": "",
            "periodMs": MetricPeriod.WEEK_MS,
            "points": [],
            "peakPct": 0.0,
            "avgPct": 0.0,
            "resetCount": 0,
        }
        if sel_acc and sel_key:
            q_since = since_ts if since_ts > 0 else max(0, until_ts - 30 * 86400)
            pts = self.db.query_quota_samples(
                since_ts=q_since,
                until_ts=until_ts,
                account_id=sel_acc,
                metric_key=sel_key,
            )
            # Match metadata from live quotas or sample rows
            live_match = next(
                (q for q in live_quotas if q["accountId"] == sel_acc and q["metricKey"] == sel_key),
                None,
            )
            if live_match:
                selected_history["metricTitle"] = live_match["title"]
                selected_history["provider"] = live_match["provider"]
                selected_history["accountName"] = live_match["accountName"]
                selected_history["windowKind"] = live_match["windowKind"]
                selected_history["resetsAt"] = live_match["resetsAt"]
                selected_history["periodMs"] = live_match["periodMs"]
            elif pts:
                selected_history["metricTitle"] = pts[-1]["metric_title"]
                selected_history["provider"] = pts[-1]["provider"]
                selected_history["accountName"] = pts[-1]["account_name"]
                selected_history["windowKind"] = pts[-1]["window_kind"]
                selected_history["resetsAt"] = pts[-1]["resets_at"]
                selected_history["periodMs"] = _period_ms_for_window(pts[-1]["window_kind"], None)

            pct_vals: List[float] = []
            resets_seen = 0
            prev_pct: Optional[float] = None
            prev_reset = ""
            clean_pts: List[Dict[str, Any]] = []
            for p in pts:
                pct = p["used_pct"]
                if pct is not None and math.isfinite(pct):
                    pct_f = float(pct)
                    pct_vals.append(pct_f)
                    if prev_pct is not None and (
                        (prev_reset and p["resets_at"] and p["resets_at"] != prev_reset and pct_f < prev_pct)
                        or (prev_pct - pct_f >= 20.0)
                    ):
                        resets_seen += 1
                    prev_pct = pct_f
                    prev_reset = str(p["resets_at"] or "")
                clean_pts.append(
                    {
                        "ts": int(p["ts"]),
                        "usedPct": p["used_pct"],
                        "usedAmount": p["used_amount"],
                        "limitAmount": p["limit_amount"],
                        "resetsAt": str(p["resets_at"] or ""),
                    }
                )
            selected_history["points"] = clean_pts
            selected_history["resetCount"] = resets_seen
            if pct_vals:
                selected_history["peakPct"] = round(max(pct_vals), 1)
                selected_history["avgPct"] = round(sum(pct_vals) / len(pct_vals), 1)

        return {
            "live": live_quotas,
            "cloudSpend": cloud_spend,
            "historyMetrics": history_metrics,
            "selectedHistory": selected_history,
        }

    def build_report(
        self,
        *,
        period: str = "today",
        provider: str = "all",
        model: str = "all",
        project: str = "all",
        hour_start: Optional[int] = None,
        history_account_id: str = "",
        history_metric_key: str = "",
        now_ts: Optional[int] = None,
    ) -> Dict[str, Any]:
        self.reload_config()
        now_s = int(now_ts if now_ts is not None else time.time())
        store = self._load_omameter_store()
        norm_settings = normalize_settings(store.settings)
        local_enabled = bool(norm_settings.get("localTokenAnalytics", False))

        # Build live quotas first so `since_reset:<acc>:<key>` can resolve bounds
        tmp_quotas = self._build_quota_section(
            store,
            since_ts=max(0, now_s - 7 * 86400),
            until_ts=now_s,
            history_account_id=history_account_id,
            history_metric_key=history_metric_key,
        )
        since_ts, until_ts, granularity, period_label, active_reset_window = self._resolve_period_bounds(
            period, now_s, tmp_quotas["live"]
        )
        quotas_section = self._build_quota_section(
            store,
            since_ts=since_ts,
            until_ts=until_ts,
            history_account_id=history_account_id,
            history_metric_key=history_metric_key,
        )

        # Scope to enabled sources
        enabled_map = self.config.get("enabledSources") or {}
        active_providers = [p for p in HARNESS_ORDER if enabled_map.get(p, True)]
        if provider != "all" and provider in HARNESS_ORDER:
            query_providers = [provider] if provider in active_providers else []
        else:
            query_providers = active_providers

        raw_events = (
            self.db.query_token_events(
                since_ts=since_ts,
                until_ts=until_ts,
                providers=query_providers,
            )
            if local_enabled
            else []
        )

        # Evaluate pricing & apply model/project/hour filters
        tz = _local_tz()
        evaluated: List[Tuple[Dict[str, Any], Any]] = []
        available_models = set()
        available_projects = set()

        for row in raw_events:
            ev_price = self.pricing.evaluate(
                model=row["model"],
                route=row["route"],
                input_tokens=int(row["input_tokens"]),
                output_tokens=int(row["output_tokens"]),
                cache_read_tokens=int(row["cache_read_tokens"]),
                cache_write_tokens=int(row["cache_write_tokens"]),
                cache_write_1h_tokens=int(row["cache_write_1h_tokens"]),
                speed_tier=str(row["speed_tier"] or "standard"),
                reported_cost_usd=row["reported_cost_usd"],
            )
            available_models.add(ev_price.resolved_model)
            if row["project"]:
                available_projects.add(row["project"])

            if model != "all" and ev_price.resolved_model != model:
                continue
            if project != "all" and row["project"] != project:
                continue
            if hour_start is not None:
                ts_val = int(row["ts"])
                if not (hour_start <= ts_val < hour_start + 3600):
                    continue
            evaluated.append((row, ev_price))

        # Summary & bucket accumulation
        total_in = total_out = total_cr = total_cw = total_cw1h = total_reas = 0
        total_cost = 0.0
        total_savings = 0.0
        unpriced_tokens = 0
        unpriced_events = 0
        sessions_set = set()

        prov_acc: Dict[str, Dict[str, Any]] = {
            p: {
                "id": p,
                "name": HARNESS_DISPLAY_NAMES[p],
                "inputTokens": 0,
                "outputTokens": 0,
                "cacheReadTokens": 0,
                "cacheWriteTokens": 0,
                "totalTokens": 0,
                "costUsd": 0.0,
                "cacheSavingsUsd": 0.0,
                "requests": 0,
                "sessions": set(),
                "unpricedTokens": 0,
            }
            for p in HARNESS_ORDER
        }
        model_acc: Dict[str, Dict[str, Any]] = {}
        project_acc: Dict[str, Dict[str, Any]] = {}
        route_acc: Dict[str, Dict[str, Any]] = {}
        session_acc: Dict[str, Dict[str, Any]] = {}
        unpriced_acc: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

        # Build time series buckets
        buckets: Dict[int, Dict[str, Any]] = {}
        if granularity == "hourly":
            base_dt = datetime.fromtimestamp(since_ts, tz=tz).replace(minute=0, second=0, microsecond=0)
            end_dt = datetime.fromtimestamp(until_ts, tz=tz).replace(minute=0, second=0, microsecond=0)
            # For Today, always show all 24 clock hours of today (00..23)
            if period == "today":
                cur = base_dt
                for _ in range(24):
                    bts = int(cur.timestamp())
                    buckets[bts] = {
                        "ts": bts,
                        "label": cur.strftime("%H:00"),
                        "totalTokens": 0,
                        "totalCost": 0.0,
                        "byProvider": {p: {"tokens": 0, "cost": 0.0} for p in HARNESS_ORDER},
                    }
                    cur = cur + timedelta(hours=1)
            else:
                cur = base_dt
                while cur <= end_dt and len(buckets) < 72:
                    bts = int(cur.timestamp())
                    buckets[bts] = {
                        "ts": bts,
                        "label": cur.strftime("%d %H:00"),
                        "totalTokens": 0,
                        "totalCost": 0.0,
                        "byProvider": {p: {"tokens": 0, "cost": 0.0} for p in HARNESS_ORDER},
                    }
                    cur = cur + timedelta(hours=1)
        else:
            if since_ts > 0:
                start_d = datetime.fromtimestamp(since_ts, tz=tz).replace(hour=0, minute=0, second=0, microsecond=0)
            elif evaluated:
                min_ts = min(int(r["ts"]) for r, _ in evaluated)
                start_d = datetime.fromtimestamp(min_ts, tz=tz).replace(hour=0, minute=0, second=0, microsecond=0)
            else:
                start_d = datetime.fromtimestamp(until_ts, tz=tz).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=6)
            end_d = datetime.fromtimestamp(until_ts, tz=tz).replace(hour=0, minute=0, second=0, microsecond=0)
            cur = start_d
            while cur <= end_d and len(buckets) < 400:
                bts = int(cur.timestamp())
                buckets[bts] = {
                    "ts": bts,
                    "label": cur.strftime("%m-%d"),
                    "date": cur.strftime("%Y-%m-%d"),
                    "totalTokens": 0,
                    "totalCost": 0.0,
                    "byProvider": {p: {"tokens": 0, "cost": 0.0} for p in HARNESS_ORDER},
                }
                cur = cur + timedelta(days=1)

        for row, ev_price in evaluated:
            prov = str(row["provider"])
            inp = int(row["input_tokens"])
            outp = int(row["output_tokens"])
            cread = int(row["cache_read_tokens"])
            cwrite = int(row["cache_write_tokens"])
            cw1h = int(row["cache_write_1h_tokens"])
            reas = int(row["reasoning_tokens"])
            tot = inp + outp + cread + cwrite
            cost = float(ev_price.cost_usd) if ev_price.cost_usd is not None else 0.0
            sav = float(ev_price.cache_savings_usd)
            ts_val = int(row["ts"])
            sid = str(row["session_id"])
            raw_model = str(row["model"])
            res_model = ev_price.resolved_model
            proj = str(row["project"] or "")
            route = str(row["route"] or prov)

            total_in += inp
            total_out += outp
            total_cr += cread
            total_cw += cwrite
            total_cw1h += cw1h
            total_reas += reas
            total_cost += cost
            total_savings += sav
            if sid:
                sessions_set.add(f"{prov}:{sid}")
            if not ev_price.priced:
                unpriced_tokens += tot
                unpriced_events += 1
                ukey = (raw_model, prov, route)
                uitem = unpriced_acc.setdefault(
                    ukey,
                    {"model": raw_model, "provider": prov, "route": route, "tokens": 0, "requests": 0},
                )
                uitem["tokens"] += tot
                uitem["requests"] += 1

            # Provider accumulator
            if prov in prov_acc:
                pa = prov_acc[prov]
                pa["inputTokens"] += inp
                pa["outputTokens"] += outp
                pa["cacheReadTokens"] += cread
                pa["cacheWriteTokens"] += cwrite
                pa["totalTokens"] += tot
                pa["costUsd"] += cost
                pa["cacheSavingsUsd"] += sav
                pa["requests"] += 1
                if sid:
                    pa["sessions"].add(sid)
                if not ev_price.priced:
                    pa["unpricedTokens"] += tot

            # Bucket
            dt_ev = datetime.fromtimestamp(ts_val, tz=tz)
            if granularity == "hourly":
                bdt = dt_ev.replace(minute=0, second=0, microsecond=0)
            else:
                bdt = dt_ev.replace(hour=0, minute=0, second=0, microsecond=0)
            bts = int(bdt.timestamp())
            bkt = buckets.get(bts)
            if bkt is not None:
                bkt["totalTokens"] += tot
                bkt["totalCost"] += cost
                if prov in bkt["byProvider"]:
                    bkt["byProvider"][prov]["tokens"] += tot
                    bkt["byProvider"][prov]["cost"] += cost

            # Model breakdown
            ma = model_acc.setdefault(
                res_model,
                {
                    "model": res_model,
                    "rawModels": set(),
                    "providers": set(),
                    "routes": set(),
                    "requests": 0,
                    "inputTokens": 0,
                    "outputTokens": 0,
                    "cacheReadTokens": 0,
                    "cacheWriteTokens": 0,
                    "totalTokens": 0,
                    "costUsd": 0.0,
                    "cacheSavingsUsd": 0.0,
                    "priced": True,
                },
            )
            ma["rawModels"].add(raw_model)
            ma["providers"].add(prov)
            ma["routes"].add(route)
            ma["requests"] += 1
            ma["inputTokens"] += inp
            ma["outputTokens"] += outp
            ma["cacheReadTokens"] += cread
            ma["cacheWriteTokens"] += cwrite
            ma["totalTokens"] += tot
            ma["costUsd"] += cost
            ma["cacheSavingsUsd"] += sav
            if not ev_price.priced:
                ma["priced"] = False

            # Project breakdown
            pkey = proj or "(no project)"
            pja = project_acc.setdefault(
                pkey,
                {
                    "project": proj,
                    "name": _project_basename(proj) or "(no project)",
                    "providers": set(),
                    "requests": 0,
                    "totalTokens": 0,
                    "costUsd": 0.0,
                    "sessions": set(),
                },
            )
            pja["providers"].add(prov)
            pja["requests"] += 1
            pja["totalTokens"] += tot
            pja["costUsd"] += cost
            if sid:
                pja["sessions"].add(f"{prov}:{sid}")

            # Route breakdown
            ra = route_acc.setdefault(
                route,
                {
                    "route": route,
                    "providers": set(),
                    "requests": 0,
                    "totalTokens": 0,
                    "costUsd": 0.0,
                },
            )
            ra["providers"].add(prov)
            ra["requests"] += 1
            ra["totalTokens"] += tot
            ra["costUsd"] += cost

            # Session breakdown
            skey = f"{prov}:{sid}"
            sa = session_acc.setdefault(
                skey,
                {
                    "sessionId": sid,
                    "provider": prov,
                    "project": proj,
                    "projectName": _project_basename(proj),
                    "ts": ts_val,
                    "requests": 0,
                    "totalTokens": 0,
                    "costUsd": 0.0,
                    "modelTokens": defaultdict(int),
                },
            )
            if ts_val > sa["ts"]:
                sa["ts"] = ts_val
            if proj and not sa["project"]:
                sa["project"] = proj
                sa["projectName"] = _project_basename(proj)
            sa["requests"] += 1
            sa["totalTokens"] += tot
            sa["costUsd"] += cost
            sa["modelTokens"][res_model] += tot

        total_tokens = total_in + total_out + total_cr + total_cw
        prompt_denom = total_in + total_cr + total_cw
        cache_hit_rate = round((total_cr / prompt_denom) * 100.0, 1) if prompt_denom > 0 else 0.0

        # Coverage & provider list
        coverage = inspect_harness_coverage(
            self.config.get("customPaths"),
            home=self.home,
            env=self.env,
        )
        plan_prices = self.config.get("planMonthlyPrices") or {}
        providers_out: List[Dict[str, Any]] = []
        plan_total_usd = 0.0
        for p in HARNESS_ORDER:
            pa = prov_acc[p]
            pm = float(plan_prices.get(p) or 0.0)
            if provider == "all" or provider == p:
                plan_total_usd += pm
            p_denom = pa["inputTokens"] + pa["cacheReadTokens"] + pa["cacheWriteTokens"]
            p_hit = round((pa["cacheReadTokens"] / p_denom) * 100.0, 1) if p_denom > 0 else 0.0
            cov = coverage.get(p) or {}
            providers_out.append(
                {
                    "id": p,
                    "name": pa["name"],
                    "inputTokens": pa["inputTokens"],
                    "outputTokens": pa["outputTokens"],
                    "cacheReadTokens": pa["cacheReadTokens"],
                    "cacheWriteTokens": pa["cacheWriteTokens"],
                    "totalTokens": pa["totalTokens"],
                    "costUsd": _round_usd(pa["costUsd"]),
                    "cacheSavingsUsd": _round_usd(pa["cacheSavingsUsd"]),
                    "cacheHitRate": p_hit,
                    "requests": pa["requests"],
                    "sessions": len(pa["sessions"]),
                    "unpricedTokens": pa["unpricedTokens"],
                    "planMonthlyUsd": pm,
                    "roiMultiplier": round(pa["costUsd"] / pm, 2) if pm > 0 and pa["costUsd"] > 0 else 0.0,
                    "installed": bool(cov.get("installed")),
                    "fileCount": int(cov.get("fileCount") or 0),
                    "enabled": bool(enabled_map.get(p, True)),
                }
            )

        models_out: List[Dict[str, Any]] = []
        for ma in model_acc.values():
            m_denom = ma["inputTokens"] + ma["cacheReadTokens"] + ma["cacheWriteTokens"]
            m_hit = round((ma["cacheReadTokens"] / m_denom) * 100.0, 1) if m_denom > 0 else 0.0
            cpm = (ma["costUsd"] / ma["totalTokens"] * 1_000_000.0) if ma["totalTokens"] > 0 else 0.0
            mapped_from = sorted(r for r in ma["rawModels"] if r != ma["model"])
            models_out.append(
                {
                    "model": ma["model"],
                    "rawModels": sorted(ma["rawModels"]),
                    "mappedFrom": mapped_from,
                    "providers": sorted(ma["providers"]),
                    "routes": sorted(ma["routes"]),
                    "requests": ma["requests"],
                    "inputTokens": ma["inputTokens"],
                    "outputTokens": ma["outputTokens"],
                    "cacheReadTokens": ma["cacheReadTokens"],
                    "cacheWriteTokens": ma["cacheWriteTokens"],
                    "totalTokens": ma["totalTokens"],
                    "cacheHitRate": m_hit,
                    "costUsd": _round_usd(ma["costUsd"]),
                    "cacheSavingsUsd": _round_usd(ma["cacheSavingsUsd"]),
                    "costPerMTokens": round(cpm, 3),
                    "priced": ma["priced"],
                }
            )
        models_out.sort(key=lambda x: (x["costUsd"], x["totalTokens"]), reverse=True)

        projects_out = [
            {
                "project": pja["project"],
                "name": pja["name"],
                "providers": sorted(pja["providers"]),
                "requests": pja["requests"],
                "totalTokens": pja["totalTokens"],
                "costUsd": _round_usd(pja["costUsd"]),
                "sessions": len(pja["sessions"]),
            }
            for pja in project_acc.values()
        ]
        projects_out.sort(key=lambda x: (x["costUsd"], x["totalTokens"]), reverse=True)

        routes_out = [
            {
                "route": ra["route"],
                "providers": sorted(ra["providers"]),
                "requests": ra["requests"],
                "totalTokens": ra["totalTokens"],
                "costUsd": _round_usd(ra["costUsd"]),
            }
            for ra in route_acc.values()
        ]
        routes_out.sort(key=lambda x: (x["costUsd"], x["totalTokens"]), reverse=True)

        sessions_out = []
        for sa in session_acc.values():
            prim_model = max(sa["modelTokens"].items(), key=lambda kv: kv[1])[0] if sa["modelTokens"] else "unknown"
            sessions_out.append(
                {
                    "sessionId": sa["sessionId"],
                    "provider": sa["provider"],
                    "project": sa["project"],
                    "projectName": sa["projectName"] or "(no project)",
                    "ts": sa["ts"],
                    "primaryModel": prim_model,
                    "requests": sa["requests"],
                    "totalTokens": sa["totalTokens"],
                    "costUsd": _round_usd(sa["costUsd"]),
                }
            )
        sessions_out.sort(key=lambda x: x["ts"], reverse=True)
        sessions_out = sessions_out[:60]

        series_out = []
        for bts in sorted(buckets):
            b = buckets[bts]
            b["totalCost"] = _round_usd(b["totalCost"])
            for p in HARNESS_ORDER:
                b["byProvider"][p]["cost"] = _round_usd(b["byProvider"][p]["cost"])
            series_out.append(b)

        unpriced_out = sorted(
            unpriced_acc.values(),
            key=lambda x: x["tokens"],
            reverse=True,
        )

        # Sum cloud-billed spend lines (e.g. Cursor, OpenRouter, Codex extra)
        cloud_spend_total = sum(
            float(c["used"])
            for c in quotas_section["cloudSpend"]
            if c.get("unit", "").upper() in ("USD", "$")
            and c.get("windowKind", "") in ("month", "monthly", "extra", "day", "today", "")
        )

        last_scan_raw = self.db.get_meta("last_scan_at", "0")
        try:
            last_scan_at = int(last_scan_raw)
        except ValueError:
            last_scan_at = 0

        return {
            "generatedAt": now_s,
            "localEnabled": local_enabled,
            "lastScanAt": last_scan_at,
            "period": {
                "id": period,
                "label": period_label,
                "sinceTs": since_ts,
                "untilTs": until_ts,
                "granularity": granularity,
                "resetWindow": active_reset_window,
            },
            "filters": {
                "provider": provider,
                "model": model,
                "project": project,
                "hourStart": hour_start,
                "availableModels": sorted(available_models),
                "availableProjects": sorted(available_projects),
            },
            "summary": {
                "totalTokens": total_tokens,
                "inputTokens": total_in,
                "outputTokens": total_out,
                "cacheReadTokens": total_cr,
                "cacheWriteTokens": total_cw,
                "cacheWrite1hTokens": total_cw1h,
                "reasoningTokens": total_reas,
                "cacheHitRate": cache_hit_rate,
                "cacheSavingsUsd": _round_usd(total_savings),
                "estimatedCostUsd": _round_usd(total_cost),
                "unpricedTokens": unpriced_tokens,
                "unpricedEvents": unpriced_events,
                "requests": len(evaluated),
                "sessions": len(sessions_set),
                "planMonthlyTotalUsd": round(plan_total_usd, 2),
                "planRoiMultiplier": round(total_cost / plan_total_usd, 2) if plan_total_usd > 0 and total_cost > 0 else 0.0,
                "cloudSpendUsd": round(cloud_spend_total, 2),
            },
            "series": series_out,
            "providers": providers_out,
            "models": models_out,
            "projects": projects_out,
            "routes": routes_out,
            "sessions": sessions_out,
            "unpricedModels": unpriced_out,
            "quotas": quotas_section,
            "coverage": coverage,
            "config": self.config,
            "pricingInfo": self.pricing.catalog_info(),
            "thresholds": {
                "warning": int(norm_settings.get("warningThreshold", 30)),
                "critical": int(norm_settings.get("criticalThreshold", 10)),
            },
            "timeFormat": str(norm_settings.get("timeFormat", "auto")),
            "privacyMode": bool(norm_settings.get("privacyMode", False)),
        }
