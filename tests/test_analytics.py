"""Unit tests for Omameter Analytics (Phase 1 quota history + Phase 2 local token & cost analytics).

All tests run strictly in temporary directories and never read or modify real user data.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.analytics.cli import _handle_command
from backend.analytics.db import AnalyticsDB, QuotaSample, TokenEvent
from backend.analytics.parsers import (
    discover_harness_roots,
    scan_antigravity_db,
    scan_claude_file,
    scan_codex_file,
    scan_opencode_db,
    scan_opencode_legacy_json,
    t3_antigravity_available,
)
from backend.analytics.pricing import PricingEngine, load_analytics_config, normalize_analytics_config
from backend.analytics.service import AnalyticsService
from backend.models import Account, MetricLine, MetricPeriod, ProviderSnapshot
from backend.store import OmameterStore


def _pb_varint(n: int) -> bytes:
    out = bytearray()
    v = int(n)
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _pb_tag(field_num: int, wire_type: int) -> bytes:
    return _pb_varint((field_num << 3) | wire_type)


def _pb_varint_field(field_num: int, value: int) -> bytes:
    return _pb_tag(field_num, 0) + _pb_varint(value)


def _pb_bytes_field(field_num: int, payload: bytes) -> bytes:
    return _pb_tag(field_num, 2) + _pb_varint(len(payload)) + payload


def _build_antigravity_db(
    db_path: Path,
    *,
    ts: int,
    model: str = "gemini-3-pro",
    input_tokens: int = 4000,
    output_tokens: int = 900,
    cache_read: int = 1200,
    reasoning: int = 300,
    request_id: str = "req-ag-001",
    project_uri: str = "file:///home/dev/work/omameter",
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE steps (idx INTEGER, step_type INTEGER, metadata BLOB)")
    conn.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB)")
    conn.execute("CREATE TABLE trajectory_metadata_blob (data BLOB)")

    # Step metadata: field 1 -> nested timestamp (field 1 = seconds), step_type = 15
    step_meta = _pb_bytes_field(1, _pb_varint_field(1, ts))
    conn.execute("INSERT INTO steps (idx, step_type, metadata) VALUES (?, ?, ?)", (0, 15, step_meta))

    # Usage blob: 2=input, 3=output, 5=cache_read, 9=reasoning
    usage_blob = (
        _pb_varint_field(2, input_tokens)
        + _pb_varint_field(3, output_tokens)
        + _pb_varint_field(5, cache_read)
        + _pb_varint_field(9, reasoning)
    )
    req_entry = _pb_bytes_field(1, b"request_id") + _pb_bytes_field(2, request_id.encode("utf-8"))
    gen_inner = (
        _pb_bytes_field(4, usage_blob)
        + _pb_bytes_field(19, model.encode("utf-8"))
        + _pb_bytes_field(20, req_entry)
    )
    gen_blob = _pb_bytes_field(1, gen_inner)
    conn.execute("INSERT INTO gen_metadata (idx, data) VALUES (?, ?)", (0, gen_blob))

    traj_blob = _pb_bytes_field(7, project_uri.encode("utf-8"))
    conn.execute("INSERT INTO trajectory_metadata_blob (data) VALUES (?)", (traj_blob,))
    conn.commit()
    conn.close()


class TestAnalyticsDB(unittest.TestCase):
    def test_quota_and_token_ledger_upsert_and_prune(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "analytics.db"
            db = AnalyticsDB(db_path)

            # Restrictive permissions on SQLite DB
            self.assertEqual(os.stat(db_path).st_mode & 0o777, 0o600)

            now = 1_750_000_000
            db.insert_quota_samples(
                [
                    QuotaSample(
                        ts=now - 3600,
                        account_id="acc-1",
                        provider="claude",
                        account_name="work",
                        metric_key="session",
                        metric_title="Session",
                        window_kind="session",
                        used_pct=25.0,
                        used_amount=25.0,
                        limit_amount=100.0,
                        unit="%",
                        resets_at="2026-06-01T15:00:00Z",
                    ),
                    QuotaSample(
                        ts=now,
                        account_id="acc-1",
                        provider="claude",
                        account_name="work",
                        metric_key="session",
                        metric_title="Session",
                        window_kind="session",
                        used_pct=60.0,
                        used_amount=60.0,
                        limit_amount=100.0,
                        unit="%",
                        resets_at="2026-06-01T15:00:00Z",
                    ),
                ]
            )
            pts = db.query_quota_samples(since_ts=now - 7200, until_ts=now, account_id="acc-1", metric_key="session")
            self.assertEqual(len(pts), 2)
            self.assertEqual(pts[-1]["used_pct"], 60.0)

            # Streaming update upsert keeps higher output token count for the same event_id
            ev1 = TokenEvent(
                event_id="ev-stream-1",
                provider="claude",
                route="anthropic",
                source_dir="/tmp/claude",
                session_id="sess-1",
                ts=now - 10,
                model="claude-sonnet-4-6",
                project="/tmp/proj",
                client="Claude Code",
                input_tokens=1000,
                output_tokens=100,
                cache_read_tokens=500,
                cache_write_tokens=0,
                cache_write_1h_tokens=0,
                reasoning_tokens=0,
            )
            ev2 = TokenEvent(
                event_id="ev-stream-1",
                provider="claude",
                route="anthropic",
                source_dir="/tmp/claude",
                session_id="sess-1",
                ts=now,
                model="claude-sonnet-4-6",
                project="/tmp/proj",
                client="Claude Code",
                input_tokens=1000,
                output_tokens=450,
                cache_read_tokens=500,
                cache_write_tokens=0,
                cache_write_1h_tokens=0,
                reasoning_tokens=0,
            )
            db.commit_scan_batch([ev1], [])
            db.commit_scan_batch([ev2], [])
            rows = db.query_token_events(since_ts=now - 60, until_ts=now + 60)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["output_tokens"], 450)

            # Retention pruning
            old_ev = TokenEvent(
                event_id="ev-old",
                provider="codex",
                route="openai",
                source_dir="/tmp/codex",
                session_id="sess-old",
                ts=now - 200 * 86400,
                model="gpt-5",
                project="",
                client="Codex CLI",
                input_tokens=10,
                output_tokens=10,
                cache_read_tokens=0,
                cache_write_tokens=0,
                cache_write_1h_tokens=0,
                reasoning_tokens=0,
            )
            db.commit_scan_batch([old_ev], [])
            db.prune_older_than(90, now_ts=now)
            remaining = db.query_token_events(since_ts=0, until_ts=now + 60)
            self.assertEqual([r["event_id"] for r in remaining], ["ev-stream-1"])


class TestPricingEngine(unittest.TestCase):
    def test_bundled_tiers_speed_cache_aliases_and_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_analytics_config(Path(tmp) / "analytics.json")
            engine = PricingEngine(config=cfg, litellm_cache_path=Path(tmp) / "litellm.json")

            # Standard Claude Sonnet 4.6 rate (<200k prompt): $3/M in, $15/M out, $0.30/M cache read, $3.75/M 5m write
            r1 = engine.evaluate(
                model="claude-sonnet-4-6",
                route="anthropic",
                input_tokens=40_000,
                output_tokens=20_000,
                cache_read_tokens=100_000,
                cache_write_tokens=20_000,
            )
            self.assertTrue(r1.priced)
            self.assertAlmostEqual(r1.cost_usd, 0.12 + 0.30 + 0.03 + 0.075, places=5)
            self.assertGreater(r1.cache_savings_usd, 0.25)

            # Long-context (>200k prompt) tier: 2x input/cache, 1.5x output
            r_long = engine.evaluate(
                model="claude-sonnet-4-6",
                route="anthropic",
                input_tokens=100_000,
                output_tokens=50_000,
                cache_read_tokens=200_000,
                cache_write_tokens=100_000,
            )
            self.assertTrue(r_long.priced)
            self.assertAlmostEqual(r_long.cost_usd, 0.60 + 1.125 + 0.12 + 0.75, places=5)

            # 1h cache write rate ($6/M on Sonnet 4.6)
            r_1h = engine.evaluate(
                model="claude-sonnet-4-6",
                route="anthropic",
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=100_000,
                cache_write_1h_tokens=100_000,
            )
            self.assertAlmostEqual(r_1h.cost_usd, 0.60, places=5)

            # Fast speed multiplier (6x on Opus 4.6)
            r_std = engine.evaluate(
                model="claude-opus-4-6",
                route="anthropic",
                input_tokens=100_000,
                output_tokens=10_000,
                cache_read_tokens=0,
                cache_write_tokens=0,
                speed_tier="standard",
            )
            r_fast = engine.evaluate(
                model="claude-opus-4-6",
                route="anthropic",
                input_tokens=100_000,
                output_tokens=10_000,
                cache_read_tokens=0,
                cache_write_tokens=0,
                speed_tier="fast",
            )
            self.assertAlmostEqual(r_fast.cost_usd, r_std.cost_usd * 6.0, places=5)

            # Unpriced model
            r_unk = engine.evaluate(
                model="my-private-model-x1",
                route="anthropic",
                input_tokens=50_000,
                output_tokens=10_000,
                cache_read_tokens=0,
                cache_write_tokens=0,
            )
            self.assertFalse(r_unk.priced)
            self.assertIsNone(r_unk.cost_usd)

            # Model alias ("Map to") resolves pricing via target model
            cfg["modelAliases"]["my-private-model-x1"] = "claude-sonnet-4-6"
            engine2 = PricingEngine(config=cfg, litellm_cache_path=Path(tmp) / "litellm.json")
            r_aliased = engine2.evaluate(
                model="my-private-model-x1",
                route="anthropic",
                input_tokens=100_000,
                output_tokens=10_000,
                cache_read_tokens=0,
                cache_write_tokens=0,
            )
            self.assertTrue(r_aliased.priced)
            self.assertEqual(r_aliased.resolved_model, "claude-sonnet-4-6")

            # Custom user price override beats catalog
            cfg["priceOverrides"]["custom-model-z"] = {
                "inputPerM": 2.0,
                "outputPerM": 8.0,
                "cacheReadPerM": 0.2,
                "cacheWritePerM": 2.5,
            }
            engine3 = PricingEngine(config=cfg, litellm_cache_path=Path(tmp) / "litellm.json")
            r_ov = engine3.evaluate(
                model="custom-model-z",
                route="custom",
                input_tokens=500_000,
                output_tokens=250_000,
                cache_read_tokens=0,
                cache_write_tokens=0,
            )
            self.assertTrue(r_ov.priced)
            self.assertAlmostEqual(r_ov.cost_usd, 1.0 + 2.0, places=5)


class TestLocalHarnessParsers(unittest.TestCase):
    def test_claude_incremental_resume_and_guard_hash_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "sess-claude.jsonl"
            line1 = json.dumps(
                {
                    "type": "assistant",
                    "sessionId": "sess-c1",
                    "cwd": "/work/app",
                    "timestamp": "2026-06-01T10:00:00Z",
                    "message": {
                        "id": "msg_01",
                        "model": "claude-opus-5-5",
                        "usage": {
                            "input_tokens": 500,
                            "output_tokens": 200,
                            "cache_read_input_tokens": 10_000,
                            "cache_creation_input_tokens": 1_000,
                            "cache_creation": {"ephemeral_1h_input_tokens": 400},
                            "speed": "fast",
                        },
                    },
                }
            )
            f.write_text(line1 + "\n", encoding="utf-8")

            res1 = scan_claude_file(f, tmp, None, 1_750_000_000)
            self.assertIsNotNone(res1)
            events1, state1 = res1
            self.assertEqual(len(events1), 1)
            self.assertEqual(events1[0].cache_write_1h_tokens, 400)
            self.assertEqual(events1[0].speed_tier, "fast")
            self.assertGreater(state1.resume_offset, 0)

            # Unchanged file -> skipped (None)
            self.assertIsNone(scan_claude_file(f, tmp, state1, 1_750_000_010))

            # Append second line -> incremental scan returns ONLY line 2
            line2 = json.dumps(
                {
                    "type": "assistant",
                    "sessionId": "sess-c1",
                    "cwd": "/work/app",
                    "timestamp": "2026-06-01T10:05:00Z",
                    "message": {
                        "id": "msg_02",
                        "model": "claude-sonnet-5-5",
                        "usage": {"input_tokens": 300, "output_tokens": 150},
                    },
                }
            )
            with f.open("a", encoding="utf-8") as fh:
                fh.write(line2 + "\n")

            res2 = scan_claude_file(f, tmp, state1, 1_750_000_020)
            self.assertIsNotNone(res2)
            events2, state2 = res2
            self.assertEqual(len(events2), 1)
            self.assertEqual(events2[0].model, "claude-sonnet-5-5")

            # Rewrite bytes inside the 64-byte guard window before resume_offset -> triggers full rescan from offset 0
            line2_rewritten = line2.replace("150", "999")
            f.write_text(line1 + "\n" + line2_rewritten + "\n" + line2.replace("msg_02", "msg_03") + "\n", encoding="utf-8")
            res3 = scan_claude_file(f, tmp, state2, 1_750_000_030)
            self.assertIsNotNone(res3)
            events3, _ = res3
            self.assertEqual(len(events3), 3)

    def test_codex_parser_and_incremental_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "rollout-codex.jsonl"
            lines = [
                json.dumps(
                    {
                        "timestamp": "2026-06-01T10:00:00Z",
                        "type": "session_meta",
                        "payload": {"id": "codex-1", "cwd": "/work/omameter", "originator": "codex-cli", "timestamp": "2026-06-01T10:00:00Z"},
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-06-01T10:00:05Z",
                        "type": "turn_context",
                        "payload": {"model": "gpt-6.2-sol", "cwd": "/work/omameter"},
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-06-01T10:01:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 12_000,
                                    "cached_input_tokens": 10_000,
                                    "cache_write_input_tokens": 500,
                                    "output_tokens": 600,
                                    "reasoning_output_tokens": 200,
                                },
                                "total_token_usage": {"input_tokens": 12_000, "output_tokens": 600},
                            },
                        },
                    }
                ),
            ]
            f.write_text("\n".join(lines) + "\n", encoding="utf-8")

            res1 = scan_codex_file(f, tmp, None, 1_750_000_000)
            self.assertIsNotNone(res1)
            events1, state1 = res1
            self.assertEqual(len(events1), 1)
            ev = events1[0]
            self.assertEqual(ev.model, "gpt-6.2-sol")
            self.assertEqual(ev.input_tokens, 1500)  # 12000 - 10000 - 500
            self.assertEqual(ev.cache_read_tokens, 10_000)
            self.assertEqual(ev.cache_write_tokens, 500)
            self.assertEqual(ev.output_tokens, 600)
            self.assertEqual(ev.reasoning_tokens, 200)

            # Duplicate fingerprint appended -> deduplicated
            with f.open("a", encoding="utf-8") as fh:
                fh.write(lines[-1] + "\n")
            res2 = scan_codex_file(f, tmp, state1, 1_750_000_010)
            self.assertIsNotNone(res2)
            self.assertEqual(len(res2[0]), 0)

    def test_opencode_v1_v2_dedup_and_legacy_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "opencode.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT)")
            conn.execute("CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT, model TEXT)")
            conn.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT)")
            conn.execute("CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, type TEXT, created_at INTEGER, data TEXT)")

            conn.execute("INSERT INTO session VALUES ('s1', '/work/proj1')")
            conn.execute("INSERT INTO session_v2 VALUES ('s2', '/work/proj2', 'moonshotai/kimi-k3.1-pro')")

            # Same message ID in both v1 and v2 -> should be emitted once
            v2_msg = json.dumps(
                {
                    "role": "assistant",
                    "model": {"id": "moonshotai/kimi-k3.1-pro", "providerID": "openrouter"},
                    "tokens": {"input": 2000, "output": 400, "cache": {"read": 8000, "write": 500}},
                    "cost": 0.042,
                    "time": {"created": 1_750_000_000_000},
                }
            )
            conn.execute("INSERT INTO session_message VALUES ('m_dup', 's2', 'assistant', 1750000000000, ?)", (v2_msg,))
            conn.execute("INSERT INTO message VALUES ('m_dup', 's2', 1750000000000, ?)", (v2_msg,))

            v1_only = json.dumps(
                {
                    "role": "assistant",
                    "modelID": "claude-sonnet-4-6",
                    "providerID": "anthropic",
                    "tokens": {"input": 1000, "output": 300, "cache": {"read": 5000, "write": 0}},
                    "time": {"created": 1_750_000_100_000},
                }
            )
            conn.execute("INSERT INTO message VALUES ('m_v1', 's1', 1750000100000, ?)", (v1_only,))
            conn.commit()
            conn.close()

            res = scan_opencode_db(db_path, tmp, None, 1_750_000_200)
            self.assertIsNotNone(res)
            events, _ = res
            self.assertEqual(len(events), 2)
            by_model = {e.model: e for e in events}
            self.assertIn("moonshotai/kimi-k3.1-pro", by_model)
            self.assertAlmostEqual(by_model["moonshotai/kimi-k3.1-pro"].reported_cost_usd or 0.0, 0.042, places=5)
            self.assertIn("claude-sonnet-4-6", by_model)

            # Legacy JSON message
            leg = Path(tmp) / "msg_legacy.json"
            leg.write_text(v1_only, encoding="utf-8")
            res_leg = scan_opencode_legacy_json(leg, tmp, None, 1_750_000_300)
            self.assertIsNotNone(res_leg)
            self.assertEqual(len(res_leg[0]), 1)

    def test_antigravity_sqlite_protobuf_parser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "conv-1.db"
            _build_antigravity_db(db_path, ts=1_750_000_000)

            res = scan_antigravity_db(db_path, tmp, None, 1_750_000_100)
            self.assertIsNotNone(res)
            events, _ = res
            self.assertEqual(len(events), 1)
            ev = events[0]
            self.assertEqual(ev.provider, "antigravity")
            self.assertEqual(ev.model, "gemini-3-pro")
            self.assertEqual(ev.input_tokens, 4000)
            self.assertEqual(ev.output_tokens, 900)
            self.assertEqual(ev.cache_read_tokens, 1200)
            self.assertEqual(ev.reasoning_tokens, 300)
            self.assertEqual(ev.project, "/home/dev/work/omameter")

    def test_never_reads_t3_directories_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "fake_user_home"
            fake_home.mkdir()
            # Even if ~/.t3 exists on disk, the default discovery must NEVER include it
            (fake_home / ".t3" / "userdata" / "providers" / "antigravity" / "abc").mkdir(parents=True)
            roots = discover_harness_roots(home=fake_home, env={})
            for harness, paths in roots.items():
                for p in paths:
                    rel = str(p.relative_to(fake_home))
                    self.assertNotIn(".t3", rel, f"Harness {harness} must never depend on ~/.t3: {rel}")

    def test_antigravity_acp_dir_is_a_default_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            roots = discover_harness_roots(home=fake_home, env={})
            self.assertIn(fake_home / ".gemini" / "antigravity-acp" / "conversations", roots["antigravity"])

    def test_t3_antigravity_opt_in_adds_only_instance_conversation_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            self.assertFalse(t3_antigravity_available(fake_home))
            base = fake_home / ".t3" / "userdata" / "providers" / "antigravity"
            for inst in ("aaa", "bbb"):
                (base / inst / "antigravity-acp" / "conversations").mkdir(parents=True)
            (base / "stray.txt").write_text("x", encoding="utf-8")
            (fake_home / ".t3" / "userdata" / "providers" / "codex").mkdir(parents=True)
            self.assertTrue(t3_antigravity_available(fake_home))

            roots = discover_harness_roots(home=fake_home, env={}, include_t3_antigravity=True)
            t3_roots = [p for p in roots["antigravity"] if ".t3" in str(p)]
            self.assertEqual(
                t3_roots,
                [base / inst / "antigravity-acp" / "conversations" for inst in ("aaa", "bbb")],
            )
            for harness in ("claude", "codex", "opencode"):
                self.assertFalse(any(".t3" in str(p) for p in roots[harness]))

    def test_include_t3_antigravity_config_is_a_strict_bool(self) -> None:
        self.assertFalse(normalize_analytics_config({})["includeT3Antigravity"])
        self.assertFalse(normalize_analytics_config({"includeT3Antigravity": "yes"})["includeT3Antigravity"])
        self.assertTrue(normalize_analytics_config({"includeT3Antigravity": True})["includeT3Antigravity"])

    def test_t3_antigravity_opt_in_scans_and_opt_out_forgets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake_home = base / "home"
            cfg_dir = base / "cfg"
            cache_dir = base / "cache"
            now_ts = int(datetime.now(timezone.utc).timestamp())
            conv = fake_home / ".t3" / "userdata" / "providers" / "antigravity" / "inst1" / "antigravity-acp" / "conversations"
            conv.mkdir(parents=True)
            _build_antigravity_db(conv / "c1.db", ts=now_ts - 600, model="gemini-4-neon", request_id="req-t3")
            acp = fake_home / ".gemini" / "antigravity-acp" / "conversations"
            acp.mkdir(parents=True)
            _build_antigravity_db(acp / "c2.db", ts=now_ts - 600, model="gemini-3-pro", request_id="req-acp")

            store = OmameterStore(config_dir=cfg_dir, cache_dir=cache_dir)
            store.save_settings()
            svc = AnalyticsService(
                db_path=base / "analytics.db",
                config_path=base / "analytics.json",
                omameter_config_dir=cfg_dir,
                omameter_cache_dir=cache_dir,
                litellm_cache_path=base / "litellm.json",
                home=fake_home,
                env={},
            )
            svc.set_local_analytics_enabled(True)

            def models() -> set:
                rep = svc.build_report(period="today", now_ts=now_ts)
                return {m["model"] for m in rep["models"]}

            def ag_row() -> dict:
                rep = svc.build_report(period="today", now_ts=now_ts)
                return {p["id"]: p for p in rep["providers"]}["antigravity"]

            svc.scan_local_harnesses()
            self.assertEqual(models(), {"gemini-3-pro"})
            self.assertTrue(ag_row()["t3Available"])
            self.assertFalse(ag_row()["t3Enabled"])

            resp = _handle_command(svc, {"id": 1, "cmd": "update_config", "patch": {"includeT3Antigravity": True}})
            self.assertTrue(resp["ok"])
            self.assertTrue(resp["config"]["includeT3Antigravity"])
            svc.scan_local_harnesses()
            self.assertEqual(models(), {"gemini-3-pro", "gemini-4-neon"})
            self.assertTrue(ag_row()["t3Enabled"])

            svc.update_config({"includeT3Antigravity": False})
            self.assertEqual(models(), {"gemini-3-pro"})
            self.assertEqual(
                [k for k in svc.db.get_all_scan_files() if ".t3" in k],
                [],
            )


class TestAnalyticsServiceAndCli(unittest.TestCase):
    def test_phase1_phase2_opt_in_since_reset_and_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake_home = base / "home"
            cfg_dir = base / "omameter_cfg"
            cache_dir = base / "omameter_cache"
            now_dt = datetime.now(timezone.utc).replace(microsecond=0)
            now_ts = int(now_dt.timestamp())

            # Populate fake installed harnesses under fake_home (Claude + Antigravity)
            claude_dir = fake_home / ".claude" / "projects" / "demo"
            claude_dir.mkdir(parents=True)
            (claude_dir / "s1.jsonl").write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": "s-claude",
                        "cwd": "/work/omameter",
                        "timestamp": (now_dt - timedelta(hours=1)).isoformat(),
                        "message": {
                            "id": "m-in-window",
                            "model": "claude-sonnet-4-6",
                            "usage": {"input_tokens": 100_000, "output_tokens": 20_000, "cache_read_input_tokens": 400_000},
                        },
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": "s-claude-old",
                        "cwd": "/work/omameter",
                        "timestamp": (now_dt - timedelta(hours=12)).isoformat(),
                        "message": {
                            "id": "m-before-window",
                            "model": "claude-sonnet-4-6",
                            "usage": {"input_tokens": 50_000, "output_tokens": 10_000},
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            ag_dir = fake_home / ".gemini" / "antigravity" / "conversations"
            ag_dir.mkdir(parents=True)
            _build_antigravity_db(ag_dir / "c1.db", ts=now_ts - 1800)

            # Seed Omameter store with a Claude account + live 5h session quota + cloud spend line
            store = OmameterStore(config_dir=cfg_dir, cache_dir=cache_dir)
            acc = Account(id="acc-claude-1", kind="claude", label="Max")
            store.accounts.append(acc)
            resets_at = (now_dt + timedelta(hours=2)).isoformat()
            snap = ProviderSnapshot(
                account_id=acc.id,
                card_id="claude",
                kind="claude",
                lines=[
                    MetricLine(
                        key="session",
                        title="Session",
                        line_type="progress",
                        kind="percent",
                        used=45.0,
                        limit=100.0,
                        resets_at=resets_at,
                        period_duration_ms=MetricPeriod.SESSION_MS,
                    ),
                    MetricLine(
                        key="extra_usage",
                        title="Extra Usage",
                        line_type="progress",
                        kind="dollars",
                        used=12.5,
                        limit=50.0,
                        currency_code="USD",
                        period_duration_ms=MetricPeriod.MONTH_MS,
                    ),
                ],
            )
            store.snapshots[acc.id] = snap
            store.save_accounts()
            store.save_snapshots()
            store.save_settings()  # localTokenAnalytics defaults to False!

            svc = AnalyticsService(
                db_path=base / "analytics.db",
                config_path=base / "analytics.json",
                omameter_config_dir=cfg_dir,
                omameter_cache_dir=cache_dir,
                litellm_cache_path=base / "litellm.json",
                home=fake_home,
                env={},
            )

            # Phase 1 works even when localTokenAnalytics is OFF
            recorded = svc.record_snapshot(acc, snap, now=now_dt)
            self.assertEqual(recorded, 2)
            scan_off = svc.scan_local_harnesses()
            self.assertFalse(scan_off["enabled"])
            rep_off = svc.build_report(period="today", now_ts=now_ts)
            self.assertFalse(rep_off["localEnabled"])
            self.assertEqual(rep_off["summary"]["totalTokens"], 0)
            self.assertEqual(len(rep_off["quotas"]["live"]), 2)
            self.assertEqual(len(rep_off["quotas"]["cloudSpend"]), 1)
            self.assertAlmostEqual(rep_off["summary"]["cloudSpendUsd"], 12.5, places=2)
            self.assertEqual(len(rep_off["quotas"]["selectedHistory"]["points"]), 1)

            # Opt-in Phase 2 via CLI command handler (returns immediately without blocking)
            en_resp = _handle_command(svc, {"id": 1, "cmd": "set_local_enabled", "enabled": True})
            self.assertTrue(en_resp["ok"])
            self.assertTrue(en_resp["localEnabled"])

            # Run scan command with streaming progress callback
            progress_events: list[dict] = []
            scan_resp = _handle_command(
                svc,
                {"id": 11, "cmd": "scan", "force": False},
                progress_cb=progress_events.append,
            )
            self.assertTrue(scan_resp["ok"])
            self.assertGreaterEqual(len(progress_events), 2)
            self.assertEqual(progress_events[-1]["phase"], "finalizing")
            self.assertGreaterEqual(scan_resp["scan"]["totalFiles"], 2)

            # Set monthly plan cost for Claude -> verify ROI calculation
            plan_resp = _handle_command(svc, {"id": 2, "cmd": "set_plan_price", "provider": "claude", "monthlyUsd": 100})
            self.assertTrue(plan_resp["ok"])

            # Full report includes both Claude and Antigravity
            rep_all = svc.build_report(period="30d", now_ts=now_ts)
            self.assertTrue(rep_all["localEnabled"])
            by_prov = {p["id"]: p for p in rep_all["providers"]}
            self.assertGreater(by_prov["claude"]["totalTokens"], 500_000)
            self.assertEqual(by_prov["antigravity"]["totalTokens"], 4000 + 900 + 1200)
            self.assertEqual(by_prov["claude"]["planMonthlyUsd"], 100.0)

            # "Since Reset" window (5h ending at now+2h -> starts at now-3h) includes only the 1h-ago event, NOT the 12h-ago event!
            reset_id = f"since_reset:{acc.id}:session"
            rep_sr = svc.build_report(period=reset_id, provider="claude", now_ts=now_ts)
            self.assertEqual(rep_sr["summary"]["requests"], 1)
            self.assertEqual(rep_sr["summary"]["inputTokens"], 100_000)


if __name__ == "__main__":
    unittest.main()
