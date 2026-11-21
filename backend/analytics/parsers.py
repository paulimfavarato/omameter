"""Local harness parsers for Claude Code, GPT Codex, OpenCode, and Antigravity.

Reads strictly from locally installed harnesses (and standard harness env vars
or user-configured extra directories) with zero dependency on external services.
Uses Python 3 stdlib only, with incremental byte-offset + guard-hash resumption
for JSONL transcripts and read-only SQLite + pure-Python protobuf decoding.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from backend.analytics.db import ScanFileState, TokenEvent

try:
    from compression import zstd as _stdlib_zstd  # Python 3.14+
except ImportError:
    _stdlib_zstd = None

GUARD_LENGTH = 64
PARSER_VERSION_CLAUDE = 1
PARSER_VERSION_CODEX = 1
PARSER_VERSION_OPENCODE = 1
PARSER_VERSION_ANTIGRAVITY = 1
MAX_CODEX_RECENT_FPS = 64

HARNESS_ORDER = ("claude", "codex", "antigravity", "opencode")


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------

def _digest(*parts: Any) -> str:
    h = hashlib.sha256()
    for p in parts:
        if isinstance(p, (dict, list)):
            raw = json.dumps(p, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        else:
            raw = str(p if p is not None else "")
        h.update(raw.encode("utf-8", errors="replace"))
        h.update(b"\x1f")
    return h.hexdigest()[:32]


def _int_val(val: Any) -> int:
    if isinstance(val, bool) or val is None:
        return 0
    if isinstance(val, int):
        return max(0, val)
    if isinstance(val, float):
        return max(0, int(val)) if math.isfinite(val) else 0
    try:
        f = float(str(val).strip())
        return max(0, int(f)) if math.isfinite(f) else 0
    except (ValueError, TypeError):
        return 0


def _float_pos(val: Any) -> Optional[float]:
    if isinstance(val, bool) or val is None:
        return None
    try:
        f = float(val)
        return f if math.isfinite(f) and f > 0.0 else None
    except (ValueError, TypeError):
        return None


def parse_timestamp_sec(raw: Any) -> int:
    if raw is None or isinstance(raw, bool):
        return 0
    if isinstance(raw, (int, float)):
        v = float(raw)
        if not math.isfinite(v) or v <= 0:
            return 0
        if v > 1e12:
            v /= 1000.0
        return int(v)
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return 0
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0, int(dt.timestamp()))
        except ValueError:
            try:
                return parse_timestamp_sec(float(s))
            except ValueError:
                return 0
    return 0


def fnv1a_32(data: bytes) -> int:
    h = 0x811C9DC5
    for b in data:
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def _normalize_speed_tier(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    if s in ("ultrafast", "ultra_fast", "ultra-fast"):
        return "ultrafast"
    if s in ("fast", "priority"):
        return "fast"
    return "standard"


def _expand_dirs(paths: Sequence[str]) -> List[Path]:
    out: List[Path] = []
    seen = set()
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip():
            continue
        for part in raw.split(","):
            s = part.strip()
            if not s:
                continue
            p = Path(s).expanduser()
            key = str(p)
            if key not in seen:
                seen.add(key)
                out.append(p)
    return out


# ---------------------------------------------------------------------------
# Harness directory discovery (installed local harnesses; T3 Code only on opt-in)
# ---------------------------------------------------------------------------

# T3 Code runs Antigravity with an isolated data dir per provider instance:
# ~/.t3/userdata/providers/antigravity/<instance>/antigravity-acp/conversations/*.db
# This is the ONLY part of ~/.t3 analytics may touch, and only when the user turns on
# `includeT3Antigravity`. Without the opt-in, nothing under ~/.t3 is listed or read.
T3_ANTIGRAVITY_SUBPATH = (".t3", "userdata", "providers", "antigravity")


def t3_antigravity_base(home: Optional[Path] = None) -> Path:
    h = home if home is not None else Path.home()
    return h.joinpath(*T3_ANTIGRAVITY_SUBPATH)


def t3_antigravity_available(home: Optional[Path] = None) -> bool:
    """Existence check only (no listing, no reads) used to offer the opt-in."""
    try:
        return t3_antigravity_base(home).is_dir()
    except OSError:
        return False


def t3_antigravity_roots(home: Optional[Path] = None) -> List[Path]:
    """Conversation dirs of every T3 Code Antigravity instance (call only after opt-in)."""
    base = t3_antigravity_base(home)
    try:
        instances = sorted(d for d in base.iterdir() if d.is_dir() and not d.is_symlink())
    except OSError:
        return []
    return [d / "antigravity-acp" / "conversations" for d in instances]


def discover_harness_roots(
    custom_paths: Optional[Dict[str, List[str]]] = None,
    *,
    home: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    include_t3_antigravity: bool = False,
) -> Dict[str, List[Path]]:
    """Return candidate directories/files for each priority harness."""
    h = home if home is not None else Path.home()
    e = env if env is not None else dict(os.environ)
    custom = custom_paths if isinstance(custom_paths, dict) else {}

    def _dedupe(items: Iterable[Path]) -> List[Path]:
        res: List[Path] = []
        seen = set()
        for p in items:
            try:
                key = str(p.resolve()) if p.exists() else str(p)
            except OSError:
                key = str(p)
            if key not in seen:
                seen.add(key)
                res.append(p)
        return res

    # 1. Claude Code
    claude_roots: List[Path] = []
    if e.get("CLAUDE_CONFIG_DIR"):
        for base in _expand_dirs([e["CLAUDE_CONFIG_DIR"]]):
            claude_roots.append(base / "projects" if (base / "projects").is_dir() else base)
    claude_roots.append(h / ".claude" / "projects")
    for p in _expand_dirs(custom.get("claude") or []):
        claude_roots.append(p / "projects" if (p / "projects").is_dir() else p)

    # 2. GPT Codex
    codex_roots: List[Path] = []
    codex_bases: List[Path] = []
    if e.get("CODEX_HOME"):
        codex_bases.extend(_expand_dirs([e["CODEX_HOME"]]))
    codex_bases.append(h / ".codex")
    for base in codex_bases:
        codex_roots.append(base / "sessions")
        codex_roots.append(base / "archived_sessions")
    for p in _expand_dirs(custom.get("codex") or []):
        if (p / "sessions").is_dir() or (p / "archived_sessions").is_dir():
            if (p / "sessions").is_dir():
                codex_roots.append(p / "sessions")
            if (p / "archived_sessions").is_dir():
                codex_roots.append(p / "archived_sessions")
        else:
            codex_roots.append(p)

    # 3. OpenCode
    opencode_roots: List[Path] = []
    if e.get("OPENCODE_DATA_DIR"):
        opencode_roots.extend(_expand_dirs([e["OPENCODE_DATA_DIR"]]))
    xdg_data = Path(e.get("XDG_DATA_HOME") or (h / ".local" / "share")).expanduser()
    opencode_roots.append(xdg_data / "opencode")
    opencode_roots.extend(_expand_dirs(custom.get("opencode") or []))

    # 4. Antigravity (CLI, desktop and ACP standard local paths; T3 Code only on opt-in)
    ag_roots: List[Path] = []
    if e.get("ANTIGRAVITY_DATA_DIR"):
        for base in _expand_dirs([e["ANTIGRAVITY_DATA_DIR"]]):
            ag_roots.append(base / "conversations" if (base / "conversations").is_dir() else base)
    ag_roots.append(h / ".gemini" / "antigravity-cli" / "conversations")
    ag_roots.append(h / ".gemini" / "antigravity" / "conversations")
    ag_roots.append(h / ".gemini" / "antigravity-acp" / "conversations")
    for p in _expand_dirs(custom.get("antigravity") or []):
        ag_roots.append(p / "conversations" if (p / "conversations").is_dir() else p)
    if include_t3_antigravity:
        ag_roots.extend(t3_antigravity_roots(h))

    return {
        "claude": _dedupe(claude_roots),
        "codex": _dedupe(codex_roots),
        "antigravity": _dedupe(ag_roots),
        "opencode": _dedupe(opencode_roots),
    }


def _file_sig(path: Path) -> Optional[Tuple[int, int]]:
    """Return (size, mtime_ns) accounting for SQLite -wal sidecar if present."""
    try:
        st = path.stat()
    except OSError:
        return None
    size = st.st_size
    mtime_ns = st.st_mtime_ns
    wal = Path(str(path) + "-wal")
    try:
        if wal.is_file():
            wst = wal.stat()
            size += wst.st_size
            mtime_ns = max(mtime_ns, wst.st_mtime_ns)
    except OSError:
        pass
    return size, mtime_ns


# ---------------------------------------------------------------------------
# Incremental JSONL reader (offset + 64-byte FNV-1a guard hash)
# ---------------------------------------------------------------------------

def _read_jsonl_incremental(
    path: Path,
    cached: Optional[ScanFileState],
    parser_version: int,
) -> Optional[Tuple[List[bytes], int, int, int, int, bool]]:
    """Read new complete lines from a JSONL file using offset + guard hash.

    Returns None when unchanged, or
    `(lines, size, mtime_ns, new_offset, new_guard_hash, resumed)`.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    size = st.st_size
    mtime_ns = st.st_mtime_ns

    if (
        cached is not None
        and cached.parser_version == parser_version
        and cached.size == size
        and cached.mtime_ns == mtime_ns
    ):
        return None

    # Compressed .jsonl.zst is parsed whole when supported.
    if path.name.endswith(".jsonl.zst"):
        if _stdlib_zstd is None:
            return None
        try:
            raw = _stdlib_zstd.decompress(path.read_bytes())
        except Exception:
            return None
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        return lines, size, mtime_ns, size, 0, False

    resumed = False
    start_offset = 0
    if (
        cached is not None
        and cached.parser_version == parser_version
        and 0 < cached.resume_offset <= size
    ):
        try:
            with path.open("rb") as fh:
                guard_len = min(GUARD_LENGTH, cached.resume_offset)
                fh.seek(cached.resume_offset - guard_len)
                guard_bytes = fh.read(guard_len)
                if len(guard_bytes) == guard_len and fnv1a_32(guard_bytes) == cached.guard_hash:
                    start_offset = cached.resume_offset
                    resumed = True
        except OSError:
            start_offset = 0
            resumed = False

    try:
        with path.open("rb") as fh:
            fh.seek(start_offset)
            chunk = fh.read()
    except OSError:
        return None

    # Only commit complete newline-terminated lines so an in-progress write is
    # safely re-read on the next scan.
    last_nl = chunk.rfind(b"\n")
    if last_nl == -1:
        # No complete line yet since start_offset
        return [], size, mtime_ns, start_offset, (cached.guard_hash if resumed and cached else 0), resumed

    complete = chunk[: last_nl + 1]
    new_offset = start_offset + len(complete)

    # Compute guard hash over the last GUARD_LENGTH bytes before new_offset
    if len(complete) >= GUARD_LENGTH:
        new_guard_hash = fnv1a_32(complete[-GUARD_LENGTH:])
    else:
        try:
            with path.open("rb") as fh:
                glen = min(GUARD_LENGTH, new_offset)
                fh.seek(new_offset - glen)
                new_guard_hash = fnv1a_32(fh.read(glen))
        except OSError:
            new_guard_hash = 0

    lines = [ln for ln in complete.splitlines() if ln.strip()]
    return lines, size, mtime_ns, new_offset, new_guard_hash, resumed


