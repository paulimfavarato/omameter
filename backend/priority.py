"""PriorityBoard ranking, UnifiedAccounts aggregation, and MenuBarContentBuilder."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from backend.models import (
    Account,
    MetricLine,
    MetricValue,
    PROVIDER_CATALOG,
    PriorityLimits,
    ProviderSnapshot,
    ResetItem,
    UNIFIABLE_KINDS,
    account_initials,
    format_iso8601,
    format_metric_value,
    metric_window_info,
    parse_iso8601,
    short_metric_label,
    title_names_window,
)
from backend.i18n import data_text, metric_title, tr, unit_label
from backend.settings import normalize_settings
from backend.pace import (
    BurningFastTracker,
    is_unused_near_reset,
    meter_state,
    pace_mark,
    pace_tooltip,
    runway_seconds,
)


# ---------------------------------------------------------------------------
# UnifiedAccounts aggregation (Claude & ChatGPT with 2+ accounts)
# ---------------------------------------------------------------------------

def _least_shown(kind: str) -> float:
    if kind == "percent":
        return 1.0
    if kind == "count":
        return 0.1
    if kind == "dollars":
        return 0.01
    return 0.01


def _sum_values(values: List[MetricValue]) -> List[MetricValue]:
    grouped: Dict[Tuple[Optional[str], str, Optional[str]], MetricValue] = {}
    order: List[Tuple[Optional[str], str, Optional[str]]] = []
    for mv in values:
        k = (mv.label, mv.kind, mv.currency_code)
        if k not in grouped:
            grouped[k] = MetricValue(
                number=mv.number,
                kind=mv.kind,
                label=mv.label,
                currency_code=mv.currency_code,
            )
            order.append(k)
        else:
            grouped[k].number += mv.number
    return [grouped[k] for k in order]


def pool_metric_lines(key: str, readings: List[MetricLine]) -> MetricLine:
    """Exact port of WidgetData.pooling(_ readings:) from UnifiedAccounts.swift."""
    assert readings
    first = readings[0]

    # Case A: Homogeneous bounded limits
    homogeneous = all(
        r.is_bounded and r.kind == first.kind and r.currency_code == first.currency_code
        for r in readings
    )
    if homogeneous:
        total_used = sum(min(r.used, r.limit or 0.0) for r in readings)
        total_limit = sum(r.limit or 0.0 for r in readings)
        if total_limit > 0:
            scale = (100.0 / total_limit) if first.kind == "percent" else 1.0
            scaled_used = total_used * scale
            scaled_limit = total_limit * scale
            reset_dates = [parse_iso8601(r.resets_at) for r in readings]
            valid_resets = [dt for dt in reset_dates if dt is not None]
            soonest_reset = format_iso8601(min(valid_resets)) if valid_resets else None

            pooled = MetricLine(
                key=key,
                title=first.title,
                line_type="progress",
                kind=first.kind,
                used=scaled_used,
                limit=scaled_limit,
                currency_code=first.currency_code,
                unit_suffix=first.unit_suffix,
                resets_at=soonest_reset,
                period_duration_ms=None,      # unified rows have no pace
                session_start_signal=None,
                resource_key=first.resource_key,
                resource_kind=first.resource_kind,
                resource_unit=first.resource_unit,
            )
            # Anti-false-spent guard: unified limit is spent only when EVERY member is spent
            if pooled.is_spent and not all(r.is_spent for r in readings):
                pooled.used = max(0.0, scaled_limit - _least_shown(first.kind))
            return pooled

    # Case B: Heterogeneous / unbounded / multi-currency / resets / badges
    base = next((r for r in readings if not r.is_bounded), first)
    all_expiries = sorted(e for r in readings for e in r.expiries_at)
    all_resets: List[ResetItem] = []
    for r in readings:
        all_resets.extend(r.reset_items)
    all_resets.sort(key=lambda item: item.sort_key())

    if base.line_type == "badge" or any(r.badge_text for r in readings):
        seen: List[str] = []
        for r in readings:
            if r.badge_text and r.badge_text not in seen:
                seen.append(r.badge_text)
        return MetricLine(
            key=key,
            title=base.title,
            line_type="badge",
            kind=base.kind,
            badge_text=" · ".join(seen),
            expiries_at=all_expiries,
            reset_items=all_resets,
            resource_key=base.resource_key,
            resource_kind=base.resource_kind,
            resource_unit=base.resource_unit,
        )

    combined_values: List[MetricValue] = []
    for r in readings:
        if r.is_bounded:
            combined_values.append(
                MetricValue(number=r.used, kind=r.kind, currency_code=r.currency_code)
            )
        else:
            combined_values.extend(r.values)
    summed = _sum_values(combined_values)

    return MetricLine(
        key=key,
        title=base.title,
        line_type="values",
        kind=base.kind,
        limit=None,
        currency_code=base.currency_code,
        unit_suffix=base.unit_suffix,
        values=summed,
        expiries_at=all_expiries,
        reset_items=all_resets,
        resource_key=base.resource_key,
        resource_kind=base.resource_kind,
        resource_unit=base.resource_unit,
    )


def merge_key_order(member_key_lists: List[List[str]]) -> List[str]:
    """UnifiedAccounts mergedOrder: preserves relative order across members."""
    merged: List[str] = []
    for keys in member_key_lists:
        for idx, k in enumerate(keys):
            if k in merged:
                continue
            insert_at: Optional[int] = None
            for subsequent in keys[idx + 1:]:
                if subsequent in merged:
                    insert_at = merged.index(subsequent)
                    break
            if insert_at is not None:
                merged.insert(insert_at, k)
            else:
                merged.append(k)
    return merged


# ---------------------------------------------------------------------------
# PriorityBoard & Dashboard projection
# ---------------------------------------------------------------------------

def format_line_amount(line: MetricLine, display_mode: str = "remaining", style: str = "row") -> str:
    if line.line_type == "badge":
        return metric_title(line.badge_text or "")
    if line.is_bounded and line.limit is not None:
        if line.kind == "percent":
            val = line.remaining_fraction * 100.0 if display_mode == "remaining" else line.used_fraction * 100.0
            txt = format_metric_value(val, "percent", style=style)
            if style == "tray":
                return txt
            if display_mode == "remaining":
                return tr("amount.left", "%1 left", txt)
            return tr("amount.used", "%1 used", txt)
        val = line.remaining if display_mode == "remaining" else line.used
        if style == "tray":
            return format_metric_value(val, line.kind, currency_code=line.currency_code, style="tray")
        left_str = format_metric_value(val, line.kind, currency_code=line.currency_code, style="row", unit_suffix=line.unit_suffix)
        lim_str = format_metric_value(line.limit, line.kind, currency_code=line.currency_code, style="row", unit_suffix=line.unit_suffix)
        if display_mode == "remaining":
            return tr("amount.leftOf", "%1 left of %2", left_str, lim_str)
        return f"{left_str} / {lim_str}"
    if line.values:
        parts: List[str] = []
        for mv in line.values:
            rendered = format_metric_value(mv.number, mv.kind, currency_code=mv.currency_code, style=style)
            if mv.label == "left":
                parts.append(tr("amount.left", "%1 left", rendered))
            elif mv.label == "available":
                parts.append(tr("amount.available", "%1 available", rendered))
            elif mv.label:
                parts.append(f"{rendered} {unit_label(mv.label)}")
            else:
                parts.append(rendered)
        return " · ".join(parts)
    return ""


def _reset_item_view(item: Any) -> Dict[str, Any]:
    view = item.to_dict()
    view["title"] = metric_title(view.get("title") or "")
    if view.get("unavailableReason"):
        view["unavailableReason"] = data_text(view["unavailableReason"])
    return view


def _runout_at(line: MetricLine, now: datetime, resets_dt: Optional[datetime]) -> str:
    """ISO instant the line runs dry at the current pace, only when that comes before its reset."""
    if not line.is_bounded or resets_dt is None:
        return ""
    rw = runway_seconds(line, now)
    if rw is None or rw >= (resets_dt - now).total_seconds():
        return ""
    return format_iso8601(now + timedelta(seconds=rw)) or ""


def _serialize_line_view(
    *,
    metric_id: str,
    copy_ids: List[str],
    account_id: str,
    card_id: str,
    kind: str,
    provider_name: str,
    account_name: str,
    badge: Optional[str],
    is_hub: bool,
    is_unified: bool,
    line: MetricLine,
    now: datetime,
    limits: PriorityLimits,
    display_mode: str,
    show_pace_marks: bool,
    is_pinned: bool,
    member_rows: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    resets_dt = parse_iso8601(line.resets_at)
    sev = line.severity(limits)
    if line.expiries_at and not line.is_bounded:
        sev = line.expiry_severity(now)
    st = meter_state(line, now) if line.is_bounded else ""
    pmark = pace_mark(line, now, display_mode=display_mode, shows_pace_marks=show_pace_marks)
    ptip = pace_tooltip(line, now)
    runout_at = _runout_at(line, now, resets_dt)
    # The panel formats both inline texts live: "outIn" counts down to runoutAt,
    # "pace" shows paceTooltip. No duration is frozen into text here.
    pace_inline_kind = ""
    if show_pace_marks and line.is_bounded:
        if st == "running_out" and runout_at:
            pace_inline_kind = "outIn"
        elif ptip:
            pace_inline_kind = "pace"
    fill_frac = line.remaining_fraction if display_mode == "remaining" else line.used_fraction
    window_kind, window_tag = metric_window_info(line)
    return {
        "metricId": metric_id,
        "copyIds": copy_ids,
        "accountId": account_id,
        "cardId": card_id,
        "kind": kind,
        "icon": PROVIDER_CATALOG[kind].icon,
        "providerName": provider_name,
        "accountName": account_name,
        "badge": badge,
        "isHub": is_hub,
        "isUnified": is_unified,
        "key": line.key,
        "title": metric_title(line.title),
        "shortTitle": metric_title(short_metric_label(line.title)),
        "windowKind": window_kind,
        "windowTag": window_tag,
        "titleNamesWindow": title_names_window(short_metric_label(line.title), window_tag),
        "isAggregateName": is_unified,
        "lineType": line.line_type,
        "isBounded": line.is_bounded,
        "fillFraction": fill_frac,
        "severity": sev,
        "meterState": st,
        "amountText": format_line_amount(line, display_mode=display_mode, style="row"),
        "trayValue": format_line_amount(line, display_mode=display_mode, style="tray"),
        "resetsAt": line.resets_at,
        "runoutAt": runout_at,
        "paceMark": pmark,
        "paceTooltip": ptip or "",
        "paceInlineKind": pace_inline_kind,
        "subtitle": unit_label(line.subtitle or ""),
        "isPinned": is_pinned,
        "resetItems": [_reset_item_view(r) for r in line.reset_items],
        "breakdown": line.breakdown.to_dict() if line.breakdown else None,
        "memberRows": member_rows or [],
    }


def build_dashboard_state(
    *,
    accounts: List[Account],
    snapshots: Dict[str, ProviderSnapshot],
    errors: Dict[str, str],
    refreshing_ids: Set[str],
    layout: Dict[str, Any],
    settings: Dict[str, Any],
    burning_tracker: BurningFastTracker,
    now: datetime,
    error_kinds: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Produces the complete All Usage, Priority Board, and Top Bar Strip payloads."""
    settings = normalize_settings(settings)
    err_kinds = error_kinds or {}
    limits = PriorityLimits.from_settings(settings)
    display_mode = settings["displayMode"]
    show_pace_marks = settings["showPaceMarks"]
    burning_fast_priority = settings["burningFastPriority"]
    use_it_or_lose_it_priority = settings["useItOrLoseItPriority"]
    max_bar_items = settings["maxBarItems"]
    always_show_in_bar = settings["alwaysShowInBar"]
    privacy_mode = settings["privacyMode"]

    hidden_ids: Set[str] = set(layout.get("hiddenMetricIDs") or [])
    pinned_ids: Set[str] = set(layout.get("priorityPinIDs") or [])
    unified_kinds: Set[str] = set(layout.get("unifiedProviderKinds") or [])
    provider_order: List[str] = list(layout.get("providerOrder") or [])
    metric_order_by_provider: Dict[str, List[str]] = dict(layout.get("metricOrderByProvider") or {})

    # Count accounts per provider kind to determine initials badge visibility
    kind_counts: Dict[str, int] = {}
    for acc in accounts:
        kind_counts[acc.kind] = kind_counts.get(acc.kind, 0) + 1

    # Order providers by user's saved provider_order, preserving insertion order for new ones
    by_kind: Dict[str, List[Account]] = {}
    for acc in accounts:
        by_kind.setdefault(acc.kind, []).append(acc)

    ordered_kinds: List[str] = []
    for k in provider_order:
        if k in by_kind and k not in ordered_kinds:
            ordered_kinds.append(k)
    for acc in accounts:
        if acc.kind not in ordered_kinds:
            ordered_kinds.append(acc.kind)

    all_usage_cards: List[Dict[str, Any]] = []
    dashboard_lines: List[Tuple[int, MetricLine, Dict[str, Any], bool]] = []  # (offset, line, view_dict, is_unified)
    hidden_metrics: List[Dict[str, Any]] = []
    notices: List[Dict[str, Any]] = []
    waiting_ids: List[str] = []
    offset_counter = 0

    for kind in ordered_kinds:
        kind_accounts = by_kind[kind]
        spec = PROVIDER_CATALOG[kind]
        can_unify = kind in UNIFIABLE_KINDS and len(kind_accounts) > 1
        is_unified = can_unify and (kind in unified_kinds)

        # Always collect per-member notices & waiting status
        for acc in kind_accounts:
            snap = snapshots.get(acc.id)
            err = errors.get(acc.id)
            if err:
                notices.append({
                    "accountId": acc.id,
                    "cardId": acc.card_id,
                    "kind": acc.kind,
                    "icon": spec.icon,
                    "providerName": spec.display_name,
                    "accountName": acc.account_name,
                    "message": err,
                    "errorKind": err_kinds.get(acc.id, ""),
                })
            elif snap and snap.notice:
                notices.append({
                    "accountId": acc.id,
                    "cardId": acc.card_id,
                    "kind": acc.kind,
                    "icon": spec.icon,
                    "providerName": spec.display_name,
                    "accountName": acc.account_name,
                    "message": data_text(snap.notice),
                    "errorKind": "",
                })
            elif snap is None:
                waiting_ids.append(acc.id)

        if is_unified:
            plans = []
            for acc in kind_accounts:
                snap = snapshots.get(acc.id)
                if snap and snap.plan and snap.plan not in plans:
                    plans.append(snap.plan)
            plan_multiple = len(plans) > 1
            unified_plan = "" if plan_multiple else (plans[0] if plans else "")

            member_key_lists: List[List[str]] = []
            member_lines_by_acc: Dict[str, Dict[str, MetricLine]] = {}
            for acc in kind_accounts:
                snap = snapshots.get(acc.id)
                if not snap:
                    continue
                saved_order = metric_order_by_provider.get(acc.id) or []
                keys = [l.key for l in snap.lines]
                ordered_keys = [k for k in saved_order if k in keys] + [k for k in keys if k not in saved_order]
                member_key_lists.append(ordered_keys)
                member_lines_by_acc[acc.id] = {l.key: l for l in snap.lines}

            merged_keys = merge_key_order(member_key_lists)
            unified_rows: List[Dict[str, Any]] = []
            for key in merged_keys:
                copy_ids = [f"{acc.id}.{key}" for acc in kind_accounts]
                if all(cid in hidden_ids for cid in copy_ids):
                    for acc in kind_accounts:
                        ln = member_lines_by_acc.get(acc.id, {}).get(key)
                        if ln is not None:
                            hidden_metrics.append({
                                "metricId": f"{acc.id}.{key}",
                                "accountId": acc.id,
                                "key": key,
                                "kind": kind,
                                "providerName": spec.display_name,
                                "accountName": acc.account_name,
                                "title": metric_title(ln.title),
                            })
                    continue
                readings: List[MetricLine] = []
                member_row_views: List[Dict[str, Any]] = []
                for acc in kind_accounts:
                    ln = member_lines_by_acc.get(acc.id, {}).get(key)
                    if ln is None:
                        continue
                    mid = f"{acc.id}.{key}"
                    if mid in hidden_ids:
                        hidden_metrics.append({
                            "metricId": mid,
                            "accountId": acc.id,
                            "key": key,
                            "kind": kind,
                            "providerName": spec.display_name,
                            "accountName": acc.account_name,
                            "title": metric_title(ln.title),
                        })
                    readings.append(ln)
                    member_row_views.append(
                        _serialize_line_view(
                            metric_id=mid,
                            copy_ids=[mid],
                            account_id=acc.id,
                            card_id=acc.card_id,
                            kind=kind,
                            provider_name=spec.display_name,
                            account_name=acc.account_name,
                            badge=account_initials(acc.account_name),
                            is_hub=acc.is_hub_account,
                            is_unified=False,
                            line=ln,
                            now=now,
                            limits=limits,
                            display_mode=display_mode,
                            show_pace_marks=show_pace_marks,
                            is_pinned=mid in pinned_ids,
                        )
                    )
                if not readings:
                    continue
                pooled = pool_metric_lines(key, readings)
                is_pinned = any(cid in pinned_ids for cid in copy_ids)
                view = _serialize_line_view(
                    metric_id=f"{kind}.unified.{key}",
                    copy_ids=copy_ids,
                    account_id=f"{kind}.unified",
                    card_id=f"{kind}.unified",
                    kind=kind,
                    provider_name=spec.display_name,
                    account_name=f"{len(kind_accounts)} Accounts",
                    badge=None,
                    is_hub=False,
                    is_unified=True,
                    line=pooled,
                    now=now,
                    limits=limits,
                    display_mode=display_mode,
                    show_pace_marks=False,
                    is_pinned=is_pinned,
                    member_rows=member_row_views,
                )
                unified_rows.append(view)
                dashboard_lines.append((offset_counter, pooled, view, True))
                offset_counter += 1

            all_usage_cards.append({
                "kind": kind,
                "displayName": spec.display_name,
                "icon": spec.icon,
                "canUnify": can_unify,
                "isUnified": True,
                "links": [lnk.to_dict() for lnk in spec.links],
                "accounts": [
                    {
                        "id": f"{kind}.unified",
                        "cardId": f"{kind}.unified",
                        "accountName": f"{len(kind_accounts)} Accounts",
                        "plan": unified_plan,
                        "planMultiple": plan_multiple,
                        "isUnified": True,
                        "memberCount": len(kind_accounts),
                        "refreshing": any(a.id in refreshing_ids for a in kind_accounts),
                        "stale": any((snapshots.get(a.id) is not None and snapshots[a.id].is_stale(now)) for a in kind_accounts),
                        "error": next((errors[a.id] for a in kind_accounts if a.id in errors), ""),
                        "errorKind": next((err_kinds.get(a.id, "") for a in kind_accounts if a.id in errors), ""),
                        "notice": "",
                        "lines": unified_rows,
                        "links": [lnk.to_dict() for lnk in spec.links],
                    }
                ],
            })
        else:
            account_cards: List[Dict[str, Any]] = []
            for acc in kind_accounts:
                snap = snapshots.get(acc.id)
                badge = account_initials(acc.account_name) if kind_counts.get(kind, 0) > 1 else None
                rows: List[Dict[str, Any]] = []
                if snap:
                    saved_order = metric_order_by_provider.get(acc.id) or []
                    line_map = {l.key: l for l in snap.lines}
                    ordered_keys = [k for k in saved_order if k in line_map] + [l.key for l in snap.lines if l.key not in saved_order]
                    for key in ordered_keys:
                        ln = line_map[key]
                        mid = f"{acc.id}.{key}"
                        if mid in hidden_ids:
                            hidden_metrics.append({
                                "metricId": mid,
                                "accountId": acc.id,
                                "key": key,
                                "kind": kind,
                                "providerName": spec.display_name,
                                "accountName": acc.account_name,
                                "title": metric_title(ln.title),
                            })
                            continue
                        is_pinned = mid in pinned_ids
                        view = _serialize_line_view(
                            metric_id=mid,
                            copy_ids=[mid],
                            account_id=acc.id,
                            card_id=acc.card_id,
                            kind=kind,
                            provider_name=spec.display_name,
                            account_name=acc.account_name,
                            badge=badge,
                            is_hub=acc.is_hub_account,
                            is_unified=False,
                            line=ln,
                            now=now,
                            limits=limits,
                            display_mode=display_mode,
                            show_pace_marks=show_pace_marks,
                            is_pinned=is_pinned,
                        )
                        rows.append(view)
                        dashboard_lines.append((offset_counter, ln, view, False))
                        offset_counter += 1

                account_cards.append({
                    "id": acc.id,
                    "cardId": acc.card_id,
                    "accountName": acc.account_name,
                    "label": acc.label,
                    "domain": acc.domain or "",
                    "organization": acc.organization or "",
                    "isHub": acc.is_hub_account,
                    "hubHost": acc.hub_host or "",
                    "plan": (snap.plan or "") if snap else "",
                    "planMultiple": False,
                    "isUnified": False,
                    "refreshing": acc.id in refreshing_ids,
                    "stale": snap.is_stale(now) if snap else False,
                    "error": errors.get(acc.id, ""),
                    "errorKind": err_kinds.get(acc.id, ""),
                    "notice": data_text(snap.notice or "") if snap else "",
                    "lines": rows,
                    "links": acc.links(),
                })

            all_usage_cards.append({
                "kind": kind,
                "displayName": spec.display_name,
                "icon": spec.icon,
                "canUnify": can_unify,
                "isUnified": False,
                "links": [lnk.to_dict() for lnk in spec.links],
                "accounts": account_cards,
            })

    # -----------------------------------------------------------------------
    # PriorityBoard evaluation & ranking
    # -----------------------------------------------------------------------
    # noteKind tells the panel which live text goes beside the amount:
    #   "amount" (empty balance), "resetCompact" (used up), "reset" (follows resetDisplayMode),
    #   "resetRelative" (always a countdown), "outIn" (counts down to runoutAt), "" (none).
    ranked_items: List[Tuple[Tuple[int, float, int], str, Dict[str, Any]]] = []
    pinned_rows_ranked: List[Tuple[Tuple[int, int], Dict[str, Any]]] = []
    pinned_bar_ranked: List[Tuple[Tuple[int, int], Dict[str, Any]]] = []
    item_by_metric_id: Dict[str, Dict[str, Any]] = {}
    pin_order_list: List[str] = list(layout.get("priorityPinIDs") or [])
    pin_rank: Dict[str, int] = {mid: idx for idx, mid in enumerate(pin_order_list)}

    for offset, ln, view, is_unified in dashboard_lines:
        mid = view["metricId"]
        is_pinned = view["isPinned"]
        resets_dt = parse_iso8601(ln.resets_at)

        reason: Optional[str] = None
        section: Optional[str] = None
        sev = "normal"
        note = ""
        tier = 99
        closeness = math.inf

        if ln.is_empty_open_balance():
            reason = "emptyBalance"
            section = "usedUp"
            sev = "critical"
            note = "amount"
            tier = 2
            closeness = math.inf
        elif ln.is_bounded:
            st = meter_state(ln, now)
            line_sev = ln.severity(limits)
            rw = runway_seconds(ln, now)
            if st == "spent":
                reason = "usedUp"
                section = "usedUp"
                sev = "critical"
                note = "resetCompact" if resets_dt else ""
                tier = 2
                closeness = resets_dt.timestamp() if resets_dt else math.inf
            elif line_sev != "normal":
                reason = "low"
                section = "runningOut"
                sev = line_sev
                if rw is not None:
                    tier = 0
                    closeness = rw
                else:
                    tier = 1
                    closeness = ln.remaining_fraction
                note = "reset" if resets_dt else ""
            elif (not is_unified) and burning_fast_priority and burning_tracker.is_burning(mid, ln, now) and rw is not None:
                reason = "burningFast"
                section = "runningOut"
                sev = "normal"
                tier = 0
                closeness = rw
                if view["runoutAt"]:
                    note = "outIn"
                else:
                    note = "reset" if resets_dt else ""
            elif (not is_unified) and use_it_or_lose_it_priority and is_unused_near_reset(ln, now) and resets_dt is not None:
                reason = "useItOrLoseIt"
                section = "useItOrLoseIt"
                sev = "normal"
                tier = 3
                closeness = resets_dt.timestamp()
                note = "resetRelative"

        if reason is not None:
            pitem = dict(view)
            pitem["priorityReason"] = reason
            pitem["prioritySeverity"] = sev
            pitem["noteKind"] = note
            ranked_items.append(((tier, closeness, offset), section, pitem))
            item_by_metric_id[mid] = pitem

        if is_pinned:
            pinned_entry = dict(item_by_metric_id.get(mid) or view)
            if mid not in item_by_metric_id:
                pinned_entry["priorityReason"] = "pinned"
                pinned_entry["prioritySeverity"] = view["severity"]
                pinned_entry["noteKind"] = "reset" if resets_dt else ""
            cids = view.get("copyIds") or [mid]
            matched_ranks = [pin_rank[cid] for cid in cids if cid in pin_rank]
            pkey = (min(matched_ranks) if matched_ranks else len(pin_rank), offset)
            pinned_bar_ranked.append((pkey, pinned_entry))
            if section != "usedUp":
                pinned_rows_ranked.append((pkey, pinned_entry))

    pinned_bar_ranked.sort(key=lambda pair: pair[0])
    pinned_rows_ranked.sort(key=lambda pair: pair[0])
    pinned_bar = [it for _, it in pinned_bar_ranked]
    pinned_rows = [it for _, it in pinned_rows_ranked]

    ranked_items.sort(key=lambda pair: pair[0])
    ranked = [(section, item) for _, section, item in ranked_items]

    running_out = [it for section, it in ranked if section == "runningOut"]
    attention = [it for it in running_out if not it["isPinned"]]
    use_it_or_lose_it = [it for section, it in ranked if section == "useItOrLoseIt" and not it["isPinned"]]
    used_up = [it for section, it in ranked if section == "usedUp"]

    awaiting_usage = len(accounts) == 0 or len(waiting_ids) > 0
    shows_clear_row = (not awaiting_usage) and len(notices) == 0 and len(running_out) == 0

    if ranked:
        summary = "listed"
    elif notices:
        summary = "unavailable"
    elif awaiting_usage:
        summary = "waitingForUsage"
    elif any(ln.is_bounded for _, ln, _, _ in dashboard_lines):
        summary = "allClear"
    else:
        summary = "noReportedLimits"

    # -----------------------------------------------------------------------
    # MenuBarContentBuilder (Top Bar Strip)
    # -----------------------------------------------------------------------
    def _serialize_bar_item(it: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "metricId": it["metricId"],
            "kind": it["kind"],
            "icon": it["icon"],
            "badge": it["badge"] or "",
            "shortTitle": it["shortTitle"],
            "windowKind": it.get("windowKind", ""),
            "windowTag": it.get("windowTag", ""),
            "value": it["trayValue"],
            "severity": it["prioritySeverity"],
            "reason": it["priorityReason"],
            "providerName": it["providerName"],
            "accountName": it["accountName"],
            "title": it["title"],
            "amountText": it["amountText"],
            "resetsAt": it["resetsAt"],
        }

    bar_items: List[Dict[str, Any]] = []
    all_bar_items: List[Dict[str, Any]] = []
    overflow = 0
    if not privacy_mode and max_bar_items > 0:
        if always_show_in_bar:
            counted = list(pinned_bar) + list(attention) + list(use_it_or_lose_it)
            used_up_pool = [it for it in used_up if not it["isPinned"]]
        elif pinned_bar:
            counted = list(pinned_bar)
            used_up_pool = []
        else:
            counted = [it for it in attention if it.get("priorityReason") == "low"]
            used_up_pool = []
        eligible = counted + used_up_pool
        shown = eligible[:max_bar_items]
        overflow = max(0, len(counted) - len(shown))
        bar_items = [_serialize_bar_item(it) for it in shown]
        all_bar_items = [_serialize_bar_item(it) for it in eligible]

    bar_alarming = any(it["severity"] == "critical" for it in bar_items) or len(notices) > 0

    return {
        "hasAccounts": len(accounts) > 0,
        "accountCount": len(accounts),
        "allUsage": all_usage_cards,
        "hiddenMetrics": hidden_metrics,
        "priority": {
            "summary": summary,
            "showsClearRow": shows_clear_row,
            "pinned": pinned_rows,
            "runningOut": attention,
            "useItOrLoseIt": use_it_or_lose_it,
            "notices": notices,
            "usedUp": used_up,
        },
        "bar": {
            "iconOnly": privacy_mode or max_bar_items == 0 or len(bar_items) == 0,
            "items": bar_items,
            "allItems": all_bar_items,
            "overflow": overflow,
            "alarming": bar_alarming,
            "privacyMode": privacy_mode,
        },
    }
