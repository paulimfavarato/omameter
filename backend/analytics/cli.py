#!/usr/bin/env python3
"""NDJSON stdio server and CLI for the standalone Omameter Analytics window.

Runs only while the Analytics window is open (zero idle RAM in omarchy-shell).
Communicates with `analytics/shell.qml` over stdin/stdout NDJSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from backend.analytics.service import AnalyticsService
from backend.http_client import redact_secrets


def _emit(payload: Dict[str, Any]) -> None:
    line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _handle_command(
    svc: AnalyticsService,
    msg: Dict[str, Any],
    *,
    progress_cb: Any = None,
) -> Dict[str, Any]:
    cmd = str(msg.get("cmd") or "").strip()
    req_id = msg.get("id")

    def _ok(data: Dict[str, Any]) -> Dict[str, Any]:
        out = {"type": "response", "ok": True, "cmd": cmd, **data}
        if req_id is not None:
            out["id"] = req_id
        return out

    def _err(message: str) -> Dict[str, Any]:
        out = {"type": "response", "ok": False, "cmd": cmd, "error": redact_secrets(message)}
        if req_id is not None:
            out["id"] = req_id
        return out

    try:
        if cmd == "get_report":
            scan_mode = str(msg.get("scan") or "none")
            scan_stats = None
            if scan_mode == "full":
                scan_stats = svc.scan_local_harnesses(force=False, progress_cb=progress_cb)
            elif scan_mode == "force":
                scan_stats = svc.scan_local_harnesses(force=True, progress_cb=progress_cb)
            elif scan_mode == "recent":
                scan_stats = svc.scan_local_harnesses(force=False, only_recent_sec=3600, progress_cb=progress_cb)
            hour_raw = msg.get("hourStart")
            hour_val = int(hour_raw) if isinstance(hour_raw, (int, float)) and hour_raw > 0 else None
            report = svc.build_report(
                period=str(msg.get("period") or "today"),
                provider=str(msg.get("provider") or "all"),
                model=str(msg.get("model") or "all"),
                project=str(msg.get("project") or "all"),
                hour_start=hour_val,
                history_account_id=str(msg.get("historyAccountId") or ""),
                history_metric_key=str(msg.get("historyMetricKey") or ""),
            )
            return _ok({"report": report, "scan": scan_stats})

        if cmd == "scan":
            stats = svc.scan_local_harnesses(force=bool(msg.get("force")), progress_cb=progress_cb)
            return _ok({"scan": stats})

        if cmd == "set_local_enabled":
            enabled = svc.set_local_analytics_enabled(bool(msg.get("enabled")))
            # Return immediately so the UI transitions out of the opt-in banner right away;
            # the client then triggers `scan` with live streaming progress.
            return _ok({"localEnabled": enabled})

        if cmd == "update_config":
            cfg = svc.update_config(msg.get("patch") if isinstance(msg.get("patch"), dict) else {})
            return _ok({"config": cfg})

        if cmd == "set_model_price":
            def _opt_float(v: Any) -> Any:
                if v is None or v == "":
                    return None
                return float(v)
            cfg = svc.set_model_price(
                str(msg.get("model") or ""),
                _opt_float(msg.get("inputPerM")),
                _opt_float(msg.get("outputPerM")),
                _opt_float(msg.get("cacheReadPerM")),
                _opt_float(msg.get("cacheWritePerM")),
            )
            return _ok({"config": cfg})

        if cmd == "set_model_alias":
            cfg = svc.set_model_alias(
                str(msg.get("sourceModel") or ""),
                msg.get("targetModel"),
            )
            return _ok({"config": cfg})

        if cmd == "set_plan_price":
            raw_p = msg.get("monthlyUsd")
            val = float(raw_p) if (raw_p is not None and raw_p != "") else None
            cfg = svc.set_plan_price(str(msg.get("provider") or ""), val)
            return _ok({"config": cfg})

        if cmd == "refresh_pricing":
            res = svc.refresh_pricing_online()
            if not res.get("ok"):
                return _err(str(res.get("error") or "Pricing refresh failed."))
            return _ok(res)

        if cmd == "clear_token_history":
            svc.db.clear_token_history()
            return _ok({"cleared": True})

        return _err(f"Unknown command: {cmd}")
    except Exception as exc:
        return _err(str(exc))


def _serve(svc: AnalyticsService) -> int:
    def _emit_progress(info: Dict[str, Any]) -> None:
        _emit({"type": "scan_progress", **info})

    _emit({"type": "ready"})
    for raw_line in sys.stdin:
        text = raw_line.strip()
        if not text:
            continue
        try:
            msg = json.loads(text)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        _emit(_handle_command(svc, msg, progress_cb=_emit_progress))
    return 0


def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser(description="Omameter Analytics worker and CLI")
    parser.add_argument("--serve", action="store_true", help="Run NDJSON stdio server")
    parser.add_argument("--scan", action="store_true", help="Run incremental local harness scan")
    parser.add_argument("--force", action="store_true", help="Force full rescan of local harnesses")
    parser.add_argument("--report", action="store_true", help="Print JSON analytics report")
    parser.add_argument("--period", default="today", help="Period: today, 7d, 30d, 90d, all, or since_reset:<acc>:<key>")
    parser.add_argument("--provider", default="all", help="Filter by provider: all, claude, codex, antigravity, opencode")
    args = parser.parse_args(argv)

    svc = AnalyticsService()
    if args.serve:
        return _serve(svc)
    if args.scan:
        stats = svc.scan_local_harnesses(force=args.force)
        print(json.dumps(stats, indent=2))
        return 0
    if args.report:
        report = svc.build_report(period=args.period, provider=args.provider)
        print(json.dumps(report, indent=2))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