# ---------------------------------------------------------------------------
# Parser 1: Claude Code (~/.claude/projects/**/*.jsonl)
# ---------------------------------------------------------------------------

def scan_claude_file(
    path: Path,
    source_dir: str,
    cached: Optional[ScanFileState],
    now_ts: int,
) -> Optional[Tuple[List[TokenEvent], ScanFileState]]:
    read_res = _read_jsonl_incremental(path, cached, PARSER_VERSION_CLAUDE)
    if read_res is None:
        return None
    lines, size, mtime_ns, new_offset, new_guard_hash, _resumed = read_res
    events: List[TokenEvent] = []
    fallback_session = path.stem

    for raw in lines:
        if b'"usage"' not in raw:
            continue
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(item, dict) or item.get("type") != "assistant":
            continue
        msg = item.get("message")
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict) or not usage:
            continue
        model = str(msg.get("model") or "unknown").strip() or "unknown"
        if model == "<synthetic>":
            continue

        ts = parse_timestamp_sec(item.get("timestamp"))
        if ts <= 0:
            ts = int(mtime_ns // 1_000_000_000)

        session_id = str(item.get("sessionId") or item.get("session_id") or fallback_session)
        project = str(item.get("cwd") or "").strip()
        msg_id = msg.get("id") or item.get("uuid")
        req_id = item.get("requestId")
        if msg_id:
            event_id = _digest("claude", msg_id, req_id or "")
        else:
            event_id = _digest("claude", session_id, ts, model, usage)

        cache_creation = usage.get("cache_creation")
        cache_write_1h = (
            _int_val(cache_creation.get("ephemeral_1h_input_tokens"))
            if isinstance(cache_creation, dict)
            else 0
        )
        out_details = usage.get("output_tokens_details")
        reasoning = (
            _int_val(out_details.get("thinking_tokens"))
            if isinstance(out_details, dict)
            else 0
        )

        speed = _normalize_speed_tier(usage.get("speed") or usage.get("service_tier"))
        inp = _int_val(usage.get("input_tokens"))
        outp = _int_val(usage.get("output_tokens"))
        cread = _int_val(usage.get("cache_read_input_tokens"))
        cwrite = _int_val(usage.get("cache_creation_input_tokens"))
        if inp == 0 and outp == 0 and cread == 0 and cwrite == 0:
            continue

        events.append(
            TokenEvent(
                event_id=event_id,
                provider="claude",
                route="anthropic",
                source_dir=source_dir,
                session_id=session_id,
                ts=ts,
                model=model,
                project=project,
                client="Claude Code",
                input_tokens=inp,
                output_tokens=outp,
                cache_read_tokens=cread,
                cache_write_tokens=cwrite,
                cache_write_1h_tokens=min(cwrite, cache_write_1h),
                reasoning_tokens=reasoning,
                speed_tier=speed,
                reported_cost_usd=None,
                time_precision="event",
            )
        )

    state = ScanFileState(
        path=str(path),
        provider="claude",
        size=size,
        mtime_ns=mtime_ns,
        resume_offset=new_offset,
        guard_hash=new_guard_hash,
        parser_state={},
        parser_version=PARSER_VERSION_CLAUDE,
        last_scanned_at=now_ts,
    )
    return events, state


# ---------------------------------------------------------------------------
# Parser 2: GPT Codex (~/.codex/sessions/**/*.jsonl)
# ---------------------------------------------------------------------------

def scan_codex_file(
    path: Path,
    source_dir: str,
    cached: Optional[ScanFileState],
    now_ts: int,
) -> Optional[Tuple[List[TokenEvent], ScanFileState]]:
    read_res = _read_jsonl_incremental(path, cached, PARSER_VERSION_CODEX)
    if read_res is None:
        return None
    lines, size, mtime_ns, new_offset, new_guard_hash, resumed = read_res

    st = dict(cached.parser_state) if (resumed and cached is not None) else {}
    session = str(st.get("session") or path.stem)
    project = str(st.get("project") or "")
    client = str(st.get("client") or "Codex CLI")
    model = str(st.get("model") or "unknown")
    route = str(st.get("route") or "openai")
    started = int(st.get("started") or 0)
    have_meta = bool(st.get("have_meta"))
    tier = str(st.get("tier") or "standard")
    pending_tier: Optional[str] = st.get("pending_tier")
    turn_usage = int(st.get("turn_usage") or 0)
    recent_fps: List[str] = list(st.get("recent_fps") or [])
    seen_fps = set(recent_fps)

    events: List[TokenEvent] = []

    for raw in lines:
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = item.get("type")

        if kind == "session_meta":
            # Forked sessions embed parent session_meta later; keep the child header.
            if have_meta:
                continue
            have_meta = True
            session = str(payload.get("id") or payload.get("session_id") or session)
            project = str(payload.get("cwd") or project).strip()
            orig = str(payload.get("originator") or "").lower()
            client = "Codex Desktop" if "desktop" in orig else "Codex CLI"
            mp = str(payload.get("model_provider") or "").strip().lower()
            if mp:
                route = mp
            started = parse_timestamp_sec(payload.get("timestamp"))

        elif kind == "turn_context":
            m = payload.get("model") or payload.get("model_slug")
            if isinstance(m, str) and m.strip():
                model = m.strip()
            cwd = payload.get("cwd")
            if isinstance(cwd, str) and cwd.strip():
                project = cwd.strip()
            if payload.get("service_tier") is not None:
                tier = _normalize_speed_tier(payload.get("service_tier"))

        elif kind == "event_msg":
            ptype = payload.get("type")
            if ptype == "task_started":
                ts = parse_timestamp_sec(item.get("timestamp"))
                if started and ts and ts < started:
                    continue
                if pending_tier is not None:
                    tier = pending_tier
                    pending_tier = None
                turn_usage = 0

            elif ptype == "thread_settings_applied":
                ts = parse_timestamp_sec(item.get("timestamp"))
                if started and ts and ts < started:
                    continue
                tset = payload.get("thread_settings")
                if not isinstance(tset, dict):
                    continue
                mp = str(tset.get("model_provider_id") or route).strip().lower() or "openai"
                val = _normalize_speed_tier(tset.get("service_tier")) if mp == "openai" else "standard"
                if turn_usage == 0:
                    tier = val
                    pending_tier = None
                elif val != (pending_tier if pending_tier is not None else tier):
                    tier = "standard"
                    pending_tier = val

            elif ptype == "token_count":
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                last_u = info.get("last_token_usage")
                if not isinstance(last_u, dict) or not last_u:
                    continue
                ts = parse_timestamp_sec(item.get("timestamp"))
                if ts <= 0 or (started and ts < started):
                    continue
                total_u = info.get("total_token_usage")
                fp = _digest(total_u) if isinstance(total_u, dict) and total_u else _digest(ts, last_u)
                if fp in seen_fps:
                    continue
                seen_fps.add(fp)
                recent_fps.append(fp)
                if len(recent_fps) > MAX_CODEX_RECENT_FPS:
                    dropped = recent_fps.pop(0)
                    seen_fps.discard(dropped)
                turn_usage += 1

                cached_read = _int_val(last_u.get("cached_input_tokens"))
                cache_write = _int_val(last_u.get("cache_write_input_tokens"))
                raw_input = _int_val(last_u.get("input_tokens"))
                uncached_in = max(0, raw_input - cached_read - cache_write)
                outp = _int_val(last_u.get("output_tokens"))
                reasoning = _int_val(last_u.get("reasoning_output_tokens"))
                if uncached_in == 0 and outp == 0 and cached_read == 0 and cache_write == 0:
                    continue

                events.append(
                    TokenEvent(
                        event_id=_digest("codex", session, fp),
                        provider="codex",
                        route=route or "openai",
                        source_dir=source_dir,
                        session_id=session,
                        ts=ts,
                        model=model,
                        project=project,
                        client=client,
                        input_tokens=uncached_in,
                        output_tokens=outp,
                        cache_read_tokens=cached_read,
                        cache_write_tokens=cache_write,
                        cache_write_1h_tokens=0,
                        reasoning_tokens=reasoning,
                        speed_tier=tier,
                        reported_cost_usd=None,
                        time_precision="event",
                    )
                )

    next_state = ScanFileState(
        path=str(path),
        provider="codex",
        size=size,
        mtime_ns=mtime_ns,
        resume_offset=new_offset,
        guard_hash=new_guard_hash,
        parser_state={
            "session": session,
            "project": project,
            "client": client,
            "model": model,
            "route": route,
            "started": started,
            "have_meta": have_meta,
            "tier": tier,
            "pending_tier": pending_tier,
            "turn_usage": turn_usage,
            "recent_fps": recent_fps,
        },
        parser_version=PARSER_VERSION_CODEX,
        last_scanned_at=now_ts,
    )
    return events, next_state


# ---------------------------------------------------------------------------
# Parser 3: OpenCode (~/.local/share/opencode/opencode.db + legacy JSON)
# ---------------------------------------------------------------------------

def _opencode_event_from_data(
    *,
    mid: str,
    sid: str,
    ts_fallback: int,
    project: str,
    data: Dict[str, Any],
    session_model: Optional[Dict[str, Any]],
    source_dir: str,
) -> Optional[TokenEvent]:
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return None
    inp = _int_val(tokens.get("input"))
    out_base = _int_val(tokens.get("output"))
    reasoning = _int_val(tokens.get("reasoning"))
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    cread = _int_val(cache.get("read"))
    cwrite = _int_val(cache.get("write"))
    # OpenCode tracks reasoning disjointly from output.
    output_total = out_base + reasoning
    if inp == 0 and output_total == 0 and cread == 0 and cwrite == 0:
        return None

    # Model & route can be top-level (v1) or nested under `model` dict (v2).
    model_obj = data.get("model") if isinstance(data.get("model"), dict) else None
    if model_obj is None and isinstance(session_model, dict):
        model_obj = session_model
    model_id = (
        str(data.get("modelID") or (model_obj.get("id") if model_obj else "") or "unknown").strip()
        or "unknown"
    )
    route = (
        str(data.get("providerID") or (model_obj.get("providerID") if model_obj else "") or "opencode").strip()
        or "opencode"
    )

    time_obj = data.get("time") if isinstance(data.get("time"), dict) else {}
    ts = parse_timestamp_sec(time_obj.get("completed") or time_obj.get("created")) or ts_fallback

    path_obj = data.get("path") if isinstance(data.get("path"), dict) else {}
    proj = project or str(path_obj.get("cwd") or path_obj.get("root") or "").strip()

    return TokenEvent(
        event_id=_digest("opencode", mid),
        provider="opencode",
        route=route,
        source_dir=source_dir,
        session_id=sid or "opencode",
        ts=ts,
        model=model_id,
        project=proj,
        client="OpenCode",
        input_tokens=inp,
        output_tokens=output_total,
        cache_read_tokens=cread,
        cache_write_tokens=cwrite,
        cache_write_1h_tokens=0,
        reasoning_tokens=reasoning,
        speed_tier="standard",
        reported_cost_usd=_float_pos(data.get("cost")),
        time_precision="event",
    )


def scan_opencode_db(
    db_path: Path,
    source_dir: str,
    cached: Optional[ScanFileState],
    now_ts: int,
) -> Optional[Tuple[List[TokenEvent], ScanFileState]]:
    sig = _file_sig(db_path)
    if sig is None:
        return None
    size, mtime_ns = sig
    if (
        cached is not None
        and cached.parser_version == PARSER_VERSION_OPENCODE
        and cached.size == size
        and cached.mtime_ns == mtime_ns
    ):
        return None

    events: List[TokenEvent] = []
    seen_ids = set()
    try:
        conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3.0)
    except sqlite3.Error:
        return None
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

        # Map session_id -> (project_dir, session_model_dict)
        sessions: Dict[str, Tuple[str, Optional[Dict[str, Any]]]] = {}
        for tbl in ("session", "session_v2"):
            if tbl not in tables:
                continue
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({tbl})")}
            dir_col = "directory" if "directory" in cols else ("path" if "path" in cols else "''")
            model_col = "model" if "model" in cols else "NULL"
            try:
                for sid, sdir, smodel in conn.execute(f"SELECT id, {dir_col}, {model_col} FROM {tbl}"):
                    parsed_m = None
                    if isinstance(smodel, str) and smodel.strip().startswith("{"):
                        try:
                            parsed_m = json.loads(smodel)
                        except ValueError:
                            parsed_m = None
                    sessions[str(sid)] = (str(sdir or "").strip(), parsed_m)
            except sqlite3.Error:
                continue

        # Table 1: OpenCode v2 `session_message`
        if "session_message" in tables:
            try:
                for mid, sid, t_created, raw in conn.execute(
                    "SELECT id, session_id, time_created, data FROM session_message WHERE type = 'assistant'"
                ):
                    mid_s = str(mid)
                    if mid_s in seen_ids or not raw:
                        continue
                    try:
                        data = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(data, dict):
                        continue
                    proj, smodel = sessions.get(str(sid), ("", None))
                    ev = _opencode_event_from_data(
                        mid=mid_s,
                        sid=str(sid),
                        ts_fallback=parse_timestamp_sec(t_created),
                        project=proj,
                        data=data,
                        session_model=smodel,
                        source_dir=source_dir,
                    )
                    if ev is not None:
                        seen_ids.add(mid_s)
                        events.append(ev)
            except sqlite3.Error:
                pass

        # Table 2: OpenCode v1 `message`
        if "message" in tables:
            try:
                for mid, sid, t_created, raw in conn.execute(
                    "SELECT id, session_id, time_created, data FROM message"
                ):
                    mid_s = str(mid)
                    if mid_s in seen_ids or not raw:
                        continue
                    try:
                        data = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(data, dict) or data.get("role") != "assistant":
                        continue
                    proj, smodel = sessions.get(str(sid), ("", None))
                    ev = _opencode_event_from_data(
                        mid=mid_s,
                        sid=str(sid),
                        ts_fallback=parse_timestamp_sec(t_created),
                        project=proj,
                        data=data,
                        session_model=smodel,
                        source_dir=source_dir,
                    )
                    if ev is not None:
                        seen_ids.add(mid_s)
                        events.append(ev)
            except sqlite3.Error:
                pass
    finally:
        conn.close()

    state = ScanFileState(
        path=str(db_path),
        provider="opencode",
        size=size,
        mtime_ns=mtime_ns,
        resume_offset=0,
        guard_hash=0,
        parser_state={},
        parser_version=PARSER_VERSION_OPENCODE,
        last_scanned_at=now_ts,
    )
    return events, state


