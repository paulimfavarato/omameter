"""SQLite WAL persistence for Omameter Analytics (Phase 1 quota history + Phase 2 token ledger).

Stores only usage counters, model/project identifiers, and timestamps — never
conversation text or credentials. Uses Python's standard-library `sqlite3`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "omameter"
DEFAULT_DB_PATH = DATA_DIR / "analytics.db"
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class TokenEvent:
    event_id: str
    provider: str
    route: str
    source_dir: str
    session_id: str
    ts: int
    model: str
    project: str
    client: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cache_write_1h_tokens: int
    reasoning_tokens: int
    speed_tier: str = "standard"
    reported_cost_usd: Optional[float] = None
    time_precision: str = "event"


@dataclass(frozen=True)
class QuotaSample:
    ts: int
    account_id: str
    provider: str
    account_name: str
    metric_key: str
    metric_title: str
    window_kind: str
    used_pct: Optional[float]
    used_amount: Optional[float]
    limit_amount: Optional[float]
    unit: str
    resets_at: str


@dataclass(frozen=True)
class ScanFileState:
    path: str
    provider: str
    size: int
    mtime_ns: int
    resume_offset: int
    guard_hash: int
    parser_state: Dict[str, Any]
    parser_version: int
    last_scanned_at: int


class AnalyticsDB:
    """Thread-safe-per-call SQLite wrapper (WAL mode, 0600 permissions)."""

    def __init__(self, path: Path = DEFAULT_DB_PATH) -> None:
        self.path = path
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        conn = sqlite3.connect(str(self.path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _ensure_schema(self) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS quota_samples (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts INTEGER NOT NULL,
                        account_id TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        account_name TEXT NOT NULL,
                        metric_key TEXT NOT NULL,
                        metric_title TEXT NOT NULL,
                        window_kind TEXT NOT NULL,
                        used_pct REAL,
                        used_amount REAL,
                        limit_amount REAL,
                        unit TEXT NOT NULL DEFAULT '',
                        resets_at TEXT NOT NULL DEFAULT ''
                    );
                    CREATE INDEX IF NOT EXISTS idx_quota_samples_ts
                        ON quota_samples(ts);
                    CREATE INDEX IF NOT EXISTS idx_quota_samples_acc_metric_ts
                        ON quota_samples(account_id, metric_key, ts);

                    CREATE TABLE IF NOT EXISTS token_events (
                        event_id TEXT PRIMARY KEY,
                        provider TEXT NOT NULL,
                        route TEXT NOT NULL,
                        source_dir TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        ts INTEGER NOT NULL,
                        model TEXT NOT NULL,
                        project TEXT NOT NULL,
                        client TEXT NOT NULL,
                        input_tokens INTEGER NOT NULL DEFAULT 0,
                        output_tokens INTEGER NOT NULL DEFAULT 0,
                        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                        cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                        cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,
                        reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                        speed_tier TEXT NOT NULL DEFAULT 'standard',
                        reported_cost_usd REAL,
                        time_precision TEXT NOT NULL DEFAULT 'event'
                    );
                    CREATE INDEX IF NOT EXISTS idx_token_events_ts
                        ON token_events(ts);
                    CREATE INDEX IF NOT EXISTS idx_token_events_provider_ts
                        ON token_events(provider, ts);
                    CREATE INDEX IF NOT EXISTS idx_token_events_model_ts
                        ON token_events(model, ts);
                    CREATE INDEX IF NOT EXISTS idx_token_events_project_ts
                        ON token_events(project, ts);
                    CREATE INDEX IF NOT EXISTS idx_token_events_session
                        ON token_events(session_id);

                    CREATE TABLE IF NOT EXISTS scan_files (
                        path TEXT PRIMARY KEY,
                        provider TEXT NOT NULL,
                        size INTEGER NOT NULL,
                        mtime_ns INTEGER NOT NULL,
                        resume_offset INTEGER NOT NULL DEFAULT 0,
                        guard_hash INTEGER NOT NULL DEFAULT 0,
                        parser_state TEXT NOT NULL DEFAULT '{}',
                        parser_version INTEGER NOT NULL DEFAULT 1,
                        last_scanned_at INTEGER NOT NULL DEFAULT 0
                    );
                    """
                )
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Meta
    # ------------------------------------------------------------------

    def get_meta(self, key: str, default: str = "") -> str:
        conn = self._connect()
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return str(row["value"]) if row else default
        finally:
            conn.close()

    def set_meta(self, key: str, value: str) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Phase 1: Quota samples
    # ------------------------------------------------------------------

    def insert_quota_samples(self, samples: Sequence[QuotaSample]) -> int:
        if not samples:
            return 0
        conn = self._connect()
        inserted = 0
        try:
            with conn:
                for s in samples:
                    # Skip duplicate sample within 45s when value & reset did not move
                    last = conn.execute(
                        """
                        SELECT ts, used_pct, used_amount, resets_at
                        FROM quota_samples
                        WHERE account_id = ? AND metric_key = ?
                        ORDER BY ts DESC LIMIT 1
                        """,
                        (s.account_id, s.metric_key),
                    ).fetchone()
                    if last is not None:
                        if (
                            abs(int(last["ts"]) - s.ts) < 45
                            and last["used_pct"] == s.used_pct
                            and last["used_amount"] == s.used_amount
                            and str(last["resets_at"] or "") == s.resets_at
                        ):
                            continue
                    conn.execute(
                        """
                        INSERT INTO quota_samples (
                            ts, account_id, provider, account_name, metric_key,
                            metric_title, window_kind, used_pct, used_amount,
                            limit_amount, unit, resets_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            s.ts,
                            s.account_id,
                            s.provider,
                            s.account_name,
                            s.metric_key,
                            s.metric_title,
                            s.window_kind,
                            s.used_pct,
                            s.used_amount,
                            s.limit_amount,
                            s.unit,
                            s.resets_at,
                        ),
                    )
                    inserted += 1
        finally:
            conn.close()
        return inserted

    def query_quota_samples(
        self,
        *,
        since_ts: int,
        until_ts: Optional[int] = None,
        account_id: Optional[str] = None,
        metric_key: Optional[str] = None,
        limit: int = 5000,
    ) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            clauses = ["ts >= ?"]
            params: List[Any] = [since_ts]
            if until_ts is not None:
                clauses.append("ts <= ?")
                params.append(until_ts)
            if account_id:
                clauses.append("account_id = ?")
                params.append(account_id)
            if metric_key:
                clauses.append("metric_key = ?")
                params.append(metric_key)
            where = " AND ".join(clauses)
            rows = conn.execute(
                f"""
                SELECT ts, account_id, provider, account_name, metric_key,
                       metric_title, window_kind, used_pct, used_amount,
                       limit_amount, unit, resets_at
                FROM quota_samples
                WHERE {where}
                ORDER BY ts ASC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def quota_sample_metrics(self, since_ts: int = 0) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT account_id, provider, account_name, metric_key, metric_title,
                       window_kind, unit, COUNT(*) AS sample_count,
                       MAX(ts) AS last_ts
                FROM quota_samples
                WHERE ts >= ?
                GROUP BY account_id, metric_key
                ORDER BY provider ASC, account_name ASC, metric_title ASC
                """,
                (since_ts,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Phase 2: Incremental scan state & token events
    # ------------------------------------------------------------------

    def get_scan_file(self, path: str) -> Optional[ScanFileState]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM scan_files WHERE path = ?", (path,)).fetchone()
            if row is None:
                return None
            try:
                state = json.loads(row["parser_state"] or "{}")
                if not isinstance(state, dict):
                    state = {}
            except ValueError:
                state = {}
            return ScanFileState(
                path=str(row["path"]),
                provider=str(row["provider"]),
                size=int(row["size"]),
                mtime_ns=int(row["mtime_ns"]),
                resume_offset=int(row["resume_offset"]),
                guard_hash=int(row["guard_hash"]),
                parser_state=state,
                parser_version=int(row["parser_version"]),
                last_scanned_at=int(row["last_scanned_at"]),
            )
        finally:
            conn.close()

    def get_all_scan_files(self, provider: Optional[str] = None) -> Dict[str, ScanFileState]:
        conn = self._connect()
        try:
            if provider:
                rows = conn.execute("SELECT * FROM scan_files WHERE provider = ?", (provider,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM scan_files").fetchall()
            out: Dict[str, ScanFileState] = {}
            for row in rows:
                try:
                    state = json.loads(row["parser_state"] or "{}")
                    if not isinstance(state, dict):
                        state = {}
                except ValueError:
                    state = {}
                out[str(row["path"])] = ScanFileState(
                    path=str(row["path"]),
                    provider=str(row["provider"]),
                    size=int(row["size"]),
                    mtime_ns=int(row["mtime_ns"]),
                    resume_offset=int(row["resume_offset"]),
                    guard_hash=int(row["guard_hash"]),
                    parser_state=state,
                    parser_version=int(row["parser_version"]),
                    last_scanned_at=int(row["last_scanned_at"]),
                )
            return out
        finally:
            conn.close()

    def commit_scan_batch(
        self,
        events: Iterable[TokenEvent],
        file_states: Sequence[ScanFileState],
    ) -> int:
        """Atomically persist token events + updated file watermarks."""
        conn = self._connect()
        count = 0
        try:
            with conn:
                for ev in events:
                    # Streaming messages (e.g. Claude Code) may emit multiple
                    # partial usage updates sharing the same event_id; keep the
                    # highest token counts without double counting.
                    conn.execute(
                        """
                        INSERT INTO token_events (
                            event_id, provider, route, source_dir, session_id, ts,
                            model, project, client, input_tokens, output_tokens,
                            cache_read_tokens, cache_write_tokens, cache_write_1h_tokens,
                            reasoning_tokens, speed_tier, reported_cost_usd, time_precision
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(event_id) DO UPDATE SET
                            ts = CASE WHEN excluded.ts > 0 THEN MAX(token_events.ts, excluded.ts) ELSE token_events.ts END,
                            model = CASE WHEN excluded.model != 'unknown' THEN excluded.model ELSE token_events.model END,
                            project = CASE WHEN excluded.project != '' THEN excluded.project ELSE token_events.project END,
                            input_tokens = MAX(token_events.input_tokens, excluded.input_tokens),
                            output_tokens = MAX(token_events.output_tokens, excluded.output_tokens),
                            cache_read_tokens = MAX(token_events.cache_read_tokens, excluded.cache_read_tokens),
                            cache_write_tokens = MAX(token_events.cache_write_tokens, excluded.cache_write_tokens),
                            cache_write_1h_tokens = MAX(token_events.cache_write_1h_tokens, excluded.cache_write_1h_tokens),
                            reasoning_tokens = MAX(token_events.reasoning_tokens, excluded.reasoning_tokens),
                            speed_tier = CASE WHEN excluded.speed_tier != 'standard' THEN excluded.speed_tier ELSE token_events.speed_tier END,
                            reported_cost_usd = COALESCE(excluded.reported_cost_usd, token_events.reported_cost_usd)
                        """,
                        (
                            ev.event_id,
                            ev.provider,
                            ev.route,
                            ev.source_dir,
                            ev.session_id,
                            ev.ts,
                            ev.model,
                            ev.project,
                            ev.client,
                            ev.input_tokens,
                            ev.output_tokens,
                            ev.cache_read_tokens,
                            ev.cache_write_tokens,
                            ev.cache_write_1h_tokens,
                            ev.reasoning_tokens,
                            ev.speed_tier,
                            ev.reported_cost_usd,
                            ev.time_precision,
                        ),
                    )
                    count += 1

                for fs in file_states:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO scan_files (
                            path, provider, size, mtime_ns, resume_offset,
                            guard_hash, parser_state, parser_version, last_scanned_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            fs.path,
                            fs.provider,
                            fs.size,
                            fs.mtime_ns,
                            fs.resume_offset,
                            fs.guard_hash,
                            json.dumps(fs.parser_state, separators=(",", ":")),
                            fs.parser_version,
                            fs.last_scanned_at,
                        ),
                    )
        finally:
            conn.close()
        return count

    def query_token_events(
        self,
        *,
        since_ts: int,
        until_ts: Optional[int] = None,
        providers: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            clauses = ["ts >= ?"]
            params: List[Any] = [since_ts]
            if until_ts is not None:
                clauses.append("ts <= ?")
                params.append(until_ts)
            if providers is not None:
                if not providers:
                    return []
                placeholders = ",".join("?" for _ in providers)
                clauses.append(f"provider IN ({placeholders})")
                params.extend(providers)
            where = " AND ".join(clauses)
            rows = conn.execute(
                f"""
                SELECT event_id, provider, route, source_dir, session_id, ts,
                       model, project, client, input_tokens, output_tokens,
                       cache_read_tokens, cache_write_tokens, cache_write_1h_tokens,
                       reasoning_tokens, speed_tier, reported_cost_usd, time_precision
                FROM token_events
                WHERE {where}
                ORDER BY ts ASC
                """,
                params,
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def provider_event_counts(self) -> Dict[str, int]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT provider, COUNT(*) AS cnt FROM token_events GROUP BY provider"
            ).fetchall()
            return {str(r["provider"]): int(r["cnt"]) for r in rows}
        finally:
            conn.close()

    def prune_older_than(self, retention_days: int, now_ts: Optional[int] = None) -> Tuple[int, int]:
        """Drop events and quota samples older than `retention_days` (0 = unlimited)."""
        if retention_days <= 0:
            return 0, 0
        cutoff = (now_ts if now_ts is not None else int(time.time())) - retention_days * 86400
        conn = self._connect()
        try:
            with conn:
                c1 = conn.execute("DELETE FROM token_events WHERE ts > 0 AND ts < ?", (cutoff,)).rowcount
                c2 = conn.execute("DELETE FROM quota_samples WHERE ts > 0 AND ts < ?", (cutoff,)).rowcount
            return int(c1 or 0), int(c2 or 0)
        finally:
            conn.close()

    def clear_token_history(self) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM token_events")
                conn.execute("DELETE FROM scan_files")
        finally:
            conn.close()