def scan_opencode_legacy_json(
    path: Path,
    source_dir: str,
    cached: Optional[ScanFileState],
    now_ts: int,
) -> Optional[Tuple[List[TokenEvent], ScanFileState]]:
    try:
        st = path.stat()
    except OSError:
        return None
    if (
        cached is not None
        and cached.parser_version == PARSER_VERSION_OPENCODE
        and cached.size == st.st_size
        and cached.mtime_ns == st.st_mtime_ns
    ):
        return None

    events: List[TokenEvent] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict) and data.get("role") == "assistant":
        mid = str(data.get("id") or path.stem)
        sid = str(data.get("sessionID") or path.parent.name)
        ev = _opencode_event_from_data(
            mid=mid,
            sid=sid,
            ts_fallback=int(st.st_mtime_ns // 1_000_000_000),
            project="",
            data=data,
            session_model=None,
            source_dir=source_dir,
        )
        if ev is not None:
            events.append(ev)

    state = ScanFileState(
        path=str(path),
        provider="opencode",
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
        resume_offset=0,
        guard_hash=0,
        parser_state={},
        parser_version=PARSER_VERSION_OPENCODE,
        last_scanned_at=now_ts,
    )
    return events, state


# ---------------------------------------------------------------------------
# Parser 4: Antigravity (local SQLite conversations + protobuf gen_metadata)
# ---------------------------------------------------------------------------

def proto_fields(data: bytes) -> Iterable[Tuple[int, int, Any]]:
    """Minimal pure-Python protobuf wire-format iterator (stdlib only)."""
    index = 0
    length_total = len(data)

    def read_varint() -> int:
        nonlocal index
        value = 0
        for shift in range(0, 70, 7):
            if index >= length_total:
                raise ValueError("Truncated protobuf varint")
            b = data[index]
            index += 1
            value |= (b & 0x7F) << shift
            if b < 0x80:
                return value
        raise ValueError("Invalid protobuf varint")

    while index < length_total:
        key = read_varint()
        field_num = key >> 3
        wire_type = key & 0x07
        if field_num <= 0:
            raise ValueError("Invalid protobuf field number")
        if wire_type == 0:
            val: Any = read_varint()
        elif wire_type == 2:
            size = read_varint()
            if size < 0 or index + size > length_total:
                raise ValueError("Truncated length-delimited field")
            val = data[index : index + size]
            index += size
        elif wire_type == 1:
            if index + 8 > length_total:
                raise ValueError("Truncated 64-bit field")
            val = data[index : index + 8]
            index += 8
        elif wire_type == 5:
            if index + 4 > length_total:
                raise ValueError("Truncated 32-bit field")
            val = data[index : index + 4]
            index += 4
        else:
            raise ValueError(f"Unsupported protobuf wire type {wire_type}")
        yield field_num, wire_type, val


def _pb_string_map(entries: Sequence[bytes]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for entry in entries:
        k = v = None
        for fnum, wtype, val in proto_fields(entry):
            if fnum == 1 and wtype == 2:
                k = val
            elif fnum == 2 and wtype == 2:
                v = val
        if isinstance(k, bytes) and isinstance(v, bytes):
            out[k.decode("utf-8", errors="replace")] = v.decode("utf-8", errors="replace")
    return out


def parse_antigravity_generation(blob: bytes) -> Optional[Tuple[Dict[int, int], str, Optional[str]]]:
    """Decode one `gen_metadata` protobuf blob -> `(usage_counts, model, request_id)`."""
    generation = None
    for fnum, wtype, val in proto_fields(blob):
        if fnum == 1 and wtype == 2:
            generation = val
            break
    if generation is None:
        return None

    usage_blob = None
    served_model = None
    requested_model = None
    map_entries: List[bytes] = []
    for fnum, wtype, val in proto_fields(generation):
        if fnum == 4 and wtype == 2:
            usage_blob = val
        elif fnum == 19 and wtype == 2:
            served_model = val.decode("utf-8", errors="replace").strip()
        elif fnum == 21 and wtype == 2:
            requested_model = val.decode("utf-8", errors="replace").strip()
        elif fnum == 20 and wtype == 2:
            map_entries.append(val)
    if usage_blob is None:
        return None
    counts = {fnum: int(val) for fnum, wtype, val in proto_fields(usage_blob) if wtype == 0}
    if not counts:
        return None
    req_id = _pb_string_map(map_entries).get("request_id") or None
    return counts, (served_model or requested_model or "unknown"), req_id


def _antigravity_step_time(metadata: Optional[bytes]) -> int:
    if not metadata:
        return 0
    for fnum, wtype, val in proto_fields(metadata):
        if fnum == 1 and wtype == 2:
            for f2, w2, v2 in proto_fields(val):
                if f2 == 1 and w2 == 0:
                    return int(v2)
    return 0


def _antigravity_project(conn: sqlite3.Connection) -> str:
    for (data,) in conn.execute("SELECT data FROM trajectory_metadata_blob"):
        if not data:
            continue
        candidate = ""
        for fnum, wtype, val in proto_fields(data):
            if fnum == 7 and wtype == 2:
                candidate = val.decode("utf-8", errors="replace").strip()
                break
            if fnum == 1 and wtype == 2:
                for f2, w2, v2 in proto_fields(val):
                    if f2 == 1 and w2 == 2:
                        candidate = v2.decode("utf-8", errors="replace").strip()
        if candidate:
            if candidate.startswith("file://"):
                return urllib.parse.unquote(candidate[len("file://") :])
            return candidate
    return ""


def _is_sqlite_file(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def scan_antigravity_db(
    db_path: Path,
    source_dir: str,
    cached: Optional[ScanFileState],
    now_ts: int,
) -> Optional[Tuple[List[TokenEvent], ScanFileState]]:
    sig = _file_sig(db_path)
    if sig is None:
        return None
    size, mtime_ns = sig
    if (
        cached is not None
        and cached.parser_version == PARSER_VERSION_ANTIGRAVITY
        and cached.size == size
        and cached.mtime_ns == mtime_ns
    ):
        return None
    if not _is_sqlite_file(db_path):
        return None

    events: List[TokenEvent] = []
    try:
        conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3.0)
    except sqlite3.Error:
        return None
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "gen_metadata" not in tables:
            return [], ScanFileState(
                path=str(db_path),
                provider="antigravity",
                size=size,
                mtime_ns=mtime_ns,
                resume_offset=0,
                guard_hash=0,
                parser_state={},
                parser_version=PARSER_VERSION_ANTIGRAVITY,
                last_scanned_at=now_ts,
            )

        session_id = db_path.stem
        try:
            project = _antigravity_project(conn) if "trajectory_metadata_blob" in tables else ""
        except (ValueError, sqlite3.Error):
            project = ""

        step_times: List[int] = []
        if "steps" in tables:
            try:
                for (meta_blob,) in conn.execute(
                    "SELECT metadata FROM steps WHERE step_type = 15 ORDER BY idx"
                ):
                    try:
                        step_times.append(_antigravity_step_time(meta_blob))
                    except ValueError:
                        step_times.append(0)
            except sqlite3.Error:
                pass

        fallback_ts = int(mtime_ns // 1_000_000_000)
        for index, (idx, blob) in enumerate(
            conn.execute("SELECT idx, data FROM gen_metadata ORDER BY idx")
        ):
            if not blob:
                continue
            try:
                parsed = parse_antigravity_generation(blob)
            except ValueError:
                continue
            if parsed is None:
                continue
            counts, model, req_id = parsed
            have_time = index < len(step_times) and step_times[index] > 0
            ts = step_times[index] if have_time else fallback_ts
            inp = max(0, counts.get(2, 0))
            outp = max(0, counts.get(3, 0))
            cread = max(0, counts.get(5, 0))
            reasoning = max(0, counts.get(9, 0))
            if inp == 0 and outp == 0 and cread == 0:
                continue

            event_id = (
                _digest("antigravity", req_id)
                if req_id
                else _digest("antigravity", session_id, idx)
            )
            events.append(
                TokenEvent(
                    event_id=event_id,
                    provider="antigravity",
                    route="google",
                    source_dir=source_dir,
                    session_id=session_id,
                    ts=ts,
                    model=model,
                    project=project,
                    client="Antigravity",
                    input_tokens=inp,
                    output_tokens=outp,
                    cache_read_tokens=cread,
                    cache_write_tokens=0,
                    cache_write_1h_tokens=0,
                    reasoning_tokens=reasoning,
                    speed_tier="standard",
                    reported_cost_usd=None,
                    time_precision="event" if have_time else "session",
                )
            )
    finally:
        conn.close()

    state = ScanFileState(
        path=str(db_path),
        provider="antigravity",
        size=size,
        mtime_ns=mtime_ns,
        resume_offset=0,
        guard_hash=0,
        parser_state={},
        parser_version=PARSER_VERSION_ANTIGRAVITY,
        last_scanned_at=now_ts,
    )
    return events, state


# ---------------------------------------------------------------------------
# Discovery & file enumeration per harness
# ---------------------------------------------------------------------------

def enumerate_harness_files(
    provider: str,
    roots: Sequence[Path],
) -> List[Tuple[Path, str]]:
    """Return `(file_path, root_dir_str)` tuples for a harness."""
    found: List[Tuple[Path, str]] = []
    seen = set()

    def _add(p: Path, root: Path) -> None:
        key = str(p)
        if key not in seen:
            seen.add(key)
            found.append((p, str(root)))

    for root in roots:
        if not root.exists():
            continue
        if provider in ("claude", "codex"):
            if root.is_file() and (root.name.endswith(".jsonl") or root.name.endswith(".jsonl.zst")):
                _add(root, root.parent)
            elif root.is_dir():
                for dirpath, _dirnames, filenames in os.walk(root):
                    for fn in filenames:
                        if fn.endswith(".jsonl") or (provider == "codex" and fn.endswith(".jsonl.zst")):
                            _add(Path(dirpath) / fn, root)
        elif provider == "opencode":
            if root.is_file() and root.name.endswith(".db"):
                _add(root, root.parent)
            elif root.is_dir():
                db = root / "opencode.db"
                if db.is_file():
                    _add(db, root)
                legacy_msg = root / "storage" / "message"
                if legacy_msg.is_dir():
                    for dirpath, _dirnames, filenames in os.walk(legacy_msg):
                        for fn in filenames:
                            if fn.endswith(".json"):
                                _add(Path(dirpath) / fn, root)
        elif provider == "antigravity":
            if root.is_file():
                _add(root, root.parent)
            elif root.is_dir():
                for dirpath, _dirnames, filenames in os.walk(root):
                    for fn in filenames:
                        if fn.endswith(("-wal", "-shm", "-journal")):
                            continue
                        p = Path(dirpath) / fn
                        if p.suffix in (".db", ".sqlite", ".sqlite3", ""):
                            _add(p, root)
    return found


def inspect_harness_coverage(
    custom_paths: Optional[Dict[str, List[str]]] = None,
    *,
    home: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    include_t3_antigravity: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """Report which local harness directories exist on this machine (metadata only)."""
    roots = discover_harness_roots(
        custom_paths, home=home, env=env, include_t3_antigravity=include_t3_antigravity
    )
    out: Dict[str, Dict[str, Any]] = {}
    for prov in HARNESS_ORDER:
        existing = [str(p) for p in roots[prov] if p.exists()]
        files = enumerate_harness_files(prov, roots[prov]) if existing else []
        out[prov] = {
            "installed": len(existing) > 0,
            "paths": [str(p) for p in roots[prov]],
            "existingPaths": existing,
            "fileCount": len(files),
        }
    return out
